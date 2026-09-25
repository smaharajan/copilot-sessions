"""Autonomy, handoff, and audit commands."""

from __future__ import annotations

import shutil
import sys
import textwrap
from collections import Counter

from .. import (
    db,
    redact,
    signals,
    ui,
)
from ._common import (
    _AUDIT_HEADS,
    _BASIS,
    _HANDOFF_HEADS,
    _HARDCODED,
    _RISK,
    _RISK_LABEL,
    _YOLO_HEADS,
    _around,
    _capture,
    _cell,
    _extra_gaps,
    _fit_columns,
    _head_rule,
    _hint,
    _names,
    _note,
    _page,
    _page_report,
    _resolve_ref,
    _row,
    _short_path,
    _sort_note,
    _sort_report,
    _tail,
    _tiers,
    _when,
    _why,
    _why_hint,
    _window_label,
)


def cmd_yolo(show_all: bool = False, sort_by: str | None = None,
             descending: bool | None = None) -> bool:
    """How autonomously each session ran — YOLO mode, as far as it can be told."""
    return _page_report(
        "yolo",
        lambda column, down: _capture(lambda: _render_yolo(show_all, column, down)),
        sort_by, descending,
    )


# ── Autonomy ─────────────────────────────────────────────────────────

_AUTONOMY = {
    "yes": (ui.ROSE, "YOLO", "approvals off, on the evidence of the session itself",
            "Approvals off", "You turned approvals off yourself."),
    "high": (ui.AMBER, "unattended", "no evidence either way, but it ran unattended",
             "Ran unattended",
             "No flag either way — these ran too far between prompts for "
             "anyone to have been watching."),
    "no": (ui.MINT, "supervised", "prompted often enough to be supervised",
           "Supervised", "Prompted often enough that you saw it happening."),
}


def _yolo_evidence(why: str) -> str:
    """The approvals-off evidence as a column rather than a sentence.

    `signals` writes the verdict as prose — "you passed --allow-all-tools" —
    which is the right shape for one session's ledger and the wrong shape for
    a table, where the same clause repeats down every row. The flag is the
    part that differs; the section heading carries the rest.
    """
    passed = "you passed "
    return why[len(passed):] if why.startswith(passed) else "typed in session"


def _yolo_table(rows: list[dict], inner: int, column: str, descending: bool,
                evidence: bool) -> None:
    """One verdict's sessions. Same shape as handoff and audit.

    The evidence column exists only where there is evidence to put in it: for
    an inferred verdict the reason *is* the rate, and a column repeating it in
    words would be the third time the same fact appeared on one row.
    """
    columns = [
        ("active", "last active", "<"), ("session", "session", "<"),
        ("turns", "turns", ">"), ("steps", "steps", ">"),
        ("ratio", "per turn", ">"), ("evidence", "evidence", "<"),
        ("summary", "summary", "<"),
    ]
    optional = [("active", 12), ("turns", 5), ("steps", 6)]
    if evidence:
        optional.insert(2, ("evidence", 17))
    spans = _fit_columns(inner - 2, 20, optional, gaps=_extra_gaps(columns))
    spans.update(session=9, ratio=9)
    shown = [spec for spec in columns if spans.get(spec[0])]
    heads = _row(shown, spans)
    _head_rule(heads, 4, column, _YOLO_HEADS, descending)
    for row in rows:
        colour = _AUTONOMY[row["verdict"]][0]
        values = {
            "active": (_when(row["active"]), ui.MUTED),
            "session": (row["id"][:8], ui.SKY),
            "turns": (str(row["turns"]), ""),
            "steps": (str(row["steps"]), ""),
            "ratio": (f"{row['ratio']:.1f}", colour),
            "evidence": (_yolo_evidence(row["why"]), ui.CODE),
            "summary": (redact.redact(row["summary"]) or "(untitled)", ""),
        }
        print("    " + _row(shown, spans, values).rstrip())
    print()


