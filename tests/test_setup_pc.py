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
while [ $# -gt 0 ]; do
    case "$1" in -i|-o) shift 2;; -*) shift;; *) break;; esac
done
shift                                   # user@host
cmd="${*:-true}"
key="$(cat "$PC_HOME/.ssh/elfdeckai_ed25519.pub" 2>/dev/null)"
if ! grep -qxF "$key" "$DECK_HOME/.ssh/authorized_keys" 2>/dev/null; then
    case "$cmd" in
        *authorized_keys*)                  # the copy step: password, or another key
            if [ -n "$KEY_ONLY" ] && [ -z "$AGENT_KEY_OK" ]; then
                echo "deck@deck: Permission denied (publickey)." >&2
                exit 255
            fi ;;
        *) exit 255 ;;
    esac
fi
cd "$DECK_HOME" && HOME="$DECK_HOME" sh -c "$cmd"
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
    assert "only accepts SSH keys" in r.stdout
    assert (pc / ".ssh/elfdeckai_ed25519.pub").read_text().strip() in r.stdout
    assert "elfdeckai" not in json.loads(config.read_text())["mcpServers"]
