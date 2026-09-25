"""Command implementations and argument dispatch for ``cs``.

The implementation is split across submodules; this package re-exports every
symbol so ``from cs.cli import run`` and ``import cs.cli as cli; cli._foo``
keep working unchanged. Functions are lifted into this package's namespace so
tests that patch ``cs.cli.*`` still affect the code paths that look those
names up as globals.
"""

from __future__ import annotations

# Intentionally re-exported onto ``cs.cli`` for callers/tests that patch or
# read ``cli.shutil``, ``cli.db``, etc. — used by lifted function globals.
import os  # noqa: F401
import re  # noqa: F401
import shutil  # noqa: F401
import sqlite3  # noqa: F401
import statistics  # noqa: F401
import subprocess  # noqa: F401
import sys  # noqa: F401
import textwrap  # noqa: F401
import time  # noqa: F401
import types
from collections import Counter  # noqa: F401
from datetime import date  # noqa: F401
from pathlib import Path  # noqa: F401

from .. import (  # noqa: F401
    __version__,
    context,
    db,
    events,
    export,
    hooks,
    mcp,
    practice,
    redact,
    signals,
    ui,
)

# Submodules in dependency order. Importing them defines the implementation;
# we then lift callables into this package's globals for patch compatibility.
from . import (  # noqa: E402
    _common,
    dispatch,
    evidence,
    governance,
    home,
    inventory,
    listing,
    practice_cmds,
    reports,
    resume,
    session,
    today,
    workflow,
)

_SUBMODULES = (
    _common,
    resume,
    reports,
    governance,
    evidence,
    today,
    practice_cmds,
    inventory,
    session,
    workflow,
    listing,
    home,
    dispatch,
)

_SKIP = {
    "__name__", "__file__", "__doc__", "__package__", "__loader__",
    "__spec__", "__builtins__", "__cached__", "__annotations__",
}


def _lift(fn: types.FunctionType) -> types.FunctionType:
    """Rebuild ``fn`` so its globals are this package's namespace."""
    new = types.FunctionType(
        fn.__code__,
        globals(),
        name=fn.__name__,
        argdefs=fn.__defaults__,
        closure=fn.__closure__,
    )
    new.__kwdefaults__ = fn.__kwdefaults__
    new.__annotations__ = getattr(fn, "__annotations__", {})
    new.__doc__ = fn.__doc__
    new.__module__ = __name__
    new.__qualname__ = fn.__qualname__
    return new


# Copy non-callables first (constants + mutable module state), then lift
# functions so they resolve those names on this package.
for _mod in _SUBMODULES:
    for _name, _value in list(_mod.__dict__.items()):
        if _name in _SKIP or _name in globals():
            continue
        if isinstance(_value, types.ModuleType):
            continue
        if isinstance(_value, types.FunctionType):
            continue
        globals()[_name] = _value

for _mod in _SUBMODULES:
    for _name, _value in list(_mod.__dict__.items()):
        if not isinstance(_value, types.FunctionType):
            continue
        if _value.__module__ != _mod.__name__:
            # Imported-from-sibling alias; skip — we lift the defining copy.
            continue
        globals()[_name] = _lift(_value)

# Public entry points (also available via star-friendly names).
run = globals()["run"]
main = globals()["main"]

__all__ = ["run", "main"]