def _render_yolo(show_all: bool, column: str = "risk",
                 descending: bool = True) -> None:
    conn = db.connect()
    rows = signals.autonomy(conn)
    conn.close()
    rows = _sort_report(rows, "yolo", column, descending)
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4

    grouped = {name: [r for r in rows if r["verdict"] == name] for name in _AUTONOMY}
    print()
    print(ui.rule(inner, f"Autonomy · {len(rows):,} sessions scanned"))
    print()
    if not rows:
        print(f"  {ui.MUTED}No session in this store has a recorded prompt.{ui.RST}")
        print()
        _note("Autonomy is steps per prompt, so a session with no prompt has "
              "nothing to divide by and is left out rather than scored.", inner)
        print()
        return

    # Lead with the verdict, the way Security leads with whether anything is
    # yours to fix. A page that opens on a table makes you count the rows to
    # find out whether it is bad news.
    loose = len(grouped["yes"]) + len(grouped["high"])
    if grouped["yes"]:
        status, colour = "APPROVALS WERE TURNED OFF", ui.ROSE
    elif grouped["high"]:
        status, colour = "RAN UNATTENDED", ui.AMBER
    else:
        status, colour = "EVERY SESSION WAS SUPERVISED", ui.MINT
    print(f"  {colour}{ui.BOLD}● {status}{ui.RST}")
    if loose:
        headline = (
            f"{loose} session{'' if loose == 1 else 's'} of {len(rows):,} ran "
            f"with nobody approving each step"
            + (f" · {len(grouped['yes'])} with approvals off outright"
               if grouped["yes"] else "")
        )
    else:
        headline = f"All {len(rows):,} sessions were prompted along the way"
    for line in textwrap.wrap(headline, max(24, inner - 2)):
        print(f"    {ui.BOLD}{line}{ui.RST}")
    print()

    _tiers([(len(grouped[name]), label, colour, meaning)
            for name, (colour, label, meaning, _head, _note) in _AUTONOMY.items()],
           len(rows), inner)
    print()

    shown = ["yes", "high"] + (["no"] if show_all else [])
    if not any(grouped[name] for name in shown):
        print(f"  {ui.MINT}Nothing ran away with itself — every session was "
              f"supervised.{ui.RST}")
        print()
        _hint("cs yolo --all — list the supervised sessions too", inner)
        print()
        return

    for name in shown:
        group = grouped[name]
        if not group:
            continue
        tone, _label, _meaning, heading, finding = _AUTONOMY[name]
        print(ui.heading(ui._fit(f"{heading} · {len(group)}", inner), tone, inner))
        _note(finding, inner, indent=4)
        print()
        _yolo_table(group, inner, column, descending, evidence=(name == "yes"))

    if not show_all and grouped["no"]:
        _hint(f"cs yolo --all — the {len(grouped['no']):,} supervised sessions too",
              inner)
    _why("The store records no approval mode, so YOLO is read from what the "
         "session shows: a flag or a toggle you typed, in one of your own "
         "messages — the store is full of the agent explaining these flags, "
         "and none of that counts. Unattended is inferred instead, from "
         f"{signals.UNATTENDED_RATIO:.0f}+ agent steps per prompt over "
         f"{signals.UNATTENDED_STEPS}+ steps.", inner)
    print(_sort_note("yolo", column, descending, inner))
    _why_hint(inner)
    print()


def cmd_handoff(ref: str | None = None, sort_by: str | None = None,
                descending: bool | None = None) -> bool:
    """Handoffs across sessions — the list, or one session's chain."""
    if ref:
        # A chain is a tree in time order; there is nothing to sort it by.
        return _page(_capture(lambda: _render_chain(ref)))
    return _page_report(
        "handoff",
        lambda column, down: _capture(lambda: _render_handoffs(column, down)),
        sort_by, descending,
    )


_ROLES = {
    "emitted": (ui.MINT, "wrote a handoff for whoever came next"),
    "received": (ui.SKY, "picked the work up from one"),
    "both": (ui.VIOLET, "took one up and left another"),
    "touched": (ui.MUTED, "opened a handoff document"),
    # Not a role a session can be listed under — only how a chain member that
    # touched no document got pulled in, by another session naming its id.
    "linked": (ui.MUTED, "named by another session"),
}


