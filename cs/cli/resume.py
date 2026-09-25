"""Resume a session from an id, prefix, or listing selection."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from .. import db
from ._common import _drain_stdin, _pause, _resolve_ref


def _resume_target(ref: str) -> tuple[str, str, str] | None:
    """Where a resume would go: the copilot binary, the session, its folder.

    None when there is nothing to resume, having already said why. Returning
    rather than exiting is what lets the listing survive a missing `copilot`
    — from inside the app that is a message, not a reason to close it.
    """
    session_id = _resolve_ref(ref)
    conn = db.connect()
    detail = db.session_detail(conn, session_id)
    conn.close()
    if not detail:
        print(f"error: session not found: {session_id}", file=sys.stderr)
        return None
    # Resolved before the chdir, not after: execvp walks $PATH from the
    # working directory, so a relative or empty $PATH entry would let a
    # `copilot` sitting in the session's own folder win over the real one.
    binary = shutil.which("copilot")
    if not binary:
        print("error: 'copilot' CLI not found on PATH", file=sys.stderr)
        return None
    cwd = detail[2]
    return binary, session_id, cwd if cwd and os.path.isdir(cwd) else ""


def cmd_resume(ref: str) -> None:
    """`cs resume <id>` from the shell: hand the terminal over for good.

    execv rather than a child, because there is nothing here to come back to
    — a wrapper process left alive would only sit holding the terminal.
    """
    target = _resume_target(ref)
    if not target:
        sys.exit(1)
    binary, session_id, cwd = target
    # Resume restores the transcript but not the working directory — without this
    # you get the conversation pointed at whatever folder you happened to be in.
    if cwd and cwd != os.getcwd():
        print(f"cd {cwd}")
        os.chdir(cwd)
    print(f"Resuming {session_id} …", flush=True)  # ponytail: execvp discards unflushed stdout
    try:
        os.execv(binary, ["copilot", "--resume", session_id])
    except OSError:
        print("error: could not start the 'copilot' CLI", file=sys.stderr)
        sys.exit(1)


def _resume_from_listing(ref: str) -> None:
    """Resume from inside the app, and come back to the listing afterwards.

    A child process, not execv. Enter on a row means "go and look at this
    session", not "close `cs`" — but execv replaced the app outright, so
    quitting Copilot dropped the user at their shell with the listing gone.

    Ctrl-C is how people close the Copilot CLI, and it lands on this process
    too: the child is not given a process group of its own, so the terminal
    signals both. Catching it here is the ordinary way back, not an error.
    The chdir is the child's alone — `cwd=` rather than `os.chdir`, so the
    listing we return to still resolves paths the way it drew them.
    """
    target = _resume_target(ref)
    if not target:
        _pause("Esc or Enter for the list · q quits ")
        return
    binary, session_id, cwd = target
    print(f"Resuming {session_id} …", flush=True)
    try:
        subprocess.run([binary, "--resume", session_id], cwd=cwd or None,
                       check=False)
    except KeyboardInterrupt:
        pass  # the child saw it too, and has already gone
    except OSError:
        print("error: could not start the 'copilot' CLI", file=sys.stderr)
        _pause("Esc or Enter for the list · q quits ")
    # Copilot runs the terminal raw and with its own mouse reporting, so
    # whatever ended it — a stray ^C, a half-read mouse report — can still be
    # sitting in the buffer. The listing reads keys the moment it redraws,
    # and would take those bytes as typing.
    _drain_stdin()
