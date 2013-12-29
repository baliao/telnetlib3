import collections
import traceback
import datetime
import logging
import socket
import time
import sys

import asyncio

from . import telsh
from . import telopt
from . import dns

__all__ = ('TelnetServer',)


class TelnetServer(asyncio.protocols.Protocol):
    """
        The begin_negotiation() method is called on-connect,
        displaying the login banner, and indicates desired options.

            The default implementations sends only: iac(DO, TTYPE).

        The negotiation DO-TTYPE is twofold: provide at least one option to
        negotiate to test the remote iac interpreter. If the remote end
        replies in the affirmitive, then ``request_advanced_opts()`` is
        called. The default implementation prefers remote line editing,
        kludge mode, and finally default NVT half-duplex local-line mode.
    """
    #: mininum on-connect time to wait for client-initiated negotiation options
    CONNECT_MINWAIT = 2.00
    #: maximum on-connect time to wait for client-initiated negotiation options
    #  before negotiation is considered 'final'. some telnet clients will fail
    #  to acknowledge bi-directionally, appearing as a timeout, while others
    #  are simply on very high-latency links.
    CONNECT_MAXWAIT = 6.00
    #: timer length for ``check_telopt_negotiation`` re-scheduling
    CONNECT_DEFERED = 0.1
    TTYPE_LOOPMAX = 8
    default_env = {
        'COLUMNS': '80',
        'LINES': '24',
        'USER': 'unknown',
        'TERM': 'unknown',
        'CHARSET': 'ascii',
        'PS1': '%s-%v %# ',
        'PS2': '> ',
        'TIMEOUT': '5',
    }

    readonly_env = ['USER', 'HOSTNAME', 'UID', 'REMOTE_IP',
                    'REMOTE_HOST', 'REMOTE_PORT', ]

    def __init__(self, shell=telsh.Telsh,
                 stream=telopt.TelnetStream,
                 encoding='utf8', log=logging):
        self.log = log
        self._shell_factory = shell
        self._stream_factory = stream
        self._default_encoding = encoding
        self._loop = asyncio.get_event_loop()

        #: session environment as S.env['key'], defaults empty string value
        self._client_env = collections.defaultdict(str, **self.default_env)

        #: default environment is server-preferred encoding if un-negotiated.
        self._client_env['CHARSET'] = encoding

        #: 'ECHO off' set for clients capable of remote line editing (fastest).
        self.fast_edit = True

        #: toggled when transport is shutting down
        self._closing = False

        #: datetime of last byte received
        self._last_received = None

        #: datetime of connection made
        self._connected = None

        #: client performed ttype; probably human
        self._advanced = False

        #: prompt sequence '%h' is result of socket.gethostname().
        self._server_name = self._loop.run_in_executor(
            None, socket.gethostname)
        self._server_name.add_done_callback(
            self.after_server_gethostname)

        #: prompt sequence '%H' is result of socket.getfqdn() of '%h'.
        self._server_fqdn = asyncio.Future()

        #: server disconnects client after self.env['TIMEOUT'] (in minutes).
        self._timeout = asyncio.Future()

        #: future result stores value of gethostbyaddr(client_ip)
        self._client_host = asyncio.Future()

        #: option negotiation status as a future.  When complete, fires
        # callback ``after_telopt_negotiation``.
        self._telopt_negotiation = asyncio.Future()
        self._telopt_negotiation.add_done_callback(
            self.after_telopt_negotiation)

        #: encoding negotiation status as a future.  When complete, fires
        # callback ``after_encoding_negotiation``.
        self._encoding_negotiation = asyncio.Future()
        self._encoding_negotiation.add_done_callback(
            self.after_encoding_negotiation)

# XXX
    def pause_writing(self):
        self.log.debug('high watermark reached')
#        self.stream.handle_xoff(None)

    def resume_writing(self):
        self.log.debug('low watermark reached')
