# Changelog

All notable changes to this project are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project follows [Semantic Versioning](https://semver.org/).

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

[1.0.7]: https://github.com/Garthnesss/ElfDeckAI/compare/v1.0.6...v1.0.7
