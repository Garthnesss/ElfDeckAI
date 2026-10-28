"""The MCP server: protocol handshake, tool listing, and tool calls that
land on (fake) daemon FIFOs. Runs the real server as a subprocess and
speaks raw JSON-RPC at it, exactly like an MCP client would."""
import json
import os
import subprocess
import sys
import threading
import time

from conftest import ROOT, elfkey


def _server(tmp_path):
    key_fifo = str(tmp_path / "key.fifo")
    pad_fifo = str(tmp_path / "pad.fifo")
    mouse_fifo = str(tmp_path / "mouse.fifo")
    os.mkfifo(key_fifo)
    os.mkfifo(pad_fifo)
    os.mkfifo(mouse_fifo)
    received = []

    def drain(path):
        # O_RDWR (like the daemons themselves): the open never blocks and
        # the FIFO counts as having a reader from the start, so the server's
        # nonblocking writer-open can't race us.
        with os.fdopen(os.open(path, os.O_RDWR), "r") as f:
            for line in f:
                received.append((path, line.strip()))

    threads = [threading.Thread(target=drain, args=(p,), daemon=True)
               for p in (key_fifo, pad_fifo, mouse_fifo)]
    for t in threads:
        t.start()
    env = {**os.environ, "ELFKEY_FIFO": key_fifo, "ELFPAD_FIFO": pad_fifo,
           "ELFMOUSE_FIFO": mouse_fifo}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "elfdeckai_mcp.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)

    def rpc(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    return proc, rpc, received, key_fifo, pad_fifo, mouse_fifo


def _received_lines(received, expected, timeout=2.0):
    """The (fifo basename, line) pairs drained so far, waiting until every
    pair in `expected` has arrived: the drain threads read asynchronously,
    so a reply from the server does not mean its FIFO line was read yet."""
    deadline = time.monotonic() + timeout
    while True:
        got = {(os.path.basename(p), line) for p, line in received}
        if expected <= got or time.monotonic() > deadline:
            return got
        time.sleep(0.01)


def test_handshake_and_tool_list(tmp_path):
    proc, rpc, *_ = _server(tmp_path)
    try:
        r = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert r["result"]["serverInfo"]["name"] == "elfdeckai"
        assert r["result"]["protocolVersion"] == "2025-06-18"
        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        r = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in r["result"]["tools"]}
        assert names == {"key_tap", "key_down", "key_up", "pad_press",
                         "pad_down", "pad_up", "pad_stick", "pad_trigger",
                         "pad_center", "pad_reset", "mouse_move",
                         "mouse_click", "mouse_down", "mouse_up",
                         "mouse_scroll", "mouse_hscroll", "key_release",
                         "mouse_release", "screenshot", "mouse_move_to",
                         "mouse_click_at"}
        for t in r["result"]["tools"]:
            assert t["inputSchema"]["type"] == "object"
        by_name = {t["name"]: t for t in r["result"]["tools"]}
        tap = by_name["key_tap"]["inputSchema"]["properties"]
        assert tap["hold_ms"]["maximum"] == elfkey.MAX_HOLD_MS
        assert tap["key"]["enum"] == sorted(elfkey.KEYS)
        stick = by_name["pad_stick"]["inputSchema"]["properties"]
        assert stick["x"]["minimum"] == -100 and stick["x"]["maximum"] == 100
    finally:
        proc.kill()


