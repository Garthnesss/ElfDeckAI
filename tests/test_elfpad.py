"""ElfPad tests: the parse_command contract and the pad's event output,
exercised against a captured fake uinput fd (no /dev/uinput needed)."""
import os
import struct
import time

import pytest

from conftest import ROOT, elfpad


# ----------------------------------------------------------------------
# parse_command: the input contract

@pytest.mark.parametrize("line,want", [
    ("press a", ("press", "a", 120)),
    ("press a 250", ("press", "a", 250)),
    ("press up", ("press", "up", 120)),            # dpad is a valid "button"
    ("press guide 0", ("press", "guide", 0)),
    ("down lb", ("down", "lb", None)),
    ("up start", ("up", "start", None)),
    ("stick left 60 -20", ("stick", "left", 60, -20, None)),
    ("stick right 0 0 800", ("stick", "right", 0, 0, 800)),
    ("trigger left 0", ("trigger", "left", 0, None)),
    ("trigger right 100 300", ("trigger", "right", 100, 300)),
    ("center", ("center",)),
    ("reset", ("reset",)),
    ("quit", ("quit",)),
])
def test_parse_ok(line, want):
    assert elfpad.parse_command(line) == want


@pytest.mark.parametrize("line", [
    "", "bogus", "press", "press nope", "press a abc", "press a -1",
    "press a 5001", "press a 1 2", "down", "down a extra", "up nope",
    "stick", "stick middle 0 0", "stick left 101 0", "stick left 0 -101",
    "stick left x 0", "stick left 0 0 999999999", "trigger", "trigger up 5",
    "trigger left -1", "trigger left 101", "trigger left abc",
    "center now", "reset all", "quit now", "x" * 201,
])
def test_parse_refused(line):
    with pytest.raises(ValueError):
        elfpad.parse_command(line)


# ----------------------------------------------------------------------
# Device behaviour against a captured fd

def _events(monkeypatch):
    """Patch os.write inside elfpad; return a list of (type, code, value)
    with SYN events filtered out."""
    seen = []

    def fake_write(fd, data):
        for _sec, _usec, typ, code, value in struct.iter_unpack("qqHHi", data):
            if typ != elfpad.EV_SYN:
                seen.append((typ, code, value))
        return len(data)

    monkeypatch.setattr(elfpad.os, "write", fake_write)
    return seen


def test_press_button_events(monkeypatch):
    seen = _events(monkeypatch)
    pad = elfpad.ElfPad(fd=object())
    pad.press("a", 0)
    assert seen == [(elfpad.EV_KEY, elfpad.BUTTONS["a"], 1),
                    (elfpad.EV_KEY, elfpad.BUTTONS["a"], 0)]
    assert pad.held == set()


def test_press_dpad_uses_hat_axis(monkeypatch):
    seen = _events(monkeypatch)
    pad = elfpad.ElfPad(fd=object())
    pad.press("up", 0)
    pad.press("right", 0)
    assert seen == [(elfpad.EV_ABS, 0x11, -1), (elfpad.EV_ABS, 0x11, 0),
                    (elfpad.EV_ABS, 0x10, 1), (elfpad.EV_ABS, 0x10, 0)]


def test_dpad_opposing_directions_cancel(monkeypatch):
    seen = _events(monkeypatch)
    pad = elfpad.ElfPad(fd=object())
    pad.set_button("left", True)
    pad.set_button("right", True)          # left+right held -> hat X back to 0
    assert seen[-1] == (elfpad.EV_ABS, 0x10, 0)
    pad.release_all()
    assert pad.dpad == set() and pad.axes[0x10] == 0


def test_stick_scaling_and_center(monkeypatch):
    seen = _events(monkeypatch)
    pad = elfpad.ElfPad(fd=object())
    pad.set_stick("left", 100, -100)
    assert (elfpad.EV_ABS, 0x00, 32767) in seen
    assert (elfpad.EV_ABS, 0x01, -32768) in seen   # full negative deflection = axis min
    pad.set_stick("left", 50, 0)
    assert pad.axes[0x00] == 16384         # round(50% of 32767)
    seen.clear()
    pad.center()
    assert pad.axes[0x00] == 0 and pad.axes[0x01] == 0
    assert (elfpad.EV_ABS, 0x00, 0) in seen


def test_trigger_scaling(monkeypatch):
    seen = _events(monkeypatch)
    pad = elfpad.ElfPad(fd=object())
    pad.set_trigger("right", 100)
    pad.set_trigger("left", 50)
    assert pad.axes[0x05] == 255 and pad.axes[0x02] == 128
    assert (elfpad.EV_ABS, 0x05, 255) in seen


def test_release_all_releases_buttons_and_axes(monkeypatch):
    seen = _events(monkeypatch)
    pad = elfpad.ElfPad(fd=object())
    pad.set_button("x", True)
    pad.set_stick("right", -40, 90)
    pad.set_trigger("left", 70)
    seen.clear()
    pad.release_all()
    assert (elfpad.EV_KEY, elfpad.BUTTONS["x"], 0) in seen
    assert pad.held == set()
    assert all(v == 0 for v in pad.axes.values())


def test_timed_stick_recenters_even_when_interrupted(monkeypatch):
    _events(monkeypatch)
    pad = elfpad.ElfPad(fd=object())
    monkeypatch.setattr(elfpad.time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        pad.stick_for("left", 80, 80, 100)
    assert pad.axes[0x00] == 0 and pad.axes[0x01] == 0


# ----------------------------------------------------------------------
# Whole-daemon behaviour through main()

def test_daemon_loop_survives_bad_input_and_quit_releases(tmp_path, monkeypatch):
    """In-process main() (thread): a malformed line must not kill the loop,
    and quit must release a held button and recenter a deflected stick."""
    import threading

    fifo_path = str(tmp_path / "elfpad.fifo")
    monkeypatch.setattr(elfpad, "FIFO", fifo_path)
    monkeypatch.setattr(elfpad, "_open_device", lambda: os.open("/dev/null", os.O_WRONLY))
    monkeypatch.setattr(elfpad.fcntl, "ioctl", lambda *a, **k: 0)
    seen = _events(monkeypatch)

    t = threading.Thread(target=elfpad.main, daemon=True)
    t.start()
    for _ in range(100):
        if os.path.exists(fifo_path):
            break
        time.sleep(0.05)
    with open(fifo_path, "w") as f:
        f.write("press a nope\n")              # refused: bad hold value
        f.write("down b\n")                    # held...
        f.write("stick left 100 0\n")          # deflected...
        f.write("quit\n")                      # ...both undone on the way out
    t.join(timeout=10)
    assert not t.is_alive()
    assert (elfpad.EV_KEY, elfpad.BUTTONS["b"], 1) in seen
    # release_all() on shutdown: button up first, then the stick recenters.
    assert seen[-2:] == [(elfpad.EV_KEY, elfpad.BUTTONS["b"], 0),
                         (elfpad.EV_ABS, 0x00, 0)]


def test_daemon_releases_held_button_on_quit():
    """Same guarantee as above, asserted on ElfPad's real atexit path in
    main() by driving the module in-process is racy; instead the unit-level
    proof is release_all() above, and main()'s finally calls it — checked
    here by source inspection so a refactor can't silently drop it."""
    src = (ROOT / "elfpad.py").read_text()
    finally_block = src.rsplit("finally:", 1)[1]
    assert "release_all()" in finally_block
    assert "UI_DEV_DESTROY" in finally_block
