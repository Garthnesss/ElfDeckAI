"""ElfMouse tests: the parse_command contract and the mouse's event
output, exercised against a captured fake uinput fd."""
import os
import struct
import time

import pytest

from conftest import elfmouse


@pytest.mark.parametrize("line,want", [
    ("move 120 -40", ("move", 120, -40, None)),
    ("move 400 0 600", ("move", 400, 0, 600)),
    ("move 0 0", ("move", 0, 0, None)),
    ("click left", ("click", "left", elfmouse.DEFAULT_CLICK_MS)),
    ("click right 250", ("click", "right", 250)),
    ("down middle", ("down", "middle", None)),
    ("up left", ("up", "left", None)),
    ("scroll -3", ("scroll", -3, False)),
    ("scroll 5", ("scroll", 5, False)),
    ("hscroll 2", ("scroll", 2, True)),
    ("quit", ("quit",)),
])
def test_parse_ok(line, want):
    assert elfmouse.parse_command(line) == want


@pytest.mark.parametrize("line", [
    "", "bogus", "move", "move 1", "move 1 2 3 4", "move x 0", "move 0 x",
    "move 10001 0", "move 0 -10001", "move 0 0 5001", "move 0 0 -5",
    "click", "click side", "click left abc", "click left 999999999",
    "down", "down left extra", "up nope",
    "scroll", "scroll 121", "scroll -121", "scroll x", "scroll 1 2",
    "hscroll", "quit now", "x" * 201,
])
def test_parse_refused(line):
    with pytest.raises(ValueError):
        elfmouse.parse_command(line)


def _events(monkeypatch):
    seen = []

    def fake_write(fd, data):
        for _s, _u, typ, code, value in struct.iter_unpack("qqHHi", data):
            if typ != elfmouse.EV_SYN:
                seen.append((typ, code, value))
        return len(data)

    monkeypatch.setattr(elfmouse.os, "write", fake_write)
    return seen


def test_move_emits_relative_events(monkeypatch):
    seen = _events(monkeypatch)
    m = elfmouse.ElfMouse(fd=object())
    m.move(120, -40)
    m.move(0, 5)                       # zero axis emits nothing
    assert seen == [(elfmouse.EV_REL, elfmouse.REL_X, 120),
                    (elfmouse.EV_REL, elfmouse.REL_Y, -40),
                    (elfmouse.EV_REL, elfmouse.REL_Y, 5)]


def test_glide_sums_to_the_requested_delta(monkeypatch):
    seen = _events(monkeypatch)
    monkeypatch.setattr(elfmouse.time, "sleep", lambda s: None)
    m = elfmouse.ElfMouse(fd=object())
    m.glide(97, -53, 200)
    assert sum(v for t, c, v in seen if c == elfmouse.REL_X) == 97
    assert sum(v for t, c, v in seen if c == elfmouse.REL_Y) == -53
    assert len([1 for t, c, v in seen if c == elfmouse.REL_X]) > 1  # stepped


def test_click_and_release_all(monkeypatch):
    seen = _events(monkeypatch)
    m = elfmouse.ElfMouse(fd=object())
    m.click("left", 0)
    assert seen == [(elfmouse.EV_KEY, elfmouse.BUTTONS["left"], 1),
                    (elfmouse.EV_KEY, elfmouse.BUTTONS["left"], 0)]
    m.set_button("left", True)         # a drag starts...
    seen.clear()
    m.release_all()                    # ...and shutdown drops it
    assert seen == [(elfmouse.EV_KEY, elfmouse.BUTTONS["left"], 0)]
    assert m.held == set()


def test_scroll_directions(monkeypatch):
    seen = _events(monkeypatch)
    m = elfmouse.ElfMouse(fd=object())
    m.scroll(-3)
    m.scroll(2, horizontal=True)
    assert seen == [(elfmouse.EV_REL, elfmouse.REL_WHEEL, -3),
                    (elfmouse.EV_REL, elfmouse.REL_HWHEEL, 2)]


def test_daemon_loop_survives_bad_input_and_quit_releases(tmp_path, monkeypatch):
    import threading

    fifo_path = str(tmp_path / "elfmouse.fifo")
    monkeypatch.setattr(elfmouse, "FIFO", fifo_path)
    monkeypatch.setattr(elfmouse, "_open_device", lambda: os.open("/dev/null", os.O_WRONLY))
    monkeypatch.setattr(elfmouse.fcntl, "ioctl", lambda *a, **k: 0)
    seen = _events(monkeypatch)

    t = threading.Thread(target=elfmouse.main, daemon=True)
    t.start()
    for _ in range(100):
        if os.path.exists(fifo_path):
            break
        time.sleep(0.05)
    with open(fifo_path, "w") as f:
        f.write("move 99999 0\n")      # refused: out of bounds
        f.write("down left\n")         # held (drag)...
        f.write("quit\n")              # ...released on the way out
    t.join(timeout=10)
    assert not t.is_alive()
    assert seen == [(elfmouse.EV_KEY, elfmouse.BUTTONS["left"], 1),
                    (elfmouse.EV_KEY, elfmouse.BUTTONS["left"], 0)]


def test_parse_release_command():
    assert elfmouse.parse_command("release") == ("release",)
    with pytest.raises(ValueError):
        elfmouse.parse_command("release all")


def test_move_is_a_single_frame(monkeypatch):
    frames = []

    def fake_write(fd, data):
        frames.extend(struct.unpack("qqHHi", data[i:i + 24])[2:]
                      for i in range(0, len(data), 24))
        return len(data)

    monkeypatch.setattr(elfmouse.os, "write", fake_write)
    m = elfmouse.ElfMouse(fd=object())
    m.move(30, -20)
    assert [e[0] for e in frames].count(elfmouse.EV_SYN) == 1
    assert frames[-1][0] == elfmouse.EV_SYN