def _render_handoffs(column: str = "active", descending: bool = True) -> None:
    conn = db.connect()
    rows = signals.handoffs(conn)
    groups = _chain_groups(conn, rows)
    conn.close()
    # The chain size is a column you can sort by, so it belongs on the row.
    sizes = {member["id"]: len(group) for group in groups for member in group}
    for row in rows:
        row["chain"] = sizes.get(row["id"], 1)
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4

    print()
    print(ui.rule(inner, f"Handoffs · {len(rows)} sessions"))
    print()
    if not rows:
        print(f"  {ui.MUTED}No session in this store wrote or read a handoff.{ui.RST}")
        print()
        print(f"  {ui.MUTED}A handoff is a document one session leaves so the next")
        print(f"  can continue — cs finds them by name and by what you asked for.{ui.RST}")
        print()
        return

    linked = [group for group in groups if len(group) > 1]
    print(ui.heading("Roles", ui.ACCENT))
    for role, (colour, meaning) in _ROLES.items():
        count = sum(1 for r in rows if r["role"] == role)
        if count:
            print(f"    {colour}{count:>4}{ui.RST}  {role:<10}"
                  f" {ui.MUTED}{ui.trunc(meaning, max(12, inner - 21))}{ui.RST}")
    print()

    # The chains are the point of the report — a flat list buries the one
    # thing nothing else in cs can tell you, which is what followed what.
    if linked:
        carried = sum(len(group) for group in linked)
        print(ui.heading(f"Chains · {len(linked)}", ui.SKY))
        # The date here is when each session *started*, which is what the
        # chain is ordered by. The table below shows last activity, so without
        # saying which is which the same session appears under two dates.
        carried_note = (f"{carried} sessions carried work on, "
                        f"oldest first, by when each started")
        print(f"    {ui.MUTED}{ui.trunc(carried_note, inner - 2)}{ui.RST}")
        docs_by_id = {row["id"]: row["docs"] for row in rows}
        for number, group in enumerate(linked, 1):
            shared = _shared_document(group, docs_by_id)
            joined = f" · {shared}" if shared else " · linked by session id"
            print(f"    {ui.MUTED}"
                  f"{ui.trunc(f'{number}. {len(group)} sessions{joined}', inner - 2)}"
                  f"{ui.RST}")
            for position, member in enumerate(group):
                stem = ("┌" if position == 0
                        else "└" if position == len(group) - 1 else "├")
                print(f"     {ui.MUTED}{stem}{ui.RST} "
                      f"{_handoff_line(member, width)}")
        print()
        hint = ui._fit("cs handoff <id> — one chain, link by link", inner)
        print(f"    {ui.MUTED}{hint}{ui.RST}")
        print()

    print(ui.heading(f"Every session · {len(rows)}", ui.ACCENT))
    rows = _sort_report(rows, "handoff", column, descending)
    # Fixed: indent 4, session 9+1, role 9+1. Everything else gives way as the
    # window narrows, the document first — it is nearly always HANDOFF.md, and
    # cs handoff <id> shows the full path anyway.
    columns = [
        ("active", "last active", "<"), ("session", "session", "<"),
        ("role", "role", "<"), ("turns", "turns", ">"),
        ("chain", "chain", ">"), ("document", "document", "<"),
        ("summary", "summary", "<"),
    ]
    spans = _fit_columns(inner - 2, 24,
                         [("document", 13), ("active", 12),
                          ("chain", 5), ("turns", 5)],
                         gaps=_extra_gaps(columns))
    spans.update(session=9, role=9)
    shown = [spec for spec in columns if spans[spec[0]]]
    # Heads and rows are built from one list, so they cannot drift apart — the
    # divider used to be measured off a separately written head string, and
    # stopped 25 columns short of the rows it was dividing.
    heads = _row(shown, spans)
    _head_rule(heads, 4, column, _HANDOFF_HEADS, descending)
    for row in rows:
        role_colour = _ROLES[row["role"]][0]
        # Only the file name: every one of these is called HANDOFF.md or near
        # enough, and the directory it sat in is the session's own cwd.
        doc = row["docs"][0].rsplit("/", 1)[-1] if row["docs"] else ""
        if len(row["docs"]) > 1:
            doc += f"+{len(row['docs']) - 1}"
        values = {
            "active": (_when(row["active"]), ui.MUTED),
            "session": (row["id"][:8], ui.SKY),
            "role": (row["role"], role_colour),
            "turns": (str(row["turns"]), ""),
            "chain": ((str(row["chain"]), "") if row["chain"] > 1
                      else ("·", ui.MUTED)),
            "document": (doc, ui.MUTED),
            "summary": (redact.redact(row["summary"]) or "(untitled)", ""),
        }
        print("    " + _row(shown, spans, values).rstrip())
    print()
    print(_sort_note("handoff", column, descending, inner))
    print()


def _shared_document(group: list[dict], docs_by_id: dict) -> str:
    """The handoff document most of a chain has in common, by file name."""
    names = Counter(
        path.rsplit("/", 1)[-1]
        for member in group
        for path in docs_by_id.get(member["id"], [])
    )
    common = names.most_common(1)
    return common[0][0] if common and common[0][1] > 1 else ""


def _handoff_line(member: dict, width: int) -> str:
    """One session inside a chain: when it started, id, role, size, subject.

    `width` is the whole report's width. Columns give way as it narrows —
    the date first, since the table below carries it too — because the fixed
    columns alone came to more than a small window has, and the line wrapped.
    """
    spans = _fit_columns(width - 2, 17,
                         [("active", 11), ("turns", 10), ("role", 9)], least=8)
    colour = _ROLES.get(member["role"], (ui.MUTED, ""))[0]
    summary = redact.redact(member["summary"]) or "(untitled)"
    cells = [_cell(member["id"][:8], 9, colour=ui.SKY)]
    if spans["active"]:
        cells.insert(0, _cell(_when(member["active"]), 11, colour=ui.MUTED))
    if spans["role"]:
        cells.append(_cell(member["role"], 9, colour=colour))
    if spans["turns"]:
        cells.append(_cell(f"{member['turns']:>4} turns", 10, colour=ui.MUTED))
    cells.append(" " + ui.trunc(summary, spans["summary"] - 1))
    return " ".join(cells)


