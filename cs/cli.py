"""Command implementations and argument dispatch for ``cs``."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
from collections import Counter
from datetime import date
from pathlib import Path

from . import (
    __version__,
    context,
    db,
    export,
    hooks,
    mcp,
    practice,
    redact,
    signals,
    ui,
)

# ── #N shortcut index ────────────────────────────────────────────────
# Listings number their rows; the map is persisted so `cs show #3` works
# even in a new shell. Stored next to the session store, never committed.


def _index_file() -> Path:
    base = db.default_db_path().parent
    return base / ".cs-last-index"


def _save_index(index: dict[int, str]) -> None:
    try:
        _index_file().write_text("\n".join(f"{k}={v}" for k, v in index.items()))
    except OSError:
        pass  # non-fatal: #N shortcuts just won't persist


def _resolve_ref(ref: str) -> str:
    """Accept a full session id, an unambiguous id prefix, or a row number.

    Row numbers (``2`` or ``#2``) come from the last listing. The prefix form
    is what the reports print — a full uuid on every line would crowd out the
    summary, and eight characters already identify a session here.
    """
    hashed = ref.startswith("#")
    token = ref[1:] if hashed else ref
    # `#N` is always a row. A bare number could be either — session ids start
    # with eight hex digits, which are sometimes all decimal — so length
    # decides, and a prefix matching nothing still falls back to a row.
    row_first = hashed or (token.isdigit() and len(token) < 6)
    if not row_first and _is_prefix(token):
        if session_id := _resolve_prefix(token):
            return session_id
    if not token.isdigit():
        return ref
    n = int(token)
    f = _index_file()
    if f.exists():
        for line in f.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.isdigit() and int(k) == n:
                return v
    print(f"error: #{n} not found — run a listing (e.g. 'cs recent') first", file=sys.stderr)
    sys.exit(1)


def _is_prefix(token: str) -> bool:
    """Hex, long enough to mean one session, shorter than a whole id."""
    return 6 <= len(token) < 36 and all(c in "0123456789abcdefABCDEF-" for c in token)


def _resolve_prefix(token: str) -> str:
    """The one session whose id starts with `token`, or '' if there is none."""
    conn = db.connect()
    matches = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM sessions WHERE id LIKE ? ORDER BY id", (token.lower() + "%",)
        )
    ]
    conn.close()
    if len(matches) == 1:
        return matches[0]
    if not matches:
        return ""
    print(f"error: '{token}' matches {len(matches)} sessions — "
          f"try more characters", file=sys.stderr)
    sys.exit(1)


# ── Ignore patterns (generic, user-configurable) ─────────────────────
# By default only zero-turn (empty) sessions are hidden. Users can add
# summary prefixes to hide scheduled/automated runs by creating an ignore
# file at $COPILOT_HOME/.cs-ignore — one prefix per line, '#' for comments.


def _ignore_prefixes() -> list[str]:
    return db.ignored_prefixes()


def _is_hidden(summary: str, turns: int, nano_aiu: int,
               prefixes: list[str]) -> bool:
    """Whether a default listing should drop this row.

    A session with no turns is almost always a launch that was closed
    without an exchange — 251 of them in a 1,465-session store — and
    dropping those is what keeps a listing about work that happened.

    Credits are the exception, because they are what a session that is
    *still running* looks like. Copilot records usage events as they are
    billed but only writes a `turns` row once an exchange has been
    persisted, so a session opened in another window sits at zero turns
    with real spend against it for as long as its first exchange takes.
    Hiding it meant the one session you had every reason to be watching was
    the one `cs` would not show you — while the strip above it, which counts
    on all three axes in SQL, had already counted it. In that same store
    exactly one of the 251 zero-turn sessions had credits: the live one.
    """
    if not turns and not nano_aiu:
        return True
    # A name is not enough on its own: `_visible` keeps the merely quiet
    # sessions for `cs all`, and that is the distinction it is drawing.
    return any((summary or "").startswith(p) for p in prefixes)


def _never_used(row: tuple) -> bool:
    """A session that recorded nothing whatsoever, on any axis.

    The CLI writes a session row the moment it launches. Close it without an
    exchange and that row is all there ever is: no turns, no summary, no
    credits — and, checked against a real store, no checkpoints, files, refs
    or usage events either. There is nothing to read, brief or review, so
    listing it only pads the view out.

    All three conditions are required rather than turns alone: a session
    that spent credits, or that was given a name, recorded *something*, and
    the point here is to drop only what is genuinely blank. Resuming is
    unaffected — ids and prefixes resolve straight against the store, so a
    dropped session can still be resumed by id if you know it.
    """
    # Indexed rather than unpacked: a listing row grew two trailing columns
    # (skills, agents) and this rule reads none of them.
    summary, turns, nano_aiu = row[2], row[5], row[6]
    return not turns and not nano_aiu and not (summary or "").strip()


# Below this the two kit columns are dropped rather than squeezed: they cost
# fourteen characters, and on a narrow window that is the summary's last
# readable third. Set above the standard 80-column terminal on purpose — the
# summary is what a listing is read for, and a review column that costs it a
# third of its width has not earned the room until the window is wider than
# the default. Sorting by either column still works when they are hidden.
_KIT_MIN_WIDTH = 88


def _kit_of(row: tuple) -> tuple[int, int]:
    """(skills, agents) for a listing row — (0, 0) for a row built without them.

    Rows grew two trailing columns, and not every caller fills them in:
    anything that hands a renderer a bare store row still gets drawn rather
    than raising. A missing count reads as none, which is what an absent
    column has always meant here.
    """
    return (row[7] if len(row) > 7 else 0, row[8] if len(row) > 8 else 0)


def _kit_cells(skills: int, agents: int) -> str:
    """The Skills and Agents cells for one row, zero drawn as a quiet dash.

    A column of noughts is a column you learn to skip, and most sessions use
    neither — so the number is only inked when there is one, and the dash
    says 'none' without competing with the counts that matter.

    Fourteen characters wide, the same as the header block above it: a space,
    six, a space, six.
    """
    def cell(value: int, colour: str) -> str:
        if not value:
            return f"{ui.DIM}{'·':>6}{ui.RST}"
        return f"{colour}{value:>6}{ui.RST}"

    return f" {cell(skills, ui.MINT)} {cell(agents, ui.SKY)}"


def _with_assets(rows: list[tuple]) -> list[tuple]:
    """Every listing row, plus how much of your kit the session actually used.

    Two numbers per session, appended to the row: the skills it referenced
    and the sub-agents it ran. They answer the question a listing could not
    before — not "what did this cost" but "what did it bring to bear" — and
    that is the one worth asking when reviewing how a team works, because a
    session that leaned on three skills and delegated to five agents is
    doing something structurally different from one that typed at the model
    for an hour.

    The two counts are not equally certain, and the views that print them
    say so. Sub-agents are exact: the store bills every model call against
    the agent that made it. Skills are inferred from the transcript, because
    the store records no invocation event for them, and the rule is
    deliberately conservative — an undercount beats a flattering guess.

    Appended rather than inserted so that every column index already in use,
    and every view that reads one, keeps meaning what it meant. Two passes,
    once per listing: a scan of the turns and a grouped read of the usage
    records, both of which cost nothing on a small store and about a second
    on a large one.
    """
    if not rows:
        return rows
    conn = db.connect()
    try:
        skills = db.assets_by_session(conn, [name for name, _ in _asset_names("skills")])
        agents = db.subagents_by_session(conn)
    finally:
        conn.close()
    return [
        (*row, skills.get(row[0], 0), agents.get(row[0], 0)) for row in rows
    ]


def _visible(rows: list[tuple], show_all: bool) -> list[tuple]:
    """The rows a listing should carry. `show_all` keeps the merely quiet
    ones — zero-turn sessions that still have a name, and anything matched
    by the user's ignore file — but never the blank ones.

    A zero-turn session that has spent credits is not quiet: it is running,
    and it is carried by every listing. See `_is_hidden`."""
    rows = [r for r in rows if not _never_used(r)]
    if show_all:
        return rows
    prefixes = _ignore_prefixes()
    return [r for r in rows if not _is_hidden(r[2], r[5], r[6], prefixes)]


def _no_summary(turns: int) -> str:
    """What to show in place of a summary the session never got.

    Almost every one of these is a session that was opened and closed
    without a single exchange — the CLI writes the row at launch, and when
    nothing follows there is nothing to summarise. Labelling those
    "untitled" described the missing field instead of the session, which
    made `cs all` read as a list of nameless work rather than what it is:
    mostly abandoned launches. A session that does have turns but no
    summary yet is the genuinely untitled case, and still says so.
    """
    return "(never used)" if turns == 0 else "(untitled)"


# Generic directory names that don't identify a project (never used as a tag).
_GENERIC_DIRS = {
    "", "/", "Downloads", "Desktop", "Documents", "tmp", "Work",
    "projects", "platform-tools", "repos", "src", "code", "demo",
    os.path.basename(os.path.expanduser("~")),
}


def _project_tag(repo: str, cwd: str) -> str:
    """A short keyword to identify a session: repo name, else project folder."""
    if repo:
        return redact.one_line(repo.split("/")[-1])
    leaf = redact.one_line(os.path.basename(cwd.rstrip("/")))
    return "" if leaf in _GENERIC_DIRS else leaf


# ── Rendering a session listing ──────────────────────────────────────
# Where a search hit came from, in words rather than table names.
_SOURCE_LABELS = {
    "turn": "turn",
    "checkpoint_overview": "overview",
    "checkpoint_work_done": "work done",
    "checkpoint_next_steps": "next steps",
    "checkpoint_history": "history",
    "checkpoint_technical": "technical",
    "checkpoint_files": "files",
    "workspace_artifact": "artifact",
}


def _snippet(text: str) -> str:
    """Make an FTS window safe to show.

    FTS returns a window cut on token boundaries, and a credential spans
    several tokens — `ghp_ZZZZ…` is `ghp`, `_`, `ZZZZ…`. A cut can therefore
    land *inside* one and hand back the secret half with nothing
    credential-shaped in front of it, so no pattern matches and redaction
    passes it through untouched. Whatever sits against an ellipsis is
    dropped rather than shown; only then is the text masked.
    """
    words = text.split()
    cut_left, cut_right = text.startswith("…"), text.endswith("…")
    if cut_left and words:
        words = words[1:]
    if cut_right and words:
        words = words[:-1]
    body = redact.snippet(" ".join(words))
    return f"{'…' if cut_left else ''}{body}{'…' if cut_right else ''}"


def _hit_text(source: str, text: str) -> str:
    """Sanitise a listing hit according to where it came from.

    Search-index values are truncated FTS windows and need `_snippet`'s
    boundary handling. File hits are paths; treating their leading ellipsis
    as an FTS marker used to discard the filename itself.
    """
    if source in _SOURCE_LABELS:
        return _snippet(text)
    return redact.one_line(text)


def _highlight(snippet: str, term: str = "") -> str:
    """Colour what the search matched.

    Run after masking, never before: marking up the text is what splits a
    credential out of a pattern's reach, so the marks only ever go on text
    that redaction has already had its say about.
    """
    out = snippet
    operators = {"and", "or", "not", "near"}
    words = sorted(
        (w for w in re.findall(r"\w+", term)
         if len(w) > 1 and w.casefold() not in operators),
        key=len,
        reverse=True,
    )
    if words:
        hit = re.compile("|".join(re.escape(w) for w in words), re.I)
        out = hit.sub(lambda m: f"{ui.AMBER}{ui.BOLD}{m.group(0)}{ui.RST}{ui.DIM}", out)
    return f"{ui.DIM}{out}{ui.RST}"



_SORT_COLUMNS = {
    # 'relevance' has no column of its own: it is the order the search came
    # back in, which is also the order #N was handed out in.
    "relevance": (None, False),
    "active": (1, True),
    "time": (1, True),
    "turns": (5, True),
    "credits": (6, True),
    "aiu": (6, True),
    "summary": (2, False),
    "repo": (3, False),
    # Appended to the row rather than inserted, so every index above — and
    # every view that reads one — means what it always did.
    "skills": (7, True),
    "agents": (8, True),
}
_TUI_COLUMNS = ("active", "turns", "credits", "skills", "agents", "summary", "repo")
_SORT_NAMES = "active, turns, credits, skills, agents, summary, repo, relevance"


# ── Sorting reports ──────────────────────────────────────────────────
# Listings all share one row tuple, so they sort by index. Reports do not:
# some rows are tuples straight from SQL, some are dicts built in signals.py.
# A report column is therefore a key function, which reads either shape
# without the sorter needing to know which it got.
#
# Every default below is the order the report already came back in, so asking
# for no sort changes nothing — sorting is something you opt into.

def _text_key(value) -> str:
    """Case-insensitive text, so 'Portal' and 'portal' sort together."""
    return (value or "").lower()


_VERDICT_ORDER = {"yes": 0, "high": 1, "no": 2}
_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

_REPORT_COLUMNS: dict[str, dict[str, tuple]] = {
    "repos": {
        "sessions": (lambda r: r[1] or 0, True),
        "repo": (lambda r: _text_key(r[0]), False),
        "turns": (lambda r: r[2] or 0, True),
        "credits": (lambda r: r[3] or 0, True),
        "active": (lambda r: r[4] or "", True),
    },
    "assets": {
        "sessions": (lambda r: r[1] or 0, True),
        "name": (lambda r: _text_key(r[0]), False),
    },
    "timeline": {
        "day": (lambda r: r[0] or "", False),
        "sessions": (lambda r: r[1] or 0, True),
        "turns": (lambda r: r[2] or 0, True),
        "credits": (lambda r: r[3] or 0, True),
    },
    "yolo": {
        # Inverted so that descending means riskiest first, which is what
        # "sorted by risk ↓" has to mean for it to be worth reading.
        "risk": (lambda r: (2 - _VERDICT_ORDER[r["verdict"]], r["ratio"]), True),
        "rate": (lambda r: r["ratio"], True),
        "steps": (lambda r: r["steps"], True),
        "turns": (lambda r: r["turns"], True),
        "active": (lambda r: r["active"], True),
        "summary": (lambda r: _text_key(r["summary"]), False),
    },
    "handoff": {
        "active": (lambda r: r["active"], True),
        "role": (lambda r: r["role"], False),
        "chain": (lambda r: r["chain"], True),
        "turns": (lambda r: r["turns"], True),
        "summary": (lambda r: _text_key(r["summary"]), False),
    },
    "audit": {
        # Inverted like yolo's, so that descending means most certain first —
        # anything else makes "sorted by risk ↓" read backwards.
        "risk": (lambda r: (len(redact.RANK) - r["rank"], r["count"]), True),
        "found": (lambda r: r["count"], True),
        "active": (lambda r: r["active"], True),
        "turn": (lambda r: r["turn"], False),
        "summary": (lambda r: _text_key(r["summary"]), False),
    },
    # Every cost section shares a shape — spend, a name, a count — so one
    # choice sorts all four the same way. The indices differ per section, so
    # the key comes from _COST_KEYS rather than from here.
    "coach": {
        # Severity first by default, because the list is a to-do list and a
        # to-do list sorted by name is a list nobody works down.
        "severity": (lambda f: (_SEVERITY_ORDER[f.severity], -f.share), False),
        "share": (lambda f: f.share, True),
        "group": (lambda f: (f.group, _SEVERITY_ORDER[f.severity]), False),
        "name": (lambda f: _text_key(f.name), False),
    },
    "hooks": {
        # Lifecycle order by default: a hook list is a picture of a session,
        # and alphabetical is a picture of nothing.
        "when": (lambda r: hooks.order(r["event"]), False),
        "tool": (lambda r: _text_key(r["matcher"]), False),
        "command": (lambda r: _text_key(r["command"]), False),
        "source": (lambda r: _text_key(r["source"].name), False),
    },
    "mcp": {
        # By name by default, not by usage: this is an inventory of what can
        # reach off the machine, and the one you are looking for is the one
        # you already have a name for.
        "name": (lambda r: _text_key(r["name"]), False),
        "transport": (lambda r: (r["transport"], _text_key(r["name"])), False),
        "tools": (lambda r: (r["all_tools"], len(r["tools"])), True),
        "sessions": (lambda r: r["sessions"], True),
        "source": (lambda r: _text_key(r["source"].name), False),
    },
    "cost": {
        "spend": (None, True),
        "name": (None, False),
        "calls": (None, True),
    },
}

_COST_KEYS = {
    #  section  → column → index into that section's row tuple
    "model": {"name": 0, "calls": 1, "spend": 2},
    "repo": {"name": 0, "calls": 1, "spend": 2},
    "day": {"name": 0, "spend": 1, "calls": 2},
    "session": {"name": 1, "spend": 2},
}


def _resolve_sort(report: str, sort_by: str | None, descending: bool | None):
    """(column, descending) for a report, filled in from its defaults.

    Raises ValueError naming the real choices — an unknown column is a typo,
    and a typo deserves the list rather than a stack trace.
    """
    columns = _REPORT_COLUMNS[report]
    name = (sort_by or next(iter(columns))).lower()
    if name not in columns:
        raise ValueError(
            f"unknown sort column '{sort_by}' for this report — "
            f"choose from: {', '.join(columns)}"
        )
    _, default_descending = columns[name]
    return name, default_descending if descending is None else descending


def _sort_report(rows: list, report: str, column: str, descending: bool) -> list:
    """Sort already-resolved. Stable, so ties keep the order SQL returned."""
    key, _ = _REPORT_COLUMNS[report][column]
    return sorted(rows, key=key, reverse=descending) if key else rows


def _sort_cost(rows: list, section: str, column: str, descending: bool) -> list:
    """The same, for one cost section — which knows its own column indices."""
    index = _COST_KEYS[section].get(column)
    if index is None:
        return rows
    if column == "name":
        return sorted(rows, key=lambda r: _text_key(r[index]), reverse=descending)
    return sorted(rows, key=lambda r: r[index] or 0, reverse=descending)


# Which word in a header line belongs to which sort column. Reports write
# their headers as one padded f-string, so the arrow is placed by replacing
# the word in place — which keeps every column exactly as wide as it was.
_REPOS_HEADS = {"repo": "repository", "sessions": "sessions", "turns": "turns",
                "credits": "credits", "active": "last active"}
_TIMELINE_HEADS = {"day": "day", "sessions": "sessions", "turns": "turns",
                   "credits": "credits"}
_ASSETS_HEADS = {"sessions": "sessions", "name": "name"}
_YOLO_HEADS = {"session": "session", "active": "last active", "turns": "turns",
               "steps": "steps", "rate": "per turn", "summary": "summary"}
_HANDOFF_HEADS = {"role": "role", "active": "last active", "turns": "turns",
                  "chain": "chain", "summary": "summary"}
_HOOKS_HEADS = {"when": "when", "tool": "tool", "command": "runs",
                "source": "from"}
_MCP_HEADS = {"name": "server", "transport": "transport", "tools": "tools",
              "sessions": "sessions", "source": "from"}
_COACH_HEADS = {"severity": "severity", "share": "share", "group": "group",
                "name": "name"}
_AUDIT_HEADS = {"risk": "risk", "active": "last active", "found": "found",
                "turn": "turn", "summary": "summary"}

# Colour and plain English for each severity. The word alone is a label; the
# meaning is what tells you whether it is worth your afternoon.
_RISK = {
    "critical": (ui.ROSE, "a documented key format — this can only be a credential"),
    "high": (ui.AMBER, "a credential-carrying shape: a token, or a URL login"),
    "medium": (ui.MUTED, "a value on a password-ish name — credible, not certain"),
}
_RISK_LABEL = {"critical": "critical", "high": "high", "medium": "review"}

# A credential *mentioned* and one *hardcoded* are different problems, and the
# severity scale cannot say which is which — it grades how certain the scanner
# is that a value is a credential, not what was done with it. So `hardcoded`
# replaces the label on a row that has both halves of the evidence (see
# signals._hardcoded) and takes the severity's colour up with it: a
# password-shaped assignment is a `review` finding until it is in a file the
# session wrote, at which point it is the thing you came to this page for.
_HARDCODED = (ui.ROSE, "hardcoded", "written into a file the session wrote")

# What the destructive scan looks for, in the order you would deal with it.
# The label is the section heading; the meaning is what the tier block says.
_DESTRUCTIVE_KINDS = {
    "history": (ui.ROSE, "Rewritten history"),
    "data": (ui.ROSE, "Dropped data"),
    "infra": (ui.ROSE, "Destroyed infrastructure"),
    "delete": (ui.AMBER, "Files removed"),
    "remote-exec": (ui.AMBER, "Code run from the network"),
    "privilege": (ui.MUTED, "Raised privilege"),
}
_BASIS = {
    "ran": (ui.ROSE, "ran",
            "the session reports having done it"),
    "proposed": (ui.AMBER, "proposed",
                 "offered in a code block; the store cannot say if it ran"),
}


def _fit_columns(budget: int, fixed: int, optional: list[tuple[str, int]],
                 least: int = 20, flex: str = "summary",
                 gaps: int = 0) -> dict[str, int]:
    """Room for each optional column, and whatever is left for `flex`.

    `optional` is (name, span) in the order columns may be dropped — the one
    worth least first. A column costs its span plus the space before it, and
    they go one at a time until the flexible column reaches `least`, because a
    truncated identifier is worth less than a summary you can read. A span of
    0 means leave the column out.

    `flex` names the column that absorbs what is left. Every report but one
    calls it the summary; `cs hooks` has a command there instead.

    Hand-tuned width steps got this wrong at the ends: a table can only be as
    wide as its narrowest useful column set, and guessing that per report left
    rows running off a small window.
    """
    spans = dict(optional)
    droppable = [name for name, _ in optional]

    def used() -> int:
        return fixed + sum(span + 1 for span in spans.values() if span)

    while droppable and budget - used() < least:
        spans[droppable.pop(0)] = 0
    # `gaps` is what _row will spend on the second space between a number and
    # the text beside it. It comes out of the flexible column at the end
    # rather than off the budget at the start, so the widest thing on the row
    # loses a character instead of a whole column disappearing at the margin.
    spans[flex] = max(8, budget - used() - gaps)
    return spans


def _cell(text: str, span: int, align: str = "<", colour: str = "") -> str:
    """One table cell: truncated, padded, then coloured.

    In that order — an escape code takes no columns on screen but does take
    len(), so padding a coloured string pads it to the wrong width.
    """
    fitted = ui._fit(text, span)
    gap = " " * (span - ui.cells(fitted))
    if align == ">":
        padded = gap + fitted
    elif align == "^":
        left = len(gap) // 2
        padded = gap[:left] + fitted + gap[left:]
    else:
        padded = fitted + gap
    return f"{colour}{padded}{ui.RST}" if colour else padded


def _row(shown: list[tuple[str, str, str]], spans: dict[str, int],
         values: dict[str, tuple[str, str]] | None = None) -> str:
    """One table line, with its columns spaced apart.

    The headings when `values` is None, and a row of the table when it is a
    {column: (text, colour)} mapping — one function, so the two can never be
    spaced differently and leave the rule measured off the wrong one.

    Two spaces where a right-aligned number meets left-aligned text, one
    everywhere else. A number ends flush against its own edge, so a single
    space put `58.0` and the word beside it in contact and the pair read as
    one value; `steps  per turn summary` read as a sentence rather than as
    three headings. Every other join is already separated by the padding
    inside the cells themselves and does not need the second space.

    :func:`_extra_gaps` counts what the spacing costs and `_fit_columns`
    takes it out of the flexible column, so a table that spaces itself never
    runs a row off a narrow window.
    """
    out = []
    for index, (key, head, align) in enumerate(shown):
        if index:
            out.append("  " if shown[index - 1][2] == ">" and align == "<" else " ")
        text, colour = (head, "") if values is None else values[key]
        out.append(_cell(text, spans[key], align, colour))
    return "".join(out)


def _extra_gaps(columns: list[tuple[str, str, str]]) -> int:
    """Columns `_row` will spend a second space on, counted before the fit.

    Measured off the full column list rather than the surviving one, so the
    reservation can only ever be too generous — a window that drops a column
    gets a slightly wider summary, never a row one character too long.
    """
    return sum(1 for before, after in zip(columns, columns[1:], strict=False)
               if before[2] == ">" and after[2] == "<")


def _chart_spans(inner: int, fixed: int, name_cap: int = 34,
                 gauge_cap: int = 40) -> tuple[int, int]:
    """(name, bar) for a chart row whose other columns cost `fixed` columns.

    Both ends are capped. Past about thirty-four characters a name column is
    whitespace between a label and its bar, and past about forty a bar is a
    ruler rather than a shape — a wide terminal should not turn a five-row
    chart into two things at opposite edges of the screen.
    """
    span = max(10, min(name_cap, inner - fixed - 8))
    return span, max(0, min(gauge_cap, inner - fixed - span))


def _name_grid(names: list[str], inner: int, limit: int = 24,
               indent: int = 4) -> list[str]:
    """A list of bare names laid out in as many columns as the window holds.

    Every inventory ends with one of these — the skills nothing referenced,
    the servers nobody called. It used to be three hard-coded 30-character
    columns, which ran 34 characters off an 80-column terminal and left a
    third of a 140-column one empty.
    """
    if not names:
        return []
    room = max(inner - indent, 12)
    widest = max(ui.cells(name) for name in names[:limit]) + 2
    columns = max(1, min(len(names), room // max(widest, 12)))
    span = room // columns
    rows = []
    for start in range(0, min(len(names), limit), columns):
        line = "".join(f"{ui._fit(name, span - 2):<{span}}"
                       for name in names[start:start + columns])
        rows.append(" " * indent + line.rstrip())
    return rows


def _around(line: str, mark: str, span: int, lead: int = 14) -> str:
    """`line` trimmed to `span`, with `mark` kept in view.

    Truncating from the left cut off the very thing the line is being shown
    for — a masked value 60 characters in left an ellipsis and no evidence.
    """
    at = line.find(mark)
    if at >= 0 and at + len(mark) > span:
        line = "…" + line[max(0, at - lead):]
    return ui.trunc(line, span)


def _audit_command(row: dict) -> str:
    """The command that opens the exact place a finding was read from.

    A checkpoint is not a turn: `--turn 0` would open the first message of
    the session, which is not where the value is. `cs show` is what reads a
    checkpoint back, so that is what the row offers.
    """
    if row.get("source") == "checkpoint":
        return f"cs show {row['id'][:8]}"
    return f"cs read {row['id'][:8]} --turn {row['turn']}"


def _names(names: list[str], span: int) -> str:
    """As many whole finding names as fit, then how many are left.

    Cutting the list mid-word — `SPassword, SpMS_DBPas…` — names something you
    cannot search for and hides how much else is there. Whole names and a
    `+2` say both, in the same room.
    """
    if not names:
        return ""
    shown: list[str] = []
    for name in names:
        hidden = len(names) - len(shown) - 1
        candidate = ", ".join([*shown, name]) + (f" +{hidden}" if hidden else "")
        if ui.cells(candidate) > span:
            break
        shown.append(name)
    if not shown:
        return ui._fit(names[0], span)
    hidden = len(names) - len(shown)
    return ", ".join(shown) + (f" +{hidden}" if hidden else "")


def _mark_column(header: str, column: str, heads: dict[str, str],
                 descending: bool) -> str:
    """Highlight the heading a report is sorted by.

    Colour rather than an arrow: reports pad their columns to the width of the
    heading word, so adding a character would push every row out of line. The
    direction is spelled out in the footer instead, where there is room.
    """
    word = heads.get(column)
    if not word or word not in header:
        return header
    # Callers print this inside a MUTED…RST pair, so it is restored on the way out.
    return header.replace(
        word, f"{ui.RST}{ui.ACCENT}{ui.BOLD}{word}{ui.RST}{ui.MUTED}", 1
    )


def _head_rule(heads: str, indent: int = 4, column: str = "",
               heads_map: dict[str, str] | None = None,
               descending: bool = True) -> None:
    """A table's heading row and the rule under it, from one measured string.

    Six reports wrote these two lines themselves and no two wrote them the
    same way: some rstripped the heading and some shipped a row of trailing
    spaces, some measured the rule with `len` and some with `ui.cells`, and
    one measured it off a different string from the rows it was dividing.

    The heading is trimmed and the rule is not: the rule is what every row is
    checked against, so it spans the full table even where the last heading
    word stops early.
    """
    pad = " " * indent
    marked = (_mark_column(heads, column, heads_map, descending)
              if heads_map else heads)
    print(f"{pad}{ui.MUTED}{marked.rstrip()}{ui.RST}")
    print(f"{pad}{ui.MUTED}{'─' * ui.cells(heads)}{ui.RST}")


def _tiers(rows: list[tuple[int, str, str, str]], total: int, inner: int,
           indent: int = 4, gauge: int = 10) -> None:
    """How a count breaks down: number, label, share of the whole, meaning.

    Autonomy and Security both open by sorting everything into three named
    tiers, and both used to draw that differently — one as a bar chart, one
    as a run-on line of chips that stopped fitting somewhere past a hundred
    columns. Two pages that answer the same shape of question should look
    like each other, so they share this.

    The bar is what stops a tier being read in isolation: "6 YOLO" means one
    thing in a store of twelve sessions and another in a store of a thousand,
    and the share is the cheapest way to say which. It is the first thing to
    go on a narrow window — what a tier *means* outlives how much of the
    store it covers.
    """
    pad = " " * indent
    if gauge and inner < 54:
        gauge = 0
    label_span = max(ui.cells(label) for _count, label, _c, _m in rows) + 1
    for count, label, colour, meaning in rows:
        share = (f"{ui.bar(count, total, gauge, colour=colour, track=True)} "
                 if gauge else "")
        room = inner - indent - 6 - label_span - gauge - (1 if gauge else 0)
        print(f"{pad}{colour}{count:>5}{ui.RST}  "
              f"{_cell(label, label_span, colour=colour)} {share}"
              f"{ui.MUTED}{ui.trunc(meaning, max(12, room))}{ui.RST}")


def _hint(text: str, inner: int, indent: int = 2) -> None:
    """The muted one-liner that points at the next command to type.

    Every report ends with one or two of these. `cs yolo` used to render its
    own through `ui.field`, which is a metadata row — it aligned the sentence
    into a value column nine characters in and then ran it off the window,
    because a field value is not a hint and does not know the report's width.
    """
    print(f"{' ' * indent}{ui.MUTED}{ui._fit(text, inner - indent)}{ui.RST}")


def _sort_note(report: str, column: str, descending: bool, width: int) -> str:
    """The footer every sortable report ends with: what it did, and the choices.

    Shrinks by dropping whole clauses rather than being cut mid-word, the same
    rule the TUI hint line follows — what is left always reads as a sentence.
    """
    head = f"Sorted by {column} {'↓' if descending else '↑'}"
    choices = f"--sort {'|'.join(_REPORT_COLUMNS[report])} [--asc|--desc]"
    reader = "←/→ and s re-sort in the reader"

    def fits(*bits: str) -> bool:
        return len("  ·  ".join(bits)) + 2 <= width

    if fits(head, choices, reader):
        lines = [f"{head}  ·  {choices}  ·  {reader}"]
    elif fits(head, choices):
        lines = [f"{head}  ·  {choices}", reader]
    else:
        lines = [head, choices, reader]
    return "\n".join(
        f"  {ui.MUTED}{ui.trunc(line, max(width - 2, 12))}{ui.RST}" for line in lines
    )


def _number_rows(rows: list[tuple],
                 existing: dict[str, int] | None = None) -> dict[str, int]:
    """Stable #N per session, assigned once in the listing's natural order.

    Numbers identify a session, not a screen position, so sorting or
    filtering never repoints a number at a different session — and neither
    does a refresh. A session that arrives while the view is open takes the
    next free number rather than pushing everything below it down one: the
    status line offers `cs resume N` as something to type *after* you quit,
    and a number that meant one session when you read it and another by the
    time you used it would be worse than no number at all.
    """
    if not existing:
        return {row[0]: i for i, row in enumerate(rows, 1)}
    numbers = {row[0]: existing[row[0]] for row in rows if row[0] in existing}
    spare = max(existing.values(), default=0) + 1
    for row in rows:
        if row[0] not in numbers:
            numbers[row[0]] = spare
            spare += 1
    return numbers


def _sort_rows(
    rows: list[tuple],
    sort_by: str | None,
    descending: bool | None,
    rank: dict[str, int] | None = None,
) -> tuple[list[tuple], bool]:
    if not sort_by:
        return rows, True
    try:
        index, default_descending = _SORT_COLUMNS[sort_by.lower()]
    except KeyError:
        raise ValueError(
            f"unknown sort column '{sort_by}' — choose from: {_SORT_NAMES}"
        ) from None
    direction = default_descending if descending is None else descending
    if index is None:  # relevance
        order = rank or {}
        return (
            sorted(rows, key=lambda row: order.get(row[0], 0), reverse=direction),
            False,
        )

    def key(row: tuple):
        return row[index] or ("" if index in (2, 3) else 0)

    return sorted(rows, key=key, reverse=direction), index == 1


def _render_listing(
    rows: list[tuple],
    title: str,
    show_all: bool,
    sort_by: str | None = None,
    descending: bool | None = None,
    default_sort: str = "active",
    hits: dict[str, tuple[str, str]] | None = None,
    term: str = "",
) -> None:
    rows = _visible(rows, show_all)
    numbers = _number_rows(rows)
    try:
        rows, group_by_day = _sort_rows(rows, sort_by or default_sort, descending, numbers)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return
    active_sort = (sort_by or default_sort).lower()
    active_sort = {"time": "active", "aiu": "credits"}.get(active_sort, active_sort)
    default_descending = _SORT_COLUMNS[active_sort][1]
    is_descending = default_descending if descending is None else descending

    width = min(shutil.get_terminal_size().columns, 96)
    print()
    sort_note = (
        " · best match first"
        if active_sort == "relevance" and not is_descending
        else f" · sorted by {active_sort} {'↓' if is_descending else '↑'}"
    )
    print(f"  {ui.BOLD}{ui._fit(title + sort_note, width - 4)}{ui.RST}")
    print()
    if not rows:
        print(f"  {ui.DIM}No sessions found.{ui.RST}")
        if not show_all:
            print(f"  {ui.DIM}(try 'cs all' to include quiet and automated sessions){ui.RST}")
        print()
        return

    labels = {
        name: f"{name.title()}{'↓' if is_descending else '↑'}"
        if active_sort == name
        else name.title()
        for name in ("active", "turns", "credits", "skills", "agents", "summary")
    }
    # The kit columns are the first thing to go on a narrow window: they are
    # a review column, and the row still has to be readable on a laptop
    # half-width. Below the threshold the summary gets their space back.
    kit = width >= _KIT_MIN_WIDTH
    kit_head = (
        f" {labels['skills']:>6} {labels['agents']:>6}" if kit else ""
    )
    print(
        f"  {ui.DIM}  #   {labels['active']:<7} {labels['turns']:<6}"
        f" {labels['credits']:<8}{kit_head} {labels['summary']}{ui.RST}"
    )
    # Sized to the window like every other view. The full-screen listing has
    # always been fluid; this is the one you get when the output is piped or
    # a report is printed straight out, and it was pinned at 72 columns.
    print(f"  {ui.DIM}{'─' * (width - 4)}{ui.RST}")

    index: dict[int, str] = {}
    current_day = ""
    for sid, started, summary, repo, cwd, turns, nano_aiu, *kit_values in rows:
        skills, agents = (kit_values + [0, 0])[:2]
        day = started[:10]
        clock = started[11:16] if len(started) >= 16 else "     "
        if group_by_day and day != current_day:
            if current_day:
                print()
            print(f"  {ui.AMBER}{ui.BOLD}{ui.friendly_day(day)}{ui.RST}")
            current_day = day
        n = numbers[sid]
        index[n] = sid
        # What is left after the fixed columns, split between the summary and
        # the repository tag — the tag capped, because past twenty-odd
        # characters it is a path and the summary is the thing being read.
        # The fixed columns cost 24 plus the timestamp — five characters when
        # the rows are grouped by day and carry a clock, eleven when they are
        # not and carry a date. Whatever is left is the summary's, and on a
        # window with nothing left the row is the numbers alone: a summary cut
        # to three characters is not a summary, and a row that runs off the
        # window is not a row.
        room = max(0, width - 24 - (5 if group_by_day else 11) - (14 if kit else 0))
        tag = _project_tag(repo, cwd)
        tag_span = min(22, room // 3) if tag and room >= 12 else 0
        summary = redact.one_line(redact.redact(summary))
        # The tag costs its span plus the leading space and the '#'.
        title_txt = (ui._fit(summary, room - tag_span - 2) if summary
                     else f"{ui.DIM}{_no_summary(turns)}{ui.RST}")
        tag_txt = f" {ui.SKY}#{ui._fit(tag, tag_span)}{ui.RST}" if tag_span else ""
        if room < 8:
            title_txt, tag_txt = "", ""
        turns_txt = f"{turns:>3}" if turns else f"{ui.DIM}  0{ui.RST}"
        cred = ui.fmt_aiu(nano_aiu)
        cred_txt = f"{ui.VIOLET}{cred:>7}{ui.RST}" if cred != "-" else f"{ui.DIM}{cred:>7}{ui.RST}"
        num = f"{ui.SKY}{n:>3}{ui.RST}"
        active = clock if group_by_day else started[5:16]
        row = (f"  {num}  {ui.DIM}{active}{ui.RST}  {turns_txt}  {cred_txt}"
               f"{_kit_cells(skills, agents) if kit else ''}   {title_txt}{tag_txt}")
        print(row)
        if hits and sid in hits:
            source, snippet = hits[sid]
            print(f"       {ui.DIM}{_SOURCE_LABELS.get(source, source)}{ui.RST} "
                  f"{_highlight(ui._fit(_hit_text(source, snippet), max(20, width - 18)), term)}")

    print()
    sortable = ("active|turns|credits|skills|agents|summary|repo"
                + ("|relevance" if hits else ""))
    _note(f"Sort: --sort {sortable} [--asc|--desc]", width - 4, indent=2)
    _note("Credits = AI units (AIU) spent  ·  cs show #1  ·  cs read #1  ·  "
          "cs resume #1", width - 4, indent=2)
    if kit:
        _note("Skills = skills the session used  ·  Agents = sub-agents it ran"
              "  ·  cs show #1 names them and shows the evidence",
              width - 4, indent=2)
    print()
    _save_index(index)


def _filter_rows(rows: list[tuple], query: str) -> list[tuple]:
    """Rows whose summary, repo or directory contain the query (case-insensitive)."""
    if not query:
        return rows
    q = query.lower()
    return [r for r in rows if any(q in (r[i] or "").lower() for i in (2, 3, 4))]


def _interactive_listing(
    rows: list[tuple],
    title: str,
    show_all: bool,
    default_sort: str = "active",
    hits: dict[str, tuple[str, str]] | None = None,
    term: str = "",
    reload=None,
) -> bool:
    """True when the full-screen view ran and so already waited for the user.

    It does not always run: with nothing to list, or on a terminal curses
    cannot drive, this prints instead — and a caller that skipped its pause
    on the strength of a blind True would wipe that message on the redraw.
    """
    import curses

    rows = _visible(rows, show_all)
    if not rows:
        _render_listing(rows, title, show_all, default_sort=default_sort,
                        hits=hits, term=term)
        return False

    # Actions run after curses has restored the terminal, so `show` prints
    # normally and `resume` can hand the terminal straight to `copilot`.
    # Showing a session then returns to the listing, which is why the view is
    # carried across in 'state' — the trip out and back is invisible.
    state: dict = {}
    while True:
        try:
            action = _curses_wrapper(
                _listing_tui, rows, title, default_sort, hits, state, reload
            )
            # The view re-reads the store on its own heartbeat; what it read
            # last is what a trip out to a detail view should come back to.
            rows = state.get("rows", rows)
            title = state.get("title", title)
        except KeyboardInterrupt:
            return True
        except curses.error:
            _render_listing(rows, title, show_all, default_sort=default_sort,
                            hits=hits, term=term)
            return False
        finally:
            # endwin restores ncurses' own reporting; the SGR switch is ours.
            _disable_mouse()

        if not action:
            return True
        verb, session_id = action
        if verb == "resume":
            _resume_from_listing(session_id)
            continue
        # A pager already waited for the user, so returning is immediate;
        # output printed straight to the terminal needs an explicit pause,
        # otherwise the listing would wipe it before it could be read.
        viewer = {"read": cmd_read, "show": cmd_show}[verb]
        dismissed = viewer(session_id)
        if not dismissed and not _pause("Esc or Enter for the list · q quits "):
            return True


def _drain_stdin() -> None:
    """Discard keys typed before the prompt appeared.

    A key pressed while the detail view was on screen — 'q' to dismiss a
    pager, say — would otherwise be read as the answer to the next question
    and quit the listing on the user's behalf.
    """
    if not sys.stdin.isatty():
        # Nothing was typed ahead into a pipe, and flushing one raises:
        # termios.error derives from Exception, not OSError, so it slipped
        # past the guard below and crashed the caller.
        return
    try:
        import termios

        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:  # noqa: BLE001 — termios.error is not an OSError
        pass


def _read_key() -> str | None:
    """One keypress, no Enter needed. None when stdin can't be put in raw mode.

    Escape only reaches us as a keystroke if we stop the terminal buffering
    whole lines — typed into input() it is just another character in the line,
    which is why Esc did nothing here before.
    """
    try:
        import termios
        import tty
    except ImportError:  # pragma: no cover — POSIX only
        return None
    try:
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
    except Exception:  # noqa: BLE001 — termios.error is not an OSError
        return None
    try:
        tty.setcbreak(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        # An arrow key is Esc plus two more bytes; without this the leftovers
        # would be read as keystrokes by whatever draws next.
        _drain_stdin()


def _pause(message: str) -> bool:
    """Wait after a detail view. False means the user asked to stop here."""
    _drain_stdin()
    prompt = f"  {ui.DIM}{message}{ui.RST}"
    try:
        print(prompt, end="", flush=True)
        key = _read_key()
        if key is None:
            return input().strip().lower() not in ("q", "quit")
        print()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return bool(key) and key.lower() not in ("q", "\x03", "\x04")


def _prompt(screen, theme, y: int, width: int, prefix: str, initial: str) -> str | None:
    """Read a line of text at row y. Returns None if cancelled with Esc."""
    import curses

    text = initial
    # Reports read while typing are dropped rather than pushed back: a stray
    # escape sequence belongs in neither the filter text nor the quit path.
    swallow: list[int] = []
    while True:
        _addstr(screen, y, 0, f"{prefix}{text}▏".ljust(width), width, theme["status"])
        screen.refresh()
        try:
            key = screen.getch()
        except KeyboardInterrupt:
            return None  # cancel the filter, same as Esc
        if key == -1:
            continue  # a timeout the caller left armed, not a keypress
        if key in (10, 13, curses.KEY_ENTER):
            return text
        if key == 27:
            # Esc cancels — but only a real Esc. Scrolling mid-filter used to
            # cancel it and then type the report's own bytes into the box.
            if _mouse_event(screen, curses, key, [0.0, -1], swallow):
                swallow.clear()
                continue
            return None
        if key in (curses.KEY_BACKSPACE, 127, 8):
            text = text[:-1]
        elif 32 <= key <= 126:
            text += chr(key)


_DOUBLE_CLICK_SECONDS = 0.4
_SGR_ENABLED = False
_MOUSE_USED = False
_CURSES_MOUSE_COMPAT: bool | None = None


def _curses_wrapper(view, *args):
    """Keep legacy ncurses from swallowing native SGR motion and wheel events."""
    import curses

    global _CURSES_MOUSE_COMPAT
    if _CURSES_MOUSE_COMPAT is None:
        _CURSES_MOUSE_COMPAT = False
        if not hasattr(curses, "BUTTON5_PRESSED"):
            curses.setupterm()
            _CURSES_MOUSE_COMPAT = curses.tigetstr("kmous") == b"\033[<"
    if not _CURSES_MOUSE_COMPAT:
        return curses.wrapper(view, *args)
    # Probe once: ncurses caches the compatibility entry after the first view.
    # Keyboard decoding stays native; the existing parser handles raw SGR.
    original = os.environ.get("TERM")
    os.environ["TERM"] = "xterm-256color"
    try:
        return curses.wrapper(view, *args)
    finally:
        if original is None:
            os.environ.pop("TERM", None)
        else:
            os.environ["TERM"] = original


def _enable_mouse(curses, *, motion: bool = False) -> bool:
    """Turn on click and wheel reporting. False when the terminal can't do it.

    Two protocols are enabled at once, because neither covers everything:

    * ncurses' own (X10) reporting, which it enables and restores itself.
      Python is commonly built against the older mouse ABI, where button 5
      does not exist — so this path can never report wheel-down.
    * SGR reporting, switched on by hand below. It only changes how the
      terminal encodes the same events, so terminals that understand it
      report everything (including wheel-down and columns past 223) in a
      form ncurses passes straight through for :func:`_sgr_report` to read.
      Terminals that ignore it keep sending X10, which ncurses still parses.
    """
    global _SGR_ENABLED, _MOUSE_USED
    # Menus request motion to preview the row under the pointer; readers do not.
    # Button presses remain excluded: ncurses can still synthesise clicks and
    # double-clicks rather than making every view pair press/release events.
    wanted = (
        curses.BUTTON1_CLICKED
        | curses.BUTTON1_DOUBLE_CLICKED
        | curses.BUTTON4_PRESSED
        | getattr(curses, "BUTTON5_PRESSED", 0)
        | (getattr(curses, "REPORT_MOUSE_POSITION", 0) if motion else 0)
    )
    try:
        available, _ = curses.mousemask(wanted)
        # Long enough to catch a comfortable double-click, short enough that a
        # plain click still feels instant.
        curses.mouseinterval(200)
    except (curses.error, AttributeError):
        return False
    if available:
        try:
            # Esc now starts a mouse report, so the wait for the rest of one
            # is what delays a bare Esc — keep it short.
            curses.set_escdelay(25)
        except (curses.error, AttributeError):
            pass
        _write_terminal(("\033[?1003h" if motion else "") + "\033[?1006h")
        _SGR_ENABLED = _MOUSE_USED = True
    return bool(available)


def _disable_mouse() -> None:
    """Undo the SGR switch; ncurses restores its own reporting at endwin."""
    global _SGR_ENABLED
    if _SGR_ENABLED:
        _SGR_ENABLED = False
        _write_terminal("\033[?1003l\033[?1006l")
    if _MOUSE_USED:
        # A flick of the wheel outruns any redraw, so reports are still queued
        # when we stop reading them. Left there, the shell reads them next and
        # echoes the raw escapes over its own prompt. Draining is safe to
        # repeat, and has to run again once curses has restored the terminal.
        _drain_stdin()


def _write_terminal(sequence: str) -> None:
    try:
        sys.stdout.write(sequence)
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def _getch(screen, tries: int = 12) -> int:
    """Read a buffered byte, briefly waiting for one still in flight."""
    for _ in range(tries):
        try:
            key = screen.getch()
        except KeyboardInterrupt:
            return -1  # let the main loop see the interrupt and quit
        if key != -1:
            return key
        time.sleep(0.002)
    return -1


def _sgr_report(screen, pending: list[int]) -> tuple[int, int, int, bool] | str | None:
    """Read what follows an Esc. One of three things comes back:

    * ``(button, x, y, pressed)`` — an ``ESC [ < b ; x ; y (M|m)`` mouse report.
    * ``'consumed'`` — some other escape sequence, swallowed whole. Anything
      ncurses didn't recognise ends up here, and must not reach the caller as
      a bare Esc, or an unknown arrow form would quit the listing.
    * ``None`` — the Esc really was a keypress.
    """
    screen.nodelay(True)
    try:
        opener = _getch(screen, tries=3)
        if opener == -1:
            return None  # nothing followed: a real Esc
        if opener != ord("["):
            if opener == ord("O"):  # SS3, e.g. an application-mode arrow
                _getch(screen)
                return "consumed"
            pending.append(opener)  # Alt-<key>: let the key through
            return None
        marker = _getch(screen)
        if marker != ord("<"):
            if not _consume_sequence(screen, marker):
                _note_partial_sequence()
            return "consumed"
        digits = ""
        while True:
            char = _getch(screen)
            if char == -1:
                _note_partial_sequence()
                return "consumed"
            if len(digits) > 24:
                return "consumed"
            if char in (ord("M"), ord("m")):
                pressed = char == ord("M")
                break
            digits += chr(char)
    finally:
        screen.nodelay(False)

    parts = digits.split(";")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return "consumed"
    button, column, row = (int(part) for part in parts)
    return button, column - 1, row - 1, pressed  # reported 1-based


def _consume_sequence(screen, char: int) -> bool:
    """Swallow the rest of a CSI sequence, which ends on a byte in @…~.

    False when the terminator has not arrived: -1 means nothing has been read
    *yet*, which is not the same as the sequence being over. Treating the two
    alike is what let the tail of a split report reach the caller as typing.
    """
    for _ in range(24):
        if 0x40 <= char <= 0x7E:
            return True
        if char == -1:
            return False
        char = _getch(screen)
    return True  # 24 bytes in, this is not a report — stop eating keys


# How long after the parser abandons a sequence its remaining bytes are still
# read as that sequence rather than as typing. A terminal splits a report
# across reads in microseconds; a human cannot press Esc and then the next
# key inside a sixth of a second, so nothing anyone types falls in here.
_ESC_ORPHAN_SECONDS = 0.15
_ESC_AT = 0.0  # when a bare Esc was last let through to the caller
_CSI_OPEN = False  # …and whether its sequence was already part-read


def _note_bare_esc() -> None:
    """Remember that an Esc went out unaccompanied, so its tail can be spotted."""
    global _ESC_AT, _CSI_OPEN
    _ESC_AT = time.monotonic()
    _CSI_OPEN = False


def _note_partial_sequence() -> None:
    """Remember that a sequence was abandoned part-read, mid-CSI.

    The Esc and at least the '[' are already spent, so what lands next is the
    middle of a report rather than its opening byte. `_orphaned_sequence`
    swallows it whatever it starts with.
    """
    global _ESC_AT, _CSI_OPEN
    _ESC_AT = time.monotonic()
    _CSI_OPEN = True


def _orphaned_sequence(screen, key: int) -> bool:
    """True when this key continues a report the parser has already left.

    `_sgr_report` waits a few milliseconds for each byte of a report and, if
    nothing has arrived, gives up. That is the right call for a bare Esc and
    the wrong one for a report the terminal split across two reads: the bytes
    already read are gone, and the rest arrives afterwards as perfectly
    ordinary printable keys.

    Every loop here types printable keys into its filter box, so the tail was
    landing there as text — which is what put ``nothing matches '[<'`` on the
    landing screen, and ``nothing matches '<64;44;22M'`` when the split fell
    one byte later. A sequence can break at **any** byte, so the seam is not
    predictable: what makes it safe is that the parser says where it stopped.
    After a bare Esc only a CSI introducer can continue it; once the '[' has
    been read the next byte is mid-report and could be anything.
    """
    global _ESC_AT, _CSI_OPEN
    if not _SGR_ENABLED:
        return False
    if not _CSI_OPEN and key not in (ord("["), ord("O")):
        return False
    if time.monotonic() - _ESC_AT > _ESC_ORPHAN_SECONDS:
        return False
    mid = _CSI_OPEN
    _ESC_AT = 0.0  # one tail per Esc
    _CSI_OPEN = False
    screen.nodelay(True)
    try:
        # Mid-report, this key is itself part of the sequence; after a bare
        # Esc it is only the introducer, and the sequence starts behind it.
        done = _consume_sequence(screen, key if mid else _getch(screen))
    finally:
        screen.nodelay(False)
    if not done:
        _note_partial_sequence()  # it split again; keep swallowing
    return True


def _mouse_event(
    screen, curses, key: int, last_click: list, pending: list[int]
) -> tuple[str, int, int] | None:
    """Normalise either protocol's report into (kind, x, y).

    Kinds are 'click', 'double', 'move', 'wheel-up', 'wheel-down' and 'ignored' — the
    last for a report that was consumed but means nothing here. Only a
    genuine keypress returns None, because an SGR report starts with Esc and
    the caller must not mistake one for the other.
    """
    if key == curses.KEY_MOUSE:  # X10, decoded by ncurses
        try:
            _, x, y, _, state = curses.getmouse()
        except curses.error:
            return "ignored", 0, 0  # outside our mask (wheel-down lands here)
        if state & curses.BUTTON4_PRESSED:
            return "wheel-up", x, y
        if state & getattr(curses, "BUTTON5_PRESSED", 0):
            return "wheel-down", x, y
        if state & curses.BUTTON1_DOUBLE_CLICKED:
            return "double", x, y
        if state & curses.BUTTON1_CLICKED:
            return "click", x, y
        if state & getattr(curses, "REPORT_MOUSE_POSITION", 0):
            return "move", x, y
        return "ignored", x, y

    if key != 27:
        if _orphaned_sequence(screen, key):
            return "ignored", 0, 0
        return None
    report = _sgr_report(screen, pending)
    if report is None:
        _note_bare_esc()
        return None  # a real Esc — the caller decides what it means
    if report == "consumed":
        return "ignored", 0, 0
    button, x, y, pressed = report
    if button == 64:
        return "wheel-up", x, y
    if button == 65:
        return "wheel-down", x, y
    if button & 32:
        return "move", x, y
    if button != 0 or pressed:
        # Only button 1 acts, and only on release — that is one full click.
        # Still 'ignored' rather than None: the Esc that opened this report
        # must not fall through to the quit key.
        return "ignored", x, y
    now = time.monotonic()
    was, where = last_click
    last_click[:] = [now, y]
    if now - was <= _DOUBLE_CLICK_SECONDS and where == y:
        last_click[0] = 0.0  # a third click starts a new pair
        return "double", x, y
    return "click", x, y


_HOME_ACTIVE = False  # set while the landing screen owns the loop
# How often a view on screen re-reads the store. No longer home-only: the
# landing page and an open session listing share one heartbeat, and every
# line that quotes the interval reads it from here rather than spelling it
# out, so the two can never drift apart.
_REFRESH_SECONDS = 60


def _fit_hints(hints: list[tuple[str, str, int]], width: int) -> str:
    """Join hints so they fit the window, dropping by rank rather than cutting.

    Each hint is (long form, short form, drop rank). Higher ranks go first;
    rank 0 is the way out and never goes.
    """
    for form in (0, 1):
        line = " · ".join(hint[form] for hint in hints)
        if ui.cells(line) <= width:
            return line
    for rank in (4, 3, 2, 1):
        if ui.cells(" · ".join(hint[1] for hint in hints)) <= width:
            break
        hints = [hint for hint in hints if hint[2] != rank]
    return " · ".join(hint[1] for hint in hints)


def _hint_line(width: int, mouse: bool, back: str = "quit") -> str:
    """Key hints that shrink to fit rather than being cut off mid-word.

    A fixed string was truncated by the terminal width, so on a narrow window
    the hints ended at '←/→ sort · E' and the keys that matter most —
    transcript, the session page, the way out — were the ones off the end.
    Each hint carries how readily it can go, so what survives is what you
    cannot guess.
    """
    hints = [
        # long form, short form, dropped on this pass (higher goes first)
        ("↑/↓ row", "↑↓", 3),
        ("←/→ sort", "←→", 3),
        ("Enter resume", "↵ resume", 1),
        # v and o both open the session page. There were three detail keys
        # when there were three views; there are two views now, and the
        # cheapest way to retire a key nobody asked to lose is to point it
        # at the page that absorbed what it used to show.
        ("v/o session", "v show", 1),
        ("t transcript", "t read", 1),
        ("/ filter", "/", 3),
        (f"q {back}", f"q {back}", 0),  # rank 0 never drops: it is the way out
    ]
    if mouse:
        hints.insert(0, ("click/scroll · double-click resume", "click", 4))
    return _fit_hints(hints, width)


def _reread_listing(reload, rows: list[tuple], title: str,
                    numbers: dict[str, int], state: dict, here: str | None):
    """Re-read the store for an open listing. Returns what to draw next.

    A store that has not changed returns the rows already on screen, so a
    quiet heartbeat costs one query and redraws nothing that moves. A store
    that has changed keeps the highlight on the session it was on — a new
    arrival must not pull the cursor onto itself while someone is reading.
    """
    try:
        fresh, fresh_title = reload()
    except (OSError, sqlite3.Error):
        # The store is Copilot's and it is being written to. A refresh that
        # cannot read it is a refresh that does not happen; the rows already
        # on screen are still the best answer there is.
        return rows, title, numbers, None
    if [row[0] for row in fresh] == [row[0] for row in rows] and fresh == rows:
        return rows, title, numbers, None
    numbers = _number_rows(fresh, numbers)
    _save_index({n: sid for sid, n in numbers.items()})
    state["rows"], state["title"] = fresh, fresh_title
    return fresh, fresh_title, numbers, here


def _listing_tui(
    screen,
    rows: list[tuple],
    title: str,
    default_sort: str = "active",
    hits: dict[str, tuple[str, str]] | None = None,
    state: dict | None = None,
    reload=None,
) -> tuple[str, str] | None:
    import curses

    # 'state' carries the view across a trip out to a detail view and back.
    state = {} if state is None else state

    screen.keypad(True)
    theme = ui.tui_theme(curses)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    try:
        screen.bkgd(" ", theme["background"])
    except curses.error:
        pass
    mouse = _enable_mouse(curses)

    sort_by = state.get("sort_by", default_sort)
    descending = state.get("descending", _SORT_COLUMNS[sort_by][1])
    offset = state.get("offset", 0)
    cursor = state.get("cursor", 0)
    query = state.get("query", "")
    follow = None  # session the cursor should stay on across a re-sort
    last_click = [0.0, -1]  # time and row, for pairing SGR clicks into one
    pending: list[int] = []  # keys read while probing for a mouse report
    # Relevance only makes sense for a search, and there it lives on the '#'
    # column — the numbers were handed out in best-match order.
    cycle = (
        ("relevance", *_TUI_COLUMNS) if default_sort == "relevance" else _TUI_COLUMNS
    )
    # Numbers are stable, so the whole map is saved once — a session stays
    # resolvable by its number even while filtered off screen.
    numbers = _number_rows(rows)
    _save_index({n: sid for sid, n in numbers.items()})

    # The store is live. A session started in another window used to arrive
    # here only when the view was next reopened, which on a screen you sit
    # and watch reads as the listing being wrong. `reload` re-reads it on
    # the same heartbeat the landing page uses; a search does not get one,
    # because re-ranking a result set under the reader is not a refresh.
    def wait(milliseconds: int) -> bool:
        """Ask for a timed getch. False when this window cannot do one."""
        try:
            screen.timeout(milliseconds)
        except (AttributeError, curses.error):
            return False
        return True

    timed = reload is not None and wait(-1)
    next_refresh = time.monotonic() + _REFRESH_SECONDS

    try:
        while True:
            sorted_rows, _ = _sort_rows(
                _filter_rows(rows, query), sort_by, descending, numbers
            )
            if follow is not None:
                # Re-sorting moves rows around; keep the highlight on the session
                # the user picked rather than on whatever lands at that position.
                cursor = next(
                    (i for i, row in enumerate(sorted_rows) if row[0] == follow), cursor
                )
                follow = None

            screen.erase()
            height, width = screen.getmaxyx()
            # A search keeps the bottom line for the matching text of the row.
            visible = max(height - 6 - (1 if hits else 0), 1)
            cursor = min(cursor, max(len(sorted_rows) - 1, 0))
            # Keep the cursor on screen; scrolling follows it rather than the reverse.
            offset = min(max(offset, cursor - visible + 1), cursor)
            offset = max(offset, 0)
            arrow = "↓" if descending else "↑"

            if sort_by == "relevance":
                heading = f"◆  {title} · best match {'last' if descending else 'first'}"
            else:
                heading = f"◆  {title} · sorted by {sort_by} {arrow}"
            if query:
                heading += f" · filter '{query}'"
            _addstr(screen, 0, 0, heading, width, theme["title"])
            _addstr(
                screen,
                1,
                0,
                _hint_line(width, mouse, "home" if _HOME_ACTIVE else "quit"),
                width,
                theme["help"],
            )

            repo_width = min(22, max(12, width // 5))
            # The kit columns are the first thing a narrow window gives up —
            # they are a review column, and the summary is what the listing
            # is for. Dropping them hands their 14 columns back to the summary
            # rather than leaving a gap, and the mouse's column map is built
            # from this same list, so a hidden column cannot be clicked.
            kit = width >= _KIT_MIN_WIDTH
            kit_span = 14 if kit else 0
            summary_width = max(12, width - 39 - kit_span - repo_width)
            columns = [
                ("active", 5, 12),
                ("turns", 17, 7),
                ("credits", 24, 9),
                *(
                    [("skills", 33, 7), ("agents", 40, 7)] if kit else []
                ),
                ("summary", 33 + kit_span, summary_width),
                ("repo", 33 + kit_span + summary_width, repo_width),
            ]
            number_label = "  #" + (arrow if sort_by == "relevance" else " ")
            _addstr(
                screen,
                3,
                0,
                f"{number_label:<5}",
                width,
                theme["selected"] if sort_by == "relevance" else theme["header"],
            )
            for name, x, column_width in columns:
                label = name.title() + (arrow if sort_by == name else "")
                style = theme["selected"] if sort_by == name else theme["header"]
                _addstr(screen, 3, x, f"{label:<{column_width}}", column_width, style)
            _addstr(screen, 4, 0, "─" * width, width, theme["separator"])

            for line, row in enumerate(sorted_rows[offset : offset + visible], 5):
                sid, started, summary, repo, cwd, turns, nano_aiu = row[:7]
                skills, agents = _kit_of(row)
                on_cursor = offset + line - 5 == cursor
                tag = _project_tag(repo, cwd)
                values = [
                    f"{date.fromisoformat(started[:10]):%d %b %Y}",
                    str(turns),
                    ui.fmt_aiu(nano_aiu),
                    *(
                        [str(skills) if skills else "·",
                         str(agents) if agents else "·"] if kit else []
                    ),
                    ui.trunc(
                        redact.one_line(redact.redact(summary)) or _no_summary(turns),
                        summary_width - 1,
                    ),
                    ui.trunc(tag, repo_width - 1) or "·",
                ]
                styles = [
                    theme["active"],
                    theme["turns"],
                    theme["credits"] if nano_aiu else theme["number"],
                    *(
                        [theme["turns"] if skills else theme["number"],
                         theme["turns"] if agents else theme["number"]] if kit else []
                    ),
                    theme["summary"],
                    # A session run outside any repository — from home, or a
                    # scratch directory too generic to name — has no tag to
                    # show. It gets the same dimmed '·' the skills and agents
                    # columns use for nothing, so the column reads as counted
                    # and empty rather than as a cell that failed to draw.
                    theme["repo"] if tag else theme["number"],
                ]
                if on_cursor:
                    _addstr(screen, line, 0, " " * width, width, theme["cursor"])
                    styles = [theme["cursor"]] * len(styles)
                _addstr(
                    screen,
                    line,
                    0,
                    f"{numbers[sid]:>3}",
                    4,
                    theme["cursor"] if on_cursor else theme["number"],
                )
                for value, (_, x, column_width), style in zip(
                    values, columns, styles, strict=True
                ):
                    _addstr(screen, line, x, f"{value:<{column_width}}", column_width, style)

            if hits:
                hit = hits.get(sorted_rows[cursor][0]) if sorted_rows else None
                if hit:
                    source, snippet = hit
                    text = (
                        f" {_SOURCE_LABELS.get(source, source)}: "
                        f"{_hit_text(source, snippet)}"
                    )
                else:
                    text = " matched on summary, repo or directory"
                _addstr(screen, height - 2, 0, text, width, theme["summary"])

            if sorted_rows:
                number = numbers[sorted_rows[cursor][0]]
                status = (
                    f" {cursor + 1} of {len(sorted_rows)} sessions "
                    f"· Enter resumes, or 'cs resume {number}' later "
                )
            else:
                status = " no sessions match the filter · press / to edit, Esc to clear "
            _addstr(screen, height - 1, 0, status, width, theme["status"])

            screen.refresh()
            # Input handlers can reset curses to blocking mode, so the
            # timeout is owned here, at the read itself.
            if timed and not pending:
                wait(max(1, min(1000, round(
                    (next_refresh - time.monotonic()) * 1000))))
            try:
                key = pending.pop(0) if pending else screen.getch()
            except KeyboardInterrupt:
                # Ctrl-C is a quit, not a crash. Returning lets curses restore
                # the terminal on the way out, the same as 'q' does.
                return None
            if key == -1:
                # A heartbeat, never a keypress. Handled before the mouse
                # parser, which reads -1 as the start of a broken sequence.
                if time.monotonic() >= next_refresh:
                    next_refresh = time.monotonic() + _REFRESH_SECONDS
                    rows, title, numbers, follow = _reread_listing(
                        reload, rows, title, numbers, state,
                        sorted_rows[cursor][0] if sorted_rows else None,
                    )
                continue
            event = _mouse_event(screen, curses, key, last_click, pending)
            if key in (ord("q"), ord("Q")):
                return None
            if key == 27 and event is None:
                # Esc clears an active filter first, so it never discards work silently.
                if query:
                    query, cursor, offset = "", 0, 0
                    continue
                return None
            if event:
                kind, mx, my = event
                if kind in ("ignored", "move"):
                    pass
                elif kind in ("wheel-up", "wheel-down"):
                    step = -3 if kind == "wheel-up" else 3
                    last = max(len(sorted_rows) - 1, 0)
                    # The view follows the cursor, so scrolling moves the cursor —
                    # and it stops at the ends rather than wrapping.
                    cursor = min(max(cursor + step, 0), last)
                elif my == 3:
                    # Clicking a header sorts by that column, like the ←/→ keys.
                    headers = list(columns)
                    if "relevance" in cycle:
                        headers.append(("relevance", 0, 5))
                    clicked = next(
                        (
                            name
                            for name, x, column_width in headers
                            if x <= mx < x + column_width
                        ),
                        None,
                    )
                    if clicked:
                        follow = sorted_rows[cursor][0] if sorted_rows else None
                        if clicked == sort_by:
                            descending = not descending
                        else:
                            sort_by = clicked
                            descending = _SORT_COLUMNS[sort_by][1]
                elif 5 <= my < 5 + visible:
                    index = offset + my - 5
                    if index < len(sorted_rows):
                        cursor = index
                        if kind == "double":
                            return "resume", sorted_rows[cursor][0]
            elif key == ord("/"):
                # The box opens empty, every time. It used to open pre-filled
                # with the filter already applied, on the theory that you
                # would want to refine it — but the reason people press '/' a
                # second time is that the first filter was wrong, and an
                # inherited box makes the commonest case the expensive one:
                # eight backspaces before you can type. The filter you are
                # leaving is not lost while you do this — it is named in the
                # heading, and Esc here cancels back to it.
                entered = _prompt(screen, theme, height - 1, width, " filter: ", "")
                if entered is not None:
                    query, cursor, offset = entered, 0, 0
            elif key in (ord("v"), ord("V"), ord("o"), ord("O")) and sorted_rows:
                return "show", sorted_rows[cursor][0]
            elif key in (ord("t"), ord("T")) and sorted_rows:
                return "read", sorted_rows[cursor][0]
            elif key in (ord("r"), ord("R")) and sorted_rows:
                return "resume", sorted_rows[cursor][0]
            elif key in (curses.KEY_LEFT, curses.KEY_RIGHT):
                # Arrows sort on press, which leaves Enter free to act on the row.
                step = -1 if key == curses.KEY_LEFT else 1
                position = (cycle.index(sort_by) + step) % len(cycle)
                sort_by = cycle[position]
                descending = _SORT_COLUMNS[sort_by][1]
                follow = sorted_rows[cursor][0] if sorted_rows else None
            elif key in (ord("s"), ord("S")):
                descending = not descending
                follow = sorted_rows[cursor][0] if sorted_rows else None
            elif key in (10, 13, curses.KEY_ENTER) and sorted_rows:
                return "resume", sorted_rows[cursor][0]
            elif key == curses.KEY_UP:
                cursor = max(cursor - 1, 0)
            elif key == curses.KEY_DOWN:
                cursor = min(cursor + 1, max(len(sorted_rows) - 1, 0))
            elif key == curses.KEY_PPAGE:
                cursor = max(cursor - visible, 0)
            elif key == curses.KEY_NPAGE:
                cursor = min(cursor + visible, max(len(sorted_rows) - 1, 0))
            elif key in (ord("g"), curses.KEY_HOME):
                cursor = 0
            elif key in (ord("G"), curses.KEY_END):
                cursor = max(len(sorted_rows) - 1, 0)
    finally:
        # Anything that leaves the loop keeps the view it left behind,
        # so returning from a detail view lands where you were.
        state.update(
            sort_by=sort_by,
            descending=descending,
            cursor=cursor,
            offset=offset,
            query=query,
        )
        if timed:
            wait(-1)  # a detail view opened from here reads keys of its own


def _addstr(screen, y: int, x: int, text: str, width: int, style: int = 0) -> None:
    import curses

    if y >= screen.getmaxyx()[0] or x >= screen.getmaxyx()[1]:
        return
    try:
        screen.addnstr(y, x, text, max(min(width, screen.getmaxyx()[1] - x - 1), 0), style)
    except curses.error:
        pass


# ── Commands ─────────────────────────────────────────────────────────

def cmd_recent(
    days: int = 7,
    show_all: bool = False,
    sort_by: str | None = None,
    descending: bool | None = None,
) -> bool:
    """True when the full-screen listing ran, so the caller need not pause."""
    mode = "all" if show_all else "interactive"

    def read() -> tuple[list[tuple], str]:
        conn = db.connect()
        try:
            found = db.recent_sessions(conn, days)
        finally:
            conn.close()
        # Filter before counting, or the header promises rows the listing
        # then declines to show. The renderers filter too, a no-op from here.
        found = _with_assets(_visible(found, show_all))
        return found, f"Sessions · {_window_label(days)} · {mode} · {len(found)} total"

    rows, title = read()
    if sort_by is None and sys.stdin.isatty() and sys.stdout.isatty():
        return _interactive_listing(rows, title, show_all, reload=read)
    _render_listing(
        rows,
        title,
        show_all,
        sort_by,
        descending,
    )
    return False


def cmd_search(term: str, sort_by: str | None = None, descending: bool | None = None) -> bool:
    conn = db.connect()
    rows, hits = db.search(conn, term)
    conn.close()
    rows = _with_assets(rows)
    title = f"Search · '{term}' · {len(rows)} session{'' if len(rows) == 1 else 's'}"
    if sort_by is None and sys.stdin.isatty() and sys.stdout.isatty():
        return _interactive_listing(
            rows, title, show_all=True, default_sort="relevance", hits=hits, term=term
        )
    _render_listing(
        rows,
        title,
        show_all=True,
        sort_by=sort_by,
        descending=descending,
        default_sort="relevance",
        hits=hits,
        term=term,
    )
    return False


# ── One session, two views ───────────────────────────────────────────
# show is the page and read is the words. There were three: brief judged,
# show inventoried, read quoted, and no fact appeared in two of them. The
# rule was good and the split was not — brief answered "what happened" and
# show answered "what it cost", and nobody wanted one without the other, so
# every brief was followed by a show. They are one page now, with `--short`
# printing its top half for the times you only want the story. What the two
# surviving views share is the way in and the way on, and those are built
# here rather than written out twice.

# One reading width for all three: the same header two lines narrower in one
# view than the next reads as a different screen, not the same session.
_SESSION_WIDTH = 100


def _session_header(detail: tuple, turns: int, nano: int | None,
                    width: int, view: str = "") -> list[str]:
    """The opening block every session view shares.

    The three views each named the same facts differently — `span` against
    `started` and `updated`, `volume` against `credits`, repo and branch on
    one line here and two lines there — so moving between them meant reading
    the header again to learn that nothing had changed.

    `view` names which of the three you are in. The facts being identical is
    the point, but it also means a screenshot of one is a screenshot of any
    of them, so the rule says so.
    """
    summary, repo, cwd, branch, created, updated = detail
    where = redact.one_line(redact.redact(repo or "-"))
    if branch and branch != "-":
        branch = redact.one_line(redact.redact(branch))
        where = f"{where}  {ui.MUTED}·{ui.RST}  {branch}"
    lead = f"{view} · " if view else ""
    title = ui.trunc(
        redact.one_line(redact.redact(summary)), max(width - 8 - len(lead), 12)
    )
    lines = [
        "",
        ui.rule(width, f"{lead}{title}"),
        "",
        ui.field("repo", where),
    ]
    # The directory only earns a line when it is not just the repo again.
    folder = redact.one_line(redact.redact(_short_path(cwd, "") or ""))
    if folder and folder != repo:
        lines.append(ui.field("dir", _tail(folder, width - 12)))
    span = f"{created[:16]} → {updated[:16]}".replace("T", " ")
    lines.append(ui.field("span", span))
    volume = f"{turns} turn{'' if turns == 1 else 's'}"
    if nano:
        volume += f" · {ui.fmt_aiu(nano)} AIU"
    lines.append(ui.field("volume", volume))
    lines.append("")
    return lines


def _short_ref(session_id: str) -> str:
    """The shortest prefix that still names only this session.

    A footer prints a command three or four times, and three 36-character
    uuids is a wall of hex in which the useful part — the verb — is the
    thing you have to hunt for. Eight hex characters already identify a
    session in a personal store, but "already" is not "always", so this
    widens the prefix until it is unambiguous and gives up on the full id
    rather than ever printing something that resolves to two sessions.
    """
    import sqlite3

    # Shortening is for uuids. An id already short enough to read whole gains
    # nothing from being clipped, and clipping it costs the reader the ability
    # to recognise it — `sess-alpha` truncated to `sess-alp` saves two
    # characters and throws away the only two that meant anything.
    if len(session_id) <= 20:
        return session_id
    try:
        conn = db.connect()
        try:
            for size in (8, 12, 16, 24):
                prefix = session_id[:size]
                count = conn.execute(
                    "SELECT count(*) FROM sessions WHERE id LIKE ?",
                    (prefix + "%",),
                ).fetchone()[0]
                if count <= 1:
                    return prefix
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        pass  # unreadable store: the full id is always correct
    return session_id


# What each sibling view actually hands back. The footer used to print bare
# commands, which answers "what can I type next" but not "which one do I
# want" — and a reader who cannot tell brief from show from read will run
# all three to find out.
_VIEW_BLURBS = {
    "brief": "the short form: what it left open",
    "show": "everything: outcome, spend, files, skills, turns",
    "read": "the conversation itself, both sides",
    "resume": "reopen it in Copilot CLI",
}


def _session_footer(session_id: str, others: str, width: int,
                    extra: str | tuple[str, str] = "") -> list[str]:
    """The way on: the id, then the views this one is not — and what each is.

    Each command is runnable as printed. `cs show|read|resume <id>` looked
    compact but is a shell pipeline, not a choice of commands.

    Commands use the short form of the id and the `id` row carries the whole
    of it, so the block is both readable and copyable — one line to paste
    into a script, three that fit an eighty-column terminal with room left
    for the sentence saying what they do.
    """
    # No leading blank: every section above already ends with one.
    short = _short_ref(session_id)
    rows = [
        (f"cs {verb} {short}", _VIEW_BLURBS.get(verb, ""))
        for verb in others.split("|")
    ]
    if extra:
        rows.append((extra, "") if isinstance(extra, str) else extra)
    pad = max(len(command) for command, _ in rows) + 2
    lines = [ui.rule(width), ui.field("id", session_id)]
    for index, (command, blurb) in enumerate(rows):
        # The gloss is an aside, not a column: if it will not fit beside the
        # command it is dropped rather than wrapped, because a footer that
        # wraps reads as content and this one is signposting.
        if blurb and pad + len(blurb) <= width - 11:
            value = f"{command:<{pad}}{ui.MUTED}{blurb}{ui.RST}"
        else:
            value = command
        lines.append(ui.field("next" if index == 0 else "", value))
    if redact.enabled():
        # The only fixed-width string on a page that is otherwise width-aware,
        # and at 49 cells it ran off any window under about fifty columns —
        # the footer of every session view, so the overrun was everywhere.
        note = "credentials masked · CS_REDACT=0 to show raw text"
        if ui.cells(note) > width:
            note = "credentials masked"
        lines.append(f"  {ui.MUTED}{note}{ui.RST}")
    lines.append("")
    return lines


def cmd_show(ref: str, short: bool = False, show_asks: bool = False) -> bool:
    """Everything worth knowing about one session, in one page.

    This used to be three commands. `brief` judged the session, `show`
    inventoried it and `read` printed it, and no fact appeared in more than
    one — which is a tidy rule and the wrong one. Nobody wants a third of a
    session: understanding one meant running two commands and holding the
    halves together in your head, and the commonest thing anyone did with
    `cs brief` was follow it immediately with `cs show`.

    So there is one page now, ordered by what a reader acts on: what is
    still open, what the session was for, what came of it, then what it
    cost, touched and reached for, then an index into the turns. `--short`
    stops after the judgement, for when you only need to remember where you
    left off. `cs brief` is that flag with a name.

    Paged, so it scrolls with the wheel.
    """
    return _page(_capture(lambda: _render_show(ref, short, show_asks)))


def _render_show(ref: str, short: bool = False, show_asks: bool = False) -> None:
    session_id = _resolve_ref(ref)
    conn = db.connect()
    detail = db.session_detail(conn, session_id)
    if not detail:
        print(f"error: session not found: {session_id}", file=sys.stderr)
        conn.close()
        sys.exit(1)
    cwd = detail[2]
    turn_count = db.session_turn_count(conn, session_id)
    usage = db.session_usage(conn, session_id)
    checkpoint = db.session_checkpoint(conn, session_id)
    prompts = db.session_prompts(conn, session_id)
    last_reply = db.session_last_reply(conn, session_id)
    refs = db.session_refs(conn, session_id)
    governance = _governance(conn, session_id)
    # The inventory half is a further six queries, and `--short` exists
    # precisely for the moment when you do not want to wait for them.
    files = db.session_files(conn, session_id) if not short else []
    split = db.session_work_split(conn, session_id) if not short else None
    used_skills, used_agents = (
        _assets_used(conn, session_id) if not short else ([], [])
    )
    subagents = db.subagent_detail(conn, session_id) if not short else []
    conn.close()

    width = min(shutil.get_terminal_size().columns, _SESSION_WIDTH)
    inner = width - 4
    body = max(inner - 4, 40)
    total_nano = sum(u[2] for u in usage)
    asks = [text for _, text in ((i, _user_text(t)) for i, t in prompts) if text]

    print("\n".join(
        _session_header(detail, turn_count, total_nano, inner,
                        "brief" if short else "show")
    ))

    # A finding outranks everything: a credential in a transcript is not
    # something to meet after four screens of spend breakdown.
    _print_governance(governance, session_id, cwd, inner)

    # ── What it meant ────────────────────────────────────────────────
    # Still open leads: it is the only section that changes what you do next.
    steps = _bullets(checkpoint["next_steps"], 5) if checkpoint else []
    if steps:
        print(ui.heading("Still open", ui.AMBER))
        for line in steps:
            _item(redact.redact(line), body, "→", ui.AMBER)
        print()

    # Then the session's own story, in the order it happened: what was asked
    # for, what came of it, where you left off. The outcome used to print
    # before the request that caused it, which reads as an answer to a
    # question you have not been shown yet.
    #
    # "First request" is the honest name for this. It was headed "Goal", but
    # it is literally the opening prompt, and an opening prompt is often
    # housekeeping the session then moved on from — calling that the goal
    # made the view look wrong whenever the summary disagreed with it.
    if asks:
        print(ui.heading("First request · turn 0", ui.ACCENT))
        _item(redact.redact(asks[0]), body)
        print()

    done = _bullets(checkpoint["work_done"], 5) if checkpoint else []
    if done:
        print(ui.heading("What got done", ui.MINT))
        for line in done:
            _item(redact.redact(line), body, "·", ui.MINT)
        print()
    elif last_reply:
        # No checkpoint: the closing reply is the only account of the outcome.
        print(ui.heading("Where it ended", ui.MINT))
        for line in _plain(last_reply)[:4]:
            _item(redact.redact(line), body)
        print()

    # Last of the story, because it is the one that hands you to `cs resume`.
    if len(asks) > 1:
        print(ui.heading(f"Last request · turn {len(asks) - 1}", ui.ACCENT))
        _item(redact.redact(asks[-1]), body)
        print()

    if show_asks and asks:
        print(ui.heading(f"Every request · {len(asks)}", ui.ACCENT))
        for number, text in enumerate(asks):
            _item(redact.redact(text), body, f"{number:>3}", ui.MUTED)
        # The left column is a turn number, and a number you cannot open is
        # decoration. This used to be said under the turn index at the foot
        # of the page; the index has gone — it was a third rendering of this
        # same list — so the one numbered list left says it instead.
        print(f"    {ui.MUTED}open one with "
              f"'cs read {_short_ref(session_id)} --turn N'{ui.RST}")
        print()

    if refs:
        commits = [v for kind, v in refs if kind == "commit"]
        prs = [v for kind, v in refs if kind == "pr"]
        print(ui.heading("Shipped", ui.ACCENT))
        if commits:
            extra = f" … +{len(commits) - 8}" if len(commits) > 8 else ""
            shown_commits = ", ".join(redact.one_line(c) for c in commits[:8])
            print(f"    {ui.MUTED}commits{ui.RST}  {shown_commits}{extra}")
        if prs:
            shown = ", ".join("#" + redact.one_line(p).lstrip("#") for p in prs[:8])
            print(f"    {ui.MUTED}PRs{ui.RST}      {shown}")
        print()

    if short:
        extra: str | tuple[str, str] = (
            f"cs show {_short_ref(session_id)}",
            "the full page: spend, files, skills, turns",
        )
        print("\n".join(_session_footer(session_id, "read|resume", inner, extra)))
        return

    # ── What it used ─────────────────────────────────────────────────
    if split and split["calls"]:
        print(ui.heading("How the work was done", ui.ACCENT))
        _print_work_split(split, inner)
        print()

    if usage:
        # The total is already in the header; this block is the breakdown.
        print(ui.heading("Models", ui.VIOLET))
        for model, events, nano in usage:
            named = ui.trunc(redact.redact(model), 24)
            print(
                f"    {ui.VIOLET}{ui.fmt_aiu(nano):>8}{ui.RST}  {named:<24}"
                f" {ui.MUTED}{events:>7,} calls{ui.RST}"
            )
        print()

    if files:
        made = sum(1 for _, tool in files if tool == "create")
        print(ui.heading(f"Files touched · {len(files)} ({made} created)", ui.MINT))
        for path, tool in files[:12]:
            mark = f"{ui.MINT}+{ui.RST}" if tool == "create" else f"{ui.AMBER}~{ui.RST}"
            print(f"    {mark} {_tail(redact.redact(_short_path(path, cwd)), inner - 6)}")
        if len(files) > 12:
            print(f"    {ui.MUTED}… and {len(files) - 12} more{ui.RST}")
        print()
    elif checkpoint and _paths(checkpoint["files"], 12):
        # An older store keeps no file record; the checkpoint named some.
        print(ui.heading("Key files · named in the checkpoint", ui.MINT))
        for path in _paths(checkpoint["files"], 12):
            print(f"    {_tail(redact.redact(_short_path(path, cwd)), inner - 6)}")
        print()

    _print_assets_used(used_skills, used_agents, inner - 4, subagents)

    # The lessons this page held back, offered once at the foot of it. `show`
    # is the page most likely to be read beside the Copilot CLI's own status
    # line, so the explanation of why the two spend figures differ has to be
    # findable from here rather than only from a flag nobody was shown.
    _why_hint(inner)
    print("\n".join(_session_footer(session_id, "read|resume", inner)))


def _print_work_split(split: dict, width: int) -> None:
    """Who did the work: you, the main agent, delegated sub-agents, compaction."""
    labels = [
        ("you", "user", ui.MINT),
        ("main agent", "agent", ui.ACCENT),
        ("sub-agents", "sub-agent", ui.VIOLET),
        ("compaction", "compaction", ui.AMBER),
    ]
    rows = [
        (label, *split["by_initiator"].get(key, (0, 0)), colour)
        for label, key, colour in labels
    ]
    # A call the store labels with anything else — or, in older sessions, with
    # nothing at all — is still spend. Naming only the four kinds we know would
    # leave these rows short of the total printed above them, and a breakdown
    # that does not add up is worse than no breakdown.
    known = {key for _, key, _ in labels}
    other = [v for key, v in split["by_initiator"].items() if key not in known]
    if other:
        rows.append(("other", sum(c for c, _ in other), sum(n for _, n in other), ui.SKY))
    peak = max((calls for _, calls, _, _ in rows), default=0)
    for label, calls, nano, colour in rows:
        if not calls:
            continue
        # Coloured by who did the work, not by size: this chart's question
        # is "who", and four rows each sweeping the same ramp would answer it
        # in the one channel that is already spoken for.
        print(
            f"    {ui.MUTED}{label:<11}{ui.RST}"
            f"{ui.bar(calls, peak, 18, colour=colour, track=True)}"
            f" {calls:>5} calls  {ui.VIOLET}{ui.fmt_aiu(nano):>8} AIU{ui.RST}"
        )
    tasks = split.get("delegated_tasks", 0)
    if tasks:
        print(
            f"    {ui.MUTED}{'delegated':<11}{ui.RST}"
            f"{tasks} task{'' if tasks == 1 else 's'} handed to sub-agents"
        )

    # Spend nobody typed a prompt for. Compaction re-summarises the context
    # when it overflows and sub-agents bill against the session that launched
    # them, so both land on the total in the header while matching none of the
    # exchanges you can actually scroll back to. On a long session it is the
    # single most surprising slice — the one that makes the header look wrong
    # — so it gets stated as a number rather than left to be inferred by
    # adding two rows of a chart together.
    indirect = sum(
        split["by_initiator"].get(key, (0, 0))[1]
        for key in ("compaction", "sub-agent")
    )
    total = sum(nano for _calls, nano in split["by_initiator"].values())
    if indirect and total:
        # Kept inside the width the chart above it already sets, so this is
        # not the one line that decides how wide the block is. The rows above
        # are named 'sub-agents' and 'compaction', so what is worth spending
        # the characters on here is the share and the fact that no prompt of
        # yours is behind any of it.
        print(
            f"    {ui.MUTED}{'indirect':<11}{ui.RST}"
            f"{ui.VIOLET}{ui.fmt_aiu(indirect)} AIU{ui.RST}"
            f"{ui.MUTED} · {indirect / total * 100:.0f}% of spend,"
            f" no prompt behind it{ui.RST}"
        )

    # Why this total will not match the number the Copilot CLI shows in its
    # own status line, which is the comparison anyone with both on screen
    # makes first. The store keeps no record of a session being reopened, so
    # this says which span is being measured rather than inventing a count of
    # runs it cannot actually see.
    _why("Spend here is the whole life of the session: every call billed to "
         "it since it was created, across every time it was resumed, "
         "compaction and sub-agents included. The Copilot CLI's status line "
         "counts only the run you are sitting in. On a resumed session it "
         "reads lower, and neither number is wrong — they measure different "
         "spans. Your plan's usage is account-wide and server-side, so it "
         "matches neither.", width)




def _short_path(path: str, cwd: str) -> str:
    """Drop the session's own directory (then $HOME) from the front of a path."""
    for base in (cwd, os.path.expanduser("~")):
        if base and base != "-" and path.startswith(base.rstrip("/") + "/"):
            return path[len(base.rstrip("/")) + 1 :]
    return path


def _tail(path: str, width: int) -> str:
    """Trim a path from the left — the file name is the part worth keeping."""
    return path if len(path) <= width else "…" + path[-(width - 1) :]




def _turn_size(prompt: str, reply: str) -> str:
    """A compact sense of how heavy a turn is, without printing raw counts."""
    total = len(prompt or "") + len(reply or "")
    if total >= 10_000:
        return f"{total / 1000:.0f}k chars"
    if total >= 1_000:
        return f"{total / 1000:.1f}k chars"
    return f"{total} chars"


def _turn_body(text: str | None, absent: str, colour: str, inner: int) -> list[str]:
    """One side of a turn, rendered and attributed to whoever said it.

    Trailing blank lines are dropped before the rail goes on. A reply almost
    always ends with a newline, and a rail drawn beside nothing reads as a
    block that has more in it than it does.

    An absent side is set in the furniture colour, not the body colour: it is
    this view describing the record, not the record itself, and printing
    "(empty)" in the same type as a prompt makes it look like one.
    """
    if text and text.strip():
        body = ui.markdown(redact.redact(text), inner)
        while body and not body[-1].strip():
            body.pop()
    else:
        body = [f"    {ui.MUTED}{absent}{ui.RST}"]
    return ui.spine(body, colour)


def _render_transcript(
    session_id: str, detail: tuple, turns: list[tuple], only: int | None,
    nano: int | None = None,
) -> str:
    width = min(shutil.get_terminal_size().columns, _SESSION_WIDTH)
    inner = width - 4

    # read is the conversation and nothing else. It used to print an index
    # ahead of the turns; so did `cs show`, until that one went too — a table
    # of contents is only distance from the text, and the same list was being
    # drawn three times. `cs show --asks` is the numbered list now.
    lines = _session_header(detail, len(turns), nano, inner, "read")
    if only is not None:
        turns = [turn for turn in turns if turn[0] == only]
        if not turns:
            lines.append(f"  {ui.DIM}no turn #{only} in this session{ui.RST}")

    for index, prompt, reply, when in turns:
        stamp = when[11:16] if len(when) >= 16 else ""
        note = " · ".join(part for part in (stamp, _turn_size(prompt, reply)) if part)
        # The ask goes in the rule itself: scrolling a long transcript should
        # say what you are looking at, not just how far in you are. When and
        # how big ride the same rule's other end, so a turn opens on one line
        # of furniture rather than on a rule and a stray line of grey.
        gist = _user_text(prompt or "")
        title = f"Turn {index}"
        if gist:
            title = f"{title} · {ui.trunc(gist, max(inner - len(note) - 26, 12))}"
        lines.append(ui.rule(inner, title, note=note))
        lines.append("")
        lines.append(ui.speaker(ui.YOU_MARK, "You", ui.MINT))
        lines.extend(_turn_body(prompt, "(empty)", ui.MINT, inner))
        lines.append("")
        lines.append(ui.speaker(ui.COPILOT_MARK, "Copilot", ui.VIOLET))
        lines.extend(_turn_body(reply, "(no reply recorded)", ui.VIOLET, inner))
        lines.append("")

    lines.extend(_session_footer(session_id, "brief|show|resume", inner))
    return "\n".join(lines)




# Answers per binary, so `less --version` runs once per process, not per page.
_LESS_MOUSE: dict[str, bool] = {}


def _less_wheel_lines(pager: str) -> bool:
    """Whether this `less` understands --mouse (it landed in less 551)."""
    import re

    binary = pager.split()[0]
    if binary in _LESS_MOUSE:
        return _LESS_MOUSE[binary]
    supported = False
    try:
        out = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=2
        ).stdout
        found = re.search(r"less (\d+)", out or "")
        supported = bool(found) and int(found.group(1)) >= 551
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        supported = False
    _LESS_MOUSE[binary] = supported
    return supported


def _capture(render) -> str:
    """Run a print-based renderer and collect what it wrote.

    Lets `brief` and `show` keep their straightforward `print` style while
    still being handed to the pager when they outgrow the screen.
    """
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        render()
    return buffer.getvalue().rstrip("\n")


def _reader_tui(
    screen, lines: list[str], mouse: bool, sort: dict | None = None
) -> None:
    """Scroll ANSI-coloured report text with the same keys as every other view.

    less is the better pager, but it cannot be made to treat Esc as "back":
    Esc is its meta prefix, so a lesskey binding for it waits for the next
    byte instead of acting. Reached from the menu that makes every long
    report a dead end for anyone who reaches for Esc, so the menu reads them
    here instead and leaves less to plain `cs repos` at the shell.

    `sort`, when a report supplied one, carries the columns and a renderer,
    so ←/→ and `s` re-sort in place — the same keys the listing uses, because
    a table is a table wherever you meet it.
    """
    import curses

    screen.keypad(True)
    theme = ui.tui_theme(curses)
    palette = ui.sgr_palette(curses)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    offset = 0
    last_click = [0.0, -1]
    pending: list[int] = []

    def wait(milliseconds: int) -> bool:
        """Ask for a timed getch. False when this window cannot do one."""
        try:
            screen.timeout(milliseconds)
        except (AttributeError, curses.error):
            return False
        return True

    # The report is wiped in, once, on the way up — the same slanted light the
    # landing screen opens with, so a view arrives the way the menu that
    # launched it did. Never on a re-sort or a scroll: motion while you are
    # reading is motion that says nothing and never stops saying it.
    reveal = 0 if wait(ui.REVEAL_MS) else None

    while True:
        height, width = screen.getmaxyx()
        page = max(1, height - 1)
        offset = max(0, min(offset, max(0, len(lines) - page)))
        screen.erase()
        screen.bkgd(" ", theme["background"])
        span = width + ui.REVEAL_LAG * page
        swept = span if reveal is None else ui.reveal_columns(reveal, span)
        for row, line in enumerate(lines[offset:offset + page]):
            column = 0
            # Each row trails the one above it, so the edge crossing the page
            # is a slant rather than a shutter.
            edge = width if reveal is None else min(
                width, max(0, swept - row * ui.REVEAL_LAG))
            for text, attr in ui.sgr_runs(line, palette):
                if column >= edge:
                    break
                _addstr(screen, row, column, text, edge - column,
                        attr or theme["summary"])
                column += ui.cells(text)
        at_end = offset + page >= len(lines)
        hints = [
            ("↑/↓ scroll", "↑↓", 3),
            ("space page", "space", 2),
            ("g/G ends", "g/G", 3),
            # The reader only runs from the menu, so both ways out go there.
            ("Esc back", "Esc", 0),
            ("q home", "q", 0),
        ]
        if sort:
            hints.insert(0, ("←/→ sort", "←/→ sort", 1))
            hints.insert(1, ("s reverse", "s", 2))
        if mouse:
            hints.insert(0, ("scroll wheel", "wheel", 4))
        place = "end" if at_end else f"{min(offset + page, len(lines))}/{len(lines)}"
        if sort:
            # The column belongs beside the position, not in the hints: hints
            # shrink to their short forms on a narrow window, and the one
            # thing you need after pressing ← is which column you landed on.
            place = f"{sort['column']}{'↓' if sort['descending'] else '↑'} · {place}"
        _addstr(screen, height - 1, 0,
                f" {_fit_hints(hints, max(1, width - len(place) - 3))} ".ljust(width),
                width, theme["status"])
        _addstr(screen, height - 1, max(0, width - len(place) - 1), place, width, theme["status"])
        screen.refresh()
        try:
            key = pending.pop(0) if pending else screen.getch()
        except KeyboardInterrupt:
            return
        if key == -1:
            # The wipe's own frame. Nothing else arms a timeout, so once the
            # wipe is done a -1 here means only "nothing typed".
            if reveal is None:
                continue
            reveal += 1
            if reveal >= ui.REVEAL_FRAMES:
                reveal = None
                wait(-1)
            continue
        if reveal is not None:
            # Any key at all lands you on the finished page. The key goes
            # back in the queue and the loop redraws first, so what is on
            # screen when it is acted on is the whole report rather than
            # however much of it the wipe had reached.
            reveal = None
            wait(-1)
            pending.insert(0, key)
            continue
        # A mouse report has to be decoded before Esc is read as "back": under
        # SGR a wheel tick *starts* with Esc, so testing for the key first
        # turned every scroll into a trip back to the menu.
        event = _mouse_event(screen, curses, key, last_click, pending)
        if event:
            kind, _x, _y = event
            if kind == "wheel-up":
                offset -= 3
            elif kind == "wheel-down":
                offset += 3
            continue
        if key in (ord("q"), ord("Q"), 27):
            return
        if key in (curses.KEY_DOWN, ord("j")):
            offset += 1
        elif key in (curses.KEY_UP, ord("k")):
            offset -= 1
        elif key in (curses.KEY_NPAGE, ord(" "), ord("f")):
            offset += page
        elif key in (curses.KEY_PPAGE, ord("b")):
            offset -= page
        elif key in (curses.KEY_HOME, ord("g")):
            offset = 0
        elif key in (curses.KEY_END, ord("G")):
            offset = len(lines)
        elif sort and key in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("s"), ord("S")):
            if key == ord("s") or key == ord("S"):
                sort["descending"] = not sort["descending"]
            else:
                order = sort["columns"]
                step = 1 if key == curses.KEY_RIGHT else -1
                where = (order.index(sort["column"]) + step) % len(order)
                sort["column"] = order[where]
                sort["descending"] = sort["defaults"][sort["column"]]
            lines = sort["render"](sort["column"], sort["descending"]).split("\n")
            # Re-sorting reorders the whole table, so the row you were looking
            # at is not there any more. The top is the only honest place to be.
            offset = 0


