"""The MCP server: protocol handshake, tool listing, and tool calls that
land on (fake) daemon FIFOs. Runs the real server as a subprocess and
speaks raw JSON-RPC at it, exactly like an MCP client would."""
import json
import os
import subprocess
import sys
import threading

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


def test_handshake_and_tool_list(tmp_path):
    proc, rpc, *_ = _server(tmp_path)
    try:
        r = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert r["result"]["serverInfo"]["name"] == "elfdeckai"
        assert r["result"]["protocolVersion"]
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
                         "mouse_release"}
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
    got = {(os.path.basename(p), line) for p, line in received}
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
        assert "refused" in r["result"]["content"][0]["text"]
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
