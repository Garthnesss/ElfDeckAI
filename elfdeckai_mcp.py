#!/usr/bin/env python3
# Copyright (c) 2026 Garth Elfstrom — MIT License (see LICENSE).
"""ElfdeckAI as MCP tools: a standard-library-only Model Context Protocol
server (stdio, JSON-RPC) that exposes ElfKey and ElfPad to any MCP client —
Claude, Hermes, your own agent — as native tool calls:

    key_tap    {key, hold_ms?}          pad_press   {button, hold_ms?}
    key_down   {key}                    pad_down    {button}
    key_up     {key}                    pad_up      {button}
    mouse_move  {dx, dy, glide_ms?}     pad_stick   {side, x, y, hold_ms?}
    mouse_click {button, hold_ms?}      pad_trigger {side, percent, hold_ms?}
    mouse_down  {button}                pad_center  {}
    mouse_up    {button}                pad_reset   {}
    mouse_scroll  {clicks}              key_release {}   (unstick everything)
    mouse_hscroll {clicks}              mouse_release {} (unstick everything)
    screenshot  {}                      (a PNG of the screen — the eyes)

The screenshot tool captures the screen of the machine this server runs
on (KDE/KWin on a Steam Deck in Desktop Mode, Spectacle, grim, or X11
tools — whichever exists) and returns it as an MCP image, so an agent
needs no video stream to play: look, press, look again.

The server holds no state and injects nothing itself: every call is
validated by the daemons' own parse_command() (the exact contract the
FIFOs enforce) and then written as one line to the daemon's FIFO. If a
daemon isn't running, the call fails cleanly with "daemon not running"
instead of blocking — opening a FIFO for write with no reader would
otherwise hang the agent.

Run the daemons first (elfkey.py, elfpad.py, elfmouse.py), then point your MCP client
at this file, e.g.:

    {"mcpServers": {"elfdeckai": {
        "command": "python3", "args": ["/path/to/elfdeckai_mcp.py"]}}}

FIFO paths default to the daemons' own constants and can be overridden
with ELFKEY_FIFO / ELFPAD_FIFO environment variables (used by the tests).
Standard library only, like everything else in this repo.
"""
import base64
import errno
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import elfkey
import elfmouse
import elfpad

KEY_FIFO = os.environ.get("ELFKEY_FIFO", elfkey.FIFO)
PAD_FIFO = os.environ.get("ELFPAD_FIFO", elfpad.FIFO)
MOUSE_FIFO = os.environ.get("ELFMOUSE_FIFO", elfmouse.FIFO)
PROTOCOL_VERSION = "2024-11-05"


def _send(fifo, line, parser):
    """Validate `line` with the daemon's parser, then hand it to the FIFO.
    Returns a human-readable confirmation or raises ValueError/OSError."""
    parser(line)  # raises ValueError on anything the daemon would refuse
    payload = (line + "\n").encode()
    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    try:
        if os.write(fd, payload) != len(payload):
            raise OSError(errno.EIO, "short FIFO write")
    finally:
        os.close(fd)
    return "sent: %s" % line


def _key_line(cmd, a):
    if cmd == "key_release":
        return "release"
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


def _mouse_line(cmd, a):
    if cmd == "mouse_release":
        return "release"
    if cmd == "mouse_move":
        ms = a.get("glide_ms")
        return "move %s %s%s" % (a["dx"], a["dy"],
                                 "" if ms is None else " %s" % ms)
    if cmd == "mouse_click":
        ms = a.get("hold_ms")
        return "click %s%s" % (a["button"], "" if ms is None else " %s" % ms)
    if cmd in ("mouse_down", "mouse_up"):
        return "%s %s" % (cmd.replace("mouse_", ""), a["button"])
    if cmd == "mouse_scroll":
        return "scroll %s" % a["clicks"]
    return "hscroll %s" % a["clicks"]  # mouse_hscroll