def _read_in_place(text: str, sort: dict | None = None) -> bool:
    """Show long text in a curses reader. False if curses could not run."""
    import curses

    mouse = [False]

    def view(screen):
        mouse[0] = _enable_mouse(curses)
        try:
            _reader_tui(screen, text.split("\n"), mouse[0], sort)
        finally:
            # Before endwin: once the terminal is back in cooked mode a late
            # report is echoed the instant it arrives, too soon to drain.
            _disable_mouse()

    try:
        _curses_wrapper(view)
    except (curses.error, OSError):
        return False
    except KeyboardInterrupt:
        pass
    finally:
        # After the wrapper, so ncurses has already stopped its own reporting:
        # dropping queued reports any earlier just races the ones still coming.
        _disable_mouse()
    return True


def _page_report(report: str, render, sort_by: str | None,
                 descending: bool | None) -> bool:
    """Render a sortable report and show it, re-sortable in the reader.

    `render(column, descending)` returns the whole report as text. Passing the
    renderer rather than its output is what lets ←/→ re-sort without leaving
    the reader — the report is rebuilt, not re-shuffled on screen.
    """
    try:
        column, is_descending = _resolve_sort(report, sort_by, descending)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    columns = list(_REPORT_COLUMNS[report])
    sort = {
        "report": report,
        "columns": columns,
        "defaults": {name: default for name, (_, default) in
                     _REPORT_COLUMNS[report].items()},
        "column": column,
        "descending": is_descending,
        "render": render,
    }
    return _page(render(column, is_descending), sort)


