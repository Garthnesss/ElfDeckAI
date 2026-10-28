# Roadmap

## Next

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
| Screenshots | **Works.** `gamescopectl screenshot <file> 4` returns exactly what is on screen (1280×800, about 300 ms). The server uses it first whenever gamescope's socket exists, and falls back to the Desktop Mode tools. |
| Keyboard | **Works.** ElfKey arrow presses moved the menu cursor in Stardew Valley. |
| Absolute mouse (`mouse_click_at`) | **Works.** Clicked LOAD on Stardew Valley's title screen. |
| Relative mouse | **Works.** A 100-unit move moved the cursor about 100 px. |
| Gamepad (ElfPad) | **Works once ElfPad is Player 1.** Many games only read Player 1. Moving ElfPad to the top in Steam's Reorder Controllers menu, while the game runs, fixes it (verified in Bastion), and the Deck's own controls keep working. The steps are in AI_SETUP_GUIDE.md. |
| Switching modes mid-session | **Works.** Gaming → Desktop with no restarts; screenshots worked on both sides. |

### Remaining work

1. **Automatic Player 1.** Steam's UI script has `SwapControllerOrder`, the
   same call its Reorder Controllers menu makes, and the controller list is
   in its `ControllerStore`. A tool built on them (commit `0de9df2`, reached
   through Steam's CEF remote debugging) changed the order reliably, but a
   running game kept listening to the pad it bound at launch: swapping
   mid-game left neither the agent nor the player in control, and swapping
   before launch locked the player out of the menus. The overlay menu does
   something more that makes a running game re-bind; finding that is what
   is left before this can be automated.
2. **Docked to a large display.** gamescope writes PNG, which the server
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