TOOLS = [
    ("key_tap", "Tap a key on the virtual keyboard.",
     {"key": {"type": "string", "enum": sorted(elfkey.KEYS)},
      "hold_ms": {"type": "integer", "minimum": 0, "maximum": elfkey.MAX_HOLD_MS}},
     ["key"]),
    ("key_down", "Hold a key down (release with key_up, or key_release for all).",
     {"key": {"type": "string", "enum": sorted(elfkey.KEYS)}}, ["key"]),
    ("key_up", "Release a held key.",
     {"key": {"type": "string", "enum": sorted(elfkey.KEYS)}}, ["key"]),
    ("key_release", "Release every held key at once — the unstick button, for "
                    "when a caller died mid-hold or state is unknown.",
     {}, []),
    ("pad_press", "Tap a gamepad button or D-pad direction.",
     {"button": {"type": "string",
                 "enum": sorted(set(elfpad.BUTTONS) | set(elfpad.DPAD) | set(elfpad.TRIGGERS))},
      "hold_ms": {"type": "integer", "minimum": 0, "maximum": elfpad.MAX_HOLD_MS}},
     ["button"]),
    ("pad_down", "Hold a gamepad button or D-pad direction down.",
     {"button": {"type": "string",
                 "enum": sorted(set(elfpad.BUTTONS) | set(elfpad.DPAD) | set(elfpad.TRIGGERS))}}, ["button"]),
    ("pad_up", "Release a held gamepad button or D-pad direction.",
     {"button": {"type": "string",
                 "enum": sorted(set(elfpad.BUTTONS) | set(elfpad.DPAD) | set(elfpad.TRIGGERS))}}, ["button"]),
    ("pad_stick", "Move an analog stick (x/y in percent, -100..100). "
                  "Without hold_ms the stick stays; with it, it recenters after.",
     {"side": {"type": "string", "enum": ["left", "right"]},
      "x": {"type": "integer", "minimum": -100, "maximum": 100},
      "y": {"type": "integer", "minimum": -100, "maximum": 100},
      "hold_ms": {"type": "integer", "minimum": 0, "maximum": elfpad.MAX_HOLD_MS}},
     ["side", "x", "y"]),
    ("pad_trigger", "Set an analog trigger (percent 0..100). "
                    "Without hold_ms it stays; with it, it releases after.",
     {"side": {"type": "string", "enum": ["left", "right"]},
      "percent": {"type": "integer", "minimum": 0, "maximum": 100},
      "hold_ms": {"type": "integer", "minimum": 0, "maximum": elfpad.MAX_HOLD_MS}},
     ["side", "percent"]),
    ("pad_center", "Return sticks, triggers and D-pad to neutral.", {}, []),
    ("pad_reset", "Release every button and recenter everything.", {}, []),
    ("mouse_move", "Move the pointer by (dx, dy) pixels. Optional glide_ms "
                   "spreads the move over time (smooth drags, human-ish motion).",
     {"dx": {"type": "integer", "minimum": -elfmouse.MAX_MOVE, "maximum": elfmouse.MAX_MOVE},
      "dy": {"type": "integer", "minimum": -elfmouse.MAX_MOVE, "maximum": elfmouse.MAX_MOVE},
      "glide_ms": {"type": "integer", "minimum": 0, "maximum": elfmouse.MAX_HOLD_MS}},
     ["dx", "dy"]),
    ("mouse_click", "Click a mouse button.",
     {"button": {"type": "string", "enum": ["left", "right", "middle"]},
      "hold_ms": {"type": "integer", "minimum": 0, "maximum": elfmouse.MAX_HOLD_MS}},
     ["button"]),
    ("mouse_down", "Hold a mouse button down (start a drag).",
     {"button": {"type": "string", "enum": ["left", "right", "middle"]}}, ["button"]),
    ("mouse_up", "Release a mouse button (drop the drag).",
     {"button": {"type": "string", "enum": ["left", "right", "middle"]}}, ["button"]),
    ("mouse_scroll", "Scroll the wheel: positive clicks up, negative down.",
     {"clicks": {"type": "integer", "minimum": -elfmouse.MAX_SCROLL,
                 "maximum": elfmouse.MAX_SCROLL}}, ["clicks"]),
    ("mouse_hscroll", "Scroll horizontally: positive clicks right.",
     {"clicks": {"type": "integer", "minimum": -elfmouse.MAX_SCROLL,
                 "maximum": elfmouse.MAX_SCROLL}}, ["clicks"]),
    ("mouse_release", "Release every held mouse button at once — the unstick "
                      "button, for when a caller died mid-drag.",
     {}, []),
    ("screenshot", "Capture the screen of the machine this server runs on "
                   "(e.g. a Steam Deck in Desktop Mode) and return it as a "
                   "PNG image — the eyes to match the hands. Look, press, "
                   "look again; no video stream needed. Requires a "
                   "graphical session on the server's machine.",
     {}, []),
]
TOOL_NAMES = {t[0] for t in TOOLS}
TOOL_SCHEMAS = {name: (props, required) for name, _d, props, required in TOOLS}