def _page(text: str, sort: dict | None = None) -> bool:
    """Show long text through the user's pager. True if a pager actually ran.

    The caller uses that to decide whether to wait afterwards: quitting the
    pager is already the user saying "done reading", so asking them to press
    Enter as well is one keystroke too many.
    """
    lines = text.count("\n") + 1
    height = shutil.get_terminal_size().lines
    if not sys.stdout.isatty():
        print(text)
        return False
    if _HOME_ACTIVE and _read_in_place(text, sort):
        # From the menu Esc has to mean back, which less cannot do — and a
        # short report must stay in the UI too, or it prints over the menu.
        return True
    if lines <= height:
        print(text)
        return False
    pager = os.environ.get("PAGER") or "less"
    command = [pager]
    if os.path.basename(pager.split()[0]) == "less":
        # -R keeps the colours, -F quits on a short page, -X leaves it on screen.
        command = [*pager.split(), "-R", "-F", "-X"]
        if _less_wheel_lines(pager):
            # Scroll with the wheel instead of walking the arrows. Text
            # selection then needs Shift (or ⌥ on macOS), which is the usual
            # trade for a mouse-aware pager.
            command += ["--mouse", "--wheel-lines=3"]
    try:
        subprocess.run(command, input=text, text=True, check=False)
    except (OSError, ValueError):
        print(text)
        return False
    return True


# A markdown table separator: pipes, dashes and alignment colons only.
_TABLE_RULE = re.compile(r"^\|?[\s:|-]*-[\s:|-]*\|?$")


def _plain(text: str) -> list[str]:
    """Readable prose lines: code fences dropped, markdown noise stripped.

    Masking happens here, on the whole text, before it is broken into lines.
    Callers used to redact each line they printed, which silently disarmed
    every rule that spans lines — a PEM block is only recognisable as one
    when its BEGIN and END are still in the same string.
    """
    import re

    out: list[str] = []
    in_code = False
    for line in redact.redact(text).replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not stripped:
            continue
        # Markdown tables are layout, not prose. A separator row (`|---|:--|`)
        # carries no words at all, and a data row read literally is a fence of
        # pipes — so the rule is dropped and the cells are joined into the
        # sentence they were standing in for. Without this a brief whose
        # closing reply happened to end in a table printed `| Page | Version |`
        # under the heading "Where it ended".
        if _TABLE_RULE.match(stripped):
            continue
        if stripped.startswith("|") and stripped.count("|") >= 2:
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            stripped = " · ".join(cell for cell in cells if cell)
            if not stripped:
                continue
        stripped = re.sub(r"\*\*|__|^#{1,6}\s*", "", stripped)
        stripped = re.sub(r"\s+", " ", stripped)
        out.append(stripped)
    return out


def _one_line(text: str) -> str:
    """A quotation fit to sit on one line: masked, and stripped of layout.

    Evidence quotes are cut out of the middle of a reply, so they land
    wherever the cut fell — often inside a Markdown table, which read
    literally is a row of pipes and asterisks rather than a sentence. They
    were also the last place in the app printing store text without going
    through the masker, which is a leak waiting for the one session that
    mentions a skill next to a token.

    The table rules have to be undone inline rather than by line, because
    a quote arrives with its newlines already collapsed — by the time it
    gets here `|---|---|` is in the middle of a sentence, not alone on a
    row, and the line-anchored cleanup in `_plain` cannot see it.
    """
    import re

    line = " ".join(part for part in _plain(text) if part)
    line = re.sub(r"\|?\s*:?-{2,}:?\s*(?=\||$)", "|", line)
    line = re.sub(r"(?:\s*\|\s*)+", " · ", line)
    return line.strip(" ·")


def _bullets(text: str, limit: int) -> list[str]:
    """Up to `limit` bullet-ish lines, leading markers removed."""
    import re

    picked = []
    for line in _plain(text):
        item = re.sub(r"^([-*•]|\d+[.)])\s+", "", line)
        if item:
            picked.append(item)
        if len(picked) >= limit:
            break
    return picked


