#!/bin/sh
# ElfDeckAI PC setup for macOS and Linux: connects your AI client to the
# Steam Deck in one step. Run on the PC (not the Deck) after the Deck
# installer has finished:
#
#   curl -fsSL https://raw.githubusercontent.com/Garthnesss/ElfDeckAI/main/setup-pc.sh | sh
#
# What it does:
#   1. Creates an SSH key just for ElfDeckAI (~/.ssh/elfdeckai_ed25519).
#   2. Copies it to the Deck; you type the Deck password once.
#   3. Checks the connection works without a password.
#   4. Adds ElfDeckAI to Claude Desktop's config (keeping a backup), or
#      prints the config block for other AI clients.
#
# Safe to re-run. Options: DECK_IP=... DECK_USER=... (default: deck).
set -eu

KEY="$HOME/.ssh/elfdeckai_ed25519"
DECK_USER="${DECK_USER:-deck}"
REMOTE_CMD=".local/bin/elfdeckai-mcp"

ask() {  # stdin may be the piped script, so read the answer from the terminal
    printf '%s' "$1" > /dev/tty
    read -r answer < /dev/tty
    printf '%s' "$answer"
}

say() { printf '%s\n' "$*"; }

for tool in ssh ssh-keygen; do
    command -v "$tool" >/dev/null 2>&1 || { say "$tool is not installed on this PC (install OpenSSH)."; exit 1; }
done

say "==> ElfDeckAI PC setup"
DECK_IP="${DECK_IP:-${1:-}}"
[ -n "$DECK_IP" ] || DECK_IP="$(ask "Deck IP address (the Deck installer printed it): ")"
[ -n "$DECK_IP" ] || { say "No IP address given."; exit 1; }
TARGET="$DECK_USER@$DECK_IP"

say "==> SSH key"
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
if [ -f "$KEY" ]; then
    say "    using existing $KEY"
else
    ssh-keygen -q -t ed25519 -N "" -C "elfdeckai" -f "$KEY"
    say "    created $KEY"
fi

deck_ssh() {
    ssh -i "$KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "$@"
}

# Appends the new public key (read from stdin) on the Deck and prints a
# marker, so an attempt only counts when the command really ran there.
# shellcheck disable=SC2016  # expanded on the Deck, not here
COPY_CMD='mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys &&
    chmod 600 ~/.ssh/authorized_keys && read -r key &&
    { grep -qxF "$key" ~/.ssh/authorized_keys || echo "$key" >> ~/.ssh/authorized_keys; } &&
    echo ELFDECKAI-KEY-ADDED'
ERRORS="$(mktemp)"
AGENT_DIR="$(mktemp -d)"
REFUSED=""
trap 'rm -rf "$ERRORS" "$AGENT_DIR"' EXIT

try_copy() {
    out="$(ssh "$@" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 \
        "$TARGET" "$COPY_CMD" < "$KEY.pub" 2>>"$ERRORS")" || true
    case "$out" in *ELFDECKAI-KEY-ADDED*) return 0 ;; esac
    if [ -n "$out" ]; then   # logged in, but the account ran something else
        REFUSED=1
        printf '%s\n' "$out" >> "$ERRORS"
    fi
    return 1
}

copy_key() {
    # 1. This PC's usual keys and ssh-agent, then the Deck's password.
    try_copy && return 0
    # A key can log in yet be limited to one command on the Deck (a forced
    # command), which stops step 1 before any other key is tried. So:
    # 2. Each key held by the ssh-agent on its own (the agent signs; the
    #    public key file only picks which identity is offered).
    if ssh-add -L > "$AGENT_DIR/keys" 2>/dev/null; then
        n=0
        while read -r line; do
            n=$((n + 1))
            printf '%s\n' "$line" > "$AGENT_DIR/key$n.pub"
            try_copy -i "$AGENT_DIR/key$n.pub" -o IdentitiesOnly=yes && return 0
        done < "$AGENT_DIR/keys"
    fi
    # 3. Each of this PC's own key files in ~/.ssh on its own.
    for f in "$HOME"/.ssh/id_*; do
        case "$f" in *.pub|"$KEY") continue ;; esac
        [ -f "$f" ] || continue
        try_copy -i "$f" -o IdentitiesOnly=yes -o IdentityAgent=none && return 0
    done
    # 4. The Deck's password only.
    try_copy -o PubkeyAuthentication=no
}

if deck_ssh -o BatchMode=yes -o ConnectTimeout=8 "$TARGET" true 2>/dev/null; then
    say "    the Deck already accepts this key"
