"""Shared helpers, constants, and mutable CLI state for the ``cs`` command package."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from .. import (
    db,
    hooks,
    redact,
    ui,
)

# ── #N shortcut index ────────────────────────────────────────────────
# Listings number their rows; the map is persisted so `cs show #3` works
# even in a new shell. Lives next to theme settings under the config home
# (see ui.settings_path), never inside COPILOT_HOME — that store is opened
# read-only, and cs has no business writing next to it.

def _index_file() -> Path:
    return ui.settings_path().parent / ".cs-last-index"


def _legacy_index_file() -> Path:
    """Pre-move location next to the session store (read fallback only)."""
    return db.default_db_path().parent / ".cs-last-index"


def _save_index(index: dict[int, str]) -> None:
    path = _index_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(f"{k}={v}" for k, v in index.items()))
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
    if not f.exists():
        f = _legacy_index_file()
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


_KIT_MIN_WIDTH = 88


_SUMMARY_MIN = 20


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
    _asset_names_fn = globals()["_asset_names"]
    if not rows:
        return rows
    conn = db.connect()
    try:
        skills = db.assets_by_session(conn, [name for name, _ in _asset_names_fn("skills")])
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
    as an FTS marker used to discard the filename itself. A title the
    listing does not show is masked like one it does: Copilot's generated
    name is often the whole first prompt, pasted token and all.
    """
    if source in _SOURCE_LABELS:
        return _snippet(text)
    if source in ("name", "summary"):
        return redact.one_line(redact.redact(text))
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


_TUI_COLUMNS = ("active", "turns", "credits", "skills", "agents", "repo", "summary")


_SORT_NAMES = "active, turns, credits, skills, agents, summary, repo, relevance"


# ── Sorting reports ──────────────────────────────────────────────────

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


_RISK = {
    "critical": (ui.ROSE, "a documented key format — this can only be a credential"),
    "high": (ui.AMBER, "a credential-carrying shape: a token, or a URL login"),
    "medium": (ui.MUTED, "a value on a password-ish name — credible, not certain"),
}


_RISK_LABEL = {"critical": "critical", "high": "high", "medium": "review"}


_HARDCODED = (ui.ROSE, "hardcoded", "written into a file the session wrote")


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
        # Keep the insertion point visible even when the query outgrows the
        # window. Count cells, since a typed character need not be one cell.
        room = max(0, width - ui.cells(prefix) - 2)
        shown = text
        if ui.cells(shown) > room:
            used, start = 0, len(text)
            for ch in reversed(text):
                used += ui.cells(ch)
                if used > max(0, room - 1):
                    break
                start -= 1
            shown = "…" + text[start:]
        _addstr(screen, y, 0, f"{prefix}{shown}|".ljust(width), width, theme["status"])
        screen.refresh()
        try:
            key = getattr(screen, "get_wch", screen.getch)()
        except curses.error:
            continue  # wide-character reads raise on a timeout
        except KeyboardInterrupt:
            return None  # cancel the filter, same as Esc
        if isinstance(key, str):
            if key.isprintable():
                text += key
                continue
            key = ord(key)
        if key == -1:
            continue  # a timeout the caller left armed, not a keypress
        if key in (10, 13, curses.KEY_ENTER):
            return text
        if key == curses.KEY_RESIZE:
            height, width = screen.getmaxyx()
            y = height - 1
            continue
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
        ("p pin", "p pin", 2),
        ("/ filter", "/", 3),
        (f"q {back}", f"q {back}", 0),  # rank 0 never drops: it is the way out
    ]
    if mouse:
        hints.insert(0, ("click/scroll · double-click resume", "click", 4))
    return _fit_hints(hints, width)


def _addstr(screen, y: int, x: int, text: str, width: int, style: int = 0) -> None:
    import curses

    if y >= screen.getmaxyx()[0] or x >= screen.getmaxyx()[1]:
        return
    try:
        clipped = ui.clip(text, max(min(width, screen.getmaxyx()[1] - x - 1), 0))
        screen.addnstr(y, x, clipped, len(clipped), style)
    except curses.error:
        pass


def _short_path(path: str, cwd: str) -> str:
    """Drop the session's own directory (then $HOME) from the front of a path."""
    for base in (cwd, os.path.expanduser("~")):
        if base and base != "-" and path.startswith(base.rstrip("/") + "/"):
            return path[len(base.rstrip("/")) + 1 :]
    return path


def _tail(path: str, width: int) -> str:
    """Trim a path from the left — the file name is the part worth keeping."""
    return path if len(path) <= width else "…" + path[-(width - 1) :]


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
    _read_in_place_fn = globals()["_read_in_place"]
    lines = text.count("\n") + 1
    height = shutil.get_terminal_size().lines
    if not sys.stdout.isatty():
        print(text)
        return False
    if _HOME_ACTIVE and _read_in_place_fn(text, sort):
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


_WHY = False


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


# ── Governance: autonomy, handoffs, exposure ─────────────────────────

def _when(stamp: str) -> str:
    """A timestamp as a person reads it: no date-time 'T' in the middle."""
    return stamp[5:16].replace("T", " ")


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

_LEVELS = {
    "high": (ui.ROSE, "worth changing this week"),
    "medium": (ui.AMBER, "worth a look"),
    "low": (ui.MUTED, "a tendency, not a problem"),
}

_PERIODS: tuple[tuple[str, int, str, str], ...] = (
    # key, days, what it says, what it says on a narrow window
    ("1", 7, "7 days", "7d"),
    ("2", 30, "30 days", "30d"),
    ("3", 90, "90 days", "90d"),
    ("4", 365, "a year", "1y"),
    ("5", 0, "all time", "all"),
)
