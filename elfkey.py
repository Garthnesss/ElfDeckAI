#!/usr/bin/env python3
# Copyright (c) 2026 Garth Elfstrom — MIT License (see LICENSE).
"""ElfKey: a virtual keyboard on /dev/uinput, driven by one-line commands
written to a FIFO.

    echo "key x 120"   > /tmp/elfkey.fifo     # tap X for 120 ms
    echo "down up"     > /tmp/elfkey.fifo     # hold the Up arrow
    echo "up up"       > /tmp/elfkey.fifo     # release it
    echo "release"     > /tmp/elfkey.fifo     # release every held key
    echo "quit"        > /tmp/elfkey.fifo

The FIFO and lock file live in /tmp, so nothing persists across a reboot.

Safety design (the caller may be a language model):
  * Malformed input never stops the daemon. Every line goes through
    parse_command(), which raises ValueError on anything invalid; the serve
    loop logs the refusal to stderr and keeps serving.
  * Taps are bounded to 0..MAX_HOLD_MS so one command cannot block the
    serve loop. Longer holds use down/up, which the caller controls.
  * Keys are never left stuck: a tap releases in a finally block, held keys
    are tracked, and on quit, SIGTERM, SIGINT, SIGHUP or any fatal error every held
    key is released before the uinput device is destroyed.
  * The FIFO is created 0600, so only the owning user can inject keys.

Standard library only.
"""
import fcntl
import os
import signal
import struct
import sys
import time

NAME = "elfkey"
FIFO = "/tmp/elfkey.fifo"
DEFAULT_HOLD_MS = 120
MAX_HOLD_MS = 5000          # a tap is a tap; use down/up for longer holds
MAX_LINE = 200              # longer FIFO lines are refused, never parsed

# Linux input-event codes (include/uapi/linux/input-event-codes.h).
KEYS = {
    "esc": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "minus": 12, "equals": 13, "equal": 13, "backspace": 14, "tab": 15,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23, "o": 24, "p": 25,
    "leftbrace": 26, "[": 26, "rightbrace": 27, "]": 27, "enter": 28,
    "ctrl": 29, "lctrl": 29,
    "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38,
    "semicolon": 39, ";": 39, "apostrophe": 40, "'": 40, "grave": 41, "`": 41,
    "shift": 42, "lshift": 42, "backslash": 43, "\\": 43,
    "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50,
    "comma": 51, ",": 51, "dot": 52, ".": 52, "period": 52, "slash": 53, "/": 53,
    "rshift": 54, "alt": 56, "lalt": 56, "space": 57, "capslock": 58,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64, "f7": 65, "f8": 66, "f9": 67, "f10": 68,
    "rctrl": 97, "ralt": 100,
    "home": 102, "up": 103, "pageup": 104, "left": 105, "right": 106, "end": 107, "down": 108,
    "pagedown": 109, "insert": 110, "delete": 111,
}
EV_KEY, EV_SYN, SYN_REPORT = 1, 0, 0
UI_SET_EVBIT, UI_SET_KEYBIT, UI_DEV_CREATE, UI_DEV_DESTROY = 1074025828, 1074025829, 21761, 21762


class _Stopped(Exception):
    """Raised by the signal handler so a blocked read/sleep unwinds at once
    (a handler that merely sets a flag would be swallowed by PEP 475 retries)."""


