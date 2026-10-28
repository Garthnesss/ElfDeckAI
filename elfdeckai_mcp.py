#!/usr/bin/env python3
# Copyright (c) 2026 Garth Elfstrom — MIT License (see LICENSE).
"""ElfDeckAI MCP server: a standard-library-only Model Context Protocol
server (stdio, JSON-RPC) that exposes the ElfKey, ElfPad and ElfMouse
daemons, plus a screenshot tool, to any MCP client:

    key_tap    {key, hold_ms?}          pad_press   {button, hold_ms?}
    key_down   {key}                    pad_down    {button}
    key_up     {key}                    pad_up      {button}
    mouse_move  {dx, dy, glide_ms?}     pad_stick   {side, x, y, hold_ms?}
    mouse_click {button, hold_ms?}      pad_trigger {side, percent, hold_ms?}
    mouse_down  {button}                pad_center  {}
    mouse_up    {button}                pad_reset   {}
    mouse_scroll  {clicks}              key_release {}   (release all keys)
    mouse_hscroll {clicks}              mouse_release {} (release all buttons)
    mouse_move_to  {x, y}               mouse_click_at {x, y, button?, hold_ms?}
    screenshot  {}                      (PNG of the local screen)

mouse_move_to and mouse_click_at take coordinates in the pixels of the most
recent screenshot; ElfMouse's absolute device maps them across the screen.

The screenshot tool captures the screen of the machine this server runs on
(Spectacle/KWin on KDE Plasma, grim on wlroots, or scrot/ImageMagick on
X11, whichever is available) and returns it as an MCP image, scaled down
to SCREENSHOT_MAX pixels on its long side.

The server holds no state and injects nothing itself: every call is
validated with the daemon's own parse_command() and then written as one
line to the daemon's FIFO. If a daemon is not running the call fails with
"daemon not running" instead of blocking.

Start the daemons first (elfkey, elfpad, elfmouse), then point your MCP
client at this server:

    {"mcpServers": {"elfdeckai": {"command": "elfdeckai-mcp"}}}

FIFO paths default to the daemons' own constants and can be overridden
with the ELFKEY_FIFO, ELFPAD_FIFO and ELFMOUSE_FIFO environment variables.
"""
import base64
import errno
import json
import operator
import os
import re
import shutil
import struct
import subprocess
import sys
import stat
import tempfile
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import elfkey
import elfmouse
import elfpad

