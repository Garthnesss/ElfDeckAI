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
    ("window.ControllerStore: every field, with lists and maps summarised", """(() => {
        const store = window.ControllerStore, out = {};
        if (!store) return "no ControllerStore";
        const flat = v => {               // an object's plain fields only
            if (!v || typeof v !== "object") return v;
            const r = {};
            for (const k of Object.keys(v)) {
                const x = v[k];
                if (x === null || ["string", "number", "boolean"].includes(typeof x)) r[k] = x;
                else r[k] = Array.isArray(x) ? "[array " + x.length + "]" : "<" + typeof x + ">";
            }
            return r;
        };
        const names = new Set();
        for (let o = store; o && o !== Object.prototype; o = Object.getPrototypeOf(o))
            Object.getOwnPropertyNames(o).forEach(n => names.add(n));
        for (const name of [...names].sort()) {
            try {
                const v = store[name];
                if (typeof v === "function") { out[name] = "function"; continue; }
                if (v && typeof v === "object" && typeof v.values === "function" && "size" in v)
                    out[name] = {map: v.size, entries: Array.from(v.entries()).slice(0, 8)
                                 .map(([k, x]) => [k, flat(x)])};
                else if (Array.isArray(v))
                    out[name] = {array: v.length, items: v.slice(0, 8).map(flat)};
                else if (v && typeof v === "object")
                    out[name] = {object: flat(v)};
                else out[name] = v;
            } catch (e) { out[name] = "error: " + e.message; }
        }
        return out;
    })()"""),
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
