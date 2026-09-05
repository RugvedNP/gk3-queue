#!/usr/bin/env python3
"""
gk3_probe.py - find out which network doors the UniFormation GK3 has open.

Run this from any machine on the same subnet as the printer, AFTER the USB WiFi
dongle is plugged in and the printer's touchscreen shows a LAN IP under
Settings -> File Sharing.

    python gk3_probe.py                 # broadcast discovery, then scan whatever answers
    python gk3_probe.py <printer-ip>    # skip discovery, probe a known IP

Stdlib only. Nothing to install.

What it tells you:
  445  open -> SMB share path works. Drop-folder queue is viable today.
  3030 open -> SDCP / ChiTu Manager. Remote START + status is viable (cassini).
  3000 replies to M99999 -> legacy ChiTu UDP protocol. Upload + start via M-codes.
"""

import socket
import sys
import time

CHITU_DISCOVERY_PORT = 3000
DISCOVERY_MSG = b"M99999"
LISTEN_SECONDS = 4.0

# port -> (label, what it unlocks)
PORTS = {
    21:   ("FTP",            "file drop over FTP"),
    80:   ("HTTP",           "possible web UI / REST"),
    445:  ("SMB",            "network folder -> drop-folder staging"),
    3000: ("ChiTu UDP/TCP",  "legacy ChiTu control channel"),
    3030: ("SDCP websocket", "ChiTu Manager: upload + remote start + status"),
    8080: ("HTTP alt",       "possible web UI / MJPEG camera"),
    8081: ("HTTP alt",       "possible camera stream"),
}


def local_broadcast_addrs():
    """Guess the /24 broadcast address for each live local IPv4 interface."""
    addrs = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))          # no traffic sent, just picks the default route
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127."):
                addrs.add(ip)
    except OSError:
        pass

    bcasts = []
    for ip in sorted(addrs):
        octets = ip.split(".")
        if len(octets) == 4:
            bcasts.append((ip, ".".join(octets[:3]) + ".255"))
    return bcasts


def discover():
    """Broadcast M99999 and collect every reply. ChiTu and SDCP both answer here."""
    targets = local_broadcast_addrs()
    print("Broadcasting M99999 on UDP/3000 ...")
    for src, bcast in targets:
        print("  via {:<16} -> {}".format(src, bcast))
    if not targets:
        print("  (no local IPv4 interface found; trying 255.255.255.255 only)")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.5)

    for _, bcast in targets or [(None, "255.255.255.255")]:
        for dest in (bcast, "255.255.255.255"):
            try:
                sock.sendto(DISCOVERY_MSG, (dest, CHITU_DISCOVERY_PORT))
            except OSError as exc:
                print("  send to {} failed: {}".format(dest, exc))

    found = {}
    deadline = time.time() + LISTEN_SECONDS
    while time.time() < deadline:
        try:
            data, addr = sock.recvfrom(8192)
        except socket.timeout:
            continue
        except OSError:
            break
        ip = addr[0]
        if ip not in found:
            found[ip] = data
    sock.close()
    return found


def scan(ip):
    """TCP connect test against the ports that matter."""
    print("\nPort scan of {}:".format(ip))
    open_ports = []
    for port in sorted(PORTS):
        label, unlocks = PORTS[port]
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.2)
        try:
            s.connect((ip, port))
            print("  {:>5}  OPEN    {:<16} {}".format(port, label, unlocks))
            open_ports.append(port)
        except (socket.timeout, ConnectionRefusedError, OSError):
            print("  {:>5}  closed  {}".format(port, label))
        finally:
            s.close()
    return open_ports


def verdict(ip, replied_to_discovery, open_ports):
    B = chr(92)
    print("\n" + "=" * 62)
    print("VERDICT for {}".format(ip))
    print("=" * 62)

    if 3030 in open_ports:
        print("BEST PATH: SDCP / ChiTu Manager on :3030")
        print("  -> Full queue: upload, remote START, live status.")
        print("  -> Try cassini:  https://github.com/vvuk/cassini")
        print("     cassini status --printer {}".format(ip))
    elif replied_to_discovery:
        print("BEST PATH: legacy ChiTu UDP on :3000")
        print("  -> Upload via M28/M29 chunks, start via M6030, poll via M4000.")
        print("  -> Protocol notes: Photonsters/anycubic-photon-docs")
    else:
        print("No control channel answered.")
        print("  -> Update firmware to >= 1.2.9, confirm the dongle has an IP,")
        print("     and re-run. Check you are on the SAME subnet (not guest wifi).")

    if 445 in open_ports:
        print("\nSTAGING: SMB is open. You can pre-load jobs today:")
        unc = (B * 2) + ip + B + "<sharename>"
        print("  Windows:  net use Z: " + unc)
        print("  Even with no remote-start, this kills all USB-stick walking.")
    else:
        print("\nSTAGING: SMB closed. Firmware >= 1.2.9 is what adds SMB sharing.")

    if not open_ports and not replied_to_discovery:
        print("\nNOTHING answered. Ping it first:  ping {}".format(ip))


def main():
    if len(sys.argv) > 1:
        ips = sys.argv[1:]
        replied = set()
    else:
        found = discover()
        if not found:
            print("\nNo device answered the broadcast.")
            print("That is common and NOT fatal - many builds only answer on a")
            print("direct IP. Re-run with the IP from the File Sharing menu:")
            print("    python gk3_probe.py 192.168.x.x")
            return 1
        print("\nReplies:")
        for ip, data in found.items():
            preview = data[:220].decode("utf-8", "replace").strip()
            print("  {} -> {}".format(ip, preview))
        ips = list(found)
        replied = set(ips)

    for ip in ips:
        open_ports = scan(ip)
        verdict(ip, ip in replied, open_ports)
    return 0


if __name__ == "__main__":
    sys.exit(main())