def _chain_groups(conn, rows: list[dict]) -> list[list[dict]]:
    """Each chain these sessions belong to, oldest session first.

    A chain can pull in a session that never touched a handoff document — one
    that was linked purely because another names its id. Its role is 'linked'
    rather than 'none': that is how the link was found, not a missing value.
    """
    if not rows:
        return []
    links = signals.edges(conn)
    roles = {row["id"]: row["role"] for row in rows}
    seen: set[str] = set()
    groups: list[list[dict]] = []
    for row in rows:
        if row["id"] in seen:
            continue
        found = signals.chain(conn, row["id"], links)
        seen |= set(found["detail"])
        members = sorted(found["detail"].values(),
                         key=lambda member: member.get("started") or "")
        groups.append([
            {**member, "active": member.get("started", ""),
             "role": roles.get(member["id"], "linked")}
            for member in members
        ])
    return groups


def _render_chain(ref: str) -> None:
    session_id = _resolve_ref(ref)
    conn = db.connect()
    detail = db.session_detail(conn, session_id)
    if not detail:
        print(f"error: session not found: {session_id}", file=sys.stderr)
        conn.close()
        sys.exit(1)
    group = signals.chain(conn, session_id)
    role = signals.session_handoff(conn, session_id)
    conn.close()

    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    print()
    print(ui.rule(inner, f"Handoff chain · {group['size']} "
                         f"session{'' if group['size'] == 1 else 's'}"))
    print()
    print(ui.field("role", role["role"]))
    for doc in role["docs"]:
        print(ui.field("document", _tail(_short_path(doc, detail[2]), inner - 12)))
    print()

    if group["size"] == 1:
        print(f"  {ui.MUTED}This session stands alone: nothing else opened its")
        print(f"  handoff document, and no other session names it.{ui.RST}")
        print()
        return

    print(ui.heading("Oldest first — each line handed on to the one below", ui.ACCENT))
    print()
    for root in group["roots"]:
        _print_chain_node(group, root, session_id, "", True, inner, set())
    print()
    print(f"  {ui.MUTED}Links are evidence, not guesses: sessions that opened the")
    print(f"  same handoff document, or that name another session's id.{ui.RST}")
    print()


def _print_chain_node(
    group: dict, node: str, focus: str, prefix: str, last: bool,
    width: int, seen: set[str], why: str = "",
) -> None:
    """One line of the tree, then its children. Cycles stop at `seen`."""
    if node in seen:
        return
    seen.add(node)
    detail = group["detail"].get(node, {})
    stem = ("└─ " if last else "├─ ") if prefix else ""
    started = (detail.get("started") or "")[:16].replace("T", " ")
    # The indent is measured without colour: escape codes take no columns.
    used = len(f"  {prefix}{stem}● {started}  ")
    mark = f"{ui.ACCENT}●{ui.RST}" if node == focus else f"{ui.MUTED}○{ui.RST}"
    summary = redact.redact(detail.get("summary", "")) or "(untitled)"
    print(f"  {prefix}{stem}{mark} {ui.MUTED}{started}{ui.RST}  "
          f"{ui.trunc(summary, max(20, width - used))}")

    below = prefix + ("   " if last else "│  ") if prefix else "  "
    note = f"{node[:8]} · {detail.get('turns', 0)} turns"
    print(f"  {below}{ui.MUTED}{note}{f' · {why}' if why else ''}{ui.RST}")

    children = group["children"].get(node, [])
    child_prefix = (prefix + ("   " if last else "│  ")) if prefix else "  "
    for index, (child, reason) in enumerate(children):
        _print_chain_node(group, child, focus, child_prefix,
                          index == len(children) - 1, width, seen, reason)


def cmd_audit(session: str | None = None, sort_by: str | None = None,
              descending: bool | None = None, days: int | None = None) -> bool:
    """Sessions holding credential-shaped text — the security view.

    Unscoped calls default to the last 30 days at the dispatch layer; pass
    `days=0` for the whole store. A session ref always scans that session
    in full.
    """
    return _page_report(
        "audit",
        lambda column, down: _capture(
            lambda: _render_audit(session, column, down, days=days)
        ),
        sort_by, descending,
    )


