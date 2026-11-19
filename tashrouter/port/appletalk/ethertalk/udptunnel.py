'''Port driver for EtherTalk using Basilisk II's "udptunnel" format.

Basilisk II (and SheepShaver, with a matching patch) can tunnel its emulated Ethernet over UDP instead of using a tap or a
raw socket.  It is enabled with the prefs lines "udptunnel true" and "udpport 6066" (6066 is the default).  The format, from
BasiliskII/src/ether.cpp:

- Each UDP datagram is one complete raw Ethernet frame (destination, source, type/length, payload) with no extra header and
  no FCS.
- Each node's Ethernet address is 'B' '2' followed by its IPv4 address in network order.  Basilisk II binds INADDR_ANY, so in
  practice the address comes from gethostbyname(gethostname()).
- A frame whose destination starts with 'B' '2' is sent to that IPv4 address on udpport.  Frames to the AppleTalk broadcast
  (09:00:07:FF:FF:FF) or the Ethernet broadcast are sent to 255.255.255.255 on udpport.  Anything else, including the
  AppleTalk zone multicasts, is not sent.
- Every datagram received is taken as a frame, except ones whose source address is the node's own.  There is no filtering on
  the destination address.
'''

import errno
import ipaddress
import logging
from queue import Queue
from select import select
import socket
import struct
import sys
from threading import Thread, Event

from . import EtherTalkPort


