"""Pins, annotations, and the daily AIU budget — sticky daily-workflow prefs."""

from __future__ import annotations

import sys

from .. import (
    db,
    redact,
    ui,
)
from ._common import (
    _HOME_ACTIVE,
    _capture,
    _page,
    _resolve_ref,
    _with_assets,
)


def _require_session(ref: str) -> str:
    """Resolve a ref and confirm the session exists in the store."""
    session_id = _resolve_ref(ref)
    conn = db.connect()
    try:
        detail = db.session_detail(conn, session_id)
    finally:
        conn.close()
    if detail is None:
        print(f"error: session not found: {session_id}", file=sys.stderr)
        sys.exit(1)
    return session_id


def cmd_pin(ref: str) -> None:
    session_id = _require_session(ref)
    if not ui.pin_session(session_id):
        print("error: could not write settings", file=sys.stderr)
        sys.exit(1)
    print(f"  pinned {session_id[:8]}")


def cmd_unpin(ref: str) -> None:
    session_id = _require_session(ref)
    if not ui.unpin_session(session_id):
        print("error: could not write settings", file=sys.stderr)
        sys.exit(1)
    print(f"  unpinned {session_id[:8]}")


def cmd_pins() -> bool:
    """List pinned sessions. True when a full-screen listing ran."""
    # Through the package namespace, not an import here: an import inside
    # the function handed back the un-lifted listing, whose `show` could not
    # find the reader and raised KeyError on the first 'v'.
    _interactive_listing = globals()["_interactive_listing"]
    _render_listing = globals()["_render_listing"]

    ids = ui.pinned_ids()
    if not ids:
        text = (f"\n  {ui.DIM}No pinned sessions. Pin one with "
                f"'cs pin <ref>'.{ui.RST}\n\n")
        if _HOME_ACTIVE:
            return _page(text)
        print(text, end="")
        return False

    conn = db.connect()
    try:
        all_rows = db.recent_sessions(conn, 0)
        names = db.session_names()
    finally:
        conn.close()
    by_id = {row[0]: row for row in all_rows}
    rows = []
    missing = []
    for sid in ids:
        row = by_id.get(sid)
        if row is None:
            missing.append(sid)
            continue
        # Prefer the user-given / workspace name when the store has one.
        named = names.get(sid)
        if named and named[0]:
            summary = named[0]
            rows.append((row[0], row[1], summary, row[3], row[4], row[5], row[6],
                         *(row[7:] if len(row) > 7 else ())))
        else:
            rows.append(row)
    rows = _with_assets(rows)
    title = f"Pinned · {len(rows)} session{'' if len(rows) == 1 else 's'}"
    if not rows:
        print()
        print(f"  {ui.DIM}Pinned ids are no longer in the store. "
              f"Unpin with 'cs unpin <id>'.{ui.RST}")
        print()
        return False
    if sys.stdin.isatty() and sys.stdout.isatty():
        return _interactive_listing(rows, title, show_all=True)
    _render_listing(rows, title, show_all=True)
    if missing:
        print(f"  {ui.DIM}({len(missing)} pinned id"
              f"{'' if len(missing) == 1 else 's'} not in the store){ui.RST}")
        print()
    return False


def cmd_note(ref: str, text: str | None) -> None:
    """Set a note on a session, or print the current one when text is None."""
    session_id = _require_session(ref)
    if text is None:
        note = ui.annotation(session_id)["note"]
        if note:
            print(f"  {session_id[:8]}  {redact.one_line(redact.redact(note))}")
        else:
            print(f"  {ui.DIM}no note on {session_id[:8]}{ui.RST}")
        return
    if not ui.set_note(session_id, text):
        print("error: could not write settings", file=sys.stderr)
        sys.exit(1)
    if text.strip():
        print(f"  note set on {session_id[:8]}")
    else:
        print(f"  note cleared on {session_id[:8]}")


