# Roadmap

## Next

- **Finish Gaming Mode.** Screenshots and the keyboard work; the mouse and
  gamepad need checking. See the plan below.
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

In Gaming Mode the Deck does not run KDE Plasma. The screen is composited
by **gamescope**, Valve's own compositor, with Steam's interface on top.

### Status (tested on a Deck, 2026-10-28)

| Area | Status |
|---|---|
| Screenshots | **Done.** `gamescopectl screenshot <file> 4` returns exactly what is on screen (1280×800, about 300 ms). The server uses it first whenever gamescope's socket exists, and falls back to the Desktop Mode tools. |
| Keyboard | **Works.** ElfKey arrow presses moved the menu cursor in Stardew Valley. |
| Absolute mouse (`mouse_click_at`) | **To verify.** It was blocked by the missing screenshot support, which is now fixed. gamescope's libinput handler processes absolute pointer events, so it is expected to work. |
| Relative mouse | **To verify.** |
| Gamepad (ElfPad) | **Known issue.** A game can ignore ElfPad when Steam gives the first controller slot to the Deck's built-in pad (seen in Dragon Quest VII). |
| Switching modes mid-session | **Done.** The server re-reads the session's variables before each screenshot. |

### Remaining work

1. **Verify the mouse.** In Gaming Mode, with a game open: `screenshot`,
   then `mouse_click_at` on a menu button, and a relative `mouse_move`.
2. **ElfPad.** Find a dependable way to make Steam give the virtual pad a
   controller slot, or document the keyboard route as the default for
   affected games.
3. **Docked to a large display.** gamescope writes PNG, which the server
   cannot scale yet (see "PNG scaling" under "Later"). On the Deck's own
   screen no scaling is needed.

`scripts/probe-gaming-mode.sh` re-checks the environment on any SteamOS
update. With the Deck in Gaming Mode, from the PC:

```sh
ssh deck@DECK-IP 'sh -s' < scripts/probe-gaming-mode.sh
```

### Risks

- `gamescopectl` is a developer tool, and its screenshot command could
  change between SteamOS releases. The probe script and the tests make a
  break quick to spot, and the Desktop Mode tools remain as fallbacks.
- Steam Input behaviour varies per game, so ElfPad results may differ
  from title to title.
