#!/usr/bin/env python3
'''Pretend to be a Basilisk II node using "udptunnel" and see what a TashRouter UdpTunnelPort answers.

This sends an AARP probe and an RTMP request the way an EtherTalk Mac starting up would, prints every frame that comes back,
and exits with status 0 once the router has answered the RTMP request (1 if it doesn't within the timeout).  It uses only the
Python standard library and does not import TashRouter, so it checks the port against an independent implementation of the
wire format.

Against a router on another host on the LAN:

  python3 tools/udptunnel_node.py

On one host, with no traffic on the LAN: run the router with bind_address='127.0.0.1' and broadcast_address set to this
host's LAN address (on Linux, 127.0.0.2 works too), and run this with that LAN address and the router's address:

  python3 tools/udptunnel_node.py --bind 192.168.1.10 --broadcast 127.0.0.1
'''

import argparse
import random
import select
import socket
import struct
import sys
import time


LLC_SNAP_AARP = bytes.fromhex('AAAA03 0000 0080F3'.replace(' ', ''))
LLC_SNAP_APPLETALK = bytes.fromhex('AAAA03 080007 809B'.replace(' ', ''))
AARP_HARDWARE_PROTOCOL = bytes.fromhex('0001 809B 06 04'.replace(' ', ''))
APPLETALK_BROADCAST = bytes.fromhex('090007FFFFFF')
AARP_FUNCTIONS = {1: 'request', 2: 'response', 3: 'probe'}
STARTUP_NETWORK = 0xFF00


def mac_str(hw_addr):
  return ':'.join('%02X' % i for i in hw_addr)


def make_frame(destination, source, payload):
  '''Build an 802.3 frame as Basilisk II sends it: header, LLC/SNAP payload, padded to 60 bytes, no FCS.'''
  frame = destination + source + struct.pack('>H', len(payload)) + payload
  return frame + b'\0' * (60 - len(frame)) if len(frame) < 60 else frame


def make_aarp_probe(hw_addr, network, node):
  return make_frame(APPLETALK_BROADCAST, hw_addr, LLC_SNAP_AARP + AARP_HARDWARE_PROTOCOL +
                    struct.pack('>H6sBHB6sBHB', 3, hw_addr, 0, network, node, b'\0' * 6, 0, network, node))


def make_aarp_response(hw_addr, network, node, destination_hw_addr, destination_network, destination_node):
  return make_frame(destination_hw_addr, hw_addr, LLC_SNAP_AARP + AARP_HARDWARE_PROTOCOL +
                    struct.pack('>H6sBHB6sBHB', 2, hw_addr, 0, network, node,
                                destination_hw_addr, 0, destination_network, destination_node))


def make_rtmp_request(hw_addr, network, node):
  '''Build an RTMP request (DDP type 5, function 1) broadcast to socket 1, as a node does to find its network range.'''
  data = b'\x01'
  ddp = struct.pack('>HHHHBBBBB', 13 + len(data), 0, 0, network, 0xFF, node, 1, 253, 5) + data
  return make_frame(APPLETALK_BROADCAST, hw_addr, LLC_SNAP_APPLETALK + ddp)


def describe(frame):
  '''Return a one-line description of an inbound frame and, if it's an RTMP response, the (network_min, network_max).'''
  if len(frame) < 14: return 'runt (%d bytes)' % len(frame), None
  text = '%s -> %s' % (mac_str(frame[6:12]), mac_str(frame[0:6]))
  length = struct.unpack('>H', frame[12:14])[0]
  payload = frame[14:14 + length]
  if payload[:8] == LLC_SNAP_AARP and len(payload) >= 36:
    func, src_hw, _, src_net, src_node, _, _, dst_net, dst_node = struct.unpack('>H6sBHB6sBHB', payload[14:36])
    return '%s  AARP %s from %d.%d (%s) for %d.%d' % (text, AARP_FUNCTIONS.get(func, func), src_net, src_node,
                                                     mac_str(src_hw), dst_net, dst_node), None
  if payload[:8] == LLC_SNAP_APPLETALK and len(payload) >= 21:
    _, _, dst_net, src_net, dst_node, src_node, dst_socket, src_socket, ddp_type = struct.unpack('>HHHHBBBBB', payload[8:21])
    data = payload[21:]
    text += '  DDP %d.%d:%d -> %d.%d:%d type %d' % (src_net, src_node, src_socket, dst_net, dst_node, dst_socket, ddp_type)
    if ddp_type == 1 and len(data) >= 10 and data[2] == 8 and data[6] & 0x80:
      router_net, _, router_node, net_min, _, net_max = struct.unpack('>HBBHBH', data[0:9])
      text += '  RTMP data from router %d.%d, network range %d-%d' % (router_net, router_node, net_min, net_max)
      if dst_socket == 253: return text, (net_min, net_max)
    return text, None
  return '%s  length/type 0x%04X, %d bytes' % (text, length, len(frame)), None


