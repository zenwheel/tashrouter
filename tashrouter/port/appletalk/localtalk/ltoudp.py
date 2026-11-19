'''Port that connects to LToUDP.'''

import errno
import os
import select
import socket
import struct
from threading import Thread, Event
import time

from . import LocalTalkPort
from .ltoudp_hub import LtoudpHub
from ....netlog import log_localtalk_frame_inbound, log_localtalk_frame_outbound


class LtoudpPort(LocalTalkPort):
  '''Port that connects to LToUDP.

  With hub=True, the port also relays LToUDP by unicast for remote machines on networks without multicast, such as a
  Tailscale tailnet, so they join the same LocalTalk segment (see ltoudp_hub.py).  The hub options are:

  multicast: whether to use the LToUDP multicast group at all (default True); False makes the port hub only.
  hub_address: the address the hub listens on (default 0.0.0.0; use '::' for IPv4 and IPv6, or a tailnet address).
  hub_port: the UDP port the hub listens on (default 1955).
  hub_allowed_networks: networks peers may come from, as a list of strings, or 'any' (default: Tailscale's 100.64.0.0/10 and
    fd7a:115c:a1e0::/48).
  hub_max_peers: the most peers at once (default 16).
  hub_peer_timeout: seconds with nothing heard before a peer is forgotten (default 60).
  hub_proxy_ack: whether the hub answers ENQs for node IDs in use elsewhere on the segment (default True).
  '''

  LTOUDP_GROUP = '239.192.76.84'  # the last two octets spell 'LT'
  LTOUDP_PORT = 1954

  DEFAULT_INTF_ADDRESS = '0.0.0.0'

  SELECT_TIMEOUT = 0.25  # seconds
  NETWORK_UP_RETRY_TIMEOUT = 1  # seconds
  NETWORK_UP_RETRY_COUNT = 10

  def __init__(self, intf_address=DEFAULT_INTF_ADDRESS, multicast=True, hub=False, hub_address=LtoudpHub.DEFAULT_ADDRESS,
               hub_port=LtoudpHub.DEFAULT_PORT, hub_allowed_networks=LtoudpHub.DEFAULT_ALLOWED_NETWORKS,
               hub_max_peers=LtoudpHub.DEFAULT_MAX_PEERS, hub_peer_timeout=LtoudpHub.DEFAULT_PEER_TIMEOUT, hub_proxy_ack=True,
               **kwargs):
    if not multicast and not hub: raise ValueError('an LToUDP port needs multicast, a hub, or both')
    super().__init__(respond_to_enq=True, **kwargs)
    self._intf_address = intf_address
    self._multicast = multicast
    self._socket = None
    self._sender_id = struct.pack('>L', os.getpid())
    self._hub = None
    if hub:
      self._hub = LtoudpHub(self.short_str(), self._sender_id, self._send_multicast if multicast else None,
                            address=hub_address, port=hub_port, allowed_networks=hub_allowed_networks,
                            max_peers=hub_max_peers, peer_timeout=hub_peer_timeout, proxy_ack=hub_proxy_ack)
    self._thread = None
    self._started_event = Event()
    self._stop_requested = False
    self._stopped_event = Event()

  def short_str(self):
    if self._intf_address == self.DEFAULT_INTF_ADDRESS:
      return 'LToUDP'
    else:
      return self._intf_address

  __str__ = short_str
  __repr__ = short_str

  def _open_multicast_socket(self):
    '''Open, bind and configure the socket for the LToUDP multicast group.'''
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, 'SO_REUSEPORT'): sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind((self._intf_address, self.LTOUDP_PORT))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    for attempt in range(self.NETWORK_UP_RETRY_COUNT):
      try:
        # this raises "OSError: [Errno 19] No such device" if network is not up, so build in some retry logic
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                        socket.inet_aton(self.LTOUDP_GROUP) + socket.inet_aton(self._intf_address))
        break
      except OSError as e:
        if e.errno != errno.ENODEV or attempt + 1 == self.NETWORK_UP_RETRY_COUNT: raise
        time.sleep(self.NETWORK_UP_RETRY_TIMEOUT)
    return sock

  def start(self, router):
    if self._multicast: self._socket = self._open_multicast_socket()
    if self._hub: self._hub.open()
    super().start(router)
    self._thread = Thread(target=self._run)
    self._thread.start()
    self._started_event.wait()

  def stop(self):
    super().stop()
    self._stop_requested = True
    self._stopped_event.wait()
    if self._hub: self._hub.close()

  def _send_multicast(self, data):
    '''Send a datagram to the LToUDP multicast group.'''
    self._socket.sendto(data, (self.LTOUDP_GROUP, self.LTOUDP_PORT))

  def send_frame(self, frame_data):
    log_localtalk_frame_outbound(frame_data, self)
    if self._multicast: self._send_multicast(self._sender_id + frame_data)
    if self._hub:
      # if the router is asking for a node ID that a node elsewhere on the segment has, the hub answers for that node
      if ack := self._hub.from_router(self._sender_id + frame_data): self._receive_frame(ack)

  def _receive_frame(self, frame_data):
    log_localtalk_frame_inbound(frame_data, self)
    self.inbound_frame(frame_data)

  def _run(self):
    sockets = tuple(i for i in (self._socket, self._hub.socket if self._hub else None) if i is not None)
    self._started_event.set()
    while not self._stop_requested:
      if self._hub: self._hub.housekeeping()
      rlist, _, _ = select.select(sockets, (), (), self.SELECT_TIMEOUT)
      if self._socket in rlist:
        data, sender_addr = self._socket.recvfrom(65507)
        if len(data) < 7: pass
        elif data[0:4] == self._sender_id: pass  #TODO check sender_addr too
        elif self._hub and self._hub.is_own_repeat(data): pass  # a hub peer's frame we repeated, come back to us
        else:
          self._receive_frame(data[4:])
          if self._hub: self._hub.from_multicast(data)
      if self._hub and self._hub.socket in rlist:
        if frame_data := self._hub.from_peer(): self._receive_frame(frame_data)
    self._stopped_event.set()
