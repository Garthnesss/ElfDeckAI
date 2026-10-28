#!/usr/bin/env python3
"""Steam controller API probe for ElfDeckAI.

Lists what Steam's UI script exposes for controllers on this SteamOS build,
so pad_make_player_one can call the right functions. Read-only: it only
looks up names and calls nothing that changes Steam.

Run it on the Deck in Gaming Mode (with a game running, so the controller
list is populated), after ElfDeckAI is installed:

    curl -fsSL https://raw.githubusercontent.com/Garthnesss/ElfDeckAI/main/scripts/probe-steam-input.py | python3 -

It needs Steam's CEF remote debugging (on when Decky Loader is installed).
"""
import json
import os
import sys

for path in (os.path.expanduser("~/.local/lib/elfdeckai"),
             os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "..")):
    sys.path.insert(0, path)
import elfdeckai_mcp  # noqa: E402

PROBES = [
    ("SteamClient.Input functions (own and inherited)", """(() => {
        const names = new Set();
        for (let o = SteamClient.Input; o && o !== Object.prototype; o = Object.getPrototypeOf(o))
            Object.getOwnPropertyNames(o).forEach(n => names.add(n));
        return [...names].sort();
    })()"""),
    ("Controller-related names in other SteamClient namespaces", """(() => {
        const out = {};
        for (const ns of Object.keys(SteamClient)) {
            const names = new Set();
            for (let o = SteamClient[ns]; o && typeof o === "object" && o !== Object.prototype;
                 o = Object.getPrototypeOf(o))
                Object.getOwnPropertyNames(o).filter(n => /controller|gamepad|slot|order/i.test(n))
                    .forEach(n => names.add(n));
            if (names.size && ns !== "Input") out[ns] = [...names].sort();
        }
        return out;
    })()"""),
    ("Controller-related globals in the UI window", """Object.keys(window)
        .filter(k => /controller|gamepad|input/i.test(k)).sort()"""),
    ("SteamClient.Input.SwapControllerOrder", """typeof SteamClient.Input.SwapControllerOrder"""),
]


def main():
    for title, expression in PROBES:
        print("== %s" % title)
        try:
            print(json.dumps(elfdeckai_mcp._cef_evaluate(expression), indent=1))
        except Exception as e:  # report and keep going
            print("ERROR: %s" % e)
        print()


if __name__ == "__main__":
    main()