def _user_text(message: str) -> str:
    """What the user actually typed, with harness-injected blocks removed.

    Copilot prepends `<system_reminder>` blocks carrying a repo's custom
    instructions. They are not asks, and they crowd out the ones that are.
    """
    import re

    text = re.sub(
        r"<system[_-]reminder>.*?</system[_-]reminder>", " ", message, flags=re.S | re.I
    )
    text = re.sub(r"</?system[_-]reminder>", " ", text, flags=re.I)
    return " ".join(_plain(text))


def _paths(text: str, limit: int) -> list[str]:
    """File paths mentioned in prose — checkpoints mix paths with commentary."""
    import re

    seen: list[str] = []
    for token in re.findall(r"`([^`\s]+)`|(?<![\w`])((?:[\w.~-]*/)+[\w.-]+)", text):
        candidate = (token[0] or token[1]).rstrip(".,;:)")
        # A slash alone is not enough — "reads/writes" and "owner/repo" are
        # prose, not paths. Anchor on a root, a trailing slash, or a suffix.
        looks_like_path = (
            candidate.startswith(("/", "~", "./", "../"))
            or candidate.endswith("/")
            or bool(re.search(r"\.\w{1,6}$", candidate))
        )
        # A lone "/" or a bare separator carries nothing; and the same file
        # written two ways ("sync.sh" and "/tmp/x/sync.sh") is one entry.
        if not looks_like_path or len(candidate.strip("/~.")) < 2:
            continue
        leaf = candidate.rstrip("/").rsplit("/", 1)[-1]
        if any(leaf == s.rstrip("/").rsplit("/", 1)[-1] for s in seen):
            continue
        if candidate not in seen:
            seen.append(candidate)
        if len(seen) >= limit:
            break
    return seen



def _note(text: str, width: int, indent: int = 2) -> None:
    """A muted paragraph that wraps to the window instead of running off it.

    Report footers used to be written as hand-broken print() lines, which is
    fine until someone opens a 55-column terminal and the explanation of the
    report is the thing that overflows it.
    """
    pad = " " * indent
    for line in textwrap.wrap(" ".join(text.split()),
                              width=max(20, width - indent),
                              break_long_words=False,
                              break_on_hyphens=False) or [""]:
        print(f"{pad}{ui.MUTED}{line}{ui.RST}")


# Teaching prose is printed only when it is asked for. Every report used to
# carry two lines under each section explaining how to read that section, which
# is exactly right the first time you open it and pure noise the fiftieth. The
# split is by *what the sentence is about*, not by length: a line about the
# data in front of you is a finding and always prints (`_note`); a line about
# how the view works is a lesson and waits to be asked for (`_why`).
_WHY = False
# Set whenever a lesson is actually withheld, so the footer hint can never
# advertise an explanation that this particular run had nothing to show. An
# empty report is the case that matters: `cs hooks` with no hooks configured
# teaches inline, because a screen with nothing on it is the one moment the
# explanation *is* the report.
_WHY_WITHHELD = False


def _why(text: str, width: int, indent: int = 2) -> None:
    """A `_note` that only prints under `--why`.

    Same wrapping and the same muted colour, so turning explanations on
    restores the old report exactly rather than showing a different one.
    """
    global _WHY_WITHHELD
    if _WHY:
        _note(text, width, indent)
    else:
        _WHY_WITHHELD = True


def _why_hint(width: int, indent: int = 2) -> None:
    """Tell the reader the explanations exist, once, at the foot of a report.

    Without this the compact report is not denser, it is just missing
    something, and no one finds a flag they were never shown. It costs one
    line, it disappears the moment the flag is used, and it stays quiet on a
    report that withheld nothing.
    """
    global _WHY_WITHHELD
    if _WHY_WITHHELD:
        print(f"{' ' * indent}{ui.MUTED}--why  explains how to read this{ui.RST}")
    _WHY_WITHHELD = False


def _item(text: str, width: int, marker: str = "", colour: str = "",
          indent: int = 4) -> None:
    """Print one wrapped item — full sentences, hanging under the marker.

    `width` is the whole line, indent and marker included. It used to be the
    text alone, so every caller had to subtract the lead itself and the one
    that forgot ran two characters off a narrow window.
    """
    import textwrap

    pad = " " * indent
    lead = f"{pad}{colour}{marker}{ui.RST} " if marker else pad
    hang = " " * (indent + (len(marker) + 1 if marker else 0))
    # Hyphens are not break points here: paths and flags ("no-secrets",
    # "--format") must survive intact to be copyable.
    wrapped = textwrap.wrap(
        text, width=max(12, width - len(hang)), break_long_words=False,
        break_on_hyphens=False,
    ) or [""]
    print(f"{lead}{wrapped[0]}")
    for line in wrapped[1:]:
        print(f"{hang}{line}")


def cmd_brief(ref: str, show_asks: bool = False) -> bool:
    """`cs show --short`, under the name people already type.

    brief was its own view, with its own renderer, its own footer and its
    own idea of what mattered. It is now the top half of `cs show`, because
    the halves were never independently useful: the commonest thing anyone
    did with a brief was follow it straight with a `show`, which is the
    behaviour of a view that stops too early rather than one that is
    deliberately small.

    Kept as a command because it is three fewer keys than `--short` and
    because it is in everyone's history.
    """
    return cmd_show(ref, short=True, show_asks=show_asks)


def _assets_used(conn, session_id: str) -> tuple[list[tuple], list[tuple]]:
    """Skills and agent profiles this session named, each with its evidence.

    Matched against what is actually installed, so a session that mentions
    someone else's skill is not credited with using yours.
    """
    skills = [name for name, _ in _asset_names("skills")]
    agents = [name for name, _ in _asset_names("agents")]
    return (
        db.asset_evidence(conn, session_id, skills),
        db.asset_evidence(conn, session_id, agents),
    )


def _print_assets_used(skills: list[tuple], agents: list[tuple], width: int,
                       subagents: list[tuple] | None = None) -> None:
    """What kit this session reached for — and the grounds for saying so.

    Two questions live in this block, and they are answered by two different
    kinds of evidence, so they are drawn as two different things.

    *Which skills and agent profiles were used* can only be inferred: the
    session store records no invocation event of any kind, so the sole trace
    a skill leaves is that the text named it. An inference is worth as much
    as its ability to be checked, so each one arrives with the turn it was
    found in and the words it was found in — enough for a reader to agree,
    or to spot a false positive and say so.

    *Which sub-agents ran* is counted rather than inferred, from the id on
    each billed model call. The store keeps no name for them, so they are
    identified the only honest way: model, spend, calls and the turn they
    were launched from, which is what tells a lookup apart from a long
    research run anyway.
    """
    subagents = subagents or []
    if not skills and not agents and not subagents:
        return
    print(ui.heading("Skills & agents", ui.ACCENT))

    if skills or agents:
        for label, rows, colour in (("skill", skills, ui.MINT),
                                    ("agent", agents, ui.SKY)):
            for name, turn, quote, how in rows:
                # 'ran' is a record and 'named' is a reading of one. Two
                # claims of different strength on the same list have to look
                # different or the weaker one borrows the other's authority.
                mark = (f"{ui.MINT}ran{ui.RST}" if how == "ran"
                        else f"{ui.DIM}named{ui.RST}")
                print(f"    {ui.MUTED}{label:<6}{ui.RST}"
                      f"{colour}{name}{ui.RST}"
                      f"  {mark}  {ui.MUTED}turn {turn}{ui.RST}")
                # Only a mention needs quoting. A load marker is the record
                # itself, and printing 'loaded by the CLI' under a row that
                # already says 'ran' is a line that costs a reader attention
                # and returns nothing.
                if how != "ran":
                    # The quote gets a line of its own rather than a tail
                    # column. Thirty characters of context is not evidence,
                    # it is a hint that evidence exists, and the reader still
                    # has to go and look — which is what this block exists to
                    # save them from.
                    print(f"    {ui.DIM}      "
                          f"{ui._fit(_one_line(quote), max(24, width - 10))}"
                          f"{ui.RST}")
        certain = sum(1 for row in skills + agents if row[3] == "ran")
        if certain:
            _item(f"{certain} loaded by the CLI itself — that much is recorded, "
                  "not inferred. Anything marked 'named' was only mentioned in "
                  "the text, so the quote is the whole of the evidence.",
                  width, colour=ui.DIM)
        else:
            _item("Inferred from what the session said — these turns carry no "
                  "load marker, so the quote is the whole of the evidence.",
                  width, colour=ui.DIM)

    if subagents:
        if skills or agents:
            print()
        for short_id, model, calls, nano, ms, first, last in subagents:
            span = f"turn {first}" if first == last else f"turns {first}–{last}"
            took = f"{ms / 60000:.0f}m" if ms >= 90_000 else f"{ms / 1000:.0f}s"
            print(f"    {ui.MUTED}agent {ui.RST}"
                  f"{ui.VIOLET}{short_id:<22}{ui.RST}"
                  f" {ui.MUTED}{span:<9}{ui.RST}"
                  f" {ui._fit(model, 20):<20}"
                  f" {ui.MUTED}{calls:>4} calls{ui.RST}"
                  f" {ui.VIOLET}{ui.fmt_aiu(nano):>7} AIU{ui.RST}"
                  f" {ui.MUTED}{took:>4}{ui.RST}")
        _item("Counted from the billing records, not inferred. The store "
              "keeps no name for a sub-agent — only the id of the call that "
              "launched it — so they are named by what they ran and spent.",
              width, colour=ui.DIM)
    print()


def _asset_dirs(kind: str) -> list[Path]:
    """Where Copilot keeps skills / agents: project first, then personal."""
    return context.asset_dirs(kind)


def _asset_names(kind: str) -> list[tuple[str, Path]]:
    """(name, path) for every skill or agent on disk, deduped by name."""
    return [(asset.name, asset.path) for asset in context.assets(kind)]


def cmd_assets(kind: str, name: str | None = None, limit: int = 25,
               sort_by: str | None = None, descending: bool | None = None,
               by_place: bool = False) -> bool:
    """The inventory, or — given a name — the sessions that reference it."""
    if by_place:
        return _page(_capture(_render_assets_by_place))
    if name:
        return _page(_capture(lambda: _render_asset_sessions(kind, name)))
    return _page_report(
        "assets",
        lambda column, down: _capture(lambda: _render_assets(kind, limit, column, down)),
        sort_by, descending,
    )


def _render_asset_sessions(kind: str, name: str) -> None:
    known = dict(_asset_names(kind))
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    conn = db.connect()
    # A name the CLI is recorded as having loaded is askable about even with
    # nothing on this disk to point at. Refusing to answer for one — which
    # is what looking only at the shelf did — denies the session record on
    # the strength of a missing file.
    elsewhere = sorted({
        loaded for names in db.skills_invoked_by_session(conn).values()
        for loaded in names
    } - {real.lower() for real in known}) if kind == "skills" else []
    match = next(
        (real for real in [*known, *elsewhere] if real.lower() == name.lower()),
        next((real for real in [*known, *elsewhere]
              if name.lower() in real.lower()), None),
    )
    print()
    if not match:
        conn.close()
        print(ui.rule(inner, f"{kind.title()} · {name}"))
        print()
        print(f"  {ui.MUTED}No {kind[:-1]} named '{name}' on disk.{ui.RST}")
        close = [real for real in known if name.lower()[:4] in real.lower()][:6]
        if close:
            print(f"  {ui.MUTED}did you mean: {', '.join(close)}{ui.RST}")
        print()
        return

    rows = db.sessions_for_asset(conn, match)
    conn.close()
    print(ui.rule(inner, f"{match} · {len(rows)} sessions"))
    print()
    if match in known:
        print(ui.field("file", str(known[match])))
    else:
        print(ui.field("file", "not installed here — it ran in sessions whose "
                               "skills this repository does not ship"))
    print()
    if not rows:
        print(f"  {ui.MUTED}No session references it.{ui.RST}")
        print()
        return
    for session_id, summary, last_active in rows:
        print(f"    {ui.MUTED}{last_active[:16]}{ui.RST}  "
              f"{ui.trunc(redact.redact(summary), inner - 26)}")
        print(f"    {ui.MUTED}            cs show {session_id}{ui.RST}")
    print()


def _asset_inventory(kind: str) -> dict:
    """What both the screen and `--json` need to say about one kind of asset.

    Shared for the same reason the disk walk is shared. Each built its own
    list for a while and only one of them knew about the skills that ran
    without being installed, so the screen said forty-six had been
    referenced while the export, from the same store a second later, said
    thirty-five.
    """
    inventory = context.assets(kind)
    on_disk = {asset.name.lower(): asset for asset in inventory}
    conn = db.connect()
    # Sessions that demonstrably *loaded* each one, as opposed to sessions
    # that merely named it. Only skills leave this trace, so agent profiles
    # get an empty map and the view quietly falls back to the signal.
    invoked = db.skills_invoked_by_session(conn) if kind == "skills" else {}
    ran: dict[str, int] = {}
    for loaded in invoked.values():
        for lowered in loaded:
            ran[lowered] = ran.get(lowered, 0) + 1
    # A skill can be proved to have run and be nowhere on this disk: it came
    # from a checkout that is gone, or from a machine that is not this one.
    # Dropping those rows is how a store where a third of the sessions ran
    # four skills could report that it had never seen them. Proof of work
    # outranks an empty directory, so they get a row and an honest label.
    missing = sorted(set(ran) - set(on_disk))
    names = [asset.name for asset in inventory] + missing
    conn_counts = db.reference_counts(conn, names) if names else {}
    # Where the ones this repository cannot load actually live. "Not
    # installed" is true and useless on its own: every one of them ran
    # somewhere, and the question it leaves you with is where.
    elsewhere = (context.locate(set(missing), db.session_directories(conn))
                 if missing else {})
    conn.close()
    return {
        "inventory": inventory, "on_disk": on_disk, "ran": ran,
        "missing": missing, "names": names, "counts": conn_counts,
        "elsewhere": elsewhere,
    }


def _skills_by_place(conn) -> list[dict]:
    """Which skills ran where, one entry per checkout, busiest first.

    The other half of the inventory. `cs skills` answers "what can be loaded
    here"; this answers "what did I actually reach for, and where" — which
    for a store spread over sixty repositories is a different question with a
    different answer.

    Grouped by repository where the store recorded one, and by directory
    where it did not. Grouping by directory alone splits a repository into a
    row per folder you happened to be standing in — this store had one
    appearing four times, three of the rows reporting that it shipped none of
    the skills it plainly ships, because a subdirectory has no `.github`.

    What a group ships is the union over every directory in it, for the same
    reason: the skills are the repository's wherever inside it you were.
    """
    from collections import Counter

    invoked = db.skills_invoked_by_session(conn)
    places = db.session_places(conn)
    found: dict[str, dict] = {}
    for session_id, loaded in invoked.items():
        directory, repository = places.get(session_id, ("", ""))
        if not directory:
            continue          # a session the store cannot place
        place = found.setdefault(repository or directory, {
            "name": repository or Path(directory).name,
            "directories": set(), "sessions": 0, "skills": Counter(),
        })
        place["sessions"] += 1
        place["skills"].update(loaded)
        place["directories"].add(directory)
    for place in found.values():
        place["ships"] = set().union(
            *(context.ships(directory) for directory in place["directories"])
        ) if place["directories"] else set()
    return sorted(found.values(),
                  key=lambda place: (-place["sessions"], place["name"]))


def _render_assets_by_place(limit: int = 20) -> None:
    """`cs skills --by-repo` — where each skill was actually reached for."""
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    conn = db.connect()
    places = _skills_by_place(conn)
    conn.close()

    print()
    if not places:
        print(ui.rule(inner, "Skills by checkout"))
        print()
        print(f"  {ui.MUTED}No session in this store recorded loading a "
              f"skill.{ui.RST}")
        print()
        return

    total = sum(len(place["skills"]) for place in places)
    own = sum(1 for place in places for name in place["skills"]
              if name in place["ships"])
    print(ui.rule(inner, f"Skills by checkout · {len(places)}"))
    print()
    print(ui.field("places", f"{len(places)} checkouts ran a skill", 11))
    print(ui.field("borrowed", f"{total - own} of {total} came from outside "
                               f"the checkout that used them", 11))
    print()

    for place in places[:limit]:
        ran = place["skills"].most_common()
        carried = sum(1 for name, _ in ran if name in place["ships"])
        # 'all its own' is worth saying plainly: it is the state a repository
        # should be in, and it is rare enough here to be news.
        shape = (f"{len(ran)} skill{'s' if len(ran) != 1 else ''} · "
                 + ("all its own" if carried == len(ran)
                    else f"{carried} its own · {len(ran) - carried} borrowed"
                    if carried else "none of its own"))
        print(f"    {ui.MINT}{place['sessions']:>4}{ui.RST}  "
              f"{ui.BOLD}{ui._fit(place['name'], inner - 34):<{inner - 34}}"
              f"{ui.RST} {ui.MUTED}{shape}{ui.RST}")
        # The skills themselves, on their own line: a repository with ten of
        # them would otherwise push its own name off the window.
        named = ", ".join(
            f"{name} {count}" + ("" if name in place["ships"] else "*")
            for name, count in ran[:6]
        )
        if len(ran) > 6:
            named += f", +{len(ran) - 6}"
        # A skill called `test-ainb` is one word; breaking it at the hyphen
        # makes it read as two skills that do not exist.
        for line in textwrap.wrap(named, inner - 10, break_on_hyphens=False):
            print(f"          {ui.MUTED}{line}{ui.RST}")
        print()

    if len(places) > limit:
        print(f"    {ui.MUTED}… and {len(places) - limit} more{ui.RST}")
        print()
    _why("Sessions that loaded a skill, grouped by the directory they ran "
         "in — the CLI's own load marker, so this is recorded rather than "
         "inferred. A checkout is named by the repository the store saw most "
         "often for it, because a long-lived one collects several as its "
         "remote is renamed. '*' marks a skill the checkout does not ship: "
         "it worked because of what you had installed, and a colleague "
         "cloning the repository would not get it.", inner)
    print()

def _render_assets(kind: str, limit: int, column: str = "sessions",
                   descending: bool = True) -> None:
    gathered = _asset_inventory(kind)
    inventory, on_disk = gathered["inventory"], gathered["on_disk"]
    ran, missing = gathered["ran"], gathered["missing"]
    names, counts = gathered["names"], gathered["counts"]
    elsewhere = gathered["elsewhere"]
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4

    print()
    if not names:
        print(ui.rule(inner, f"{kind.title()}"))
        print()
        # One path per line, like the other two empty states. Joined with
        # dots they made a single 160-character line on any machine with a
        # long home directory, which is most of them.
        print(f"  {ui.MUTED}None found. cs looks for them in:{ui.RST}")
        for folder in _asset_dirs(kind):
            print(f"    {ui.MUTED}{ui._fit(str(folder), inner - 2)}{ui.RST}")
        print()
        return

    counted = [(name, counts.get(name, 0)) for name in names]
    used = _sort_report([r for r in counted if r[1]], "assets", column, descending)
    switched_off = [asset.name for asset in inventory if asset.disabled]
    idle = [name for name, seen in counted
            if not seen and not on_disk[name.lower()].disabled]

    print(ui.rule(inner, f"{kind.title()} · {len(inventory)} on disk"))
    print()
    # Where they came from, not just how many. A bare total hid the bug this
    # split was added for: standing in a repository with twenty skills of its
    # own, the inventory only ever counted the personal ones.
    scopes = Counter(asset.scope for asset in inventory)
    shelf = " · ".join(
        f"{scopes[scope]} {label}" for scope, label in (
            ("project", "from this repo"),
            ("personal", "personal"),
            ("plugin", "from plugins"),
            ("builtin", "built in"),
        ) if scopes.get(scope)
    )
    print(ui.field("installed",
                   f"{len(inventory)}  ({shelf})" if shelf
                   else str(len(inventory)), 11))
    if problem := context.settings_problem():
        # Said out loud, because "nothing is switched off" and "I could not
        # find out what is switched off" otherwise draw the same table.
        print(ui.field("enablement", f"{problem} — none treated as off", 11))
    if switched_off:
        # Not neglect. A skill in disabledSkills was a decision, and filing
        # it under 'never referenced' read as an accusation: eleven of the
        # sixteen this view used to shame were switched off on purpose.
        print(ui.field("disabled", f"{len(switched_off)} switched off in "
                                   f"settings.json", 11))
    print(ui.field("referenced", f"{len(used)} appear in at least one session", 11))
    if ran:
        # 'here' rather than 'no longer': a skill absent from this shelf may
        # be alive and well in another repository. What is true is that
        # Copilot could not load it standing in this one.
        strangers = len(missing) - len(elsewhere)
        print(ui.field("ran", f"{len(ran)} run by the CLI"
                              + (f" · {len(elsewhere)} from another checkout"
                                 if elsewhere else "")
                              + (f" · {strangers} not on this disk"
                                 if strangers else ""), 11))
    print(ui.field("idle", f"{len(idle)}" + (" enabled," if switched_off else "")
                           + " never referenced", 11))
    print()

    def _note(name: str) -> str:
        """What to say beside a row, plain — the widths are measured on this.

        One fact each, in order of what changes what you would do next: a
        skill that is not installed cannot be run, one that is switched off
        will not be, and after that the recorded load is the strongest thing
        there is to say.
        """
        if name.lower() not in on_disk:
            # Named, when the store knows a checkout that ships it. The bare
            # form is reserved for the ones that really are nowhere.
            found = elsewhere.get(name.lower())
            return f" · in {found}" if found else " · not installed"
        if on_disk[name.lower()].disabled:
            return " · off"
        if not ran:
            return ""          # no marker anywhere: the column does not apply
        return f" · {ran[name.lower()]} ran" if ran.get(name.lower()) else " · none"

    def _paint(name: str) -> str:
        """The same note, coloured."""
        plain = _note(name)
        if not plain:
            return ""
        colour = (ui.AMBER if plain == " · not installed"
                  else ui.MUTED if plain in (" · off", " · none")
                  else ui.VIOLET if plain.startswith(" · in ") else ui.MINT)
        return f" {colour}{plain.lstrip()}{ui.RST}"

    if used:
        title = "Most referenced" if column == "sessions" and descending else "Referenced"
        print(ui.heading(f"{title} {kind} · by {column}", ui.ACCENT, inner))
        peak = max(n for _, n in used)
        # What the note column costs, if it is drawn at all. It used to cost
        # nothing in this sum and be printed anyway, so every row carrying a
        # '· N ran' ran nine columns off the right edge of a 72-column
        # window — the bar was sized as though the note were not there.
        rows = used[:limit]
        load = max((len(_note(name)) for name, _ in rows), default=0)
        # Name and bar split what the window has, instead of a fixed 34 and
        # 16 that ran off anything under 64 columns. The bar gives way first:
        # which skill it is matters more than how long its bar is.
        span, gauge = _chart_spans(inner, 11 + load, name_cap=36)
        for name, sessions in rows:
            print(
                f"    {ui.MINT}{sessions:>4}{ui.RST}  {ui._fit(name, span):<{span}}"
                f" {ui.bar(sessions, peak, gauge, pad=bool(load))}{_paint(name)}".rstrip()
            )
        if len(used) > limit:
            print(f"    {ui.MUTED}… and {len(used) - limit} more{ui.RST}")
        print()

    if idle:
        print(ui.heading(f"Never referenced · {len(idle)}", ui.AMBER, inner))
        for line in _name_grid(idle, inner):
            print(f"{ui.MUTED}{line}{ui.RST}")
        if len(idle) > 24:
            print(f"    {ui.MUTED}… and {len(idle) - 24} more{ui.RST}")
        print()

    if switched_off:
        print(ui.heading(f"Switched off · {len(switched_off)}", ui.DIM, inner))
        for line in _name_grid(switched_off, inner):
            print(f"{ui.MUTED}{line}{ui.RST}")
        if len(switched_off) > 24:
            print(f"    {ui.MUTED}… and {len(switched_off) - 24} more{ui.RST}")
        print()

    print(ui.field("drill down", f"cs {kind if kind == 'skills' else 'profiles'} <name>"))
    print()
    _why(f"Counts are sessions that reference the {kind[:-1]} in a qualified "
          f"way — a path, a /command, backticks, or the word 'skill'/'agent' "
          f"beside it: a usage signal, not a call count. Where the CLI wrote "
          f"its own load marker into the turn, '{'· N ran'}' says so — that "
          f"part is recorded rather than inferred. '· none' means no load was "
          f"recorded, which is weaker than it sounds: the CLI only started "
          f"writing that marker partway through this store's life, so an "
          f"older session that did run one leaves no trace of it. What is "
          f"installed is what Copilot could load standing here — this "
          f"repository's, your own, and the enabled plugins' — because that "
          f"is the set it resolves from. '· off' is in settings.json. '· in "
          f"<name>' is the checkout that ships it — Copilot would load it "
          f"standing there, not here — and '· not installed' is the rest: "
          f"it ran somewhere this disk no longer has.", inner)
    print(_sort_note("assets", column, descending, inner))
    _why_hint(inner)
    print()


def cmd_instructions() -> bool:
    """The instruction files every session in this repo starts with."""
    return _page(_capture(_render_instructions))


def _render_instructions() -> None:
    """Instruction files on disk, and which of them the model won't read whole.

    `cs skills` asks which files a session referenced. Nothing references an
    instruction file — it is loaded before you type — so the only questions
    left are which ones are loaded, from where, and whether each is short
    enough to survive the trip.
    """
    found = context.audit()
    items = [item for item in found["items"] if item.kind == "instructions"]
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4

    print()
    print(ui.rule(inner, f"Instructions · {len(items)} on disk"))
    print()
    if not items:
        print(f"  {ui.MUTED}None found. cs looks for them in:{ui.RST}")
        print()
        for path in context.instruction_paths():
            short = hooks.short(str(path), keep=max(24, inner - 6))
            print(f"    {ui.CODE}{ui._fit(short, inner - 2)}{ui.RST}")
        print()
        _note("A repository with no instruction file starts every session by "
              "explaining itself again. Anything you would say twice belongs "
              "in one.", inner)
        print()
        return

    for scope, root in (("project", found["root"]),
                        ("personal", found["personal_root"])):
        scoped = [item for item in items if item.scope == scope]
        chars = sum(item.chars for item in scoped)
        shape = (f"{len(scoped)} file{'s' if len(scoped) != 1 else ''} · "
                 f"{chars:,} chars" if scoped else "nothing")
        print(ui.field(scope, ui.trunc(shape, inner - 11)))
        # Under its own value, at `field`'s hanging column. Elided from the
        # middle rather than cut short: a checkout deep enough to overflow
        # the window is identified by its last two segments, never its first.
        root_text = hooks.short(str(root), keep=max(24, inner - 13))
        print(f"           {ui.MUTED}{ui._fit(root_text, inner - 11)}{ui.RST}")
    print(ui.field("limit", f"{context.INSTRUCTION_LIMIT:,} chars per file "
                            f"before Copilot truncates"))
    print()

    print(ui.heading(f"Loaded before your first prompt · {len(items)}",
                     ui.ACCENT, inner))
    columns = [("scope", "scope", "<"), ("file", "file", "<"),
               ("chars", "chars", ">"), ("lines", "lines", ">"),
               ("headings", "headings", ">")]
    # Only `scope` and the space after it are fixed; the file name is the
    # flexible column. The old figure reserved another eleven characters for
    # nothing, and the rule under the headings came up eleven short of the
    # section hairline over them.
    # Dropped worst-first, and `chars` is worth the most here: it is the
    # number the limit applies to and the reason the view exists. It used to
    # be the first column to go, so a narrow window kept the heading count
    # and lost the only figure that decides whether a file is read whole.
    spans = _fit_columns(inner - 2, 9,
                         [("headings", 9), ("lines", 6), ("chars", 8)],
                         least=20, flex="file", gaps=_extra_gaps(columns))
    spans.update(scope=8)
    shown = [spec for spec in columns if spans[spec[0]]]
    heads = _row(shown, spans)
    _head_rule(heads)
    for item in items:
        colour = ui.ROSE if item.oversized else ""
        values = {
            "scope": (item.scope, ui.MUTED),
            "file": (item.label, colour),
            "chars": (f"{item.chars:,}", colour),
            "lines": (str(item.lines), ui.MUTED),
            "headings": (str(item.headings) if item.headings else "·", ui.MUTED),
        }
        print("    " + _row(shown, spans, values).rstrip())
    print()

    # The two faults worth naming are the ones that change what the model
    # actually reads: a file past the limit loses its tail, and a long file
    # with no headings is read as one undifferentiated topic. They used to
    # float loose under the table with no heading over them, and each
    # oversized file repeated the same twenty-word remedy — so a checkout
    # with three long files said "move the scoped rules into…" three times.
    oversized = [item for item in items if item.oversized]
    unsectioned = [item for item in items if not item.oversized and item.unsectioned]
    if oversized or unsectioned:
        faults = len(oversized) + len(unsectioned)
        print(ui.heading(f"Not read as written · {faults}",
                         ui.ROSE if oversized else ui.AMBER, inner))
        for item in oversized:
            over = item.chars - context.INSTRUCTION_LIMIT
            print(f"    {ui.ROSE}● {item.scope} {item.label}{ui.RST}")
            _item(f"{item.chars:,} characters — the last {over:,} are past the "
                  f"limit and are not read.",
                  inner - 6, marker="→", colour=ui.ROSE, indent=6)
        for item in unsectioned:
            print(f"    {ui.AMBER}● {item.scope} {item.label}{ui.RST}")
            _item(f"{item.lines} lines with no headings — read as one "
                  f"undifferentiated topic.",
                  inner - 6, marker="→", colour=ui.AMBER, indent=6)
        print()
        if oversized:
            _note("Move the scoped rules into .github/instructions/"
                  "*.instructions.md, which load only when they match.",
                  inner, indent=4)
        if unsectioned:
            _note("Add ## sections — a model skims structure the same way "
                  "you do.", inner, indent=4)
        print()

    _note("Read from disk, not from the store: this is what your next session "
          "starts with, whatever the last one did. Nothing here is written or "
          "changed.", inner)
    print()
    _why("There is no 'referenced' count here as there is on Skills, and that "
         "is the point of the view: an instruction file is loaded before your "
         "first word, so every session got all of it that fit. What varies is "
         "how much fit.", inner)
    _why_hint(inner)
    print()


def cmd_hooks(event: str | None = None, sort_by: str | None = None,
              descending: bool | None = None) -> bool:
    """What Copilot will run around a session, or one event in full."""
    if event:
        return _page(_capture(lambda: _render_hook_event(event)))
    return _page_report(
        "hooks",
        lambda column, down: _capture(lambda: _render_hooks(column, down)),
        sort_by, descending,
    )


def _wrap_path(path: str, width: int) -> list[str]:
    """A path broken after its separators, never through a name in it.

    `textwrap` breaks wherever the column runs out, which turned
    `.copilot/settings.json` into `.copilot/setti` and `ngs.json` — two
    strings, neither of them a path and neither of them a name.
    """
    head, _, tail = path.rpartition("/")
    parts = ([segment + "/" for segment in head.split("/")] + [tail]
             if head or path.startswith("/") else [path])
    lines, line = [], ""
    for part in parts:
        if line and len(line) + len(part) > width:
            lines.append(line)
            line = ""
        while len(part) > width:  # one name longer than the whole row
            lines.append(part[:width])
            part = part[width:]
        line += part
    if line:
        lines.append(line)
    return lines or [""]


def _search_paths(places: list[tuple[object, str]], inner: int) -> None:
    """Where a kind of configuration can be declared, scope first.

    Both empty states printed `f"{path}  ({scope})"` truncated from the
    right, which cut the scope off the end of every workspace line — the one
    word that says whether the file belongs to you or to the repository. The
    scope leads in a fixed column, the home directory contracts to `~`, and
    what is left of a long path is elided from the middle, where the least
    of it is.
    """
    room = max(20, inner - 14)
    for path, scope in places:
        # Wrapped, never elided. This is the one screen whose whole job is to
        # tell you where to put the file, and half a path is not somewhere
        # you can put a file. Only the home directory contracts, to `~`.
        text = hooks.short(str(path), keep=10_000)
        for index, part in enumerate(_wrap_path(text, room)):
            label = scope if index == 0 else ""
            print(f"    {ui.MUTED}{label:<10}{ui.RST}{ui.CODE}{part}{ui.RST}")


def _hook_source(entry: dict) -> str:
    """Where a hook came from, short enough for a column."""
    path = entry["source"]
    if path.name == "settings.json":
        return f"{entry['scope'][:4]}:settings"
    return redact.one_line(path.name)


