#!/bin/sh
# ElfDeckAI one-paste installer for a Steam Deck (or any Linux box).
#
# On the Deck: switch to Desktop Mode, open Konsole, paste:
#
#   curl -fsSL https://raw.githubusercontent.com/Garthnesss/ElfDeckAI/main/install-on-deck.sh | sh
#
# What it does, all inside your home directory (no sudo, nothing system-wide):
#   1. Installs the elfdeckai package for your user (pip), or — if pip is
#      unavailable — downloads the four single-file programs directly.
#   2. Installs and starts three systemd *user* services: elfkey, elfpad,
#      elfmouse (the virtual keyboard / gamepad / mouse).
#   3. Prints the exact MCP config block to paste into your AI client on
#      your PC, and the two commands that turn on SSH if you want them.
#
# Safe to re-run: it upgrades in place and restarts the services.
set -eu

REPO_RAW="https://raw.githubusercontent.com/Garthnesss/ElfDeckAI/main"
BIN_DIR="$HOME/.local/bin"
LIB_DIR="$HOME/.local/lib/elfdeckai"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$BIN_DIR" "$LIB_DIR" "$UNIT_DIR"

echo "==> Installing ElfDeckAI"
if python3 -m pip --version >/dev/null 2>&1; then
    python3 -m pip install --user --upgrade elfdeckai 2>/dev/null \
        || python3 -m pip install --user --upgrade --break-system-packages elfdeckai
else
    echo "    (pip not found — fetching the single-file programs directly)"
    for f in elfkey.py elfpad.py elfmouse.py elfdeckai_mcp.py; do
        curl -fsSL "$REPO_RAW/$f" -o "$LIB_DIR/$f"
    done
    for pair in "elfkey:elfkey.py" "elfpad:elfpad.py" \
                "elfmouse:elfmouse.py" "elfdeckai-mcp:elfdeckai_mcp.py"; do
        cmd="${pair%%:*}"; file="${pair##*:}"
        printf '#!/bin/sh\nexec python3 "%s/%s" "$@"\n' "$LIB_DIR" "$file" \
            > "$BIN_DIR/$cmd"
        chmod +x "$BIN_DIR/$cmd"
    done
fi

echo "==> Installing services"
for pair in "elfkey:ElfKey virtual keyboard" \
            "elfpad:ElfPad virtual gamepad" \
            "elfmouse:ElfMouse virtual mouse"; do
    cmd="${pair%%:*}"; desc="${pair##*:}"
    cat > "$UNIT_DIR/$cmd.service" <<EOF
[Unit]
Description=$desc daemon (ElfDeckAI)
After=graphical-session.target

[Service]
ExecStart=$BIN_DIR/$cmd
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF
done
systemctl --user daemon-reload
systemctl --user enable --now elfkey.service elfpad.service elfmouse.service
sleep 2

echo "==> Checking the virtual devices"
ok=1
for name in ElfKey ElfPad ElfMouse; do
    if grep -q "Name=\"$name\"" /proc/bus/input/devices; then
        echo "    $name: up"
    else
        echo "    $name: NOT VISIBLE — check: systemctl --user status ${name} | head"
        ok=0
    fi
done
[ "$ok" = 1 ] || echo "    (On non-Deck Linux you may need the udev rule from the repo: 70-elfdeckai-uinput.rules (repo root))"

IP="$(ip -4 -o addr show scope global 2>/dev/null | awk '{split($4,a,"/"); print a[1]; exit}')"
[ -n "$IP" ] || IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

Done. Your Deck now has hands. To give your AI on your PC control:

1) Turn on SSH on this Deck (lets your PC reach it, LAN only) — run:
       passwd                  # set a password for the deck user, once
       systemctl enable --now sshd

2) On your PC, add this to your AI client's MCP config
   (Claude Desktop: Settings -> Developer -> Edit Config;
    the file is claude_desktop_config.json):

   {"mcpServers": {"elfdeckai": {
       "command": "ssh",
       "args": ["deck@${IP:-YOUR-DECK-IP}", "$BIN_DIR/elfdeckai-mcp"]}}}

3) Restart the AI client, start a game on the Deck (Desktop Mode),
   and ask your AI to take a screenshot — if it can see your Deck,
   everything works.

Full guide, including a file you can hand to your AI so IT explains
all of this to you: see AI_SETUP_GUIDE.md in the repo.
EOF
