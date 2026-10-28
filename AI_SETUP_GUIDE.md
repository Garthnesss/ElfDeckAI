# ElfDeckAI setup guide — written for an AI to read

**Human reading this:** you don't have to. Give this file (or its URL) to
your AI — Claude, Codex, ChatGPT, any assistant that can read a document —
and say *"walk me through setting this up."* It knows what to do.

**AI reading this:** your job is to get your human from zero to "an AI is
playing a game on their Steam Deck" with the least friction possible. Ask
short questions, give one step at a time, and verify each step before
moving on. Everything below is written for you.

---

## What ElfDeckAI is

Three small daemons run **on the Steam Deck** and create virtual input
devices at the Linux kernel level (`/dev/uinput`): a keyboard (ElfKey), an
Xbox-360-class gamepad (ElfPad), and a mouse (ElfMouse). Games cannot tell
them apart from real hardware. A fourth program, an MCP server, exposes
them — plus a `screenshot` tool that returns a picture of the Deck's
screen — as 21 tools an AI can call. The AI's loop is: `screenshot` →
decide → `pad_press` / `key_tap` / `mouse_click_at` → `screenshot` again.
For the mouse, prefer `mouse_click_at` / `mouse_move_to`: they take x, y
in the pixels of the last screenshot, so clicks land where you aimed.

The AI client (Claude Desktop, Claude Code, Codex, …) usually runs on the
human's **PC**. It reaches the Deck's MCP server over SSH on the home
network. Nothing leaves the LAN except the AI's normal cloud traffic.

## Phase 1 — the Deck (about 2 minutes)

1. Ask the human to switch the Deck to **Desktop Mode** (Steam button →
   Power → Switch to Desktop) and open **Konsole** (the terminal).
2. Have them paste this one line. It installs everything as user-level
   services — no sudo, nothing system-wide, fully removable:

   ```sh
   curl -fsSL https://raw.githubusercontent.com/Garthnesss/ElfDeckAI/main/install-on-deck.sh | sh
   ```

3. The script ends by checking that `ElfKey`, `ElfPad` and `ElfMouse` are
   visible as input devices and prints an MCP config block containing the
   Deck's current IP address. Ask the human to paste you that output (or
   confirm all three say "up").
4. SSH: the script prints these two commands but does **not** run them.
   Explain why SSH is needed (it's how the PC-side AI reaches the Deck),
   then have the human run them in the same Konsole:

   ```sh
   passwd                        # one-time: set the deck user's password
   sudo systemctl enable --now sshd
   ```

## Phase 2 — the PC (about 3 minutes)

1. Have the human paste one line on the PC. It creates an SSH key just for
   ElfDeckAI, copies it to the Deck (they type the Deck password once),
   checks that the connection works without a password, and adds
   ElfDeckAI to Claude Desktop's config, keeping a backup and their other
   settings.
   - **Windows**, in PowerShell:
     `irm https://raw.githubusercontent.com/Garthnesss/ElfDeckAI/main/setup-pc.ps1 | iex`
   - **Mac or Linux**, in Terminal:
     `curl -fsSL https://raw.githubusercontent.com/Garthnesss/ElfDeckAI/main/setup-pc.sh | sh`

   It asks for the Deck's IP address (the Deck installer printed it). At
   the end it also prints the config block for other MCP clients and the
   `claude mcp add` command for Claude Code. `ssh` is built into Windows
   10/11, macOS and Linux; on Windows, if it is missing, it is under
   Settings → System → Optional features → OpenSSH Client.
2. Restart the AI client so it picks up the new server. On Windows, quit
   Claude Desktop from the system tray, not just the window.

**If the script cannot be used,** do the same by hand. Key-based login is
required, because the AI client starts `ssh` in the background where nobody
can type a password:

1. `ssh-keygen -t ed25519 -N "" -f ~/.ssh/elfdeckai_ed25519`
2. Copy `~/.ssh/elfdeckai_ed25519.pub` into `~/.ssh/authorized_keys` on the
   Deck (`ssh-copy-id -i ~/.ssh/elfdeckai_ed25519 deck@DECK-IP` on Mac and
   Linux).
3. Check that `ssh -i ~/.ssh/elfdeckai_ed25519 deck@DECK-IP echo ok`
   prints `ok` with no password prompt.
4. Add this to the client's MCP config (Claude Desktop: Settings →
   Developer → Edit Config), using the full path to the key:

   ```json
   {"mcpServers": {"elfdeckai": {
       "command": "ssh",
       "args": ["-i", "/full/path/to/.ssh/elfdeckai_ed25519", "-o", "IdentitiesOnly=yes",
                "-o", "BatchMode=yes", "deck@DECK-IP", ".local/bin/elfdeckai-mcp"]}}}
   ```