def cmd_tag(ref: str, tag: str) -> None:
    session_id = _require_session(ref)
    if not tag.strip():
        print("error: missing tag — usage: cs tag <ref> <tag>", file=sys.stderr)
        sys.exit(1)
    if not ui.add_tag(session_id, tag):
        print("error: could not write settings", file=sys.stderr)
        sys.exit(1)
    print(f"  tagged {session_id[:8]}  #{tag.strip()}")


def cmd_untag(ref: str, tag: str) -> None:
    session_id = _require_session(ref)
    if not tag.strip():
        print("error: missing tag — usage: cs untag <ref> <tag>", file=sys.stderr)
        sys.exit(1)
    if not ui.remove_tag(session_id, tag):
        print("error: could not write settings", file=sys.stderr)
        sys.exit(1)
    print(f"  untagged {session_id[:8]}  #{tag.strip()}")


def _today_nano() -> int:
    """Spend in the last 24 hours — same window standup uses for days=1."""
    conn = db.connect()
    try:
        totals = db.cost_totals(conn, 1)
    finally:
        conn.close()
    return int(totals.get("nano_aiu", 0) or 0) if totals else 0


def budget_check() -> int:
    """One line for a hook or a script, and an exit code: 1 over budget, else 0.

    Over means strictly over — spending exactly the limit is still within it.
    No budget set is never over.
    """
    limit = ui.daily_budget_aiu()
    spent = _today_nano() / 1e9
    if limit is None:
        print(f"budget: none set · {spent:,.2f} AIU in the last 24 hours")
        return 0
    over = spent > limit
    print(f"budget: {spent:,.2f} / {limit:g} AIU in the last 24 hours · "
          f"{'over by ' + format(spent - limit, ',.2f') if over else 'within'}")
    return 1 if over else 0


def cmd_budget_view() -> bool:
    """The home screen's Budget row: today against the limit, as a page."""
    import shutil

    inner = min(shutil.get_terminal_size().columns, 96) - 4
    return _page("\n" + ui.rule(inner, "Budget · last 24 hours") + "\n"
                 + _capture(lambda: cmd_budget(None)) + "\n\n"
                 + "\n".join(f"  {ui.DIM}{line}{ui.RST}" for line in (
                     "←/→ on the home row changes the limit.",
                     "For hooks and scripts: cs budget --check")) + "\n")


def cmd_budget(arg: str | None) -> None:
    """Show, set, or clear the daily AIU budget."""
    if arg is None:
        limit = ui.daily_budget_aiu()
        spent_nano = _today_nano()
        spent = spent_nano / 1e9
        print()
        if limit is None:
            print(f"  {ui.DIM}No daily budget set.{ui.RST}")
            print(f"  {ui.DIM}Set one with 'cs budget <aiu>'.{ui.RST}")
            print(f"  spent last 24h  {ui.fmt_aiu(spent_nano)} AIU")
        else:
            colour = ui.budget_colour(spent, limit)
            line = (f"  {colour}{spent:,.2f} / {limit:g} AIU{ui.RST}  "
                    f"{ui.DIM}· last 24 hours{ui.RST}")
            print(line)
            ratio = spent / limit if limit else 0
            if ratio > 1.0:
                print(f"  {ui.ROSE}over budget by {spent - limit:,.2f} AIU{ui.RST}")
            elif ratio >= 0.7:
                print(f"  {ui.AMBER}{ratio:.0%} of daily budget{ui.RST}")
        print()
        return

    if arg.lower() in ("clear", "none", "off"):
        if not ui.set_daily_budget(None):
            print("error: could not write settings", file=sys.stderr)
            sys.exit(1)
        print("  daily budget cleared")
        return

    try:
        value = float(arg)
    except ValueError:
        print(f"error: budget wants a number of AIU (or 'clear'), not '{arg}'",
              file=sys.stderr)
        sys.exit(1)
    if value < 0:
        print("error: budget cannot be negative", file=sys.stderr)
        sys.exit(1)
    if not ui.set_daily_budget(value if value > 0 else None):
        print("error: could not write settings", file=sys.stderr)
        sys.exit(1)
    if value > 0:
        print(f"  daily budget set to {value:g} AIU")
    else:
        print("  daily budget cleared")



