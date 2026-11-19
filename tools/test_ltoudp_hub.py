#!/usr/bin/env python3
'''Test the LToUDP hub on this machine, without touching the LAN.

Runs a TashRouter with a hub-mode LToUDP port whose multicast side is confined to the loopback interface (IP_MULTICAST_IF
127.0.0.1, TTL 0) on non-standard ports, and checks it with fake hub clients and fake multicast nodes:

1. Hub plus multicast: two hub clients and one multicast node each see the others' frames exactly once, the router gets every
   frame once, the router's frames reach everyone once, keepalives go nowhere, the hub ACKs ENQs for node IDs in use elsewhere
   (and for the router's own ENQs), and silent peers expire.
2. Hub only, through tools/ltoudp_hub_client.py: a multicast node behind the client and a direct hub client see each other.
3. IPv6 and allowlists: a dual-stack hub on '::' relays between IPv6 and IPv4 peers; a hub with the default (Tailscale)
   allowlist ignores loopback peers.

Run from the repository root: python3 tools/test_ltoudp_hub.py
'''

import logging
import os
import random
import select
import socket
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from tashrouter.port.appletalk.localtalk.ltoudp import LtoudpPort  # noqa: E402
from tashrouter.router.router import Router  # noqa: E402


GROUP = LtoudpPort.LTOUDP_GROUP
LOOPBACK = '127.0.0.1'
WINDOW = 0.4  # seconds to collect frames after each step
ENQ, ACK = 0x81, 0x82

failures = 0


def check(condition, description):
  global failures
  print('%s  %s' % ('ok  ' if condition else 'FAIL', description))
  if not condition: failures += 1


def data_frame(source_node, marker, destination_node=0xFF):
  '''An LLAP short-header data frame to socket 200 (nobody listens there), carrying a marker to find it by.'''
  payload = marker.encode('ascii')
  ddp = struct.pack('>HBBB', 5 + len(payload), 200, 200, 99) + payload
  return bytes((destination_node, source_node, 0x01)) + ddp


def multicast_socket(port):
  s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
  s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
  if hasattr(socket, 'SO_REUSEPORT'): s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
  s.bind(('0.0.0.0', port))
  s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(GROUP) + socket.inet_aton(LOOPBACK))
  s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(LOOPBACK))
  s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)
  return s