def _render_hooks(column: str = "when", descending: bool = False) -> None:
    entries, problems = hooks.load()
    switched_off = hooks.parked()
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4

    print()
    print(ui.rule(inner, f"Hooks · {len(entries)} commands"))
    print()
    if not entries:
        print(f"  {ui.MUTED}No hook is configured.{ui.RST}")
        print()
        _note("A hook is a command Copilot runs on the lifecycle — when a "
              "session starts, before a tool call, when the agent stops. "
              "cs looks for them in:", inner)
        print()
        _search_paths(hooks.search_paths(), inner)
        print()
        _print_hook_problems(problems, switched_off, inner)
        return

    by_event: Counter[str] = Counter(entry["event"] for entry in entries)
    files = {entry["source"] for entry in entries}
    absent = [entry for entry in entries if entry["missing"]]
    personal = sum(1 for entry in entries if entry["scope"] == "personal")

    print(ui.field("files", f"{len(files)} declaring {len(entries)} commands"))
    unknown = [name for name in by_event if name not in hooks.EVENTS]
    known = f"{len(by_event) - len(unknown)} of the {len(hooks.EVENTS)} cs knows"
    print(ui.field("events", known + (f" · {len(unknown)} it does not"
                                      if unknown else "")))
    print(ui.field("scope", f"{personal} personal · {len(entries) - personal} "
                            f"from this workspace"))
    if absent:
        print(ui.field("missing", f"{len(absent)} point at a script that is "
                                  f"not on disk"))
    print()

    # Lifecycle order, not commonest first: the question a hook list answers
    # is "what happens to my session, in what order", and sorting by count
    # shuffles the answer.
    print(ui.heading(f"When they run · {len(by_event)}", ui.ACCENT, inner))
    peak = max(by_event.values())
    for name in sorted(by_event, key=hooks.order):
        count = by_event[name]
        known = "" if name in hooks.EVENTS else f" {ui.AMBER}?{ui.RST}"
        print(f"    {ui.MINT}{count:>5}{ui.RST}  {ui.trunc(name, 22):<22}"
              f" {ui.bar(count, peak, 12)}{known}")
    print()

    if absent:
        print(ui.heading(f"Scripts that are gone · {len(absent)}", ui.ROSE, inner))
        print(f"    {ui.MUTED}"
              f"{ui.trunc('Copilot will still run these, and the shell will fail.', inner - 2)}"
              f"{ui.RST}")
        for entry in absent:
            gone = ui.trunc(redact.plain(str(entry["target"])), inner - 2)
            print(f"    {ui.ROSE}{gone}{ui.RST}")
            print(f"      {ui.MUTED}{entry['event']} · "
                  f"{ui.trunc(_hook_source(entry), inner - 20)}{ui.RST}")
        print()

    print(ui.heading(f"Every hook · {len(entries)}", ui.ACCENT, inner))
    entries = _sort_report(entries, "hooks", column, descending)
    # Fixed: indent 4, when 19+1. The matcher goes first when the window
    # narrows — most hooks have none — then where it was declared.
    columns = [("when", "when", "<"), ("tool", "tool", "<"),
               ("command", "runs", "<"), ("source", "from", "<")]
    spans = _fit_columns(inner - 2, 24, [("tool", 8), ("source", 14)],
                         least=24, flex="command", gaps=_extra_gaps(columns))
    spans.update(when=19)
    shown = [spec for spec in columns if spans[spec[0]]]
    heads = _row(shown, spans)
    _head_rule(heads, 4, column, _HOOKS_HEADS, descending)
    for entry in entries:
        values = {
            "when": (entry["event"], ui.SKY),
            "tool": (entry["matcher"] or "·",
                     "" if entry["matcher"] else ui.MUTED),
            # Masked like every other view: a hook command is a shell line,
            # and shell lines are where an exported token ends up.
            "command": (hooks.short(redact.redact(entry["command"])),
                        ui.ROSE if entry["missing"] else ""),
            "source": (_hook_source(entry), ui.MUTED),
        }
        print("    " + _row(shown, spans, values).rstrip())
    print()
    _print_hook_problems(problems, switched_off, inner)
    _why("Hooks are configuration, not history: the session store records "
          "no hook event, so this is what Copilot will run — never a count "
          "of what it did run.", inner)
    drill = ui.trunc("cs hooks <event> — one event, commands in full", inner - 2)
    print(f"  {ui.MUTED}{drill}{ui.RST}")
    print(_sort_note("hooks", column, descending, inner))
    _why_hint(inner)
    print()


def _print_hook_problems(problems: list, switched_off: list, inner: int) -> None:
    """Files that would have declared hooks, and don't — for two reasons."""
    if problems:
        print(ui.heading(f"Not loaded · {len(problems)}", ui.ROSE, inner))
        for path, why in problems:
            print(f"    {ui.ROSE}{ui.trunc(redact.plain(path.name), inner - 2)}{ui.RST}")
            print(f"      {ui.MUTED}{ui.trunc(why, inner - 6)}{ui.RST}")
        print()
    if switched_off:
        print(ui.heading(f"Switched off · {len(switched_off)}", ui.AMBER, inner))
        for path in switched_off:
            # Abbreviated from the middle: `.off` and `.bak` are the whole
            # story of a parked file, and truncating loses exactly that.
            short = hooks.short(redact.plain(str(path)), keep=max(24, inner - 6))
            print(f"    {ui.MUTED}{ui._fit(short, inner - 2)}{ui.RST}")
        print()


def _render_hook_event(event: str) -> None:
    """One event, with every command in full — the drill-down `cs hooks` offers."""
    entries, _ = hooks.load()
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    wanted = event.lower().replace("-", "").replace("_", "")
    matched = [
        entry for entry in entries
        if entry["event"].lower().startswith(wanted)
        or wanted in entry["event"].lower()
    ]
    print()
    if not matched:
        print(ui.rule(inner, f"Hooks · {event}"))
        print()
        print(f"  {ui.MUTED}No hook runs on '{event}'.{ui.RST}")
        names = sorted({entry["event"] for entry in entries}, key=hooks.order)
        if names:
            listed = ui.trunc(", ".join(names), inner - 22)
            print(f"  {ui.MUTED}configured events: {listed}{ui.RST}")
        print()
        return

    name = matched[0]["event"]
    label = "command" if len(matched) == 1 else "commands"
    print(ui.rule(inner, f"{name} · {len(matched)} {label}"))
    print()
    for position, entry in enumerate(matched, 1):
        colour = ui.ROSE if entry["missing"] else ui.SKY
        head = f"{position}. {_hook_source(entry)}"
        if entry["matcher"]:
            head += f" · on {entry['matcher']}"
        if entry["timeout"]:
            head += f" · {entry['timeout']}s limit"
        print(f"  {colour}{ui.trunc(head, inner)}{ui.RST}")
        _item(redact.redact(entry["command"]), inner - 2)
        if entry["missing"]:
            gone = f"{entry['target']} is not on disk"
            print(f"      {ui.ROSE}{ui.trunc(gone, inner - 6)}{ui.RST}")
        print()


def cmd_mcp(name: str | None = None, sort_by: str | None = None,
            descending: bool | None = None) -> bool:
    """MCP servers wired up for this session, or one of them in full."""
    if name:
        return _page(_capture(lambda: _render_mcp_server(name)))
    return _page_report(
        "mcp",
        lambda column, down: _capture(lambda: _render_mcp(column, down)),
        sort_by, descending,
    )


def _mcp_source(server: dict) -> str:
    """Where a server was declared, short enough for a column.

    Without the extension: every file this can name is JSON, so five of the
    twenty characters said nothing and cost the rest of the name.
    """
    name = redact.one_line(server["source"].name)
    return f"{server['scope'][:4]}:{name.removesuffix('.json')}"


def _mcp_endpoint(server: dict) -> str:
    """What the server actually is, with the part that never varies removed.

    Every remote endpoint began `https://`, which is eight columns of the
    same eight characters on every row and was pushing the host — the only
    thing that identifies the server — off the end of the column.
    """
    endpoint = redact.redact(server["endpoint"])
    for scheme in ("https://", "http://"):
        if endpoint.startswith(scheme):
            return endpoint[len(scheme):]
    return hooks.short(endpoint, keep=34)


def _mcp_tools(server: dict) -> str:
    """What this server is allowed to expose, in a column's worth of words."""
    if server["off"]:
        return "none"
    if server["all_tools"]:
        return "all" if not server["tools"] else f"all +{len(server['tools'])}"
    return str(len(server["tools"]))


def _render_mcp(column: str = "name", descending: bool = False) -> None:
    servers, problems = mcp.load()
    switched_off = mcp.parked()
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4

    print()
    print(ui.rule(inner, f"MCP servers · {len(servers)}"))
    print()
    if not servers:
        print(f"  {ui.MUTED}No MCP server is configured.{ui.RST}")
        print()
        _note("An MCP server is a tool source Copilot can call that is not "
              "its own — a local process, or an HTTP endpoint. cs looks for "
              "them in:", inner)
        print()
        _search_paths(mcp.search_paths(), inner)
        print()
        _print_mcp_problems(problems, switched_off, inner)
        return

    conn = db.connect()
    counts = db.mcp_reference_counts(conn, [s["name"] for s in servers])
    conn.close()
    for server in servers:
        server["sessions"] = counts.get(server["name"], 0)

    remote = [s for s in servers if s["transport"] != "local"]
    personal = sum(1 for s in servers if s["scope"] == "personal")
    absent = [s for s in servers if s["missing"]]
    open_ended = [s for s in servers if s["all_tools"] and not s["off"]]
    leaking = [s for s in servers if s["secrets"]]
    files = {s["source"] for s in servers}

    print(ui.field("files", f"{len(files)} declaring {len(servers)} servers"))
    # Local versus remote first, because it is the only line here that says
    # whether the conversation leaves this machine.
    print(ui.field("type", f"{len(servers) - len(remote)} local · "
                           f"{len(remote)} remote"))
    print(ui.field("scope", f"{personal} personal · {len(servers) - personal} "
                            f"from this workspace"))
    print(ui.field("tools", f"{len(open_ended)} of {len(servers)} expose "
                            f"everything the server offers"))
    if absent:
        print(ui.field("missing", f"{len(absent)} run a command that is not "
                                  f"on this machine"))
    print()

    print(ui.heading(f"Every server · {len(servers)}", ui.ACCENT, inner))
    ordered = _sort_report(servers, "mcp", column, descending)
    # Indent 4, then name, transport 9, tools 6 and their gaps. The endpoint
    # flexes, and `from` is the first column a narrow window drops — a handful
    # of files declare all of these, and the name identifies the server.
    # `least` is low because a truncated command still reads (the program is
    # at the front of it), and losing the session count to keep four more
    # characters of `npx -y @some/server` would be the wrong trade. The name
    # gives ground too rather than staying at 20 and pushing the row off a
    # small window.
    name_span = max(10, min(20, inner - 33,
                            max(ui.cells(s["name"]) for s in servers)))
    columns = [("name", "server", "<"), ("transport", "transport", "<"),
               ("endpoint", "runs", "<"), ("tools", "tools", ">"),
               ("sessions", "sessions", ">"), ("source", "from", "<")]
    # name + transport(9) + tools(6), each with the space after it: the old
    # figure was four columns too generous, so the rule under the headings
    # stopped short of the section hairline above them.
    spans = _fit_columns(inner - 2, name_span + 1 + 10 + 7,
                         [("source", 18), ("sessions", 9)],
                         least=14, flex="endpoint", gaps=_extra_gaps(columns))
    spans.update(name=name_span, transport=9, tools=6)
    shown = [spec for spec in columns if spans[spec[0]]]
    heads = _row(shown, spans)
    _head_rule(heads, 4, column, _MCP_HEADS, descending)
    for server in ordered:
        # A remote server is coloured because it is the one that matters: a
        # local command is code you already have, an https endpoint is your
        # session going somewhere else.
        transport = (ui.AMBER if server["transport"] != "local"
                     else ui.MUTED)
        values = {
            "name": (server["name"], ui.ROSE if server["missing"] else ui.SKY),
            "transport": (server["transport"], transport),
            # Masked like every other view: a config line is where a pasted
            # token ends up, and this one is read out of the repository.
            "endpoint": (_mcp_endpoint(server),
                         ui.ROSE if server["missing"] else ""),
            "tools": (_mcp_tools(server),
                      ui.AMBER if server["all_tools"] else ui.MUTED),
            "sessions": (str(server["sessions"]) if server["sessions"] else "·",
                         "" if server["sessions"] else ui.MUTED),
            "source": (_mcp_source(server), ui.MUTED),
        }
        print("    " + _row(shown, spans, values).rstrip())
    print()

    if leaking:
        print(ui.heading(
            ui._fit(f"Credentials written into the config · {len(leaking)}", inner),
            ui.ROSE, inner))
        warning = ("A literal value, not ${VAR} — so it is in the file, and "
                   "the file gets committed.")
        print(f"    {ui.MUTED}{ui.trunc(warning, inner - 2)}{ui.RST}")
        for server in leaking:
            keys = ", ".join(server["secrets"])
            print(f"    {ui.ROSE}{ui.trunc(server['name'], 20):<20}{ui.RST}"
                  f" {ui.MUTED}{ui.trunc(keys, inner - 26)}{ui.RST}")
        print()

    if absent:
        print(ui.heading(f"Commands that are gone · {len(absent)}", ui.ROSE, inner))
        warning = "Copilot will still try to start these, and the spawn will fail."
        print(f"    {ui.MUTED}{ui.trunc(warning, inner - 2)}{ui.RST}")
        for server in absent:
            print(f"    {ui.ROSE}{ui.trunc(server['name'], inner - 2)}{ui.RST}")
            print(f"      {ui.MUTED}"
                  f"{ui.trunc(redact.redact(server['endpoint']), inner - 6)}{ui.RST}")
        print()

    idle = [s["name"] for s in servers if not s["sessions"]]
    if idle:
        print(ui.heading(f"Never referenced · {len(idle)}", ui.AMBER, inner))
        for line in _name_grid(idle, inner):
            print(f"{ui.MUTED}{line}{ui.RST}")
        if len(idle) > 24:
            print(f"    {ui.MUTED}… and {len(idle) - 24} more{ui.RST}")
        print()

    _print_mcp_problems(problems, switched_off, inner)
    _why("MCP servers are configuration, not history: the store records no "
          "MCP invocation event. The session counts are a text signal — a "
          "tool named mcp__server__tool, or the server named as one — so "
          "they are evidence a server was reached for, never a call count.",
          inner)
    drill = ui.trunc("cs mcp <name> — one server, and the sessions that named it",
                     inner - 2)
    print(f"  {ui.MUTED}{drill}{ui.RST}")
    print(_sort_note("mcp", column, descending, inner))
    _why_hint(inner)
    print()


def _print_mcp_problems(problems: list, switched_off: list, inner: int) -> None:
    """Files that would have declared a server, and don't — for two reasons."""
    if problems:
        print(ui.heading(f"Not loaded · {len(problems)}", ui.ROSE, inner))
        for path, why in problems:
            print(f"    {ui.ROSE}{ui.trunc(redact.plain(path.name), inner - 2)}{ui.RST}")
            print(f"      {ui.MUTED}{ui.trunc(why, inner - 6)}{ui.RST}")
        print()
    if switched_off:
        print(ui.heading(f"Switched off · {len(switched_off)}", ui.AMBER, inner))
        for path in switched_off:
            # Abbreviated from the middle, not truncated from the right: the
            # suffix is the whole point of the line — `.bak` and `.sample` are
            # different stories — and truncating loses exactly that.
            short = hooks.short(redact.plain(str(path)), keep=inner - 8)
            print(f"    {ui.MUTED}{ui.trunc(short, inner - 2)}{ui.RST}")
        print()


def _render_mcp_server(name: str) -> None:
    """One server in full — what it is, what it may call, who reached for it."""
    servers, _ = mcp.load()
    known = {server["name"]: server for server in servers}
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    match = next(
        (real for real in known if real.lower() == name.lower()),
        next((real for real in known if name.lower() in real.lower()), None),
    )
    print()
    if not match:
        print(ui.rule(inner, f"MCP servers · {redact.one_line(name)}"))
        print()
        print(f"  {ui.MUTED}No MCP server named '{redact.one_line(name)}' is "
              f"configured.{ui.RST}")
        if known:
            print(f"  {ui.MUTED}configured: "
                  f"{ui.trunc(', '.join(known), inner - 14)}{ui.RST}")
        print()
        return

    server = known[match]
    conn = db.connect()
    rows = db.sessions_for_mcp(conn, match)
    conn.close()

    print(ui.rule(inner, f"MCP servers · {match}",
                  note=f"{len(rows)} sessions"))
    print()
    print(ui.field("type", server["transport"]))
    print(ui.field("runs", ui.trunc(redact.redact(server["endpoint"]), inner - 11)))
    print(ui.field("scope", server["scope"]))
    declared = hooks.short(redact.plain(str(server["source"])),
                           keep=max(24, inner - 13))
    print(ui.field("declared", ui._fit(declared, inner - 11)))
    if server["off"]:
        print(ui.field("tools", "none — every tool is switched off"))
    elif server["all_tools"]:
        print(ui.field("tools", "every tool the server offers, including ones "
                                "it adds later"))
    else:
        print(ui.field("tools", ui.trunc(", ".join(server["tools"]), inner - 11)))
    print()

    if server["missing"]:
        print(f"  {ui.ROSE}The command is not on this machine — this server "
              f"will fail to start.{ui.RST}")
        print()
    if server["secrets"]:
        print(f"  {ui.ROSE}A literal credential is written into the config: "
              f"{ui.trunc(', '.join(server['secrets']), inner - 44)}{ui.RST}")
        print()

    if not rows:
        print(f"  {ui.MUTED}No session names it.{ui.RST}")
        print()
        return

    # The same table shape as every other listing, rather than two lines per
    # session with a full uuid and `cs show` repeated down the page: the id
    # is a column, and the command that takes one is named once underneath.
    columns = [("active", "last active", "<"), ("session", "session", "<"),
               ("summary", "summary", "<")]
    spans = _fit_columns(inner - 2, 10, [("active", 12)],
                         gaps=_extra_gaps(columns))
    spans.update(session=9)
    shown = [spec for spec in columns if spans[spec[0]]]
    heads = _row(shown, spans)
    print(ui.heading(f"Sessions that named it · {len(rows)}", ui.ACCENT, inner))
    _head_rule(heads)
    for session_id, summary, last_active in rows:
        values = {
            "active": (_when(last_active), ui.MUTED),
            "session": (session_id[:8], ui.SKY),
            "summary": (redact.redact(summary) or "(untitled)", ""),
        }
        print("    " + _row(shown, spans, values).rstrip())
    print()
    _hint("cs show <session> — one session's ledger", inner)
    print()


# How loudly a finding is worth reading. The bar is not decoration: it is the
# share of the sample the finding covers, which is the only thing that tells
# "nine sessions did this" apart from "nine sessions did this, out of eleven".
_LEVELS = {
    "high": (ui.ROSE, "worth changing this week"),
    "medium": (ui.AMBER, "worth a look"),
    "low": (ui.MUTED, "a tendency, not a problem"),
}


def cmd_coach(days: int = 30, sort_by: str | None = None,
              descending: bool | None = None) -> bool:
    """What the record says about how the work is being done.

    The one view in cs that grades the person rather than the setup. Every
    finding names the sample it came from and shows real examples, because a
    habit you cannot see an instance of is an accusation rather than a
    finding.
    """
    return _page_report(
        "coach",
        lambda column, down: _capture(lambda: _render_coach(days, column, down)),
        sort_by, descending,
    )


def _render_coach(days: int, column: str = "severity",
                  descending: bool = False) -> None:
    conn = db.connect()
    snap = practice.snapshot(conn, days)
    conn.close()
    findings, scores = practice.review(snap)
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4

    print()
    print(ui.rule(inner, f"Practice · {_window_label(days)} · "
                         f"{len(snap.sessions):,} sessions"))
    print()
    if not snap.sessions:
        print(f"  {ui.MUTED}No sessions in this window.{ui.RST}")
        print()
        return

    counts = Counter(found.severity for found in findings)
    print(ui.field("read", f"{len(snap.sessions):,} sessions · "
                           f"{len(snap.turns):,} turns"))
    if snap.calls():
        print(ui.field("calls", f"{snap.calls():,} to the models"))
    if findings:
        spread = " · ".join(f"{counts[level]} {level}"
                            for level in ("high", "medium", "low")
                            if counts[level])
        summary = f"{len(findings)} habits · {spread}"
        print(ui.field("found", ui.trunc(summary, inner - 11)))
    else:
        print(ui.field("found", "nothing worth naming here"))
    print()

    print(ui.heading("Scores", ui.ACCENT, inner))
    # The bar is the last thing to keep its size: the number is the answer,
    # and on a narrow window the bar becomes a hint rather than a gauge.
    # Coloured by the score rather than by the ramp — a meter that goes red
    # is the whole point of a meter.
    steps = max(4, min(20, inner - 26))
    for group in practice.GROUPS:
        score = scores[group]
        colour = ui.MINT if score >= 80 else ui.AMBER if score >= 55 else ui.ROSE
        print(f"    {colour}{score:>4}{ui.RST}  {group:<17} "
              f"{ui.meter(score / 100, steps, colour)}")
    print()

    if not findings:
        print(f"  {ui.MINT}Every rule came back clean for this window.{ui.RST}")
        print()
        _coach_footnote(inner)
        print(_sort_note("coach", column, descending, inner))
        print()
        return

    findings = _sort_report(findings, "coach", column, descending)
    print(ui.heading(f"What to change · {len(findings)}", ui.ACCENT))
    print()
    # The group and the share are worth having and worth losing: on a narrow
    # window the name of the habit is the only part that cannot go.
    spans = _fit_columns(inner - 2, 11, [("group", 16), ("share", 11)],
                         least=18, flex="name")
    for found in findings:
        colour, _ = _LEVELS[found.severity]
        tail = ""
        if spans["group"]:
            tail += " " + _cell(found.group, spans["group"])
        if spans["share"]:
            tail += " " + _cell(f"{found.count:,}/{found.total:,}",
                                spans["share"], ">")
        head = _cell(found.name, spans["name"])
        print(f"    {colour}● {found.severity:<7}{ui.RST}{ui.BOLD}{head}{ui.RST}"
              f"{ui.MUTED}{tail}{ui.RST}".rstrip())
        _item(redact.redact(found.headline), inner - 6, indent=6)
        _item(redact.redact(found.fix), inner - 6, marker="→", colour=colour,
              indent=6)
        for line in found.evidence:
            print(f"        {ui.MUTED}"
                  f"{ui.trunc(redact.redact(line), inner - 8)}{ui.RST}")
        print()
    _coach_footnote(inner)
    print(_sort_note("coach", column, descending, inner))
    print()


def _coach_footnote(inner: int) -> None:
    _note(
        f"Each group starts at 100 and every finding costs its severity — "
        f"high {practice.COST['high']}, medium {practice.COST['medium']}, "
        f"low {practice.COST['low']}. Nothing else moves the number, so the "
        f"list above is the whole calculation.", inner
    )
    _note(
        "Rules stay quiet below their minimum sample: silence here means "
        "'not enough to say', never 'nothing to find'.", inner
    )


def cmd_rhythm(days: int = 30) -> bool:
    """When the work happens — described, not judged.

    Late nights and weekends are counted and shown without comment. A run at
    23:00 may be a deadline, a timezone or a scheduled job, and the store
    cannot tell which, so this view declines to guess.
    """
    return _page(_capture(lambda: _render_rhythm(days)))


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Below this many turns, rhythm reports counts and stops reporting shares.
# Picked at two working weeks' worth of light use rather than from a table:
# the number only has to be large enough that one unusual afternoon cannot
# own the percentage, and small enough that a real fortnight clears it.
_RHYTHM_FLOOR = 25


def _widen_hint(days: int) -> str:
    """The next window or two worth suggesting when this one came back empty.

    Named windows only, so the suggestion is something the reader can also
    reach with a keypress on the landing screen rather than a number they
    would have to invent. All-time is left out because the caller offers it
    separately, and offering it twice in one sentence reads like a stutter.
    """
    wider = [d for _key, d, _label, _short in _PERIODS if d > days]
    return " or ".join(f"'cs rhythm {d}'" for d in wider[:2])


def _render_rhythm(days: int) -> None:
    conn = db.connect()
    snap = practice.snapshot(conn, days)
    conn.close()
    beat = practice.rhythm(snap)
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4

    print()
    print(ui.rule(inner, f"Rhythm · {_window_label(days)}"))
    print()
    if not beat["turns"]:
        # Two different nothings, and they want different answers. The old
        # text blamed the schema for both, which sent anyone with a simply
        # quiet month looking for a database problem that was not there.
        #
        # `snap.sessions` is already filtered to sessions that recorded a
        # turn (see practice.snapshot — a session with no turns is a launch,
        # not a habit), so empty means "nothing was asked in this window"
        # rather than "no session row exists".
        if not snap.sessions:
            print(f"  {ui.MUTED}Nothing was asked in this window.{ui.RST}")
            print()
            hint = _widen_hint(days)
            _note((f"Try a longer one — {hint} — or " if hint else "Try ")
                  + "'cs rhythm all' for every record.", inner)
        else:
            print(f"  {ui.MUTED}No turn in this window carries a "
                  f"timestamp.{ui.RST}")
            print()
            _note("Older stores predate turns.timestamp; without it there is "
                  "no hour to put the work at.", inner)
        print()
        return

    total = beat["turns"]
    # A percentage is a claim about a tendency, and a tendency needs enough
    # turns to be one. At nine turns a single Saturday afternoon reads as
    # "67% weekend work", which is arithmetically true and completely
    # misleading. Below the floor the view still draws — the histogram and
    # the counts are facts at any size — it just stops phrasing them as
    # shares and says why.
    thin = total < _RHYTHM_FLOOR
    active, run = beat["days_active"], beat["longest_streak"]
    print(ui.field("turns", f"{total:,} on {active} working "
                            f"day{'' if active == 1 else 's'}"))
    print(ui.field("span", f"{beat['first']} → {beat['last']}"))
    # "1 days, back to back" was describing a single day as a streak. One day
    # is not back to back with anything.
    print(ui.field("streak", f"{run} days, back to back" if run > 1
                             else "no day followed another"))
    busiest, busy_count = beat["busiest_day"]
    if busiest:
        print(ui.field("busiest", f"{busiest} · {busy_count} turns"))
    late = beat["late_night"]
    weekend = beat["weekend"]

    def share(count: int, when: str) -> str:
        body = f"{count:,} turns {when}"
        return body if thin else f"{body} ({count / total:.0%})"

    print(ui.field("late", share(late, "22:00–05:00")))
    print(ui.field("weekend", share(weekend, "Sat/Sun")))
    if beat["median_ms"]:
        print(ui.field("slowest", f"{beat['median_ms'] / 1000:.0f}s median · "
                                  f"{beat['p90_ms'] / 1000:.0f}s p90"))
    print()
    if thin:
        _note(f"{total} turns is too few to read as a pattern, so the counts "
              f"above are left as counts. Shares appear from "
              f"{_RHYTHM_FLOOR} turns.", inner)
        print()

    print(ui.heading("Hour of day", ui.ACCENT, inner))
    hours = beat["hours"]
    peak = max(hours.values()) if hours else 1
    span = max(12, inner - 18)
    for hour in range(24):
        count = hours.get(hour, 0)
        # Night hours are marked rather than coloured: the point is to make
        # the block of late work visible at a glance without claiming it is
        # bad, which is a judgement this report does not get to make.
        mark = f"{ui.MUTED}·{ui.RST}" if hour >= 22 or hour < 5 else " "
        print(f"    {ui.MUTED}{hour:02d}{ui.RST} {mark} "
              f"{ui.bar(count, peak, span, pad=True)} "
              f"{ui.MUTED}{count or ''}{ui.RST}")
    print()

    print(ui.heading("Day of week", ui.ACCENT, inner))
    weekdays = beat["weekdays"]
    peak = max(weekdays.values()) if weekdays else 1
    for index, name in enumerate(_WEEKDAYS):
        count = weekdays.get(index, 0)
        # The weekend keeps its own colour: it is the one distinction on this
        # chart that is not about size, and the ramp is already saying size.
        colour = ui.AMBER if index >= 5 else ""
        print(f"    {ui.MUTED}{name}{ui.RST} "
              f"{ui.bar(count, peak, span, colour=colour, pad=True)} "
              f"{ui.MUTED}{count or ''}{ui.RST}")
    print()
    _note("Times are local, converted from the UTC the store records. "
          "The slowest figure is the longest single call in a turn. "
          "Scheduled runs hidden by .cs-ignore are left out, so this is "
          "when you were working.", inner)
    print()


def cmd_context() -> bool:
    """What this repository hands the agent before you type anything.

    Reads the working directory rather than the session store, so it is the
    one Improve view with no time window: instruction files, prompts, skills,
    agent profiles and hooks are either on disk now or they are not.
    """
    return _page(_capture(_render_context))


def _render_context() -> None:
    found = context.audit()
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    items = found["items"]

    print()
    print(ui.rule(inner, f"Context · {found['root'].name}"))
    print()
    for scope, root in (("project", found["root"]),
                        ("personal", found["personal_root"]),
                        ("plugin", found["plugin_root"]),
                        ("builtin", found["builtin_root"])):
        kinds = Counter(item.kind for item in items if item.scope == scope)
        # 'nothing' is worth saying for the two scopes you always have. A
        # plugin row on a machine with no plugins is furniture.
        if not kinds and scope in ("plugin", "builtin"):
            continue
        shape = " · ".join(f"{count} {kind}" for kind, count in
                           sorted(kinds.items())) or "nothing"
        print(ui.field(scope, ui.trunc(shape, inner - 11)))
        print(f"           {ui.MUTED}{ui.trunc(str(root), inner - 11)}{ui.RST}")
    print(ui.field("hooks", f"{len(found['hooks'])} commands on the lifecycle"))
    servers, _ = mcp.load()
    remote = sum(1 for server in servers if server["transport"] != "local")
    print(ui.field("mcp", f"{len(servers)} servers · {remote} remote"))
    print()

    problems = context.gaps(found)
    if problems:
        print(ui.heading(f"Gaps · {len(problems)}", ui.ACCENT))
        print()
        for severity, what, fix in problems:
            colour, _ = _LEVELS[severity]
            print(f"    {colour}● {severity:<7}{ui.RST}"
                  f"{ui.BOLD}{ui.trunc(what, inner - 13)}{ui.RST}")
            _item(fix, inner - 6, marker="→", colour=colour, indent=6)
            print()
    else:
        print(f"  {ui.MINT}Nothing missing that cs knows to look for.{ui.RST}")
        print()

    # Only instruction and prompt files are worth a row each. Skills and
    # agent profiles are an inventory, and cs already has two commands that
    # do inventories properly.
    listed = [item for item in items if item.kind in ("instructions", "prompts")]
    if items:
        print(ui.heading(f"Instruction files · {len(listed)}", ui.ACCENT))
        spans = _fit_columns(inner - 2, 20, [("chars", 8), ("lines", 6)],
                             least=20, flex="file")
        spans.update(scope=8, kind=12)
        columns = [("scope", "scope", "<"), ("kind", "kind", "<"),
                   ("file", "file", "<"), ("chars", "chars", ">"),
                   ("lines", "lines", ">")]
        shown = [spec for spec in columns if spans[spec[0]]]
        heads = " ".join(_cell(head, spans[key], align)
                         for key, head, align in shown)
        print(f"    {ui.MUTED}{heads}{ui.RST}")
        print(f"    {ui.MUTED}{'─' * len(heads)}{ui.RST}")
        for item in listed:
            colour = ui.ROSE if item.oversized else ""
            values = {
                "scope": (item.scope, ui.MUTED),
                "kind": (item.kind, ui.MUTED),
                "file": (item.label, colour),
                "chars": (f"{item.chars:,}", colour),
                "lines": (str(item.lines), ui.MUTED),
            }
            print("    " + " ".join(
                _cell(values[key][0], spans[key], align, values[key][1])
                for key, _head, align in shown
            ).rstrip())
        rest = len(items) - len(listed)
        if rest:
            _note(f"… and {rest} skills and agent profiles — "
                  f"cs skills · cs profiles", inner, indent=4)
        print()
    _note("Read from disk, not from the store: this is the setup your next "
          "session will start from, whatever the last one did. Nothing here "
          "is written or changed.", inner)
    print()


def cmd_agents(days: int = 30) -> bool:
    """Delegation and context churn: who actually did the work."""
    return _page(_capture(lambda: _render_agents(days)))


def _render_agents(days: int) -> None:
    conn = db.connect()
    if not db.has_delegation(conn):
        conn.close()
        print(
            "error: this session store does not record who initiated each call "
            "(no initiator/agent_id columns), so delegation cannot be measured",
            file=sys.stderr,
        )
        sys.exit(1)
    split = db.work_split(conn, days)
    conn.close()

    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    rows = split.get("by_initiator") or []
    print()
    print(ui.rule(inner, f"Delegation · {_window_label(days)}"))
    print()
    if not rows:
        print(f"  {ui.MUTED}No AI usage recorded in this range.{ui.RST}")
        print()
        return

    total_calls = sum(calls for _, calls, _, _ in rows)
    total_nano = sum(nano for _, _, nano, _ in rows)
    print(ui.field("sessions", str(split["sessions"])))
    print(ui.field("calls", f"{total_calls:,}"))
    print(ui.field("tasks", f"{split['delegated_tasks']} handed to sub-agents"))
    print(ui.field("spend", f"{ui.fmt_aiu(total_nano)} AIU"))
    print()

    print(ui.heading("Who initiated the work", ui.ACCENT, inner))
    labels = {
        "user": ("you", ui.MINT),
        "agent": ("main agent", ui.ACCENT),
        "sub-agent": ("sub-agents", ui.VIOLET),
        "compaction": ("compaction", ui.AMBER),
        "unknown": ("unattributed", ui.MUTED),
    }
    # A named, ruled table like every other report, instead of a fixed-width
    # row that ran off any window under 86 columns. The bar is last and takes
    # whatever is left, so it is the only thing that changes size.
    spans = _fit_columns(inner - 2, 13,
                         [("sessions", 8), ("share", 5),
                          ("spend", 9), ("calls", 7)], least=6)
    spans["who"] = 12
    columns = [
        ("who", "who", "<"), ("calls", "calls", ">"), ("spend", "spend", ">"),
        ("share", "share", ">"), ("sessions", "sessions", ">"),
        ("summary", "", "<"),
    ]
    layout = [spec for spec in columns if spans[spec[0]]]
    heads = " ".join(_cell(head, spans[key], align) for key, head, align in layout)
    print(f"    {ui.MUTED}{heads.rstrip()}{ui.RST}")
    print(f"    {ui.MUTED}{'─' * len(heads)}{ui.RST}")
    peak = max((calls for _, calls, _, _ in rows), default=0)
    for name, calls, nano, sessions in rows:
        label, colour = labels.get(name, (name, ui.MUTED))
        # At least one block for any non-zero count: a blank cell beside 2,115
        # calls reads as nothing happening, which is the opposite of the truth.
        filled = int(calls / peak * spans["summary"]) if peak else 0
        bar = "█" * max(1, filled) if calls else ""
        values = {
            "who": (label, ui.MUTED),
            "calls": (f"{calls:,}", ""),
            "spend": (ui.fmt_aiu(nano), ui.VIOLET),
            "share": (f"{nano / total_nano * 100:.0f}%" if total_nano else "-",
                      ui.MUTED),
            "sessions": (str(sessions), ui.MUTED),
            "summary": (bar, colour),
        }
        print("    " + " ".join(
            _cell(values[key][0], spans[key], align, values[key][1])
            for key, _head, align in layout
        ).rstrip())
    print()

    note = ("A 'task' is one delegation. The store records no agent name — "
            "agent_id is the delegating tool-call id.")
    for line in textwrap.wrap(note, max(30, inner)):
        print(f"  {ui.MUTED}{line}{ui.RST}")
    print()

    if split["top_delegating"]:
        print(ui.heading("Sessions that delegate most", ui.VIOLET, inner))
        # One row per session, not two: the id used to appear only in a hint
        # line under each row, which doubled the height of the block and left
        # nothing to scan down.
        top = _fit_columns(inner - 2, 10, [("calls", 6), ("tasks", 6)])
        top["session"] = 9
        top_columns = [
            ("session", "session", "<"), ("calls", "calls", ">"),
            ("tasks", "tasks", ">"), ("summary", "summary", "<"),
        ]
        top_layout = [spec for spec in top_columns if top[spec[0]]]
        top_heads = " ".join(
            _cell(head, top[key], align) for key, head, align in top_layout)
        print(f"    {ui.MUTED}{top_heads.rstrip()}{ui.RST}")
        print(f"    {ui.MUTED}{'─' * len(top_heads)}{ui.RST}")
        for sid, summary, delegated, tasks, _nano in split["top_delegating"]:
            values = {
                "session": (sid[:8], ui.SKY),
                "calls": (f"{delegated:,}", ui.VIOLET),
                "tasks": (str(tasks), ui.ACCENT),
                "summary": (
                    redact.one_line(redact.redact(summary)) or "(untitled)",
                    "",
                ),
            }
            print("    " + " ".join(
                _cell(values[key][0], top[key], align, values[key][1])
                for key, _head, align in top_layout
            ).rstrip())
        print()
        print(f"    {ui.MUTED}cs show <id> — one session's ledger{ui.RST}")
        print()

    compaction = next((r for r in rows if r[0] == "compaction"), None)
    if compaction and total_nano:
        churn = (f"Compaction is context being re-summarised: {compaction[1]} "
                 f"events cost {ui.fmt_aiu(compaction[2])} AIU "
                 f"({compaction[2] / total_nano * 100:.0f}% of spend).")
        for line in textwrap.wrap(churn, max(30, inner)):
            print(f"  {ui.MUTED}{line}{ui.RST}")
        print()


