# Driving notes — an LLM at the controls

Practical notes from driving RetroArch (mGBA core, Pokémon FireRed) on a
Steam Deck through ElfKey, with the game viewed over a Sunshine/Moonlight
stream. Most of this transfers to any "screenshot → decide → press" loop.

## Why input is injected on the device

- Moonlight forwards mouse clicks from desktop-automation tools, but
  synthetic keyboard events sent at the Moonlight window never arrive at
  the host (verified with `evtest` on the host's virtual keyboard device:
  clicks appear, keys don't).
- xdotool can't reach native Wayland clients.
- So the keys are created on the device itself, via `/dev/uinput`, where
  the compositor and every app see them as a real keyboard.

## Running the daemon

- Run it as a **user service**, not a shell child — a plain backgrounded
  copy dies when its starting SSH session closes, and a later FIFO writer
  then blocks forever:

  ```
  systemd-run --user --unit=elfkey --collect python3 elfkey.py
  systemctl --user is-active elfkey.service
  grep ElfKey /proc/bus/input/devices
  ```

- The daemon opens its own FIFO `O_RDWR`, so writers never block waiting
  for a reader and the read end never sees EOF as writers come and go.
- Handy client patterns:
  - **Menus/dialogue:** taps of ~110 ms with ~0.75 s spacing.
  - **Walking:** holds of ~520 ms with ~0.62 s spacing (≈2 tiles per hold).

## Calibration (RetroArch + Pokémon)

- RetroArch default keyboard bindings: **A = X, B = Z**, X = S, Y = A,
  L = Q, R = W, **Start = Enter**, Select = Right Shift, D-pad = arrows.
  **Hold Space = fast-forward** — the single biggest speed lever; hold it
  through dialogue and scripted sequences.
- Walking: one tile ≈ 265 ms of hold at walk speed. A short tap in a new
  direction only *turns* the character; ~300 ms ≈ 1 tile, ~520 ms ≈ 2.
- Menus remember their cursor (the Start menu reopens on the last-used
  entry) — look before navigating; don't count presses blind.
- Battles are safe to batch: the command menu defaults to FIGHT and the
  move list remembers the last move, so a repeated pattern under
  fast-forward plays a known fight. Verify HP at checkpoints.
- Set the game's own options for driving: **TEXT SPEED = FAST,
  BATTLE SCENE = OFF**.
- Game quirks seen (FireRed/LeafGreen): location-name banners block
  movement until you step away or dismiss them; pressing A while facing an
  NPC restarts their speech; text-entry screens eat the first cursor move
  while they open.

## The loop, honestly

Round-trip latency dominates everything: capture + model + input ≈
8–15 s per look → decide → act cycle. Batch inputs per cycle, lean on
fast-forward for scripted stretches, and verify at scene boundaries
rather than per step. Turn-based and menu-driven games are a great fit;
anything real-time is not.