def _render_audit(ref: str | None, column: str = "risk",
                  descending: bool = True, days: int | None = None) -> None:
    session_id = _resolve_ref(ref) if ref else None
    # Session-scoped audits are never day-filtered; unscoped None means the
    # caller wants the library default (whole store) — dispatch supplies 30.
    window = None if session_id else days
    conn = db.connect()
    if session_id and not db.session_detail(conn, session_id):
        conn.close()
        print(f"error: session not found: {session_id}", file=sys.stderr)
        sys.exit(1)
    rows = signals.exposures(conn, session_id, days=window)
    touched = signals.sensitive_files(conn, session_id, days=window)
    destructive = signals.destructive(conn, session_id, days=window)
    if session_id:
        scanned = 1
    elif window is not None and window > 0:
        scanned = conn.execute(
            """SELECT COUNT(*) FROM sessions s
               WHERE MAX(s.created_at, s.updated_at) >= datetime('now', ?)""",
            (f"-{window} days",),
        ).fetchone()[0]
    else:
        scanned = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    conn.close()
    rows = _sort_report(rows, "audit", column, descending)
    # Security is a scanning view, not prose. The old 96-column reading width
    # wasted almost half of a 159-column terminal and crushed the summary.
    width = min(shutil.get_terminal_size().columns, 156)
    inner = width - 4

    # Named for the menu row that opens it. It used to be "Security posture"
    # on a wide window and "Security" on a narrow one, which is two names for
    # one page and neither of them the one you chose from the landing screen.
    if session_id:
        title = f"Security · session {session_id[:8]}"
    else:
        title = f"Security · {_window_label(window)} · {scanned:,} sessions"
    print()
    print(ui.rule(inner, title))
    print()
    if not rows and not touched:
        print(f"  {ui.MINT}Nothing credential-shaped found.{ui.RST}")
        print()
        _note("Checked every prompt and reply, the latest checkpoint content "
              "shown by cs show, and every touched path whose name indicates "
              "a credential file.", inner)
        print()
        # Clean of credentials is not clean: a session that removed a tree or
        # forced a push has nothing credential-shaped in it and is still the
        # thing you opened this page to find.
        _print_destructive(destructive, inner, "ran")
        _print_destructive(destructive, inner, "proposed")
        _print_audit_footnote(inner, column, descending)
        return
    if not rows:
        # Files alone is still a finding, and still the whole report.
        print(f"  {ui.MINT}No credential-shaped text in scanned session content.{ui.RST}")
        print()
        _print_destructive(destructive, inner, "ran")
        _print_credential_files(touched, inner)
        _print_destructive(destructive, inner, "proposed")
        _print_audit_footnote(inner, column, descending)
        return

    findings = sum(r["count"] for r in rows)
    pasted = [r for r in rows if r["side"] == "you"]
    assistant = [r for r in rows if r["side"] == "agent"]
    saved = [r for r in rows if r["side"] == "checkpoint"]
    session_count = len({r["id"] for r in rows})
    kinds: dict[str, int] = {}
    for row in rows:
        for kind, count in row["kinds"].items():
            kinds[kind] = kinds.get(kind, 0) + count

    typed = sum(r["count"] for r in pasted)
    replied = sum(r["count"] for r in assistant)
    checkpointed = sum(r["count"] for r in saved)
    risk_totals = {
        name: sum(count for kind, count in kinds.items()
                  if redact.severity(kind) == name)
        for name in redact.RANK
    }

    # This is an action screen, not an inventory report. Lead with ownership
    # and urgency; totals and confidence exist to size the work underneath.
    #
    # A destruction the session says it carried out outranks a credential:
    # a key can be rotated, and a deleted tree is a restore or it is gone.
    done = [row for row in destructive if row["basis"] == "ran"]
    status = ("ACTION REQUIRED" if pasted or done else "REVIEW REQUIRED")
    status_colour = ui.ROSE if pasted or done else ui.AMBER
    print(f"  {status_colour}{ui.BOLD}● {status}{ui.RST}")
    if pasted:
        headline = (
            f"{len(pasted)} session{'' if len(pasted) == 1 else 's'} "
            f"{'needs' if len(pasted) == 1 else 'need'} your action"
            f" · {typed} value{'' if typed == 1 else 's'} pasted by you"
        )
    else:
        review_parts = []
        if replied:
            review_parts.append(
                f"{replied} value{'' if replied == 1 else 's'} in assistant output"
            )
        if checkpointed:
            review_parts.append(
                f"{checkpointed} value{'' if checkpointed == 1 else 's'} "
                "in saved checkpoints"
            )
        headline = (
            f"{session_count} session{'' if session_count == 1 else 's'} need review"
            f" · {' · '.join(review_parts)}"
        )
    hardcoded = [r for r in rows if r["hardcoded"]]
    if hardcoded:
        headline += (f" · {len(hardcoded)} hardcoded in "
                     f"{'a session' if len(hardcoded) == 1 else 'sessions'} "
                     f"that wrote files")
    if done:
        wrecked = len({row["id"] for row in done})
        headline += (f" · {wrecked} session{'' if wrecked == 1 else 's'} "
                     f"{'reports' if wrecked == 1 else 'report'} "
                     f"destroying something")
    for line in textwrap.wrap(headline, max(24, inner - 2)):
        print(f"    {ui.BOLD}{line}{ui.RST}")
    detail = (f"{findings} finding{'' if findings == 1 else 's'} across "
              f"{session_count} session{'' if session_count == 1 else 's'}. "
              "Rotate confirmed live credentials first.")
    for line in textwrap.wrap(detail, max(24, inner - 2)):
        print(f"    {ui.MUTED}{line}{ui.RST}")
    print()

    # Each count sits next to what it means, and next to how much of the
    # total it is. This was a run-on line of chips that fitted a hundred
    # columns and ran off anything narrower, and before that a three-column
    # grid you had to count across to pair a label with its meaning. It is
    # the same block Autonomy opens with, because it answers the same shape
    # of question: which tier is this store actually made of?
    # First, because it is the one thing on this page that cannot be undone
    # by rotating a value — and because it was last, under a hundred and forty
    # lines of credential rows, which is the same as not being here.
    _print_destructive(destructive, inner, "ran")

    print(ui.heading(f"Credentials · {findings}", ui.ACCENT, inner))
    _tiers([(risk_totals[name], _RISK_LABEL[name].upper(), _RISK[name][0], meaning)
            for name, meaning in (("critical", "confirmed key format"),
                                  ("high", "token or URL login"),
                                  ("medium", "named assignment"))],
           max(findings, 1), inner)
    print()

    # Split rather than mixed: a secret you pasted and one the agent read out
    # of a file are different problems with different owners, and sorting them
    # into one list buries the eight rows that are actually yours to fix.
    # Risk and session never disappear: they answer "how urgent?" and "where?"
    # Date, count, turn and finding fall away in that order so the summary
    # remains readable instead of becoming a separate four-line block.
    columns = [
        ("risk", "risk", "<"),
        ("active", "last active", "<"), ("session", "session", "<"),
        ("found", "found", ">"), ("turn", "turn", ">"),
        ("names", "finding", "<"), ("summary", "summary", "<"),
    ]
    # The finding column takes what the longest finding actually needs, up to
    # twenty-two. Fixed at twenty-two it padded `DB_PASSWORD` with eleven
    # spaces and took them off the summary, which is the column that runs out
    # first on a small window.
    named = max(ui.cells(_names(r["hints"] or list(r["kinds"]), 22)) for r in rows)
    spans = _fit_columns(inner - 2, 20, [
        ("active", 12), ("found", 5), ("turn", 5), ("names", min(22, named)),
    ], gaps=_extra_gaps(columns))
    spans.update(risk=9, session=9)
    shown = [spec for spec in columns if spans[spec[0]]]
    heads = _row(shown, spans)
    table = ui.cells(heads)  # what every rule, row and continuation is measured to
    for group, label, colour, note in (
        (pasted, "Immediate action · pasted by you", ui.ROSE,
         "These values are stored in session transcripts. Rotate live "
         "credentials, then remove or expire every copied value."),
        (assistant, "Assistant output · review context", ui.AMBER,
         "These appeared in Copilot replies. They may be file content, "
         "examples, or generated text; inspect before acting."),
        (saved, "Saved checkpoints · outlive the turns", ui.AMBER,
         "The agent wrote these into the session's checkpoint, which is a "
         "separate record and is what `cs show` reads back. Clearing the "
         "conversation does not clear them."),
    ):
        if not group:
            continue
        print(ui.heading(ui._fit(f"{label} · {len(group)}", inner), colour, inner))
        _why(note, inner, indent=4)
        print()
        _head_rule(heads, 4, column, _AUDIT_HEADS, descending)
        ordered = _sort_report(group, "audit", column, descending)
        # The evidence sits on its own line under the row, hanging off a rail
        # so the table keeps its left edge. It used to share that line with
        # `inspect cs read <id> --turn <n>`, repeated on all forty rows of a
        # real store — thirty-five columns of boilerplate whose only two
        # variables, the session and the turn, are already columns of the row
        # above it. The command is named once, under the table, the way every
        # other view in cs names its drill-down.
        available = table - 2
        for row in ordered:
            # With no identifier to show — a private key, a JWT — the kind is
            # the most specific thing that can be said without quoting it.
            values = {
                "risk": ((_HARDCODED[1], _HARDCODED[0]) if row["hardcoded"]
                         else (_RISK_LABEL[row["severity"]],
                               _RISK[row["severity"]][0])),
                "active": (_when(row["active"]), ui.MUTED),
                "session": (row["id"][:8], ui.SKY),
                "found": (str(row["count"]), colour),
                # A checkpoint has no turn to open, so the column stays empty
                # rather than claiming turn 0.
                "turn": ("" if row.get("source") == "checkpoint"
                         else str(row["turn"]), ""),
                "names": (_names(row["hints"] or list(row["kinds"]), spans["names"]),
                          ui.CODE),
                "summary": (redact.redact(row["summary"]) or "(untitled)", ""),
            }
            print("    " + _row(shown, spans, values).rstrip())
            if row["line"]:
                print(f"      {ui.MUTED}└ {ui.RST}{ui.CODE}"
                      f"{_around(row['line'], '[redacted', available - 2)}{ui.RST}")
        print()
        _hint(_audit_drill(ordered), inner, indent=4)
        print()

    _print_credential_files(touched, inner)
    _print_destructive(destructive, inner, "proposed")
    _print_audit_footnote(inner, column, descending)


