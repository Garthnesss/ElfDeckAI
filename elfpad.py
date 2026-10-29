#!/usr/bin/env python3
# Copyright (c) 2026 Garth Elfstrom — MIT License (see LICENSE).
"""ElfPad: a virtual gamepad on /dev/uinput, driven by one-line commands
written to a FIFO.

    echo "press a 150"        > /tmp/elfpad.fifo   # tap A for 150 ms
    echo "down x"             > /tmp/elfpad.fifo   # hold X
    echo "up x"               > /tmp/elfpad.fifo   # release X
    echo "press up"           > /tmp/elfpad.fifo   # tap D-pad up (hat axis)
    echo "stick left 60 -20"  > /tmp/elfpad.fifo   # left stick x=60% y=-20%
    echo "stick right 0 0"    > /tmp/elfpad.fifo   # recenter right stick
    echo "trigger right 80"   > /tmp/elfpad.fifo   # RT to 80%
    echo "center"             > /tmp/elfpad.fifo   # sticks/triggers/hat neutral
    echo "reset"              > /tmp/elfpad.fifo   # release everything
    echo "quit"               > /tmp/elfpad.fifo

Many games only accept gamepad input. ElfPad presents as a standard
Xbox 360-class pad (VID 0x045e / PID 0x028e, device name "ElfPad"), so SDL,
Steam Input and RetroArch map it with no configuration: A/B/X/Y, bumpers,
analog triggers, Start/Back/Guide, stick clicks, two analog sticks and a
hat D-pad.

Safety design, shared with ElfKey and ElfMouse:
  * Malformed input never stops the daemon: parse_command() raises
    ValueError on anything invalid; the serve loop logs the refusal and
    keeps serving.
  * Timed holds are bounded to 0..MAX_HOLD_MS; longer holds use down/up,
    which the caller controls.
  * Nothing is left stuck: taps and timed stick/trigger moves restore in a
    finally block, held buttons are tracked, and on quit, SIGTERM, SIGINT, SIGHUP or
    any fatal error every button is released and every axis recentered
    before the device is destroyed.
  * The FIFO is created 0600, so only the owning user can inject input.

Standard library only. Runs on any Linux with /dev/uinput; no root is
needed where uinput is user-accessible (see 70-elfdeckai-uinput.rules).
"""
import fcntl
import os
import signal
import struct
import sys
import time

NAME = "elfpad"
FIFO = "/tmp/elfpad.fifo"
DEFAULT_HOLD_MS = 120
MAX_HOLD_MS = 5000          # a tap is a tap; use down/up for longer holds
MAX_LINE = 200              # longer FIFO lines are refused, never parsed

# Linux input-event codes (include/uapi/linux/input-event-codes.h).
EV_SYN, EV_KEY, EV_ABS, SYN_REPORT = 0, 1, 3, 0
BUTTONS = {
    "a": 0x130, "b": 0x131, "x": 0x133, "y": 0x134,          # SOUTH/EAST/NORTH/WEST
    "lb": 0x136, "rb": 0x137,
    "select": 0x13A, "back": 0x13A, "start": 0x13B,
    "guide": 0x13C, "home": 0x13C,
    "l3": 0x13D, "r3": 0x13E,
}
DPAD = ("up", "down", "left", "right")                       # hat axes, not buttons
# l2/r2 are NOT buttons: a real Xbox 360 pad has no digital trigger buttons
# (no BTN_TL2/BTN_TR2 bits), and declaring them shifted every later button
# two indices off the stock layout for index-based consumers (SDL's 360
# mapping, joydev) — verified against a real 360 pad with evtest on a Deck.
# press/down/up on l2/r2 drive the analog trigger axis instead.
TRIGGERS = {"l2": "left", "r2": "right"}
# axis code -> (min, max)
AXES = {
    0x00: (-32768, 32767),   # ABS_X   left stick X
    0x01: (-32768, 32767),   # ABS_Y   left stick Y
    0x02: (0, 255),          # ABS_Z   left trigger
    0x03: (-32768, 32767),   # ABS_RX  right stick X
    0x04: (-32768, 32767),   # ABS_RY  right stick Y
    0x05: (0, 255),          # ABS_RZ  right trigger
    0x10: (-1, 1),           # ABS_HAT0X
    0x11: (-1, 1),           # ABS_HAT0Y
}
STICK_AXES = {"left": (0x00, 0x01), "right": (0x03, 0x04)}
TRIGGER_AXIS = {"left": 0x02, "right": 0x05}
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_ABSBIT = 1074025828, 1074025829, 1074025831
UI_DEV_CREATE, UI_DEV_DESTROY = 21761, 21762


