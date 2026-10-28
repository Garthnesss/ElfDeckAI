# 🎮 ElfdeckAI

### Your AI can *see* the game. Now it can hold the controller.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.8+-blue.svg)
![Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)
![Tests](https://img.shields.io/badge/tests-91%20passing-brightgreen.svg)

**ElfdeckAI gives an LLM agent hands on real hardware.** Two tiny daemons
turn one-line commands — from a shell, a script, or native **MCP tool
calls** — into real keyboard and gamepad input on a Linux machine:

- ⌨️ **ElfKey** — a virtual keyboard on `/dev/uinput`
- 🕹️ **ElfPad** — a virtual gamepad (Xbox-class: buttons, analog triggers,
  two sticks, D-pad)

Every "LLM plays Pokémon" project you've seen plugs into an **emulator's
API**. ElfdeckAI presses buttons on a **real device** — proven live when an
LLM played Pokémon FireRed on a physical Steam Deck end-to-end: full
intro, starter picked, first rival battle won, game saved. No emulator, no
RAM reading, no capture card — just a stream for eyes and these daemons
for hands. ([How it drives](notes/driving.md))

## ⚡ Quickstart (60 seconds)

```sh
git clone https://github.com/Garthnesss/ElfdeckAI && cd ElfdeckAI

python3 elfkey.py &          # virtual keyboard
python3 elfpad.py &          # virtual gamepad

echo "key x 150"        > /tmp/elfkey.fifo    # tap X
echo "press a"          > /tmp/elfpad.fifo    # tap A
echo "stick left 60 0"  > /tmp/elfpad.fifo    # push the left stick

grep -E "ElfKey|ElfPad" /proc/bus/input/devices   # they're really there
```

That's the whole install. Two single-file Python programs, **standard
library only** — nothing to pip, nothing to build. State lives in `/tmp`;
reboot and it's gone.

> **Steam Deck?** You're already done — `/dev/uinput` is user-writable
> there out of the box. On other Linux machines, install the included
> udev rule once (see [Install](#install)).

## 🤖 Give it to your agent (MCP)

`elfdeckai_mcp.py` is a zero-dependency [Model Context
Protocol](https://modelcontextprotocol.io) server that exposes both
devices as native tools — `key_tap`, `key_down`, `pad_press`, `pad_stick`,
`pad_trigger`, `pad_reset`, … — for Claude, Hermes, or any MCP client:

```json
{"mcpServers": {"elfdeckai": {
    "command": "python3", "args": ["/path/to/ElfdeckAI/elfdeckai_mcp.py"]}}}
```

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
systemctl --user enable --now elfkey.service elfpad.service
```

## Tests

```sh
python3 -m pytest    # 91 tests: both input contracts, device behaviour
                     # against a fake uinput, and the MCP server spoken
                     # to over real JSON-RPC
```

## Roadmap

- **ElfMouse** — relative pointer + buttons, completing the set.
- One combined daemon (keyboard + pad + mouse in a single process).
- A `pip install`able package once the FIFO/MCP interfaces settle.

## License

MIT — see [LICENSE](LICENSE). Steal it, ship it, teach your agent to
speedrun something.
