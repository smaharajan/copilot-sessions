"""Single-session views: show, brief, read, export, and the transcript reader."""

from __future__ import annotations

import shutil
import sys

from .. import (
    db,
    export,
    redact,
    ui,
)
from ._common import (
    _addstr,
    _bullets,
    _capture,
    _curses_wrapper,
    _disable_mouse,
    _enable_mouse,
    _fit_hints,
    _item,
    _mouse_event,
    _one_line,
    _page,
    _paths,
    _plain,
    _prompt,
    _resolve_ref,
    _short_path,
    _tail,
    _user_text,
    _why,
    _why_hint,
)
from .evidence import _print_session_failures
from .governance import _governance, _print_governance
from .inventory import _asset_names

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

    # From the session's event log, when it has one: every tool call, which
    # failed, and on which turn — the store keeps none of that.
    _print_session_failures(session_id, inner)

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
        lines.extend(_transcript_turn(index, prompt, reply, when, inner))

    lines.extend(_session_footer(session_id, "brief|show|resume", inner))
    return "\n".join(lines)


def _transcript_turn(index: int, prompt: str, reply: str, when: str,
                     inner: int) -> list[str]:
    """One turn as `cs read` sets it: the rule, then both sides, masked.

    Shared with `cs replay`, which shows one of these per page.
    """
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
    return [
        ui.rule(inner, title, note=note),
        "",
        ui.speaker(ui.YOU_MARK, "You", ui.MINT),
        *_turn_body(prompt, "(empty)", ui.MINT, inner),
        "",
        ui.speaker(ui.COPILOT_MARK, "Copilot", ui.VIOLET),
        *_turn_body(reply, "(no reply recorded)", ui.VIOLET, inner),
        "",
    ]


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

    # Long lines wrap rather than stopping at the edge. They used to be cut
    # there with no sign that anything was missing — a transcript opened
    # from the menu lost the right-hand end of every table and block of
    # tool output in it. The rows are shaped once per width (and per
    # re-sort), not once per keypress: a long transcript is thousands of
    # lines to measure.
    shaped: dict = {"width": None, "lines": None, "rows": [], "term": None,
                    "anchors": []}
    # Find: less has it, and from the menu less is not what opens a
    # transcript — so a 1,700-row conversation could be scrolled but not
    # searched. `found` holds the rows that match, recomputed when the rows
    # are reshaped.
    term = ""
    found: list[int] = []
    selected_match: int | None = None

    def rows_for(width: int) -> list[list[tuple[str, int]]]:
        nonlocal found, selected_match, offset
        if (shaped["width"] != width or shaped["lines"] is not lines
                or shaped["term"] != term):
            anchor = None
            was_match = selected_match is not None
            if shaped["lines"] is lines and shaped["term"] == term and shaped["anchors"]:
                index = selected_match if was_match else offset
                anchor = shaped["anchors"][min(index, len(shaped["anchors"]) - 1)]
            anchors: list[tuple[int, int]] = []
            rows, found = _reader_rows(lines, palette, width, term, anchors=anchors)
            shaped.update(width=width, lines=lines, rows=rows, term=term, anchors=anchors)
            selected_match = None
            if anchor is not None:
                offset = next((i for i in range(len(anchors) - 1, -1, -1)
                               if anchors[i] <= anchor), 0)
                if was_match and offset in found:
                    selected_match = offset
        return shaped["rows"]

    while True:
        height, width = screen.getmaxyx()
        page = max(1, height - 1)
        rows = rows_for(width)
        offset = max(0, min(offset, max(0, len(rows) - page)))
        screen.erase()
        screen.bkgd(" ", theme["background"])
        span = width + ui.REVEAL_LAG * page
        swept = span if reveal is None else ui.reveal_columns(reveal, span)
        for row, runs in enumerate(rows[offset:offset + page]):
            column = 0
            # Each row trails the one above it, so the edge crossing the page
            # is a slant rather than a shutter.
            edge = width if reveal is None else min(
                width, max(0, swept - row * ui.REVEAL_LAG))
            for text, attr in runs:
                if column >= edge:
                    break
                _addstr(screen, row, column, text, edge - column,
                        theme["cursor"] if attr == -1 else attr or theme["summary"])
                column += ui.cells(text)
        at_end = offset + page >= len(rows)
        hints = [
            ("↑/↓ scroll", "↑↓", 3),
            ("space page", "space", 2),
            ("g/G ends", "g/G", 3),
            # The reader only runs from the menu, so both ways out go there.
            ("Esc back", "Esc", 0),
            ("q home", "q", 0),
        ]
        hints.insert(3, ("/ find", "/", 0))
        if term:
            hints.insert(4, ("n/N next", "n/N", 0))
        if sort:
            hints.insert(0, ("←/→ sort", "←/→ sort", 1))
            hints.insert(1, ("s reverse", "s", 2))
        if mouse:
            hints.insert(0, ("scroll wheel", "wheel", 4))
        place = "end" if at_end else f"{min(offset + page, len(rows))}/{len(rows)}"
        position = place
        result = ""
        if term:
            here = sum(1 for index in found
                       if index <= (selected_match if selected_match is not None else offset))
            result = f"{max(here, 1)}/{len(found)}" if found else "not found"
            place = f"'{ui.trunc(term, 16)}' {result} · {place}"
        order = ""
        if sort:
            # The column belongs beside the position, not in the hints: hints
            # shrink to their short forms on a narrow window, and the one
            # thing you need after pressing ← is which column you landed on.
            order = (sort["label"](sort["column"]) if sort.get("label") else
                     f"{sort['column']}{'↓' if sort['descending'] else '↑'}")
            place = f"{order} · {place}"
        # Allocate both sides before drawing: status must never overwrite
        # the search keys or the way back on a narrow terminal.
        budget = max(0, width - 3 - ui.cells(_fit_hints(hints, 0)))
        forms = [place, " · ".join(part for part in (order, result, position) if part),
                 " · ".join(part for part in (result, position) if part), result or position]
        place = next((form for form in forms if ui.cells(form) <= budget),
                     ui.trunc(forms[-1], budget))
        room = ui.cells(place)
        _addstr(screen, height - 1, 0,
                f" {_fit_hints(hints, max(1, width - room - 3))} ".ljust(width),
                width, theme["status"])
        _addstr(screen, height - 1, max(0, width - room - 1), place, width, theme["status"])
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
                selected_match = None
            elif kind == "wheel-down":
                offset += 3
                selected_match = None
            continue
        if key in (ord("q"), ord("Q"), 27):
            return
        if key in (curses.KEY_DOWN, curses.KEY_UP, curses.KEY_NPAGE, curses.KEY_PPAGE,
                   curses.KEY_HOME, curses.KEY_END, *map(ord, "jk fbgG")):
            selected_match = None
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
            offset = len(shaped["rows"])
        elif key == ord("/"):
            entered = _prompt(screen, theme, height - 1, width, " find: ", "")
            if entered is not None:
                term = entered
                rows_for(width)
                offset = _next_match(found, offset - 1, 1, offset)
                selected_match = offset if found else None
        elif key in (ord("n"), ord("N")) and found:
            after = selected_match if selected_match is not None else offset
            offset = _next_match(found, after, 1 if key == ord("n") else -1, offset)
            selected_match = offset
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


