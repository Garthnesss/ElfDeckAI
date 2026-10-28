# ElfDeckAI

[![tests](https://github.com/Garthnesss/ElfDeckAI/actions/workflows/tests.yml/badge.svg)](https://github.com/Garthnesss/ElfDeckAI/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/elfdeckai.svg)](https://pypi.org/project/elfdeckai/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.9+-blue.svg)

**Let an AI play games on your Steam Deck.**

<!-- mcp-name: io.github.Garthnesss/elfdeckai -->

ElfDeckAI gives an AI assistant such as Claude a virtual controller,
keyboard and mouse on your Steam Deck, plus the ability to see the screen.
The AI looks at the game, decides what to do, and presses the buttons
itself. It works in both Gaming Mode and Desktop Mode, and on other Linux
machines too.

## Start here: Steam Deck (about 5 minutes)

You need a Steam Deck, and a PC on the same Wi-Fi with an AI app that
supports MCP, such as [Claude Desktop](https://claude.ai/download).

**1. Install on the Deck.** Switch the Deck to Desktop Mode (Steam button →
Power → Switch to Desktop), open **Konsole**, and paste:

```sh
curl -fsSL https://raw.githubusercontent.com/Garthnesss/ElfDeckAI/main/install-on-deck.sh | sh
```

At the end it prints the Deck's IP address. Keep it for step 3.

**2. Turn on SSH on the Deck** (once), so your PC can reach it. In the same
Konsole window:

```sh
passwd                            # choose a password for the Deck, if you haven't
sudo systemctl enable --now sshd
```

**3. Connect your PC.** On the PC, paste one line. It asks for the Deck's IP
address and its password (once), then sets everything up, including
Claude Desktop.

- **Windows** (in PowerShell):
  ```powershell
  irm https://raw.githubusercontent.com/Garthnesss/ElfDeckAI/main/setup-pc.ps1 | iex
  ```
- **Mac or Linux** (in Terminal):
  ```sh
  curl -fsSL https://raw.githubusercontent.com/Garthnesss/ElfDeckAI/main/setup-pc.sh | sh
  ```

**4. Try it.** Restart Claude Desktop and ask: *"Take a screenshot of my
Steam Deck."* When you see your Deck's screen, start a game and ask the AI
to play.

**Tips**

- Turn-based games, RPGs, puzzles and menus work best. The AI needs a
  second or more per move, so fast action games are out of reach.
- In Gaming Mode, if a game ignores the AI's controller, ask the AI to make
  its controller **Player 1**. It knows how, using Steam's Reorder
  Controllers menu.
- Something not working? Give [AI_SETUP_GUIDE.md](AI_SETUP_GUIDE.md) to
  your AI and ask it to help. It covers setup and troubleshooting step by
  step.

## How it works

Three small programs on the Deck create virtual input devices through the
Linux kernel's `/dev/uinput`, so games and Steam see them as real hardware:

- **ElfKey**: a virtual keyboard
- **ElfPad**: a virtual Xbox 360-class gamepad (buttons, analog triggers,
  two sticks, D-pad)
- **ElfMouse**: a virtual mouse, which can also click an exact point on
  the screen

A fourth program, an MCP server, exposes them, together with a screenshot
tool, as tools any MCP client can call. Your PC's AI app starts that server
on the Deck over SSH. Nothing depends on a particular game or emulator:
ElfDeckAI has played Pokémon FireRed in RetroArch on a physical Steam Deck,
from the intro through the first rival battle, with the agent seeing only
screenshots. See [notes/driving.md](notes/driving.md) for what was learned.

Everything is single-file Python using only the standard library, and the
Deck install needs no sudo.

## Other Linux machines

```sh
pip install elfdeckai

elfkey &
elfpad &
elfmouse &

echo "key x 150"     > /tmp/elfkey.fifo     # tap X for 150 ms
echo "press a"       > /tmp/elfpad.fifo     # tap A
echo "move 300 -100" > /tmp/elfmouse.fifo   # move the pointer
echo "click left"    > /tmp/elfmouse.fifo   # left click

grep -E "ElfKey|ElfPad|ElfMouse" /proc/bus/input/devices   # confirm the devices exist
```

Without pip, clone the repository and run `python3 elfkey.py` (and so on)
directly. Runtime state lives in `/tmp` and is cleared on reboot.

`/dev/uinput` is user-writable on the Steam Deck by default. On other
distributions, install the udev rule first (see [Installation](#installation)).

## MCP server

`elfdeckai-mcp` is a Model Context Protocol server (stdio transport) that
exposes 21 tools:

| Group | Tools |
|---|---|
| Keyboard | `key_tap`, `key_down`, `key_up`, `key_release` |
| Gamepad | `pad_press`, `pad_down`, `pad_up`, `pad_stick`, `pad_trigger`, `pad_center`, `pad_reset` |
| Mouse | `mouse_click_at`, `mouse_move_to`, `mouse_move`, `mouse_click`, `mouse_down`, `mouse_up`, `mouse_scroll`, `mouse_hscroll`, `mouse_release` |
| Vision | `screenshot` |

`screenshot` captures the screen of the machine the server runs on and
returns a PNG, so an agent can work in a look, act, look again loop
without a separate video stream. It supports the Steam Deck in both Gaming
Mode (through gamescope) and Desktop Mode (KDE Plasma), wlroots compositors
and X11. Screens larger than
1280 pixels on their long side are scaled down before sending (set
`ELFDECKAI_SCREENSHOT_MAX` to change the limit, or `0` to turn it off), and
the result includes the image's size.

`mouse_click_at` and `mouse_move_to` take coordinates in the pixels of the
most recent screenshot, so an agent can click exactly what it sees, whatever
the screen's real resolution or the scaling applied. They assume a single
display.

Running on the same machine as the daemons:

```json
{"mcpServers": {"elfdeckai": {"command": "elfdeckai-mcp"}}}
```

Running on a Steam Deck from an AI client on your PC, over SSH (the
`setup-pc` scripts in [Start here](#start-here-steam-deck-about-5-minutes)
write this for you):

```json
{"mcpServers": {"elfdeckai": {
    "command": "ssh",
    "args": ["-i", "~/.ssh/elfdeckai_ed25519", "-o", "IdentitiesOnly=yes",
             "-o", "BatchMode=yes", "deck@DECK-IP-ADDRESS", ".local/bin/elfdeckai-mcp"]}}}
```

It logs in with an SSH key, because the MCP client cannot answer a password
prompt. Without pip, use
`"command": "python3", "args": ["/path/to/elfdeckai_mcp.py"]`.

Every tool call is validated against the tool's schema and the daemon's
own parser before anything is written to a FIFO. If a daemon is not
running, the call returns an error immediately instead of blocking.

## Command reference

### ElfKey (`/tmp/elfkey.fifo`)

| Command | Effect |
|---|---|
| `key <name> [ms]` | Tap a key (default 120 ms) |
| `down <name>` / `up <name>` | Hold / release a key |
| `release` | Release every held key |
| `quit` | Release everything and exit |

Keys: `a`–`z`, `0`–`9`, arrows, `enter`, `esc`, `space`, `tab`, `backspace`,
`shift`, `rshift`, `ctrl`, `rctrl`, `alt`, `ralt`, `capslock`, `f1`–`f10`,
`home`, `end`, `pageup`, `pagedown`, `insert`, `delete` and common
punctuation. The full table is `KEYS` in [elfkey.py](elfkey.py).

### ElfPad (`/tmp/elfpad.fifo`)

| Command | Effect |
|---|---|
| `press <btn> [ms]` | Tap a button or D-pad direction (default 120 ms) |
| `down <btn>` / `up <btn>` | Hold / release |
| `stick left\|right <x> <y> [ms]` | Set an analog stick, −100 to 100 per axis; with `ms`, recenter afterwards |
| `trigger left\|right <0..100> [ms]` | Set an analog trigger; with `ms`, release afterwards |
| `center` | Return sticks, triggers and D-pad to neutral |
| `reset` | Release everything |
| `quit` | Release everything and exit |

Buttons: `a b x y lb rb l2 r2 select start guide l3 r3`, plus D-pad
directions `up down left right`.

ElfPad identifies as an Xbox 360 controller, so SDL, Steam Input and
RetroArch map it without configuration. `l2` and `r2` drive the analog
trigger axes, as on the real controller; there are intentionally no
digital trigger buttons, because declaring them shifts Select, Start and
Guide off the standard button indices.

### ElfMouse (`/tmp/elfmouse.fifo`)

| Command | Effect |
|---|---|
| `move <dx> <dy> [glide_ms]` | Move the pointer relatively; with `glide_ms`, spread the move over that time |
| `moveto <x> <y> [<width> <height>]` | Place the pointer at an absolute position: pixel (x, y) of a `width`×`height` frame, or device units 0–32767 across the screen when no frame is given |
| `click <btn> [ms]` | Click `left`, `right` or `middle` (default 100 ms) |
| `down <btn>` / `up <btn>` | Hold / release a button (for drags: `down`, `move`, `up`) |
| `scroll <ticks>` / `hscroll <ticks>` | Vertical / horizontal wheel (positive is up / right) |
| `release` | Release every held button |
| `quit` | Release everything and exit |

`move` is relative: negative `dy` moves up. Its units are motion counts,
not guaranteed screen pixels, because pointer acceleration applies. Each
command is limited to ±10,000 counts and ±120 wheel ticks.

`moveto` uses a second virtual device, **ElfMouse Absolute**, which
compositors treat like a virtual machine's tablet pointer, so the position
is exact. Buttons are pressed on whichever device moved last.

## Safety model

Because the caller may be a language model, the daemons are built so that
bad input cannot leave the machine in a bad state:

- **Malformed input is refused, not fatal.** Every line passes a strict
  parser; invalid commands are logged and ignored.
- **Taps are bounded.** A timed hold lasts at most 5 seconds. Longer holds
  use `down`/`up`.
- **Nothing stays stuck.** Timed actions restore in a `finally` block,
  held inputs are tracked, and on `quit`, SIGTERM or SIGINT every input is
  released before the device is removed. `key_release`, `mouse_release`
  and `pad_reset` release everything on demand.
- **Owner-only access.** Each FIFO is created with mode 0600.
- **Audit log.** Every accepted command is written to stderr (the
  journal, when run as a service).
- **Robust protocol handling.** The MCP server returns proper JSON-RPC
  errors for malformed or oversized requests and never blocks on a stopped
  daemon.

**Scope:** these are real OS-level input devices. They act on whichever
window has focus, including terminals and browsers. ElfDeckAI keeps input
bounded, validated and releasable; deciding where an agent is allowed to
act is up to the operator. Only run agents you trust, in sessions you
can supervise.

## Comparison with other approaches

| Approach | Limitation |
|---|---|
| Emulator APIs (PyBoy, mGBA scripting) | Only work inside a specific emulator. |
| Sending keys through Moonlight or other streaming clients | Mouse clicks are forwarded, but synthetic keyboard events are not. |
| xdotool | Cannot reach native Wayland clients. |
| ydotool, dotool | Same uinput approach for keyboard and mouse, but no gamepad, raw keycodes, and no agent-facing interface (MCP, input bounds, release commands). |
| Hardware input emulators (KVM gadgets, controller adapters) | Work, but require extra hardware. |

## Installation

**Requirements:** 64-bit Linux with `/dev/uinput` and Python 3.9 or newer.

Where `/dev/uinput` is not writable by your user, install the udev rule
once:

```sh
sudo cp 70-elfdeckai-uinput.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
getfacl /dev/uinput   # your user should appear in the ACL
```

To run the daemons as systemd user services after `pip install --user elfdeckai`
(the unit files expect the commands in `~/.local/bin`):

```sh
mkdir -p ~/.config/systemd/user
cp systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now elfkey.service elfpad.service elfmouse.service
```

## Development

```sh
pip install -e ".[test]"
python3 -m pytest
```

The test suite covers each daemon's input parser, device behaviour against
a fake uinput device, and the MCP server over real JSON-RPC. No
`/dev/uinput` is required to run it.

## Roadmap

See [ROADMAP.md](ROADMAP.md).

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE).

## Author

Created and maintained by Garth Elfstrom ([@Garthnesss](https://github.com/Garthnesss)).
Issues and pull requests are welcome.