def _validate_args(name, args):
    """Enforce the advertised schema at runtime: arguments must be an
    object with exactly the known fields, correctly typed (a JSON true is
    not an integer, whatever Python thinks), in range, and in the enum.
    The daemon parser remains the final authority; this layer exists so a
    bad call fails HERE, with a precise reason, before any FIFO write."""
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    props, required = TOOL_SCHEMAS[name]
    unknown = sorted(set(args) - set(props))
    if unknown:
        raise ValueError("unknown argument(s): %s" % ", ".join(unknown))
    missing = [k for k in required if k not in args]
    if missing:
        raise ValueError("missing argument(s): %s" % ", ".join(missing))
    for key, value in args.items():
        schema = props[key]
        if schema.get("type") == "integer":
            if type(value) is not int:  # bool is an int subclass; exclude it
                raise ValueError("%s must be an integer" % key)
            if "minimum" in schema and value < schema["minimum"]:
                raise ValueError("%s must be >= %d" % (key, schema["minimum"]))
            if "maximum" in schema and value > schema["maximum"]:
                raise ValueError("%s must be <= %d" % (key, schema["maximum"]))
        elif schema.get("type") == "string":
            if not isinstance(value, str):
                raise ValueError("%s must be a string" % key)
        if "enum" in schema and value not in schema["enum"]:
            raise ValueError("%s must be one of: %s"
                             % (key, ", ".join(map(str, schema["enum"]))))


def tools_list():
    return {"tools": [
        {"name": name, "description": desc,
         "inputSchema": {"type": "object", "properties": props,
                         "required": required, "additionalProperties": False}}
        for name, desc, props, required in TOOLS]}


def _screenshot_png():
    """PNG bytes of the screen of the machine this server runs on.

    Tries, in order: Spectacle (KDE — a Steam Deck in Desktop Mode), grim
    (wlroots), the X11 tools scrot and ImageMagick import, then KWin's
    D-Bus screenshot as a fallback (bounded to ~8 s: on some Plasma
    versions it waits on an interactive authorization prompt). Raises
    RuntimeError listing every failure when no method works, so the
    caller learns WHY instead of getting a black box.
    """
    errors = []
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        for label, cmd in (("spectacle", ["spectacle", "-b", "-n", "-o", tmp]),
                           ("grim", ["grim", tmp]),
                           ("scrot", ["scrot", "-o", tmp]),
                           ("import", ["import", "-window", "root", tmp])):
            if not shutil.which(cmd[0]):
                continue
            try:
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=20)
                if r.returncode == 0 and os.path.getsize(tmp) > 0:
                    with open(tmp, "rb") as f:
                        return f.read()
                errors.append("%s: %s" % (
                    label, (r.stderr or "").strip() or
                    "exit %d" % r.returncode))
            except Exception as e:
                errors.append("%s: %s" % (label, e))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if shutil.which("dbus-send"):
        try:
            out = subprocess.run(
                ["dbus-send", "--session", "--print-reply",
                 "--reply-timeout=8000",
                 "--dest=org.kde.KWin", "/Screenshot",
                 "org.kde.KWin.Screenshot.screenshotFullscreen"],
                capture_output=True, text=True, timeout=12)
            m = re.search(r'string "([^"]+\.png)"', out.stdout or "")
            if m and os.path.getsize(m.group(1)) > 0:
                with open(m.group(1), "rb") as f:
                    return f.read()
            errors.append("KWin D-Bus: %s" % (
                (out.stderr or "").strip() or "no screenshot path returned"))
        except Exception as e:
            errors.append("KWin D-Bus: %s" % e)
    raise RuntimeError(
        "screenshot failed — no working capture method here (the server "
        "needs a graphical session on its own machine). Tried: %s"
        % ("; ".join(errors) or "no capture tools installed"))


