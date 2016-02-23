#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
*sshtunnel* - Initiate SSH tunnels via a remote gateway.

``sshtunnel`` works by opening a port forwarding SSH connection in the
background, using threads.

The connection(s) are closed when explicitly calling the
:meth:`SSHTunnelForwarder.stop` method or using it as a context.

"""

import sys
import socket
import getpass
import logging
import os
import argparse
import warnings
import threading
from binascii import hexlify
from select import select

import paramiko

if sys.version_info[0] < 3:  # pragma: no cover
    import SocketServer as socketserver
    string_types = basestring,  # noqa
    input_ = raw_input
else:
    import socketserver
    string_types = str
    input_ = input


__version__ = '0.0.8'
__author__ = 'pahaz'



DEFAULT_LOGLEVEL = logging.ERROR  #: default level if no logger passed
LOCAL_CHECK_TIMEOUT = 1.0  #: Timeout (seconds) for local tunnel side detection
DAEMON = False
TRACE = False
_CONNECTION_COUNTER = 1
_lock = threading.Lock()
#: Timeout (seconds) for the connection to the SSH gateway, ``None`` to disable
SSH_TIMEOUT = None
SSH_CONFIG_FILE = '~/.ssh/config'  #: Default path for ssh configuration file
DEPRECATIONS = {'ssh_address': 'ssh_address_or_host',
                'ssh_host': 'ssh_address_or_host',
                'ssh_private_key': 'ssh_pkey'}

########################
#                      #
#       Utils          #
#                      #
########################


def check_host(host):
    assert isinstance(host, string_types), "IP is not a string ({0})".format(
        type(host).__name__
    )


def check_port(port):
    assert isinstance(port, int), "PORT is not a number"
    assert port >= 0, "PORT < 0 ({0})".format(port)


def check_address(address):
    """
    Check that the format of the address is correct

    Arguments:
        address (tuple):
            (``str``, ``int``) representing an IP address and port,
            respectively
    Raises:
        AssertionError:
            raised when address has an incorrect format

    Example:

        >>> check_address(("127.0.0.1", 22))
    """
    assert isinstance(address, tuple), "ADDRESS is not a tuple ({0})".format(
        type(address).__name__
    )
    check_host(address[0])
    check_port(address[1])


def check_addresses(address_list):
    """
    Check that the format of the address list is correct

    Arguments:
        address_list (list[tuple]):
            Sequence of (``str``, ``int``) pairs, each representing an IP
            address and port respectively
    Raises:
        AssertionError:
            raised when any address in the list has an incorrect format

    Example:

        >>> check_addresses([("127.0.0.1", 22), ("127.0.0.1", 2222)])
    """
    assert isinstance(address_list, (list, tuple))
    for address in address_list:
        check_address(address)


def create_logger(logger=None, loglevel=None, add_paramiko_handler=True):
    """
    Attach or create a new logger and add a console handler if not present

    Arguments:

        logger (Optional[logging.Logger]):
            :class:`logging.Logger` instance; a new one is created if this
            argument is empty

        loglevel (Optional[str or int]):
            :class:`logging.Logger`'s level, either as a string (i.e.
            ``ERROR``) or in numeric format (10 == ``DEBUG``)

        add_paramiko_handler (boolean):
            Whether or not add a console handler for ``paramiko.transport``'s
            logger if no handler present

            Default: True
    Return:
        logging.Logger
    """
    logger = logger or logging.getLogger(
        '{0}.SSHTunnelForwarder'.format(__name__)
    )
    if not logger.handlers:  # if no handlers, add a new one (console)
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(
            logging.Formatter('%(asctime)s | %(levelname)-8s| %(message)s')
        )
        logger.setLevel(loglevel or DEFAULT_LOGLEVEL)
        console_handler.setLevel(loglevel or DEFAULT_LOGLEVEL)
        logger.addHandler(console_handler)
    elif loglevel:  # only override if loglevel was set
        logger.setLevel(loglevel)
        for handler in logger.handlers:
            handler.setLevel(loglevel)
    _check_paramiko_handlers()
    return logger


def _check_paramiko_handlers(logger=None):
    """
    Add a console handler for paramiko.transport's logger if not present
    """
    paramiko_logger = logging.getLogger('paramiko.transport')
    if not paramiko_logger.handlers:
        if logger:
            paramiko_logger.handlers = logger.handlers
        else:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(
                logging.Formatter('%(asctime)s | %(levelname)-8s| PARAMIKO: '
                                  '%(lineno)03d@%(module)-10s| %(message)s')
            )
            paramiko_logger.addHandler(console_handler)


def address_to_str(address):
    return "{0[0]}:{0[1]}".format(address)


def get_connection_id():
    global _CONNECTION_COUNTER
    with _lock:
        uid = _CONNECTION_COUNTER
        _CONNECTION_COUNTER += 1
    return uid


def _remove_none_values(dictionary):
    """ Remove dict keys whose value is None """
    return list(map(dictionary.pop,
                    [i for i in dictionary if dictionary[i] is None]))

########################
#                      #
#       Errors         #
#                      #
########################


class BaseSSHTunnelForwarderError(Exception):
    """ Base exception for Tunnel forwarder errors """
    pass


class HandlerSSHTunnelForwarderError(BaseSSHTunnelForwarderError):
    """ Handler exception for Tunnel forwarder errors """
    pass


########################
#                      #
#       Handlers       #
#                      #
########################


class _ForwardHandler(socketserver.BaseRequestHandler):
    """ Base handler for tunnel connections """
    remote_address = None
    ssh_transport = None
    logger = None

    def handle(self):
        uid = get_connection_id()
        info = 'In #{0} <-- {1}'.format(uid, self.client_address)
        try:
            assert isinstance(self.remote_address, tuple)
            chan = self.ssh_transport.open_channel('direct-tcpip',
                                                   self.remote_address,
                                                   self.request.getpeername())
        except AssertionError:
            msg = 'Remote address MUST be a tuple (ip:port): {0}' \
                .format(self.remote_address)
            self.logger.error(msg)
            raise HandlerSSHTunnelForwarderError(msg)
        except paramiko.SSHException as e:
            msg = '{0} to {1} failed: {2}' \
                .format(info, self.remote_address, repr(e))
            self.logger.error(msg)
            raise HandlerSSHTunnelForwarderError(msg)

        if chan is None:
            msg = '{0} to {1} was rejected ' \
                  'by the SSH server.'.format(info, self.remote_address)
            self.logger.error(msg)
            raise HandlerSSHTunnelForwarderError(msg)

        self.logger.info('{0} connected'.format(info))
        try:
            while True:
                rqst, _, _ = select([self.request, chan], [], [], 5)
                if self.request in rqst:
                    data = self.request.recv(1024)
                    if TRACE:
                        self.logger.info('{0} recv: {1}'
                                         .format(info, repr(data)))
                    chan.send(data)
                    if len(data) == 0:
                        break
                if chan in rqst:
                    data = chan.recv(1024)
                    if TRACE:
                        self.logger.info('{0} recv: {1}'
                                         .format(info, repr(data)))
                    self.request.send(data)
                    if len(data) == 0:
                        break
        except socket.error:
            # Sometimes a RST is sent and a socket error is raised, treat this
            # exception. It was seen that a 3way FIN is processed later on, so
            # no need to make an ordered close of the connection here or raise
            # the exception beyond this point...
            self.logger.warning('{0} sending RST'.format(info))
        except Exception as e:
            self.logger.error('{0} error: {1}'.format(info, repr(e)))
        finally:
            chan.close()
            self.request.close()
            self.logger.info('{0} connection closed.'.format(info))


class _ForwardServer(socketserver.TCPServer):  # Not Threading
    """
    Non-threading version of the forward server
    """
    allow_reuse_address = True  # faster rebinding

    @property
    def local_address(self):
        return self.server_address

    @property
    def local_host(self):
        return self.server_address[0]

    @property
    def local_port(self):
        return self.server_address[1]

    @property
    def remote_address(self):
        return self.RequestHandlerClass.remote_address

    @property
    def remote_host(self):
        return self.RequestHandlerClass.remote_address[0]

    @property
    def remote_port(self):
        return self.RequestHandlerClass.remote_address[1]


class _ThreadingForwardServer(socketserver.ThreadingMixIn, _ForwardServer):
    """
    Allows concurrent connections to each tunnel
    """
    # If True, cleanly stop threads created by ThreadingMixIn when quitting
    daemon_threads = DAEMON


class SSHTunnelForwarder(object):
    """
    **SSH tunnel class**

        - Initialize a SSH tunnel to a remote host according to the input
          arguments

        - Optionally:
            + Read an SSH configuration file (typically ``~/.ssh/config``)
            + Load keys from a running SSH agent (i.e. Pageant, GNOME Keyring)

    Keyword Arguments:

        ssh_address_or_host (tuple or str):
            IP or hostname of ``REMOTE GATEWAY``. It may be a two-element
            tuple (``str``, ``int``) representing IP and port respectively,
            or a ``str`` representing the IP address only

            .. versionadded:: 0.0.4

        ssh_config_file (str):
            SSH configuration file that will be read. If explicitly set to
            ``None``, parsing of this configuration is omitted

            Default: :const:`SSH_CONFIG_FILE`

            .. versionadded:: 0.0.4

        ssh_host_key (str):
            Representation of a line in an OpenSSH-style "known hosts"
            file.

            ``REMOTE GATEWAY``'s key fingerprint will be compared to this
            host key in order to prevent against SSH server spoofing.
            Important when using passwords in order not to accidentally
            do a login attempt to a wrong (perhaps an attacker's) machine

        ssh_username (str):
            Username to authenticate as in ``REMOTE SERVER``

            Default: current local user name

        ssh_password (str):
            Text representing the password used to connect to ``REMOTE
            SERVER`` or for unlocking a private key.

            .. note::
                Avoid coding secret password directly in the code, since this
                may be visible and make your service vulnerable to attacks

        ssh_port (int):
            Optional port number of the SSH service on ``REMOTE GATEWAY``,
            when `ssh_address_or_host`` is a ``str`` representing the
            IP part of ``REMOTE GATEWAY``'s address

            Default: 22

        ssh_pkey (str or paramiko.PKey):
            **Private** key file name (``str``) to obtain the public key
            from or a **public** key (:class:`paramiko.pkey.PKey`)

        ssh_private_key_password (str):
            Password for an encrypted ``ssh_pkey``

            .. note::
                Avoid coding secret password directly in the code, since this
                may be visible and make your service vulnerable to attacks


        ssh_proxy (paramiko.ProxyCommand or tuple):
            Proxy where all SSH traffic will be passed through.
            See either the :class:`paramiko.proxy.ProxyCommand` documentation
            or ``ProxyCommand`` in ``ssh_config(5)`` for more information

            It is also possible to specify the proxy address as a tuple of
            type (``str``, ``int``) representing proxy's IP and port

            ..note::
                Ignored if ``ssh_proxy_enabled`` is False

            .. versionadded:: 0.0.5

        ssh_proxy_enabled (boolean):
            Enable/disable SSH proxy. If True and user's
            ``ssh_config_file`` contains a ``ProxyCommand`` directive
            that matches the specified ``ssh_address_or_host``,
            a :class:`paramiko.proxy.ProxyCommand` object will be created where
            all SSH traffic will be passed through
            Default: ``True``

            .. versionadded:: 0.0.4

        local_bind_address (tuple):
            Local tuple in the format (``str``, ``int``) representing the
            IP and port of the local side of the tunnel. Both elements in
            the tuple are optional so both ('', 8000) and ('10.0.0.1', )
            are valid values
            Default: ("0.0.0.0", ``RANDOM PORT``)

        local_bind_addresses (list[tuple]):
            In case more than one tunnel is established at once, a list
            of tuples (in the same format as ``local_bind_address``)
            can be specified, such as [(ip1, port_1), (ip_2, port2), ...]
            Default: [``local_bind_address``]

            .. versionadded:: 0.0.4

        remote_bind_address (tuple):
            Remote tuple in the format (``str``, ``int``) representing the
            IP and port of the remote side of the tunnel. Port element in
            the tuple is optional and defaults to 22

        remote_bind_addresses (list[tuple]):
            In case more than one tunnel is established at once, a list
            of tuples (in the same format as ``remote_bind_address``)
            can be specified, such as [(ip1, port_1), (ip_2, port2), ...]
            Default: [``remote_bind_address``]

            .. versionadded:: 0.0.4

        logger (logging.Logger):
            logging instance for sshtunnel and paramiko
            Default: :class:`logging.Logger` instance with a single
            `StreamHandler` handler and :const:`DEFAULT_LOGLEVEL` level

            .. versionadded:: 0.0.3

        raise_exception_if_any_forwarder_have_a_problem (boolean):
            Allow silencing :class:`BaseSSHTunnelForwarderError` or
            :class:`HandlerSSHTunnelForwarderError` exceptions when set to
            False
            Default: ``True``

            .. versionadded:: 0.0.4

        set_keepalive (float):
            Interval in seconds defining the period in which, if no data
            was sent over the connection, a *'keepalive'* packet will be
            sent (and ignored by the remote host). This can be useful to
            keep connections alive over a NAT
            Default: 0.0 (no keepalive packets)

            .. versionadded:: 0.0.7

        threaded (boolean):
            Allow concurrent connections over a single tunnel
            Default: ``True``

            .. versionadded:: 0.0.3

        compression (boolean):
            Turn on/off compression. By default compression is off since it
            negatively affects interactive sessions
            Default: ``False``

            .. versionadded:: 0.0.8

        allow_agent (boolean):
            Enable/disable load of keys from an SSH agent
            Default: ``False``

            .. versionadded:: 0.0.8


        ssh_address (str):
            Superseeded by ``ssh_address_or_host```, tuple of type
            (str, int) representing the IP and port of ``REMOTE SERVER``

            .. deprecated:: 0.0.4

        ssh_host (str):
            Superseeded by ``ssh_address_or_host``, tuple of type
            (str, int) representing the IP and port of ``REMOTE SERVER``

            .. deprecated:: 0.0.4

        ssh_private_key (str or paramiko.PKey):
            Superseeded by ``ssh_pkey``, which can represent either a
            **private** key file name (``str``) or a **public** key
            (:class:`paramiko.pkey.PKey`)

            .. deprecated:: 0.0.8

    Attributes:

        tunnel_is_up (dict):
            Define whether or not the other side of the tunnel was reported to
            be up (and we must close it) or not (skip shutting down that tunnel
            ).

            **Example**::

                {('127.0.0.1', 55550): True,
                 ('127.0.0.1', 55551): False}

            where 55550 and 55551 are the local bind ports
    """
    is_use_local_check_up = False
    daemon_forward_servers = DAEMON
    daemon_transport = DAEMON

    def local_is_up(self, target):
        """
        Check if local side of the tunnel is up (remote target_host is
        reachable on TCP target_port)

        Arguments:
            target (tuple):
                tuple of type (``str``, ``int``) indicating the listen IP
                address and port
        Return:
            boolean
        """
        try:
            check_address(target)
        except AssertionError:
            self.logger.warning('Target must be a tuple (ip, port), where ip '
                                'is a string (i.e. "192.168.0.1") and port is '
                                'an integer (i.e. 40000).')
            return False

        (host, port) = target
        reachable_from = []
        self._local_interfaces = self._local_interfaces \
            if host in ['', '0.0.0.0'] \
            else [host]

        for host in self._local_interfaces:
            address = (host, port)
            reachable = self._is_address_reachable(address)
            reachable_text = "" if reachable else "*NOT* "
            self.logger.debug('Local side of the tunnel is {1}reachable from '
                              '({0[0]}:{0[1]})'.format(address,
                                                       reachable_text))
            if reachable:
                reachable_from.append(address)

        if reachable_from:
            reachable_from_text = ', '.join(['{0}:{1}'.format(h, p)
                                             for h, p in reachable_from])
            self.logger.info('Local side of the tunnel ({0[0]}:{0[1]}) '
                             'is UP and reachable from ({1})'
                             .format(target, reachable_from_text))
        else:
            self.logger.warning('Local side of tunnel ({0[0]}:{0[1]}) is DOWN,'
                                ' we will not attempt to connect.'
                                .format(target))
        return reachable

    def _make_ssh_forward_handler_class(self, remote_address_):
        """
        Make SSH Handler class.
        """
        _Handler = _ForwardHandler
        if not issubclass(_Handler, socketserver.BaseRequestHandler):
            msg = "base_ssh_forward_handler is not a subclass " \
                  "socketserver.BaseRequestHandler"
            self._raise(BaseSSHTunnelForwarderError, msg)

        class Handler(_Handler):
            """ handler class for remote tunnels """
            remote_address = remote_address_
            ssh_transport = self._transport
            logger = self.logger

        return Handler

    def _make_ssh_forward_server_class(self, remote_address_):
        return _ThreadingForwardServer if self._threaded else _ForwardServer

    def _make_ssh_forward_server(self, remote_address, local_bind_address):
        """
        Make SSH forward proxy Server class.
        """
        _Handler = self._make_ssh_forward_handler_class(remote_address)
        _Server = self._make_ssh_forward_server_class(remote_address)
        try:
            ssh_forward_server = _Server(local_bind_address, _Handler)
            if ssh_forward_server:
                self._server_list.append(ssh_forward_server)
            else:
                self._raise(
                    BaseSSHTunnelForwarderError,
                    'Problem with make ssh {0} <> {1} forwarder. You can '
                    'suppress this exception by using the '
                    '`raise_exception_if_any_forwarder_have_a_problem` '
                    'argument'.format(address_to_str(local_bind_address),
                                      address_to_str(remote_address))
                )
        except IOError:
            self.logger.error("Couldn't open tunnel {0} <> {1} "
                              "might be in use or destination not reachable."
                              .format(address_to_str(local_bind_address),
                                      address_to_str(remote_address)))

    def __init__(
            self,
            ssh_address_or_host=None,
            ssh_config_file=SSH_CONFIG_FILE,
            ssh_host_key=None,
            ssh_password=None,
            ssh_pkey=None,
            ssh_private_key_password=None,
            ssh_proxy=None,
            ssh_proxy_enabled=True,
            ssh_username=None,
            local_bind_address=None,
            local_bind_addresses=None,
            logger=None,
            raise_exception_if_any_forwarder_have_a_problem=True,
            remote_bind_address=None,
            remote_bind_addresses=None,
            set_keepalive=0.0,
            threaded=True,  # old version False
            compression=None,
            allow_agent=False,  # look for keys from an SSH agent
            **kwargs  # for backwards compatibility
    ):
        self.logger = logger or create_logger()
        # Ensure paramiko.transport has a console handler
        _check_paramiko_handlers(logger=logger)

        self.ssh_host_key = ssh_host_key
        self.set_keepalive = set_keepalive

        self._raise_fwd_exc = raise_exception_if_any_forwarder_have_a_problem
        self._threaded = threaded
        self._is_started = False

        # Check if deprecated arguments ssh_address or ssh_host were used
        for deprecated_argument in ['ssh_address', 'ssh_host']:
            ssh_address_or_host = self._process_deprecated(ssh_address_or_host,
                                                           deprecated_argument,
                                                           kwargs)
        ssh_pkey = self._process_deprecated(ssh_pkey,
                                            'ssh_private_key',
                                            kwargs)

        if isinstance(ssh_address_or_host, tuple):
            check_address(ssh_address_or_host)
            (self.ssh_host, ssh_port) = ssh_address_or_host
        else:
            self.ssh_host = ssh_address_or_host
            ssh_port = kwargs.pop('ssh_port', None)

        if kwargs:
            raise ValueError('Unknown arguments: {0}'.format(kwargs))

        # remote binds
        self._remote_binds = self._get_binds(remote_bind_address,
                                             remote_bind_addresses)
        # local binds
        self._local_binds = self._get_binds(local_bind_address,
                                            local_bind_addresses,
                                            remote=False)
        self._local_binds = self._consolidate_binds(self._local_binds,
                                                    self._remote_binds)

        (self.ssh_username,
         ssh_pkey,  # still needs to go through _consolidate_auth
         self.ssh_port,
         self.ssh_proxy,
         self.compression) = self._read_ssh_config(
             self.ssh_host,
             ssh_config_file,
             ssh_username,
             ssh_pkey,
             ssh_port,
             ssh_proxy if ssh_proxy_enabled else None,
             compression,
             logger)

        (self.ssh_password, ssh_pkey) = self._consolidate_auth(
            ssh_password=ssh_password,
            ssh_pkey=ssh_pkey,
            ssh_pkey_password=ssh_private_key_password,
            logger=logger
        )
        self.ssh_pkeys = self.get_keys(key=ssh_pkey,
                                       allow_agent=allow_agent,
                                       logger=self.logger)

        if isinstance(self.ssh_proxy, tuple):
            _pxcmd = 'ssh {0} -W {1}:{2}'.format(
                ':'.join((str(k) for k in self.ssh_proxy)),
                self.ssh_host,
                self.ssh_port
            )
            self.ssh_proxy = paramiko.proxy.ProxyCommand(_pxcmd)

        if not self.ssh_port:
            self.ssh_port = 22  # fallback value

        check_host(self.ssh_host)
        check_port(self.ssh_port)
        check_addresses(self._remote_binds)
        check_addresses(self._local_binds)

        self._local_interfaces = self._get_local_interfaces()
        self.logger.info('Connecting to gateway: {0}:{1} as user "{2}".'
                         .format(self.ssh_host,
                                 self.ssh_port,
                                 self.ssh_username))

        self.logger.debug('Concurrent connections allowed: {0}'
                          .format(self._threaded))

    @staticmethod
    def _read_ssh_config(ssh_host,
                         ssh_config_file,
                         ssh_username=None,
                         ssh_pkey=None,
                         ssh_port=None,
                         ssh_proxy=None,
                         compression=None,
                         logger=None):
        """
        Read ssh_config_file and tries to look for user (ssh_username),
        identityfile (ssh_pkey), port (ssh_port) and proxycommand
        (ssh_proxy) entries for ssh_host
         """
        ssh_config = paramiko.SSHConfig()

        # Try to read SSH_CONFIG_FILE
        try:
            # open the ssh config file
            with open(os.path.expanduser(ssh_config_file), 'r') as f:
                ssh_config.parse(f)
            # looks for information for the destination system
            hostname_info = ssh_config.lookup(ssh_host)
            # gather settings for user, port and identity file
            # last resort: use the 'login name' of the user
            ssh_username = (
                ssh_username or
                hostname_info.get('user')
            )
            ssh_pkey = (
                ssh_pkey or
                hostname_info.get('identityfile', [None])[0]
            )
            ssh_port = ssh_port or hostname_info.get('port')
            proxycommand = hostname_info.get('proxycommand')
            ssh_proxy = ssh_proxy or (paramiko.ProxyCommand(proxycommand) if
                                      proxycommand else None)
            if compression is None:
                compression = hostname_info.get('compression', '')
                compression = True if compression.upper() == 'YES' else False
        except IOError:
            if logger:
                logger.warning(
                    'Could not read SSH configuration file: {0}'
                    .format(ssh_config_file)
                )
        except AttributeError:  # ssh_config_file is None
            if logger:
                logger.info('Skipping loading of ssh config file')
        finally:
            return (ssh_username or getpass.getuser(),
                    ssh_pkey,
                    ssh_port,
                    ssh_proxy,
                    compression)

    @staticmethod
    def get_keys(key=None, allow_agent=False, logger=None):
        agent_keys = []
        if allow_agent:
            agent = paramiko.Agent()
            agent_keys.extend(agent.get_keys())
            if logger:
                logger.debug('{0} keys loaded from agent'
                             .format(len(agent_keys)))
        if key:  # last one to try
            agent_keys.append(key)
        return agent_keys

    @staticmethod
    def _consolidate_binds(local_binds, remote_binds):
        """
        Fill local_binds with defaults when no value/s were specified,
        leaving paramiko to decide in which local port the tunnel will be open
        """
        count = len(remote_binds) - len(local_binds)
        if count < 0:
            raise ValueError('Too many local bind addresses '
                             '(local_bind_addresses > remote_bind_addresses)')
        local_binds.extend([('0.0.0.0', 0) for x in range(count)])
        return local_binds

    @staticmethod
    def _consolidate_auth(ssh_password=None,
                          ssh_pkey=None,
                          ssh_pkey_password=None,
                          logger=None):
        """
        Get sure authentication information is in place.
        ``ssh_pkey`` may be of classes:
            - ``str`` - in this case it represents a private key file; public
            key will be obtained from it
            - ``paramiko.Pkey`` - it will be transparently returned

        """
        if isinstance(ssh_pkey, string_types):
            if os.path.exists(ssh_pkey):
                ssh_pkey = SSHTunnelForwarder.read_private_key_file(
                    pkey_file=ssh_pkey,
                    pkey_password=ssh_pkey_password or ssh_password,
                    logger=logger
                )
            elif logger:
                logger.warning('Private key file not found: {0}'
                               .format(ssh_pkey))
                ssh_pkey = None
        if not ssh_password and not ssh_pkey:
            raise ValueError('No password or public key available!')

        return (ssh_password, ssh_pkey)

    def _raise(self, exception, reason):
        if self._raise_fwd_exc:
            raise exception(reason)

    def _get_transport(self):
        """ Return the SSH transport to the remote gateway """
        if self.ssh_proxy:
            assert(isinstance(self.ssh_proxy, paramiko.proxy.ProxyCommand))
            self.logger.debug('Connecting via proxy: {0}'
                              .format(repr(self.ssh_proxy.cmd[1])))
            _socket = self.ssh_proxy
            _socket.settimeout(SSH_TIMEOUT)
        else:
            _socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            _socket.settimeout(SSH_TIMEOUT)
            _socket.connect((self.ssh_host, self.ssh_port))
        transport = paramiko.Transport(_socket)
        transport.set_keepalive(self.set_keepalive)
        transport.use_compression(compress=self.compression)
        transport.daemon = DAEMON

        return transport

    def create_tunnels(self):
        """ Create SSH tunnels on top of a transport to the remote gateway """
        self.tunnel_is_up = {}  # handle status of the other side of the tunnel
        try:
            self._transport = self._get_transport()
            for (rem, loc) in zip(self._remote_binds, self._local_binds):
                self._make_ssh_forward_server(rem, loc)
        except socket.gaierror:  # raised by paramiko.Transport
            msg = 'Could not resolve IP address for {0}, aborting!' \
                .format(self.ssh_host)
            self.logger.error(msg)
            raise BaseSSHTunnelForwarderError(msg)
        except (paramiko.SSHException, socket.error) as e:
            template = 'Could not connect to gateway: {0} ({1})'
            msg = template.format(self.ssh_host, e.args)
            self.logger.error(msg)
            raise BaseSSHTunnelForwarderError(msg)
        except BaseSSHTunnelForwarderError as e:
            msg = 'Problem setting SSH Forwarder up: {0}'.format(repr(e))
            self.logger.error(msg)
            raise BaseSSHTunnelForwarderError(msg)

    @staticmethod
    def _get_binds(bind_address, bind_addresses, remote=True):
        """
        """
        addr_kind = 'remote' if remote else 'local'

        if not bind_address and not bind_addresses:
            if remote:
                raise ValueError("No {0} bind addresses specified. Use "
                                 "'{0}_bind_address' or '{0}_bind_addresses'"
                                 " argument".format(addr_kind))
            else:
                return []
        elif bind_address and bind_addresses:
            raise ValueError("You can't use both '{0}_bind_address' and "
                             "'{0}_bind_addresses' arguments. Use one of "
                             "them.".format(addr_kind))
        if bind_address:
            return [bind_address]
        else:
            return bind_addresses

    @staticmethod
    def _process_deprecated(attrib, deprecated_attrib, kwargs):
        """
        Processes optional deprecate arguments
        """
        if deprecated_attrib not in DEPRECATIONS:
            raise ValueError('{0} not included deprecations list'
                             .format(deprecated_attrib))
        if deprecated_attrib in kwargs:
            warnings.warn("'{0}' is DEPRECATED use '{1}' instead"
                          .format(deprecated_attrib,
                                  DEPRECATIONS[deprecated_attrib]),
                          DeprecationWarning)
            if attrib:
                raise ValueError("You can't use both '{0}' and '{1}'."
                                 "Please only use one of them"
                                 .format(deprecated_attrib,
                                         DEPRECATIONS[deprecated_attrib]))
            else:
                return kwargs.pop(deprecated_attrib)
        return attrib

    @staticmethod
    def read_private_key_file(pkey_file,
                              pkey_password=None,
                              logger=None):
        """
        Get SSH Public key from a private key file, given an optional password

        Arguments:
            pkey_file (str):
                File containing a private key (RSA, DSS or ECDSA)
        Keyword Arguments:
            pkey_password (Optional[str]):
                Password to decrypt the private key
            logger (Optional[logging.Logger])
        Return:
            paramiko.Pkey
        """
        ssh_pkey = None
        for pkey_class in (paramiko.RSAKey,
                           paramiko.DSSKey,
                           paramiko.ECDSAKey):
            try:
                ssh_pkey = pkey_class.from_private_key_file(
                    pkey_file,
                    password=pkey_password
                )
            except paramiko.PasswordRequiredException:
                if logger:
                    logger.error('Password is required for key {0}'
                                 .format(pkey_file))
                break
            except paramiko.SSHException:
                if logger:
                    logger.debug('Private key file ({0}) could not be loaded '
                                 'or bad password. Tried with type: {1}'
                                 .format(pkey_file, pkey_class))
        return ssh_pkey

    def start(self):
        """ Start the SSH tunnels """
        if self._is_started:
            self.logger.warning("Already started!")
            return
        try:
            self._server_list = []  # reset server list
            self.create_tunnels()
            self._connect_to_gateway()
        except paramiko.ssh_exception.AuthenticationException:
            self.logger.error('Could not open connection to gateway')
            self._stop_transport()
            return

        threads = [
            threading.Thread(
                target=self._serve_forever_wrapper, args=(_srv,),
                name="Srv-" + address_to_str(_srv.local_address)
            )
            for _srv in self._server_list
        ]

        for thread in threads:
            thread.daemon = DAEMON
            thread.start()

        self._threads = threads
        if self.is_use_local_check_up:
            self.check_local_side_of_tunnels()
        self._is_started = True

    def _connect_to_gateway(self):
        """
        Open connection to SSH gateway
         - First try with all keys loaded from an SSH agent (if allowed)
         - Then with those passed directly or read from ~/.ssh/config
         - As last resort, try with a provided password
        """
        for key in self.ssh_pkeys:
            self.logger.debug('Trying to log in with key: {0}'
                              .format(hexlify(key.get_fingerprint())))
            try:
                self._transport.connect(hostkey=self.ssh_host_key,
                                        username=self.ssh_username,
                                        pkey=key)
                return
            except paramiko.AuthenticationException:
                self.logger.error('Could not open connection to gateway')
                self._stop_transport()

        if self.ssh_password:  # avoid conflict using both pass and pkey
            self.logger.debug('Logging in with password {0}'
                              .format('*' * len(self.ssh_password)))
            self._transport.connect(hostkey=self.ssh_host_key,
                                    username=self.ssh_username,
                                    password=self.ssh_password)
        else:
            raise paramiko.AuthenticationException(
                'No authentication methods left'
            )

    def check_local_side_of_tunnels(self):
        """
        Mark the tunnels as up or down by checking the local side with the
        result of running :meth:`.local_is_up` and updating
        :attr:`.tunnel_is_up`
        """
        for _srv in self._server_list:
            self.tunnel_is_up[_srv.local_address] = \
                self.local_is_up(_srv.local_address)

        if not any(self.tunnel_is_up.values()):
            self.logger.error("An error occurred while opening tunnels.")

    def _serve_forever_wrapper(self, _srv, poll_interval=0.1):
        """
        Wrapper for the server created for a SSH forward
        """
        try:
            self.logger.info('Opening tunnel: {0} <> {1}'.format(
                address_to_str(_srv.local_address),
                address_to_str(_srv.remote_address))
            )
            _srv.serve_forever(poll_interval)
        except socket.error as e:
            self.logger.error("Tunnel: {0} <> {1} socket error: {2}".format(
                address_to_str(_srv.local_address),
                address_to_str(_srv.remote_address),
                e)
            )
        except Exception as e:
            self.logger.error("Tunnel: {0} <> {1} error: {2}".format(
                address_to_str(_srv.local_address),
                address_to_str(_srv.remote_address),
                e)
            )
        finally:
            self.logger.info('Tunnel: {0} <> {1} is closed'.format(
                address_to_str(_srv.local_address),
                address_to_str(_srv.remote_address))
            )

    def stop(self):
        """
        Shut the tunnel down. This has to be handled with care:

            - if a port redirection is opened
            - the destination is not reachable
            - we attempt a connection to that tunnel (``SYN`` is sent and
              acknowledged, then a ``FIN`` packet is sent and never
              acknowledged... weird)
            - we try to shutdown: it will not succeed until ``FIN_WAIT_2`` and
              ``CLOSE_WAIT`` time out.

        | Handle these scenarios with :attr:`.tunnel_is_up`: if True, server
        | ``shutdown()`` will be skipped on servers
        """
        try:
            self._check_is_started()
        except BaseSSHTunnelForwarderError as e:
            self.logger.warning(e)
            return

        self.logger.info('Closing all open connections...')
        opened_address_text = ', '.join(
            (address_to_str(k.local_address) for k in self._server_list)
        ) or 'None'
        self.logger.debug('Open local addresses: ' + opened_address_text)

        for _srv in self._server_list:
            is_open = _srv.local_address in self.tunnel_is_up if \
                self.is_use_local_check_up else True
            if is_open:
                self.logger.info(
                    'Shutting down tunnel {0}'.format(
                        address_to_str(_srv.local_address)
                    )
                )
                _srv.shutdown()
            _srv.server_close()
        self._stop_transport()
        self._is_started = False

    def _stop_transport(self):
        """Close the underlying transport when nothing more is needed"""
        self._transport.close()
        self._transport.stop_thread()
        self.logger.debug('Transport is closed')

    @property
    def local_bind_port(self):
        # BACKWARD COMPATABILITY
        self._check_is_started()
        if len(self._server_list) != 1:
            raise BaseSSHTunnelForwarderError("Use .local_bind_ports property "
                                              "for more than one tunnel")
        return self.local_bind_ports[0]

    @property
    def local_bind_host(self):
        # BACKWARD COMPATABILITY
        self._check_is_started()
        if len(self._server_list) != 1:
            raise BaseSSHTunnelForwarderError("Use .local_bind_hosts property "
                                              "for more than one tunnel")
        return self.local_bind_hosts[0]

    @property
    def local_bind_address(self):
        # BACKWARD COMPATABILITY
        self._check_is_started()
        if len(self._server_list) != 1:
            raise BaseSSHTunnelForwarderError("Use .local_bind_addresses "
                                              "property for more than one "
                                              "tunnel")
        return self.local_bind_addresses[0]

    @property
    def local_bind_ports(self):
        """
        Return a list containing the ports of local side of the TCP tunnels
        """
        self._check_is_started()
        return [_server.local_port for _server in self._server_list]

    @property
    def local_bind_hosts(self):
        """
        Return a list containing the IP addresses listening for the tunnels
        """
        self._check_is_started()
        return [_server.local_host for _server in self._server_list]

    @property
    def local_bind_addresses(self):
        self._check_is_started()
        return [_server.local_address for _server in self._server_list]

    @staticmethod
    def _get_local_interfaces():
        """
        Return all local network interface's IP addresses
        """
        local_if = socket.gethostbyname_ex(socket.gethostname())[-1]
        # In Linux, if /etc/hosts is populated with the hostname it will only
        # return 127.0.0.1
        if '127.0.0.1' not in local_if:
            local_if.append('127.0.0.1')
        return local_if

    def _check_is_started(self):
        if not self._is_started:
            m = 'Server is not started. Please .start() first!'
            raise BaseSSHTunnelForwarderError(m)

    def _is_address_reachable(self, target, timeout=LOCAL_CHECK_TIMEOUT):
        conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            conn.settimeout(timeout)
            conn.connect(target)
            reachable = True
        except socket.error as e:
            self.logger.info('Socket connect({1}:{2}) problem: {0}'
                             .format(e, *target))
            reachable = False
        finally:
            conn.close()
        return reachable

    def __str__(self):
        proxy_str = ('proxy: {0}'.format(self.ssh_proxy.cmd[1]) if
                     self.ssh_proxy else 'no proxy')
        credentials = {
            'password': self.ssh_password,
            'pkeys': [(key.get_name(), hexlify(key.get_fingerprint()))
                      for key in self.ssh_pkeys]
            if self.ssh_pkeys else None
        }
        _remove_none_values(credentials)
        template = os.linesep.join(['{0}',
                                    'ssh gateway: {1}:{2}',
                                    '{3}',
                                    'username: {4}',
                                    'authentication: {5}',
                                    'hostkey: {6}',
                                    '{7}started',
                                    'keepalive messages: {8}',
                                    'local tunnel side detection: {9}',
                                    'concurrent connections {10}allowed',
                                    'compression {11}requested',
                                    'logging level: {12}'])
        return (template.format(
            repr(self),
            self.ssh_host, self.ssh_port,
            proxy_str,
            self.ssh_username,
            credentials,
            self.ssh_host_key if self.ssh_host_key else'not checked',
            '' if self._is_started else 'Not ',
            'disabled' if not self.set_keepalive else
            'every {0} sec'.format(self.set_keepalive),
            'enabled' if self.is_use_local_check_up else 'disabled',
            '' if self._threaded else 'not ',
            '' if self.compression else 'not ',
            logging.getLevelName(self.logger.level)
        ))

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()

    def close(self):
        """ Stop the an active tunnel, alias to :meth:`.stop` """
        self.stop()


def open_tunnel(*args, **kwargs):
    """
    Open an SSH Tunnel

    Arguments:
        destination:
            SSH server's IP address and port in the format
            (ssh_address, ssh_port)

    .. note::
        See :class:`SSHTunnelForwarder` for keyword arguments

    Example::

        from sshtunnel import open_tunnel

        with open_tunnel(SERVER,
                         ssh_username=SSH_USER,
                         ssh_port=22,
                         ssh_password=SSH_PASSWORD,
                         remote_bind_address=(REMOTE_HOST, REMOTE_PORT)
                         local_bind_address=('', LOCAL_PORT)
                         ) as server:

            def do_something(port):
                pass

            print("LOCAL PORTS:", server.local_bind_port)

            do_something(server.local_bind_port)
    """
    # Remove all "None" input values
    _remove_none_values(kwargs)

    # if ssh_config_file is None, skip config file lookup
    if 'ssh_config_file' not in kwargs:  # restore it as None
        kwargs['ssh_config_file'] = None

    # LOGGER - Create a console handler if not passed as argument
    kwargs['logger'] = create_logger(logger=kwargs.get('logger', None),
                                     loglevel=kwargs.pop('debug_level', None))

    ssh_address = kwargs.pop('ssh_address_or_host', None)
    # Check if deprecated arguments ssh_address or ssh_host were used
    for deprecated_argument in ['ssh_address', 'ssh_host']:
        ssh_address = SSHTunnelForwarder._process_deprecated(
            ssh_address,
            deprecated_argument,
            kwargs
        )

    ssh_port = kwargs.pop('ssh_port', None)

    if not args:
        args = ((ssh_address, ssh_port), )
    forwarder = SSHTunnelForwarder(*args, **kwargs)
    return forwarder


def _bindlist(input_str):
    """ Define type of data expected for remote and local bind address lists
        Returns a tuple (ip_address, port) whose elements are (str, int)
    """
    try:
        ip_port = input_str.split(':')
        if len(ip_port) == 1:
            _ip = ip_port[0]
            _port = None
        else:
            (_ip, _port) = ip_port
        if not _ip and not _port:
            raise AssertionError
        elif not _port:
            _port = '22'  # default port if not given
        return _ip, int(_port)
    except ValueError:
        raise argparse.ArgumentTypeError("Address tuple must be of type "
                                         "IP_ADDRESS:PORT")
    except AssertionError:
        raise argparse.ArgumentTypeError("Both IP:PORT can't be missing!")


def _parse_arguments(args=None):
    """
    Parse arguments directly passed from CLI
    """
    parser = argparse.ArgumentParser(
        description='Pure python ssh tunnel utils',
        formatter_class=argparse.RawTextHelpFormatter
    )

    parser.add_argument(
        'ssh_address', type=str,
        help='SSH server IP address (GW for SSH tunnels)\n'
             'set with "-- ssh_address" if immediately after '
             '-R or -L'
    )

    parser.add_argument(
        '-U', '--username', type=str, dest='ssh_username',
        help='SSH server account username'
    )

    parser.add_argument(
        '-p', '--server_port', type=int, dest='ssh_port',
        help='SSH server TCP port (default: 22)'
    )

    parser.add_argument(
        '-P', '--password', type=str, dest='ssh_password',
        help='SSH server account password'
    )

    parser.add_argument(
        '-R', '--remote_bind_address', type=_bindlist,
        nargs='+', default=[], metavar='IP:PORT',
        required=True,
        dest='remote_bind_addresses',
        help='Remote bind address sequence: '
             'ip_1:port_1 ip_2:port_2 ... ip_n:port_n\n'
             'Equivalent to ssh -Lxxxx:IP_ADDRESS:PORT\n'
             'If omitted, default port is 22.\n'
             'Example: -R 10.10.10.10: 10.10.10.10:5900'
    )

    parser.add_argument(
        '-L', '--local_bind_address', type=_bindlist, nargs='*',
        dest='local_bind_addresses', metavar='IP:PORT',
        help='Local bind address sequence: '
             'ip_1:port_1 ip_2:port_2 ... ip_n:port_n\n'
             'Equivalent to ssh -LPORT:xxxxxxxxx:xxxx, '
             'being the local IP address optional.\n'
             'By default it will listen in all interfaces '
             '(0.0.0.0) and choose a random port.\n'
             'Example: -L :40000'
    )

    parser.add_argument(
        '-k', '--ssh_host_key', type=str,
        help="Gateway's host key"
    )

    parser.add_argument(
        '-K', '--private_key_file', dest='ssh_private_key',
        metavar='KEY_FILE',
        type=str, help='RSA/DSS/ECDSA private key file'
    )

    parser.add_argument(
        '-S', '--private_key_password', dest='ssh_private_key_password',
        metavar='KEY_PASSWORD',
        type=str, help='RSA/DSS/ECDSA private key password'
    )

    parser.add_argument(
        '-t', '--threaded', action='store_true',
        help='Allow concurrent connections to each tunnel'
    )

    parser.add_argument(
        '-v', '--verbose', action='count', default=0,
        help='Increase output verbosity (default: {0})'.format(
            logging.getLevelName(DEFAULT_LOGLEVEL)
        )
    )

    parser.add_argument(
        '-V', '--version', action='version',
        version='%(prog)s {version}'.format(version=__version__),
        help='Show version number'
    )

    parser.add_argument(
        '-x', '--proxy', type=_bindlist,
        dest='ssh_proxy', metavar='IP:PORT',
        help='IP and port of SSH proxy to destination'
    )

    parser.add_argument(
        '-c', '--config', type=str,
        default=SSH_CONFIG_FILE, dest='ssh_config_file',
        help='SSH configuration file, defaults to {0}'.format(SSH_CONFIG_FILE)
    )

    parser.add_argument(
        '-z', '--compress', action='store_true', dest='compression',
        help='Request server for compression over SSH transport'
    )

    parser.add_argument(
        '-A', '--agent', action='store_true', dest='allow_agent',
        help='Allow looking for keys from an SSH agent'
    )
    return vars(parser.parse_args(args))


def _cli_main(args=None):
    """ Pass input arguments to open_tunnel

        Mandatory: ssh_address, -R (remote bind address list)

        Optional:
        -U (username) we may gather it from SSH_CONFIG_FILE or current username
        -p (server_port), defaults to 22
        -P (password)
        -L (local_bind_address), default to 0.0.0.0:22
        -k (ssh_host_key)
        -K (private_key_file), may be gathered from SSH_CONFIG_FILE
        -S (private_key_password)
        -t (threaded), allow concurrent connections over tunnels
        -v (verbose), up to 3 (-vvv) to raise loglevel from ERROR to DEBUG
        -V (version)
        -x (proxy), ProxyCommand's IP:PORT, may be gathered from config file
        -c (ssh_config), ssh configuration file (defaults to SSH_CONFIG_FILE)
        -z (compress)
        -A (allow_agent), allow looking for keys from an Agent
    """
    arguments = _parse_arguments(args)
    verbosity = min(arguments.pop('verbose'), 3)
    levels = [logging.ERROR, logging.WARNING, logging.INFO, logging.DEBUG]
    arguments.setdefault('debug_level', levels[verbosity])

    with open_tunnel(**arguments) as tunnel:
        if tunnel._is_started:
            input_('''

            Press <Ctrl-C> or <Enter> to stop!

            ''')

if __name__ == '__main__':  # pragma: no cover
    _cli_main()