## Phase 3 — verify, then play

1. **Verify vision first:** have the AI (that's you, if you're the client
   with the tools connected) call `screenshot`. Success = an image of the
   Deck's desktop comes back. If it errors, see Troubleshooting.
2. **Verify hands:** open any text field or menu on the Deck and call
   `key_tap` with `key: "x"`, or in a game menu call `pad_press` with
   `button: "a"`. The human confirms the Deck reacted.
3. **Play:** the human launches a game, in Desktop Mode or Gaming Mode.
   Then you drive: screenshot → act → screenshot. Keep holds
   short (menu taps ~100 ms). If anything ever feels stuck, call
   `key_release`, `pad_reset` and `mouse_release` — they exist exactly for
   that, and every hold auto-releases within 5 seconds regardless.

## Honest limits (tell your human these)

- The loop thinks in screenshots — roughly a second or more per move.
  Turn-based games, RPGs, visual novels, point-and-click, menus and
  settings screens: excellent. Twitch action, platformers, shooters,
  racing: the AI will lose, and that's a physics problem, not a setup
  problem.
- Gaming Mode works for screenshots, keyboard, mouse and gamepad. For the
  gamepad, ElfPad usually has to be made Player 1 first; see below.
- Anyone who can log in to the Deck over SSH can send inputs. That's the
  same trust level as SSH itself; don't expose SSH to the internet.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Tool error: "daemon not running" | A service stopped | On the Deck: `systemctl --user restart elfkey elfpad elfmouse` |
| Tool error: "permission denied on the FIFO" | MCP server running as a different user than the daemons | Run everything as the `deck` user |
| `screenshot` errors | No graphical session (the Deck is at the login screen or asleep), or Gaming Mode on an older ElfDeckAI | Wake the Deck; update with the installer; or switch to Desktop Mode |
| MCP client can't start the server, or it times out | SSH is asking for a password the client can't type | Re-run the PC setup script from Phase 2; it re-checks the key and the connection |
| MCP client can't start the server | Wrong Deck IP (DHCP changed it) | On the Deck, `ip -4 addr show scope global` shows the current one; update the config |
| `elfdeckai-mcp`: command not found over SSH | PATH over SSH is minimal | Use the absolute path from the installer output (`/home/deck/.local/bin/elfdeckai-mcp`) |
| A key/button seems stuck | A caller died mid-hold | `key_release`, `mouse_release`, or `pad_reset`. Holds also self-release ≤ 5 s |
| Game ignores `pad_press` but responds to keys (Gaming Mode) | Steam gives each controller its own player slot, the Deck's own controls are Player 1, and the game only listens to Player 1 | Make ElfPad Player 1 (see "Using the gamepad in Gaming Mode"). Or use the keyboard: arrow keys and Enter via `key_tap` |
| Virtual devices missing after install (non-Deck Linux) | No `/dev/uinput` permission | Install the repo's `70-elfdeckai-uinput.rules` into `/etc/udev/rules.d/`, replug/reboot |

## Using the gamepad in Gaming Mode

In Gaming Mode, Steam gives each controller its own player slot. The Deck's
own controls are Player 1, ElfPad is Player 2, and many games only listen
to Player 1. Before driving a game with `pad_press`, make ElfPad Player 1.
You can do this yourself:

1. With the game running, open the Steam menu: `pad_press` with
   `button: "guide"` (or ask the human to press the Steam button).
2. Open **Controller Settings**, then **Reorder Controllers**. This option
   only appears while a game is running.
3. Using `screenshot` and `mouse_click_at`, move the controller that is
   **not** the Steam Deck to the top (Player 1), then close the menu.
4. Check with one `pad_press` and a `screenshot`.

While ElfPad is Player 1, the game ignores the Deck's own controls. When
the human wants to play again, move the Steam Deck back to the top the same
way. Verified in Bastion; the same cause explains games such as Dragon
Quest VII ignoring the pad.

## Uninstall

On the Deck:

```sh
systemctl --user disable --now elfkey elfpad elfmouse
python3 -m pip uninstall elfdeckai
```

That's everything — the install never touched the system outside the
user's home directory (plus the optional SSH service, which SteamOS
itself provides: `systemctl disable --now sshd` undoes that part).