def main():
  parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
  parser.add_argument('--port', type=int, default=6066, help='UDP port (Basilisk II "udpport", default 6066)')
  parser.add_argument('--bind', default='0.0.0.0', help='address to bind to (default 0.0.0.0)')
  parser.add_argument('--broadcast', default='255.255.255.255', help='where to send broadcasts (default 255.255.255.255)')
  parser.add_argument('--address', help="this node's IPv4 address, for its 'B2' Ethernet address (default: the bind address, "
                                         'or the address that routes to the broadcast address)')
  parser.add_argument('--timeout', type=float, default=10, help='seconds to wait for the RTMP response (default 10)')
  args = parser.parse_args()

  sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
  sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
  sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
  sock.bind((args.bind, args.port))
  address = args.address
  if not address and args.bind != '0.0.0.0': address = args.bind
  if not address:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
      s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
      s.connect((args.broadcast, 9))
      address = s.getsockname()[0]
  hw_addr = b'B2' + socket.inet_aton(address)
  node = random.randint(1, 0xFD)
  print('node %s (%s), address %d.%d, broadcasting to %s:%d' % (mac_str(hw_addr), address, STARTUP_NETWORK, node,
                                                                 args.broadcast, args.port))

  def send(frame, destination_hw_addr=APPLETALK_BROADCAST):
    if destination_hw_addr[0] & 0x01:
      ip = args.broadcast
    elif destination_hw_addr.startswith(b'B2'):
      ip = socket.inet_ntoa(destination_hw_addr[2:6])
    else:
      return
    sock.sendto(frame, (ip, args.port))

  send(make_aarp_probe(hw_addr, STARTUP_NETWORK, node))
  print('sent AARP probe for %d.%d' % (STARTUP_NETWORK, node))
  deadline = time.monotonic() + args.timeout
  next_request = time.monotonic()
  while (now := time.monotonic()) < deadline:
    if now >= next_request:
      # the router won't answer until it has claimed its own address, so ask again every second
      send(make_rtmp_request(hw_addr, STARTUP_NETWORK, node))
      print('sent RTMP request')
      next_request = now + 1
    rlist, _, _ = select.select((sock,), (), (), min(deadline, next_request) - now)
    if not rlist: continue
    frame, sender = sock.recvfrom(65507)
    if frame[6:12] == hw_addr: continue  # our own broadcast, as Basilisk II drops it
    text, network_range = describe(frame)
    print('from %s:%d  %s' % (sender[0], sender[1], text))
    if frame[14:22] == LLC_SNAP_AARP and frame[22 + 6:22 + 8] == b'\x00\x01':  # AARP request: answer if it's for us
      _, src_hw, _, src_net, src_node, _, _, dst_net, dst_node = struct.unpack('>H6sBHB6sBHB', frame[28:50])
      if (dst_net, dst_node) == (STARTUP_NETWORK, node):
        send(make_aarp_response(hw_addr, STARTUP_NETWORK, node, src_hw, src_net, src_node), src_hw)
    if network_range:
      print('router answered: network range %d-%d' % network_range)
      return 0
  print('no RTMP response within %g seconds' % args.timeout)
  return 1


if __name__ == '__main__':
  sys.exit(main())
