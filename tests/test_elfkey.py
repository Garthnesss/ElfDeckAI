"""ElfKey (elfkey.py): the FIFO input contract and the held-key
bookkeeping. The uinput device itself needs /dev/uinput on a Deck, so the
device is faked here — parse_command() and ElfKey.emit bookkeeping are the
parts a bad line can actually break, and they are fully testable.

Regression targets from the 2026-10-27 adversarial review:
  * `key x abc` crashed the old daemon (uncaught int()).
  * `key x 999999999` blocked the serve loop holding the key for days.
  * a killed daemon could leave a `down` key stuck forever.
  * unknown keys/commands vanished silently.
"""
import os, stat, struct

import pytest

from conftest import ROOT, elfkey


# --- parse_command: the valid protocol ---------------------------------------

@pytest.mark.parametrize("line,want", [
    ("key x", ("key", "x", elfkey.DEFAULT_HOLD_MS)),
    ("key x 150", ("key", "x", 150)),
    ("key enter 0", ("key", "enter", 0)),
    ("key space 5000", ("key", "space", 5000)),
    ("key , 100", ("key", ",", 100)),
    ("key minus 100", ("key", "minus", 100)),
    ("key f5 100", ("key", "f5", 100)),
    ("down up", ("down", "up", None)),
    ("up space", ("up", "space", None)),
    ("down ctrl", ("down", "ctrl", None)),
    ("quit", ("quit", None, None)),
    ("wait 250", ("wait", None, 250)),
    ("wait 0", ("wait", None, 0)),
    ("  key   z   265  ", ("key", "z", 265)),
])
def test_parse_ok(line, want):
    assert elfkey.parse_command(line) == want


# --- parse_command: everything malformed is a ValueError, never a crash -------

@pytest.mark.parametrize("line", [
    "", "   ", "key", "key nosuchkey", "key x abc", "key x 1.5", "key x -1",
    "key x 5001", "key x 999999999", "key x 100 extra", "down", "down x y",
    "up nosuchkey", "quit now", "wait", "wait -1", "wait 5001", "wait x", "wait 1 2", "press x", "KEY x", "ke y x",
    "key x " + "9" * 300,                       # absurd hold, also over the line cap below
    "key " + "x" * 300,                         # over MAX_LINE
])
def test_parse_refuses(line):
    with pytest.raises(ValueError):
        elfkey.parse_command(line)


def test_every_retroarch_key_still_there():
    for name in ("x", "z", "s", "a", "q", "w", "enter", "rshift", "space",
                 "up", "down", "left", "right", "esc"):
        assert name in elfkey.KEYS, name


def test_key_codes_are_valid_and_unique_per_code_meaning():
    for name, code in elfkey.KEYS.items():
        assert isinstance(code, int) and 0 < code < 768, (name, code)


def test_punctuation_and_modifiers_now_exist():
    for name in ("minus", "equals", "comma", "dot", "slash", "semicolon",
                 "apostrophe", "backslash", "ctrl", "alt", "capslock", "f1", "f10"):
        assert name in elfkey.KEYS, name


# --- ElfKey bookkeeping with a fake device fd ----------------------------------

@pytest.fixture
def fake(tmp_path):
    """An ElfKey writing to a real temp file instead of /dev/uinput.
    events() reads the file back as (type, code, value) triples."""
    fdpath = tmp_path / "fake-uinput"
    fdpath.write_bytes(b"")
    fd = os.open(fdpath, os.O_RDWR)
    dev = elfkey.ElfKey(fd)

    def events():
        os.lseek(fd, 0, 0)
        data = os.read(fd, 1 << 20)
        return [struct.unpack("qqHHi", data[i:i + 24])[2:] for i in range(0, len(data) - 23, 24)]

    yield dev, events
    os.close(fd)


def _events(events):
    return events()


def test_tap_presses_and_releases(fake):
    dev, writes = fake
    dev.tap("x", 0)
    ev = [e for e in _events(writes) if e[0] == elfkey.EV_KEY]
    assert ev == [(elfkey.EV_KEY, elfkey.KEYS["x"], 1), (elfkey.EV_KEY, elfkey.KEYS["x"], 0)]
    assert dev.held == set()


def test_tap_releases_even_if_sleep_blows_up(fake, monkeypatch):
    dev, writes = fake
    def boom(_s):
        raise elfkey._Stopped()
    monkeypatch.setattr(elfkey.time, "sleep", boom)
    with pytest.raises(elfkey._Stopped):
        dev.tap("space", 100)
    ev = [e for e in _events(writes) if e[0] == elfkey.EV_KEY]
    assert ev[-1] == (elfkey.EV_KEY, elfkey.KEYS["space"], 0)
    assert dev.held == set()


def test_release_all_clears_held_keys(fake):
    dev, writes = fake
    dev.set_key("space", True)
    dev.set_key("up", True)
    assert dev.held == {"space", "up"}
    dev.release_all()
    assert dev.held == set()
    ev = [e for e in _events(writes) if e[0] == elfkey.EV_KEY]
    assert (elfkey.EV_KEY, elfkey.KEYS["space"], 0) in ev
    assert (elfkey.EV_KEY, elfkey.KEYS["up"], 0) in ev


# --- The daemon loop itself, with a fake /dev/uinput ----------------------------