#        self.stream.handle_xon(None)
# XXX

    def connection_made(self, transport):
        """ Receive a new telnet client connection.

            A ``telopt.TelnetStream`` instance is created for reading on
            the transport as class attribute ``stream``, and various IAC,
            SLC, and extended callbacks are registered to local handlers.

            A ``TelnetShell`` instance is created for writing on
            the transport as ``shell``. It receives in-band data
            from the telnet transport, providing line editing and
            command line processing.

            ``begin_negotiation()`` is fired after connection is
            registered.
        """
        self.transport = transport
        self._client_ip, self._client_port = (
            transport.get_extra_info('peername'))
        self.stream = self._stream_factory(
            transport=transport, server=True, log=self.log)
        self.shell = self._shell_factory(server=self, log=self.log)
        self.set_stream_callbacks()
        self._last_received = datetime.datetime.now()
        self._connected = datetime.datetime.now()

        # resolve client fqdn (and later, reverse-dns)
        self._client_host = self._loop.run_in_executor(
            None, socket.gethostbyaddr, self._client_ip)
        self._client_host.add_done_callback(self.after_client_lookup)

        # begin connect-time negotiation
        self._loop.call_soon(self.begin_negotiation)

        self.log.info('Connection from {}:{}'.format(
            self.client_ip, self.client_port))

        self.env_update({
            'REMOTE_IP': self.client_ip,
            'REMOTE_PORT': str(self.client_port),
            'REMOTE_HOST': self.client_ip  # override by dns, later
            })

    def set_stream_callbacks(self):
        """ XXX Set default iac, slc, and ext callbacks for telnet stream
        """
        stream, server = self.stream, self
        from .slc import SLC_AYT
        from .telopt import (AYT, AO, IP, BRK, SUSP, ABORT, EC, EL, CMD_EOR,
                             TTYPE, TSPEED, XDISPLOC, NEW_ENVIRON, LOGOUT,
                             SNDLOC, CHARSET, NAWS, TM)
        # wire AYT and SLC_AYT (^T) to callback ``handle_ayt()``
        stream.set_iac_callback(AYT, self.handle_are_you_there)
        stream.set_slc_callback(SLC_AYT, self.handle_are_you_there)
        # wire TM to callback ``handle_timing_mark(cmd)``, cmd is one
        # of (DO, DONT, WILL, WONT).
        stream.set_iac_callback(TM, self.handle_timing_mark)

        # wire various 'interrupts', such as AO, IP to
        # ``special_received()``, which forwards as
        # shell editing cmds of SLC equivalents.
        for cmd in (AO, IP, BRK, SUSP, ABORT, EC, EL, CMD_EOR):
            stream.set_iac_callback(cmd, self.special_received)

        # wire extended rfc callbacks for receipt of terminal atributes, etc.
        for (opt, func) in (
                (TTYPE, self.ttype_received),
                (TSPEED, self.tspeed_received),
                (XDISPLOC, self.xdisploc_received),
                (NEW_ENVIRON, self.env_update),
                (NAWS, self.naws_received),
                (LOGOUT, self.logout),
                (SNDLOC, self.sndloc_received),
                (CHARSET, self.charset_received),):
            stream.set_ext_callback(opt, func)

    def begin_negotiation(self):
        """ XXX begin on-connect negotiation.

            A Telnet Server is expected to assert the preferred session
            options immediately after connection.

            The default implementation sends only (DO, TTYPE) and the
            shell prompt. The default ``ttype_received()`` handler fires
            ``request_advanced_opts()``, further requesting more advanced
            negotiations that may otherwise confuse or corrupt output of the
            remote end if it is not equipped with an IAC interpreter (such as
            a network scanner).
        """
        if self._closing:
            self._telopt_negotiation.cancel()
            return
        from .telopt import DO, TTYPE
        self.stream.iac(DO, TTYPE)
        self._loop.call_soon(self.check_telopt_negotiation)
        self.shell.display_prompt()

    def begin_encoding_negotiation(self):
        """ XXX Request bi-directional binary encoding and CHARSET;
            called only if remote end replies affirmitively to (DO, TTYPE).
        """
        from .telopt import WILL, BINARY, DO, CHARSET
        self.stream.iac(WILL, BINARY)
        self.stream.iac(DO, CHARSET)

        self._loop.call_soon(self.check_encoding_negotiation)

    def check_telopt_negotiation(self):
        """ XXX negotiation check-loop, schedules itself for continual callback
            until negotiation is considered final, firing
            ``after_telopt_negotiation`` callback when complete.
        """
        if self._closing:
            self._telopt_negotiation.cancel()
            return

        pots = self.stream.pending_option
        # negotiation completed: all pending values have been replied
        if not any(pots.values()):
            if self.duration > self.CONNECT_MINWAIT:
                self._telopt_negotiation.set_result(self.stream.__repr__())
                return
        # negotiation has gone on long enough, give up and set result,
        # either a very, very slow-to-respond client or a dumb network
        # network scanner.
        elif self.duration > self.CONNECT_MAXWAIT:
            self._telopt_negotiation.set_result(self.stream.__repr__())
            return
        # negotiation not yet complete, check again in CONNECT_DEFERED seconds,
        self._loop.call_later(self.CONNECT_DEFERED,
                              self.check_telopt_negotiation)

    def check_encoding_negotiation(self):
        """ XXX encoding negotiation check-loop, schedules itself for continual
            callback until both outbinary and inbinary has been answered in
            the affirmitive, firing ``after_encoding_negotiation`` callback
            when complete.
        """
        from .telopt import DO, BINARY
        if self._closing:
            self._encoding_negotiation.cancel()
            return

        # encoding negotiation is complete
        if self.outbinary and self.inbinary:
            self.log.debug('outbinary and inbinary negotiated.')
            self._encoding_negotiation.set_result(True)

        # if (WILL, BINARY) requested by begin_negotiation() is answered in
        # the affirmitive, then request (DO, BINARY) to ensure bi-directional
        # transfer of non-ascii characters.
        elif self.outbinary and not self.inbinary and (
                not (DO, BINARY,) in self.stream.pending_option):
            self.log.debug('outbinary=True, requesting inbinary.')
            self.stream.iac(DO, BINARY)
            self._loop.call_later(self.CONNECT_DEFERED,
                                  self.check_encoding_negotiation)

        elif self.duration > self.CONNECT_MAXWAIT:
            # Many IAC interpreters do not differentiate 'local' from 'remote'
            # options -- they are treated equivalently.
            #
            # tintin++ for example, cannot answer "DONT BINARY" after already
            # having sent "WONT BINARY"; it wrongly evaluates all telnet
            # options as single direction, client-host viewpoint, thereby
            # "failing" to negotiate a pending option: it ignores our request,
            # it believes it has already been sent!
            #
            # Note: these kinds of IAC interpreters may be discovered by
            # requesting (DO, ECHO): the client replies (WILL, ECHO),
            # which is proposterous!
            self._encoding_negotiation.set_result(False)

        else:
            self._loop.call_later(self.CONNECT_DEFERED,
                                  self.check_encoding_negotiation)

    def after_telopt_negotiation(self, status):
        """ XXX telnet stream option negotiation completed
        """
        from .telopt import WONT, ECHO
        from .slc import SLC_nosupport, SLC_EC
        if status.cancelled():
            self.log.debug('telopt negotiation cancelled')
            return

        # enable 'fast edit' for remote line editing by sending 'wont echo'
        if self.fast_edit and self.stream.mode == 'remote':
            erase_is = self.stream.slctab.get(SLC_EC, SLC_nosupport())
            if erase_is.variable and erase_is.val == bytes([127]):
                # delete (int 127) actually forwards the carriage when echoed
                # locally (LIT_ECHO enabled); we work around this by echoing
                # '\b\b \b', but for fast typist over slow links, this will
                # only serve to fudge up the inputline, so do not set
                # 'fast_edit' (local echo)
                self.log.debug('Cannot enable fast_edit (EC is ^?)')
            else:
                self.log.debug("`fast_edit' enabled (wont echo)")
                self.stream.iac(WONT, ECHO)

        # log about connection
        self.log.info('{}.'.format(self))
        self.log.info('stream status: {}.'.format(status.result()))
        self.log.info('client environment: {}.'.format(describe_env(self)))

    def after_encoding_negotiation(self, status):
        """ XXX this callback fires after encoding negotiation has completed,
            the value of client and remote encoding are final.  Some
            implementors may wish to display a non-english login banner.
        """
        if status.cancelled():
            self.log.debug('encoding negotiation cancelled')
            return
        self.log.debug('client encoding is {}.'.format(
            self.encoding(outgoing=True, incoming=True)))

    def request_advanced_opts(self):
        """ XXX Request advanced telnet options; called only if remote
            end replies affirmitively to (DO, TTYPE).
        """
        # Once the remote end has been identified as capable of at least TTYPE,
        # this callback is fired a single time. This is the preferred method
        # of delaying advanced negotiation attempts only for those clients
        # deemed smart enough to attempt them, as some non-compliant clients
        # may crash or close connection on receipt of unsupported options.

        # Request *additional* TTYPE response from clients who have replied
        # already, beginning a 'looping' mechanism of ``ttype_received()``
        # replies, by by which MUD clients may be identified.
        from .telopt import WILL, DO, SGA, ECHO, LINEMODE
        from .telopt import LFLOW, NEW_ENVIRON, NAWS, STATUS

        # 'supress go-ahead' + 'will echo' is kludge mode remote line editing
        self.stream.iac(WILL, SGA)
        self.stream.iac(WILL, ECHO)

        # LINEMODE negotiation solicits advanced remote line editing.
        self.stream.iac(DO, LINEMODE)

        # bsd telnet client uses STATUS to verify option state.
        self.stream.iac(WILL, STATUS)

        # lineflow allows pause/resume of transmission.
        self.stream.iac(WILL, LFLOW)

        # the 'new_environ' variables reveal client exported values.
        self.stream.iac(DO, NEW_ENVIRON)

        # 'negotiate about window size', for effective screen draws.
        self.stream.iac(DO, NAWS)

        if self.env['TTYPE0'] != 'ansi':
            # windows-98 era telnet ('ansi'), or terminals replying as
            # such won't have anything more interesting to say in reply
            # to subsequent requests for TTYPE. Windows socket transport
            # is said to hang if a second TTYPE is requested, others may
            # fail to reply.
            self.stream.request_ttype()

        # Also begin request of CHARSET, and bi-directional BINARY.
        self.begin_encoding_negotiation()

    def ttype_received(self, ttype):
        """ XXX Callback for TTYPE response.

        In the default implementation, an affirmitive reply to TTYPE acts
        as a canary for detecting more advanced options by firing the callback
        ``request_advanced_opts()``.

        The value of 'TERM' in class instance lookup table ``client_env`` is
        set to the lowercased value of ``ttype`` recieved.

        TTYPE may be requested multiple times, MUD implementations will
        reply a curses-capable terminal type (usually xterm-256color) on the
        2nd reply, and 'MTTS <client identifier>' on the third. Other clients
        will, in time, loop back to their first response.
        """
        if self.client_dumb:
            self.log.debug('client terminal is {}.'.format(ttype))
            # track TTYPE seperately from the NEW_ENVIRON 'TERM' value to
            # avoid telnet loops in TTYPE cycling
            self.env_update({'TERM': ttype, 'TTYPE0': ttype})
            self._advanced = 1
            self._loop.call_soon(self.request_advanced_opts)
            return

        self.env_update({'TTYPE{}'.format(self._advanced): ttype})

        lastval = self.env['TTYPE{}'.format(self._advanced - 1)].lower()

        # ttype value has looped
        if ttype == self.env['TTYPE0']:
            self.env_update({'TERM': ttype.lower()})
            self.log.debug('end on TTYPE{}: {}, using {env[TERM]}.'
                    .format(self._advanced, ttype, env=self.env))
            return

        # ttype empty or maximum loops reached, stop.
        elif (not ttype or
                self._advanced == self.TTYPE_LOOPMAX or
                ttype.lower() == 'unknown'):
            self.env_update({'TERM': ttype.lower()})
            self.log.debug('TTYPE stop on {}, using {env[TERM]}.'.format(
                self._advanced, env=self.env))
            return

        # Mud Terminal type (MTTS), use previous ttype, end negotiation
        elif (self._advanced == 2 and
                ttype.upper().startswith('MTTS ')):
            self.env_update({'TERM': self.env['TTYPE1']})
            self.log.debug('TTYPE{} is {}, using {env[TERM]}.'.format(
                self._advanced, ttype, env=self.env))
            return

        # ttype value repeated
        elif (ttype.lower() == lastval):
            self.log.debug('TTYPE repeated at {}, using {}.'.format(
                self._advanced, ttype))
            self.env_update({'TERM': ttype.lower()})
            return

        else:
            self.log.debug('TTYPE{} is {}, requesting another.'.format())
            self.env_update({'TERM': ttype})
            self.stream.request_ttype()
            self._advanced += 1

    def data_received(self, data):
        """ Process each byte as received by transport.

            Derived impl. should instead extend or override the
            shell stream methods ``feed_byte()`` and ``feed_slc()``.
        """
        self.log.debug('data_received: {!r}'.format(data))
        self._last_received = datetime.datetime.now()
        self._restart_timeout()
        for byte in (bytes([value]) for value in data):

            try:
                self.stream.feed_byte(byte)
            except (ValueError, AssertionError):
                exc_info = sys.exc_info()
                tbl_exception = (
                    traceback.format_tb(exc_info[2]) +
                    traceback.format_exception_only(exc_info[0], exc_info[1]))
                for tb in tbl_exception:
                    tb_msg = tb.splitlines()
                    tbl_srv = [row.rstrip() for row in tb_msg]
                    for line in tbl_srv:
                        self.log.error(line)
                continue

            if self.stream.is_oob:
                # byte is 'out-of-band', handled only by iac interpreter
                continue

            if self.stream.slc_received:
                self.shell.feed_slc(byte, func=self.stream.slc_received)
                continue

            self.shell.feed_byte(byte)

    def encoding(self, outgoing=False, incoming=False):
        """ Returns the session's preferred input or output encoding.

            Always 'ascii' for the direction(s) indicated unless ``inbinary``
            or ``outbinary`` has been negotiated. Then, the session value
            CHARSET is used, or the constructor kwarg ``encoding`` if CHARSET
            is not negotiated.
        """
        # of note: UTF-8 input with ascii output or vice-versa is possible.
        assert outgoing or incoming
        return (self.env.get('CHARSET', self._default_encoding)
                if (outgoing and not incoming and self.outbinary) or (
                    not outgoing and incoming and self.inbinary) or (
                    outgoing and incoming and self.outbinary and self.inbinary
                    ) else 'ascii')

    def handle_ayt(self, opt_byte):
    def handle_timing_mark(self, cmd):
        """ XXX Callback when IAC <cmd> TM (Timing Mark) is received,
            where <cmd> is any of (DO, DONT, WILL, WONT).

            This is a simple method by which pingtime may be measured.
            If the remote end performs any IAC interpretation, it should
            always answer at least WONT.
        """
        from .telopt import name_command
        self.log.debug('client sends: {} TIMING MARK.'
                       .format(name_command(cmd)))

    def handle_are_you_there(self, opt_byte):
        """ XXX Callback when IAC, AYT or SLC_AYT is received.
            opt_byte is value slc.SLC_AYT or telopt.AYT, indicating
            which method AYT was received by.

            Default implementation outputs the status of connection
            and displays shell prompt when opt_byte is AYT. Nothing
            is done when opt_byte is SLC_AYT, it is presumed handled
            by the shell as any other editing command (^T).
        """
        from .telopt import AYT
        from .slc import SLC_AYT
        self.log.debug('client sends: Are You There?')
        if opt_byte == AYT:
            # if (IAC, AYT) is recieved, and the editing command
            # SLC_AYT is unsupported, display connection status
            # and re-display the shell prompt.
            if self.stream.slctab[SLC_AYT].nosupport:
                self.shell.display_status()
                self.shell.display_prompt()
            # Otherwise, emulate as though the SLC_AYT editing cmd was
            # recieved (usually, ^T) by the shell. By default, the shell
            # does the same thing.
            else:
                self.shell.feed_slc(byte=self.stream.slctab[SLC_AYT].val,
                                    func=SLC_AYT)

    def special_received(self, iac_cmd):
        """ XXX Callback receives telnet IAC bytes for special functions.

                iac_cmd indicates which IAC was recieved, the default
                ``set_stream_callbacks()`` method registers for receipt
                for any of (AO, IP, BRK, SUSP, ABORT, EC).

                The default implementation maps these to their various
                SLC equivalents, if supported. Otherwise, nothing is
                done.
        """
        from .telopt import (AO, IP, BRK, ABORT, SUSP, EC, EL, EOR,
                             name_command)
        from .slc import (SLC_AO, SLC_IP, SLC_ABORT, SLC_SUSP, SLC_EC, SLC_EL,
                          SLC_EOR, name_slc_command)
        map_iac_slc = {
            AO: SLC_AO, IP: SLC_IP, BRK: SLC_ABORT,
            ABORT: SLC_ABORT, SUSP: SLC_SUSP, EC: SLC_EC,
            EL: SLC_EL, EOR: SLC_EOR, }

        slc_byte = map_iac_slc.get(iac_cmd, None)
        named_iac = name_command(iac_cmd)
        if slc_byte:
            slc_value = self.stream.slctab[slc_byte].val
            if not self.stream.slctab[slc_byte].nosupport:
                self.shell.feed_slc(byte=slc_value, func=slc_byte)
            else:
                named_slc = name_slc_command(slc_byte)
                self.log.debug('special_received unhandled: {} '
                               '(slc {} not supported).'.format(
                                   named_iac, named_slc))
        else:
            self.log.debug('special_received unhandled: {}.'
                           .format(named_iac))

    def timeout(self):
        """ XXX Callback received on session timeout.
        """
        self.shell.stream.write(
            '\r\nTimeout after {:1.0f}s.\r\n'.format(self.idle))
        self.log.debug('Timeout after {:1.3f}s.'.format(self.idle))
        self.transport.close()

    def logout(self, opt=None):
        """ XXX Callback received by shell exit or IAC-<opt>-LOGOUT.
        """
        from .telopt import DO
        if opt is not None and opt != DO:
            return self.stream.handle_logout(opt)
        self.log.debug('Logout by client.')
        msgs = ('The black thing inside rejoices at your departure',
                'The very earth groans at your depature',
                'The very trees seem to moan as you leave',
                'Echoing screams fill the wastelands as you close your eyes',
                'Your very soul aches as you wake up from your favorite dream')
        self.shell.stream.write(
            '\r\n{}.\r\n'.format(
                msgs[int(time.time()/84) % len(msgs)]))
        self.transport.close()

    def eof_received(self):
        self.connection_lost('EOF')
        return False

    def connection_lost(self, exc):
        if self._closing:
            return
        self._closing = True
        self.log.info('{}{}'.format(self.__str__(),
                                    ': {}'.format(exc) if exc is not None
                                    else ''))
        for task in (self._server_name, self._server_fqdn,
                     self._client_host, self._timeout,
                     self._telopt_negotiation):
            task.cancel()

    def env_update(self, env):
        " Callback receives no environment variables "
        # if client sends a readonly_env variable as part of host neogitation,
        # such as volunteering their 'HOSTNAME', replace it with the same name,
        # prefixed by '_'.
        if 'TIMEOUT' in env:
            try:
                val = int(env['TIMEOUT'])
                self._restart_timeout(val)
            except ValueError as err:
                self.log.debug('bad TIMEOUT {!r}, {}.'.format(
                    env['TIMEOUT'], err))
                del env['TIMEOUT']
        if 'TERM' in env:
            env['TERM'] = env['TERM'].lower()
            self.shell.term_received(env['TERM'])
        self.log.debug('env_update: %r', env)
        self._client_env.update(env)

    def after_client_lookup(self, arg):
        """ Callback receives result of client name resolution,
            Logs warning if reverse dns verification failed,
        """
        if arg.cancelled():
            self.log.debug('client dns lookup cancelled')
            return
        if self.client_ip != self.client_reverse_ip.result():
            # OpenSSH will log 'POSSIBLE BREAK-IN ATTEMPT!'
            # but we dont care .. just demonstrating these values,
            self.log.warn('reverse lookup: {cip} != {rcip} ({arg})'.format(
                cip=self.client_ip, rcip=self.client_reverse_ip,
                arg=arg.result()))
        self.env_update({
            'REMOTE_IP': self.client_ip,
            'REMOTE_PORT': str(self.client_port),
            'REMOTE_HOST': self.client_hostname.result(),
            })

    def after_server_gethostname(self, arg):
        """ Callback receives result of server name resolution,
            Begins fqdn resolution, available as '%H' prompt character.
        """
        if arg.cancelled():
            self.log.debug('server gethostname cancelled')
            return
        #: prompt sequence '%H' is result of socket.get_fqdn(self._server_name)
        self._server_fqdn = asyncio.get_event_loop().run_in_executor(
            None, socket.getfqdn, arg.result())
        self._server_fqdn.add_done_callback(self.after_server_getfqdn)
        self.env_update({'HOSTNAME': self.server_name.result()})

    def after_server_getfqdn(self, arg):
        """ Callback receives result of server fqdn resolution,
        """
        if arg.cancelled():
            self.log.debug('server getfqdn cancelled')
        else:
            if self.env['HOSTNAME'] != arg.result():
                self.env_update({'HOSTNAME': arg.result()})
                self.log.debug('HOSTNAME is {}'.format(arg.result()))

    def __str__(self):
        """ XXX Returns string suitable for status of server session.
        """
        return describe_connection(self)

    @property
    def client_ip(self):
        """ .. client_ip() -> string

            Returns Client IP address as string.
        """
        return self._client_ip

    @property
    def client_port(self):
        """ .. client_port() -> string

            Returns Client Port address as integer.
        """
        return self._client_port

    @property
    def client_hostname(self):
        """ .. client_hostname() -> Future()

            Returns DNS name of client as Future.
        """
        return dns.future_hostname(
            future_gethostbyaddr=self._client_host,
            fallback_ip=self.client_ip)

    @property
    def client_fqdn(self):
        """ .. client_fqdn() -> Future()

            Returns FQDN dns name of client as Future.
        """
        return dns.future_fqdn(
            future_gethostbyaddr=self._client_host,
            fallback_ip=self.client_ip)

    @property
    def client_reverse_ip(self):
        """ .. client_reverse_ip() -> Future()

            Returns reverse DNS lookup IP address of client as Future.
        """
        return dns.future_reverse_ip(
            future_gethostbyaddr=self._client_host,
            fallback_ip=self.client_ip)

    @property
    def client_dumb(self):
        """ .. client_dumb() -> bool

            Indicates the client is a dumb terminal (fe., a network scanner).
        """
        # _advanced is incremented by response to TTYPE negotiation.
        return not self._advanced

    @property
    def server_name(self):
        """ .. server_name() -> Future()

            Returns name of server as string as Future.
        """
        return self._server_name

    @property
    def server_fqdn(self):
        """ .. server_fqdn() -> Future()

            Returns fqdn string of server as Future String.
        """
        return self._server_fqdn

    @property
    def env(self):
        """ Returns hash of session environment values
        """
        return self._client_env

    @property
    def connected(self):
        """ Returns datetime connection was made.
        """
        return self._connected

    @property
    def duration(self):
        """ Returns seconds elapsed since client connected.
        """
        return (datetime.datetime.now() - self._connected).total_seconds()

    @property
    def idle(self):
        """ Returns seconds elapsed since last received data on transport.
        """
        return (datetime.datetime.now() - self._last_received).total_seconds()

    @property
    def inbinary(self):
        """ Returns True if server status ``inbinary`` is True.
        """
        from .telopt import BINARY
        # character values above 127 should not be expected to be read
        # inband from the transport unless inbinary is set True.
        return self.stream.remote_option.enabled(BINARY)

    @property
    def outbinary(self):
        """ Returns True if server status ``outbinary`` is True.
        """
        from .telopt import BINARY
        # character values above 127 should not be written to the transport
        # unless outbinary is set True.
        return self.stream.local_option.enabled(BINARY)

    def _restart_timeout(self, val=None):
        self._timeout.cancel()
        val = val if val is not None else self.env['TIMEOUT']
        if val:
            try:
                val = int(val)
            except ValueError:
                val = ''
            if val:
                self._timeout = self._loop.call_later(val * 60, self.timeout)

    def charset_received(self, charset):
        " Callback receives CHARSET value, rfc2066. "
        self.env_update({'CHARSET': charset.lower()})

    def naws_received(self, width, height):
        " Callback receives NAWS (negotiate about window size), rfc1073. "
        self.env_update({'COLUMNS': str(width), 'LINES': str(height)})

    def xdisploc_received(self, xdisploc):
        " Callback receives XDISPLOC value, rfc1096. "
        self.env_update({'DISPLAY': xdisploc})

    def tspeed_received(self, rx, tx):
        " Callback receives TSPEED values, rfc1079. "
        self.env_update({'TSPEED': '%s,%s' % (rx, tx)})

    def sndloc_received(self, location):
        " Callback receives SNDLOC values, rfc779. "
        self.env_update({'SNDLOC': location})


def describe_env(server):
    env_fingerprint = dict()
    for key, value in server.env.items():
        # do not display default env values, or our own hostname
        if key in server.default_env and value == server.default_env[key]:
            continue
        if key in ('HOSTNAME',):
            continue
        env_fingerprint[key] = value
    sfp_items = sorted(env_fingerprint.items())
    return '{{{}}}'.format(', '.join(['{!r}: {!r}'.format(key, value)
                                      for key, value in sfp_items]))


def describe_connection(server):
    state = (server._closing and 'dis' or '') + 'connected'
    hostname = (server.client_hostname.done() and
                ' ({})'.format(server.client_hostname.result())
                or '')
    duration = '{:0.1f}s'.format(server.duration)
    return ('{user} using {terminal} '
            '{state} from {clientip}:{port}{hostname} after {duration}'
            .format(
                user=server.env['USER'],
                terminal=server.env['TERM'],
                state=state,
                clientip=server.client_ip,
                port=server.client_port,
                hostname=hostname,
                duration=duration)
            )