def parse_command(line):
    """One FIFO line -> (cmd, key_name_or_None, hold_ms_or_None).

    Raises ValueError for anything malformed. This is the whole input
    contract, kept separate from the device code so it can be unit-tested
    without /dev/uinput.
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
        return ("quit", None, None)
    if cmd == "release":
        if args:
            raise ValueError("release takes no arguments")
        return ("release", None, None)
    if cmd == "wait":
        if len(args) != 1:
            raise ValueError("wait takes exactly one duration in ms")
        try:
            ms = int(args[0])
        except ValueError:
            raise ValueError("wait must be a whole number of ms: %r" % args[0])
        if not 0 <= ms <= MAX_HOLD_MS:
            raise ValueError("wait must be 0-%d ms" % MAX_HOLD_MS)
        return ("wait", None, ms)
    if cmd not in ("key", "down", "up"):
        raise ValueError("unknown command: %s" % cmd)
    if not args or args[0] not in KEYS:
        raise ValueError("unknown key: %s" % (args[0] if args else ""))
    if cmd in ("down", "up"):
        if len(args) != 1:
            raise ValueError("%s takes exactly one key" % cmd)
        return (cmd, args[0], None)
    if len(args) > 2:
        raise ValueError("key takes a key name and an optional hold in ms")
    hold = DEFAULT_HOLD_MS
    if len(args) == 2:
        try:
            hold = int(args[1])
        except ValueError:
            raise ValueError("hold must be a whole number of ms: %r" % args[1])
        if not 0 <= hold <= MAX_HOLD_MS:
            raise ValueError("hold must be 0-%d ms (use down/up for longer)" % MAX_HOLD_MS)
    return ("key", args[0], hold)


class ElfKey:
    """The uinput device plus the set of keys currently held down."""

    def __init__(self, fd):
        self.fd = fd
        self.held = set()

    def emit(self, code, value):
        os.write(self.fd, struct.pack("qqHHi", 0, 0, EV_KEY, code, value))

    def syn(self):
        os.write(self.fd, struct.pack("qqHHi", 0, 0, EV_SYN, SYN_REPORT, 0))

    def tap(self, name, hold_ms):
        code = KEYS[name]
        self.held.add(name)          # tracked BEFORE the event goes out:
        self.emit(code, 1)           # a signal landing mid-tap can't orphan it
        self.syn()
        try:
            if hold_ms:
                time.sleep(hold_ms / 1000.0)
        finally:
            self.emit(code, 0)
            self.syn()
            self.held.discard(name)

    def set_key(self, name, down):
        (self.held.add if down else self.held.discard)(name)
        self.emit(KEYS[name], 1 if down else 0)
        self.syn()

    def release_all(self):
        for name in list(self.held):
            try:
                self.emit(KEYS[name], 0)
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
    for code in sorted(set(KEYS.values())):
        fcntl.ioctl(fd, UI_SET_KEYBIT, code)
    # struct uinput_user_dev: name[80], id{ bustype, vendor, product, version }, ff_effects_max, absmax[64], absmin[64], absfuzz[64], absflat[64]
    dev = struct.pack("80sHHHHI", b"ElfKey", 0x03, 0x1209, 0xBEEF, 1, 0) + b"\x00" * (64 * 4 * 4)
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
        raise SystemExit("elfkey: cannot open /dev/uinput: %s "
                         "(module loaded? permission? see the udev rule)" % e)
    keys = ElfKey(fd)
    print("ElfKey device created", flush=True)

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
                    cmd, name, hold = parse_command(line)
                except ValueError as e:
                    print("elfkey: refused: %s (%s)" % (line.strip()[:80], e), file=sys.stderr, flush=True)
                    continue
                # audit log: every accepted command, so "what did the agent
                # actually press?" is answerable later (journald/stderr).
                print("elfkey: %s" % line.strip()[:80], file=sys.stderr, flush=True)
                try:
                    if cmd == "quit":
                        break
                    if cmd == "release":
                        keys.release_all()
                    elif cmd == "wait":
                        time.sleep(hold / 1000.0)
                    elif cmd == "key":
                        keys.tap(name, hold)
                    else:
                        keys.set_key(name, cmd == "down")
                except OSError as e:
                    print("elfkey: emit failed: %s" % e, file=sys.stderr, flush=True)
    except _Stopped:
        pass
    finally:
        keys.release_all()
        try:
            fcntl.ioctl(fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(fd)


if __name__ == "__main__":
    main()