def _audit_drill(rows: list[dict]) -> str:
    """The one command a section's rows are all opened with.

    A group is either turns or checkpoints, never both, so one line covers
    it: a checkpoint has no turn to pass and is read back by `cs show`.
    """
    if all(row.get("source") == "checkpoint" for row in rows):
        return "cs show <session> — the checkpoint this was written into"
    return "cs read <session> --turn <turn> — open the exact turn above"


_DESTRUCTIVE_WORD = {
    "history": "history", "data": "data", "infra": "infra",
    "delete": "delete", "remote-exec": "network", "privilege": "sudo",
}


def _destructive_table(group: list[dict], inner: int) -> None:
    """One tier's rows, with the command that produced each hanging under it."""
    columns = [
        ("active", "last active", "<"), ("session", "session", "<"),
        ("turn", "turn", ">"), ("seen", "seen", ">"),
        ("what", "what", "<"), ("summary", "summary", "<"),
    ]
    spans = _fit_columns(inner - 2, 19,
                         [("active", 12), ("seen", 5), ("turn", 5)],
                         gaps=_extra_gaps(columns))
    spans.update(session=9, what=8)
    shown = [spec for spec in columns if spans[spec[0]]]
    heads = _row(shown, spans)
    _head_rule(heads)
    for row in group:
        tone = _RISK[row["severity"]][0]
        values = {
            "active": (_when(row["active"]), ui.MUTED),
            "session": (row["id"][:8], ui.SKY),
            "turn": (str(row["turn"]), ""),
            "seen": (str(row["count"]), tone),
            "what": (_DESTRUCTIVE_WORD[row["kind"]], tone),
            "summary": (redact.redact(row["summary"]) or "(untitled)", ""),
        }
        print("    " + _row(shown, spans, values).rstrip())
        if row["line"]:
            print(f"      {ui.MUTED}└ {ui.RST}{ui.CODE}"
                  f"{ui._fit(row['line'], ui.cells(heads) - 4)}{ui.RST}")
    print()
    _hint("cs read <session> --turn <turn> — open the exact turn above",
          inner, indent=4)
    print()