def test_tool_calls_reach_the_fifos(tmp_path):
    proc, rpc, received, key_fifo, pad_fifo, mouse_fifo = _server(tmp_path)
    try:
        rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        r = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "key_tap",
                            "arguments": {"key": "x", "hold_ms": 150}}})
        assert r["result"]["isError"] is False
        r = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "pad_stick",
                            "arguments": {"side": "left", "x": 60, "y": -20}}})
        assert r["result"]["isError"] is False
        r = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "pad_center", "arguments": {}}})
        assert r["result"]["isError"] is False
        r = rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                 "params": {"name": "mouse_move",
                            "arguments": {"dx": 120, "dy": -40, "glide_ms": 300}}})
        assert r["result"]["isError"] is False
        r = rpc({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                 "params": {"name": "mouse_scroll",
                            "arguments": {"clicks": -3}}})
        assert r["result"]["isError"] is False
        r = rpc({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                 "params": {"name": "key_release", "arguments": {}}})
        assert r["result"]["isError"] is False
        r = rpc({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                 "params": {"name": "mouse_release", "arguments": {}}})
        assert r["result"]["isError"] is False
    finally:
        proc.kill()
    key, pad, mouse = (os.path.basename(f) for f in (key_fifo, pad_fifo, mouse_fifo))
    got = _received_lines(received, {
        (key, "key x 150"), (pad, "stick left 60 -20"), (pad, "center"),
        (mouse, "move 120 -40 300"), (mouse, "scroll -3"),
        (key, "release"), (mouse, "release")})
    assert (os.path.basename(key_fifo), "key x 150") in got
    assert (os.path.basename(pad_fifo), "stick left 60 -20") in got
    assert (os.path.basename(pad_fifo), "center") in got
    assert (os.path.basename(mouse_fifo), "move 120 -40 300") in got
    assert (os.path.basename(mouse_fifo), "scroll -3") in got
    assert (os.path.basename(key_fifo), "release") in got
    assert (os.path.basename(mouse_fifo), "release") in got


def test_bad_calls_are_tool_errors_not_crashes(tmp_path):
    proc, rpc, *_ = _server(tmp_path)
    try:
        rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        r = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "key_tap",
                            "arguments": {"key": "x", "hold_ms": 999999999}}})
        assert r["result"]["isError"] is True
        assert "invalid arguments" in r["result"]["content"][0]["text"]
        r = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "nope", "arguments": {}}})
        assert r["result"]["isError"] is True
        r = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
        assert "tools" in r["result"]          # server still alive
    finally:
        proc.kill()


def test_garbage_lines_get_protocol_errors_and_never_kill_the_server(tmp_path):
    proc, rpc, *_ = _server(tmp_path)
    try:
        rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        for raw in ("null", "[1, 2]", '"just a string"', "{not json"):
            proc.stdin.write(raw + "\n")
        proc.stdin.flush()
        codes = []
        for _ in range(4):
            codes.append(json.loads(proc.stdout.readline())["error"]["code"])
        assert codes == [-32600, -32600, -32600, -32700]
        r = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert "tools" in r["result"]          # still alive and serving
    finally:
        proc.kill()


def test_missing_daemon_reports_not_running(tmp_path):
    env = {**os.environ,
           "ELFKEY_FIFO": str(tmp_path / "no-such.fifo"),
           "ELFPAD_FIFO": str(tmp_path / "no-such.fifo"),
           "ELFMOUSE_FIFO": str(tmp_path / "no-such.fifo")}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "elfdeckai_mcp.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
    try:
        def rpc(msg):
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
            return json.loads(proc.stdout.readline())

        rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        r = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "key_tap", "arguments": {"key": "x"}}})
        assert r["result"]["isError"] is True
        assert "not running" in r["result"]["content"][0]["text"]
    finally:
        proc.kill()


def _call(rpc, id_, name, arguments):
    return rpc({"jsonrpc": "2.0", "id": id_, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}})


def test_strict_argument_validation(tmp_path):
    proc, rpc, received, *_ = _server(tmp_path)
    try:
        rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        cases = [
            ({"key": "x", "bogus": 1}, "unknown argument"),
            ({"key": "x", "hold_ms": True}, "must be an integer"),   # bool != int
            ({"key": "x", "hold_ms": 1.5}, "must be an integer"),
            ({"key": "notakey"}, "must be one of"),
            ({"hold_ms": 100}, "missing argument"),
        ]
        for i, (args, needle) in enumerate(cases, start=2):
            r = _call(rpc, i, "key_tap", args)
            assert r["result"]["isError"] is True, args
            text = r["result"]["content"][0]["text"]
            assert "invalid arguments" in text and needle in text, text
        r = _call(rpc, 99, "key_release", {"surprise": 1})
        assert r["result"]["isError"] is True
        r = rpc({"jsonrpc": "2.0", "id": 100, "method": "tools/list"})
        assert "tools" in r["result"]          # server unharmed
    finally:
        proc.kill()
    assert received == []                      # nothing reached a FIFO


def test_tools_call_param_shapes_are_protocol_errors(tmp_path):
    proc, rpc, *_ = _server(tmp_path)
    try:
        rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        r = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "key_tap", "arguments": [1, 2]}})
        assert r["error"]["code"] == -32602
        r = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": 42, "arguments": {}}})
        assert r["error"]["code"] == -32602
        r = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
        assert "tools" in r["result"]
    finally:
        proc.kill()


