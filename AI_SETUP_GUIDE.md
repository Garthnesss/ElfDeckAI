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
   systemctl enable --now sshd
   ```

## Phase 2 — the PC (about 3 minutes)

1. Ask which AI client they use and give them the matching instruction:
   - **Claude Desktop:** Settings → Developer → Edit Config opens
     `claude_desktop_config.json`. Add the `elfdeckai` block the installer
     printed. If they lost it, the template is:

     ```json
     {"mcpServers": {"elfdeckai": {
         "command": "ssh",
         "args": ["deck@DECK-IP-ADDRESS", "/home/deck/.local/bin/elfdeckai-mcp"]}}}
     ```

   - **Claude Code:** `claude mcp add elfdeckai -- ssh deck@DECK-IP /home/deck/.local/bin/elfdeckai-mcp`
   - **Codex / other MCP clients:** same idea — the "server" is a command:
     `ssh deck@DECK-IP /home/deck/.local/bin/elfdeckai-mcp`.
2. **Set up key-based SSH login (required).** The AI client starts `ssh`
   in the background, where nobody can type a password, so a password
   prompt makes the server hang or fail to start. `ssh` is built into
   Windows 10/11, macOS and Linux. On the PC:

   ```sh
   ssh-keygen -t ed25519          # skip if ~/.ssh/id_ed25519 already exists; Enter accepts the defaults
   ssh-copy-id deck@DECK-IP       # macOS / Linux
   ```

   On Windows (PowerShell), where `ssh-copy-id` is not available:

   ```powershell
   type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh deck@DECK-IP "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
   ```

   These ask for the Deck password one last time. Then verify:
   `ssh deck@DECK-IP echo ok` must print `ok` **without** a password
   prompt (answer `yes` to the host-key question the first time). Do not
   continue until it does.
3. Restart the AI client so it picks up the new server.

## Phase 3 — verify, then play

1. **Verify vision first:** have the AI (that's you, if you're the client
   with the tools connected) call `screenshot`. Success = an image of the
   Deck's desktop comes back. If it errors, see Troubleshooting.
2. **Verify hands:** open any text field or menu on the Deck and call
   `key_tap` with `key: "x"`, or in a game menu call `pad_press` with
   `button: "a"`. The human confirms the Deck reacted.
3. **Play:** the human launches a game **in Desktop Mode** (screenshot
   capture needs the desktop session; Gaming Mode is not supported for
   vision yet). Then you drive: screenshot → act → screenshot. Keep holds
   short (menu taps ~100 ms). If anything ever feels stuck, call
   `key_release`, `pad_reset` and `mouse_release` — they exist exactly for
   that, and every hold auto-releases within 5 seconds regardless.

## Honest limits (tell your human these)

- The loop thinks in screenshots — roughly a second or more per move.
  Turn-based games, RPGs, visual novels, point-and-click, menus and
  settings screens: excellent. Twitch action, platformers, shooters,
  racing: the AI will lose, and that's a physics problem, not a setup
  problem.
- Vision is Desktop Mode only for now.
- Anyone who can log in to the Deck over SSH can send inputs. That's the
  same trust level as SSH itself; don't expose SSH to the internet.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Tool error: "daemon not running" | A service stopped | On the Deck: `systemctl --user restart elfkey elfpad elfmouse` |
| Tool error: "permission denied on the FIFO" | MCP server running as a different user than the daemons | Run everything as the `deck` user |
| `screenshot` errors | Deck is in Gaming Mode, or no graphical session | Switch to Desktop Mode |
| MCP client can't start the server, or it times out | SSH is asking for a password the client can't type | Set up key-based login (Phase 2, step 2) and check `ssh deck@DECK-IP echo ok` prints `ok` with no prompt |
| MCP client can't start the server | Wrong Deck IP (DHCP changed it) | On the Deck, `ip -4 addr show scope global` shows the current one; update the config |
| `elfdeckai-mcp`: command not found over SSH | PATH over SSH is minimal | Use the absolute path from the installer output (`/home/deck/.local/bin/elfdeckai-mcp`) |
| A key/button seems stuck | A caller died mid-hold | `key_release`, `mouse_release`, or `pad_reset`. Holds also self-release ≤ 5 s |
| Game ignores `pad_press` but responds to keys | Steam gives the controller slot to the Deck's built-in pad, so the virtual gamepad gets no slot | Use the keyboard instead: arrow keys and Enter via `key_tap`. Example: in Dragon Quest VII, ElfPad presses had no effect, while arrows moved the character and Enter confirmed |
| Virtual devices missing after install (non-Deck Linux) | No `/dev/uinput` permission | Install the repo's `70-elfdeckai-uinput.rules` into `/etc/udev/rules.d/`, replug/reboot |

## Uninstall

On the Deck:

```sh
systemctl --user disable --now elfkey elfpad elfmouse
python3 -m pip uninstall elfdeckai
```

That's everything — the install never touched the system outside the
user's home directory (plus the optional SSH service, which SteamOS
itself provides: `systemctl disable --now sshd` undoes that part).