def _print_destructive(rows: list[dict], inner: int, basis: str) -> None:
    """What the sessions took away — removals, rewrites, drops, teardowns.

    The two tiers are printed at two ends of the page rather than together.
    What a session says it *did* is the most irreversible thing here and
    leads; what it *offered* is the least certain and the longest, and
    seventy-four rows of it between the urgent findings and the credentials
    is the same as burying both.

    Grouped by basis and not by kind: splitting on kind as well would put six
    one-row tables on the page. The kind is what a row is, so it is a column.
    """
    group = [row for row in rows if row["basis"] == basis]
    if basis == "ran":
        if not rows:
            return
        print(ui.heading(f"Destructive actions · {len(rows)}",
                         ui.ROSE if group else ui.AMBER, inner))
        # A finding, not a lesson: it says what these rows are evidence of,
        # and every reading of them depends on knowing it.
        _note("Read out of the conversation. The store records file creates "
              "and edits but no deletion, and no command exit code — so "
              "nothing here is proof that something ran.", inner, indent=4)
        print()
        _tiers([(sum(1 for row in rows if row["basis"] == name),
                 label, colour, meaning)
                for name, (colour, label, meaning) in _BASIS.items()],
               len(rows), inner)
        print()
        if not group:
            print(f"    {ui.MINT}No session reports having carried one out."
                  f"{ui.RST}")
            print()
            _hint("The offered ones are listed at the foot of this report.",
                  inner, indent=4)
            print()
            return
        print(ui.heading(ui._fit(f"Reported as done · {len(group)}", inner),
                         _BASIS["ran"][0], inner))
        _why(_BASIS["ran"][2].capitalize() + ".", inner, indent=4)
        print()
        _destructive_table(group, inner)
        return

    if not group:
        return
    print(ui.heading(ui._fit(f"Offered, outcome unknown · {len(group)}", inner),
                     _BASIS["proposed"][0], inner))
    _note("Destructive commands the sessions put forward. Nothing records "
          "whether any of them was run.", inner, indent=4)
    print()
    _destructive_table(group, inner)


def _print_credential_files(touched: list[dict], inner: int) -> None:
    """Sessions that touched a path whose job is to hold a credential."""
    if not touched:
        return
    print(ui.heading(
        ui._fit(f"Credential files touched · {len(touched)} sessions", inner),
        ui.AMBER, inner,
    ))
    _why("The store records that these paths were created or edited. It "
         "does not prove their contents were read; inspect the session "
         "before deciding whether a credential was exposed.", inner, indent=4)
    print()

    # Same budget arithmetic as the tables above, so the two rules line up:
    # 10 is the session column and its gap, everything else can be dropped.
    columns = [
        ("active", "last active", "<"), ("session", "session", "<"),
        ("files", "files", ">"), ("kinds", "what", "<"),
        ("summary", "summary", "<"),
    ]
    spans = _fit_columns(inner - 2, 10,
                         [("active", 12), ("files", 5), ("kinds", 24)],
                         gaps=_extra_gaps(columns))
    spans.update(session=9)
    shown = [spec for spec in columns if spans[spec[0]]]
    heads = _row(shown, spans)
    _head_rule(heads)
    for entry in touched:
        values = {
            "active": (_when(entry["active"]), ui.MUTED),
            "session": (entry["id"][:8], ui.SKY),
            "files": (str(entry["count"]), ui.AMBER),
            "kinds": (_names(list(entry["kinds"]), spans["kinds"]), ui.CODE),
            "summary": (redact.redact(entry["summary"]) or "(untitled)", ""),
        }
        print("    " + _row(shown, spans, values).rstrip())
        # Hung off the same rail as the audit's evidence, and unlabelled: a
        # session with four paths under it repeated the word "path" four
        # times to say what the block already says.
        path_width = max(12, ui.cells(heads) - 4)
        for path in entry["paths"]:
            shown_path = redact.redact(_short_path(path, entry["cwd"]))
            parts = textwrap.wrap(
                shown_path, path_width, break_long_words=True,
                break_on_hyphens=False,
            ) or [shown_path]
            for index, part in enumerate(parts):
                rail = "└ " if index == 0 else "  "
                print(f"      {ui.MUTED}{rail}{ui.RST}{ui.CODE}{part}{ui.RST}")
    print()