def test_oversized_and_non_json_constant_lines(tmp_path):
    proc, rpc, *_ = _server(tmp_path)
    try:
        rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        proc.stdin.write("x" * (1024 * 1024 + 10) + "\n")
        proc.stdin.flush()
        r = json.loads(proc.stdout.readline())
        assert r["error"]["code"] == -32600    # request too large
        proc.stdin.write('{"jsonrpc": "2.0", "id": 2, "method": "tools/call",'
                         ' "params": {"name": "key_tap", "arguments":'
                         ' {"key": "x", "hold_ms": NaN}}}\n')
        proc.stdin.flush()
        r = json.loads(proc.stdout.readline())
        assert r["error"]["code"] == -32700    # NaN is not JSON
        r = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        assert "tools" in r["result"]
    finally:
        proc.kill()


def test_short_fifo_write_is_an_error(tmp_path, monkeypatch):
    import importlib.util
    import threading
    spec = importlib.util.spec_from_file_location(
        "elfdeckai_mcp_unit", ROOT / "elfdeckai_mcp.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    fifo = str(tmp_path / "k.fifo")
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDWR)   # a held reader end, so opens succeed
    monkeypatch.setattr(m, "KEY_FIFO", fifo)
    monkeypatch.setattr(m.os, "write", lambda fd, data: 1)   # always short
    try:
        content, is_error = m.call_tool("key_tap", {"key": "x"})
        assert is_error and "I/O error" in content[0]["text"]
    finally:
        os.close(reader)


def test_tools_call_notification_executes_but_is_never_answered(tmp_path):
    proc, rpc, received, key_fifo, *_ = _server(tmp_path)
    try:
        rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": "tools/call",
             "params": {"name": "key_tap", "arguments": {"key": "x"}}}) + "\n")
        proc.stdin.flush()
        r = rpc({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        assert r["id"] == 2 and r["result"] == {}   # the FIRST reply is ping's
    finally:
        proc.kill()
    expected = (os.path.basename(key_fifo), "key x")
    assert expected in _received_lines(received, {expected})   # ...but it executed


import base64 as _b64

import elfdeckai_mcp as _mcp

_PNG_1PX = _b64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def test_screenshot_tool_returns_an_image(tmp_path):
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    png = tmp_path / "fake.png"
    png.write_bytes(_PNG_1PX)
    script = fakebin / "spectacle"
    script.write_text(
        '#!/bin/sh\nwhile [ $# -gt 0 ]; do\n'
        '  if [ "$1" = "-o" ]; then cp "%s" "$2"; fi\n'
        "  shift\ndone\n" % png)
    script.chmod(0o755)
    env = {**os.environ, "PATH": "%s:%s" % (fakebin, os.environ["PATH"]),
           "DBUS_SESSION_BUS_ADDRESS": "/nonexistent"}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "elfdeckai_mcp.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
    try:
        def rpc(msg):
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
            return json.loads(proc.stdout.readline())
        rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"}}})
        names = [t["name"] for t in rpc(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )["result"]["tools"]]
        assert "screenshot" in names and len(names) == 21
        r = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "screenshot", "arguments": {}}})
        item = r["result"]["content"][0]
        assert r["result"]["isError"] is False
        assert item["type"] == "image" and item["mimeType"] == "image/png"
        assert _b64.b64decode(item["data"]) == _PNG_1PX
    finally:
        proc.kill()


def test_screenshot_failure_is_a_clean_error(monkeypatch):
    monkeypatch.setattr(_mcp.shutil, "which", lambda _: None)
    content, is_error = _mcp.call_tool("screenshot", {})
    assert is_error is True
    assert "screenshot failed" in content[0]["text"]


def test_ensure_graphical_env_imports_from_systemd(monkeypatch):
    class R:
        stdout = ("WAYLAND_DISPLAY=wayland-0\nDISPLAY=:0\n"
                  "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus\n"
                  "UNRELATED=nope\n")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(_mcp.subprocess, "run", lambda *a, **k: R())
    _mcp._ensure_graphical_env()
    import os as _os
    assert _os.environ["WAYLAND_DISPLAY"] == "wayland-0"
    assert _os.environ["DISPLAY"] == ":0"
    assert "UNRELATED" not in _os.environ