class _Stopped(Exception):
    """Raised by the signal handler so a blocked read/sleep unwinds at once
    (a handler that merely sets a flag would be swallowed by PEP 475 retries)."""


def _bounded_int(text, lo, hi, what):
    try:
        value = int(text)
    except ValueError:
        raise ValueError("%s must be a whole number: %r" % (what, text))
    if not lo <= value <= hi:
        raise ValueError("%s must be %d..%d: %d" % (what, lo, hi, value))
    return value


def _hold_arg(args, i):
    """Optional hold-ms argument at position i (bounded, like ElfKey)."""
    if len(args) <= i:
        return None
    return _bounded_int(args[i], 0, MAX_HOLD_MS, "hold")


def parse_command(line):
    """One FIFO line -> a command tuple. Raises ValueError on anything
    malformed. This is the whole input contract, kept separate from the
    device code so it can be unit-tested without /dev/uinput.

    Returns one of:
      ("press", name, hold_ms)        name: button or dpad direction
      ("down", name, None) / ("up", name, None)
      ("stick", side, x_pct, y_pct, hold_ms_or_None)
      ("trigger", side, pct, hold_ms_or_None)
      ("center",) / ("reset",) / ("quit",)
    """
    if not isinstance(line, str) or len(line) > MAX_LINE:
        raise ValueError("line too long")
    parts = line.split()
    if not parts:
        raise ValueError("empty line")
    cmd, args = parts[0], parts[1:]
    if cmd in ("quit", "center", "reset"):
        if args:
            raise ValueError("%s takes no arguments" % cmd)
        return (cmd,)
    if cmd == "wait":
        if len(args) != 1:
            raise ValueError("wait takes exactly one duration in ms")
        return ("wait", _bounded_int(args[0], 0, MAX_HOLD_MS, "wait"))
    if cmd in ("press", "down", "up"):
        if not args or (args[0] not in BUTTONS and args[0] not in DPAD
                        and args[0] not in TRIGGERS):
            raise ValueError("unknown button: %s" % (args[0] if args else ""))
        if cmd == "press":
            if len(args) > 2:
                raise ValueError("press takes a button and an optional hold in ms")
            hold = _hold_arg(args, 1)
            return ("press", args[0], DEFAULT_HOLD_MS if hold is None else hold)
        if len(args) != 1:
            raise ValueError("%s takes exactly one button" % cmd)
        return (cmd, args[0], None)
    if cmd == "stick":
        if len(args) not in (3, 4) or args[0] not in STICK_AXES:
            raise ValueError("usage: stick left|right <x -100..100> <y -100..100> [ms]")
        x = _bounded_int(args[1], -100, 100, "stick x")
        y = _bounded_int(args[2], -100, 100, "stick y")
        return ("stick", args[0], x, y, _hold_arg(args, 3))
    if cmd == "trigger":
        if len(args) not in (2, 3) or args[0] not in TRIGGER_AXIS:
            raise ValueError("usage: trigger left|right <0..100> [ms]")
        pct = _bounded_int(args[1], 0, 100, "trigger")
        return ("trigger", args[0], pct, _hold_arg(args, 2))
    raise ValueError("unknown command: %s" % cmd)


