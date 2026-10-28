#!/usr/bin/env python3
# Copyright (c) 2026 Garth Elfstrom — MIT License (see LICENSE).
"""ElfMouse: a virtual mouse on /dev/uinput, driven by one-line commands
written to a FIFO.

    echo "move 120 -40"     > /tmp/elfmouse.fifo   # move right 120, up 40
    echo "moveto 640 400 1280 800" > /tmp/elfmouse.fifo   # pixel (640, 400) of a 1280x800 frame
    echo "move 400 0 600"   > /tmp/elfmouse.fifo   # glide 400 px over 600 ms
    echo "click left"       > /tmp/elfmouse.fifo   # click
    echo "down left"        > /tmp/elfmouse.fifo   # hold (drag)...
    echo "move 200 120"     > /tmp/elfmouse.fifo   # ...drag...
    echo "up left"          > /tmp/elfmouse.fifo   # ...drop
    echo "scroll -3"        > /tmp/elfmouse.fifo   # wheel down 3 ticks
    echo "quit"             > /tmp/elfmouse.fifo

`move` is relative, like a physical mouse. `moveto` places the pointer at an
absolute screen position through a second device, "ElfMouse Absolute",
which compositors treat like a virtual machine's tablet pointer: either in
pixels of a given frame size (typically the screenshot the caller is looking
at), or in device units 0..ABS_MAX across the whole screen. Buttons are
pressed on whichever device moved last, so a move followed by a click
arrives in order.

Safety design, shared with ElfKey and ElfPad:
  * Malformed input never stops the daemon: parse_command() raises
    ValueError on anything invalid; the serve loop logs the refusal and
    keeps serving.
  * Moves, glides and scrolls are bounded per command; longer travel is
    done by chaining commands. Timed actions cap at MAX_HOLD_MS.
  * Buttons are never left held: clicks release in a finally block, held
    buttons are tracked, and on quit, SIGTERM, SIGINT or any fatal error
    every held button is released before the device is destroyed.
  * The FIFO is created 0600, so only the owning user can inject input.

Standard library only. Works on any Linux with /dev/uinput.
"""
import fcntl
import os
import signal
import struct
import sys
import time

NAME = "elfmouse"
FIFO = "/tmp/elfmouse.fifo"
DEFAULT_CLICK_MS = 100
MAX_HOLD_MS = 5000          # glides and clicks cap here, like every tap
MAX_LINE = 200              # longer FIFO lines are refused, never parsed
MAX_MOVE = 10000            # px per command — plenty; chain for more
MAX_SCROLL = 120            # wheel ticks per command
ABS_MAX = 32767             # absolute device range per axis (0..ABS_MAX)
MAX_FRAME = 20000           # largest frame size accepted by moveto, in px

# Linux input-event codes (include/uapi/linux/input-event-codes.h).
EV_SYN, EV_KEY, EV_REL, EV_ABS, SYN_REPORT = 0, 1, 2, 3, 0
REL_X, REL_Y, REL_HWHEEL, REL_WHEEL = 0x00, 0x01, 0x06, 0x08
ABS_X, ABS_Y = 0x00, 0x01
BUTTONS = {"left": 0x110, "right": 0x111, "middle": 0x112}
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_RELBIT = 1074025828, 1074025829, 1074025830
UI_SET_ABSBIT = 1074025831
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


def frame_to_abs(px, size):
    """Pixel index in a frame `size` pixels wide -> absolute device units,
    aimed at the pixel's centre (compositors map the device range 0..ABS_MAX
    across the screen)."""
    return min(ABS_MAX, int((px + 0.5) * (ABS_MAX + 1) / size))


