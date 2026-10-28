# 🎮 ElfdeckAI

### Your AI can *see* the game. Now it can hold the controller.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.8+-blue.svg)
![Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)
![Tests](https://img.shields.io/badge/tests-142%20passing-brightgreen.svg)

**ElfdeckAI gives an LLM agent hands on real hardware.** Three tiny
daemons turn one-line commands — from a shell, a script, or native
**MCP tool calls** — into real keyboard, gamepad and mouse input on a
Linux machine:

- ⌨️ **ElfKey** — a virtual keyboard on `/dev/uinput`
- 🕹️ **ElfPad** — a virtual gamepad (Xbox-class: buttons, analog triggers,
  two sticks, D-pad)
- 🖱️ **ElfMouse** — a virtual mouse (relative pointer, buttons, wheel)

Every "LLM plays Pokémon" project you've seen plugs into an **emulator's
API**. ElfdeckAI presses buttons on a **real device** — proven live when an
LLM played Pokémon FireRed on a physical Steam Deck end-to-end: full
intro, starter picked, first rival battle won, game saved. No emulator, no
RAM reading, no capture card — just a stream for eyes and these daemons
for hands. ([How it drives](notes/driving.md))

## ⚡ Run it yourself (60 seconds)

```sh
pip install git+https://github.com/Garthnesss/ElfDeckAI

elfkey &                     # virtual keyboard   (command: elfkey)
elfpad &                     # virtual gamepad    (command: elfpad)
elfmouse &                   # virtual mouse      (command: elfmouse)

echo "key x 150"        > /tmp/elfkey.fifo     # tap X
echo "press a"          > /tmp/elfpad.fifo     # tap A
echo "move 300 -100"    > /tmp/elfmouse.fifo   # flick the pointer
echo "click left"       > /tmp/elfmouse.fifo   # click

grep -E "ElfKey|ElfPad|ElfMouse" /proc/bus/input/devices   # they're really there
```

That's the whole install. Three single-file Python programs, **standard
library only** — nothing to build, no dependencies to resolve. State
lives in `/tmp`; reboot and it's gone. Prefer no pip at all? Clone the
repo and run `python3 elfkey.py` — same thing.

For your agent, the pip install also provides the MCP server as a
command: `"command": "elfdeckai-mcp"` (details below).

> **Steam Deck?** You're already done — `/dev/uinput` is user-writable
> there out of the box. On other Linux machines, install the included
> udev rule once (see [Install](#install)).

## 🤖 Give it to your agent (MCP)

`elfdeckai_mcp.py` is a zero-dependency [Model Context
Protocol](https://modelcontextprotocol.io) server that exposes all
three devices as 18 native tools — `key_tap`, `pad_press`, `pad_stick`,
`mouse_move`, `mouse_click`, `pad_reset`, `key_release`, … — for Claude,
Hermes, or any MCP client:

```json
{"mcpServers": {"elfdeckai": {"command": "elfdeckai-mcp"}}}
```

(No pip? `"command": "python3", "args": ["/path/to/elfdeckai_mcp.py"]`
works identically.)

Start the daemons, add that to your client's config, and your agent can
press buttons the same way it calls any other tool. Every call is
validated by the daemons' own input contract before it touches a FIFO,
and a stopped daemon fails the call cleanly instead of hanging your agent.

## 🎛️ Commands

### ElfKey — `/tmp/elfkey.fifo`

| Command | Effect |
|---|---|
| `key <name> [ms]` | tap a key (default 120 ms) |
| `down <name>` / `up <name>` | hold / release |
| `release` | let go of every held key at once (the unstick command) |
| `quit` | release everything and exit |

Keys: `a`–`z`, `0`–`9`, arrows, `enter`, `esc`, `space`, `tab`, `shift`,
`rshift`, `ctrl`, `alt`, punctuation, `f1`–`f10`, `home`, `end`, `insert`,
`delete`, … (full table in the source).

### ElfPad — `/tmp/elfpad.fifo`

| Command | Effect |
|---|---|
| `press <btn> [ms]` | tap a button — or a D-pad direction |
| `down <btn>` / `up <btn>` | hold / release |
| `stick left\|right <x> <y> [ms]` | analog stick, −100..100 % per axis; `ms` = auto-recenter after |
| `trigger left\|right <0..100> [ms]` | analog trigger; `ms` = auto-release after |
| `center` | sticks, triggers, D-pad to neutral |
| `reset` | release everything |
| `quit` | release everything and exit |

Buttons: `a b x y lb rb l2 r2 select start guide l3 r3`. ElfPad presents
as an Xbox-360-class pad, so SDL, Steam Input and RetroArch map it the
standard way with zero configuration.

### ElfMouse — `/tmp/elfmouse.fifo`

| Command | Effect |
|---|---|
| `move <dx> <dy> [glide_ms]` | move the pointer (relative); `glide_ms` = glide there over time |
| `click <btn> [ms]` | click `left` / `right` / `middle` |
| `down <btn>` / `up <btn>` | hold / release a button — drags are `down`, `move`, `up` |
| `scroll <ticks>` / `hscroll <ticks>` | wheel / horizontal wheel (positive = up / right) |
| `release` | let go of every held button at once (the unstick command) |
| `quit` | release any held button and exit |

Movement is relative, like a real mouse — pair it with screenshots and
the same look → move → verify loop the rest of the set uses. Per-command
bounds (±10 000 px, ±120 ticks) keep a confused call from flinging the
pointer into orbit; chain commands for long travel.

## 🛡️ Built for callers that hallucinate

Letting a language model press buttons is safe only if the keyboard
itself can't be talked into a bad state. So:

- **Bad input never kills the daemon.** Every line passes a strict
  parser; malformed commands are logged and refused. (`key x abc` and
  `key x 999999999` were real crash/wedge bugs in the first version —
  the tests now pin them shut.)
- **Holds are bounded.** A tap is at most 5 s. Longer holds use
  `down`/`up`, which the caller controls.
- **Nothing gets stuck.** Timed moves restore in a `finally` block; held
  inputs are tracked; on `quit`, SIGTERM or SIGINT, everything is
  released and recentered before the device is destroyed.
- **The FIFO is owner-only (0600).** Only your user can inject input.
- **The server survives garbage.** Malformed JSON and non-object
  requests get proper JSON-RPC protocol errors; one bad line — or a
  caller dying mid-hold — can't wedge the session. `key_release`,
  `mouse_release` and `pad_reset` are the always-available unstick
  buttons.
- **Scope honesty:** these are real OS-level input devices. They type
  and click in whatever window has focus — a game, but equally a
  terminal or a browser. ElfdeckAI makes that input *un-mess-up-able*
  (bounded, validated, releasable); deciding *where* an agent may act
  is the operator's call. Run agents you trust, on sessions you'd
  watch.

## 🤔 Why not…?

| Approach | Why it falls short |
|---|---|
| Emulator APIs (PyBoy, mGBA scripting) | Only exists inside an emulator. Your Deck is not an emulator. |
| Sending keys through Moonlight/streaming | Clicks get forwarded; synthetic keyboard events don't (verified). |
| xdotool / desktop automation | Can't reach native Wayland clients. |
| KVM gadget / controller-spoofing hardware | Works, but it's extra hardware on your desk doing what 200 lines of Python do on the device itself. |

## Install

**Requirements:** Linux with `/dev/uinput`, Python 3.8+. Nothing else.

Anywhere `/dev/uinput` isn't user-writable, install the udev rule once:

```sh
sudo cp 99-elfdeckai-uinput.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Run persistently as user services (expects the repo at `~/elfdeckai/`):

```sh
cp systemd/*.service ~/.config/systemd/user/
systemctl --user enable --now elfkey.service elfpad.service elfmouse.service
```

## Tests

```sh
python3 -m pytest    # 142 tests: all three input contracts, device behaviour
                     # against a fake uinput, and the MCP server spoken
                     # to over real JSON-RPC
```

## Roadmap

- One combined daemon (keyboard + pad + mouse in a single process).
- PyPI release, so `pip install elfdeckai` works without the git URL.
- Demo clip: the Pokémon run, on camera.

## License

MIT — see [LICENSE](LICENSE). Steal it, ship it, teach your agent to
speedrun something.

## Author

Created and maintained by **Garth Elfstrom**
([@Garthnesss](https://github.com/Garthnesss)). Issues and PRs welcome
on the repo.
