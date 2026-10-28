#!/usr/bin/env python3
"""Gamepad probe for ElfDeckAI: which controller does a game actually see
when ElfPad presses a button?

In Steam Deck Gaming Mode, Steam Input takes over physical controllers and
hands games its own virtual Xbox 360 pads instead (USB id 28de:11ff), one
per controller, in the order the controllers appeared. This script lists
the joystick devices, shows which processes hold ElfPad open, then presses
A and D-pad Down through ElfPad and reports every device that reacted, so
you can see which of Steam's virtual pads (Player 1, 2, ...) carries
ElfPad's input.

Run it on the Deck with the ElfPad service running, ideally with a game
open in Gaming Mode:

    ssh deck@DECK-IP 'python3 - --deck' < scripts/probe-gamepad.py

With --deck (as above), it also waits 5 seconds for someone to press A on the Deck's
own controls, to show which virtual pad belongs to them. It changes
nothing; it only reads input devices and writes two taps to ElfPad's FIFO.
"""
import errno
import fcntl
import os
import select
import struct
import sys
import time

FIFO = "/tmp/elfpad.fifo"
EV_KEY, EV_ABS = 1, 3
EVENT = struct.Struct("qqHHi")
EVIOCGID = (2 << 30) | (8 << 16) | (ord("E") << 8) | 0x02
STEAM_VIRTUAL = (0x28DE, 0x11FF)
STEAM_DECK = (0x28DE, 0x1205)


def eviocgname(length=256):
    return (2 << 30) | (length << 16) | (ord("E") << 8) | 0x06


def joysticks(text=None):
    """(event node, name, (vendor, product)) for every joystick-like device,
    from /proc/bus/input/devices."""
    found = []
    if text is None:
        try:
            with open("/proc/bus/input/devices") as f:
                text = f.read()
        except OSError:
            return found
    blocks = text.split("\n\n")
    for block in blocks:
        handlers = name = None
        vid = pid = 0
        for line in block.splitlines():
            if line.startswith("I:"):
                fields = dict(f.split("=") for f in line[3:].split())
                vid, pid = int(fields["Vendor"], 16), int(fields["Product"], 16)
            elif line.startswith("N: Name="):
                name = line[len('N: Name="'):-1]
            elif line.startswith("H: Handlers="):
                handlers = line.split("=", 1)[1].split()
        if not handlers or not any(h.startswith("js") for h in handlers):
            continue
        for h in handlers:
            if h.startswith("event"):
                found.append((h, name, (vid, pid)))
    return sorted(found, key=lambda d: int(d[0][5:]))


def holders(node):
    """Names of processes (that this user can see) holding /dev/input/<node>."""
    target = "/dev/input/" + node
    names = set()
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            for fd in os.listdir("/proc/%s/fd" % pid):
                if os.readlink("/proc/%s/fd/%s" % (pid, fd)) == target:
                    with open("/proc/%s/comm" % pid) as f:
                        names.add(f.read().strip())
                    break
        except OSError:
            continue
    return sorted(names)


def label(device, steam_index):
    node, name, ids = device
    tag = ""
    if ids == STEAM_VIRTUAL:
        tag = "  <- Steam virtual pad #%d (Player %d by default order)" % (
            steam_index[node], steam_index[node])
    elif ids == STEAM_DECK:
        tag = "  <- the Deck's built-in controls"
    elif name == "ElfPad":
        tag = "  <- ElfPad itself"
    return "%-8s %04x:%04x  %s%s" % (node, ids[0], ids[1], name, tag)


def watch(devices, seconds, action=None):
    """Open every device, run `action`, and return the nodes that produced a
    button or axis event within `seconds`."""
    fds = {}
    for node, _name, _ids in devices:
        try:
            fds[os.open("/dev/input/" + node, os.O_RDONLY | os.O_NONBLOCK)] = node
        except OSError as e:
            print("  (cannot read %s: %s)" % (node, os.strerror(e.errno)))
    reacted = set()
    try:
        if action:
            action()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select(list(fds), [], [], 0.1)
            for fd in ready:
                try:
                    data = os.read(fd, EVENT.size * 64)
                except OSError:
                    continue
                for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
                    _s, _u, typ, _code, _value = EVENT.unpack_from(data, i)
                    if typ in (EV_KEY, EV_ABS):
                        reacted.add(fds[fd])
    finally:
        for fd in fds:
            os.close(fd)
    return reacted


def send(*lines):
    fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
    try:
        for line in lines:
            os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)


def main():
    devices = joysticks()
    steam_index, n = {}, 0
    for node, _name, ids in devices:
        if ids == STEAM_VIRTUAL:
            n += 1
            steam_index[node] = n

    print("== Joystick devices (in creation order)")
    for d in devices:
        print("  " + label(d, steam_index))
    if not devices:
        print("  (none)")

    elfpad = [d for d in devices if d[1] == "ElfPad"]
    if elfpad:
        print("\n== Processes holding ElfPad open")
        print("  " + (", ".join(holders(elfpad[0][0])) or "(none visible)"))

    print("\n== ElfPad presses A, then D-pad Down; devices that reacted:")
    try:
        reacted = watch(devices, 1.5, lambda: send("press a 150", "press down 150"))
    except OSError as e:
        if e.errno in (errno.ENXIO, errno.ENOENT):
            print("  ElfPad is not running (no reader on %s)" % FIFO)
            return 1
        raise
    for d in devices:
        if d[0] in reacted:
            print("  " + label(d, steam_index))
    if not reacted:
        print("  (none)")
    elif not reacted - {d[0] for d in elfpad}:
        print("  Only ElfPad itself reacted: no Steam virtual pad carries it.")

    if "--deck" in sys.argv[1:]:
        print("\n== Press A on the Deck's own controls now (5 seconds)...")
        sys.stdout.flush()
        reacted = watch(devices, 5)
        for d in devices:
            if d[0] in reacted:
                print("  " + label(d, steam_index))
        if not reacted:
            print("  (none)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