def test_initialize_negotiates_the_protocol_version():
    for requested, answered in (("2024-11-05", "2024-11-05"),
                                ("2025-03-26", "2025-03-26"),
                                ("2099-01-01", _mcp.PROTOCOL_VERSIONS[0])):
        r = _mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": requested}})
        assert r["result"]["protocolVersion"] == answered


def test_versions_agree():
    """server.json and the package version must not drift apart."""
    with open(ROOT / "server.json") as f:
        server = json.load(f)
    assert server["version"] == _mcp.__version__
    assert [p["version"] for p in server["packages"]] == [_mcp.__version__]
    r = _mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r["result"]["serverInfo"]["version"] == _mcp.__version__


def test_readme_carries_the_registry_ownership_marker():
    """The MCP Registry verifies PyPI ownership by finding this string in
    the package README."""
    with open(ROOT / "server.json") as f:
        name = json.load(f)["name"]
    assert "<!-- mcp-name: %s -->" % name in (ROOT / "README.md").read_text()


def test_kwin_fallback_returns_the_image_and_removes_its_file(tmp_path, monkeypatch):
    shot = tmp_path / "kwin-shot.png"
    shot.write_bytes(_PNG_1PX)

    class R:
        returncode = 0
        stdout = 'method return\n   string "%s"\n' % shot
        stderr = ""

    monkeypatch.setattr(_mcp.shutil, "which",
                        lambda cmd: "/usr/bin/dbus-send" if cmd == "dbus-send" else None)
    monkeypatch.setattr(_mcp.subprocess, "run", lambda *a, **k: R())
    monkeypatch.setattr(_mcp, "_ensure_graphical_env", lambda: None)
    assert _mcp._capture_screen() == _PNG_1PX
    assert not shot.exists()



# --- screenshot scaling ------------------------------------------------------

import random
import zlib as _zlib


def _decode_up_png(data):
    """Test-side decoder for the PNGs _encode_png writes (RGB, Up filter)."""
    w, h = _mcp._png_size(data)
    pos, idat = 8, b""
    while pos < len(data):
        n = int.from_bytes(data[pos:pos + 4], "big")
        if data[pos + 4:pos + 8] == b"IDAT":
            idat += data[pos + 8:pos + 8 + n]
        pos += 12 + n
    raw, stride, prev, out = _zlib.decompress(idat), w * 3, bytes(w * 3), b""
    for r in range(h):
        line = raw[r * (stride + 1):(r + 1) * (stride + 1)]
        assert line[0] == 2
        prev = bytes((a + b) & 0xFF for a, b in zip(line[1:], prev))
        out += prev
    return w, h, out


def _ppm(w, h, rgb, comment=b""):
    return b"P6\n" + comment + b"%d %d\n255\n" % (w, h) + rgb


