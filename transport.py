#!/usr/bin/python

# Copyright (C) 2003-2004 Robey Pointer <robey@lag.net>
#
# This file is part of paramiko.
#
# Paramiko is free software; you can redistribute it and/or modify it under the
# terms of the GNU Lesser General Public License as published by the Free
# Software Foundation; either version 2.1 of the License, or (at your option)
# any later version.
#
# Paramiko is distrubuted in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with Foobar; if not, write to the Free Software Foundation, Inc.,
# 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA.

"""
L{BaseTransport} handles the core SSH2 protocol.
"""

import sys, os, string, threading, socket, logging, struct

from common import *
from ssh_exception import SSHException
from message import Message
from channel import Channel
from util import format_binary, safe_string, inflate_long, deflate_long, tb_strings
from rsakey import RSAKey
from dsskey import DSSKey
from kex_group1 import KexGroup1
from kex_gex import KexGex
from primes import ModulusPack

# these come from PyCrypt
#     http://www.amk.ca/python/writing/pycrypt/
# i believe this on the standards track.
# PyCrypt compiled for Win32 can be downloaded from the HashTar homepage:
#     http://nitace.bsd.uchicago.edu:8080/hashtar
from Crypto.Cipher import Blowfish, AES, DES3
from Crypto.Hash import SHA, MD5, HMAC

from logging import DEBUG, INFO, WARNING, ERROR, CRITICAL


# for thread cleanup
_active_threads = []
def _join_lingering_threads():
    for thr in _active_threads:
        thr.active = False
import atexit
atexit.register(_join_lingering_threads)