class UdpTunnelPort(EtherTalkPort):
  '''Port driver for EtherTalk using Basilisk II's "udptunnel" format.

  port: the UDP port, which must match Basilisk II's "udpport" (default 6066).
  broadcast_address: where to send broadcast and multicast frames.  An IPv4 address (255.255.255.255 by default, or a
    subnet's directed broadcast such as 192.168.1.255) or the name of a network interface (such as 'en0' or 'eth0'), in which
    case that interface's broadcast address is used.  macOS sends 255.255.255.255 only out of the primary interface, so on a
    host with more than one interface, give a directed broadcast address or an interface name.
  local_address: this router's IPv4 address, from which its Ethernet address is made.  By default it is bind_address if that
    is not 0.0.0.0, else the address of the interface given as broadcast_address, else the address of the interface that
    routes to broadcast_address.  It must differ from the address of every Basilisk II on the network.
  bind_address: the address to bind the socket to (default 0.0.0.0).  A socket bound to a unicast address does not receive
    broadcasts, so leave this as 0.0.0.0 except for testing.  Basilisk II binds 0.0.0.0 without SO_REUSEADDR, so the router
    can't share a host (more precisely, an IP stack) with a Basilisk II; run it on another host, VM or container.
  '''

  DEFAULT_PORT = 6066
  DEFAULT_BROADCAST_ADDRESS = '255.255.255.255'
  DEFAULT_BIND_ADDRESS = '0.0.0.0'

  HW_ADDR_PREFIX = b'B2'
  ETHERNET_HEADER_LENGTH = 14
  MAX_DATAGRAM_LENGTH = 65507

  SELECT_TIMEOUT = 0.25  # seconds

  # ioctls for looking up an interface's IPv4 address and broadcast address; struct ifreq is 40 bytes on Linux, 32 on BSD
  if sys.platform.startswith('linux'):
    SIOCGIFADDR = 0x8915
    SIOCGIFBRDADDR = 0x8919
    IFREQ_LENGTH = 40
  else:
    SIOCGIFADDR = 0xC0206921
    SIOCGIFBRDADDR = 0xC0206923
    IFREQ_LENGTH = 32

  def __init__(self, port=DEFAULT_PORT, broadcast_address=DEFAULT_BROADCAST_ADDRESS, local_address=None,
               bind_address=DEFAULT_BIND_ADDRESS, **kwargs):
    super().__init__(None, **kwargs)  # hardware address is determined at start time, since it depends on the network
    self._udp_port = port
    self._broadcast_address_setting = broadcast_address
    self._local_address_setting = local_address
    self._bind_address = bind_address
    self._broadcast_ip = None
    self._local_ip = None
    self._socket = None
    self._peer_ips = {}  # loopback 'B2' Ethernet address -> IP address its frames came from
    self._reader_thread = None
    self._reader_started_event = Event()
    self._reader_stop_requested = False
    self._reader_stopped_event = Event()
    self._writer_thread = None
    self._writer_started_event = Event()
    self._writer_stop_flag = object()
    self._writer_stopped_event = Event()
    self._writer_queue = Queue()

  def short_str(self):
    if self._udp_port == self.DEFAULT_PORT:
      return 'UDPTunnel'
    else:
      return 'UDPTunnel:%d' % self._udp_port

  __str__ = short_str
  __repr__ = short_str

  @staticmethod
  def _is_ipv4_address(s):
    '''Return True if the given string is a dotted-quad IPv4 address.'''
    try:
      ipaddress.IPv4Address(s)
    except ValueError:
      return False
    return True

  @classmethod
  def interface_addresses(cls, interface_name):
    '''Return the IPv4 address and broadcast address of the named network interface as a tuple of strings.'''
    try:
      from fcntl import ioctl
    except ImportError:
      raise OSError('interface names are not supported on this platform, give an IPv4 broadcast address instead') from None
    name = interface_name.encode('ascii')
    if not 0 < len(name) < 16: raise ValueError('invalid interface name %r' % interface_name)
    results = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
      for request in (cls.SIOCGIFADDR, cls.SIOCGIFBRDADDR):
        try:
          ifreq = ioctl(s.fileno(), request, struct.pack('16s', name).ljust(cls.IFREQ_LENGTH, b'\0'))
        except OSError as e:
          raise OSError(e.errno, 'interface %s has no IPv4 %s address' % (interface_name,
                                                                        'broadcast' if request == cls.SIOCGIFBRDADDR else
                                                                        'unicast')) from None
        results.append(socket.inet_ntoa(ifreq[20:24]))  # sockaddr_in.sin_addr, after the 16-byte name and 4 bytes of header
    return tuple(results)

  @staticmethod
  def route_source_address(destination):
    '''Return the local IPv4 address the operating system would send from to reach the given destination.'''
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
      s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
      s.connect((destination, 9))  # UDP connect sends nothing, it only picks a route and a source address
      return s.getsockname()[0]

  def _resolve_addresses(self):
    '''Determine the broadcast and local IP addresses from the settings given to the constructor.'''
    interface_ip = None
    if self._is_ipv4_address(self._broadcast_address_setting):
      self._broadcast_ip = self._broadcast_address_setting
    else:
      interface_ip, self._broadcast_ip = self.interface_addresses(self._broadcast_address_setting)
    if self._local_address_setting:
      self._local_ip = self._local_address_setting
    elif self._bind_address != self.DEFAULT_BIND_ADDRESS:
      self._local_ip = self._bind_address
    elif interface_ip:
      self._local_ip = interface_ip
    else:
      try:
        self._local_ip = self.route_source_address(self._broadcast_ip)
      except OSError as e:
        raise OSError(e.errno, "can't find a local address that routes to %s, give local_address" % self._broadcast_ip) from None
    if not self._is_ipv4_address(self._local_ip) or self._local_ip == '0.0.0.0':
      raise ValueError('invalid local address %r, give local_address' % self._local_ip)

  def start(self, router):
    self._resolve_addresses()
    self._hw_addr = self.HW_ADDR_PREFIX + socket.inet_aton(self._local_ip)
    logging.info('%s using address %s (Ethernet address %s), broadcasting to %s:%d', str(self), self._local_ip,
                 ':'.join('%02X' % i for i in self._hw_addr), self._broadcast_ip, self._udp_port)
    self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
      self._socket.bind((self._bind_address, self._udp_port))
    except OSError as e:
      self._socket.close()
      if e.errno == errno.EADDRINUSE:
        raise OSError(e.errno, 'UDP port %d is in use on %s; a Basilisk II on this host? (the router needs its own IP address)' %
                      (self._udp_port, self._bind_address)) from None
      raise
    super().start(router)
    self._reader_thread = Thread(target=self._reader_run)
    self._reader_thread.start()
    self._writer_thread = Thread(target=self._writer_run)
    self._writer_thread.start()
    self._reader_started_event.wait()
    self._writer_started_event.wait()

  def stop(self):
    self._reader_stop_requested = True
    self._writer_queue.put(self._writer_stop_flag)
    self._reader_stopped_event.wait()
    self._writer_stopped_event.wait()
    self._socket.close()
    super().stop()

  def send_frame(self, frame_data):
    self._writer_queue.put(frame_data)

  def destination_ip(self, frame_data):
    '''Return the IP address to which an Ethernet frame should be sent, or None if it should not be sent.'''
    if len(frame_data) < self.ETHERNET_HEADER_LENGTH: return None
    if frame_data[0] & 0x01: return self._broadcast_ip  # any group address: broadcast or (AppleTalk zone) multicast
    if not frame_data.startswith(self.HW_ADDR_PREFIX): return None  # not a Basilisk II address, can't send there
    destination_ip = socket.inet_ntoa(frame_data[2:6])
    if frame_data[2] == 127:
      # a Basilisk II whose host name resolves to a loopback address (such as Debian's 127.0.1.1) has a 'B2' address that
      # doesn't reach it from here, so use the address its frames came from instead, unless that address is loopback or our
      # own, which means it's on this host and the 'B2' address will reach it (or that sending there would loop back to us)
      peer_ip = self._peer_ips.get(frame_data[0:6])
      if peer_ip and not peer_ip.startswith('127.') and peer_ip not in (self._local_ip, self._bind_address):
        return peer_ip
    return destination_ip

  def accept_datagram(self, data, sender_ip):
    '''Decide whether a UDP datagram should be taken as an inbound frame, and learn where loopback 'B2' addresses are.'''
    if len(data) < self.ETHERNET_HEADER_LENGTH: return False
    source_hw_addr = data[6:12]
    if source_hw_addr == self._hw_addr: return False  # our own broadcast, come back to us
    if source_hw_addr.startswith(self.HW_ADDR_PREFIX) and source_hw_addr[2] == 127: self._peer_ips[source_hw_addr] = sender_ip
    return True

  def _reader_run(self):
    self._reader_started_event.set()
    while not self._reader_stop_requested:
      rlist, _, _ = select((self._socket,), (), (), self.SELECT_TIMEOUT)
      if self._socket not in rlist: continue
      try:
        data, sender_addr = self._socket.recvfrom(self.MAX_DATAGRAM_LENGTH)
      except OSError as e:
        logging.debug('%s error receiving: %s', str(self), e)  # e.g. ICMP port unreachable reported on Windows
        continue
      if self.accept_datagram(data, sender_addr[0]): self.inbound_frame(data)
    self._reader_stopped_event.set()

  def _writer_run(self):
    self._writer_started_event.set()
    while True:
      frame_data = self._writer_queue.get()
      if frame_data is self._writer_stop_flag: break
      destination_ip = self.destination_ip(frame_data)
      if destination_ip is None: continue
      try:
        self._socket.sendto(frame_data, (destination_ip, self._udp_port))
      except OSError as e:
        logging.debug('%s error sending to %s: %s', str(self), destination_ip, e)
    self._writer_stopped_event.set()
