#!/bin/sh
# Gaming Mode probe for ElfDeckAI. Run it on a Steam Deck while it is in
# Gaming Mode, over SSH from a PC:
#
#   ssh deck@DECK-IP 'sh -s' < scripts/probe-gaming-mode.sh
#
# It changes nothing: it reads the session environment, checks for the
# tools a Gaming Mode screenshot would need, takes one test screenshot to
# /tmp, and lists the virtual input devices. Paste the whole output back to
# whoever is working on Gaming Mode support (see ROADMAP.md).
set -u

section() { printf '\n== %s\n' "$1"; }

section "SteamOS"
grep -E '^(NAME|VERSION_ID|BUILD_ID)=' /etc/os-release 2>/dev/null

section "Session processes"
for p in gamescope gamescope-wl steam kwin_wayland plasmashell; do
    if pgrep -x "$p" >/dev/null 2>&1; then echo "$p: running"; else echo "$p: -"; fi
done

section "Graphical environment known to systemd --user"
systemctl --user show-environment 2>/dev/null \
    | grep -E '^(DISPLAY|WAYLAND_DISPLAY|GAMESCOPE_WAYLAND_DISPLAY|XDG_RUNTIME_DIR|XDG_CURRENT_DESKTOP|DBUS_SESSION_BUS_ADDRESS)=' \
    || echo "(none)"

runtime="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
section "Wayland sockets in $runtime"
found=""
for sock in "$runtime"/gamescope* "$runtime"/wayland*; do
    [ -e "$sock" ] && { echo "${sock##*/}"; found=1; }
done
[ -n "$found" ] || echo "(none)"

section "Capture tools"
for t in gamescopectl spectacle grim import scrot ffmpeg; do
    printf '%s: %s\n' "$t" "$(command -v "$t" || echo -)"
done

section "gamescopectl info"
gs_display="$(systemctl --user show-environment 2>/dev/null \
    | sed -n 's/^GAMESCOPE_WAYLAND_DISPLAY=//p')"
if [ -z "$gs_display" ]; then
    for sock in "$runtime"/gamescope-[0-9]*; do
        case "$sock" in *.lock) continue;; esac
        [ -S "$sock" ] && { gs_display="${sock##*/}"; break; }
    done
fi
echo "using GAMESCOPE_WAYLAND_DISPLAY=${gs_display:-<unknown>}"
if command -v gamescopectl >/dev/null 2>&1; then
    XDG_RUNTIME_DIR="$runtime" GAMESCOPE_WAYLAND_DISPLAY="$gs_display" \
        timeout 5 gamescopectl 2>&1 | head -n 30
fi

section "Test screenshot (gamescopectl, screen buffer)"
shot=/tmp/elfdeckai-probe.png
rm -f "$shot"
if command -v gamescopectl >/dev/null 2>&1; then
    XDG_RUNTIME_DIR="$runtime" GAMESCOPE_WAYLAND_DISPLAY="$gs_display" \
        timeout 5 gamescopectl screenshot "$shot" 4 2>&1 | head -n 5
    i=0
    while [ ! -s "$shot" ] && [ "$i" -lt 30 ]; do sleep 0.1; i=$((i + 1)); done
    if [ -s "$shot" ]; then
        echo "OK: $(wc -c < "$shot") bytes after ~$((i * 100)) ms"
        # shellcheck disable=SC2046
        set -- $(od -An -tx1 -j16 -N8 "$shot")
        echo "PNG size: $((0x$1$2$3$4)) x $((0x$5$6$7$8))"
    else
        echo "no file written within 3 s"
    fi
else
    echo "skipped: gamescopectl not installed"
fi

section "Steam screenshot folders (fallback route)"
ls -d "$HOME"/.local/share/Steam/userdata/*/760/remote 2>/dev/null || echo "(none)"

section "Virtual input devices"
grep -E '^N: Name="(ElfKey|ElfPad|ElfMouse|ElfMouse Absolute)"' /proc/bus/input/devices || echo "(none: are the services running?)"
systemctl --user is-active elfkey elfpad elfmouse 2>/dev/null | paste -sd ' ' -