class BaseTransport (threading.Thread):
    """
    Handles protocol negotiation, key exchange, encryption, and the creation
    of channels across an SSH session.  Basically everything but authentication
    is done here.
    """
    _PROTO_ID = '2.0'
    _CLIENT_ID = 'pyssh_1.1'

    preferred_ciphers = [ 'aes128-cbc', 'blowfish-cbc', 'aes256-cbc', '3des-cbc' ]
    preferred_macs = [ 'hmac-sha1', 'hmac-md5', 'hmac-sha1-96', 'hmac-md5-96' ]
    preferred_keys = [ 'ssh-rsa', 'ssh-dss' ]
    preferred_kex = [ 'diffie-hellman-group1-sha1', 'diffie-hellman-group-exchange-sha1' ]

    _cipher_info = {
        'blowfish-cbc': { 'class': Blowfish, 'mode': Blowfish.MODE_CBC, 'block-size': 8, 'key-size': 16 },
        'aes128-cbc': { 'class': AES, 'mode': AES.MODE_CBC, 'block-size': 16, 'key-size': 16 },
        'aes256-cbc': { 'class': AES, 'mode': AES.MODE_CBC, 'block-size': 16, 'key-size': 32 },
        '3des-cbc': { 'class': DES3, 'mode': DES3.MODE_CBC, 'block-size': 8, 'key-size': 24 },
        }

    _mac_info = {
        'hmac-sha1': { 'class': SHA, 'size': 20 },
        'hmac-sha1-96': { 'class': SHA, 'size': 12 },
        'hmac-md5': { 'class': MD5, 'size': 16 },
        'hmac-md5-96': { 'class': MD5, 'size': 12 },
        }

    _kex_info = {
        'diffie-hellman-group1-sha1': KexGroup1,
        'diffie-hellman-group-exchange-sha1': KexGex,
        }

    REKEY_PACKETS = pow(2, 30)
    REKEY_BYTES = pow(2, 30)

    OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED, OPEN_FAILED_CONNECT_FAILED, OPEN_FAILED_UNKNOWN_CHANNEL_TYPE, \
	OPEN_FAILED_RESOURCE_SHORTAGE = range(1, 5)

    _modulus_pack = None

    def __init__(self, sock):
        """
        Create a new SSH session over an existing socket, or socket-like
        object.  This only creates the Transport object; it doesn't begin the
        SSH session yet.  Use L{connect} or L{start_client} to begin a client
        session, or L{start_server} to begin a server session.

        If the object is not actually a socket, it must have the following
        methods:
            - C{settimeout(float)}: Sets a timeout for read & write calls.
            - C{send(string)}: Writes from 1 to C{len(string)} bytes, and
              returns an int representing the number of bytes written.  Returns
              0 or raises C{EOFError} if the stream has been closed.
            - C{recv(int)}: Reads from 1 to C{int} bytes and returns them as a
              string.  Returns 0 or raises C{EOFError} if the stream has been
              closed.

        For ease of use, you may also pass in an address (as a tuple) or a host
        string as the C{sock} argument.  (A host string is a hostname with an
        optional port (separated by C{":"}) which will be converted into a
        tuple of C{(hostname, port)}.)  A socket will be connected to this
        address and used for communication.  Exceptions from the C{socket} call
        may be thrown in this case.

        @param sock: a socket or socket-like object to create the session over.
        @type sock: socket
    	"""
        if type(sock) is str:
            # convert "host:port" into (host, port)
            hl = sock.split(':', 1)
            if len(hl) == 1:
                sock = (hl[0], 22)
            else:
                sock = (hl[0], int(hl[1]))
        if type(sock) is tuple:
            # connect to the given (host, port)
            hostname, port = sock
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((hostname, port))
        # okay, normal socket-ish flow here...
        threading.Thread.__init__(self, target=self._run)
        self.randpool = randpool
        self.sock = sock
        self.sock.settimeout(0.1)
        # negotiated crypto parameters
        self.local_version = 'SSH-' + self._PROTO_ID + '-' + self._CLIENT_ID
        self.remote_version = ''
        self.block_size_out = self.block_size_in = 8
        self.local_mac_len = self.remote_mac_len = 0
        self.engine_in = self.engine_out = None
        self.local_cipher = self.remote_cipher = ''
        self.sequence_number_in = self.sequence_number_out = 0L
        self.local_kex_init = self.remote_kex_init = None
        self.session_id = None
        # /negotiated crypto parameters
        self.expected_packet = 0
        self.active = False
        self.initial_kex_done = 0
        self.write_lock = threading.Lock()	# lock around outbound writes (packet computation)
        self.lock = threading.Lock()		# synchronization (always higher level than write_lock)
        self.channels = { }			# (id -> Channel)
        self.channel_events = { }		# (id -> Event)
        self.channel_counter = 1
        self.logger = logging.getLogger('paramiko.transport')
        self.window_size = 65536
        self.max_packet_size = 2048
        self.ultra_debug = 0
        self.saved_exception = None
        # used for noticing when to re-key:
        self.received_bytes = 0
        self.received_packets = 0
        self.received_packets_overflow = 0
        # user-defined event callbacks:
        self.completion_event = None
        # server mode:
        self.server_mode = 0
        self.server_key_dict = { }
        self.server_accepts = [ ]
        self.server_accept_cv = threading.Condition(self.lock)

    def __repr__(self):
        """
        Returns a string representation of this object, for debugging.

        @rtype: string
        """
        if not self.active:
            return '<paramiko.BaseTransport (unconnected)>'
        out = '<paramiko.BaseTransport'
        if self.local_cipher != '':
            out += ' (cipher %s, %d bits)' % (self.local_cipher, self._cipher_info[self.local_cipher]['key-size'] * 8)
        if len(self.channels) == 1:
            out += ' (active; 1 open channel)'
        else:
            out += ' (active; %d open channels)' % len(self.channels)
        out += '>'
        return out

    def start_client(self, event=None):
        """
        Negotiate a new SSH2 session as a client.  This is the first step after
        creating a new L{Transport}.  A separate thread is created for protocol
        negotiation, so this method returns immediately.
        
        When negotiation is done (successful or not), the given C{Event} will
        be triggered.  On failure, L{is_active} will return C{False}.

        After a successful negotiation, you will usually want to authenticate,
        calling L{auth_password <Transport.auth_password>} or
        L{auth_publickey <Transport.auth_publickey>}.

        @note: L{connect} is a simpler method for connecting as a client.
        
        @note: After calling this method (or L{start_server} or L{connect}),
        you should no longer directly read from or write to the original socket
        object.

        @param event: an event to trigger when negotiation is complete.
        @type event: threading.Event
        """
        self.completion_event = event
        self.start()

    def start_server(self, event=None):
        """
        Negotiate a new SSH2 session as a server.  This is the first step after
        creating a new L{Transport} and setting up your server host key(s).  A
        separate thread is created for protocol negotiation, so this method
        returns immediately.

        When negotiation is done (successful or not), the given C{Event} will
        be triggered.  On failure, L{is_active} will return C{False}.

        After a successful negotiation, the client will need to authenticate.
        Override the methods
        L{get_allowed_auths <Transport.get_allowed_auths>},
        L{check_auth_none <Transport.check_auth_none>},
        L{check_auth_password <Transport.check_auth_password>}, and
        L{check_auth_publickey <Transport.check_auth_publickey>} to control the
        authentication process.

        After a successful authentication, the client should request to open
        a channel.  Override L{check_channel_request} to allow channels to
        be opened.

        @note: After calling this method (or L{start_client} or L{connect}),
        you should no longer directly read from or write to the original socket
        object.

        @param event: an event to trigger when negotiation is complete.
        @type event: threading.Event
        """
        self.server_mode = 1
        self.completion_event = event
        self.start()

    def add_server_key(self, key):
        """
        Add a host key to the list of keys used for server mode.  When behaving
        as a server, the host key is used to sign certain packets during the
        SSH2 negotiation, so that the client can trust that we are who we say
        we are.  Because this is used for signing, the key must contain private
        key info, not just the public half.
        
        @param key: the host key to add, usually an L{RSAKey <rsakey.RSAKey>} or
        L{DSSKey <dsskey.DSSKey>}.
        @type key: L{PKey <pkey.PKey>}
        """
        self.server_key_dict[key.get_name()] = key

    def get_server_key(self):
        """
        Return the active host key, in server mode.  After negotiating with the
        client, this method will return the negotiated host key.  If only one
        type of host key was set with L{add_server_key}, that's the only key
        that will ever be returned.  But in cases where you have set more than
        one type of host key (for example, an RSA key and a DSS key), the key
        type will be negotiated by the client, and this method will return the
        key of the type agreed on.  If the host key has not been negotiated
        yet, C{None} is returned.  In client mode, the behavior is undefined.

        @return: host key of the type negotiated by the client, or C{None}.
        @rtype: L{PKey <pkey.PKey>}
        """
        try:
            return self.server_key_dict[self.host_key_type]
        except KeyError:
            return None

    def load_server_moduli(filename=None):
        """
        I{(optional)}
        Load a file of prime moduli for use in doing group-exchange key
        negotiation in server mode.  It's a rather obscure option and can be
        safely ignored.

        In server mode, the remote client may request "group-exchange" key
        negotiation, which asks the server to send a random prime number that
        fits certain criteria.  These primes are pretty difficult to compute,
        so they can't be generated on demand.  But many systems contain a file
        of suitable primes (usually named something like C{/etc/ssh/moduli}).
        If you call C{load_server_moduli} and it returns C{True}, then this
        file of primes has been loaded and we will support "group-exchange" in
        server mode.  Otherwise server mode will just claim that it doesn't
        support that method of key negotiation.

        @param filename: optional path to the moduli file, if you happen to
        know that it's not in a standard location.
        @type filename: string
        @return: True if a moduli file was successfully loaded; False
        otherwise.
        @rtype: bool

        @since: doduo
        
        @note: This has no effect when used in client mode.
        """
        BaseTransport._modulus_pack = ModulusPack(randpool)
        # places to look for the openssh "moduli" file
        file_list = [ '/etc/ssh/moduli', '/usr/local/etc/moduli' ]
        if filename is not None:
            file_list.insert(0, filename)
        for fn in file_list:
            try:
                BaseTransport._modulus_pack.read_file(fn)
                return True
            except IOError:
                pass
        # none succeeded
        BaseTransport._modulus_pack = None
        return False
    load_server_moduli = staticmethod(load_server_moduli)

    def close(self):
        """
        Close this session, and any open channels that are tied to it.
        """
        self.active = False
        self.engine_in = self.engine_out = None
        self.sequence_number_in = self.sequence_number_out = 0L
        for chan in self.channels.values():
            chan._unlink()

    def get_remote_server_key(self):
        """
        Return the host key of the server (in client mode).

        @note: Previously this call returned a tuple of (key type, key string).
        You can get the same effect by calling L{PKey.get_name} for the key
        type, and C{str(key)} for the key string.

        @raise SSHException: if no session is currently active.
        
        @return: public key of the remote server.
        @rtype: L{PKey}
        """
        if (not self.active) or (not self.initial_kex_done):
            raise SSHException('No existing session')
        return self.host_key

    def is_active(self):
        """
        Return true if this session is active (open).

        @return: True if the session is still active (open); False if the session is closed.
        @rtype: bool
        """
        return self.active

    def open_session(self):
        """
        Request a new channel to the server, of type C{"session"}.  This
        is just an alias for C{open_channel('session')}.

        @return: a new L{Channel} on success, or C{None} if the request is
        rejected or the session ends prematurely.
        @rtype: L{Channel}
        """
        return self.open_channel('session')

    def open_channel(self, kind):
        """
        Request a new channel to the server.  L{Channel}s are socket-like
        objects used for the actual transfer of data across the session.
        You may only request a channel after negotiating encryption (using
        L{connect} or L{start_client} and authenticating.

        @param kind: the kind of channel requested (usually C{"session"}).
        @type kind: string
        @return: a new L{Channel} on success, or C{None} if the request is
        rejected or the session ends prematurely.
        @rtype: L{Channel}
        """
        chan = None
        try:
            self.lock.acquire()
            chanid = self.channel_counter
            self.channel_counter += 1
            m = Message()
            m.add_byte(chr(MSG_CHANNEL_OPEN))
            m.add_string(kind)
            m.add_int(chanid)
            m.add_int(self.window_size)
            m.add_int(self.max_packet_size)
            self.channels[chanid] = chan = Channel(chanid)
            self.channel_events[chanid] = event = threading.Event()
            chan._set_transport(self)
            chan._set_window(self.window_size, self.max_packet_size)
            self._send_message(m)
        finally:
            self.lock.release()
        while 1:
            event.wait(0.1);
            if not self.active:
                return None
            if event.isSet():
                break
        try:
            self.lock.acquire()
            if not self.channels.has_key(chanid):
                chan = None
        finally:
            self.lock.release()
        return chan

    def send_ignore(self, bytes=None):
        """
        Send a junk packet across the encrypted link.  This is sometimes used
        to add "noise" to a connection to confuse would-be attackers.  It can
        also be used as a keep-alive for long lived connections traversing
        firewalls.

        @param bytes: the number of random bytes to send in the payload of the
        ignored packet -- defaults to a random number from 10 to 41.
        @type bytes: int
        """
        m = Message()
        m.add_byte(chr(MSG_IGNORE))
        if bytes is None:
            bytes = (ord(randpool.get_bytes(1)) % 32) + 10
        m.add_bytes(randpool.get_bytes(bytes))
        self._send_message(m)

    def renegotiate_keys(self):
        """
        Force this session to switch to new keys.  Normally this is done
        automatically after the session hits a certain number of packets or
        bytes sent or received, but this method gives you the option of forcing
        new keys whenever you want.  Negotiating new keys causes a pause in
        traffic both ways as the two sides swap keys and do computations.  This
        method returns when the session has switched to new keys, or the
        session has died mid-negotiation.

        @return: True if the renegotiation was successful, and the link is
        using new keys; False if the session dropped during renegotiation.
        @rtype: bool
        """
        self.completion_event = threading.Event()
        self._send_kex_init()
        while 1:
            self.completion_event.wait(0.1);
            if not self.active:
                return False
            if self.completion_event.isSet():
                break
        return True

    def global_request(self, kind, data=None, wait=True):
        """
        Make a global request to the remote host.  These are normally
        extensions to the SSH2 protocol.

        @param kind: name of the request.
        @type kind: string
        @param data: an optional tuple containing additional data to attach
        to the request.
        @type data: tuple
        @param wait: C{True} if this method should not return until a response
        is received; C{False} otherwise.
        @type wait: bool
        @return: a L{Message} containing possible additional data if the
        request was successful (or an empty L{Message} if C{wait} was
        C{False}); C{None} if the request was denied.
        @rtype: L{Message}
        """
        if wait:
            self.completion_event = threading.Event()
        m = Message()
        m.add_byte(chr(MSG_GLOBAL_REQUEST))
        m.add_string(kind)
        m.add_boolean(wait)
        if data is not None:
            for item in data:
                m.add(item)
        self._send_message(m)
        if not wait:
            return True
        while True:
            self.completion_event.wait(0.1)
            if not self.active:
                return False
            if self.completion_event.isSet():
                break
        return self.global_response

    def check_channel_request(self, kind, chanid):
        """
        I{(subclass override)}
        Determine if a channel request of a given type will be granted, and
        return a suitable L{Channel} object.  This method is called in server
        mode when the client requests a channel, after authentication is
        complete.

        In server mode, you will generally want to subclass L{Channel} to
        override some of the methods for handling client requests (such as
        connecting to a subsystem or opening a shell) to determine what you
        want to allow or disallow.  For this reason, L{check_channel_request}
        must return a new object of that type.  The C{chanid} parameter is
        passed so that you can use it in L{Channel}'s constructor.

        The default implementation always returns C{None}, rejecting any
        channel requests.  A useful server must override this method.

        @param kind: the kind of channel the client would like to open
        (usually C{"session"}).
        @type kind: string
        @param chanid: ID of the channel, required to create a new L{Channel}
        object.
        @type chanid: int
        @return: a new L{Channel} object (or subclass thereof), or C{None} to
        refuse the request.
        @rtype: L{Channel}
        """
        return None

    def check_global_request(self, kind, msg):
        """
        I{(subclass override)}
        Handle a global request of the given C{kind}.  This method is called
        in server mode and client mode, whenever the remote host makes a global
        request.  If there are any arguments to the request, they will be in
        C{msg}.

        There aren't any useful global requests defined, aside from port
        forwarding, so usually this type of request is an extension to the
        protocol.

        If the request was successful and you would like to return contextual
        data to the remote host, return a tuple.  Items in the tuple will be
        sent back with the successful result.  (Note that the items in the
        tuple can only be strings, ints, longs, or bools.)

        The default implementation always returns C{False}, indicating that it
        does not support any global requests.

        @param kind: the kind of global request being made.
        @type kind: string
        @param msg: any extra arguments to the request.
        @type msg: L{Message}
        @return: C{True} or a tuple of data if the request was granted;
        C{False} otherwise.
        @rtype: bool
        """
        return False

    def accept(self, timeout=None):
        try:
            self.lock.acquire()
            if len(self.server_accepts) > 0:
                chan = self.server_accepts.pop(0)
            else:
                self.server_accept_cv.wait(timeout)
                if len(self.server_accepts) > 0:
                    chan = self.server_accepts.pop(0)
                else:
                    # timeout
                    chan = None
        finally:
            self.lock.release()
        return chan

    def connect(self, hostkeytype=None, hostkey=None, username='', password=None, pkey=None):
        """
        Negotiate an SSH2 session, and optionally verify the server's host key
        and authenticate using a password or private key.  This is a shortcut
        for L{start_client}, L{get_remote_server_key}, and
        L{Transport.auth_password} or L{Transport.auth_publickey}.  Use those
        methods if you want more control.

        You can use this method immediately after creating a Transport to
        negotiate encryption with a server.  If it fails, an exception will be
        thrown.  On success, the method will return cleanly, and an encrypted
        session exists.  You may immediately call L{open_channel} or
        L{open_session} to get a L{Channel} object, which is used for data
        transfer.

        @note: If you fail to supply a password or private key, this method may
        succeed, but a subsequent L{open_channel} or L{open_session} call may
        fail because you haven't authenticated yet.

        @param hostkeytype: the type of host key expected from the server
        (usually C{"ssh-rsa"} or C{"ssh-dss"}), or C{None} if you don't want
        to do host key verification.
        @type hostkeytype: string
        @param hostkey: the host key expected from the server, or C{None} if
        you don't want to do host key verification.
        @type hostkey: string
        @param username: the username to authenticate as.
        @type username: string
        @param password: a password to use for authentication, if you want to
        use password authentication; otherwise C{None}.
        @type password: string
        @param pkey: a private key to use for authentication, if you want to
        use private key authentication; otherwise C{None}.
        @type pkey: L{PKey<pkey.PKey>}
        
        @raise SSHException: if the SSH2 negotiation fails, the host key
        supplied by the server is incorrect, or authentication fails.

        @since: doduo
        """
        if hostkeytype is not None:
            self.preferred_keys = [ hostkeytype ]

        event = threading.Event()
        self.start_client(event)
        while 1:
            event.wait(0.1)
            if not self.active:
                e = self.saved_exception
                self.saved_exception = None
                if e is not None:
                    raise e
                raise SSHException('Negotiation failed.')
            if event.isSet():
                break

        # check host key if we were given one
        if (hostkeytype is not None) and (hostkey is not None):
            type, key = self.get_remote_server_key()
            if (type != hostkeytype) or (key != hostkey):
                print repr(type) + ' - ' + repr(hostkeytype)
                print repr(key) + ' - ' + repr(hostkey)
                raise SSHException('Bad host key from server')
            self._log(DEBUG, 'Host key verified (%s)' % hostkeytype)

        if (pkey is not None) or (password is not None):
            event.clear()
            if password is not None:
                self._log(DEBUG, 'Attempting password auth...')
                self.auth_password(username, password, event)
            else:
                self._log(DEBUG, 'Attempting pkey auth...')
                self.auth_publickey(username, pkey, event)
            while 1:
                event.wait(0.1)
                if not self.active:
                    e = self.saved_exception
                    self.saved_exception = None
                    if e is not None:
                        raise e
                    raise SSHException('Authentication failed.')
                if event.isSet():
                    break
            if not self.is_authenticated():
                raise SSHException('Authentication failed.')

        return


    ###  internals...

    
    def _log(self, level, msg):
        if type(msg) == type([]):
            for m in msg:
                self.logger.log(level, m)
        else:
            self.logger.log(level, msg)

    def _get_modulus_pack(self):
        "used by KexGex to find primes for group exchange"
        return self._modulus_pack

    def _unlink_channel(self, chanid):
        "used by a Channel to remove itself from the active channel list"
        try:
            self.lock.acquire()
            if self.channels.has_key(chanid):
                del self.channels[chanid]
        finally:
            self.lock.release()

    def _read_all(self, n):
        out = ''
        while n > 0:
            try:
                x = self.sock.recv(n)
                if len(x) == 0:
                    raise EOFError()
                out += x
                n -= len(x)
            except socket.timeout:
                if not self.active:
                    raise EOFError()
        return out

    def _write_all(self, out):
        while len(out) > 0:
            n = self.sock.send(out)
            if n <= 0:
                raise EOFError()
            if n == len(out):
                return
            out = out[n:]
        return

    def _build_packet(self, payload):
        # pad up at least 4 bytes, to nearest block-size (usually 8)
        bsize = self.block_size_out
        padding = 3 + bsize - ((len(payload) + 8) % bsize)
        packet = struct.pack('>I', len(payload) + padding + 1)
        packet += chr(padding)
        packet += payload
        packet += randpool.get_bytes(padding)
        return packet

    def _send_message(self, data):
        # FIXME: should we check for rekeying here too?
        # encrypt this sucka
        packet = self._build_packet(str(data))
        if self.ultra_debug:
            self._log(DEBUG, format_binary(packet, 'OUT: '))
        if self.engine_out != None:
            out = self.engine_out.encrypt(packet)
        else:
            out = packet
        # + mac
        try:
            self.write_lock.acquire()
            if self.engine_out != None:
                payload = struct.pack('>I', self.sequence_number_out) + packet
                out += HMAC.HMAC(self.mac_key_out, payload, self.local_mac_engine).digest()[:self.local_mac_len]
            self.sequence_number_out += 1L
            self.sequence_number_out %= 0x100000000L
            self._write_all(out)
        finally:
            self.write_lock.release()

    def _read_message(self):
        "only one thread will ever be in this function"
        header = self._read_all(self.block_size_in)
        if self.engine_in != None:
            header = self.engine_in.decrypt(header)
        if self.ultra_debug:
            self._log(DEBUG, format_binary(header, 'IN: '));
        packet_size = struct.unpack('>I', header[:4])[0]
        # leftover contains decrypted bytes from the first block (after the length field)
        leftover = header[4:]
        if (packet_size - len(leftover)) % self.block_size_in != 0:
            raise SSHException('Invalid packet blocking')
        buffer = self._read_all(packet_size + self.remote_mac_len - len(leftover))
        packet = buffer[:packet_size - len(leftover)]
        post_packet = buffer[packet_size - len(leftover):]
        if self.engine_in != None:
            packet = self.engine_in.decrypt(packet)
        if self.ultra_debug:
            self._log(DEBUG, format_binary(packet, 'IN: '));
        packet = leftover + packet
        if self.remote_mac_len > 0:
            mac = post_packet[:self.remote_mac_len]
            mac_payload = struct.pack('>II', self.sequence_number_in, packet_size) + packet
            my_mac = HMAC.HMAC(self.mac_key_in, mac_payload, self.remote_mac_engine).digest()[:self.remote_mac_len]
            if my_mac != mac:
                raise SSHException('Mismatched MAC')
        padding = ord(packet[0])
        payload = packet[1:packet_size - padding + 1]
        randpool.add_event(packet[packet_size - padding + 1])
        if self.ultra_debug:
            self._log(DEBUG, 'Got payload (%d bytes, %d padding)' % (packet_size, padding))
        msg = Message(payload[1:])
        msg.seqno = self.sequence_number_in
        self.sequence_number_in = (self.sequence_number_in + 1) & 0xffffffffL
        # check for rekey
        self.received_bytes += packet_size + self.remote_mac_len + 4
        self.received_packets += 1
        if (self.received_packets >= self.REKEY_PACKETS) or (self.received_bytes >= self.REKEY_BYTES):
            # only ask once for rekeying
            if self.local_kex_init is None:
                self._log(DEBUG, 'Rekeying (hit %d packets, %d bytes)' % (self.received_packets,
                                                                          self.received_bytes))
                self.received_packets_overflow = 0
                self._send_kex_init()
            else:
                # we've asked to rekey already -- give them 20 packets to
                # comply, then just drop the connection
                self.received_packets_overflow += 1
                if self.received_packets_overflow >= 20:
                    raise SSHException('Remote transport is ignoring rekey requests')
                
        return ord(payload[0]), msg

    def _set_K_H(self, k, h):
        "used by a kex object to set the K (root key) and H (exchange hash)"
        self.K = k
        self.H = h
        if self.session_id == None:
            self.session_id = h

    def _expect_packet(self, type):
        "used by a kex object to register the next packet type it expects to see"
        self.expected_packet = type

    def _key_from_blob(self, keytype, keyblob):
        if keytype == 'ssh-rsa':
            return RSAKey(Message(keyblob))
        elif keytype == 'ssh-dss':
            return DSSKey(Message(keyblob))
        else:
            return None

    def _verify_key(self, host_key, sig):
        key = self._key_from_blob(self.host_key_type, host_key)
        if (key == None) or not key.valid:
            raise SSHException('Unknown host key type')
        if not key.verify_ssh_sig(self.H, Message(sig)):
            raise SSHException('Signature verification (%s) failed.  Boo.  Robey should debug this.' % self.host_key_type)
        self.host_key = key

    def _compute_key(self, id, nbytes):
        "id is 'A' - 'F' for the various keys used by ssh"
        m = Message()
        m.add_mpint(self.K)
        m.add_bytes(self.H)
        m.add_byte(id)
        m.add_bytes(self.session_id)
        out = sofar = SHA.new(str(m)).digest()
        while len(out) < nbytes:
            m = Message()
            m.add_mpint(self.K)
            m.add_bytes(self.H)
            m.add_bytes(sofar)
            hash = SHA.new(str(m)).digest()
            out += hash
            sofar += hash
        return out[:nbytes]

    def _get_cipher(self, name, key, iv):
        if not self._cipher_info.has_key(name):
            raise SSHException('Unknown client cipher ' + name)
        return self._cipher_info[name]['class'].new(key, self._cipher_info[name]['mode'], iv)

    def _run(self):
        self.active = True
        _active_threads.append(self)
        try:
            # SSH-1.99-OpenSSH_2.9p2
            self._write_all(self.local_version + '\r\n')
            self._check_banner()
            self._send_kex_init()
            self.expected_packet = MSG_KEXINIT

            while self.active:
                ptype, m = self._read_message()
                if ptype == MSG_IGNORE:
                    continue
                elif ptype == MSG_DISCONNECT:
                    self._parse_disconnect(m)
                    self.active = False
                    break
                elif ptype == MSG_DEBUG:
                    self._parse_debug(m)
                    continue
                if self.expected_packet != 0:
                    if ptype != self.expected_packet:
                        raise SSHException('Expecting packet %d, got %d' % (self.expected_packet, ptype))
                    self.expected_packet = 0
                    if (ptype >= 30) and (ptype <= 39):
                        self.kex_engine.parse_next(ptype, m)
                        continue

                if self._handler_table.has_key(ptype):
                    self._handler_table[ptype](self, m)
                elif self._channel_handler_table.has_key(ptype):
                    chanid = m.get_int()
                    if self.channels.has_key(chanid):
                        self._channel_handler_table[ptype](self.channels[chanid], m)
                else:
                    self._log(WARNING, 'Oops, unhandled type %d' % ptype)
                    msg = Message()
                    msg.add_byte(chr(MSG_UNIMPLEMENTED))
                    msg.add_int(m.seqno)
                    self._send_message(msg)
        except SSHException, e:
            self._log(DEBUG, 'Exception: ' + str(e))
            self._log(DEBUG, tb_strings())
            self.saved_exception = e
        except EOFError, e:
            self._log(DEBUG, 'EOF')
            self._log(DEBUG, tb_strings())
            self.saved_exception = e
        except Exception, e:
            self._log(DEBUG, 'Unknown exception: ' + str(e))
            self._log(DEBUG, tb_strings())
            self.saved_exception = e
        _active_threads.remove(self)
        if self.active:
            self.active = False
            if self.completion_event != None:
                self.completion_event.set()
            if self.auth_event != None:
                self.auth_event.set()
            for event in self.channel_events.values():
                event.set()
        self.sock.close()


    ###  protocol stages


    def _negotiate_keys(self, m):
        # throws SSHException on anything unusual
        if self.local_kex_init == None:
            # remote side wants to renegotiate
            self._send_kex_init()
        self._parse_kex_init(m)
        self.kex_engine.start_kex()

    def _check_banner(self):
        # this is slow, but we only have to do it once
        for i in range(5):
            buffer = ''
            while not '\n' in buffer:
                buffer += self._read_all(1)
            buffer = buffer[:-1]
            if (len(buffer) > 0) and (buffer[-1] == '\r'):
                buffer = buffer[:-1]
            if buffer[:4] == 'SSH-':
                break
            self._log(DEBUG, 'Banner: ' + buffer)
        if buffer[:4] != 'SSH-':
            raise SSHException('Indecipherable protocol version "' + buffer + '"')
        # save this server version string for later
        self.remote_version = buffer
        # pull off any attached comment
        comment = ''
        i = string.find(buffer, ' ')
        if i >= 0:
            comment = buffer[i+1:]
            buffer = buffer[:i]
        # parse out version string and make sure it matches
        segs = buffer.split('-', 2)
        if len(segs) < 3:
            raise SSHException('Invalid SSH banner')
        version = segs[1]
        client = segs[2]
        if version != '1.99' and version != '2.0':
            raise SSHException('Incompatible version (%s instead of 2.0)' % (version,))
        self._log(INFO, 'Connected (version %s, client %s)' % (version, client))

    def _send_kex_init(self):
        """
        announce to the other side that we'd like to negotiate keys, and what
        kind of key negotiation we support.
        """
        if self.server_mode:
            if (self._modulus_pack is None) and ('diffie-hellman-group-exchange-sha1' in self.preferred_kex):
                # can't do group-exchange if we don't have a pack of potential primes
                self.preferred_kex.remove('diffie-hellman-group-exchange-sha1')
            available_server_keys = filter(self.server_key_dict.keys().__contains__,
                                           self.preferred_keys)
        else:
            available_server_keys = self.preferred_keys

        m = Message()
        m.add_byte(chr(MSG_KEXINIT))
        m.add_bytes(randpool.get_bytes(16))
        m.add(','.join(self.preferred_kex))
        m.add(','.join(available_server_keys))
        m.add(','.join(self.preferred_ciphers))
        m.add(','.join(self.preferred_ciphers))
        m.add(','.join(self.preferred_macs))
        m.add(','.join(self.preferred_macs))
        m.add('none')
        m.add('none')
        m.add('')
        m.add('')
        m.add_boolean(0)
        m.add_int(0)
        # save a copy for later (needed to compute a hash)
        self.local_kex_init = str(m)
        self._send_message(m)

    def _parse_kex_init(self, m):
        # reset counters of when to re-key, since we are now re-keying
        self.received_bytes = 0
        self.received_packets = 0
        self.received_packets_overflow = 0

        cookie = m.get_bytes(16)
        kex_algo_list = m.get_list()
        server_key_algo_list = m.get_list()
        client_encrypt_algo_list = m.get_list()
        server_encrypt_algo_list = m.get_list()
        client_mac_algo_list = m.get_list()
        server_mac_algo_list = m.get_list()
        client_compress_algo_list = m.get_list()
        server_compress_algo_list = m.get_list()
        client_lang_list = m.get_list()
        server_lang_list = m.get_list()
        kex_follows = m.get_boolean()
        unused = m.get_int()

        # no compression support (yet?)
        if (not('none' in client_compress_algo_list) or
            not('none' in server_compress_algo_list)):
            raise SSHException('Incompatible ssh peer.')

        # as a server, we pick the first item in the client's list that we support.
        # as a client, we pick the first item in our list that the server supports.
        if self.server_mode:
            agreed_kex = filter(self.preferred_kex.__contains__, kex_algo_list)
        else:
            agreed_kex = filter(kex_algo_list.__contains__, self.preferred_kex)
        if len(agreed_kex) == 0:
            raise SSHException('Incompatible ssh peer (no acceptable kex algorithm)')
        self.kex_engine = self._kex_info[agreed_kex[0]](self)

        if self.server_mode:
            available_server_keys = filter(self.server_key_dict.keys().__contains__,
                                           self.preferred_keys)
            agreed_keys = filter(available_server_keys.__contains__, server_key_algo_list)
        else:
            agreed_keys = filter(server_key_algo_list.__contains__, self.preferred_keys)
        if len(agreed_keys) == 0:
            raise SSHException('Incompatible ssh peer (no acceptable host key)')
        self.host_key_type = agreed_keys[0]
        if self.server_mode and (self.get_server_key() is None):
            raise SSHException('Incompatible ssh peer (can\'t match requested host key type)')

        if self.server_mode:
            agreed_local_ciphers = filter(self.preferred_ciphers.__contains__,
                                           server_encrypt_algo_list)
            agreed_remote_ciphers = filter(self.preferred_ciphers.__contains__,
                                          client_encrypt_algo_list)
        else:
            agreed_local_ciphers = filter(client_encrypt_algo_list.__contains__,
                                          self.preferred_ciphers)
            agreed_remote_ciphers = filter(server_encrypt_algo_list.__contains__,
                                           self.preferred_ciphers)
        if (len(agreed_local_ciphers) == 0) or (len(agreed_remote_ciphers) == 0):
            raise SSHException('Incompatible ssh server (no acceptable ciphers)')
        self.local_cipher = agreed_local_ciphers[0]
        self.remote_cipher = agreed_remote_ciphers[0]
        self._log(DEBUG, 'Ciphers agreed: local=%s, remote=%s' % (self.local_cipher, self.remote_cipher))

        if self.server_mode:
            agreed_remote_macs = filter(self.preferred_macs.__contains__, client_mac_algo_list)
            agreed_local_macs = filter(self.preferred_macs.__contains__, server_mac_algo_list)
        else:
            agreed_local_macs = filter(client_mac_algo_list.__contains__, self.preferred_macs)
            agreed_remote_macs = filter(server_mac_algo_list.__contains__, self.preferred_macs)
        if (len(agreed_local_macs) == 0) or (len(agreed_remote_macs) == 0):
            raise SSHException('Incompatible ssh server (no acceptable macs)')
        self.local_mac = agreed_local_macs[0]
        self.remote_mac = agreed_remote_macs[0]

        self._log(DEBUG, 'kex algos:' + str(kex_algo_list) + ' server key:' + str(server_key_algo_list) + \
                  ' client encrypt:' + str(client_encrypt_algo_list) + \
                  ' server encrypt:' + str(server_encrypt_algo_list) + \
                  ' client mac:' + str(client_mac_algo_list) + \
                  ' server mac:' + str(server_mac_algo_list) + \
                  ' client compress:' + str(client_compress_algo_list) + \
                  ' server compress:' + str(server_compress_algo_list) + \
                  ' client lang:' + str(client_lang_list) + \
                  ' server lang:' + str(server_lang_list) + \
                  ' kex follows?' + str(kex_follows))
        self._log(DEBUG, 'using kex %s; server key type %s; cipher: local %s, remote %s; mac: local %s, remote %s' %
                  (agreed_kex[0], self.host_key_type, self.local_cipher, self.remote_cipher, self.local_mac,
                   self.remote_mac))

        # save for computing hash later...
        # now wait!  openssh has a bug (and others might too) where there are
        # actually some extra bytes (one NUL byte in openssh's case) added to
        # the end of the packet but not parsed.  turns out we need to throw
        # away those bytes because they aren't part of the hash.
        self.remote_kex_init = chr(MSG_KEXINIT) + m.get_so_far()

    def _activate_inbound(self):
        "switch on newly negotiated encryption parameters for inbound traffic"
        self.block_size_in = self._cipher_info[self.remote_cipher]['block-size']
        if self.server_mode:
            IV_in = self._compute_key('A', self.block_size_in)
            key_in = self._compute_key('C', self._cipher_info[self.remote_cipher]['key-size'])
        else:
            IV_in = self._compute_key('B', self.block_size_in)
            key_in = self._compute_key('D', self._cipher_info[self.remote_cipher]['key-size'])
        self.engine_in = self._get_cipher(self.remote_cipher, key_in, IV_in)
        self.remote_mac_len = self._mac_info[self.remote_mac]['size']
        self.remote_mac_engine = self._mac_info[self.remote_mac]['class']
        # initial mac keys are done in the hash's natural size (not the potentially truncated
        # transmission size)
        if self.server_mode:
            self.mac_key_in = self._compute_key('E', self.remote_mac_engine.digest_size)
        else:
            self.mac_key_in = self._compute_key('F', self.remote_mac_engine.digest_size)

    def _activate_outbound(self):
        "switch on newly negotiated encryption parameters for outbound traffic"
        m = Message()
        m.add_byte(chr(MSG_NEWKEYS))
        self._send_message(m)
        self.block_size_out = self._cipher_info[self.local_cipher]['block-size']
        if self.server_mode:
            IV_out = self._compute_key('B', self.block_size_out)
            key_out = self._compute_key('D', self._cipher_info[self.local_cipher]['key-size'])
        else:
            IV_out = self._compute_key('A', self.block_size_out)
            key_out = self._compute_key('C', self._cipher_info[self.local_cipher]['key-size'])
        self.engine_out = self._get_cipher(self.local_cipher, key_out, IV_out)
        self.local_mac_len = self._mac_info[self.local_mac]['size']
        self.local_mac_engine = self._mac_info[self.local_mac]['class']
        # initial mac keys are done in the hash's natural size (not the potentially truncated
        # transmission size)
        if self.server_mode:
            self.mac_key_out = self._compute_key('F', self.local_mac_engine.digest_size)
        else:
            self.mac_key_out = self._compute_key('E', self.local_mac_engine.digest_size)
        # we always expect to receive NEWKEYS now
        self.expected_packet = MSG_NEWKEYS

    def _parse_newkeys(self, m):
        self._log(DEBUG, 'Switch to new keys ...')
        self._activate_inbound()
        # can also free a bunch of stuff here
        self.local_kex_init = self.remote_kex_init = None
        self.e = self.f = self.K = self.x = None
        if not self.initial_kex_done:
            # this was the first key exchange
            self.initial_kex_done = 1
        # send an event?
        if self.completion_event != None:
            self.completion_event.set()
        return

    def _parse_disconnect(self, m):
        code = m.get_int()
        desc = m.get_string()
        self._log(INFO, 'Disconnect (code %d): %s' % (code, desc))

    def _parse_global_request(self, m):
        kind = m.get_string()
        want_reply = m.get_boolean()
        ok = self.check_global_request(kind, m)
        extra = ()
        if type(ok) is tuple:
            extra = ok
            ok = True
        if want_reply:
            msg = Message()
            if ok:
                msg.add_byte(chr(MSG_REQUEST_SUCCESS))
                for item in extra:
                    msg.add(item)
            else:
                msg.add_byte(chr(MSG_REQUEST_FAILURE))
            self._send_message(msg)

    def _parse_request_success(self, m):
        self.global_response = m
        if self.completion_event is not None:
            self.completion_event.set()
        
    def _parse_request_failure(self, m):
        self.global_response = None
        if self.completion_event is not None:
            self.completion_event.set()

    def _parse_channel_open_success(self, m):
        chanid = m.get_int()
        server_chanid = m.get_int()
        server_window_size = m.get_int()
        server_max_packet_size = m.get_int()
        if not self.channels.has_key(chanid):
            self._log(WARNING, 'Success for unrequested channel! [??]')
            return
        try:
            self.lock.acquire()
            chan = self.channels[chanid]
            chan._set_remote_channel(server_chanid, server_window_size, server_max_packet_size)
            self._log(INFO, 'Secsh channel %d opened.' % chanid)
            if self.channel_events.has_key(chanid):
                self.channel_events[chanid].set()
                del self.channel_events[chanid]
        finally:
            self.lock.release()
        return

    def _parse_channel_open_failure(self, m):
        chanid = m.get_int()
        reason = m.get_int()
        reason_str = m.get_string()
        lang = m.get_string()
        if _CONNECTION_FAILED_CODE.has_key(reason):
            reason_text = _CONNECTION_FAILED_CODE[reason]
        else:
            reason_text = '(unknown code)'
        self._log(INFO, 'Secsh channel %d open FAILED: %s: %s' % (chanid, reason_str, reason_text))
        try:
            self.lock.aquire()
            if self.channels.has_key(chanid):
                del self.channels[chanid]
                if self.channel_events.has_key(chanid):
                    self.channel_events[chanid].set()
                    del self.channel_events[chanid]
        finally:
            self.lock.release()
        return

    def _parse_channel_open(self, m):
        kind = m.get_string()
        chanid = m.get_int()
        initial_window_size = m.get_int()
        max_packet_size = m.get_int()
        reject = False
        if not self.server_mode:
            self._log(DEBUG, 'Rejecting "%s" channel request from server.' % kind)
            reject = True
            reason = self.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        else:
            try:
                self.lock.acquire()
                my_chanid = self.channel_counter
                self.channel_counter += 1
            finally:
                self.lock.release()
            chan = self.check_channel_request(kind, my_chanid)
            if (chan is None) or (type(chan) is int):
                self._log(DEBUG, 'Rejecting "%s" channel request from client.' % kind)
                reject = True
                if type(chan) is int:
                    reason = chan
                else:
                    reason = self.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        if reject:
            msg = Message()
            msg.add_byte(chr(MSG_CHANNEL_OPEN_FAILURE))
            msg.add_int(chanid)
            msg.add_int(reason)
            msg.add_string('')
            msg.add_string('en')
            self._send_message(msg)
            return
        try:
            self.lock.acquire()
            self.channels[my_chanid] = chan
            chan._set_transport(self)
            chan._set_window(self.window_size, self.max_packet_size)
            chan._set_remote_channel(chanid, initial_window_size, max_packet_size)
        finally:
            self.lock.release()
        m = Message()
        m.add_byte(chr(MSG_CHANNEL_OPEN_SUCCESS))
        m.add_int(chanid)
        m.add_int(my_chanid)
        m.add_int(self.window_size)
        m.add_int(self.max_packet_size)
        self._send_message(m)
        self._log(INFO, 'Secsh channel %d opened.' % my_chanid)
        try:
            self.lock.acquire()
            self.server_accepts.append(chan)
            self.server_accept_cv.notify()
        finally:
            self.lock.release()

    def _parse_debug(self, m):
        always_display = m.get_boolean()
        msg = m.get_string()
        lang = m.get_string()
        self._log(DEBUG, 'Debug msg: ' + safe_string(msg))

    _handler_table = {
        MSG_NEWKEYS: _parse_newkeys,
        MSG_GLOBAL_REQUEST: _parse_global_request,
        MSG_REQUEST_SUCCESS: _parse_request_success,
        MSG_REQUEST_FAILURE: _parse_request_failure,
        MSG_CHANNEL_OPEN_SUCCESS: _parse_channel_open_success,
        MSG_CHANNEL_OPEN_FAILURE: _parse_channel_open_failure,
        MSG_CHANNEL_OPEN: _parse_channel_open,
        MSG_KEXINIT: _negotiate_keys,
        }

    _channel_handler_table = {
        MSG_CHANNEL_SUCCESS: Channel._request_success,
        MSG_CHANNEL_FAILURE: Channel._request_failed,
        MSG_CHANNEL_DATA: Channel._feed,
        MSG_CHANNEL_WINDOW_ADJUST: Channel._window_adjust,
        MSG_CHANNEL_REQUEST: Channel._handle_request,
        MSG_CHANNEL_EOF: Channel._handle_eof,
        MSG_CHANNEL_CLOSE: Channel._handle_close,
        }
