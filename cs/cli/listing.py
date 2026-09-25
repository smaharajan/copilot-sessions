"""Session listing commands and the interactive listing TUI."""

from __future__ import annotations

import shutil
import sqlite3
import sys
import time
from datetime import date

from .. import (
    db,
    redact,
    ui,
)
from ._common import (
    _HOME_ACTIVE,
    _KIT_MIN_WIDTH,
    _REFRESH_SECONDS,
    _SORT_COLUMNS,
    _SOURCE_LABELS,
    _SUMMARY_MIN,
    _TUI_COLUMNS,
    _addstr,
    _curses_wrapper,
    _disable_mouse,
    _enable_mouse,
    _highlight,
    _hint_line,
    _hit_text,
    _kit_cells,
    _kit_of,
    _mouse_event,
    _no_summary,
    _note,
    _number_rows,
    _pause,
    _project_tag,
    _prompt,
    _save_index,
    _short_path,
    _sort_rows,
    _visible,
    _window_label,
    _with_assets,
)
from .resume import _resume_from_listing
from .session import cmd_read, cmd_show


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
    rows = ui.float_pins(rows)
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
    pin_pad = " "
    print(
        f"  {ui.DIM}{pin_pad}  #   {labels['active']:<7} {labels['turns']:<6}"
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
        # One-cell marker so the # column stays aligned under the header.
        mark = f"{ui.AMBER}*{ui.RST}" if ui.is_pinned(sid) else " "
        row = (f"  {mark}{num}  {ui.DIM}{active}{ui.RST}  {turns_txt}  {cred_txt}"
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


def _filter_rows(
    rows: list[tuple], query: str, found: set[str] | frozenset[str] = frozenset()
) -> list[tuple]:
    """Rows whose summary, repo or directory contain the query, plus `found`.

    The substring match is what lets a few letters find a title as they are
    typed; `found` is what the store's full-text search returned for the
    same words, which is how a session that mentions them only inside its
    conversation stays in the list.
    """
    if not query:
        return rows
    q = query.lower()
    return [
        r for r in rows
        if r[0] in found or any(q in (r[i] or "").lower() for i in (2, 3, 4))
    ]


def _find_sessions(query: str) -> tuple[set[str], dict[str, tuple[str, str]]]:
    """What `cs search` finds for a listing's filter: which sessions, and why.

    The filter used to read only the titles on screen, so a session that
    mentioned the words anywhere else — in a prompt, a reply, a checkpoint —
    could not be reached from a listing at all, and typing a project's name
    into 'All sessions' found five sessions out of fifty-six. A store that
    cannot be read right now finds nothing extra, and the titles still match.
    """
    try:
        conn = db.connect(fatal=False)
        try:
            found, reasons = db.search(conn, query)
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return set(), {}
    return {row[0] for row in found}, reasons


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
                _listing_tui, rows, title, default_sort, hits, state, reload,
                _find_sessions,
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


_LISTING_KEYS = frozenset("vVoOtTrRsSgGqQpP/")


def _listing_tui(
    screen,
    rows: list[tuple],
    title: str,
    default_sort: str = "active",
    hits: dict[str, tuple[str, str]] | None = None,
    state: dict | None = None,
    reload=None,
    find=None,
) -> tuple[str, str] | None:
    """The full-screen listing. Returns (verb, session id), or None to leave.

    `reload` re-reads the rows on the refresh heartbeat. `find` answers a
    filter the way `cs search` would — (session ids, why each matched) — so
    that filtering reaches the conversations and not just the titles. With
    neither, the rows are all there is, and the filter reads them alone.
    """
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
    # What `find` returned, and for which words. Kept in 'state' so a trip
    # out to a session and back does not search the store again.
    found_for, found, reasons = state.get("found", ("", set(), {}))

    def refind() -> None:
        nonlocal found_for, found, reasons
        found_for = query
        found, reasons = find(query) if find and query else (set(), {})

    if query and found_for != query:
        refind()
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
                _filter_rows(rows, query, found), sort_by, descending, numbers
            )
            sorted_rows = ui.float_pins(sorted_rows)
            if follow is not None:
                # Re-sorting moves rows around; keep the highlight on the session
                # the user picked rather than on whatever lands at that position.
                cursor = next(
                    (i for i, row in enumerate(sorted_rows) if row[0] == follow), cursor
                )
                follow = None

            screen.erase()
            height, width = screen.getmaxyx()
            # A search keeps the bottom line for the matching text of the
            # row, and so does a filter, which is a search too. A filter's
            # reason wins over the search's where both have one: it is about
            # the words just typed.
            why = {**(hits or {}), **reasons} if query else (hits or {})
            explains = bool(hits) or bool(query)
            visible = max(height - 6 - (1 if explains else 0), 1)
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
                # Cut at the edge, the filter was the part that went — and a
                # filter you cannot see reads as sessions that have vanished.
                if ui.cells(heading) > width:
                    heading = f"◆  filter '{query}' · {title}"
            _addstr(screen, 0, 0, heading, width, theme["title"])
            _addstr(
                screen,
                1,
                0,
                _hint_line(width, mouse, "home" if _HOME_ACTIVE else "quit"),
                width,
                theme["help"],
            )

            # The summary is the last column, and the repository the one
            # before it. The other way round, the summary took every cell
            # the rest left over, so on a wide window the repository sat at
            # the far edge, a hundred blank cells from the title it belonged
            # to. Last, the slack falls off the end of the row where it is
            # not a gap. The repository is only as wide as its longest name
            # (never wider than it used to be), and it is measured over every
            # row, not the filtered ones, so typing a filter does not slide
            # the titles sideways.
            longest_tag = max(
                (ui.cells(_project_tag(row[3], row[4])) for row in rows), default=0
            )
            repo_width = min(22, max(12, width // 5), max(8, longest_tag + 2))
            # Columns give way as the window narrows, least needed first —
            # the kit pair, then turns, credits and the repository — and the
            # summary keeps _SUMMARY_MIN cells whatever else has to go, since
            # it is what a listing is read for. At 40 columns the numbers
            # used to take every cell there was and no title showed at all.
            # The mouse's column map is built from this same list, so a
            # column that has given way cannot be clicked either.
            spans = {"active": 12, "turns": 7, "credits": 9, "skills": 7,
                     "agents": 7, "repo": repo_width}
            room = width - 5 - spans["active"] - 1
            kept = {"active"}
            for group in (("repo",), ("credits",), ("turns",), ("skills", "agents")):
                if group[0] == "skills" and width < _KIT_MIN_WIDTH:
                    break
                cost = sum(spans[name] for name in group)
                if room - cost >= _SUMMARY_MIN:
                    kept.update(group)
                    room -= cost
            columns = []
            x = 5
            for name in ("active", "turns", "credits", "skills", "agents", "repo"):
                if name in kept:
                    columns.append((name, x, spans[name]))
                    x += spans[name]
            columns.append(("summary", x, max(12, width - x - 1)))
            summary_width = columns[-1][2]
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
                cells = {
                    "active": (f"{date.fromisoformat(started[:10]):%d %b %Y}",
                               theme["active"]),
                    "turns": (str(turns), theme["turns"]),
                    "credits": (ui.fmt_aiu(nano_aiu),
                                theme["credits"] if nano_aiu else theme["number"]),
                    "skills": (str(skills) if skills else "·",
                               theme["turns"] if skills else theme["number"]),
                    "agents": (str(agents) if agents else "·",
                               theme["turns"] if agents else theme["number"]),
                    # A session run outside any repository — from home, or a
                    # scratch directory too generic to name — has no tag to
                    # show. It gets the same dimmed '·' the skills and agents
                    # columns use for nothing, so the column reads as counted
                    # and empty rather than as a cell that failed to draw.
                    "repo": (ui.trunc(tag, repo_width - 1) or "·",
                             theme["repo"] if tag else theme["number"]),
                    "summary": (ui.trunc(
                        redact.one_line(redact.redact(summary)) or _no_summary(turns),
                        summary_width - 1,
                    ), theme["summary"]),
                }
                if on_cursor:
                    _addstr(screen, line, 0, " " * width, width, theme["cursor"])
                _addstr(
                    screen,
                    line,
                    0,
                    f"{numbers[sid]:>3}",
                    4,
                    theme["cursor"] if on_cursor else theme["number"],
                )
                # One-cell pin mark in the spare column of the #N field, so
                # existing click targets and frame assertions stay put.
                if ui.is_pinned(sid):
                    _addstr(
                        screen,
                        line,
                        3,
                        "*",
                        1,
                        theme["cursor"] if on_cursor else theme["warn"],
                    )
                for name, x, column_width in columns:
                    value, style = cells[name]
                    _addstr(screen, line, x, value, column_width,
                            theme["cursor"] if on_cursor else style)

            if explains:
                hit = why.get(sorted_rows[cursor][0]) if sorted_rows else None
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
                at, total = cursor + 1, len(sorted_rows)
                forms = (
                    f" {at} of {total:,} sessions · Enter resumes, or "
                    f"'cs resume {number}' later ",
                    f" {at} of {total:,} · ↵ resumes · cs resume {number} ",
                    f" {at}/{total:,} · cs resume {number} ",
                    f" {at}/{total:,} ",
                )
            else:
                forms = (
                    " no sessions match the filter · press / to edit, Esc to clear ",
                    " no match · / edits · Esc clears ",
                    " no match ",
                )
            status = next((form for form in forms if ui.cells(form) <= width),
                          forms[-1])
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
                    before = rows
                    rows, title, numbers, follow = _reread_listing(
                        reload, rows, title, numbers, state,
                        sorted_rows[cursor][0] if sorted_rows else None,
                    )
                    # A session that arrived since may be one the filter is
                    # looking for.
                    if query and rows is not before:
                        refind()
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
                    refind()
            elif key in (ord("v"), ord("V"), ord("o"), ord("O")) and sorted_rows:
                return "show", sorted_rows[cursor][0]
            elif key in (ord("t"), ord("T")) and sorted_rows:
                return "read", sorted_rows[cursor][0]
            elif key in (ord("r"), ord("R")) and sorted_rows:
                return "resume", sorted_rows[cursor][0]
            elif key in (ord("p"), ord("P")) and sorted_rows:
                sid = sorted_rows[cursor][0]
                ui.toggle_pin(sid)
                follow = sid
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
            elif 32 < key < 127 and chr(key) not in _LISTING_KEYS:
                # Typing finds, as it does on the landing page. A letter with
                # no job of its own used to do nothing at all, so typing a
                # project's name into a listing read as a search that found
                # nothing. It opens the filter with that letter already in
                # it; the letters that are keys here keep their jobs, even
                # when there is no row for them to act on.
                entered = _prompt(screen, theme, height - 1, width, " filter: ",
                                  chr(key))
                if entered is not None:
                    query, cursor, offset = entered, 0, 0
                    refind()
    finally:
        # Anything that leaves the loop keeps the view it left behind,
        # so returning from a detail view lands where you were.
        state.update(
            sort_by=sort_by,
            descending=descending,
            cursor=cursor,
            offset=offset,
            query=query,
            found=(found_for, found, reasons),
        )
        if timed:
            wait(-1)  # a detail view opened from here reads keys of its own


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