class ElfPad:
    """The uinput device plus everything currently held or deflected."""

    def __init__(self, fd):
        self.fd = fd
        self.held = set()        # button names currently down
        self.dpad = set()        # dpad directions currently held
        self.axes = {code: 0 for code in AXES}

    def emit(self, ev_type, code, value):
        # One event, NO frame boundary: callers batch a whole command's
        # events and close the frame with syn(), so a stick move or a hat
        # change is never visible half-applied.
        os.write(self.fd, struct.pack("qqHHi", 0, 0, ev_type, code, value))

    def syn(self):
        os.write(self.fd, struct.pack("qqHHi", 0, 0, EV_SYN, SYN_REPORT, 0))

    def _set_axis(self, code, value):
        if self.axes.get(code) != value:
            self.axes[code] = value
            self.emit(EV_ABS, code, value)

    def _apply_hat(self):
        x = (1 if "right" in self.dpad else 0) - (1 if "left" in self.dpad else 0)
        y = (1 if "down" in self.dpad else 0) - (1 if "up" in self.dpad else 0)
        self._set_axis(0x10, x)
        self._set_axis(0x11, y)

    # -- buttons ---------------------------------------------------------
    def set_button(self, name, down):
        if name in TRIGGERS:
            self.set_trigger(TRIGGERS[name], 100 if down else 0)
            return
        if name in DPAD:
            (self.dpad.add if down else self.dpad.discard)(name)
            self._apply_hat()
            self.syn()
            return
        (self.held.add if down else self.held.discard)(name)  # state first
        self.emit(EV_KEY, BUTTONS[name], 1 if down else 0)
        self.syn()

    def press(self, name, hold_ms):
        self.set_button(name, True)
        try:
            if hold_ms:
                time.sleep(hold_ms / 1000.0)
        finally:
            self.set_button(name, False)

    # -- sticks and triggers ---------------------------------------------
    @staticmethod
    def _scale(pct, lo, hi):
        return max(lo, min(hi, int(round(pct / 100.0 * (hi if pct >= 0 else -lo)))))

    def set_stick(self, side, x_pct, y_pct):
        ax, ay = STICK_AXES[side]
        self._set_axis(ax, self._scale(x_pct, *AXES[ax]))
        self._set_axis(ay, self._scale(y_pct, *AXES[ay]))
        self.syn()

    def stick_for(self, side, x_pct, y_pct, hold_ms):
        self.set_stick(side, x_pct, y_pct)
        try:
            if hold_ms:
                time.sleep(hold_ms / 1000.0)
        finally:
            self.set_stick(side, 0, 0)

    def set_trigger(self, side, pct):
        axis = TRIGGER_AXIS[side]
        self._set_axis(axis, self._scale(pct, *AXES[axis]))
        self.syn()

    def trigger_for(self, side, pct, hold_ms):
        self.set_trigger(side, pct)
        try:
            if hold_ms:
                time.sleep(hold_ms / 1000.0)
        finally:
            self.set_trigger(side, 0)

    # -- global state ------------------------------------------------------
    def center(self):
        """Sticks, triggers and hat back to neutral; buttons untouched."""
        self.dpad.clear()
        for side in STICK_AXES:
            self.set_stick(side, 0, 0)
        for side in TRIGGER_AXIS:
            self.set_trigger(side, 0)
        self._apply_hat()
        self.syn()

    def release_all(self):
        for name in list(self.held):
            try:
                self.emit(EV_KEY, BUTTONS[name], 0)
            except OSError:
                pass
            self.held.discard(name)
        self.center()


_LOCK_FD = None