def _reader_rows(
    lines: list[str], palette: dict[str, int], width: int, term: str,
    *, anchors: list[tuple[int, int]] | None = None,
) -> tuple[list[list[tuple[str, int]]], list[int]]:
    """Wrap styled lines, marking matches before wrapping so words can cross rows.

    Attribute -1 marks matching text until the reader assigns its highlight.
    Case folding can expand a character (ß becomes ss), so folded positions
    are mapped back to the original text before applying the highlight.
    """
    rows = []
    wanted = term.casefold()
    for line_number, line in enumerate(lines):
        runs = ui.sgr_runs(line, palette)
        if wanted:
            plain = "".join(text for text, _ in runs)
            folded = plain.casefold()
            positions = [at for at, ch in enumerate(plain) for _ in ch.casefold()]
            marked: set[int] = set()
            start = folded.find(wanted)
            while start >= 0:
                marked.update(range(positions[start], positions[start + len(wanted) - 1] + 1))
                start = folded.find(wanted, start + len(wanted))
            styled: list[tuple[str, int]] = []
            at = 0
            for text, attr in runs:
                begin = 0
                for end in range(1, len(text) + 1):
                    if end == len(text) or ((at + end) in marked) != ((at + begin) in marked):
                        styled.append((text[begin:end], -1 if at + begin in marked else attr))
                        begin = end
                at += len(text)
            runs = styled
        starts: list[int] = []
        rows.extend(ui.wrap_runs(runs, width, starts=starts))
        if anchors is not None:
            anchors.extend((line_number, start) for start in starts)
    return rows, [at for at, runs in enumerate(rows) if any(attr == -1 for _, attr in runs)]


def _next_match(found: list[int], after: int, step: int, stay: int) -> int:
    """The match after (or before) row `after`, round the end if need be.

    `stay` is where to remain when there is nothing to find.
    """
    if not found:
        return stay
    if step > 0:
        return next((index for index in found if index > after), found[0])
    return next((index for index in reversed(found) if index < after), found[-1])


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
