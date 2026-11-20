# TashRouter

## Introduction

### What is it?

TashRouter is a fully standards-compliant AppleTalk router that supports LocalTalk (via [LToUDP](https://web.archive.org/web/20260102054321/https://windswept.home.blog/2019/12/10/localtalk-over-udp/) and/or [TashTalk](https://github.com/lampmerchant/tashtalk)) in addition to EtherTalk.

### What can it do?

TashRouter can connect multiple AppleTalk networks of different types and define zones in them.  It can fully replace the functionality of EtherTalk bridge devices (such as AsantéTalk) without resorting to the kinds of standards-breaking hacks that they use.  For example, you could connect an instance of [Mini vMac](https://www.gryphel.com/c/minivmac/index.html) [v37](https://68kmla.org/bb/index.php?threads/emulation-binaries-for-mini-vmac-37-with-ltoudp.46443/) (which can emulate LocalTalk over LToUDP) and a Macintosh 512k (which can connect to a LocalTalk network that TashRouter can access using [TashTalk](https://github.com/lampmerchant/tashtalk)) to a [Netatalk](https://github.com/Netatalk/netatalk) v2.x server to share files and printers.

### What do I need to run it?

A single-board computer such as a Raspberry Pi makes an ideal host for TashRouter.  A [TashTalk Hat](https://ko-fi.com/s/60b561a0e3) will allow a device with a Raspberry Pi-compatible GPIO header to connect to a LocalTalk network.

However, a single-board computer is not required - any computer that can run [Python](https://www.python.org/) v3.x can run TashRouter.  For example, a server running [Void Linux](https://voidlinux.org/) can route between an EtherTalk network and a LocalTalk network running LToUDP, while an [AirTalk](https://airtalk.shop/product/airtalk-complete/) wirelessly bridges the LToUDP network to a physical LocalTalk network.  TashRouter will also run on Windows, but since it currently has no EtherTalk port drivers that support Windows, it is limited to routing between LocalTalk networks via LToUDP and TashTalk.

### Where can I get support?

There is a thread on the [68kMLA forum](https://68kmla.org/bb/index.php?threads/tashrouter-an-appletalk-router.46047/) frequented by the author and other knowledgeable vintage Mac enthusiasts.

## Status

Fully usable, code-complete, and ready for some real-world experience.  Codebase is not yet mature, however - undetected bugs may exist.

## Quick Start - macvtap

### Creating a macvtap device

A macvtap device is necessary to support EtherTalk.  Not all kernels may have support for this built in; [Void Linux](https://voidlinux.org/) for Raspberry Pi is known to have support for macvtap out of the box.  Use the following shell commands (as root) to set up a macvtap device for use with TashRouter:

```
# ip link add link eth0 name macvtap0 type macvtap
# ip link set dev macvtap0 promisc on
# ip link set dev macvtap0 up
```

This process can be automated, though the method of doing so depends on your operating system.  In Void Linux, for example, the above commands can be added to `rc.local`.

### Running TashRouter

Download and unzip the Python v3.x source code for TashRouter.

Put the following into a file called `test_router.py` at the same level as the `tashrouter` directory, optionally customizing parameters such as the serial port for TashTalk, network numbers, and zone names:

```python
import logging
import time
import signal
import sys

from tashrouter.netlog import set_log_str_func
from tashrouter.port.appletalk.ethertalk.macvtap import MacvtapPort
from tashrouter.port.appletalk.localtalk.ltoudp import LtoudpPort
from tashrouter.port.appletalk.localtalk.tashtalk import TashTalkPort
from tashrouter.router.router import Router


logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s: %(message)s')
set_log_str_func(logging.debug)  # comment this line for speed and reduced spam

router = Router('router', ports=(
  LtoudpPort(seed_network=1, seed_zone_name=b'LToUDP Network'),
  TashTalkPort(serial_port='/dev/ttyAMA0', seed_network=2, seed_zone_name=b'TashTalk Network'),
  MacvtapPort(macvtap_name='macvtap0', seed_network_min=3, seed_network_max=5, seed_zone_names=[b'EtherTalk Network']),
))

print('router away!')
router.start()
signal.signal(signal.SIGTERM, lambda _signo, _stack_frame: sys.exit(0))  # raises SystemExit on SIGTERM

try:
  while True: time.sleep(1)
except (KeyboardInterrupt, SystemExit):
  router.stop()
```

Run the script you just created: `python3 test_router.py`

## Joining LToUDP from afar: the LToUDP hub

LToUDP is multicast, and so it stays on one LAN.  Networks such as a [Tailscale](https://tailscale.com/) tailnet (or the internet) carry no multicast, so a remote machine can't hear or reach the group.  In hub mode, an `LtoudpPort` also listens on a unicast address and port, and relays LToUDP to and from remote machines there.  The local multicast group, the router and every remote machine form one LocalTalk segment, with one network number and one zone.  This matters for classic AppleTalk games, many of which look for players only in their own zone.

### How it works

The hub is a repeater:

* A frame from a remote machine (a hub peer) goes to the router, to the multicast group and to every other peer, unchanged, with its original sender ID.
* A frame from the multicast group goes to the router (as always) and to every peer.
* A frame the router sends goes to the multicast group (as always) and to every peer.
* No frame is ever sent back to where it came from, and the hub recognizes its own repeats coming back to it through multicast loopback, so nothing loops or arrives twice.
* Every LLAP frame type (data, ENQ, ACK, RTS, CTS) is forwarded as is.

Peers are learned from the first datagram they send, and forgotten after 60 seconds with nothing heard.  So the remote side always speaks first and keeps the path open, which suits Tailscale node sharing, where the remote machine can open a flow to the shared router but not the other way around.

LocalTalk node ID acquisition assumes a fast shared wire: a node sends a few ENQs for the ID it wants and takes it if nobody ACKs within a fraction of a millisecond.  Across a hub the answer can take tens of milliseconds, so the hub keeps a table of the node IDs it has heard in use on each side.  When an ENQ arrives for an ID in use elsewhere, the hub ACKs at once on the owner's behalf, back to where the ENQ came from (`hub_proxy_ack`, on by default).

### The hub client protocol

It is LToUDP itself, by unicast, so other projects (emulators, Blue Box) can speak it natively:

* Send `[4-byte sender ID][LLAP frame]` to the hub's address and port (1955 by default); receive the same from the hub.
* While idle, send a datagram of only your 4-byte sender ID about every 20 seconds.  This keepalive keeps you on the hub's peer list and is not repeated anywhere.

For emulators that only speak multicast LToUDP, `tools/ltoudp_hub_client.py` (Python 3, standard library only) runs next to them: it joins the local multicast group, relays local frames to the hub and the hub's frames onto the local group, and sends the keepalives.

### Example: sharing a Raspberry Pi with a friend's tailnet

Here the Pi runs TashRouter in your tailnet, with the machine name `pi`, the tailnet address `100.101.102.103` and the tailnet DNS name `example-tailnet.ts.net`.  Your friend has their own tailnet, and a computer that runs an LToUDP emulator (such as a Mini vMac LToUDP build).

On your side:

1. Give the Pi's LToUDP port hub mode, alongside any other ports, and start TashRouter:

   ```python
   router = Router('router', ports=(
     LtoudpPort(seed_network=1, seed_zone_name=b'LToUDP Network', hub=True, hub_address='100.101.102.103'),
     MacvtapPort(macvtap_name='macvtap0', seed_network_min=3, seed_network_max=5, seed_zone_names=[b'EtherTalk Network']),
   ))
   ```

   Binding the tailnet address means Tailscale must be up before TashRouter starts.  `hub_address='0.0.0.0'` avoids that, and the default allowlist still admits only tailnet addresses.

2. [Share the Pi](https://tailscale.com/docs/features/sharing) with your friend: open the [Machines](https://console.tailscale.com/admin/machines) page of the admin console, find the Pi, and send your friend an invite by email or by link.  This needs the Owner, Admin or IT admin role in your tailnet.

3. Check your tailnet policy.  The default policy allows everything, so there is nothing to do.  If you have a custom policy, it must let shared users reach UDP port 1955 on the Pi, for example with this grant:

   ```json
   "grants": [
     {
       "src": ["autogroup:shared"],
       "dst": ["100.101.102.103"],
       "ip": ["udp:1955"]
     }
   ]
   ```

   `autogroup:shared` means every user who has accepted a share from your tailnet; use your friend's login email instead to admit only them.

On your friend's side:

1. Install Tailscale, sign in to their own tailnet, and accept the invite.  The Pi then appears among their machines (`tailscale status` lists it).  If their tailnet has a custom policy file, it must also allow connections to the shared Pi.

2. Start the LToUDP emulator as usual, and next to it, on the same computer or the same LAN, run the client:

   ```
   python3 tools/ltoudp_hub_client.py 100.101.102.103
   ```

   With MagicDNS on in their tailnet, the Pi's full name works too: `pi.example-tailnet.ts.net`.  A shared machine is reachable only by its full name, not by `pi` alone.  Use `--interface` (such as `--interface en0`) to choose which local interface's multicast group to bridge.

Their emulated Mac joins the `LToUDP Network` zone along with the Macs on your LAN.  Tailscale [quarantines](https://tailscale.com/docs/features/sharing#quarantine) shared machines: the Pi can answer your friend's machine but not start a connection to it.  That's why the client speaks first and sends keepalives, and why nothing needs sharing in the other direction.

### Hub options

These go to `LtoudpPort` along with `hub=True`:

* `multicast`: `False` turns the multicast side off, making the port hub only (default `True`).
* `hub_address`: the address to listen on (default `'0.0.0.0'`; `'::'` takes IPv4 and IPv6).
* `hub_port`: the UDP port to listen on (default 1955).
* `hub_allowed_networks`: the networks peers may come from, as a list such as `['192.168.0.0/16']`, or `'any'` (default: Tailscale's `100.64.0.0/10` and `fd7a:115c:a1e0::/48`).
* `hub_max_peers`: the most peers at once (default 16); more are ignored.
* `hub_peer_timeout`: seconds with nothing heard before a peer is forgotten (default 60).
* `hub_proxy_ack`: whether to ACK ENQs for node IDs in use elsewhere on the segment (default `True`).

### Limits

* The hub has no encryption or authentication of its own; it relies on the tailnet for both.  Don't expose it on a public address or port without an allowlist that names only the peers you trust: anyone who can reach it joins your LocalTalk network.
* Every frame crosses the tailnet, so its latency is added to everything.  File sharing and turn-based games tolerate this better than fast action games, which may suffer.
* Every frame goes to every peer, so traffic grows with the number of peers; `hub_max_peers` caps it.
* Don't run `ltoudp_hub_client.py` on the same LAN as the hub's multicast side: the two would form a loop.

`tools/test_ltoudp_hub.py` checks the hub on one machine, using the loopback interface only.

## Using TashRouter with a tap and Netatalk 2.x

See [this post](https://68kmla.org/bb/index.php?threads/tashrouter-an-appletalk-router.46047/post-518796) on the 68kMLA forum.  Note that `TapPort` has been changed to `LinuxTapPort` and `BsdTapPort` for Linux and BSD, respectively.