def test_bytewise_average_and_subtract_match_per_byte_arithmetic():
    rnd = random.Random(1)
    a = bytes(rnd.randrange(256) for _ in range(5000))
    b = bytes(rnd.randrange(256) for _ in range(5000))
    assert _mcp._average(a, b) == bytes((x + y) // 2 for x, y in zip(a, b))
    assert _mcp._subtract(a, b) == bytes((x - y) & 0xFF for x, y in zip(a, b))


def test_png_encoding_round_trips():
    rnd = random.Random(2)
    rgb = bytes(rnd.randrange(256) for _ in range(7 * 5 * 3))
    assert _decode_up_png(_mcp._encode_png(7, 5, rgb)) == (7, 5, rgb)


def test_ppm_parsing_with_a_comment():
    rgb = bytes(range(12))
    assert _mcp._parse_ppm(_ppm(2, 2, rgb, b"# made by a test\n")) == (2, 2, rgb)


def test_16_bit_ppm_is_reduced_to_8_bit():
    rgb16 = bytes([0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC])
    assert _mcp._parse_ppm(b"P6\n1 1\n65535\n" + rgb16) == (1, 1, b"\x12\x56\x9a")


def test_scaling_keeps_aspect_and_averages_pixels():
    # a 4x2 image: left half black, right half white -> 2x1 black, white
    rgb = (b"\x00" * 6 + b"\xff" * 6) * 2
    assert _mcp._scale_rgb(4, 2, rgb, 2) == (2, 1, b"\x00\x00\x00\xff\xff\xff")
    w, h, out = _mcp._scale_rgb(2560, 1600, b"\x40\x80\xc0" * (2560 * 1600), 1280)
    assert (w, h) == (1280, 800) and out == b"\x40\x80\xc0" * (1280 * 800)
    w, h, _ = _mcp._scale_rgb(1920, 1080, bytes(1920 * 1080 * 3), 1280)
    assert (w, h) == (1280, 720)


def test_screenshot_is_scaled_and_reports_its_size(monkeypatch):
    big = _ppm(2560, 1600, b"\x10\x20\x30" * (2560 * 1600))
    monkeypatch.setattr(_mcp, "_capture_screen", lambda: big)
    monkeypatch.setattr(_mcp, "SCREENSHOT_MAX", 1280)
    content, is_error = _mcp.call_tool("screenshot", {})
    assert not is_error
    w, h, rgb = _decode_up_png(_b64.b64decode(content[0]["data"]))
    assert (w, h) == (1280, 800) and rgb[:3] == b"\x10\x20\x30"
    assert "1280x800" in content[1]["text"] and "2560x1600" in content[1]["text"]
    assert _mcp._frame == (1280, 800)


def test_capture_falls_back_to_png_when_a_tool_cannot_write_ppm(tmp_path, monkeypatch):
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    png = tmp_path / "fake.png"
    png.write_bytes(_PNG_1PX)
    calls = tmp_path / "calls"
    script = fakebin / "spectacle"
    script.write_text(
        '#!/bin/sh\nfor a; do out="$a"; done\necho "$out" >> "%s"\n'
        'case "$out" in *.ppm) exit 1;; esac\ncp "%s" "$out"\n' % (calls, png))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", "%s:/usr/bin:/bin" % fakebin)
    monkeypatch.setattr(_mcp, "_ensure_graphical_env", lambda: None)
    monkeypatch.setattr(_mcp, "_no_ppm", set())
    assert _mcp._capture_screen() == _PNG_1PX
    assert _mcp._capture_screen() == _PNG_1PX
    tried = [os.path.splitext(l)[1] for l in calls.read_text().split()]
    assert tried == [".ppm", ".png", ".png"]      # PPM is not retried


# --- absolute pointer ----------------------------------------------------------

def _mouse_reader(tmp_path, monkeypatch):
    fifo = str(tmp_path / "mouse.fifo")
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    monkeypatch.setattr(_mcp, "MOUSE_FIFO", fifo)

    def lines():
        try:
            return os.read(reader, 4096).decode().splitlines()
        except BlockingIOError:
            return []
    return reader, lines


def test_click_at_moves_then_clicks_in_screenshot_pixels(tmp_path, monkeypatch):
    reader, lines = _mouse_reader(tmp_path, monkeypatch)
    monkeypatch.setattr(_mcp, "_frame", (1280, 800))
    try:
        content, is_error = _mcp.call_tool("mouse_click_at",
                                           {"x": 640, "y": 400, "button": "right"})
        assert not is_error, content
        assert lines() == ["moveto 640 400 1280 800", "click right"]
        content, is_error = _mcp.call_tool("mouse_move_to", {"x": 0, "y": 799})
        assert not is_error and lines() == ["moveto 0 799 1280 800"]
    finally:
        os.close(reader)


def test_pointer_outside_the_last_screenshot_is_refused(tmp_path, monkeypatch):
    reader, lines = _mouse_reader(tmp_path, monkeypatch)
    monkeypatch.setattr(_mcp, "_frame", (1280, 800))
    try:
        content, is_error = _mcp.call_tool("mouse_click_at", {"x": 1280, "y": 0})
        assert is_error and "1280x800" in content[0]["text"]
        assert lines() == []
    finally:
        os.close(reader)


def test_pointer_without_a_screenshot_takes_one_for_the_size(tmp_path, monkeypatch):
    reader, lines = _mouse_reader(tmp_path, monkeypatch)
    monkeypatch.setattr(_mcp, "_frame", None)
    monkeypatch.setattr(_mcp, "_screenshot", lambda: (b"", 800, 600, 800, 600))
    try:
        content, is_error = _mcp.call_tool("mouse_move_to", {"x": 10, "y": 20})
        assert not is_error and lines() == ["moveto 10 20 800 600"]
    finally:
        os.close(reader)
