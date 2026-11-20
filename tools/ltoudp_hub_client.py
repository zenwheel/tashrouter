#!/usr/bin/env python3
'''Join a remote TashRouter LToUDP hub's LocalTalk segment from this machine's local LToUDP network.

Unmodified LToUDP emulators (Mini vMac LToUDP builds, Snow, Blue Box and so on) on this machine or its LAN talk to the LToUDP
multicast group as usual.  This relays their frames to the hub by unicast, relays the hub's frames onto the local multicast
group, and sends keepalives so the hub keeps us on its peer list and the path to it stays open.

  python3 tools/ltoudp_hub_client.py <hub address> [--port 1955] [--interface en0]

Don't run this on the same LAN as the hub's own multicast side: the two would form a loop.  Python 3 standard library only.
'''

import argparse
import ipaddress
import logging
import os
import select
import socket
import struct
import sys
import time


LTOUDP_GROUP = '239.192.76.84'
LTOUDP_PORT = 1954
HUB_PORT = 1955
KEEPALIVE_INTERVAL = 20  # seconds
RECENT_REPEAT_TIMEOUT = 2  # seconds
SENDER_ID_LENGTH = 4
MIN_FRAME_DATAGRAM_LENGTH = SENDER_ID_LENGTH + 3


def interface_ipv4_address(name):
  '''Return the IPv4 address of a network interface given by name (or the address itself, if given an address).'''
  try:
    return str(ipaddress.IPv4Address(name))
  except ValueError:
    pass
  from fcntl import ioctl  # not on Windows; give an address there instead
  if sys.platform.startswith('linux'):
    request, ifreq_length = 0x8915, 40  # SIOCGIFADDR
  else:
    request, ifreq_length = 0xC0206921, 32  # SIOCGIFADDR on BSD and macOS
  with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
    ifreq = ioctl(s.fileno(), request, struct.pack('16s', name.encode('ascii')).ljust(ifreq_length, b'\0'))
  return socket.inet_ntoa(ifreq[20:24])


class RecentRepeats:
  '''Datagrams we sent to the multicast group, so their loopback copies (one each) can be recognized and dropped.'''

  def __init__(self):
    self._entries = {}  # datagram -> [count not yet seen back, expiry time]

  def add(self, data):
    entry = self._entries.setdefault(data, [0, 0])
    entry[0] += 1
    entry[1] = time.monotonic() + RECENT_REPEAT_TIMEOUT

  def consume(self, data):
    entry = self._entries.get(data)
    if entry is None: return False
    entry[0] -= 1
    if entry[0] <= 0: self._entries.pop(data)
    return True

  def expire(self):
    now = time.monotonic()
    for data in [data for data, (_, expiry) in self._entries.items() if now >= expiry]: self._entries.pop(data)


def main():
  parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
  parser.add_argument('hub', help="the hub's address or host name, such as the router's tailnet address")
  parser.add_argument('--port', type=int, default=HUB_PORT, help='the hub port (default %d)' % HUB_PORT)
  parser.add_argument('--interface', default='0.0.0.0',
                      help='interface name or IPv4 address for the local multicast group (default: the system default)')
  parser.add_argument('--ltoudp-port', type=int, default=LTOUDP_PORT, help=argparse.SUPPRESS)  # for testing
  parser.add_argument('--multicast-ttl', type=int, default=1, help=argparse.SUPPRESS)  # for testing
  parser.add_argument('-v', '--verbose', action='store_true', help='log every frame')
  args = parser.parse_args()
  logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')

  interface_address = interface_ipv4_address(args.interface)
  family, _, _, _, hub_address = socket.getaddrinfo(args.hub, args.port, type=socket.SOCK_DGRAM)[0]
  hub = socket.socket(family, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
  hub.connect(hub_address)  # we only talk to, and hear from, the hub

  local = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
  local.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
  if hasattr(socket, 'SO_REUSEPORT'): local.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
  local.bind(('0.0.0.0', args.ltoudp_port))
  local.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(LTOUDP_GROUP) + socket.inet_aton(interface_address))
  local.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface_address))
  local.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, args.multicast_ttl)
  local.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)  # so emulators on this machine hear the hub's frames
  group = (LTOUDP_GROUP, args.ltoudp_port)

  sender_id = struct.pack('>L', os.getpid())
  recent = RecentRepeats()
  logging.info('relaying LToUDP between %s port %d and hub %s port %d', interface_address, args.ltoudp_port, hub_address[0],
               hub_address[1])

  def send_to_hub(data):
    try:
      hub.send(data)
    except OSError as e:
      logging.debug('error sending to hub: %s', e)

  send_to_hub(sender_id)  # say hello, so the hub starts sending to us
  last_sent_to_hub = time.monotonic()
  heard_hub = False
  try:
    while True:
      now = time.monotonic()
      if now - last_sent_to_hub >= KEEPALIVE_INTERVAL:
        send_to_hub(sender_id)
        last_sent_to_hub = now
      recent.expire()
      rlist, _, _ = select.select((hub, local), (), (), max(0.0, last_sent_to_hub + KEEPALIVE_INTERVAL - now))
      if local in rlist:
        data = local.recv(65507)
        if len(data) < MIN_FRAME_DATAGRAM_LENGTH or data[0:SENDER_ID_LENGTH] == sender_id: pass
        elif recent.consume(data): pass  # a frame from the hub that we put on the group, come back to us
        else:
          logging.debug('local -> hub: %s', data[SENDER_ID_LENGTH:SENDER_ID_LENGTH + 3].hex())
          send_to_hub(data)
          last_sent_to_hub = time.monotonic()
      if hub in rlist:
        try:
          data = hub.recv(65507)
        except OSError as e:  # e.g. ICMP port unreachable while the hub is down
          logging.debug('error receiving from hub: %s', e)
          continue
        if len(data) < MIN_FRAME_DATAGRAM_LENGTH: continue
        if not heard_hub:
          logging.info('hearing from hub')
          heard_hub = True
        logging.debug('hub -> local: %s', data[SENDER_ID_LENGTH:SENDER_ID_LENGTH + 3].hex())
        recent.add(data)
        local.sendto(data, group)
  except KeyboardInterrupt:
    return 0


if __name__ == '__main__':
  sys.exit(main())
