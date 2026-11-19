'''Unicast relay ("hub") for LToUDP, so that remote machines can join a LocalTalk segment over a unicast-only network.

LToUDP carries one LocalTalk (LLAP) frame per UDP datagram, prefixed by a 4-byte sender ID, multicast to 239.192.76.84 port
1954.  Networks such as a Tailscale tailnet carry no multicast, so a remote machine can't hear or reach the group.  The hub
listens on a unicast address and port instead, and acts as a repeater: the multicast group, the router and every hub peer
form one LocalTalk segment, with one network number and zone.

The hub client protocol is LToUDP itself, sent by unicast:
- A client sends [4-byte sender ID][LLAP frame] to the hub, and receives the same from the hub.
- A client sends a datagram of only its 4-byte sender ID as a keepalive, about every 20 seconds while idle.  This keeps it
  on the hub's peer list and keeps stateful paths (such as Tailscale node sharing, where only the client can open the flow)
  open.  Keepalives are not repeated or delivered.
- Peers are learned from their first valid datagram and forgotten after a while with nothing heard.
'''

from collections import deque
import ipaddress
import logging
import socket
from threading import Lock
import time


class LtoudpHub:
  '''Unicast relay for an LtoudpPort.  Not used directly; give hub=True and the hub_* options to LtoudpPort.'''

  DEFAULT_ADDRESS = '0.0.0.0'
  DEFAULT_PORT = 1955
  DEFAULT_ALLOWED_NETWORKS = ('100.64.0.0/10', 'fd7a:115c:a1e0::/48')  # Tailscale's IPv4 and IPv6 ranges
  DEFAULT_MAX_PEERS = 16
  DEFAULT_PEER_TIMEOUT = 60  # seconds

  SENDER_ID_LENGTH = 4
  LLAP_HEADER_LENGTH = 3
  LLAP_ENQ = 0x81
  LLAP_ACK = 0x82

  MAX_DATAGRAM_LENGTH = 65507
  RECENT_REPEAT_TIMEOUT = 2  # seconds to remember a datagram the hub repeated to the multicast group
  HOUSEKEEPING_INTERVAL = 1  # seconds
  REFUSAL_LOG_INTERVAL = 60  # seconds

  MULTICAST = 'multicast'  # owner of node IDs heard on the multicast side
  ROUTER = 'router'  # where frames sent by the router come from

  def __init__(self, port_str, sender_id, send_multicast, address=DEFAULT_ADDRESS, port=DEFAULT_PORT,
               allowed_networks=DEFAULT_ALLOWED_NETWORKS, max_peers=DEFAULT_MAX_PEERS, peer_timeout=DEFAULT_PEER_TIMEOUT,
               proxy_ack=True):
    '''port_str names the owning port in logs; sender_id is its LToUDP sender ID; send_multicast is a function that sends a
    datagram to the multicast group, or None if the port has no multicast side.
    '''
    self._port_str = port_str
    self._sender_id = sender_id
    self._send_multicast = send_multicast
    self._address = address
    self._port = port
    if allowed_networks == 'any':
      self._allowed_networks = None
    else:
      if isinstance(allowed_networks, str): allowed_networks = (allowed_networks,)
      self._allowed_networks = tuple(ipaddress.ip_network(i) for i in allowed_networks)
    self._max_peers = max_peers
    self._peer_timeout = peer_timeout
    self._proxy_ack = proxy_ack
    self.socket = None
    self._lock = Lock()
    self._peers = {}  # (host, port) -> [socket address to send to, time.monotonic() when last heard]
    self._nodes = {}  # LLAP node ID -> (owner: a peer key or MULTICAST, time.monotonic() when last heard)
    self._recent_repeats = {}  # datagram repeated to the multicast group -> [count not yet seen back, expiry time]
    self._next_housekeeping = 0
    self._last_refusal_log = None

  def open(self):
    '''Open and bind the hub's socket.'''
    family = socket.AF_INET6 if ':' in self._address else socket.AF_INET
    self.socket = socket.socket(family, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    if family == socket.AF_INET6 and ipaddress.ip_address(self._address.split('%')[0]).is_unspecified:
      self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)  # '::' takes IPv4 peers too
    self.socket.bind((self._address, self._port))
    logging.info('%s hub listening on %s port %d', self._port_str, self._address, self._port)

  def close(self):
    '''Close the hub's socket.'''
    self.socket.close()

  @staticmethod
  def _normalize_host(host):
    '''Return an ip_address for a socket address host, turning IPv4-mapped IPv6 addresses back into IPv4.'''
    address = ipaddress.ip_address(host.split('%')[0])
    if address.version == 6 and address.ipv4_mapped: address = address.ipv4_mapped
    return address

  def _allowed(self, address):
    if self._allowed_networks is None: return True
    return any(address in network for network in self._allowed_networks)

  def _sendto(self, data, socket_address):
    try:
      self.socket.sendto(data, socket_address)
    except OSError as e:
      logging.debug('%s hub error sending to %s: %s', self._port_str, socket_address[0], e)

  def _send_to_peers(self, data, exclude=None):
    '''Send a datagram to every live peer except the one given.'''
    with self._lock: addresses = [address for key, (address, _) in self._peers.items() if key != exclude]
    for address in addresses: self._sendto(data, address)

  def _repeat_to_multicast(self, data):
    '''Send a datagram to the multicast group, remembering it so its multicast loopback copy can be recognized.'''
    if self._send_multicast is None: return
    with self._lock:
      entry = self._recent_repeats.setdefault(data, [0, 0])
      entry[0] += 1
      entry[1] = time.monotonic() + self.RECENT_REPEAT_TIMEOUT
    self._send_multicast(data)

  def is_own_repeat(self, data):
    '''Return True (once) if a datagram from the multicast group is one the hub repeated there itself.'''
    with self._lock:
      entry = self._recent_repeats.get(data)
      if entry is None: return False
      entry[0] -= 1
      if entry[0] <= 0: self._recent_repeats.pop(data)
      return True

  def _note_frame(self, frame, origin):
    '''Track node IDs in use and work out whether to answer an ENQ on its owner's behalf.  Returns an ACK frame or None.

    LLAP node ID acquisition assumes a fast shared wire: a node sends a few ENQs for the ID it wants and, if nobody ACKs within
    a few hundred microseconds, takes it.  Across a hub, an ENQ and the owner's ACK can each take tens of milliseconds, so a
    remote node may take an ID that is already in use.  The hub keeps a table of the source node IDs heard from each peer and
    from the multicast side.  When an ENQ arrives for an ID in use somewhere else on the segment, the hub ACKs at once on the
    owner's behalf, as the owner would, back to where the ENQ came from only.  The ENQ is still repeated, so the owner can
    answer too.
    '''
    destination_node, source_node, llap_type = frame[0], frame[1], frame[2]
    now = time.monotonic()
    with self._lock:
      if llap_type == self.LLAP_ENQ:  # an ENQ's source is the ID being asked about, not one in use
        if not self._proxy_ack: return None
        owner = self._nodes.get(destination_node)
        if owner is None or owner[0] == origin or now - owner[1] >= self._peer_timeout: return None
        return bytes((destination_node, destination_node, self.LLAP_ACK))
      if origin != self.ROUTER and source_node not in (0x00, 0xFF): self._nodes[source_node] = (origin, now)
    return None

  def from_peer(self):
    '''Read a datagram from the hub socket and relay it.  Returns the LLAP frame to deliver to the router, or None.'''
    try:
      data, socket_address = self.socket.recvfrom(self.MAX_DATAGRAM_LENGTH)
    except OSError as e:
      logging.debug('%s hub error receiving: %s', self._port_str, e)
      return None
    try:
      address = self._normalize_host(socket_address[0])
    except ValueError:
      return None
    if not self._allowed(address): return None
    is_keepalive = len(data) == self.SENDER_ID_LENGTH
    if not is_keepalive and len(data) < self.SENDER_ID_LENGTH + self.LLAP_HEADER_LENGTH: return None
    if data[0:self.SENDER_ID_LENGTH] == self._sender_id: return None  # our own frame, looped around somehow
    key = (str(address), socket_address[1])
    now = time.monotonic()
    with self._lock:
      if key in self._peers:
        self._peers[key][1] = now
      elif len(self._peers) < self._max_peers:
        self._peers[key] = [socket_address, now]
        logging.info('%s hub peer %s port %d joined', self._port_str, key[0], key[1])
      else:
        if self._last_refusal_log is None or now - self._last_refusal_log >= self.REFUSAL_LOG_INTERVAL:
          logging.warning('%s hub refusing peer %s port %d, already at maximum of %d peers', self._port_str, key[0], key[1],
                          self._max_peers)
          self._last_refusal_log = now
        return None
    if is_keepalive: return None
    frame = data[self.SENDER_ID_LENGTH:]
    if ack := self._note_frame(frame, key): self._sendto(self._sender_id + ack, socket_address)
    self._repeat_to_multicast(data)
    self._send_to_peers(data, exclude=key)
    return frame

  def from_multicast(self, data):
    '''Relay a datagram heard on the multicast group (not our own, not a repeat of ours) to the peers.'''
    if ack := self._note_frame(data[self.SENDER_ID_LENGTH:], self.MULTICAST):
      self._send_multicast(self._sender_id + ack)  # loopback copy is dropped as our own
    self._send_to_peers(data)

  def from_router(self, data):
    '''Relay a datagram sent by the router to the peers.  Returns an ACK frame to deliver to the router, or None.'''
    ack = self._note_frame(data[self.SENDER_ID_LENGTH:], self.ROUTER)
    self._send_to_peers(data)
    return ack

  def housekeeping(self):
    '''Expire silent peers, stale node IDs and remembered repeats.  Call often; it does its work about once a second.'''
    now = time.monotonic()
    if now < self._next_housekeeping: return
    self._next_housekeeping = now + self.HOUSEKEEPING_INTERVAL
    with self._lock:
      expired_peers = deque(key for key, (_, last_heard) in self._peers.items() if now - last_heard >= self._peer_timeout)
      for key in expired_peers: self._peers.pop(key)
      expired_nodes = deque(node for node, (owner, last_heard) in self._nodes.items()
                            if owner in expired_peers or now - last_heard >= self._peer_timeout)
      for node in expired_nodes: self._nodes.pop(node)
      expired_repeats = deque(data for data, (_, expiry) in self._recent_repeats.items() if now >= expiry)
      for data in expired_repeats: self._recent_repeats.pop(data)
    for key in expired_peers: logging.info('%s hub peer %s port %d left (nothing heard for %g seconds)', self._port_str,
                                           key[0], key[1], self._peer_timeout)

  def peers(self):
    '''Return a list of the live peers' (host, port) keys.'''
    with self._lock: return list(self._peers.keys())