class FakeNode:
  '''A fake LToUDP node, either on the multicast group or talking to a hub by unicast.'''

  def __init__(self, name, sender_id, sock, destination):
    self.name = name
    self.sender_id = sender_id
    self.sock = sock
    self.destination = destination
    self.received = []

  @classmethod
  def on_multicast(cls, name, sender_id, port):
    return cls(name, sender_id, multicast_socket(port), (GROUP, port))

  @classmethod
  def hub_client(cls, name, sender_id, hub_address, hub_port, family=socket.AF_INET, bind_address=LOOPBACK):
    s = socket.socket(family, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.bind((bind_address, 0))
    return cls(name, sender_id, s, (hub_address, hub_port))

  def send(self, frame):
    self.sock.sendto(self.sender_id + frame, self.destination)

  def keepalive(self):
    self.sock.sendto(self.sender_id, self.destination)

  def collect(self):
    while select.select((self.sock,), (), (), 0)[0]:
      data = self.sock.recv(65507)
      if data[0:4] != self.sender_id: self.received.append(data)  # a multicast node hears its own frames; drop them

  def take(self):
    self.collect()
    received, self.received = self.received, []
    return received

  def count(self, marker):
    return sum(1 for i in self.received if marker.encode('ascii') in i)


class LoopbackLtoudpPort(LtoudpPort):
  '''LtoudpPort with its multicast side on the loopback interface, a test port, and a record of the frames it receives.'''

  def __init__(self, multicast_port, **kwargs):
    self.LTOUDP_PORT = multicast_port
    self.frames_in = []
    super().__init__(**kwargs)

  def _open_multicast_socket(self):
    return multicast_socket(self.LTOUDP_PORT)

  def inbound_frame(self, frame_data):
    self.frames_in.append(frame_data)
    super().inbound_frame(frame_data)

  def count(self, marker):
    return sum(1 for i in self.frames_in if marker.encode('ascii') in i)


def settle(nodes):
  '''Wait for frames in flight, then collect them on every node.'''
  time.sleep(WINDOW)
  for node in nodes: node.collect()


def start_router(port):
  router = Router('test', ports=(port,))
  router.start()
  deadline = time.monotonic() + 5
  while not port.node and time.monotonic() < deadline: time.sleep(0.1)
  return router


def test_hub_and_multicast():
  print('\n1. hub plus multicast')
  multicast_port, hub_port = random.randint(20000, 29999), random.randint(30000, 39999)
  port = LoopbackLtoudpPort(multicast_port, seed_network=1, seed_zone_name=b'Hub', hub=True, hub_address=LOOPBACK,
                            hub_port=hub_port, hub_allowed_networks=['127.0.0.0/8'], hub_peer_timeout=3)
  router = start_router(port)
  try:
    check(port.node != 0, 'router claimed node %d' % port.node)
    a = FakeNode.hub_client('A', b'AAAA', LOOPBACK, hub_port)
    b = FakeNode.hub_client('B', b'BBBB', LOOPBACK, hub_port)
    m = FakeNode.on_multicast('M', b'MMMM', multicast_port)
    nodes = (a, b, m)

    a.keepalive()
    b.keepalive()
    settle(nodes)
    check(len(port._hub.peers()) == 2, 'keepalives make A and B peers')
    check(not [i for n in nodes for i in n.take() if i[0:4] in (a.sender_id, b.sender_id)] and not port.frames_in,
          'keepalives are not forwarded or delivered')

    for sender, source_node, others in ((a, 20, (b, m)), (b, 30, (a, m)), (m, 10, (a, b))):
      marker = 'from-%s' % sender.name
      for n in nodes: n.take()
      sender.send(data_frame(source_node, marker))
      settle(nodes)
      check(all(o.count(marker) == 1 for o in others),
            '%s frame seen once each by %s' % (sender.name, ' and '.join(o.name for o in others)))
      check(sender.count(marker) == 0, '%s frame not sent back to %s' % (sender.name, sender.name))
      check(port.count(marker) == 1, '%s frame delivered to the router once' % sender.name)

    for n in nodes: n.take()
    port.send_frame(data_frame(port.node, 'from-router'))
    settle(nodes)
    check(all(n.count('from-router') == 1 for n in nodes), 'router frame seen once each by A, B and M')

    # proxy ACK: M owns node 10, B owns 30, A owns 20
    for n in nodes: n.take()
    a.send(bytes((10, 10, ENQ)))
    settle(nodes)
    acks = [i for i in a.received if i[4:7] == bytes((10, 10, ACK))]
    check(len(acks) == 1, 'hub ACKs A\'s ENQ for node 10 (in use on the multicast side), to A')
    check(not [i for n in (b, m) for i in n.received if i[6] == ACK], 'that ACK goes only to A')
    check(sum(1 for i in m.received if i[4:7] == bytes((10, 10, ENQ))) == 1 and
          sum(1 for i in b.received if i[4:7] == bytes((10, 10, ENQ))) == 1, 'the ENQ is still repeated to M and B')
    for n in nodes: n.take()
    m.send(bytes((30, 30, ENQ)))
    settle(nodes)
    check(sum(1 for i in m.received if i[4:7] == bytes((30, 30, ACK))) == 1, 'hub ACKs M\'s ENQ for node 30 (B\'s), to M')
    check(not [i for i in a.received + b.received if i[6] == ACK], 'that ACK goes only to the multicast side')
    for n in nodes: n.take()
    a.send(bytes((20, 20, ENQ)))
    a.send(bytes((99, 99, ENQ)))
    settle(nodes)
    check(not [i for i in a.received if i[6] == ACK], 'no ACK for A\'s own node 20, or for unused node 99')
    port.frames_in.clear()
    port.send_frame(bytes((30, 30, ENQ)))
    time.sleep(WINDOW)
    check(bytes((30, 30, ACK)) in port.frames_in, 'hub ACKs the router\'s own ENQ for node 30 (B\'s)')

    # expiry: B goes quiet, A keeps sending keepalives
    deadline = time.monotonic() + 4.5
    while time.monotonic() < deadline:
      a.keepalive()
      time.sleep(0.5)
    check([k[1] for k in port._hub.peers()] == [a.sock.getsockname()[1]], 'B expired after 3 s of silence, A kept alive')
    for n in nodes: n.take()
    m.send(data_frame(10, 'after-expiry'))
    settle(nodes)
    check(a.count('after-expiry') == 1 and b.count('after-expiry') == 0, 'frames reach A but no longer B')
  finally:
    router.stop()


def test_hub_only_with_client_tool():
  print('\n2. hub only, through tools/ltoudp_hub_client.py')
  multicast_port, hub_port = random.randint(20000, 29999), random.randint(30000, 39999)
  port = LoopbackLtoudpPort(multicast_port, seed_network=1, seed_zone_name=b'Hub', multicast=False, hub=True,
                            hub_address=LOOPBACK, hub_port=hub_port, hub_allowed_networks='any')
  router = start_router(port)
  client = subprocess.Popen((sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ltoudp_hub_client.py'),
                             LOOPBACK, '--port', str(hub_port), '--interface', LOOPBACK, '--ltoudp-port', str(multicast_port),
                             '--multicast-ttl', '0'), stderr=subprocess.DEVNULL)
  try:
    e = FakeNode.on_multicast('E', b'EEEE', multicast_port)  # an emulator next to the client
    c = FakeNode.hub_client('C', b'CCCC', LOOPBACK, hub_port)
    c.keepalive()
    deadline = time.monotonic() + 5
    while len(port._hub.peers()) < 2 and time.monotonic() < deadline: time.sleep(0.1)
    check(len(port._hub.peers()) == 2, 'client tool and C are peers')
    for sender, other in ((e, c), (c, e)):
      marker = 'tool-from-%s' % sender.name
      for n in (e, c): n.take()
      sender.send(data_frame(40 if sender is e else 41, marker))
      settle((e, c))
      check(other.count(marker) == 1 and sender.count(marker) == 0 and port.count(marker) == 1,
            '%s frame seen once by %s and the router, not echoed' % (sender.name, other.name))
    for n in (e, c): n.take()
    port.send_frame(data_frame(port.node, 'tool-from-router'))
    settle((e, c))
    check(e.count('tool-from-router') == 1 and c.count('tool-from-router') == 1, 'router frame seen once by E and C')
  finally:
    client.terminate()
    client.wait()
    router.stop()


def test_ipv6_and_allowlist():
  print('\n3. IPv6 and allowlists')
  hub_port = random.randint(40000, 49999)
  port = LoopbackLtoudpPort(0, seed_network=1, seed_zone_name=b'Hub', multicast=False, hub=True, hub_address='::',
                            hub_port=hub_port, hub_allowed_networks='any')
  router = start_router(port)
  try:
    v6 = FakeNode.hub_client('V6', b'6666', '::1', hub_port, family=socket.AF_INET6, bind_address='::1')
    v4 = FakeNode.hub_client('V4', b'4444', LOOPBACK, hub_port)
    v6.keepalive()
    v4.keepalive()
    time.sleep(WINDOW)
    v6.send(data_frame(50, 'v6'))
    v4.send(data_frame(51, 'v4'))
    settle((v6, v4))
    check(v4.count('v6') == 1 and v6.count('v4') == 1, 'dual-stack hub relays between IPv6 and IPv4 peers')
  finally:
    router.stop()
  hub_port = random.randint(40000, 49999)
  port = LoopbackLtoudpPort(0, seed_network=1, seed_zone_name=b'Hub', multicast=False, hub=True, hub_address=LOOPBACK,
                            hub_port=hub_port)
  router = start_router(port)
  try:
    x = FakeNode.hub_client('X', b'XXXX', LOOPBACK, hub_port)
    x.send(data_frame(60, 'refused'))
    time.sleep(WINDOW)
    check(not port._hub.peers() and port.count('refused') == 0, 'default allowlist (Tailscale ranges) ignores 127.0.0.1')
  finally:
    router.stop()


if __name__ == '__main__':
  logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s: %(message)s')
  test_hub_and_multicast()
  test_hub_only_with_client_tool()
  test_ipv6_and_allowlist()
  print('\n%s' % ('all checks passed' if not failures else '%d check(s) failed' % failures))
  sys.exit(1 if failures else 0)