KEY_FIFO = os.environ.get("ELFKEY_FIFO", elfkey.FIFO)
PAD_FIFO = os.environ.get("ELFPAD_FIFO", elfpad.FIFO)
MOUSE_FIFO = os.environ.get("ELFMOUSE_FIFO", elfmouse.FIFO)
__version__ = "1.1.0"
# Newest first. The server answers with the client's requested version when
# it supports it, otherwise with its newest (MCP lifecycle negotiation).
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


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
    ("mouse_move", "Move the pointer relatively by (dx, dy). Pointer "
                   "acceleration applies, so prefer mouse_move_to for "
                   "targeting. Optional glide_ms spreads the move over time.",
     {"dx": {"type": "integer", "minimum": -elfmouse.MAX_MOVE, "maximum": elfmouse.MAX_MOVE},
      "dy": {"type": "integer", "minimum": -elfmouse.MAX_MOVE, "maximum": elfmouse.MAX_MOVE},
      "glide_ms": {"type": "integer", "minimum": 0, "maximum": elfmouse.MAX_HOLD_MS}},
     ["dx", "dy"]),
    ("mouse_move_to", "Move the pointer to (x, y), in the pixel coordinates of "
                      "the most recent screenshot (0,0 is its top-left corner). "
                      "Takes a screenshot first if none has been taken yet.",
     {"x": {"type": "integer", "minimum": 0, "maximum": elfmouse.MAX_FRAME - 1},
      "y": {"type": "integer", "minimum": 0, "maximum": elfmouse.MAX_FRAME - 1}},
     ["x", "y"]),
    ("mouse_click_at", "Move the pointer to (x, y) in the pixel coordinates of "
                       "the most recent screenshot, then click (left by default).",
     {"x": {"type": "integer", "minimum": 0, "maximum": elfmouse.MAX_FRAME - 1},
      "y": {"type": "integer", "minimum": 0, "maximum": elfmouse.MAX_FRAME - 1},
      "button": {"type": "string", "enum": ["left", "right", "middle"]},
      "hold_ms": {"type": "integer", "minimum": 0, "maximum": elfmouse.MAX_HOLD_MS}},
     ["x", "y"]),
    ("mouse_click", "Click a mouse button at the pointer's current position.",
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
                   "PNG image, scaled down if the screen is large. "
                   "mouse_move_to and mouse_click_at take coordinates in this "
                   "image's pixels. Requires a graphical session on the "
                   "server's machine.",
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


_SESSION_KEYS = ("WAYLAND_DISPLAY", "DISPLAY", "GAMESCOPE_WAYLAND_DISPLAY",
                 "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")
# Session variables this process was started with; those are left alone.
_INHERITED = frozenset(k for k in _SESSION_KEYS if os.environ.get(k))


def _ensure_graphical_env():
    """Fill in the graphical-session variables an SSH-launched server lacks.

    A server started from an MCP client over SSH inherits no WAYLAND_DISPLAY
    or DISPLAY, and capture tools abort without them. The systemd user
    manager knows the current values, so they are re-read from
    `systemctl --user show-environment` before every capture: switching the
    Deck between Desktop Mode and Gaming Mode changes them while this
    server keeps running. Variables the server was started with are kept.
    """
    if _INHERITED.issuperset(_SESSION_KEYS):
        return
    try:
        out = subprocess.run(["systemctl", "--user", "show-environment"],
                             capture_output=True, text=True, timeout=5)
    except Exception:
        return
    if out.returncode != 0:
        return
    current = {}
    for line in (out.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep and value and key in _SESSION_KEYS:
            current[key] = value
    for key in _SESSION_KEYS:
        if key in _INHERITED:
            continue
        if key in current:
            os.environ[key] = current[key]
        else:
            os.environ.pop(key, None)   # gone since the last mode switch


def _gamescope_display():
    """Name of gamescope's Wayland socket (Steam Deck Gaming Mode), or None
    when gamescope is not running."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    named = os.environ.get("GAMESCOPE_WAYLAND_DISPLAY")
    if named:
        candidates = [named]
    else:
        try:
            candidates = sorted(n for n in os.listdir(runtime)
                                if re.match(r"gamescope-\d+$", n))
        except OSError:
            return None
    for name in candidates:
        try:
            if stat.S_ISSOCK(os.stat(os.path.join(runtime, name)).st_mode):
                return name
        except OSError:
            continue
    return None


def _capture_gamescope(path, display, timeout=3.0):
    """Ask gamescope for a screenshot of exactly what is on screen (capture
    type 4, "screen buffer") and wait for the file: gamescope writes it
    asynchronously after the command returns."""
    env = dict(os.environ, GAMESCOPE_WAYLAND_DISPLAY=display)
    r = subprocess.run(["gamescopectl", "screenshot", path, "4"],
                       capture_output=True, text=True, timeout=10, env=env)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "").strip()
                           or "exit %d" % r.returncode)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with open(path, "rb") as f:
                data = f.read()
            if data.endswith(_PNG_END):   # complete, not mid-write
                return data
        except OSError:
            pass
        time.sleep(0.05)
    raise RuntimeError("no screenshot written within %.0f s" % timeout)


# Screenshots wider or taller than this are scaled down before they are
# sent: smaller images are faster to transfer and cheaper for the model, and
# clients that shrink large images themselves would otherwise leave the model
# guessing at coordinates. 0 disables scaling.
try:
    SCREENSHOT_MAX = max(0, int(os.environ.get("ELFDECKAI_SCREENSHOT_MAX", "1280")))
except ValueError:
    SCREENSHOT_MAX = 1280

# Size of the last image the client was sent, which mouse_move_to and
# mouse_click_at coordinates refer to.
_frame = None
# Tools that failed to write PPM; they are asked for PNG from then on.
_no_ppm = set()


def _capture_commands(path):
    """(label, argv, writes_ppm) for each capture tool, in order of
    preference. PPM output is uncompressed, so it can be scaled quickly
    without an image library; PNG is the fallback."""
    ppm, png = path + ".ppm", path + ".png"
    for label, argv in (("spectacle", ["spectacle", "-b", "-n", "-o", "%s"]),
                        ("grim", ["grim", "-t", "%t", "%s"]),
                        ("scrot", ["scrot", "-o", "%s"]),
                        ("import", ["import", "-window", "root", "%s"])):
        if not shutil.which(argv[0]):
            continue
        for out, fmt in ((ppm, "ppm"), (png, "png")):
            if fmt == "ppm" and label in _no_ppm:
                continue
            yield label, [{"%s": out, "%t": fmt}.get(a, a) for a in argv], out, fmt == "ppm"


def _capture_screen():
    """Raw screenshot bytes (PPM or PNG) of the machine this server runs on.

    Tries, in order: gamescope (a Steam Deck in Gaming Mode, when its
    socket exists), Spectacle (KDE, e.g. a Steam Deck in Desktop Mode),
    grim (wlroots), the X11 tools scrot and ImageMagick import, then KWin's
    D-Bus screenshot as a fallback (bounded to ~8 s: on some Plasma versions
    it waits on an interactive authorization prompt). Raises RuntimeError
    listing every failure when no method works.
    """
    _ensure_graphical_env()
    errors = []
    tmpdir = tempfile.mkdtemp(prefix="elfdeckai-")
    try:
        display = _gamescope_display()
        if display and shutil.which("gamescopectl"):
            try:
                return _capture_gamescope(os.path.join(tmpdir, "gamescope.png"), display)
            except Exception as e:
                errors.append("gamescope: %s" % e)
        for label, cmd, out, is_ppm in _capture_commands(os.path.join(tmpdir, "shot")):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
                if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
                    with open(out, "rb") as f:
                        return f.read()
                if is_ppm:
                    _no_ppm.add(label)
                    continue
                errors.append("%s: %s" % (
                    label, (r.stderr or "").strip() or "exit %d" % r.returncode))
            except Exception as e:
                errors.append("%s: %s" % (label, e))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if shutil.which("dbus-send"):
        try:
            out = subprocess.run(
                ["dbus-send", "--session", "--print-reply",
                 "--reply-timeout=8000",
                 "--dest=org.kde.KWin", "/Screenshot",
                 "org.kde.KWin.Screenshot.screenshotFullscreen"],
                capture_output=True, text=True, timeout=12)
            m = re.search(r'string "([^"]+\.png)"', out.stdout or "")
            if m:
                try:
                    with open(m.group(1), "rb") as f:
                        data = f.read()
                finally:
                    try:
                        os.unlink(m.group(1))  # KWin leaves it in /tmp
                    except OSError:
                        pass
                if data:
                    return data
            errors.append("KWin D-Bus: %s" % (
                (out.stderr or "").strip() or "no screenshot path returned"))
        except Exception as e:
            errors.append("KWin D-Bus: %s" % e)
    raise RuntimeError(
        "screenshot failed — no working capture method here (the server "
        "needs a graphical session on its own machine). Tried: %s"
        % ("; ".join(errors) or "no capture tools installed"))


def _parse_ppm(data):
    """Binary PPM (P6, 8- or 16-bit) -> (width, height, 8-bit rgb bytes)."""
    fields, pos = [], 2
    while len(fields) < 3:
        while data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            pos = data.index(b"\n", pos) + 1
            continue
        start = pos
        while data[pos:pos + 1].isdigit():
            pos += 1
        if start == pos:
            raise ValueError("malformed PPM header")
        fields.append(int(data[start:pos]))
    w, h, maxval = fields
    if maxval not in (255, 65535):
        raise ValueError("unsupported PPM depth: %d" % maxval)
    size = w * h * 3 * (2 if maxval == 65535 else 1)
    pixels = data[pos + 1:pos + 1 + size]
    if len(pixels) != size:
        raise ValueError("truncated PPM")
    if maxval == 65535:
        pixels = pixels[0::2]   # 16-bit samples, big-endian: keep the high byte
    return w, h, pixels


def _bytewise(n, byte):
    return int.from_bytes(bytes([byte]) * n, "big")


def _average(a, b):
    """Per-byte floor((a + b) / 2) of two equal-length byte strings, done on
    whole buffers at once as big integers (no carries cross byte lanes)."""
    n = len(a)
    x, y = int.from_bytes(a, "big"), int.from_bytes(b, "big")
    return ((x & y) + (((x ^ y) & _bytewise(n, 0xFE)) >> 1)).to_bytes(n, "big")


def _subtract(a, b):
    """Per-byte (a - b) mod 256 of two equal-length byte strings."""
    n = len(a)
    hi, lo = _bytewise(n, 0x80), _bytewise(n, 0x7F)
    x, y = int.from_bytes(a, "big"), int.from_bytes(b, "big")
    return (((x | hi) - (y & lo)) ^ ((x ^ y ^ hi) & hi)).to_bytes(n, "big")


def _halve(plane, w, h):
    """2x2 box-average one colour plane."""
    w2, h2 = w // 2, h // 2
    rows = [plane[r * w:r * w + 2 * w2] for r in range(2 * h2)]
    v = _average(b"".join(rows[0::2]), b"".join(rows[1::2]))
    return _average(v[0::2], v[1::2]), w2, h2


def _resample(plane, w, h, nw, nh):
    """Nearest-neighbour resize of one colour plane."""
    cols = [i * w // nw for i in range(nw)] + [0]   # [0] keeps a tuple result
    pick = operator.itemgetter(*cols)
    return b"".join(bytes(pick(plane[(j * h // nh) * w:(j * h // nh + 1) * w])[:-1])
                    for j in range(nh))


def _scale_rgb(w, h, rgb, max_side):
    """Shrink an RGB image so neither side exceeds max_side: box-filter
    halvings while it is at least twice too big, then one nearest-neighbour
    step to the exact size."""
    planes = [rgb[0::3], rgb[1::3], rgb[2::3]]
    while max(w, h) >= 2 * max_side:
        results = [_halve(p, w, h) for p in planes]
        planes = [r[0] for r in results]
        w, h = results[0][1], results[0][2]
    if max(w, h) > max_side:
        nw = max(1, w * max_side // max(w, h))
        nh = max(1, h * max_side // max(w, h))
        planes = [_resample(p, w, h, nw, nh) for p in planes]
        w, h = nw, nh
    out = bytearray(w * h * 3)
    out[0::3], out[1::3], out[2::3] = planes
    return w, h, bytes(out)


def _png_chunk(kind, body):
    return (struct.pack(">I", len(body)) + kind + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))


def _encode_png(w, h, rgb):
    """RGB bytes -> PNG, every row with the Up filter (row minus the row
    above), which compresses screenshots well and vectorises cleanly."""
    stride = w * 3
    up = _subtract(rgb, bytes(stride) + rgb[:-stride])
    raw = b"".join(b"\x02" + up[r * stride:(r + 1) * stride] for r in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(raw, 6))
            + _png_chunk(b"IEND", b""))


_PNG_END = b"IEND\xaeB`\x82"


def _png_size(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("not a PNG")
    return struct.unpack(">II", data[16:24])


def _screenshot():
    """(png, width, height, captured_width, captured_height) of the screen,
    scaled down to SCREENSHOT_MAX where the capture format allows it."""
    data = _capture_screen()
    if data[:2] == b"P6":
        cw, ch, rgb = _parse_ppm(data)
        w, h = cw, ch
        if SCREENSHOT_MAX and max(w, h) > SCREENSHOT_MAX:
            w, h, rgb = _scale_rgb(w, h, rgb, SCREENSHOT_MAX)
        return _encode_png(w, h, rgb), w, h, cw, ch
    w, h = _png_size(data)   # PNG from a tool without PPM output: sent as is
    return data, w, h, w, h


def _text(s):
    return [{"type": "text", "text": s}]


def _screenshot_result():
    global _frame
    try:
        png, w, h, cw, ch = _screenshot()
    except (RuntimeError, ValueError) as e:
        return _text(str(e)), True
    _frame = (w, h)
    note = "Screenshot %dx%d px" % (w, h)
    if (w, h) != (cw, ch):
        note += " (screen captured at %dx%d, scaled down)" % (cw, ch)
    note += (". mouse_move_to and mouse_click_at take x in 0..%d and "
             "y in 0..%d of this image." % (w - 1, h - 1))
    return [{"type": "image",
             "data": base64.b64encode(png).decode(),
             "mimeType": "image/png"}] + _text(note), False


def _pointer_lines(name, args):
    """FIFO lines for mouse_move_to / mouse_click_at. Coordinates are pixels
    of the last screenshot sent; ElfMouse maps them across the screen."""
    global _frame
    if _frame is None:
        _frame = _screenshot()[1:3]
    w, h = _frame
    if args["x"] >= w or args["y"] >= h:
        raise ValueError("(%d, %d) is outside the last screenshot, which is "
                         "%dx%d px" % (args["x"], args["y"], w, h))
    lines = ["moveto %d %d %d %d" % (args["x"], args["y"], w, h)]
    if name == "mouse_click_at":
        lines.append(_mouse_line("mouse_click", {
            "button": args.get("button", "left"), "hold_ms": args.get("hold_ms")}))
    return lines


def call_tool(name, args):
    """Returns (content_items, is_error)."""
    if name not in TOOL_NAMES:
        return _text("unknown tool: %s" % name), True
    try:
        _validate_args(name, args)
    except ValueError as e:
        return _text("invalid arguments: %s" % e), True
    if name == "screenshot":
        return _screenshot_result()
    try:
        if name.startswith("key_"):
            sends = [(KEY_FIFO, _key_line(name, args), elfkey.parse_command)]
        elif name in ("mouse_move_to", "mouse_click_at"):
            try:
                lines = _pointer_lines(name, args)
            except RuntimeError as e:
                return _text("cannot place the pointer: %s" % e), True
            sends = [(MOUSE_FIFO, line, elfmouse.parse_command) for line in lines]
        elif name.startswith("mouse_"):
            sends = [(MOUSE_FIFO, _mouse_line(name, args), elfmouse.parse_command)]
        else:
            sends = [(PAD_FIFO, _pad_line(name, args), elfpad.parse_command)]
        return _text("; ".join(_send(*s) for s in sends)), False
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
        params = msg.get("params")
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "elfdeckai", "version": __version__}}}
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