def test_daemon_survives_bad_input_and_releases_on_quit(tmp_path, monkeypatch):
    import threading, time as _time
    fifo_path = tmp_path / "elfkey.fifo"
    devpath = tmp_path / "fake-uinput"
    devpath.write_bytes(b"")
    fd = os.open(devpath, os.O_RDWR)
    monkeypatch.setattr(elfkey, "FIFO", str(fifo_path))
    monkeypatch.setattr(elfkey, "_open_device", lambda: fd)
    t = threading.Thread(target=elfkey.main, daemon=True)
    t.start()
    try:
        for _ in range(100):
            if fifo_path.exists():
                break
            _time.sleep(0.05)
        assert fifo_path.exists()

        def send(line):
            w = os.open(fifo_path, os.O_WRONLY)
            try:
                os.write(w, (line + "\n").encode())
            finally:
                os.close(w)

        w = os.open(fifo_path, os.O_WRONLY)
        os.write(w, b"key \xff\xfe\n")   # not UTF-8: refused, not fatal
        os.close(w)
        send("key x abc")          # not a number: refused, not fatal
        send("down space")         # hold fast-forward...
        send("key x 10")           # ...a good tap still works after the bad line...
        send("quit")               # ...and quit releases the held key
        t.join(timeout=5)
        assert not t.is_alive()
    finally:
        if t.is_alive():
            w = os.open(fifo_path, os.O_WRONLY)
            os.write(w, b"quit\n")
            os.close(w)
            t.join(timeout=5)
    data = devpath.read_bytes()
    ev = [struct.unpack("qqHHi", data[i:i + 24])[2:] for i in range(0, len(data) - 23, 24)]
    keys_only = [e for e in ev if e[0] == elfkey.EV_KEY]
    assert (elfkey.EV_KEY, elfkey.KEYS["space"], 1) in keys_only
    assert keys_only[-1] == (elfkey.EV_KEY, elfkey.KEYS["space"], 0)   # released last, by quit cleanup
    assert (elfkey.EV_KEY, elfkey.KEYS["x"], 1) in keys_only


# --- FIFO creation --------------------------------------------------------------

def test_fifo_is_owner_only(tmp_path):
    p = tmp_path / "elfkey.fifo"
    elfkey._make_fifo(str(p))
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o600, oct(mode)
    assert stat.S_ISFIFO(os.stat(p).st_mode)


def test_fifo_recreated_if_a_plain_file_is_in_the_way(tmp_path):
    p = tmp_path / "elfkey.fifo"
    p.write_text("junk")
    os.chmod(p, 0o644)
    elfkey._make_fifo(str(p))
    assert stat.S_ISFIFO(os.stat(p).st_mode)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_parse_release_command():
    assert elfkey.parse_command("release") == ("release", None, None)
    with pytest.raises(ValueError):
        elfkey.parse_command("release now")


def test_daemon_release_command_unsticks_a_held_key(tmp_path, monkeypatch):
    import threading, time as _time
    fifo_path = tmp_path / "elfkey.fifo"
    devpath = tmp_path / "fake-uinput"
    devpath.write_bytes(b"")
    fd = os.open(devpath, os.O_RDWR)
    monkeypatch.setattr(elfkey, "FIFO", str(fifo_path))
    monkeypatch.setattr(elfkey, "_open_device", lambda: fd)
    t = threading.Thread(target=elfkey.main, daemon=True)
    t.start()
    try:
        for _ in range(100):
            if fifo_path.exists():
                break
            _time.sleep(0.05)

        def send(line):
            w = os.open(fifo_path, os.O_WRONLY)
            try:
                os.write(w, (line + "\n").encode())
            finally:
                os.close(w)

        send("down space")         # a caller holds fast-forward...
        send("release")            # ...a NEW caller unsticks it, daemon stays up
        send("down space")         # and the daemon still works afterwards
        send("quit")
        t.join(timeout=5)
        assert not t.is_alive()
    finally:
        if t.is_alive():
            w = os.open(fifo_path, os.O_WRONLY)
            os.write(w, b"quit\n")
            os.close(w)
            t.join(timeout=5)
    data = devpath.read_bytes()
    ev = [struct.unpack("qqHHi", data[i:i + 24])[2:] for i in range(0, len(data) - 23, 24)]
    space = [e for e in ev if e[0] == elfkey.EV_KEY and e[1] == elfkey.KEYS["space"]]
    assert [e[2] for e in space] == [1, 0, 1, 0]   # hold, release, hold, quit-cleanup


def test_instance_lock_refuses_a_second_process(tmp_path):
    import subprocess, sys as _sys
    if elfkey._LOCK_FD is not None:      # a daemon-loop test may hold it
        os.close(elfkey._LOCK_FD)
        elfkey._LOCK_FD = None
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import elfkey\n"
        "elfkey._acquire_instance_lock()\n"
        "print('locked', flush=True)\n"
        "import time; time.sleep(5)\n" % str(ROOT)
    )
    child = subprocess.Popen([_sys.executable, "-c", code],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(SystemExit):
            elfkey._acquire_instance_lock()
    finally:
        child.kill()
        child.wait()
    elfkey._acquire_instance_lock()      # free again once the child is gone
    os.close(elfkey._LOCK_FD)
    elfkey._LOCK_FD = None