def parse_command(line):
    """One FIFO line -> a command tuple. Raises ValueError on anything
    malformed. The whole input contract, unit-testable without /dev/uinput.

    Returns one of:
      ("move", dx, dy, glide_ms_or_None)
      ("moveto", abs_x, abs_y)              abs_* in 0..ABS_MAX
      ("click", button, hold_ms) / ("down", button, None) / ("up", button, None)
      ("scroll", ticks, horizontal_bool)
      ("release",) / ("quit",)
    """
    if not isinstance(line, str) or len(line) > MAX_LINE:
        raise ValueError("line too long")
    parts = line.split()
    if not parts:
        raise ValueError("empty line")
    cmd, args = parts[0], parts[1:]
    if cmd == "quit":
        if args:
            raise ValueError("quit takes no arguments")
        return ("quit",)
    if cmd == "release":
        if args:
            raise ValueError("release takes no arguments")
        return ("release",)
    if cmd == "move":
        if len(args) not in (2, 3):
            raise ValueError("usage: move <dx> <dy> [glide_ms]")
        dx = _bounded_int(args[0], -MAX_MOVE, MAX_MOVE, "dx")
        dy = _bounded_int(args[1], -MAX_MOVE, MAX_MOVE, "dy")
        glide = _bounded_int(args[2], 0, MAX_HOLD_MS, "glide") if len(args) == 3 else None
        return ("move", dx, dy, glide)
    if cmd == "moveto":
        if len(args) == 2:
            x = _bounded_int(args[0], 0, ABS_MAX, "x")
            y = _bounded_int(args[1], 0, ABS_MAX, "y")
            return ("moveto", x, y)
        if len(args) == 4:
            w = _bounded_int(args[2], 1, MAX_FRAME, "width")
            h = _bounded_int(args[3], 1, MAX_FRAME, "height")
            x = _bounded_int(args[0], 0, w - 1, "x")
            y = _bounded_int(args[1], 0, h - 1, "y")
            return ("moveto", frame_to_abs(x, w), frame_to_abs(y, h))
        raise ValueError("usage: moveto <x> <y> [<width> <height>]")
    if cmd in ("click", "down", "up"):
        if not args or args[0] not in BUTTONS:
            raise ValueError("unknown button: %s" % (args[0] if args else ""))
        if cmd == "click":
            if len(args) > 2:
                raise ValueError("click takes a button and an optional hold in ms")
            hold = _bounded_int(args[1], 0, MAX_HOLD_MS, "hold") if len(args) == 2 else DEFAULT_CLICK_MS
            return ("click", args[0], hold)
        if len(args) != 1:
            raise ValueError("%s takes exactly one button" % cmd)
        return (cmd, args[0], None)
    if cmd in ("scroll", "hscroll"):
        if len(args) != 1:
            raise ValueError("%s takes exactly one tick count" % cmd)
        ticks = _bounded_int(args[0], -MAX_SCROLL, MAX_SCROLL, "scroll")
        return ("scroll", ticks, cmd == "hscroll")
    raise ValueError("unknown command: %s" % cmd)


class ElfMouse:
    """The relative and absolute uinput devices, plus the buttons held down
    (each mapped to the device that pressed it)."""

    def __init__(self, fd, abs_fd=None):
        self.fd = fd
        self.abs_fd = abs_fd
        self.button_fd = fd      # buttons go to the device that moved last
        self.abs_pos = None      # last absolute position sent
        self.held = {}

    def emit(self, ev_type, code, value, fd=None):
        # One event, NO frame boundary: callers batch and close with syn().
        os.write(self.fd if fd is None else fd,
                 struct.pack("qqHHi", 0, 0, ev_type, code, value))

    def syn(self, fd=None):
        self.emit(EV_SYN, SYN_REPORT, 0, fd)

    def move(self, dx, dy):
        self.button_fd = self.fd
        if dx:
            self.emit(EV_REL, REL_X, dx)
        if dy:
            self.emit(EV_REL, REL_Y, dy)
        if dx or dy:
            self.syn()

    def move_to(self, x, y):
        """Place the pointer at absolute (x, y), in 0..ABS_MAX device units."""
        if self.abs_fd is None:
            raise OSError("no absolute pointer device")
        if self.abs_pos == (x, y):
            # The kernel drops an ABS value equal to the last one, so after a
            # relative move a repeated moveto would do nothing: nudge first.
            nudge = x - 1 if x > 0 else x + 1
            self.emit(EV_ABS, ABS_X, nudge, self.abs_fd)
            self.syn(self.abs_fd)
        self.emit(EV_ABS, ABS_X, x, self.abs_fd)
        self.emit(EV_ABS, ABS_Y, y, self.abs_fd)
        self.syn(self.abs_fd)
        self.abs_pos = (x, y)
        self.button_fd = self.abs_fd

    def glide(self, dx, dy, duration_ms):
        """Move (dx, dy) spread over duration_ms in ~20 ms steps, with the
        remainder distributed by cumulative rounding (no drift, no jumps)."""
        if not duration_ms:
            self.move(dx, dy)
            return
        steps = max(1, duration_ms // 20)
        sent_x = sent_y = 0
        for i in range(1, steps + 1):
            target_x = int(round(dx * i / steps))
            target_y = int(round(dy * i / steps))
            self.move(target_x - sent_x, target_y - sent_y)
            sent_x, sent_y = target_x, target_y
            if i < steps:
                time.sleep(0.02)

    def scroll(self, ticks, horizontal=False):
        if ticks:
            self.emit(EV_REL, REL_HWHEEL if horizontal else REL_WHEEL, ticks)
            self.syn()

    def set_button(self, name, down):
        if down:
            fd = self.held[name] = self.button_fd   # state first
        else:
            fd = self.held.pop(name, self.button_fd)  # release where pressed
        self.emit(EV_KEY, BUTTONS[name], 1 if down else 0, fd)
        self.syn(fd)

    def click(self, name, hold_ms):
        self.set_button(name, True)
        try:
            if hold_ms:
                time.sleep(hold_ms / 1000.0)
        finally:
            self.set_button(name, False)

    def release_all(self):
        for name, fd in list(self.held.items()):
            try:
                self.emit(EV_KEY, BUTTONS[name], 0, fd)
            except OSError:
                pass
            del self.held[name]
        for fd in {self.fd, self.abs_fd} - {None}:
            try:
                self.syn(fd)
            except OSError:
                pass


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
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_REL)
    for code in sorted(BUTTONS.values()):
        fcntl.ioctl(fd, UI_SET_KEYBIT, code)
    for code in (REL_X, REL_Y, REL_HWHEEL, REL_WHEEL):
        fcntl.ioctl(fd, UI_SET_RELBIT, code)
    # struct uinput_user_dev: name[80], id{ bustype, vendor, product, version }, ff_effects_max, absmax[64], absmin[64], absfuzz[64], absflat[64]
    dev = struct.pack("80sHHHHI", b"ElfMouse", 0x03, 0x1209, 0xBEE1, 1, 0) + b"\x00" * (64 * 4 * 4)
    os.write(fd, dev)
    fcntl.ioctl(fd, UI_DEV_CREATE)
    return fd


