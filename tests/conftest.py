"""Load the repo's modules straight from the repo root (no packaging: the
daemons are single files you copy around, and the tests import them the
same way)."""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


elfkey = _load("elfkey")
elfpad = _load("elfpad")
