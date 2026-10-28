#!/usr/bin/env python3
# Copyright (c) 2026 Garth Elfstrom — MIT License (see LICENSE).
"""ElfMouse: a virtual mouse on /dev/uinput, driven by one-line shell
commands over a FIFO — the third hand of ElfdeckAI, next to ElfKey
(keyboard) and ElfPad (gamepad).

    echo "move 120 -40"     > /tmp/elfmouse.fifo   # move right 120, up 40
    echo "move 400 0 600"   > /tmp/elfmouse.fifo   # glide 400 px over 600 ms
    echo "click left"       > /tmp/elfmouse.fifo   # click
    echo "down left"        > /tmp/elfmouse.fifo   # hold (drag)...
    echo "move 200 120"     > /tmp/elfmouse.fifo   # ...drag...
    echo "up left"          > /tmp/elfmouse.fifo   # ...drop
    echo "scroll -3"        > /tmp/elfmouse.fifo   # wheel down 3 ticks
    echo "quit"             > /tmp/elfmouse.fifo

Movement is relative (that's what a mouse is); an agent pairs it with
screenshots, the same look → move → verify loop as the rest of the set.

Same safety design as its siblings, for the same reason — the caller
might be a language model:

  * Bad input never kills the daemon: parse_command() raises ValueError
    on anything malformed; the serve loop logs the refusal and keeps
    serving.
  * Moves, glides and scrolls are bounded per command (an agent can
    chain commands; it cannot ask for a 10-metre teleport or an 11-day
    hold). Timed actions cap at MAX_HOLD_MS like the other daemons.
  * Buttons are never left held: clicks release in a finally block,
    held buttons are tracked, and on quit / SIGTERM / SIGINT / any
    fatal error every held button is released before the device is
    destroyed. (Pointer position needs no cleanup — it just stops.)
  * The FIFO is created 0600: only the owning user can inject input.

Standard library only. Works on any Linux with /dev/uinput.
"""
import fcntl, os, signal, struct, sys, time

NAME = "elfmouse"
FIFO = "/tmp/elfmouse.fifo"
DEFAULT_CLICK_MS = 100
MAX_HOLD_MS = 5000          # glides and clicks cap here, like every tap
MAX_LINE = 200              # longer FIFO lines are refused, never parsed
MAX_MOVE = 10000            # px per command — plenty; chain for more
MAX_SCROLL = 120            # wheel ticks per command

# Linux input-event codes (include/uapi/linux/input-event-codes.h).
EV_SYN, EV_KEY, EV_REL, SYN_REPORT = 0, 1, 2, 0
REL_X, REL_Y, REL_HWHEEL, REL_WHEEL = 0x00, 0x01, 0x06, 0x08
BUTTONS = {"left": 0x110, "right": 0x111, "middle": 0x112}
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_RELBIT = 1074025828, 1074025829, 1074025830
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


def parse_command(line):
    """One FIFO line -> a command tuple. Raises ValueError on anything
    malformed. The whole input contract, unit-testable without /dev/uinput.

    Returns one of:
      ("move", dx, dy, glide_ms_or_None)
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
    """The uinput device plus the set of buttons currently held down."""

    def __init__(self, fd):
        self.fd = fd
        self.held = set()

    def emit(self, ev_type, code, value):
        # One event, NO frame boundary: callers batch and close with syn().
        os.write(self.fd, struct.pack("qqHHi", 0, 0, ev_type, code, value))

    def syn(self):
        os.write(self.fd, struct.pack("qqHHi", 0, 0, EV_SYN, SYN_REPORT, 0))

    def move(self, dx, dy):
        if dx:
            self.emit(EV_REL, REL_X, dx)
        if dy:
            self.emit(EV_REL, REL_Y, dy)
        if dx or dy:
            self.syn()

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
        (self.held.add if down else self.held.discard)(name)  # state first
        self.emit(EV_KEY, BUTTONS[name], 1 if down else 0)
        self.syn()

    def click(self, name, hold_ms):
        self.set_button(name, True)
        try:
            if hold_ms:
                time.sleep(hold_ms / 1000.0)
        finally:
            self.set_button(name, False)

    def release_all(self):
        for name in list(self.held):
            try:
                self.emit(EV_KEY, BUTTONS[name], 0)
            except OSError:
                pass
            self.held.discard(name)
        try:
            self.syn()
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
        raise SystemExit("elfmouse: cannot open /dev/uinput: %s "
                         "(module loaded? permission? see the udev rule)" % e)
    mouse = ElfMouse(fd)
    print("ElfMouse device created", flush=True)

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
        # blocks, so the daemon survives writers coming and going.
        with os.fdopen(os.open(FIFO, os.O_RDWR), "r") as fifo:
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
        try:
            fcntl.ioctl(fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(fd)


if __name__ == "__main__":
    main()