def _text(s):
    return [{"type": "text", "text": s}]


def call_tool(name, args):
    """Returns (content_items, is_error)."""
    if name not in TOOL_NAMES:
        return _text("unknown tool: %s" % name), True
    try:
        _validate_args(name, args)
    except ValueError as e:
        return _text("invalid arguments: %s" % e), True
    if name == "screenshot":
        try:
            data = _screenshot_png()
        except RuntimeError as e:
            return _text(str(e)), True
        return [{"type": "image",
                 "data": base64.b64encode(data).decode(),
                 "mimeType": "image/png"}], False
    try:
        if name.startswith("key_"):
            return _text(_send(KEY_FIFO, _key_line(name, args),
                               elfkey.parse_command)), False
        if name.startswith("mouse_"):
            return _text(_send(MOUSE_FIFO, _mouse_line(name, args),
                               elfmouse.parse_command)), False
        return _text(_send(PAD_FIFO, _pad_line(name, args),
                           elfpad.parse_command)), False
    except ValueError as e:
        return _text("refused: %s" % e), True
    except KeyError as e:
        return _text("missing argument: %s" % e), True
    except OSError as e:
        if e.errno in (errno.ENXIO, errno.ENOENT):
            return _text("daemon not running (no reader on the FIFO)"), True
        if e.errno == errno.EAGAIN:
            return _text("daemon busy (FIFO full) — retry shortly"), True
        if e.errno in (errno.EACCES, errno.EPERM):
            return _text("permission denied on the FIFO — the daemons "
                         "create it owner-only (0600); run this server as "
                         "the same user"), True
        if e.errno == errno.EPIPE:
            return _text("daemon disconnected (broken pipe)"), True
        return _text("FIFO I/O error: %s" % (e.strerror or e)), True


def handle(msg):
    """One JSON-RPC message -> response dict, or None for notifications."""
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {
            "code": -32600, "message": "invalid request: not a JSON object"}}
    method = msg.get("method")
    msg_id = msg.get("id")
    notification = "id" not in msg  # JSON-RPC: notifications get NO response,
    if notification and method != "tools/call":  # not even errors or results
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "elfdeckai", "version": "1.0.5"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": tools_list()}
    if method == "tools/call":
        params = msg.get("params") or {}
        if not isinstance(params, dict) or not isinstance(params.get("name"), str) \
                or ("arguments" in params and not isinstance(params["arguments"], dict)):
            return {"jsonrpc": "2.0", "id": msg_id, "error": {
                "code": -32602, "message": "invalid params: tools/call needs a "
                "string name and an object arguments"}}
        content, is_error = call_tool(params.get("name"),
                                      params.get("arguments") or {})
        if notification:
            return None  # the call executed; a notification is never answered
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "content": content, "isError": is_error}}
    if msg_id is None:
        return None  # notification (e.g. notifications/initialized)
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": "method not found: %s" % method}}


def _reject_constant(value):
    raise ValueError("invalid JSON constant: %s" % value)


MAX_REQUEST_BYTES = 1024 * 1024  # one request line; plenty for a tool call


def main():
    stream = sys.stdin.buffer
    while True:
        raw = stream.readline(MAX_REQUEST_BYTES + 1)
        if not raw:
            break
        if len(raw) > MAX_REQUEST_BYTES:
            while raw and not raw.endswith(b"\n"):
                raw = stream.readline(MAX_REQUEST_BYTES + 1)
            _emit({"jsonrpc": "2.0", "id": None, "error": {
                "code": -32600, "message": "request too large"}})
            continue
        try:
            line = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            _emit({"jsonrpc": "2.0", "id": None, "error": {
                "code": -32700, "message": "parse error"}})
            continue
        if not line:
            continue
        try:
            msg = json.loads(line, parse_constant=_reject_constant)
        except ValueError:
            _emit({"jsonrpc": "2.0", "id": None, "error": {
                "code": -32700, "message": "parse error"}})
            continue
        try:
            response = handle(msg)
        except Exception:
            response = {"jsonrpc": "2.0",
                        "id": msg.get("id") if isinstance(msg, dict) else None,
                        "error": {"code": -32603, "message": "internal error"}}
        if response is not None:
            _emit(response)


def _emit(response):
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