def _acquire_instance_lock():
    """Single-instance guard: a second daemon would unlink the first one's
    FIFO and orphan it, so refuse to start. flock is per open-file, and the
    fd is held for the process lifetime; a same-process second call (the
    test suite runs main() in threads) is a no-op."""
    global _LOCK_FD
    if _LOCK_FD is not None:
        return
    path = "/tmp/%s.lock" % NAME
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise SystemExit("%s: already running (another instance holds %s)"
                         % (NAME, path))
    _LOCK_FD = fd


def _open_device():
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_ABS)
    for code in sorted(set(BUTTONS.values())):
        fcntl.ioctl(fd, UI_SET_KEYBIT, code)
    absmax, absmin = [0] * 64, [0] * 64
    for code, (lo, hi) in AXES.items():
        fcntl.ioctl(fd, UI_SET_ABSBIT, code)
        absmax[code], absmin[code] = hi, lo
    # struct uinput_user_dev: name[80], id{ bustype, vendor, product, version },
    # ff_effects_max, absmax[64], absmin[64], absfuzz[64], absflat[64]
    dev = struct.pack("80sHHHHI", b"ElfPad", 0x03, 0x045E, 0x028E, 1, 0)
    dev += struct.pack("<64i", *absmax) + struct.pack("<64i", *absmin)
    dev += b"\x00" * (64 * 4 * 2)   # absfuzz, absflat: exact values, no mangling
    os.write(fd, dev)
    fcntl.ioctl(fd, UI_DEV_CREATE)
    return fd


def _make_fifo(path=FIFO):
    """(Re)create the FIFO owner-only: mkfifo's mode is umask-filtered, so
    chmod after creating to enforce exactly 0600."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    os.mkfifo(path, 0o600)
    os.chmod(path, 0o600)
    return path


def main():
    _acquire_instance_lock()
    try:
        fd = _open_device()
    except OSError as e:
        raise SystemExit("elfpad: cannot open /dev/uinput: %s "
                         "(module loaded? permission? see the udev rule)" % e)
    pad = ElfPad(fd)
    print("ElfPad device created", flush=True)

    def _stop(signum, frame):
        raise _Stopped()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    _make_fifo(FIFO)
    try:
        # O_RDWR on our own FIFO: the read end never sees EOF and open never
        # blocks, so the daemon survives writers coming and going. Undecodable
        # bytes become U+FFFD and are refused by the parser, never raised.
        with os.fdopen(os.open(FIFO, os.O_RDWR), "r", encoding="utf-8",
                       errors="replace") as fifo:
            while True:
                line = fifo.readline()
                if not line:
                    time.sleep(0.05)
                    continue
                try:
                    parsed = parse_command(line)
                except ValueError as e:
                    print("elfpad: refused: %s (%s)" % (line.strip()[:80], e), file=sys.stderr, flush=True)
                    continue
                # audit log: every accepted command (see elfkey.py).
                print("elfpad: %s" % line.strip()[:80], file=sys.stderr, flush=True)
                try:
                    cmd = parsed[0]
                    if cmd == "quit":
                        break
                    if cmd == "press":
                        pad.press(parsed[1], parsed[2])
                    elif cmd in ("down", "up"):
                        pad.set_button(parsed[1], cmd == "down")
                    elif cmd == "stick":
                        _, side, x, y, hold = parsed
                        pad.stick_for(side, x, y, hold) if hold is not None else pad.set_stick(side, x, y)
                    elif cmd == "trigger":
                        _, side, pct, hold = parsed
                        pad.trigger_for(side, pct, hold) if hold is not None else pad.set_trigger(side, pct)
                    elif cmd == "wait":
                        time.sleep(parsed[1] / 1000.0)
                    elif cmd == "center":
                        pad.center()
                    elif cmd == "reset":
                        pad.release_all()
                except OSError as e:
                    print("elfpad: emit failed: %s" % e, file=sys.stderr, flush=True)
    except _Stopped:
        pass
    finally:
        pad.release_all()
        try:
            fcntl.ioctl(fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(fd)


if __name__ == "__main__":
    main()