# ── Governance: autonomy, handoffs, exposure ─────────────────────────
# Three questions you have to be able to answer about work an agent did:
# did it run unattended, was it handed on, and is there a credential in it.
# None of the three is a column in the store — see cs/signals.py for how each
# is read out of what is recorded, and what evidence backs it.

def _when(stamp: str) -> str:
    """A timestamp as a person reads it: no date-time 'T' in the middle."""
    return stamp[5:16].replace("T", " ")


def cmd_yolo(show_all: bool = False, sort_by: str | None = None,
             descending: bool | None = None) -> bool:
    """How autonomously each session ran — YOLO mode, as far as it can be told."""
    return _page_report(
        "yolo",
        lambda column, down: _capture(lambda: _render_yolo(show_all, column, down)),
        sort_by, descending,
    )


# ── Autonomy ─────────────────────────────────────────────────────────
# One tier per verdict, in the order you would deal with them. The label is
# what the section heading says, the meaning is what the tier block says, and
# the note is the finding under the heading — printed always, because it is
# about the rows in front of you rather than about how the view works.
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
              descending: bool | None = None) -> bool:
    """Sessions holding credential-shaped text — the security view."""
    return _page_report(
        "audit",
        lambda column, down: _capture(lambda: _render_audit(session, column, down)),
        sort_by, descending,
    )


def _render_audit(ref: str | None, column: str = "risk",
                  descending: bool = True) -> None:
    session_id = _resolve_ref(ref) if ref else None
    conn = db.connect()
    if session_id and not db.session_detail(conn, session_id):
        conn.close()
        print(f"error: session not found: {session_id}", file=sys.stderr)
        sys.exit(1)
    rows = signals.exposures(conn, session_id)
    touched = signals.sensitive_files(conn, session_id)
    destructive = signals.destructive(conn, session_id)
    total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    conn.close()
    rows = _sort_report(rows, "audit", column, descending)
    # Security is a scanning view, not prose. The old 96-column reading width
    # wasted almost half of a 159-column terminal and crushed the summary.
    width = min(shutil.get_terminal_size().columns, 156)
    inner = width - 4

    # Named for the menu row that opens it. It used to be "Security posture"
    # on a wide window and "Security" on a narrow one, which is two names for
    # one page and neither of them the one you chose from the landing screen.
    title = (f"Security · session {session_id[:8]}" if session_id
             else f"Security · {total_sessions:,} sessions scanned")
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


# The kind as a column word. The heading says it in full; a table says it in
# eight characters or it stops being a table.
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


def cmd_read(ref: str, turn: int | None = None) -> bool:
    """Print a session's full transcript. True if a pager showed it."""
    session_id = _resolve_ref(ref)
    conn = db.connect()
    detail = db.session_detail(conn, session_id)
    if not detail:
        print(f"error: session not found: {session_id}", file=sys.stderr)
        conn.close()
        sys.exit(1)
    turns = db.session_transcript(conn, session_id)
    nano = sum(entry[2] for entry in db.session_usage(conn, session_id))
    conn.close()
    if not turns:
        print(f"\n  {ui.DIM}This session has no recorded turns.{ui.RST}\n")
        return False
    return _page(_render_transcript(session_id, detail, turns, turn, nano))


_COMPLETION_COMMANDS = (
    "home", "recent", "all", "search", "repos", "stats", "cost", "efficiency",
    "agents", "timeline", "yolo", "handoff", "audit", "skills", "profiles",
    "instructions", "hooks", "mcp", "coach", "rhythm", "context", "show",
    "brief", "read",
    "export", "files", "resume", "help", "version",
)

# The flags worth offering after a subcommand. Named once because bash and
# zsh each need the same list verbatim, and a flag that reaches only two of
# the three shells is a flag most people never discover.
_COMPLETION_FLAGS = (
    "--json", "--csv", "--sort", "--asc", "--desc",
    "--all", "--turn", "--asks", "--short", "--by-repo",
)