else
    say "==> Copying the key to the Deck (enter the Deck's password if asked)"
    if ! copy_key; then
        say ""
        if grep -q "Permission denied" "$ERRORS" || [ -n "$REFUSED" ]; then
            say "No key on this PC can add the new one to the Deck, and the Deck does not"
            say "accept a password. Add this line to ~/.ssh/authorized_keys on the Deck (in"
            say "Konsole on the Deck, or from a computer that can already log in), then run"
            say "this again:"
            say ""
            say "  $(cat "$KEY.pub")"
        else
            sed 's/^/    /' "$ERRORS"
            say "Could not reach $TARGET. Check that:"
            say "  - the Deck is on and on the same network, and the IP is right"
            say "  - SSH is on: on the Deck run 'passwd' once, then 'sudo systemctl enable --now sshd'"
        fi
        exit 1
    fi
fi

say "==> Checking the connection"
if ! deck_ssh -o BatchMode=yes -o ConnectTimeout=8 "$TARGET" "test -x $REMOTE_CMD"; then
    say "    Connected, but ElfDeckAI is not installed on the Deck."
    say "    On the Deck, in Konsole, run:"
    say "    curl -fsSL https://raw.githubusercontent.com/Garthnesss/ElfDeckAI/main/install-on-deck.sh | sh"
    exit 1
fi
say "    OK: passwordless connection works and ElfDeckAI is installed"

ARGS_JSON="[\"-i\", \"$KEY\", \"-o\", \"IdentitiesOnly=yes\", \"-o\", \"BatchMode=yes\", \"$TARGET\", \"$REMOTE_CMD\"]"

case "$(uname -s)" in
    Darwin) CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json" ;;
    *)      CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/Claude/claude_desktop_config.json" ;;
esac

configure() {
    # Merge {"mcpServers": {"elfdeckai": ...}} into Claude Desktop's config,
    # keeping every other setting. On macOS, JavaScript for Automation is
    # always there, while python3 may be a stub that pops up an "install
    # developer tools" dialog; elsewhere python3 does the merge.
    mkdir -p "$(dirname "$CONFIG")"
    [ -f "$CONFIG" ] && cp "$CONFIG" "$CONFIG.bak"
    if [ "$(uname -s)" = Darwin ] && command -v osascript >/dev/null 2>&1; then
        osascript -l JavaScript - "$CONFIG" "$ARGS_JSON" <<'EOF'
function run(argv) {
  ObjC.import('Foundation');
  const path = argv[0], fm = $.NSFileManager.defaultManager;
  let config = {};
  if (fm.fileExistsAtPath(path)) {
    const text = $.NSString.stringWithContentsOfFileEncodingError(path, $.NSUTF8StringEncoding, null).js;
    if (text && text.trim()) config = JSON.parse(text);
  }
  config.mcpServers = config.mcpServers || {};
  config.mcpServers.elfdeckai = {command: 'ssh', args: JSON.parse(argv[1])};
  $(JSON.stringify(config, null, 2) + '\n').writeToFileAtomicallyEncodingError(path, true, $.NSUTF8StringEncoding, null);
}
EOF
    elif command -v python3 >/dev/null 2>&1 && python3 -c 'import json' 2>/dev/null; then
        python3 - "$CONFIG" "$ARGS_JSON" <<'EOF'
import json, os, sys
path, args = sys.argv[1], json.loads(sys.argv[2])
config = {}
if os.path.exists(path) and os.path.getsize(path):
    with open(path) as f:
        config = json.load(f)
config.setdefault("mcpServers", {})["elfdeckai"] = {"command": "ssh", "args": args}
with open(path, "w") as f:
    json.dump(config, f, indent=2)
    f.write("\n")
EOF
    else
        return 1
    fi
}

say "==> Configuring your AI client"
if configure 2>/dev/null; then
    say "    Added ElfDeckAI to Claude Desktop: $CONFIG"
    [ -f "$CONFIG.bak" ] && say "    (previous config saved as $CONFIG.bak)"
else
    say "    Could not edit Claude Desktop's config automatically."
fi

cat <<EOF

Done. Restart Claude Desktop, then ask it: "take a screenshot of my Deck".

Using a different AI client? Add this MCP server to it:

  {"mcpServers": {"elfdeckai": {"command": "ssh", "args": $ARGS_JSON}}}

Claude Code:
  claude mcp add elfdeckai -- ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes $TARGET $REMOTE_CMD
EOF