def _print_audit_footnote(inner: int, column: str, descending: bool) -> None:
    _why("Safe display: names, public prefixes and masked evidence only. "
         "Credential values are never printed by this view.", inner)
    print(_sort_note("audit", column, descending, inner))
    _why_hint(inner)
    print()


def _governance(conn, session_id: str) -> dict:
    """The governance readings for one session, gathered while the connection
    is open so rendering can happen later, like everything else."""
    return {
        "autonomy": signals.session_autonomy(conn, session_id),
        "handoff": signals.session_handoff(conn, session_id),
        "exposure": signals.exposures(conn, session_id),
        "files": signals.sensitive_files(conn, session_id),
        "destructive": signals.destructive(conn, session_id),
    }


def _print_governance(found: dict, session_id: str, cwd: str, width: int) -> None:
    """The three governance lines, when any of them says something.

    A session that ran supervised, handed nothing on and holds no credential
    gets no block at all — silence is the good outcome, and a row of "none"
    would only make the interesting ones harder to spot.
    """
    autonomy, handoff, exposure, files = (
        found["autonomy"], found["handoff"], found["exposure"], found["files"]
    )
    destructive = found.get("destructive", [])
    if (autonomy["verdict"] == "no" and handoff["role"] == "none"
            and not exposure and not files and not destructive):
        return

    print(ui.heading("Risk & continuity", ui.AMBER))
    if autonomy["verdict"] != "no":
        colour, label = _AUTONOMY[autonomy["verdict"]][:2]
        print(f"    {colour}{label:<11}{ui.RST}{ui.MUTED}{autonomy['why']}{ui.RST}")
    if handoff["role"] != "none":
        docs = ", ".join(_tail(_short_path(d, cwd), 40) for d in handoff["docs"][:2])
        print(f"    {ui.SKY}{handoff['role']:<11}{ui.RST}"
              f"{ui.MUTED}{docs or 'no document recorded'} · "
              f"cs handoff {session_id[:8]}{ui.RST}")
    if exposure:
        typed = sum(entry["count"] for entry in exposure if entry["side"] == "you")
        replied = sum(entry["count"] for entry in exposure if entry["side"] == "agent")
        checkpointed = sum(
            entry["count"] for entry in exposure if entry["side"] == "checkpoint"
        )
        places = []
        if typed:
            places.append(f"{typed} pasted by you")
        if replied:
            places.append(f"{replied} in assistant output")
        if checkpointed:
            places.append(f"{checkpointed} in saved checkpoints")
        hints = list(dict.fromkeys(
            hint for entry in exposure for hint in entry["hints"]
        ))
        print(f"    {ui.ROSE}{'secrets':<11}{ui.RST}{ui.MUTED}"
              f"{sum(entry['count'] for entry in exposure)} found · "
              f"{' · '.join(places)}"
              f"{' · ' + ', '.join(hints[:3]) if hints else ''}{ui.RST}")
    if destructive:
        # Ranked the way the page ranks it: what the session says it did
        # first, and only then what it offered to do.
        done = [row for row in destructive if row["basis"] == "ran"]
        lead = (done or destructive)[0]
        kinds = ", ".join(dict.fromkeys(
            _DESTRUCTIVE_WORD[row["kind"]] for row in (done or destructive)
        ))
        colour = ui.ROSE if done else ui.AMBER
        label = "destroyed" if done else "offered"
        print(f"    {colour}{label:<11}{ui.RST}{ui.MUTED}"
              f"{kinds} · "
              f"{'reported done' if done else 'outcome not recorded'} · "
              f"cs read {session_id[:8]} --turn {lead['turn']}{ui.RST}")
    if files:
        entry = files[0]
        print(f"    {ui.AMBER}{'files':<11}{ui.RST}{ui.MUTED}"
              f"{entry['count']} credential file"
              f"{'' if entry['count'] == 1 else 's'} touched · "
              f"{', '.join(entry['kinds'])}{ui.RST}")
    print()
