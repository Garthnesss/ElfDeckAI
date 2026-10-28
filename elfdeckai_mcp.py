#!/usr/bin/env python3
"""ElfdeckAI as MCP tools: a standard-library-only Model Context Protocol
server (stdio, JSON-RPC) that exposes ElfKey and ElfPad to any MCP client —
Claude, Hermes, your own agent — as native tool calls:

    key_tap    {key, hold_ms?}          pad_press   {button, hold_ms?}
    key_down   {key}                    pad_down    {button}
    key_up     {key}                    pad_up      {button}
                                        pad_stick   {side, x, y, hold_ms?}
                                        pad_trigger {side, percent, hold_ms?}
                                        pad_center  {}
                                        pad_reset   {}

The server holds no state and injects nothing itself: every call is
validated by the daemons' own parse_command() (the exact contract the
FIFOs enforce) and then written as one line to the daemon's FIFO. If a
daemon isn't running, the call fails cleanly with "daemon not running"
instead of blocking — opening a FIFO for write with no reader would
otherwise hang the agent.

Run the daemons first (elfkey.py / elfpad.py), then point your MCP client
at this file, e.g.:

    {"mcpServers": {"elfdeckai": {
        "command": "python3", "args": ["/path/to/elfdeckai_mcp.py"]}}}

FIFO paths default to the daemons' own constants and can be overridden
with ELFKEY_FIFO / ELFPAD_FIFO environment variables (used by the tests).
Standard library only, like everything else in this repo.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import elfkey
import elfpad

KEY_FIFO = os.environ.get("ELFKEY_FIFO", elfkey.FIFO)
PAD_FIFO = os.environ.get("ELFPAD_FIFO", elfpad.FIFO)
PROTOCOL_VERSION = "2024-11-05"


def _send(fifo, line, parser):
    """Validate `line` with the daemon's parser, then hand it to the FIFO.
    Returns a human-readable confirmation or raises ValueError/OSError."""
    parser(line)  # raises ValueError on anything the daemon would refuse
    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)
    return "sent: %s" % line


def _key_line(cmd, a):
    if cmd == "key_tap":
        ms = a.get("hold_ms")
        return "key %s%s" % (a["key"], "" if ms is None else " %s" % ms)
    return "%s %s" % (cmd.replace("key_", ""), a["key"])  # key_down / key_up


def _pad_line(cmd, a):
    if cmd == "pad_press":
        ms = a.get("hold_ms")
        return "press %s%s" % (a["button"], "" if ms is None else " %s" % ms)
    if cmd in ("pad_down", "pad_up"):
        return "%s %s" % (cmd.replace("pad_", ""), a["button"])
    if cmd == "pad_stick":
        ms = a.get("hold_ms")
        return "stick %s %s %s%s" % (a["side"], a["x"], a["y"],
                                     "" if ms is None else " %s" % ms)
    if cmd == "pad_trigger":
        ms = a.get("hold_ms")
        return "trigger %s %s%s" % (a["side"], a["percent"],
                                    "" if ms is None else " %s" % ms)
    return cmd.replace("pad_", "")  # pad_center / pad_reset


TOOLS = [
    ("key_tap", "Tap a key on the virtual keyboard.",
     {"key": {"type": "string"}, "hold_ms": {"type": "integer"}}, ["key"]),
    ("key_down", "Hold a key down (release with key_up).",
     {"key": {"type": "string"}}, ["key"]),
    ("key_up", "Release a held key.",
     {"key": {"type": "string"}}, ["key"]),
    ("pad_press", "Tap a gamepad button or D-pad direction.",
     {"button": {"type": "string"}, "hold_ms": {"type": "integer"}}, ["button"]),
    ("pad_down", "Hold a gamepad button or D-pad direction down.",
     {"button": {"type": "string"}}, ["button"]),
    ("pad_up", "Release a held gamepad button or D-pad direction.",
     {"button": {"type": "string"}}, ["button"]),
    ("pad_stick", "Move an analog stick (x/y in percent, -100..100). "
                  "Without hold_ms the stick stays; with it, it recenters after.",
     {"side": {"type": "string", "enum": ["left", "right"]},
      "x": {"type": "integer"}, "y": {"type": "integer"},
      "hold_ms": {"type": "integer"}}, ["side", "x", "y"]),
    ("pad_trigger", "Set an analog trigger (percent 0..100). "
                    "Without hold_ms it stays; with it, it releases after.",
     {"side": {"type": "string", "enum": ["left", "right"]},
      "percent": {"type": "integer"}, "hold_ms": {"type": "integer"}},
     ["side", "percent"]),
    ("pad_center", "Return sticks, triggers and D-pad to neutral.", {}, []),
    ("pad_reset", "Release every button and recenter everything.", {}, []),
]
TOOL_NAMES = {t[0] for t in TOOLS}


def tools_list():
    return {"tools": [
        {"name": name, "description": desc,
         "inputSchema": {"type": "object", "properties": props,
                         "required": required}}
        for name, desc, props, required in TOOLS]}


def call_tool(name, args):
    """Returns (text, is_error)."""
    if name not in TOOL_NAMES:
        return "unknown tool: %s" % name, True
    try:
        if name.startswith("key_"):
            return _send(KEY_FIFO, _key_line(name, args), elfkey.parse_command), False
        return _send(PAD_FIFO, _pad_line(name, args), elfpad.parse_command), False
    except ValueError as e:
        return "refused: %s" % e, True
    except KeyError as e:
        return "missing argument: %s" % e, True
    except OSError:
        return "daemon not running (no reader on the FIFO)", True


def handle(msg):
    """One JSON-RPC message -> response dict, or None for notifications."""
    method = msg.get("method")
    msg_id = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "elfdeckai", "version": "1.0.0"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": tools_list()}
    if method == "tools/call":
        params = msg.get("params") or {}
        text, is_error = call_tool(params.get("name"), params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "content": [{"type": "text", "text": text}], "isError": is_error}}
    if msg_id is None:
        return None  # notification (e.g. notifications/initialized)
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": "method not found: %s" % method}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        response = handle(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
