# Roadmap

## Next

- **Steam Deck Gaming Mode.** Screenshots and verified input while the Deck
  is in Gaming Mode. See the plan below.
- **Demo recording.** A short clip of an agent playing on a physical Deck.
- **Combined daemon.** Keyboard, gamepad and mouse in one process and one
  systemd service.

## Later

- **PNG scaling.** Screenshots from tools that can only write PNG (the KWin
  D-Bus fallback, and gamescope in Gaming Mode) are sent at full size,
  because decoding PNG quickly needs an image library. A pure-Python
  decoder fast enough for 1080p and larger would close this gap.
- **Multi-display pointer mapping.** `mouse_click_at` assumes one display.
  Supporting several means mapping the absolute device to one output.
- **More keys.** `f11`, `f12` and media keys for ElfKey.

---

## Plan: Steam Deck Gaming Mode

### Where things stand

In Gaming Mode the Deck does not run KDE Plasma. The screen is composited
by **gamescope**, Valve's own compositor, and Steam's interface runs on top
of it. That changes three things:

| Area | Desktop Mode today | Gaming Mode |
|---|---|---|
| Screenshots | Spectacle / KWin | Spectacle and KWin are not running. Needs gamescope's own capture. |
| Keyboard and mouse | Work | The services keep running, and gamescope reads input through libinput, so ElfKey and ElfMouse should reach the focused game. Not yet verified. |
| Absolute pointer | Works | gamescope's libinput handler processes absolute pointer events, so `mouse_click_at` should work. Not yet verified. |
| Gamepad | Works, with one known issue | Steam Input owns controllers. A game can ignore ElfPad when Steam gives the first controller slot to the Deck's built-in pad (seen in Dragon Quest VII). |

### Is it possible?

Very likely, yes. gamescope has a built-in screenshot command, reached
through its `gamescopectl` tool:

```sh
gamescopectl screenshot /tmp/shot.png 4
```

The last argument is the capture type from gamescope's control protocol:
`1` is the game only, `2` the game plus overlays, `3` the full composition,
and `4` exactly what is on screen. Type 4 shows what a player sees,
including Steam's menus, so it is the right default for an agent. The
command returns at once and the file appears shortly afterwards, so the
server has to wait for it.

The unknowns can only be answered on a real Deck:

1. Is `gamescopectl` installed on current SteamOS?
2. Can a process started over SSH reach gamescope? It needs
   `GAMESCOPE_WAYLAND_DISPLAY` (normally `gamescope-0`) and
   `XDG_RUNTIME_DIR`.
3. How long does the file take to appear, and at what resolution?
4. Do ElfKey, ElfMouse and ElfMouse Absolute reach games in Gaming Mode?

### Phase 1: probe (about 10 minutes, on a Deck)

`scripts/probe-gaming-mode.sh` answers all four questions without changing
anything. With the Deck in Gaming Mode, from the PC:

```sh
ssh deck@DECK-IP 'sh -s' < scripts/probe-gaming-mode.sh
```

It prints the session environment, the available capture tools, the result
of one test screenshot (size and timing), and which virtual devices exist.
Then check input by hand: open a game, and use `key_tap` and
`mouse_click_at` from the AI client.

### Phase 2: Gaming Mode screenshots

Depending on the probe results:

- Detect Gaming Mode: a gamescope Wayland socket in `XDG_RUNTIME_DIR`, or
  `GAMESCOPE_WAYLAND_DISPLAY` in the systemd user environment, with no
  KWin running.
- Add a gamescope capture method ahead of the Desktop Mode tools: run
  `gamescopectl screenshot <tmp>.png 4`, wait up to about 3 seconds for
  the file to finish writing, then return it.
- Import `GAMESCOPE_WAYLAND_DISPLAY` from the systemd user environment,
  the same way `DISPLAY` and `WAYLAND_DISPLAY` are imported today.
- Tests with a fake `gamescopectl`, like the existing fake Spectacle.

gamescope writes PNG. On the Deck's own 1280×800 screen no scaling is
needed. When docked to a large display the image would go out at full
size until PNG scaling (under "Later") lands.

**Fallback if `gamescopectl` is not usable:** Steam's own screenshot
feature, which saves to
`~/.local/share/Steam/userdata/<id>/760/remote/<app>/screenshots/`.
ElfKey would press Steam's screenshot key (F12 by default, which needs the
"More keys" item) and the server would read the newest file. This is slower
and only works inside games, so it is a backup plan.

### Phase 3: input in Gaming Mode

- Verify keyboard, relative mouse and absolute mouse in two or three games
  and in Steam's own menus.
- ElfPad: find a dependable way to make Steam give the virtual pad a
  controller slot, or document the keyboard route as the default for
  affected games.
- Update AI_SETUP_GUIDE.md so assistants know Gaming Mode works, and which
  controls to prefer there.

### Risks

- `gamescopectl` is a developer tool, and its screenshot command could
  change between SteamOS releases. The probe script and tests make a break
  quick to spot.
- HDR games: gamescope may capture in a different colour format. PNG
  capture is requested explicitly to avoid that.
- Steam Input behaviour varies per game, so ElfPad results may differ from
  title to title.
