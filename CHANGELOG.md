# Changelog

All notable changes to this project are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project follows [Semantic Versioning](https://semver.org/).

## [1.1.3] - Unreleased

Fixes from an adversarial review of the 1.0.x/1.1.x line, checked against
1.1.2 (several review findings were already fixed by 1.1.2 and are not
listed here).

### Fixed
- ElfMouse: a duplicate `down` after a `moveto` switched devices orphaned
  the button on the first device — `held` forgot its fd, so neither `up`
  nor `release` could ever release it. The first device is now released
  when a held button is re-pressed on the other device.
- Stuck input when the MCP client disconnects: the server exits on stdin
  EOF without releasing anything, leaving a `down` held forever. It now
  sends release/reset to all three daemons on the way out.
- Daemons now trap SIGHUP as well as SIGTERM/SIGINT, so a daemon started
  with `elfkey &` over SSH cleans up (and releases) when the session
  closes instead of dying mid-hold.

### Added
- `wait <ms>` command in all three daemons and a `wait` MCP tool
  (0–5000 ms): timing between inputs is now expressible. Queued taps
  otherwise land back-to-back, and a frame-polled game can merge them
  into one continuous hold.

### Changed
- README Safety model: the audit log now warns that typed secrets
  (passwords, one line per keystroke) land in the journal, and a new
  "Manipulated callers" paragraph states plainly that the validation
  layer covers hallucinating callers, not an agent manipulated by
  on-screen content.

## [1.1.2] - Unreleased

### Added
- One-line PC setup: `setup-pc.ps1` (Windows) and `setup-pc.sh` (macOS,
  Linux) create a dedicated SSH key, copy it to the Deck, check the
  passwordless connection, and add ElfDeckAI to Claude Desktop's config.
  They work with Decks that only accept keys, including when the PC's
  ssh-agent holds a key restricted to one command.

### Changed
- README opens with a four-step "Start here" for Steam Deck owners; the
  technical description follows it. The Deck installer and
  AI_SETUP_GUIDE.md point to the PC setup scripts, and SSH is enabled with
  `sudo`.

## [1.1.1] - 2026-10-28

### Fixed
- Gamepad in Steam Deck Gaming Mode: games that ignored ElfPad were only
  reading Player 1, which Steam gives to the Deck's own controls. The
  `pad_press` and `pad_down` tool descriptions and AI_SETUP_GUIDE.md now
  tell agents how to make ElfPad Player 1 through Steam's Reorder
  Controllers (verified in Bastion).

### Added
- `scripts/probe-gamepad.py`, which shows which of Steam's virtual pads
  carries ElfPad's input.

## [1.1.0] - 2026-10-28

### Added
- `mouse_click_at` and `mouse_move_to` MCP tools: click or point at (x, y)
  in the pixels of the most recent screenshot. Backed by a new absolute
  pointer device, "ElfMouse Absolute", and the ElfMouse command
  `moveto <x> <y> [<width> <height>]`.
- Screenshots larger than 1280 px on their long side are scaled down
  before sending (`ELFDECKAI_SCREENSHOT_MAX` sets the limit; `0` disables).
  The screenshot result now also states the image size.
- Steam Deck Gaming Mode screenshots, through gamescope's `gamescopectl`.
  Used first whenever gamescope is running; the Desktop Mode tools remain
  the fallback.
- ROADMAP.md, and `scripts/probe-gaming-mode.sh` for checking Gaming Mode
  support on a Deck.

### Changed
- Screenshots are captured as uncompressed PPM where the capture tool
  supports it, falling back to PNG.
- The installer also checks for the ElfMouse Absolute device.
- The server re-reads the graphical session's variables before every
  screenshot, so switching between Desktop Mode and Gaming Mode no longer
  needs a server restart.

## [1.0.7] - 2026-10-28

### Fixed
- Daemons no longer exit when a FIFO line contains bytes that are not
  valid UTF-8; the line is refused like any other malformed command.
- The KWin D-Bus screenshot fallback now deletes the temporary file KWin
  writes to `/tmp`.
- The installer's troubleshooting hint names the correct systemd unit.
- Setup documentation now requires key-based SSH login: an MCP client
  launching `ssh` cannot answer a password prompt.

### Changed
- The MCP server negotiates the protocol version (`2025-06-18`,
  `2025-03-26` or `2024-11-05`) instead of always answering `2024-11-05`.
- Python 3.9 or newer is required (3.8 is end-of-life).
- The package version is defined once, in `elfdeckai_mcp.__version__`.
- Rewritten README and module documentation.

### Added
- MCP Registry metadata (`server.json` package entry and README marker).
- Continuous integration and a release workflow that publishes to PyPI
  and the MCP Registry and creates the GitHub Release. It runs on a
  version tag, or from the Actions tab, in which case it creates the tag.
- `glama.json` listing the maintainer for Glama.

## [1.0.6]

### Fixed
- `screenshot` imports the graphical session environment when the server
  is launched over SSH.
- The installer detects the Deck's IP address with `ip`, since `hostname`
  is not available on SteamOS.

## [1.0.5]

### Added
- `screenshot` tool.
- One-command Steam Deck installer.
- Setup guide written for an AI assistant to follow.

[1.1.3]: https://github.com/Garthnesss/ElfDeckAI/compare/v1.1.2...v1.1.3
[1.1.2]: https://github.com/Garthnesss/ElfDeckAI/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/Garthnesss/ElfDeckAI/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/Garthnesss/ElfDeckAI/compare/v1.0.7...v1.1.0
[1.0.7]: https://github.com/Garthnesss/ElfDeckAI/compare/v1.0.6...v1.0.7
