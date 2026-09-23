"""copilot-sessions (cs) — a terminal application for your GitHub Copilot CLI sessions.

Reads the local Copilot session store (read-only) and lets you list, search,
inspect and resume sessions. Zero third-party dependencies.
"""

import sys

__version__ = "1.1.0"

# Every entry point - the `cs` console script, `python -m cs`, and the bin/cs
# launcher - imports this package first, so one check here covers all of them.
# pip enforces requires-python, but install.sh and bin/cs do not go through pip,
# and the modules carry `from __future__ import annotations`, so an old
# interpreter gets past import and fails much later inside a view instead.
# ruff's UP036 calls this dead code because target-version promises 3.10. That
# promise is exactly what install.sh and bin/cs can break, so the check stays.
if sys.version_info < (3, 10):  # noqa: UP036
    raise SystemExit(
        f"cs needs Python 3.10 or newer, but this is Python "
        f"{'.'.join(str(n) for n in sys.version_info[:3])} at {sys.executable}.\n"
        f"The simplest fix is Homebrew, which brings its own Python:\n"
        f"    brew install smaharajan/tap/copilot-sessions"
    )