def cmd_completion(shell: str) -> None:
    """Print a completion script for bash, zsh or fish.

    Small, static, and generated from the same command list the dispatcher
    uses, so a subcommand cannot exist without being completable. Nothing
    here shells back into `cs` to compute candidates: a completion that runs
    a program on every Tab is a completion that makes the shell feel broken
    the first time the store is large.
    """
    names = " ".join(_COMPLETION_COMMANDS)
    flags = " ".join(_COMPLETION_FLAGS)
    if shell == "bash":
        script = f"""# cs completions for bash — add to ~/.bashrc:
#   source <(cs completion bash)
_cs_complete() {{
    local cur=${{COMP_WORDS[COMP_CWORD]}}
    if [ "$COMP_CWORD" -eq 1 ]; then
        COMPREPLY=($(compgen -W "{names}" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "{flags}" -- "$cur"))
    fi
}}
complete -F _cs_complete cs
"""
    elif shell == "zsh":
        script = f"""# cs completions for zsh — add to ~/.zshrc:
#   source <(cs completion zsh)
_cs() {{
    local -a commands
    commands=({names})
    if (( CURRENT == 2 )); then
        compadd -- $commands
    else
        compadd -- {flags}
    fi
}}
compdef _cs cs
"""
    elif shell == "fish":
        lines = [
            "# cs completions for fish — save to "
            "~/.config/fish/completions/cs.fish:",
            "#   cs completion fish > ~/.config/fish/completions/cs.fish",
            "complete -c cs -f",
        ]
        lines += [
            f"complete -c cs -n __fish_use_subcommand -a {name}"
            for name in _COMPLETION_COMMANDS
        ]
        lines += [
            "complete -c cs -l json -d 'machine-readable output'",
            "complete -c cs -l csv -d 'comma-separated output'",
            "complete -c cs -l sort -d 'sort column'",
            "complete -c cs -l asc -d 'ascending'",
            "complete -c cs -l desc -d 'descending'",
            "complete -c cs -l short -d 'the story, without the inventory'",
            "complete -c cs -l asks -d 'list every request in order'",
            "complete -c cs -l turn -d 'one turn of a transcript'",
            "complete -c cs -l all -d 'every record, not just the window'",
            "complete -c cs -l by-repo -d 'skills grouped by where they ran'",
        ]
        script = "\n".join(lines) + "\n"
    else:
        print(
            f"error: unknown shell '{shell}' — choose from: bash, zsh, fish",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.stdout.write(script)


def cmd_export(ref: str, fmt: str = "md") -> None:
    """Write one session out as a document, to stdout.

    The reader is for reading; this is for keeping. A session that only
    exists inside a terminal cannot be attached to a pull request, pasted
    into an incident write-up, checked into a repository beside the change it
    produced, or handed back to a model for a summary — and those are the
    four things people actually want to do with a transcript once the work is
    finished.

    Masked on the way out like every other view. Text written to a file is
    more exposed than text on a screen, not less.
    """
    session_id = _resolve_ref(ref)
    conn = db.connect()
    detail = db.session_detail(conn, session_id)
    if not detail:
        print(f"error: session not found: {session_id}", file=sys.stderr)
        conn.close()
        sys.exit(1)
    turns = db.session_transcript(conn, session_id)
    nano = sum(entry[2] for entry in db.session_usage(conn, session_id))
    skills, agents = _assets_used(conn, session_id)
    subagents = db.subagent_detail(conn, session_id)
    conn.close()
    if fmt == "json":
        summary, repo, cwd, branch, created, updated = detail
        export.emit({
            "view": "session",
            "id": session_id,
            "summary": redact.one_line(redact.redact(summary or "")),
            "repository": repo or None,
            "branch": branch or None,
            "cwd": cwd or None,
            "created_at": created,
            "updated_at": updated,
            "nano_aiu": nano,
            # Names, not counts. A count in a pipe is a number somebody has
            # to come back to the terminal to explain, which defeats the
            # point of piping it. Each carries how it was established, so a
            # consumer can filter on certainty rather than trusting all of
            # it equally.
            "skills": [
                {"name": name, "turn": turn, "evidence": how, "quote": quote}
                for name, turn, quote, how in skills
            ],
            "agent_profiles": [
                {"name": name, "turn": turn, "evidence": how, "quote": quote}
                for name, turn, quote, how in agents
            ],
            "subagents": [
                {
                    "id": short_id, "model": model, "calls": calls,
                    "nano_aiu": agent_nano, "duration_ms": ms,
                    "first_turn": first, "last_turn": last,
                }
                for short_id, model, calls, agent_nano, ms, first, last
                in subagents
            ],
            "turns": [
                {
                    "turn": index,
                    "timestamp": stamp,
                    "prompt": redact.redact(prompt or ""),
                    "reply": redact.redact(reply or ""),
                }
                for index, prompt, reply, stamp in turns
            ],
        })
        return
    sys.stdout.write(
        export.transcript_markdown(detail, session_id, turns, nano,
                                   skills, agents, subagents)
    )


def cmd_files(pattern: str, sort_by: str | None = None,
              descending: bool | None = None) -> bool:
    """The sessions that touched a file — the way back from a path to the work.

    There used to be a bare form as well, a leaderboard of the most worked-on
    files. It was trivia: the busiest path in a 1,100-session store is a
    README touched nine times, and knowing that leads nowhere. Going from a
    file you are looking at to the session that wrote it does.
    """
    conn = db.connect()
    rows, hits = db.sessions_for_file(conn, pattern)
    conn.close()
    rows = _with_assets(rows)
    hits = {sid: (tool, _short_path(path, "")) for sid, (tool, path) in hits.items()}
    title = f"Files · '{pattern}' · {len(rows)} session{'' if len(rows) == 1 else 's'}"
    if sys.stdin.isatty() and sys.stdout.isatty():
        return _interactive_listing(rows, title, show_all=True, hits=hits)
    _render_listing(rows, title, show_all=True, hits=hits,
                    sort_by=sort_by, descending=descending)
    return False


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


def cmd_repos(sort_by: str | None = None, descending: bool | None = None) -> bool:
    return _page_report(
        "repos",
        lambda column, down: _capture(lambda: _render_repos(column, down)),
        sort_by, descending,
    )


def _render_repos(column: str = "sessions", descending: bool = True) -> None:
    conn = db.connect()
    rows = db.repos(conn)
    conn.close()
    rows = _sort_report(rows, "repos", column, descending)
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    print()
    print(ui.rule(inner, f"Repositories · {len(rows)}"))
    print()
    if not rows:
        print(f"  {ui.MUTED}No repositories recorded.{ui.RST}")
        print()
        return
    # The shared table shape, like every other report: this one was laid out
    # by hand at a fixed 75 columns and ran off any window narrower than that.
    # 4 is the indent and the gap after the name column.
    spans = _fit_columns(inner - 2, 4,
                         [("share", 12), ("active", 11), ("turns", 7),
                          ("credits", 8), ("sessions", 8)],
                         least=14, flex="repo")
    columns = [("repo", "repository", "<"), ("sessions", "sessions", ">"),
               ("turns", "turns", ">"), ("credits", "credits", ">"),
               ("active", "last active", ">")]
    shown = [spec for spec in columns if spans[spec[0]]]
    heads = " ".join(_cell(head, spans[key], align) for key, head, align in shown)
    if spans["share"]:
        heads += " " + _cell("share", spans["share"])
    print(f"  {ui.MUTED}{_mark_column(heads, column, _REPOS_HEADS, descending)}{ui.RST}")
    print(f"  {ui.MUTED}{'─' * ui.cells(heads)}{ui.RST}")
    peak = max((row[1] or 0 for row in rows), default=0)
    for repo, count, total_turns, nano_aiu, last in rows:
        values = {
            "repo": (redact.one_line(redact.redact(repo)), ""),
            "sessions": (f"{count:,}", ui.BOLD),
            "turns": (f"{total_turns or 0:,}", ui.MUTED),
            "credits": (ui.fmt_aiu(nano_aiu), ui.VIOLET),
            "active": (last, ui.MUTED),
        }
        line = "  " + " ".join(
            _cell(values[key][0], spans[key], align, values[key][1])
            for key, _head, align in shown
        )
        if spans["share"]:
            line += " " + ui.bar(count, peak, spans["share"], track=True)
        print(line.rstrip())
    print()
    print(_sort_note("repos", column, descending, inner))
    print()


def cmd_stats(days: int | None = None) -> bool:
    """Everything the store can say about what the work produced."""
    return _page(_capture(lambda: _render_stats(days)))


def _render_stats(days: int | None) -> None:
    from datetime import date

    conn = db.connect()
    basics = db.stats(conn)
    made = db.impact(conn, days)
    repos = db.top_repos_by_output(conn)
    conn.close()

    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    span = 1
    if basics["oldest"] and basics["newest"]:
        try:
            span = max(
                (
                    date.fromisoformat(basics["newest"])
                    - date.fromisoformat(basics["oldest"])
                ).days,
                1,
            )
        except ValueError:
            pass

    scope = _window_label(days)
    print()
    print(ui.rule(inner, f"Copilot sessions · {scope}"))
    print()

    print(ui.heading("Activity", ui.ACCENT))
    print(ui.field("sessions", f"{basics['total']:,} ({basics['interactive']:,} interactive)"))
    print(ui.field("turns", f"{basics['total_turns']:,} · {basics['avg_turns']} avg per session"))
    print(ui.field("repos", f"{made['repos']} · {made['days_active']} days with activity"))
    print(ui.field("pace", f"{basics['total'] / span:.1f} sessions/day over {span} days"))
    print(ui.field("range", f"{basics['oldest']} → {basics['newest']}"))
    print()

    print(ui.heading("What it produced", ui.MINT))
    print(ui.field("commits",
                   f"{made['commits']:,} recorded ({made['unique_commits']:,} distinct)"))
    print(ui.field("PRs", f"{made['prs']:,} recorded ({made['unique_prs']:,} distinct)"))
    print(ui.field("files", f"{made['files_created']:,} created · {made['files_edited']:,} edited"))
    print(ui.field("handoffs", f"{made['checkpoints']:,} checkpoints written"))
    print()

    if made["nano_aiu"]:
        sent = made["input_tokens"] + made["cache_read_tokens"]
        cache_share = f"{made['cache_read_tokens'] / sent * 100:.0f}%" if sent else "-"
        print(ui.heading("What it cost", ui.VIOLET))
        print(ui.field("credits", f"{ui.fmt_aiu(made['nano_aiu'])} AIU"))
        print(ui.field("tokens", f"{_thousands(made['input_tokens'])} in · "
                                 f"{_thousands(made['output_tokens'])} out · "
                                 f"{_thousands(made['reasoning_tokens'])} reasoning"))
        print(ui.field("cache", f"{_thousands(made['cache_read_tokens'])} read "
                                f"({cache_share} of tokens sent)"))
        print(ui.field("time", f"{made['model_hours']}h of model time"))
        print()

    if made["delegated_calls"] or made["compactions"]:
        print(ui.heading("How it was done", ui.AMBER))
        # Labels stay inside the 9-column gutter; a longer one pushes its
        # value right and breaks the alignment of the whole view.
        print(ui.field("agents", f"{made['delegated_calls']:,} sub-agent calls "
                                 f"over {made['delegated_tasks']:,} delegated tasks"))
        print(ui.field("context", f"{made['compactions']:,} re-summarisations"))
        print()

    if repos:
        print(ui.heading("Where it landed", ui.ACCENT, inner))
        peak = repos[0][1]
        span, gauge = _chart_spans(inner, 20)
        share = f"{'share':<{gauge}} " if gauge else ""
        print(f"    {ui.MUTED}{'refs':>4}  {'repository':<{span}} "
              f"{share}{'sessions':>8}{ui.RST}".rstrip())
        for repo, refs, sessions in repos:
            name = redact.one_line(repo.split("/")[-1] if "/" in repo else repo)
            print(f"    {ui.MINT}{refs:>4}{ui.RST}  {ui._fit(name, span):<{span}}"
                  f" {ui.bar(refs, peak, gauge, pad=True)} "
                  f"{ui.MUTED}{sessions:>8,}{ui.RST}")
        print()

    if basics.get("top_model") and basics["top_model"][1]:
        model, nano = basics["top_model"]
        model = redact.one_line(model)
        print(ui.field("model", f"{model} ({ui.fmt_aiu(nano)} AIU)"))
    if basics["busiest_day"]:
        day, count = basics["busiest_day"]
        print(ui.field("busiest", f"{day} ({count} sessions)"))
    print(ui.field("detail", "cs cost · cs agents · cs skills · cs repos"))
    print()




def cmd_timeline(days: int = 30, sort_by: str | None = None,
                 descending: bool | None = None) -> bool:
    return _page_report(
        "timeline",
        lambda column, down: _capture(lambda: _render_timeline(days, column, down)),
        sort_by, descending,
    )


def _render_timeline(days: int, column: str = "day",
                     descending: bool = False) -> None:
    conn = db.connect()
    rows = db.timeline(conn, days)
    conn.close()
    rows = _sort_report(rows, "timeline", column, descending)
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    print()
    print(ui.rule(inner, f"Working days · {_window_label(days)}"))
    print()
    if not rows:
        print(f"  {ui.MUTED}No sessions in range.{ui.RST}")
        print()
        return
    # The bar tracks turns, not sessions. Turns are the unit of work asked
    # for; sessions are the unit of *filing* it, and a day spent in one long
    # session would otherwise draw as the emptiest bar on the chart. When a
    # store has no per-turn timestamps at all the series is flat zero, and
    # the chart falls back to sessions rather than drawing nothing.
    by_turns = any(r[2] for r in rows)
    peak = max((r[2] if by_turns else r[1]) for r in rows) or 1
    # The shared table shape, like every other report. Credits are dropped
    # first because the AI-spend view reports them in far more detail anyway;
    # the bar absorbs what is left, because four numbers with nothing to
    # compare them against is a table, not a chart. 14 is the indent plus the
    # day column, which is the one thing every row must keep.
    spans = _fit_columns(inner - 2, 14,
                         [("credits", 9), ("turns", 7), ("sessions", 8)],
                         least=8, flex="share")
    spans["day"] = 12
    columns = [("day", "day", "<"), ("sessions", "sessions", ">"),
               ("turns", "turns", ">"), ("credits", "credits", ">")]
    shown = [spec for spec in columns if spans[spec[0]]]
    heads = " ".join(_cell(head, spans[key], align) for key, head, align in shown)
    heads += " " + _cell("share of turns" if by_turns else "share",
                         spans["share"])
    print(f"  {ui.MUTED}{_mark_column(heads, column, _TIMELINE_HEADS, descending)}{ui.RST}")
    print(f"  {ui.MUTED}{'─' * ui.cells(heads)}{ui.RST}")
    for day, sessions, turns, nano in rows:
        values = {
            "day": (_weekday(day), ui.MUTED),
            "sessions": (f"{sessions:,}", ui.PAPER),
            "turns": (f"{turns:,}", ui.PAPER),
            "credits": (ui.fmt_aiu(nano), ui.VIOLET),
        }
        line = "  " + " ".join(
            _cell(values[key][0], spans[key], align, values[key][1])
            for key, _head, align in shown
        )
        print(f"{line} "
              f"{ui.bar(turns if by_turns else sessions, peak, spans['share'], track=True)}")
    print()
    # Totals, because the question after "which day was busiest" is always
    # "and what did the window come to" — and adding a column of bars in your
    # head is exactly the arithmetic a report is meant to have already done.
    # Written through _note so it wraps rather than running off a narrow
    # window: it is the one line here whose length depends on the data.
    #
    # The spend total is decided by the data, not by whether the credits
    # column survived _fit_columns. A narrow window is a reason to drop a
    # column, not a reason to stop reporting what the window cost.
    active = len(rows)
    turns_total = sum(row[2] for row in rows)
    spend_total = sum(row[3] for row in rows)
    parts = [f"{active} working day{'' if active == 1 else 's'}",
             f"{sum(row[1] for row in rows):,} sessions",
             f"{turns_total:,} turns"]
    if spend_total:
        parts.append(f"{ui.fmt_aiu(spend_total)} AIU")
    if turns_total:
        rate = turns_total / active
        parts.append(f"{rate:.0f} turn{'' if rate < 1.5 else 's'} a day")
    _note(" · ".join(parts), inner)
    print()
    print(_sort_note("timeline", column, descending, inner))
    print()


def _bar(value: float, peak: float, width: int = 24, colour: str = "") -> str:
    """The spend bars: exactly `width` cells, filled part then dim track.

    Fixed-width by construction rather than by an f-string pad, because a
    coloured bar is mostly escape characters and `:<18` counts those — the
    padding silently stopped happening the moment these bars gained a colour.

    Each spend section keeps its own hue instead of the length ramp: the
    sections are the thing being told apart here, and the number to the left
    of the bar is already the magnitude.
    """
    return ui.bar(value, peak, width, colour=colour, track=True)


# One shape for every spend breakdown: amount, name, share, then the numbers
# in fixed columns. The rows used to end in free-form text, so a call count
# gaining a digit pushed everything after it out of line.
_SPEND_NAME = 22
_SPEND_BAR = 18

# How many days the Per day section draws. It is a summary inside a report
# with four other sections, not the per-day view — `cs timeline <days>` is
# that, and it is uncapped. What matters is that the cut happens after the
# sort, so this is the top fourteen by whatever the report is sorted on
# rather than a fortnight the sort was never allowed to leave.
_COST_DAYS = 14


def _spend_spans(inner: int, note: int) -> tuple[int, int]:
    """(name, bar) for a spend section, given what its numbers need.

    The name and the bar are the only two elastic things in the row, and the
    bar yields first: which model spent it is the answer, the bar is how the
    answer looks. Below about fifty columns there is no bar at all, which is
    the same trade every other table here makes.

    Fixed cost is 4 indent + 8 amount + three 2-column gaps + the note.
    """
    spare = inner - 18 - note
    name = max(10, min(_SPEND_NAME, spare - 8))
    gauge = spare - name
    return name, gauge if gauge >= 6 else 0


def _spend_row(amount: str, name: str, bar: str, note: str,
               span: int = _SPEND_NAME) -> str:
    # `name` is a model or a repository, both recorded from the environment
    # rather than typed here, so it is stripped before it is measured into
    # the column — every spend section shares this row, and so shares the
    # guarantee. `bar` arrives already the width of its column: see _bar.
    name = redact.one_line(name)
    gap = f"  {bar}" if bar else ""
    return (
        f"    {ui.VIOLET}{amount:>8}{ui.RST}  "
        f"{ui._fit(name, span):<{span}}"
        f"{gap}  {ui.DIM}{note}{ui.RST}"
    )


def _spend_header(name: str, note: str, span: int = _SPEND_NAME,
                  gauge: int = _SPEND_BAR) -> str:
    """Name the columns once, so the numbers under them need no explaining."""
    share = f"  {'share':<{gauge}}" if gauge else ""
    return (
        f"    {ui.MUTED}{'spend':>8}  {name:<{span}}{share}  {note}{ui.RST}"
    )


def cmd_cost(days: int = 30, sort_by: str | None = None,
             descending: bool | None = None) -> bool:
    return _page_report(
        "cost",
        lambda column, down: _capture(lambda: _render_cost(days, column, down)),
        sort_by, descending,
    )


def _render_cost(days: int = 30, column: str = "spend",
                 descending: bool = True) -> None:
    conn = db.connect()
    if not db.has_usage(conn):
        conn.close()
        print(
            "error: this session store records no AI usage "
            "(no assistant_usage_events table)",
            file=sys.stderr,
        )
        sys.exit(1)
    totals = db.cost_totals(conn, days)
    by_model = db.cost_by_model(conn, days)
    by_repo = db.cost_by_repo(conn, days)
    by_day = db.cost_by_day(conn, days)
    top = db.cost_top_sessions(conn, days)
    conn.close()

    # Every section shares a shape — spend, a name, a count — so one choice
    # sorts all four the same way.
    by_model = _sort_cost(by_model, "model", column, descending)
    by_repo = _sort_cost(by_repo, "repo", column, descending)
    top = _sort_cost(top, "session", column, descending)
    # Per day is sorted *before* it is cut, which it was not. Trimming to the
    # trailing fortnight first meant the sort only ever reordered the last
    # fourteen days: the header said "sorted by spend ↓" and the dearest day
    # on record could not appear under it — not in a year, not in all time,
    # because it was dropped before the sort ever saw it. Every window wider
    # than about a fortnight showed the same fortnight.
    recorded = len(by_day)
    by_day = _sort_cost(by_day, "day", column, descending)[:_COST_DAYS]

    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    print()
    print(ui.rule(inner, f"AI spend · {_window_label(days)}"))
    print()
    if not totals.get("calls"):
        print(f"  {ui.DIM}No AI usage recorded in this range.{ui.RST}")
        print()
        return

    # Cache reads are counted separately from input tokens, so the ratio is
    # over what was sent in total.
    cache = totals["cache_read_tokens"]
    sent = totals["input_tokens"] + cache
    cache_pct = f"{cache / sent * 100:.0f}%" if sent else "-"
    seconds = totals["duration_ms"] / 1000
    avg_call = seconds / totals["calls"] if totals["calls"] else 0

    print(ui.heading("Totals", ui.ACCENT, inner))
    print(ui.field("spend", f"{ui.VIOLET}{ui.fmt_aiu(totals['nano_aiu'])} AIU{ui.RST}"))
    print(ui.field("calls", f"{totals['calls']:,} across {totals['sessions']:,} sessions"))
    print(ui.field("tokens", f"{_thousands(totals['input_tokens'])} in · "
                             f"{_thousands(totals['output_tokens'])} out · "
                             f"{_thousands(totals['reasoning_tokens'])} reasoning"))
    print(ui.field("cache", f"{_thousands(cache)} read ({cache_pct} of tokens sent)"))
    print(ui.field("time", f"{seconds / 3600:.1f}h of model time · {avg_call:.1f}s per call"))
    # No `issues` row. It read "0 errors · 7 filtered" — thirteen events out of
    # thirty-nine thousand, on a page about where three hundred thousand
    # credits went, and a content filter is not a spend fact at all. Every
    # other line in this block is a quantity you can act on. `cs efficiency`
    # already breaks the same two numbers out by finish reason, under "Calls
    # that ended badly", which is the page whose question they answer.
    print()

    # The timing columns are the first thing a narrow window loses: which
    # model, and what it cost, is what this section is for — how long its
    # calls took is a second question, and `cs stats` also answers it.
    print(ui.heading("By model", ui.VIOLET, inner))
    timings = inner >= 62
    span, gauge = _spend_spans(inner, 23 if timings else 9)
    print(_spend_header("model",
                        f"{'calls':>9}{'avg':>7}{'first':>7}" if timings
                        else f"{'calls':>9}", span, gauge))
    peak = max((m[2] for m in by_model), default=0)
    for model, calls, nano, avg_ms, ttft_ms in by_model:
        avg = f"{(avg_ms or 0) / 1000:.1f}s"
        first = f"{ttft_ms / 1000:.1f}s" if ttft_ms else "-"
        print(_spend_row(
            ui.fmt_aiu(nano), model, _bar(nano, peak, gauge, ui.SKY),
            f"{calls:>9,}{avg:>7}{first:>7}" if timings else f"{calls:>9,}",
            span,
        ))
    print()

    print(ui.heading("By repository", ui.MINT, inner))
    span, gauge = _spend_spans(inner, 9)
    print(_spend_header("repository", f"{'sessions':>9}", span, gauge))
    peak = max((r[2] for r in by_repo), default=0)
    for place, sessions, nano in by_repo:
        name = place.split("/")[-1] if "/" in place else place
        print(_spend_row(
            ui.fmt_aiu(nano), name, _bar(nano, peak, gauge, ui.MINT),
            f"{sessions:>9,}", span,
        ))
    print()

    print(ui.heading("Per day" if len(by_day) == recorded
                     else f"Per day · {len(by_day)} of {recorded}",
                     ui.ACCENT, inner))
    span, gauge = _spend_spans(inner, 9)
    print(_spend_header("day", f"{'calls':>9}", span, gauge))
    peak = max((d[1] for d in by_day), default=0)
    for day, nano, calls in by_day:
        print(_spend_row(
            ui.fmt_aiu(nano), _weekday(day), _bar(nano, peak, gauge, ui.VIOLET),
            f"{calls:>9,}", span,
        ))
    print()

    print(ui.heading("Dearest sessions", ui.VIOLET, inner))
    peak = max((t[2] for t in top), default=0)
    gauge = max(0, min(_SPEND_BAR, inner - 34))
    for _sid, summary, nano in top:
        # The summary is the one field with no natural width, so it goes last
        # and the columns before it stay put.
        share = f"{_bar(nano, peak, gauge, ui.SKY)}  " if gauge else ""
        print(f"    {ui.VIOLET}{ui.fmt_aiu(nano):>8}{ui.RST}  {share}"
              f"{ui._fit(redact.one_line(redact.redact(summary)), max(10, inner - 16 - gauge))}")
    print()
    _note("Credits = AI units (AIU) spent · avg = per call · first = to "
          "first token. 'cs cost <days>' narrows the range; 'cs cost all' "
          "widens it. Per day is the top "
          f"{_COST_DAYS} for the sort in force — 'cs timeline <days>' lists "
          "every day.", inner)
    print(_sort_note("cost", column, descending, width - 4))
    print()


def cmd_efficiency(days: int = 30) -> bool:
    return _page(_capture(lambda: _render_efficiency(days)))


def _render_efficiency(days: int = 30) -> None:
    """Not what the work cost, but whether it had to cost that.

    `cs cost` is the bill: which model, which repository, which day. This is
    the verdict on it, and every section is a lever rather than a number to
    admire — the cache you are or aren't hitting, the rate multiplier you are
    paying, the latency you are waiting through, the reasoning you are buying.

    The readings are the ones the rest of the industry already agrees on
    (Claude Code's telemetry schema uses the same cache formula and the same
    p50/p95 latency split), so a number here means what it means anywhere
    else. Nothing is scored and nothing is graded: each section says what was
    measured and what it implies, and leaves the judgement to the reader.
    """
    conn = db.connect()
    if not db.has_usage(conn):
        conn.close()
        print(
            "error: this session store records no AI usage "
            "(no assistant_usage_events table)",
            file=sys.stderr,
        )
        sys.exit(1)
    reading = db.efficiency(conn, days)
    conn.close()

    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    print()
    print(ui.rule(inner, f"Efficiency · {_window_label(days)}"))
    print()
    if not reading.get("cache") and not reading.get("by_model"):
        print(f"  {ui.DIM}No AI usage recorded in this range.{ui.RST}")
        print()
        return
    if not reading.get("windowed"):
        _note("This store does not date its usage records, so every reading "
              "below covers all of time rather than the window asked for.", inner)
        print()

    cache = reading.get("cache") or {}
    if cache.get("hit_rate") is not None:
        rate = cache["hit_rate"]
        # Coloured by what the number means, not by how big it is: past about
        # half the input coming from cache a long session is behaving itself,
        # and under a quarter is the single cheapest thing left to fix.
        colour = ui.MINT if rate >= 0.5 else ui.AMBER if rate >= 0.25 else ui.ROSE
        print(ui.heading("Cache", ui.ACCENT, inner))
        print(ui.field("hit rate",
                       f"{colour}{rate * 100:.0f}%{ui.RST}  "
                       f"{ui.meter(rate, max(10, min(28, inner - 30)), colour)}"))
        print(ui.field("tokens", f"{_thousands(cache['read'])} read from cache · "
                                 f"{_thousands(cache['written'])} written to it · "
                                 f"{_thousands(cache['fresh'])} sent fresh"))
        _why("Cached input is the cheapest input there is, and the share of it "
              "is the biggest single lever on what a long session costs. Read = "
              "cache_read / (fresh + cache_read + cache_write), the same "
              "definition the published agent telemetry schemas use.", inner)
        print()

    multipliers = reading.get("multipliers") or []
    if len(multipliers) > 1:
        print(ui.heading("Rate multiplier", ui.VIOLET, inner))
        total = sum(nano for _rate, _calls, nano in multipliers) or 1
        span, gauge = _spend_spans(inner, 9)
        print(_spend_header("multiplier", f"{'calls':>9}", span, gauge))
        peak = max(nano for _rate, _calls, nano in multipliers)
        for rate, calls, nano in multipliers:
            colour = ui.ROSE if rate > 1 else ui.MINT
            print(_spend_row(
                ui.fmt_aiu(nano), f"{rate:g}× · {nano / total * 100:.0f}% of spend",
                _bar(nano, peak, gauge, colour), f"{calls:>9,}", span,
            ))
        _why("Every call is billed at a rate. A premium multiplier earning its "
              "keep on hard work is money well spent; the same multiplier on "
              "lookups and file reads is the most common way a bill runs away "
              "without anyone deciding that it should.", inner)
        print()

    latency = reading.get("latency") or {}
    if latency.get("p50") is not None:
        print(ui.heading("First token", ui.SKY, inner))
        p50, p95 = latency["p50"] / 1000, latency["p95"] / 1000
        print(ui.field("p50", f"{p50:.1f}s  {ui.DIM}half of calls answer faster{ui.RST}"))
        colour = ui.ROSE if p95 >= 15 else ui.AMBER if p95 >= 8 else ui.MINT
        print(ui.field("p95", f"{colour}{p95:.1f}s{ui.RST}  "
                              f"{ui.DIM}the slow tail, over {latency['calls']:,} "
                              f"calls{ui.RST}"))
        _why("The mean hides exactly the tail that makes a tool feel slow, so "
              "this is quoted the way latency is quoted everywhere else: the "
              "middle, and the bad end.", inner)
        print()

    if cache.get("reasoning_share") is not None and cache["reasoning"]:
        print(ui.heading("Reasoning", ui.INDIGO, inner))
        share = cache["reasoning_share"]
        print(ui.field("share", f"{share * 100:.0f}% of output tokens were "
                                f"reasoning ({_thousands(cache['reasoning'])})"))
        effort = reading.get("effort") or []
        if effort:
            print(ui.field("effort", " · ".join(
                f"{name} {count:,}" for name, count in effort[:5])))
        _why("Reasoning tokens are output tokens you pay for and never read. "
              "Worth it on genuinely hard work; on routine edits a lower effort "
              "setting buys the same answer for less.", inner)
        print()

    by_model = reading.get("by_model") or []
    if by_model:
        print(ui.heading("By model", ui.MINT, inner))
        timings = inner >= 62
        span, gauge = _spend_spans(inner, 21 if timings else 9)
        print(_spend_header("model",
                            f"{'calls':>9}{'cache':>6}{'first':>6}" if timings
                            else f"{'calls':>9}", span, gauge))
        peak = max(row[2] for row in by_model)
        for model, calls, nano, cached, offered, ttft in by_model:
            hit = f"{cached / offered * 100:.0f}%" if offered else "-"
            first = f"{ttft / 1000:.1f}s" if ttft else "-"
            print(_spend_row(
                ui.fmt_aiu(nano), model, _bar(nano, peak, gauge, ui.SKY),
                f"{calls:>9,}{hit:>6}{first:>6}" if timings else f"{calls:>9,}",
                span,
            ))
        print()

    finish = reading.get("finish") or []
    # 'tool_calls' and 'stop' are both a call that ended the way it meant to;
    # anything else is one that did not, and only that is worth a line.
    unhappy = [(name, count) for name, count in finish
               if name not in ("stop", "tool_calls", "(none)")]
    if unhappy:
        print(ui.heading("Calls that ended badly", ui.ROSE, inner))
        for name, count in unhappy:
            _item(f"{name}: {count:,}", inner, marker="·", colour=ui.ROSE)
        print()

    _why("Every reading here comes from the store's own usage records — "
         "nothing on this screen is inferred from the text of a turn.", inner)
    _note("cs efficiency <days> narrows the range · cs efficiency all "
          "widens it", inner)
    _why_hint(inner)
    print()


def _weekday(day: str) -> str:
    """'2026-08-04' → 'Tue 04 Aug' — a date you can read at a glance."""
    from datetime import date

    try:
        return date.fromisoformat(day).strftime("%a %d %b")
    except ValueError:
        return day


def _thousands(n: int | None) -> str:
    """Compact token counts: 3.0B, 1.2M, 45.3k, 900."""
    n = n or 0
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _home_items(period: int = 30,
                theme: str | None = None) -> list[tuple[str, str, str, object, str]]:
    """The landing screen's menu: icon, label, what it does, what to run, asks.

    Every action is callable, so choosing a row can never call something that
    isn't. `asks` says what the menu has to hand a row when it opens it:
    `"term"` for the search box, `"period"` for the views that count over a
    window, and `""` for the rest.

    `period` is the window those counting rows will use, and it is written
    into their own descriptions rather than left to a prompt — a row that
    already says "last 30 days" has nothing left to ask after you press
    Enter, which is what lets every row on this menu open in one keystroke.

    Actions return True when they already waited for the user (a pager, or a
    full-screen listing), which is what tells the caller not to ask twice.

    The icons are picked to be told apart at a glance rather than to be read:
    fifteen rows of identical text is a list you have to work down every time,
    and the shape of a row is what you actually remember about it.
    """
    window = _window_label(period)
    theme = theme or ui.theme_name()
    return [
        (ui.menu_icon("recent"), "Recent sessions",
         "browse, read and resume · last 7 days",
         lambda: cmd_recent(7), ""),
        (ui.menu_icon("all"), "All sessions",
         "every session ever recorded · scroll to browse",
         lambda: cmd_recent(0, show_all=True), ""),
        (ui.menu_icon("search"), "Search", "full text across every turn and checkpoint",
         cmd_search, "term"),
        (ui.menu_icon("repos"), "Repositories",
         "sessions grouped by repository", cmd_repos, ""),
        (ui.menu_icon("stats"), "Stats",
         f"commits, PRs, files and what they cost · {window}",
         cmd_stats, "period"),
        # Working days is commented off the menu, and `cs timeline` still
        # runs. It is the weakest row in Measure: Stats already carries the
        # window's totals and AI spend already carries the per-day bars, so
        # a third counting view mostly asks the room to hold one more shape
        # in its head. Its own comment used to argue that three numbers
        # side by side answer what one cannot — that is still true, and it
        # is still one keystroke away for anyone who wants it.
        # (ui.menu_icon("days"), "Working days",
        #  f"sessions, turns and spend per day · {window}",
        #  cmd_timeline, "period"),
        (ui.menu_icon("spend"), "AI spend",
         f"credits by model, repository and day · {window}",
         cmd_cost, "period"),
        (ui.menu_icon("efficiency"), "Efficiency",
         f"cache, rate multiplier, latency, reasoning · {window}",
         cmd_efficiency, "period"),
        (ui.menu_icon("delegation"), "Delegation",
         f"you vs the main agent vs sub-agents · {window}",
         cmd_agents, "period"),
        (ui.menu_icon("autonomy"), "Autonomy",
         "which sessions ran unattended · YOLO", cmd_yolo, ""),
        (ui.menu_icon("handoff"), "Handoffs",
         "work passed from one session to the next",
         cmd_handoff, ""),
        (ui.menu_icon("security"), "Security",
         "credentials found in session text", cmd_audit, ""),
        # Improve · commented out of the menu on request. The three commands
        # still work when typed (`cs coach`, `cs rhythm`, `cs context`) and
        # every test of them still runs — only the landing screen and
        # `cs help` stop offering them.
        #
        # Restoring the group means uncommenting these three rows *and* the
        # ("Practice", "Improve") anchor in _HOME_GROUP_STARTS. Those two
        # halves are checked against each other at startup, so putting back
        # one without the other fails loudly instead of quietly filing three
        # rows under the wrong heading.
        # (ui.menu_icon("practice"), "Practice",
        #  f"habits the record shows, worst first · {window}",
        #  cmd_coach, "period"),
        # (ui.menu_icon("rhythm"), "Rhythm",
        #  f"when the work actually happens · {window}",
        #  cmd_rhythm, "period"),
        # (ui.menu_icon("context"), "Context",
        #  "what this repo hands the agent before you type",
        #  cmd_context, ""),
        (ui.menu_icon("skills"), "Skills", "what Copilot can load here versus used",
         lambda: cmd_assets("skills"), ""),
        (ui.menu_icon("profiles"), "Agents",
         "the same, for the agents you have defined",
         lambda: cmd_assets("agents"), ""),
        (ui.menu_icon("instructions"), "Instructions",
         "what every session here is told before you type",
         cmd_instructions, ""),
        # Hooks is configuration rather than history, which is why it was once
        # taken off this menu — `copilot plugins list --json` enumerates the
        # same declarations first-hand. What that argument missed is the one
        # thing this view does that reading the config cannot: it resolves
        # every hook command against the disk and names the ones whose script
        # is gone. Copilot will still run those, and the shell will still
        # fail, and nothing else you own will tell you before it does.
        (ui.menu_icon("hooks"), "Hooks",
         "what Copilot runs around a session, and what's missing",
         cmd_hooks, ""),
        (ui.menu_icon("mcp"), "MCP servers",
         "tool sources wired up, and which were used",
         cmd_mcp, ""),
        (ui.menu_icon("theme"), "Theme",
         f"{ui.theme_label(theme)} · choose from {len(ui.THEMES)} palettes",
         ui.next_theme, "theme"),
        (ui.menu_icon("help"), "Help", "every command and every key",
         lambda: _page(_capture(cmd_help)), ""),
    ]


# What a window can be, and the key that picks it. Five is enough to cover
# "this sprint" through "everything" without becoming a form to fill in.
_PERIODS: tuple[tuple[str, int, str, str], ...] = (
    # key, days, what it says, what it says on a narrow window
    ("1", 7, "7 days", "7d"),
    ("2", 30, "30 days", "30d"),
    ("3", 90, "90 days", "90d"),
    ("4", 365, "a year", "1y"),
    ("5", 0, "all time", "all"),
)


def _step_period(current: int, delta: int) -> int:
    """The next window along, clamped at both ends.

    This replaced a modal picker that opened *after* you pressed Enter on a
    counting view and wanted an Enter of its own to dismiss — so six of the
    menu's rows took two keys to launch and the rest took one. Stepping the
    window on the menu costs nothing when the last one was right, which is
    almost always, and shows what you are about to get before you commit.

    Clamped rather than wrapped: ← at 7 days should stay at 7 days, not jump
    to all time.
    """
    windows = [days for _key, days, _label, _short in _PERIODS]
    at = windows.index(current) if current in windows else 1
    return windows[min(max(at + delta, 0), len(windows) - 1)]


# Where a new group starts, and what to call it. Nineteen destinations in one
# undifferentiated column is a list you read every time; five short lists are
# ones you learn the shape of — "governance is the third block" is a thing you
# remember, "Autonomy is the ninth row" is not.
#
# The headings are dropped whole only on a window with barely more rows than
# there are groups, where they would be most of what is on screen. Everywhere
# else they are paid for out of the wordmark, which knows how to be smaller.
#
# Govern and Improve were the pair worth being deliberate about, because both
# read like "things that are wrong". Govern is about a *session*: this one ran
# unattended, this one holds a credential, this one was handed off. You go
# there to find the row and deal with it. Improve was about *you*: habits
# across a month, the hours they happened in, and the setup they started from.
# Improve is commented out of the menu (see _home_items), so its anchor is
# commented out here to match — the two are checked against each other.
#
# Anchored to the label of the row that opens each group rather than to its
# index, because indices move every time a row is added, retired or brought
# back — and a stale index does not fail, it quietly files three rows under
# the wrong heading and nothing in the output looks broken. A label either
# matches a row or it does not, and _home_groups raises when it does not.
# What each group is drawn in. The menu was the one place in `cs` where a
# section heading had no accent: four grey words and a hairline so dark it
# was not there, over eighteen rows of grey label and dull blue. Every report
# opens on a coloured bar, and the screen you look at most often opened on
# nothing.
#
# The hues are the palette's own, and they carry the same meaning here as
# they do in a report rather than being picked to look busy: the product blue
# for finding things, violet for the counting views (it is what spend is
# drawn in), amber for the group that exists to tell you something is wrong,
# and mint for reference material that is simply there.
_HOME_GROUP_TONE = {
    "Find": "title",        # 39  — the product blue
    "Measure": "credits",   # 177 — violet, as spend is everywhere else
    "Govern": "warn",       # 214 — amber: this group is the bad news
    "Improve": "credits",
    "Reference": "active",  # 49  — mint: present, and nothing to answer for
}

_HOME_GROUP_STARTS: tuple[tuple[str, str], ...] = (
    ("Recent sessions", "Find"),
    ("Repositories", "Measure"),
    ("Autonomy", "Govern"),
    # ("Practice", "Improve"),
    ("Skills", "Reference"),
)


def _home_groups(items: list | None = None) -> dict[int, str]:
    """Group name by the index of the row that starts it.

    Worked out on demand rather than fixed at import, because the anchors are
    labels and the labels live in `_home_items`, which is defined in terms of
    helpers further down the module. Cached after the first call: the menu
    redraws on every keystroke and this is the same answer every time.

    Raises rather than guessing when an anchor names a row that is not on the
    menu: a heading with nothing under it and a group silently merged into
    the one above are both worse than a stack trace on the first frame.
    """
    if items is None and _HOME_GROUPS:
        return _HOME_GROUPS
    rows = _home_items() if items is None else items
    at = {label: index for index, (_icon, label, *_rest) in enumerate(rows)}
    if missing := [label for label, _ in _HOME_GROUP_STARTS if label not in at]:
        raise KeyError(
            f"_HOME_GROUP_STARTS names rows that are not on the menu: "
            f"{', '.join(missing)}"
        )
    groups = {at[label]: group for label, group in _HOME_GROUP_STARTS}
    if items is None:
        _HOME_GROUPS.update(groups)
    return groups


_HOME_GROUPS: dict[int, str] = {}


def _home_group(index: int) -> str:
    """Which group a menu row belongs to — the last one that started at or
    before it."""
    groups = _home_groups()
    return groups[max(start for start in groups if start <= index)]


def _home_layout(indices, grouped: bool) -> list[tuple[str, object]]:
    """The menu's drawn rows: ('head', title) and ('item', index), in order.

    Scrolling, clicking and the cursor all work off this rather than off the
    item list, so a heading is a row that takes space and cannot be chosen.

    `indices` is which items to draw, which is the whole menu until you start
    typing. A heading appears when its group does, so filtering down to two
    rows shows the two headings those rows belong under rather than all five.
    """
    rows: list[tuple[str, object]] = []
    seen = None
    for index in indices:
        if grouped and (group := _home_group(index)) != seen:
            rows.append(("head", group))
            seen = group
        rows.append(("item", index))
    return rows


def _home_step(shown: list[int], cursor: int, delta: int) -> int:
    """Move the cursor through the rows on screen, not through the whole menu.

    With a filter up, the options between two matches are not there to be
    stepped onto — moving by item index would land the cursor on a row that
    is not drawn.
    """
    if not shown:
        return cursor
    at = shown.index(cursor) if cursor in shown else 0
    return shown[min(max(at + delta, 0), len(shown) - 1)]


def _home_status(query: str, matched: int, total: int, width: int,
                 period: int = 30, theme: str = "dark",
                 refresh_error: bool = False, refreshed: str = "",
                 theme_error: bool = False) -> str:
    """The one hint line. It says what you can do, or what you have typed.

    One line rather than two: a key list above the menu and a second one
    below it were each telling half a story, and neither said that typing
    does anything.

    The window rides here because it is a setting, not an action — and
    because the rows it applies to now caption themselves with it, this line
    only has to say which key moves it.
    """
    if query:
        found = f"{matched} of {total}" if matched else "no match"
        line = f" find: {query}▏ {found} · Esc clears · ↵ opens "
        return line if ui.cells(line) <= width else " find: … · Esc · ↵ "
    if refresh_error:
        line = (f" refresh failed · retrying in {_REFRESH_SECONDS}s · "
                f"t theme · q quit ")
        return line if ui.cells(line) <= width else " refresh failed · q "
    short = next((s for _k, days, _l, s in _PERIODS if days == period), "30d")
    theme = ui.theme_label(theme)
    # A theme that could not be written is one you will have to pick again
    # next run. It rides alongside the theme hint rather than taking the
    # line the way a failed refresh does: the palette did apply, and the
    # note has to survive into the shorter forms to be worth saying at all.
    note = " · not saved" if theme_error else ""
    live = (f"updated {refreshed}" if refreshed
            else f"refresh {_REFRESH_SECONDS}s")
    for line in (
        f" ↑↓ move · ↵ open · ←→ window {short} · t theme {theme}{note} · "
        f"{live} · type to find · / search · q quit ",
        f" ↑↓ · ↵ open · ←→ {short} · t themes{note} · {live} · / search · q ",
        f" ↑↓ · ↵ · ←→ {short} · t themes{note} · {live} · q ",
        f" ↑↓ · ↵ · {live} · q ",
        " ↑↓ · ↵ · type · q ",
    ):
        if ui.cells(line) <= width:
            return line
    return " ↑↓ · ↵ · q "


def _home_matches(items, query: str) -> list[int]:
    """Which rows survive what has been typed. Empty query means all of them.

    Matched against the label and the description together: "spend" should
    find AI spend, and so should "credits", which is only in its description.
    """
    if not query:
        return list(range(len(items)))
    wanted = query.lower()
    return [
        index for index, item in enumerate(items)
        if wanted in f"{item[1]} {item[2]}".lower()
    ]


def _home_snapshot(days: int = 120) -> tuple[list[tuple[str, str]], list[int]]:
    """The current facts and activity strip, read in one database connection.

    Kept as pairs rather than one joined string so the numbers can be drawn
    apart from their labels: the counts are what the line is for, and in one
    flat colour they were the hardest part of it to pick out.
    """
    conn = db.connect()
    try:
        basics = db.stats(conn)
        skills = [name for name, _ in _asset_names("skills")]
        agents = [name for name, _ in _asset_names("agents")]
        skills_used = db.assets_used(conn, skills)
        agents_used = db.assets_used(conn, agents)
        subagents = sum(db.subagents_by_session(conn).values())
        series = db.activity(conn, days)
    finally:
        conn.close()
    # Two halves, and the line is only as long as it can be: what the store
    # holds, then what is wired up to work on it. 'interactive' used to sit
    # second and is gone — it is a distinction the listing already makes in
    # its own title, and on a 92-column window it was pushing the inventory
    # counts off the end of the row.
    #
    # The kit counts read 'used of installed' rather than a bare inventory.
    # A shelf of 125 skills says nothing about whether any of them are
    # earning their place; '9/125' says it immediately, and it is the number
    # worth looking at before writing the hundred and twenty-sixth. Sub-agents
    # are counted as runs, because the store bills those exactly and 'how
    # much did we actually delegate' is the question behind the row.
    nano_aiu = basics["total_nano_aiu"]
    # Precise credits come first so small live changes survive narrow windows.
    facts = [
        (f"{nano_aiu / 1e9:,.2f}" if nano_aiu > 0 else "-", "AIU"),
        (f"{basics['total']:,}", "sessions"),
        (f"{basics['total_turns']:,}", "turns"),
        (f"{basics['repos']}", "repos"),
        (f"{skills_used}/{len(skills)}", "skills used"),
        (f"{agents_used}/{len(agents)}", "agents used"),
        (f"{subagents:,}", "sub-agents run"),
        (f"{len(mcp.load()[0])}", "mcp"),
    ]
    return facts, series if any(series) else []


def _refresh_home(state: dict) -> bool:
    """Replace the landing readings from the live, read-only store."""
    try:
        state["facts"], state["activity"] = _home_snapshot()
    except (OSError, sqlite3.Error):
        state["refresh_error"] = True
        return False
    state["refreshed"] = time.strftime("%H:%M:%S")
    state.pop("refresh_error", None)
    return True


def _home_header_rows(width: int, height: int, menu_rows: int,
                      spark: bool = False) -> int:
    """How many rows _draw_home_header will take, without drawing it.

    The menu has to know whether it can afford its headings before it knows
    how tall the banner will be, and the banner's height depends on how many
    rows the menu wants — so the sum is worked out once, here, from the
    layout the caller is proposing.

    The activity line rides with the wordmark: a window too short for one is
    too short for the other, which keeps this arithmetic straight rather
    than circular.
    """
    art = _home_art(width, height, menu_rows, spark)
    # The wordmark (or the one-line mark that replaces it), the counts, and
    # the rule under them — plus the activity row when the wordmark earned
    # its place. The version rides on the counts and the keys ride on the
    # status bar, because a row holding one short string is a row the menu
    # could have had.
    return (len(art) or 1) + 2 + (1 if art and spark else 0)


def _home_plan(width: int, height: int, shown: list[int],
               spark: bool = False) -> tuple[list[tuple[str, object]], list[str]]:
    """The menu's rows and the wordmark they leave room for.

    One function so the loop and its tests cannot disagree about the trade.
    Two rules, in this order:

    1. **The menu is grouped.** Five captioned blocks are what makes nineteen
       destinations navigable; an undivided column is a list you re-read
       every time. This used to be the other way round — headings happened
       only if they were free — and on any window under about forty rows
       they never were, so nobody ever saw them.
    2. **The wordmark pays for them.** It shrinks 7 rows to 4 to 1 as the
       window does, which is a thing it already knows how to do. The
       headings only go on a window with barely more menu rows than there
       are groups, where captions would be most of what is on screen.

    The menu scrolls, so a heading never puts an option out of reach — it
    costs a little scrolling at worst.
    """
    heads = len({_home_group(index) for index in shown})
    wanted = len(shown) + heads
    room = height - 1 - _home_header_rows(width, height, wanted, spark)
    layout = _home_layout(shown, room >= heads * 3)
    return layout, _home_art(width, height, len(layout), spark)


def _home_art(width: int, height: int, menu_rows: int,
              spark: bool = False) -> list[str]:
    """The wordmark a menu of this many rows leaves room for — [] for none.

    Asked separately from the row count because the wordmark's size and the
    shape of the menu are one decision, and it has to be answerable before
    either is drawn.
    """
    spare = height - 3 - menu_rows - (1 if spark else 0)
    return ui.banner(width, spare) if spare >= 4 else []


def _draw_home_header(screen, theme, width: int, art: list[str],
                      facts: list[tuple[str, str]],
                      sweep: list[int] | None = None,
                      reveal: int | None = None, start: int = 0,
                      activity: list[int] | None = None,
                      pace: int | None = None) -> int:
    """Draw the banner and the facts above the menu. Returns the first menu row.

    `art` is the wordmark _home_plan chose — passed in rather than worked out
    again here, because the size it can be and the shape of the menu are one
    decision, and two copies of it would eventually disagree.

    `sweep` is the purple→cyan ramp, when the terminal has 256 colours; the
    wordmark is coloured by *position* along it, so it matches the splash
    rather than being the same art in a flat blue. `reveal` is how many
    frame of the wipe to draw — and None once it is done.

    `activity` is sessions per day, oldest first. It is drawn as a sparkline
    under the counts and fills in on the same wipe, so what arrives is the
    store's own shape rather than an effect: the screen is telling you how
    the last few months went while it opens.

    `pace` is which frame of the agent's walk to draw on the rule, and None
    when it should not be drawn at all — during the wipe, which has the
    screen to itself, and on a terminal that cannot time a keypress.
    """
    # How far the wipe has to travel. The last row starts latest, so the span
    # includes its lag — otherwise the slant would still be finishing after
    # the frames had run out. The activity strip counts as one more row, so
    # the light carries on down the screen instead of stopping at the letters.
    widest = max((len(line) for line in art), default=0)
    trailing = max(len(art) - 1, 0) + (1 if art and activity else 0)
    span = widest + ui.REVEAL_LAG * trailing
    swept = span if reveal is None else ui.reveal_columns(reveal, span)
    row = start
    for index, line in enumerate(art):
        left = max((width - len(line)) // 2, 0)
        edge = swept - index * ui.REVEAL_LAG
        shown = line if reveal is None else line[:max(edge, 0)]
        if sweep:
            for column, run, colour in ui.gradient_runs(line, len(sweep)):
                # The bands are measured on the whole line so a letter keeps
                # its colour as the wipe passes it, rather than sliding
                # through the ramp on its way in.
                if column >= len(shown):
                    break
                _addstr(screen, row, left + column, run[:len(shown) - column],
                        width, sweep[colour])
        else:
            _addstr(screen, row, left, shown, width, theme["title"])
        row += 1
    if not art:
        _addstr(screen, row, 0,
                f"◆  cs · Copilot sessions browser · v{__version__}",
                width, theme["title"])
        row += 1

    # Counts in the bright colour, what they count in the dim one: the numbers
    # are the reason the line is there. The version sits at the far right of
    # the same row: it is worth having on screen and not worth a row.
    stamp = f"v{__version__}" if art else ""
    if stamp:
        _addstr(screen, row, max(width - len(stamp) - 2, 0), stamp, width,
                theme["help"])
    # The counts stop before the version rather than at the edge of the
    # window: they shared a row with it and ran straight through it, so the
    # last count on a narrow terminal read as '48 agentsv·.2.mc'.
    edge = width - len(stamp) - 3 if stamp else width
    column = 2
    for index, (value, label) in enumerate(facts):
        if column + len(value) + len(label) + 3 > edge:
            break
        if index:
            _addstr(screen, row, column, "·", width, theme["separator"])
            column += 2
        _addstr(screen, row, column, value, width, theme["credits"])
        column += len(value) + 1
        _addstr(screen, row, column, label, width, theme["repo"])
        column += len(label) + 1
    row += 1
    if art and activity:
        row = _draw_home_activity(screen, theme, width, row, activity, sweep,
                                  swept - len(art) * ui.REVEAL_LAG, widest)
    _addstr(screen, row, 0, "─" * width, width, theme["separator"])
    if pace is not None:
        # Drawn over the rule rather than on a row of its own: a line that
        # was already there costs nothing, and the walk reads as following
        # the divider instead of floating above the menu.
        icon = ui.menu_icon("copilot")
        at = ui.pace_column(pace, max(width - ui.cells(icon), 1))
        _addstr(screen, row, at, icon, ui.cells(icon), theme["title"])
    return row + 1


def _draw_home_activity(screen, theme, width: int, row: int,
                        activity: list[int], sweep: list[int] | None,
                        edge: int, full: int) -> int:
    """One row of sessions-per-day, right-aligned so today is the last cell.

    Right-aligned because the newest day is the one you look for, and it
    should not move when the window is resized. Coloured along the same ramp
    as the wordmark, so the oldest day is purple and today is cyan.
    """
    label, tail = "  activity ", f" {len(activity)} days"
    room = width - len(label) - len(tail) - 1
    if room < 12:
        return row
    series = activity[-room:]
    spark = ui.sparkline(series)
    if not spark.strip():
        return row          # nothing recorded: an empty row says less than none
    # The one wipe drives both, so they finish together whatever their
    # widths — the strip is a fraction of the wordmark's travel, not a
    # column count of its own.
    if full and edge < full:
        spark = spark[:max(0, round(len(spark) * edge / full))]
    _addstr(screen, row, 0, label, width, theme["repo"])
    if sweep:
        for column, run, colour in ui.gradient_runs(spark, len(sweep)):
            _addstr(screen, row, len(label) + column, run, width, sweep[colour])
    else:
        _addstr(screen, row, len(label), spark, width, theme["turns"])
    if len(spark) == len(series):
        _addstr(screen, row, len(label) + len(spark), tail, width,
                theme["repo"])
    return row + 1


def _theme_picker(screen, current: str) -> str:
    """Preview and choose one of the built-in themes."""
    import curses

    themes = list(ui.THEMES)
    cursor = themes.index(current)
    last_click = [0.0, -1]
    pending: list[int] = []
    offset = 0
    screen.timeout(-1)
    while True:
        selected = themes[cursor]
        palette = ui.tui_theme(curses, selected)
        try:
            screen.bkgd(" ", palette["background"])
        except curses.error:
            pass
        screen.erase()
        height, width = screen.getmaxyx()
        panel_width = min(width, 100)
        left = max((width - panel_width) // 2, 0)
        box_height = min(len(themes) + 4, height)
        top = max((height - box_height) // 2, 0)
        _addstr(screen, top, left, " Choose theme", panel_width, palette["title"])
        _addstr(
            screen, top + 1, left,
            f" {len(themes)} curated palettes · hover or arrows preview instantly",
            panel_width, palette["help"],
        )
        visible = max(box_height - 4, 1)
        if cursor < offset:
            offset = cursor
        elif cursor >= offset + visible:
            offset = cursor - visible + 1
        offset = min(offset, max(len(themes) - visible, 0))
        for row, index in enumerate(
            range(offset, min(offset + visible, len(themes))), top + 2
        ):
            name = themes[index]
            on_cursor = index == cursor
            style = palette["cursor"] if on_cursor else palette["summary"]
            if on_cursor:
                _addstr(screen, row, left, " " * panel_width, panel_width, style)
            marker = ">" if on_cursor else ("*" if name == current else " ")
            _addstr(screen, row, left + 1, marker, 1,
                    palette["cursor"] if on_cursor else palette["active"])
            _addstr(
                screen, row, left + 3, f"{ui.theme_label(name):<18}", 18, style
            )
            if panel_width > 38:
                _addstr(
                    screen, row, left + 23, ui.theme_description(name),
                    panel_width - 24,
                    style if on_cursor else palette["repo"],
                )
        footer = min(top + box_height - 1, height - 1)
        hint = " hover/click preview · double-click/Enter apply & return · Esc back "
        if ui.cells(hint) > panel_width:
            hint = " ↑↓ preview · Enter apply · Esc back "
        _addstr(
            screen, footer, left,
            hint,
            panel_width, palette["status"],
        )
        screen.refresh()
        try:
            key = pending.pop(0) if pending else screen.getch()
        except KeyboardInterrupt:
            return current
        event = _mouse_event(screen, curses, key, last_click, pending)
        if event:
            kind, x, y = event
            start = top + 2
            if (kind in ("move", "click", "double")
                    and left <= x < left + panel_width
                    and start <= y < start + visible):
                hovered = offset + y - start
                if hovered < len(themes):
                    cursor = hovered
                    if kind == "double":
                        return themes[cursor]
            elif kind == "wheel-up":
                cursor = max(cursor - 1, 0)
            elif kind == "wheel-down":
                cursor = min(cursor + 1, len(themes) - 1)
            continue
        if key in (27, ord("q"), ord("Q")):
            return current
        if key in (10, 13, curses.KEY_ENTER):
            return selected
        if key == curses.KEY_UP:
            cursor = max(cursor - 1, 0)
        elif key == curses.KEY_DOWN:
            cursor = min(cursor + 1, len(themes) - 1)
        elif key == curses.KEY_HOME:
            cursor = 0
        elif key == curses.KEY_END:
            cursor = len(themes) - 1


def _home_tui(screen, state: dict):
    """Draw the menu. Returns an item index, ('search', term), or None to quit."""
    import curses

    screen.keypad(True)
    active_theme = ui.set_theme(state.get("theme", ui.theme_name()))
    state["theme"] = active_theme
    items = _home_items(state.get("period", 30), active_theme)
    theme = ui.tui_theme(curses)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    try:
        screen.bkgd(" ", theme["background"])
    except curses.error:
        pass
    mouse = _enable_mouse(curses, motion=True)
    sweep = ui.banner_palette(curses)
    cursor = state.get("cursor", 0)
    offset = state.get("offset", 0)
    query = ""
    last_click = [0.0, -1]
    pending: list[int] = []
    next_refresh = state.get(
        "next_refresh", time.monotonic() + _REFRESH_SECONDS
    )

    def wait(milliseconds: int) -> bool:
        """Ask for a timed getch. False when this window cannot do one."""
        try:
            screen.timeout(milliseconds)
        except (AttributeError, curses.error):
            return False
        return True

    def wait_until(milliseconds: int) -> bool:
        remaining = max(1, round((next_refresh - time.monotonic()) * 1000))
        return wait(min(milliseconds, remaining))

    # Once per run, not once per visit: replaying the wipe every time a view
    # hands you back would turn a greeting into a stutter.
    timed = wait_until(ui.REVEAL_MS)
    reveal = None if state.get("revealed") or not timed else 0
    # The agent's walk. None on a window that cannot time a keypress, where
    # asking for one would block and the screen would simply never redraw.
    # `rested` is how many idle frames it has spent pacing: it stops at
    # ui.PACE_FRAMES while the refresh heartbeat keeps running.
    pace = 0 if timed else None
    rested = 0

    def settle() -> None:
        """End the wipe — because it finished, or because a key arrived."""
        nonlocal reveal
        if reveal is not None:
            reveal = None
            state["revealed"] = True

    def activate_theme(name: str) -> None:
        nonlocal active_theme, theme, sweep
        chosen = ui.set_theme(name)
        # Written only when the answer actually changed: entering the menu
        # re-applies what is already stored, and Esc out of the gallery
        # hands back the theme you came in with.
        if chosen != active_theme:
            state["theme_error"] = not ui.save_theme(chosen)
        active_theme = chosen
        state["theme"] = active_theme
        theme = ui.tui_theme(curses)
        sweep = ui.banner_palette(curses)
        try:
            screen.bkgd(" ", theme["background"])
        except curses.error:
            pass

    def open_item(index: int, height: int, width: int):
        """What to hand back for a chosen row. None means stay on the menu.

        **Enter opens the row. Every row, one press.** The counting views
        used to answer a second question first — which window to count over —
        so six of the nineteen entries took two Enters to launch and the
        other thirteen took one. The window is chosen on the menu now, with
        ←/→, and it is on screen before you commit to anything.

        Search is the one thing still asked for here, because a search with
        no term is not a view that can be opened at all.
        """
        asks = items[index][4]
        if not asks:
            return index
        if asks == "theme":
            activate_theme(_theme_picker(screen, active_theme))
            return None
        if asks == "term":
            term = _prompt(screen, theme, height - 1, width, " search: ", "")
            return (index, term) if term else None
        return (index, state.get("period", 30))

    try:
        while True:
            if time.monotonic() >= next_refresh:
                _refresh_home(state)
                next_refresh += _REFRESH_SECONDS
                if next_refresh <= time.monotonic():
                    next_refresh = time.monotonic() + _REFRESH_SECONDS
            screen.erase()
            height, width = screen.getmaxyx()
            # Rebuilt each frame because the counting rows caption themselves
            # with the window they will use, and ←/→ changes it under you.
            items = _home_items(state.get("period", 30), active_theme)
            shown = _home_matches(items, query)
            if shown and cursor not in shown:
                cursor = shown[0]
            cursor = min(max(cursor, 0), len(items) - 1)
            # Headings pay for themselves out of the wordmark, not out of the
            # menu. Eighteen options in one undivided column is a list you
            # have to read every time; four rows of ASCII art is decoration,
            # and the banner already knows how to be smaller.
            activity = state.get("activity") or None
            layout, art = _home_plan(width, height, shown, bool(activity))
            # On a tall window everything sat at the top with ten empty rows
            # under it and the status bar stranded below them. The slack is
            # split, so the screen has a margin rather than a hole. It is
            # taken after the wordmark has been chosen, and only out of rows
            # nothing else wanted, so it can never push the menu off.
            slack = height - 1 - _home_header_rows(
                width, height, len(layout), bool(activity))
            pad = (slack - len(layout)) // 2 if slack - len(layout) >= 6 else 0
            top = _draw_home_header(screen, theme, width, art,
                                    state.get("facts", []), sweep, reveal, pad,
                                    activity,
                                    None if reveal is not None else pace)

            # The menu scrolls rather than spilling off a short window, so
            # every option stays reachable however small the terminal is.
            visible = max(height - top - 1, 1)
            place = next((i for i, (kind, value) in enumerate(layout)
                          if kind == "item" and value == cursor), 0)
            if place < offset:
                offset = place
            elif place >= offset + visible:
                offset = place - visible + 1
            offset = min(max(offset, 0), max(len(layout) - visible, 0))
            # During the wipe the menu arrives a few rows at a time under it.
            # It is a cap on what is *drawn*, never on what exists: the
            # layout, the scroll offset and the cursor are all computed over
            # the whole menu, so a key pressed mid-cascade lands exactly where
            # it would have on the finished screen.
            drawn = (len(layout) if reveal is None
                     else ui.reveal_rows(reveal, len(layout)))
            for line, row in enumerate(layout[offset:offset + visible], top):
                if line - top >= drawn:
                    break
                kind, value = row
                if kind == "head":
                    # `▌CAPTION ─────`, the shape ui.heading draws in every
                    # report, in the group's own hue — so the menu and the
                    # page it opens are visibly the same product.
                    tone = theme[_HOME_GROUP_TONE.get(value, "header")]
                    _addstr(screen, line, 1, "▌", 1, tone)
                    _addstr(screen, line, 2, value.upper(), width, tone)
                    # A hairline from the caption to the right edge. It used
                    # to stop at column 24, under the labels, which read as an
                    # underline on the word rather than as the top of a block.
                    rule = width - 4 - len(value)
                    if rule > 2:
                        _addstr(screen, line, 3 + len(value), " " + "─" * (rule - 1),
                                width, theme["separator"])
                    continue
                index = value
                icon, label, description = items[index][:3]
                on_cursor = index == cursor
                style = theme["cursor"] if on_cursor else None
                if on_cursor:
                    # The bar starts at column 1 so the marker sits outside it
                    # and reads as a pointer rather than as part of the fill.
                    _addstr(screen, line, 1, " " * (width - 1), width - 1,
                            theme["cursor"])
                    _addstr(screen, line, 0, "▌", 1, theme["title"])
                # Every column after the icon is placed absolutely, so a
                # terminal that draws an emoji one cell wide rather than two
                # shifts nothing: it just leaves a slightly wider gap.
                _addstr(screen, line, 3, icon, 2, style or theme["summary"])
                _addstr(screen, line, 6, f"{ui.trunc(label, 16):<16}", 16,
                        style or theme["label"])
                if width > 45:
                    _addstr(screen, line, 24, description, width - 25,
                            style or theme["repo"])
            if not shown:
                _addstr(screen, top, 3, f"nothing matches '{query}'", width,
                        theme["repo"])

            _addstr(screen, height - 1, 0,
                    _home_status(query, len(shown), len(items), width,
                                 state.get("period", 30), active_theme,
                                 state.get("refresh_error", False),
                                 state.get("refreshed", ""),
                                 state.get("theme_error", False)),
                    width, theme["status"])
            screen.refresh()

            # Input handlers can reset curses to blocking mode. Own the
            # timeout at the read boundary, with a one-second idle heartbeat.
            if timed:
                wait_until(
                    ui.REVEAL_MS if reveal is not None else
                    ui.PACE_MS if rested < ui.PACE_FRAMES else 1000
                )
            try:
                key = pending.pop(0) if pending else screen.getch()
            except KeyboardInterrupt:
                return None
            if key == -1:
                # Animation tick or idle heartbeat, never a keypress.
                if reveal is not None:
                    reveal += 1
                    if reveal >= ui.REVEAL_FRAMES:
                        settle()
                    continue
                if pace is not None and rested < ui.PACE_FRAMES:
                    pace += 1
                    rested += 1
                continue
            settle()  # any key at all lands you on the finished screen
            event = _mouse_event(screen, curses, key, last_click, pending)
            rested = 0
            if event:
                kind, _mx, my = event
                if kind in ("wheel-up", "wheel-down"):
                    cursor = _home_step(shown, cursor,
                                        -1 if kind == "wheel-up" else 1)
                elif (kind in ("move", "click", "double")
                      and top <= my < top + visible
                      and offset + my - top < len(layout)):
                    row = layout[offset + my - top]
                    if row[0] == "item":
                        cursor = row[1]
                        if kind == "double":
                            chosen = open_item(cursor, height, width)
                            if chosen is not None:
                                return chosen
                continue
            if key == 27:
                # Esc clears what you typed before it quits, the same as it
                # does in the listing — one key that always means "back one
                # step" rather than one that sometimes throws the screen away.
                if query:
                    query = ""
                    continue
                return None
            if key in (curses.KEY_BACKSPACE, 127, 8):
                query = query[:-1]
            elif key in (10, 13, curses.KEY_ENTER):
                if not shown:
                    continue
                chosen = open_item(cursor, height, width)
                if chosen is not None:
                    return chosen
            elif key == curses.KEY_UP:
                cursor = _home_step(shown, cursor, -1)
            elif key == curses.KEY_DOWN:
                cursor = _home_step(shown, cursor, 1)
            elif key in (curses.KEY_LEFT, curses.KEY_RIGHT):
                # The same keys that step a column in the listing and the
                # reader, stepping the window here. They are free on this
                # screen and — unlike a letter — they cannot be a filter
                # character, which is what rules out '1'-'5' and 'w'.
                state["period"] = _step_period(
                    state.get("period", 30), 1 if key == curses.KEY_RIGHT else -1
                )
            elif key in (ord("t"), ord("T")) and not query:
                activate_theme(_theme_picker(screen, active_theme))
            elif key == curses.KEY_HOME:
                cursor = shown[0] if shown else cursor
            elif key == curses.KEY_END:
                cursor = shown[-1] if shown else cursor
            elif key == ord("/"):
                search = next(i for i, item in enumerate(items)
                              if item[4] == "term")
                chosen = open_item(search, height, width)
                if chosen is not None:
                    return chosen
            elif key in (ord("q"), ord("Q")) and not query:
                return None
            elif 32 <= key < 127:
                # Typing narrows the menu. This is what replaced the column
                # of numbers: they only ever reached the first nine rows, and
                # the menu has twice that. It also means every letter belongs
                # to the filter, so 'q' quits only when nothing is typed and
                # Esc is what clears it.
                query += chr(key)
    finally:
        state["cursor"] = cursor
        state["offset"] = offset
        state["next_refresh"] = next_refresh
        wait(-1)  # a view opened from here reads keys of its own
        if mouse:
            _disable_mouse()


def cmd_home() -> None:
    """The landing screen. Every view is one key away, and returns here."""
    import curses

    global _HOME_ACTIVE

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        conn = db.connect()
        basics = db.stats(conn)
        conn.close()
        ui.render_splash(basics)
        cmd_recent(1)
        return

    facts, activity = _home_snapshot()
    state = {
        "facts": facts,
        "activity": activity,
        "theme": ui.theme_name(),
        "refreshed": time.strftime("%H:%M:%S"),
    }
    _HOME_ACTIVE = True
    try:
        while True:
            try:
                choice = _curses_wrapper(_home_tui, state)
            except KeyboardInterrupt:
                return
            except curses.error:
                # No usable terminal for a menu — the printed help still works.
                cmd_help()
                return
            finally:
                _disable_mouse()

            if choice is None:
                return
            index, given = choice if isinstance(choice, tuple) else (choice, None)
            *_naming, action, asks = _home_items()[index]
            if asks and given is None:
                continue  # nothing chosen — straight back to the menu
            try:
                # `is None`, not falsy: 0 is a window, and it means all time.
                waited = action(given) if asks else action()
            except SystemExit:
                # A view that has nothing to show exits the process when run
                # as a one-shot command. From the menu that would take the
                # whole app down, so it is just a message to read.
                waited = False
            if not waited and not _pause("Esc or Enter for the menu · q quits "):
                return
    finally:
        _HOME_ACTIVE = False


def cmd_help() -> None:
    # The Improve section is commented out of this listing on request. It sat
    # between the hooks line and Govern, and read:
    #
    #   {ui.BOLD}Improve{ui.RST}
    #     cs coach [N|all]      Habits the record shows, scored and worst first
    #     cs rhythm [N|all]     When the work happens: hours, days, streaks
    #     cs context            What this repo hands the agent before you type
    #                           {ui.DIM}coach and rhythm read the sessions; context and
    #                           hooks read disk. Scheduled runs hidden by
    #                           .cs-ignore are left out.{ui.RST}
    #
    # All three still run when typed. It cannot be a `#` comment in place
    # because the listing below is one f-string.
    print(f"""
  {ui.BOLD}cs — Copilot Sessions{ui.RST}  v{__version__}

  {ui.BOLD}Start here{ui.RST}
    cs                    Landing screen — every view a keypress away
    cs home               The same, by name

  {ui.BOLD}List{ui.RST}
    cs recent [N|all]     Interactive sessions, last N days (default 7)
    cs all [N|all]        All sessions incl. quiet/automated (default: all time)
    cs repos              Sessions grouped by repository
    cs stats [N|all]      Output ledger: commits, PRs, files, cost, delegation
    cs timeline [N|all]   Working days: sessions, turns and spend per day
    cs cost [N|all]       AI spend by model, repo and day (default 30)
    cs efficiency [N|all] Whether it had to cost that: cache hit rate, rate
                          multiplier, first-token latency, reasoning share
    cs agents [N|all]     Delegation: you vs main agent vs sub-agents
                          {ui.DIM}N is a number of days; 'all' is every record, however
                          old. Every title says which window it counted.{ui.RST}
    cs skills             Skills Copilot can load here vs referenced in
                          sessions — yours, this repo's, the enabled plugins'
                          '--by-repo' regroups it by where each one was run
    cs profiles           The same for the agents you have defined
    cs instructions       Instruction files every session here starts with,
                          and which are past the length Copilot reads
    cs mcp [name]         MCP servers wired up — local, remote, and what they
                          may call
    cs hooks [event]      Commands Copilot runs on the session lifecycle, and
                          which of them point at a script that is gone

  {ui.BOLD}Govern{ui.RST}
    cs yolo [--all]       Which sessions ran unattended, and on what evidence
    cs handoff            Sessions that wrote or picked up a handoff
    cs handoff <N|id>     The chain of sessions that one belongs to
    cs audit [N|id]       Credential-shaped text found in sessions
                          {ui.DIM}Names and prefixes only — never the value itself.{ui.RST}

  {ui.BOLD}Find{ui.RST}
    cs search <words>     Full-text search, best match first
                          {ui.DIM}Searches summaries, repos, both sides of every turn and
                          session checkpoints. Supports AND / OR / NEAR and "phrases".{ui.RST}

  {ui.BOLD}Inspect & resume{ui.RST}
    {ui.DIM}Two views of one session: show is the page, read is the words.{ui.RST}
    cs show <N|id>        The session, whole: what's open, what was asked and
                          done, then spend, files, skills, agents, risk, turns
                          {ui.DIM}'--short' stops after the story · '--asks' lists every
                          request in order · 'cs brief' == 'cs show --short'{ui.RST}
    cs read <N|id>        The conversation itself, both sides, in full
                          {ui.DIM}'--turn N' prints one turn · 'transcript' is an alias{ui.RST}
    cs files <path>       Sessions that touched a file (globs and partials work)
    cs resume <N|id>      Resume (cd's to session dir, runs 'copilot --resume')

  {ui.BOLD}Pipe it somewhere{ui.RST}
    {ui.DIM}The same readings without the drawing, for a spreadsheet, a dashboard,
    a weekly report or a CI check.{ui.RST}
    cs <view> --json      Structured output, for any of:
                          {ui.DIM}recent, all, search, stats, timeline, cost, efficiency,
                          agents, repos, skills, profiles{ui.RST}
    cs <view> --csv       The view's main table, as CSV
    cs export <N|id>      One session as Markdown ('--json' for structured turns)
    cs completion <shell> Completions for bash, zsh or fish
                          {ui.DIM}Credentials are masked on the way out, exactly as they
                          are on screen — a file is more exposed than a screen.{ui.RST}

  {ui.BOLD}Notes{ui.RST}
    {ui.DIM}· N refers to a row from your last listing — 'cs show 3' or 'cs show #3'.
    · N stays with its session when you sort or filter, so it's safe to note.
    · Any unambiguous id prefix works too — that's the short id in footers.
    · Credits = AI units (AIU) spent, from the store's usage records — a
      session's whole life, every run of it, compaction and sub-agents
      included. Copilot CLI's status line counts only the run you are in,
      so on a resumed session it reads lower. 'cs show' splits the total.
    · Listings count the skills a session referenced and the sub-agents it ran;
      sort with --sort skills or --sort agents.
    · Reads $COPILOT_HOME/session-store.db (default ~/.copilot), read-only.
    · Skills also load from ~/.agents/skills; CS_AGENTS_HOME moves that root.
    · Theme: choose Theme on the home screen or press t. What you apply is
      kept in ~/.config/cs/settings.json and is what the next run starts
      in; CS_THEME=<name> overrides it for one run, CS_CONFIG_HOME moves
      the file.
    · The home screen and the session listings re-read the store every
      {_REFRESH_SECONDS} seconds, so a session started in another window
      arrives on its own. A #N always keeps its session: a new arrival takes
      the next free number rather than shifting the ones you already read.
    · CS_GLYPHS=ascii swaps every emoji for a plain marker.
    · Empty & automated sessions hidden by default; 'cs all' shows them.
    · Hide extra summaries by prefix in $COPILOT_HOME/.cs-ignore.
    · Credentials are masked in every view; CS_REDACT=0 shows raw text.{ui.RST}

  {ui.BOLD}Interactive mode{ui.RST}
    {ui.DIM}'cs' opens the landing screen; every view returns to it:
      ↑/↓    move down the menu           Enter  open the highlighted view
      type   narrow the menu as you go    Esc    clear what you typed
      /      full-text search             q      quit (when nothing is typed)
      t      open the live-preview theme picker
      ←/→    the window the counting views use — Stats, Timeline, AI spend
             and Delegation. Those rows say which window they will use, and
             Enter opens them with it.
      click  open, wheel scrolls

    'cs recent' and 'cs search' run full-screen in a terminal:
      ↑/↓    move the row cursor        ←/→    sort by prev/next column
      Enter  resume the session         v      brief: goal, outcome, open
      o      show: cost, files, turns    t      transcript: the conversation
      s      reverse the sort order     /      filter as you type
      g/G    jump to first/last         Esc    clear filter, then quit
      q      back to the menu, or quit
    Key hints shrink to fit a narrow window rather than being cut off, so
    the keys you cannot guess stay on screen.
    The mouse works too: click a row to select it (Enter then resumes),
    double-click a row to resume it straight away, click a column header
    to sort by it, and scroll with the wheel.
    Sorting keeps the highlight on your session, so it never moves out
    from under you.{ui.RST}

  {ui.BOLD}Sorting{ui.RST}
    {ui.DIM}Listings — recent, all, search, 'files <path>':
      --sort credits|turns|active|summary|repo|relevance [--asc|--desc]
      In a terminal, ←/→ pick the column and 's' reverses it.

    Reports — repos, timeline, cost, skills, profiles, hooks, mcp,
    yolo, handoff, audit:
      --sort <column> [--asc|--desc]; each report lists its own columns in
      its footer, and rejecting a typo prints them too.
      Opened from the menu, ←/→ and 's' re-sort without leaving the report.

    Every default is the order the report already came in, so sorting only
    ever happens because you asked for it.{ui.RST}

  {ui.BOLD}Explaining{ui.RST}
    {ui.DIM}cs <view> --why       How to read the view, section by section

    Reports print their findings and hold back their lessons. A paragraph
    explaining what a p95 is belongs on your first run, not your fiftieth,
    and a tool built for daily use should default to the fiftieth.
    CS_WHY=1 asks for the lessons every time, for as long as they help.{ui.RST}
""")

# ── Dispatch ─────────────────────────────────────────────────────────

def _window_label(days: int | None) -> str:
    """How a report says what it counted.

    `0` (or None) means every record, the convention `recent_sessions`
    already used for `cs all`. Spelling it once keeps five titles honest:
    a report headed "last 30 days" that had quietly reported everything —
    because the store could not be windowed — was lying in the one place
    the reader had no way to check.

    Spelling it once is also the only reason the plural is worth fixing:
    "last 1 days" appeared in the title of every windowed report, and this
    is the single line all of them go through.
    """
    if not days:
        return "all time"
    return "last 24 hours" if days == 1 else f"last {days} days"


def _turn_option(rest: list[str]) -> tuple[int | None, str | None]:
    """Read '--turn N' (or '--turn=N') from what follows a session reference."""
    i = 0
    turn = None
    while i < len(rest):
        arg = rest[i]
        if arg == "--turn":
            i += 1
            if i >= len(rest):
                return None, "missing value for --turn"
            value = rest[i]
        elif arg.startswith("--turn="):
            value = arg.split("=", 1)[1]
        else:
            return None, f"unknown option '{arg}'"
        if not value.lstrip("#").isdigit():
            return None, f"--turn wants a number, not '{value}'"
        turn = int(value.lstrip("#"))
        i += 1
    return turn, None


def _listing_options(
    rest: list[str], *, term: bool = False, default_days: int = 7
) -> tuple[int, str | None, bool | None, str | None, str | None]:
    days = default_days
    sort_by = None
    descending = None
    words: list[str] = []
    value = None
    i = 0
    while i < len(rest):
        arg = rest[i]
        if term and not arg.startswith("-"):
            # Every bare word joins the query: 'cs search entra group removal'
            # is one search, not a search plus two stray arguments.
            words.append(arg)
        elif not term and i == 0 and arg.lower() == "all":
            days = 0          # every session ever recorded
        elif not term and i == 0 and arg.removeprefix("-").isdigit():
            days = int(arg)
            if days < 1:
                return days, sort_by, descending, None, "days must be a positive integer"
        elif arg == "--sort":
            i += 1
            if i >= len(rest):
                return days, sort_by, descending, None, "missing value for --sort"
            sort_by = rest[i]
        elif arg.startswith("--sort="):
            sort_by = arg.split("=", 1)[1]
        elif arg == "--asc":
            if descending is True:
                return days, sort_by, descending, None, "use only one of --asc or --desc"
            descending = False
        elif arg == "--desc":
            if descending is False:
                return days, sort_by, descending, None, "use only one of --asc or --desc"
            descending = True
        else:
            return days, sort_by, descending, None, f"unknown option '{arg}'"
        i += 1
    value = " ".join(words) if words else None
    if term and value is None:
        return days, sort_by, descending, None, "missing search term"
    if sort_by and sort_by.lower() not in _SORT_COLUMNS:
        error = f"unknown sort column '{sort_by}' — choose from: {_SORT_NAMES}"
        return days, sort_by, descending, value, error
    return days, sort_by, descending, value, None


def _report_options(
    rest: list[str], report: str | None, *, days: bool = False,
    word: bool = False, flags: tuple[str, ...] = (),
) -> tuple[int | None, str | None, bool | None, str | None, set[str], str | None]:
    """Parse a report's arguments: [days|word] [--sort X] [--asc|--desc] [flags].

    Returns days, sort column, direction, the bare word, the flags that were
    given, and an error — reported the same way listings report theirs, so
    every command fails in one recognisable shape.

    `report` names the column set to check `--sort` against. It is None for
    `cs files`, which is a listing rather than a report and so is checked
    against the listing's columns by the caller.
    """
    day_count: int | None = None
    sort_by: str | None = None
    descending: bool | None = None
    value: str | None = None
    seen: set[str] = set()
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in flags:
            seen.add(arg)
        elif arg == "--sort":
            i += 1
            if i >= len(rest):
                return (day_count, sort_by, descending, value, seen,
                        "missing value for --sort")
            sort_by = rest[i]
        elif arg.startswith("--sort="):
            sort_by = arg.split("=", 1)[1]
        elif arg == "--asc":
            if descending is True:
                return (day_count, sort_by, descending, value, seen,
                        "use only one of --asc or --desc")
            descending = False
        elif arg == "--desc":
            if descending is False:
                return (day_count, sort_by, descending, value, seen,
                        "use only one of --asc or --desc")
            descending = True
        elif arg.startswith("-"):
            return (day_count, sort_by, descending, value, seen,
                    f"unknown option '{arg}'")
        elif days and arg.lower() == "all":
            day_count = 0     # every record, however old
        elif days and arg.isdigit():
            day_count = int(arg)
            if day_count < 1:
                return (day_count, sort_by, descending, value, seen,
                        "days must be a positive integer")
        elif word and value is None:
            value = arg
        else:
            return (day_count, sort_by, descending, value, seen,
                    f"unexpected argument '{arg}'")
        i += 1
    if report and sort_by and sort_by.lower() not in _REPORT_COLUMNS[report]:
        choices = ", ".join(_REPORT_COLUMNS[report])
        return (day_count, sort_by, descending, value, seen,
                f"unknown sort column '{sort_by}' — choose from: {choices}")
    return day_count, sort_by, descending, value, seen, None


def main(argv: list[str] | None = None) -> int:
    """Run a command. Ctrl-C anywhere exits quietly with the shell's 130."""
    try:
        return _dispatch(argv)
    except KeyboardInterrupt:
        print()
        return 130
    except BrokenPipeError:
        # `cs recent | head` closes the pipe early; that is not an error.
        # Nothing is done to sys.stdout here on purpose — main() is callable
        # from tests and from bin/cs, and redirecting the real fd would reach
        # far outside this function.
        return 0


def run() -> int:
    """Process entry point. main() plus the care a real process needs.

    Redirecting the fd on a broken pipe belongs here, not in main(): this
    only ever runs when cs *is* the process, so there is no caller's stream
    to disturb. Without it the interpreter prints a BrokenPipeError to
    stderr while flushing at shutdown — `cs recent | head` looked like it
    had failed when it had simply been cut off.
    """
    code = main()
    try:
        sys.stdout.flush()
    except BrokenPipeError:
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return 0
    return code


def _data_format(rest: list[str]) -> tuple[str | None, list[str], str | None]:
    """Pull `--json` / `--csv` out of a command's arguments.

    Returns the format asked for (or None), whatever is left for the command
    to parse, and an error when both were asked for at once — a request for
    two formats on one stream has no sensible answer, and picking one for the
    user would be a guess written to a file.
    """
    wanted = [flag for flag in ("--json", "--csv") if flag in rest]
    if len(wanted) > 1:
        return None, rest, "use only one of --json or --csv"
    if not wanted:
        return None, rest, None
    return wanted[0][2:], [a for a in rest if a not in wanted], None


def _emit_data(cmd: str, rest: list[str], fmt: str) -> int:
    """Answer a `--json` / `--csv` request, or explain that this view has none.

    Only the views whose answer is *figures* are here. `resume` runs a
    program, `read` is a conversation and `help` is prose — none of them have
    a data form, and inventing one for the sake of a uniform flag would mean
    maintaining an interface nobody asked for. Anything not listed says so,
    and names what can be asked for instead.
    """
    if cmd == "export":
        if fmt == "csv":
            print("error: a transcript has no CSV form — use 'cs export <#N|id>' "
                  "for Markdown or '--json' for structured turns", file=sys.stderr)
            return 1
        if not rest:
            print("error: export <#N|id> [--json]", file=sys.stderr)
            return 1
        cmd_export(rest[0], "json")
        return 0

    if cmd in ("recent", "list", "ls", "all"):
        show_all = cmd == "all"
        days, _sort, _desc, _term, error = _listing_options(
            rest, default_days=0 if show_all else 7
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        conn = db.connect()
        rows = db.recent_sessions(conn, days)
        conn.close()
        rows = _with_assets(_visible(rows, show_all))
        export.emit(export.sessions(days, show_all, rows), fmt)
        return 0

    if cmd in ("search", "find", "grep"):
        _days, _sort, _desc, term, error = _listing_options(rest, term=True)
        if error or not term:
            print(f"error: {error or 'search <words> — nothing to search for'}",
                  file=sys.stderr)
            return 1
        conn = db.connect()
        rows, hits = db.search(conn, term)
        conn.close()
        export.emit(export.search(term, _with_assets(rows), hits), fmt)
        return 0

    if cmd in ("skills", "profiles", "agent-profiles"):
        kind = "skills" if cmd == "skills" else "agents"
        if "--by-repo" in rest:
            if kind != "skills":
                print("error: --by-repo needs load markers, which only skills "
                      "leave — try 'cs skills --by-repo'", file=sys.stderr)
                return 1
            conn = db.connect()
            places = _skills_by_place(conn)
            conn.close()
            export.emit(export.assets_by_place(places), fmt)
            return 0
        gathered = _asset_inventory(kind)
        export.emit(export.assets(
            kind, gathered["names"], gathered["counts"],
            scopes={a.name: a.scope for a in gathered["inventory"]},
            states={a.name: a.state for a in gathered["inventory"]},
            ran=gathered["ran"],
        ), fmt)
        return 0

    if cmd == "repos":
        export.emit(export.repos(), fmt)
        return 0

    windowed = {
        "stats": (export.stats, None),
        "timeline": (export.timeline, 30),
        "cost": (export.cost, 30),
        "spend": (export.cost, 30),
        "efficiency": (export.efficiency, 30),
        "eff": (export.efficiency, 30),
        "agents": (export.delegation, 30),
        "delegation": (export.delegation, 30),
    }
    if cmd in windowed:
        build, fallback = windowed[cmd]
        days, _sort, _desc, _word, _flags, error = _report_options(
            rest, None, days=True
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        export.emit(build(fallback if days is None else days), fmt)
        return 0

    print(
        f"error: '{cmd}' has no data form — "
        f"--json and --csv work on: {', '.join(export.DATA_COMMANDS)}",
        file=sys.stderr,
    )
    return 1


def _dispatch(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]

    if not args:
        cmd_home()
        return 0

    cmd, rest = args[0], args[1:]

    # `--why` is global and stripped here rather than parsed per command,
    # because it changes how *every* report prints and not what any of them
    # computes. Threading it through twenty option parsers would be twenty
    # chances to forget it.
    global _WHY
    _WHY = "--why" in rest or os.environ.get("CS_WHY", "") not in ("", "0")
    rest = [a for a in rest if a != "--why"]

    # `--json` / `--csv` are answered before anything is drawn. A data request
    # is a different question from a view request — it has no window to page,
    # no cursor and no colour — so it takes its own path out rather than
    # threading a format flag through every renderer.
    fmt, rest, error = _data_format(rest)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if fmt:
        return _emit_data(cmd, rest, fmt)
    args = [cmd, *rest]

    if cmd in ("home", "menu"):
        cmd_home()
        return 0

    if cmd in ("recent", "list", "ls"):
        days, sort_by, descending, _, error = _listing_options(rest)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_recent(days, sort_by=sort_by, descending=descending)
    elif cmd == "all":
        # No window by default: "all" that meant "the last week" was the
        # single most confusing thing in the listing.
        days, sort_by, descending, _, error = _listing_options(rest, default_days=0)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_recent(days, show_all=True, sort_by=sort_by, descending=descending)
    elif cmd in ("search", "find", "grep"):
        _, sort_by, descending, term, error = _listing_options(rest, term=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_search(term, sort_by=sort_by, descending=descending)
    elif cmd in ("show", "view", "info"):
        _require(rest, "show <#N|id> [--short] [--asks]")
        known = {"--short", "--brief", "--asks"}
        flags = [a for a in rest[1:] if a not in known]
        if flags:
            print(f"error: unknown option '{flags[0]}'", file=sys.stderr)
            return 1
        cmd_show(
            rest[0],
            short=bool({"--short", "--brief"} & set(rest[1:])),
            show_asks="--asks" in rest[1:],
        )
    elif cmd in ("brief", "digest", "summary"):
        _require(rest, "brief <#N|id> [--asks]")
        flags = [a for a in rest[1:] if a != "--asks"]
        if flags:
            print(f"error: unknown option '{flags[0]}'", file=sys.stderr)
            return 1
        cmd_brief(rest[0], show_asks="--asks" in rest[1:])
    elif cmd in ("read", "transcript"):
        _require(rest, "read <#N|id> [--turn N]")
        turn, error = _turn_option(rest[1:])
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_read(rest[0], turn)
    elif cmd == "files":
        _, sort_by, descending, pattern, _, error = _report_options(
            rest, None, word=True
        )
        # A path is what this command is for; without one there is nothing to
        # look up, and the answer is the usage line rather than a leaderboard.
        if not error and not pattern:
            error = "files <path> — which sessions touched a file"
        if not error and sort_by and sort_by.lower() not in _SORT_COLUMNS:
            error = (f"unknown sort column '{sort_by}' — "
                     f"choose from: {', '.join(_SORT_COLUMNS)}")
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_files(pattern, sort_by=sort_by, descending=descending)
    elif cmd == "completion":
        _require(rest, "completion <bash|zsh|fish>")
        cmd_completion(rest[0])
    elif cmd == "export":
        _require(rest, "export <#N|id> [--json]")
        cmd_export(rest[0])
    elif cmd in ("resume", "r"):
        _require(rest, "resume <#N|id>")
        cmd_resume(rest[0])
    elif cmd == "repos":
        _, sort_by, descending, _, _, error = _report_options(rest, "repos")
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_repos(sort_by=sort_by, descending=descending)
    elif cmd == "stats":
        days, _, _, _, _, error = _report_options(rest, None, days=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_stats(days)
    elif cmd == "timeline":
        days, sort_by, descending, _, _, error = _report_options(
            rest, "timeline", days=True
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_timeline(30 if days is None else days,
                     sort_by=sort_by, descending=descending)
    elif cmd in ("cost", "spend"):
        days, sort_by, descending, _, _, error = _report_options(
            rest, "cost", days=True
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_cost(30 if days is None else days,
                 sort_by=sort_by, descending=descending)
    elif cmd in ("efficiency", "eff"):
        days, _, _, _, _, error = _report_options(rest, None, days=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_efficiency(30 if days is None else days)
    elif cmd in ("agents", "delegation"):
        days, _, _, _, _, error = _report_options(rest, None, days=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_agents(30 if days is None else days)
    elif cmd in ("yolo", "autonomy"):
        _, sort_by, descending, _, seen, error = _report_options(
            rest, "yolo", flags=("--all", "-a")
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_yolo(show_all=bool(seen), sort_by=sort_by, descending=descending)
    elif cmd in ("handoff", "handoffs"):
        _, sort_by, descending, ref, _, error = _report_options(
            rest, "handoff", word=True
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_handoff(ref, sort_by=sort_by, descending=descending)
    elif cmd in ("audit", "secrets", "security"):
        _, sort_by, descending, ref, _, error = _report_options(
            rest, "audit", word=True
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_audit(ref, sort_by=sort_by, descending=descending)
    elif cmd in ("coach", "practice", "review"):
        days, sort_by, descending, _, _, error = _report_options(
            rest, "coach", days=True
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_coach(30 if days is None else days,
                  sort_by=sort_by, descending=descending)
    elif cmd in ("rhythm", "when"):
        days, _, _, _, _, error = _report_options(rest, None, days=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_rhythm(30 if days is None else days)
    elif cmd in ("context", "setup"):
        if rest:
            print(f"error: unexpected argument '{rest[0]}'", file=sys.stderr)
            return 1
        cmd_context()
    elif cmd in ("instructions", "rules"):
        if rest:
            print(f"error: unexpected argument '{rest[0]}'", file=sys.stderr)
            return 1
        cmd_instructions()
    elif cmd == "hooks":
        _, sort_by, descending, event, _, error = _report_options(
            rest, "hooks", word=True
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_hooks(event, sort_by=sort_by, descending=descending)
    elif cmd in ("mcp", "servers"):
        _, sort_by, descending, name, _, error = _report_options(
            rest, "mcp", word=True
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_mcp(name, sort_by=sort_by, descending=descending)
    elif cmd in ("skills", "profiles", "agent-files"):
        kind = "skills" if cmd == "skills" else "agents"
        _, sort_by, descending, name, flags, error = _report_options(
            rest, "assets", word=True, flags=("--by-repo",)
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        if "--by-repo" in flags:
            # Agent profiles leave no load marker, so there is nothing to
            # group. Saying so beats drawing an empty page.
            if kind != "skills":
                print("error: --by-repo needs load markers, which only skills "
                      "leave — try 'cs skills --by-repo'", file=sys.stderr)
                return 1
            # Refused rather than quietly ignored. A flag that is dropped on
            # the floor is worse than one that is rejected: the reader gets
            # an answer to a question they did not ask and no sign of it.
            if name:
                print(f"error: --by-repo lists every checkout, so it cannot "
                      f"also be narrowed to '{name}' — use one or the other",
                      file=sys.stderr)
                return 1
            if sort_by or descending is not None:
                print("error: --by-repo is ordered by how much work each "
                      "checkout did; --sort does not apply to it",
                      file=sys.stderr)
                return 1
        cmd_assets(kind, name, sort_by=sort_by, descending=descending,
                   by_place="--by-repo" in flags)
    elif cmd in ("help", "-h", "--help"):
        cmd_help()
    elif cmd in ("version", "-v", "--version"):
        print(f"cs {__version__}")
    else:
        print(f"error: unknown command '{cmd}' — run 'cs help'", file=sys.stderr)
        return 1
    return 0


def _require(rest: list[str], usage: str) -> None:
    if not rest:
        print(f"error: missing argument — usage: cs {usage}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    sys.exit(run())
