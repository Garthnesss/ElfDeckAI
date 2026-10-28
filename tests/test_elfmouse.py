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
    ("moveto 0 0", ("moveto", 0, 0)),
    ("moveto 32767 100", ("moveto", 32767, 100)),
    ("moveto 0 0 1280 800", ("moveto", 12, 20)),
    ("moveto 1279 799 1280 800", ("moveto", 32755, 32747)),
    ("moveto 0 0 1 1", ("moveto", 16384, 16384)),
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
    "moveto", "moveto 1", "moveto 1 2 3", "moveto 1 2 3 4 5", "moveto -1 0",
    "moveto 32768 0", "moveto 1280 0 1280 800", "moveto 0 800 1280 800",
    "moveto 0 0 0 800", "moveto 0 0 20001 800", "moveto x 0",
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
    assert m.held == {}


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
    monkeypatch.setattr(elfmouse, "_open_abs_device", lambda: os.open("/dev/null", os.O_WRONLY))
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


def _events_by_fd(monkeypatch):
    seen = []

    def fake_write(fd, data):
        for _s, _u, typ, code, value in struct.iter_unpack("qqHHi", data):
            if typ != elfmouse.EV_SYN:
                seen.append((fd, typ, code, value))
        return len(data)

    monkeypatch.setattr(elfmouse.os, "write", fake_write)
    return seen


def test_frame_to_abs_hits_pixel_centres():
    # a compositor maps device unit v to screen x = v * W / (ABS_MAX + 1)
    for size in (800, 1280, 1920, 3840):
        for px in (0, 1, size // 2, size - 1):
            v = elfmouse.frame_to_abs(px, size)
            assert int(v * size / (elfmouse.ABS_MAX + 1)) == px


def test_move_to_emits_on_the_absolute_device(monkeypatch):
    seen = _events_by_fd(monkeypatch)
    m = elfmouse.ElfMouse(fd="rel", abs_fd="abs")
    m.move_to(100, 200)
    assert seen == [("abs", elfmouse.EV_ABS, elfmouse.ABS_X, 100),
                    ("abs", elfmouse.EV_ABS, elfmouse.ABS_Y, 200)]


def test_repeated_move_to_nudges_past_the_kernels_duplicate_filter(monkeypatch):
    seen = _events_by_fd(monkeypatch)
    m = elfmouse.ElfMouse(fd="rel", abs_fd="abs")
    m.move_to(100, 200)
    m.move(5, 5)                       # pointer wandered off relatively
    seen.clear()
    m.move_to(100, 200)                # same target again: must still move
    assert seen == [("abs", elfmouse.EV_ABS, elfmouse.ABS_X, 99),
                    ("abs", elfmouse.EV_ABS, elfmouse.ABS_X, 100),
                    ("abs", elfmouse.EV_ABS, elfmouse.ABS_Y, 200)]


def test_buttons_follow_the_device_that_moved_last(monkeypatch):
    seen = _events_by_fd(monkeypatch)
    m = elfmouse.ElfMouse(fd="rel", abs_fd="abs")
    m.move_to(10, 10)
    m.set_button("left", True)         # pressed on the absolute device
    m.move(3, 0)                       # a relative drag...
    m.set_button("left", False)        # ...releases where it was pressed
    m.click("right", 0)                # the next click follows the last move
    keys = [(fd, code, v) for fd, typ, code, v in seen if typ == elfmouse.EV_KEY]
    assert keys == [("abs", elfmouse.BUTTONS["left"], 1),
                    ("abs", elfmouse.BUTTONS["left"], 0),
                    ("rel", elfmouse.BUTTONS["right"], 1),
                    ("rel", elfmouse.BUTTONS["right"], 0)]


def test_release_all_releases_on_each_buttons_own_device(monkeypatch):
    seen = _events_by_fd(monkeypatch)
    m = elfmouse.ElfMouse(fd="rel", abs_fd="abs")
    m.set_button("right", True)
    m.move_to(1, 1)
    m.set_button("left", True)
    seen.clear()
    m.release_all()
    assert sorted(seen) == [("abs", elfmouse.EV_KEY, elfmouse.BUTTONS["left"], 0),
                            ("rel", elfmouse.EV_KEY, elfmouse.BUTTONS["right"], 0)]
    assert m.held == {}