def _open_abs_device():
    """The absolute pointer: ABS_X/ABS_Y plus mouse buttons and no touch or
    pen bits, which udev classifies as a mouse with absolute axes (the same
    shape as a virtual machine's tablet pointer)."""
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_ABS)
    for code in sorted(BUTTONS.values()):
        fcntl.ioctl(fd, UI_SET_KEYBIT, code)
    absmax = [0] * 64
    for code in (ABS_X, ABS_Y):
        fcntl.ioctl(fd, UI_SET_ABSBIT, code)
        absmax[code] = ABS_MAX
    # struct uinput_user_dev: name[80], id, ff_effects_max, absmax[64],
    # absmin[64], absfuzz[64], absflat[64]
    dev = struct.pack("80sHHHHI", b"ElfMouse Absolute", 0x03, 0x1209, 0xBEE2, 1, 0)
    dev += struct.pack("<64i", *absmax) + b"\x00" * (64 * 4 * 3)
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
        abs_fd = _open_abs_device()
    except OSError as e:
        raise SystemExit("elfmouse: cannot open /dev/uinput: %s "
                         "(module loaded? permission? see the udev rule)" % e)
    mouse = ElfMouse(fd, abs_fd)
    print("ElfMouse devices created", flush=True)

    def _stop(signum, frame):
        raise _Stopped()

    for sig in (signal.SIGTERM, signal.SIGINT):
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
                    print("elfmouse: refused: %s (%s)" % (line.strip()[:80], e), file=sys.stderr, flush=True)
                    continue
                # audit log: every accepted command (see elfkey.py).
                print("elfmouse: %s" % line.strip()[:80], file=sys.stderr, flush=True)
                try:
                    cmd = parsed[0]
                    if cmd == "quit":
                        break
                    if cmd == "release":
                        mouse.release_all()
                    elif cmd == "move":
                        _, dx, dy, glide = parsed
                        mouse.glide(dx, dy, glide) if glide is not None else mouse.move(dx, dy)
                    elif cmd == "moveto":
                        mouse.move_to(parsed[1], parsed[2])
                    elif cmd == "click":
                        mouse.click(parsed[1], parsed[2])
                    elif cmd in ("down", "up"):
                        mouse.set_button(parsed[1], cmd == "down")
                    elif cmd == "scroll":
                        mouse.scroll(parsed[1], parsed[2])
                except OSError as e:
                    print("elfmouse: emit failed: %s" % e, file=sys.stderr, flush=True)
    except _Stopped:
        pass
    finally:
        mouse.release_all()
        for dev_fd in (fd, abs_fd):
            try:
                fcntl.ioctl(dev_fd, UI_DEV_DESTROY)
            except OSError:
                pass
            os.close(dev_fd)


if __name__ == "__main__":
    main()
