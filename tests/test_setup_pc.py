"""setup-pc.sh end to end, against a fake `ssh` that plays the Deck: it
only accepts the key once it is in the fake Deck's authorized_keys, and runs
remote commands in a fake Deck home directory."""
import json
import os
import shutil
import subprocess

import pytest

from conftest import ROOT

pytestmark = pytest.mark.skipif(not shutil.which("ssh-keygen"),
                                reason="needs ssh-keygen")

FAKE_SSH = r'''#!/bin/sh
# Fake Deck. Logs in with, in order: the elfdeckai key once authorized; the
# agent (unless disabled), which may be a restricted forced-command key; a
# key file named id_full when FULL_KEY_OK; or the password unless KEY_ONLY.
ids="" ; agent=1 ; pubkey=1 ; batch=""
while [ $# -gt 0 ]; do
    case "$1" in
        -i) ids="$ids $2"; shift 2 ;;
        -o) case "$2" in
                IdentitiesOnly=yes|IdentityAgent=none) agent="" ;;
                PubkeyAuthentication=no) pubkey="" ; agent="" ;;
                BatchMode=yes) batch=1 ;;
            esac; shift 2 ;;
        -*) shift ;;
        *) break ;;
    esac
done
shift                                   # user@host
cmd="${*:-true}"
key="$(cat "$PC_HOME/.ssh/elfdeckai_ed25519.pub" 2>/dev/null)"
shell() { cd "$DECK_HOME" && HOME="$DECK_HOME" sh -c "$cmd"; exit $?; }
if [ -n "$pubkey" ]; then
    case "$ids" in *elfdeckai_ed25519*)
        grep -qxF "$key" "$DECK_HOME/.ssh/authorized_keys" 2>/dev/null && shell ;;
    esac
    if [ -n "$agent" ]; then
        [ -n "$RESTRICTED_AGENT" ] && { echo '{"ok": false, "error": "bad characters"}'; exit 1; }
        [ -n "$AGENT_KEY_OK" ] && shell
    fi
    case "$ids" in *id_full*) [ -n "$FULL_KEY_OK" ] && shell ;; esac
fi
[ -z "$KEY_ONLY" ] && [ -z "$batch" ] && shell   # typed the password
echo "deck@deck: Permission denied (publickey)." >&2
exit 255
'''


@pytest.fixture
def world(tmp_path):
    pc, deck, bin_dir = tmp_path / "pc", tmp_path / "deck", tmp_path / "bin"
    for d in (pc, deck / ".local/bin", bin_dir):
        d.mkdir(parents=True)
    server = deck / ".local/bin/elfdeckai-mcp"
    server.write_text("#!/bin/sh\n")
    server.chmod(0o755)
    (bin_dir / "ssh").write_text(FAKE_SSH)
    (bin_dir / "ssh").chmod(0o755)
    config = pc / ".config/Claude/claude_desktop_config.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}')

    def run(**extra):
        env = {"HOME": str(pc), "PC_HOME": str(pc), "DECK_HOME": str(deck),
               "PATH": "%s:%s" % (bin_dir, os.environ["PATH"]),
               "DECK_IP": "192.168.1.50", **extra}
        env.pop("XDG_CONFIG_HOME", None)
        return subprocess.run(["sh", str(ROOT / "setup-pc.sh")], env=env,
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=60)
    return run, pc, deck, config


def test_setup_connects_and_configures_claude_desktop(world):
    run, pc, deck, config = world
    r = run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "passwordless connection works" in r.stdout
    data = json.loads(config.read_text())
    assert data["theme"] == "dark" and "other" in data["mcpServers"]   # kept
    entry = data["mcpServers"]["elfdeckai"]
    assert entry["command"] == "ssh"
    assert entry["args"][-2:] == ["deck@192.168.1.50", ".local/bin/elfdeckai-mcp"]
    assert "BatchMode=yes" in entry["args"]
    assert entry["args"][1] == str(pc / ".ssh/elfdeckai_ed25519")
    assert (config.parent / "claude_desktop_config.json.bak").exists()


def test_setup_is_safe_to_rerun(world):
    run, _pc, deck, _config = world
    assert run().returncode == 0
    r = run()
    assert r.returncode == 0 and "already accepts this key" in r.stdout
    assert len((deck / ".ssh/authorized_keys").read_text().splitlines()) == 1


def test_setup_stops_when_elfdeckai_is_missing_on_the_deck(world):
    run, _pc, deck, config = world
    (deck / ".local/bin/elfdeckai-mcp").unlink()
    r = run()
    assert r.returncode != 0 and "not installed on the Deck" in r.stdout
    assert "elfdeckai" not in json.loads(config.read_text())["mcpServers"]


def test_key_only_deck_accepts_the_new_key_through_an_existing_one(world):
    run, _pc, deck, config = world
    r = run(KEY_ONLY="1", AGENT_KEY_OK="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (deck / ".ssh/authorized_keys").exists()
    assert "elfdeckai" in json.loads(config.read_text())["mcpServers"]


def test_key_only_deck_without_an_accepted_key_explains(world):
    run, pc, _deck, config = world
    r = run(KEY_ONLY="1")
    assert r.returncode != 0
    assert "No key on this PC can add the new one" in r.stdout
    assert (pc / ".ssh/elfdeckai_ed25519.pub").read_text().strip() in r.stdout
    assert "elfdeckai" not in json.loads(config.read_text())["mcpServers"]


def test_restricted_agent_key_falls_back_to_the_pcs_own_key_file(world):
    run, pc, deck, config = world
    (pc / ".ssh").mkdir(exist_ok=True)
    (pc / ".ssh/id_full").write_text("private key\n")
    r = run(KEY_ONLY="1", RESTRICTED_AGENT="1", FULL_KEY_OK="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "passwordless connection works" in r.stdout
    assert "elfdeckai" in json.loads(config.read_text())["mcpServers"]


def test_restricted_agent_key_and_nothing_else_explains(world):
    run, pc, _deck, config = world
    r = run(KEY_ONLY="1", RESTRICTED_AGENT="1")
    assert r.returncode != 0
    assert "No key on this PC can add the new one" in r.stdout
    assert (pc / ".ssh/elfdeckai_ed25519.pub").read_text().strip() in r.stdout
