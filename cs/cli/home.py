"""Home screen TUI, ``cmd_home``, and ``cmd_help``."""

from __future__ import annotations

import sqlite3
import sys
import time

from .. import (
    __version__,
    db,
    mcp,
    ui,
)
from ._common import (
    _PERIODS,
    _REFRESH_SECONDS,
    _addstr,
    _capture,
    _curses_wrapper,
    _disable_mouse,
    _enable_mouse,
    _mouse_event,
    _page,
    _pause,
    _prompt,
    _window_label,
)
from .evidence import cmd_endings, cmd_failures, cmd_subagents, cmd_switches
from .governance import cmd_audit, cmd_handoff, cmd_yolo
from .inventory import (
    _asset_names,
    cmd_agents,
    cmd_assets,
    cmd_context,
    cmd_hooks,
    cmd_instructions,
    cmd_mcp,
)
from .listing import cmd_recent, cmd_search
from .practice_cmds import cmd_coach, cmd_rhythm, cmd_standup
from .reports import (
    cmd_cost,
    cmd_efficiency,
    cmd_repos,
    cmd_stats,
)
from .today import (
    cmd_asks,
    cmd_cleanup,
    cmd_eod,
    cmd_file_history,
    cmd_next,
    cmd_saved_menu,
    cmd_similar,
    cmd_weekly,
)
from .workflow import cmd_budget_view, cmd_pins


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
    limit = ui.daily_budget_aiu()
    return [
        # Today · what to do now, and what the day and week came to.
        (ui.menu_icon("next"), "Next up",
         "open handoffs, cut-off endings, stuck loops, wip, pins",
         cmd_next, ""),
        (ui.menu_icon("standup"), "Standup",
         f"today's brief: what moved, handoffs, risks · {window}",
         cmd_standup, "period"),
        (ui.menu_icon("eod"), "End of day",
         "commits, PRs, handoffs, spend and failures since midnight",
         cmd_eod, ""),
        (ui.menu_icon("weekly"), "Weekly review",
         "7 days against the 7 before: spend, failures, habits",
         cmd_weekly, ""),
        (ui.menu_icon("budget"), "Budget",
         (f"daily limit {limit:g} AIU · ←/→ changes it" if limit
          else "no daily limit · ←/→ sets one"),
         cmd_budget_view, "budget"),
        (ui.menu_icon("recent"), "Recent sessions",
         "browse, read and resume · last 7 days",
         lambda: cmd_recent(7), ""),
        (ui.menu_icon("pin"), "Pinned",
         "sessions you marked to keep handy",
         cmd_pins, ""),
        (ui.menu_icon("all"), "All sessions",
         "every session ever recorded · scroll to browse",
         lambda: cmd_recent(0, show_all=True), ""),
        (ui.menu_icon("search"), "Search", "full text across every turn and checkpoint",
         cmd_search, "term"),
        (ui.menu_icon("similar"), "Similar work",
         "like this, the sessions that shipped something first",
         cmd_similar, "term"),
        (ui.menu_icon("asks"), "My asks",
         f"what you opened each session asking for · {window}",
         cmd_asks, "period"),
        (ui.menu_icon("saved"), "Saved searches",
         "pick one and run it live",
         cmd_saved_menu, ""),
        (ui.menu_icon("history"), "File history",
         "every session, agent and turn that touched a file",
         cmd_file_history, "term"),
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
        (ui.menu_icon("subagents"), "Sub-agents",
         f"which agents ran, on which models, how long · {window}",
         cmd_subagents, "period"),
        (ui.menu_icon("switches"), "Model switches",
         f"model or effort changed mid-run, cost either side · {window}",
         cmd_switches, "period"),
        (ui.menu_icon("autonomy"), "Autonomy",
         "which sessions ran unattended · YOLO", cmd_yolo, ""),
        (ui.menu_icon("handoff"), "Handoffs",
         "work passed from one session to the next",
         cmd_handoff, ""),
        (ui.menu_icon("security"), "Security",
         f"credentials found in session text · {window}",
         lambda days=30: cmd_audit(days=days), "period"),
        (ui.menu_icon("failures"), "Tool failures",
         f"which tools fail, where, worst sessions · {window}",
         cmd_failures, "period"),
        (ui.menu_icon("loops"), "Stuck loops",
         f"one tool failing again and again, with turns · {window}",
         lambda days=30: cmd_failures(days, loops=True), "period"),
        (ui.menu_icon("endings"), "Unclean endings",
         f"sessions cut off by an error, length or filter · {window}",
         cmd_endings, "period"),
        # Improve · Standup moved up to Today, so Practice opens this group;
        # the ("Practice", "Improve") anchor in _HOME_GROUP_STARTS follows it.
        (ui.menu_icon("practice"), "Practice",
         f"habits the record shows, worst first · {window}",
         cmd_coach, "period"),
        (ui.menu_icon("rhythm"), "Rhythm",
         f"when the work actually happens · {window}",
         cmd_rhythm, "period"),
        (ui.menu_icon("context"), "Context",
         "what this repo hands the agent before you type",
         cmd_context, ""),
        (ui.menu_icon("cleanup"), "Clean-up",
         "stale pins, quiet wip, handoffs nobody took · suggests only",
         cmd_cleanup, ""),
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
         "what runs around a session, how often it fails, what's missing",
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


_HOME_GROUP_TONE = {
    "Today": "turns",       # the ramp's last hue: the one group about now
    "Find": "title",        # 39  — the product blue
    "Measure": "credits",   # 177 — violet, as spend is everywhere else
    "Govern": "warn",       # 214 — amber: this group is the bad news
    "Improve": "credits",
    "Reference": "active",  # 49  — mint: present, and nothing to answer for
}


_HOME_GROUP_STARTS: tuple[tuple[str, str], ...] = (
    ("Next up", "Today"),
    ("Recent sessions", "Find"),
    ("Repositories", "Measure"),
    ("Autonomy", "Govern"),
    ("Practice", "Improve"),
    ("Skills", "Reference"),
)

# What the prompt says on a row that asks for text. Search is the default.
_TERM_PROMPTS = {
    "Similar work": " similar to: ",
    "File history": " file: ",
}


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


def _home_snapshot(days: int = 120) -> tuple[list[tuple], list[int]]:
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
        budget = ui.daily_budget_aiu()
        today_nano = 0
        if budget is not None:
            today = db.cost_totals(conn, 1)
            today_nano = int(today.get("nano_aiu", 0) or 0) if today else 0
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
    # When a daily budget is set, show today's spend against it (same 24h
    # window standup uses) and colour by how close to the limit you are.
    if budget is not None:
        spent = today_nano / 1e9
        aiu_fact: tuple = (
            f"{spent:,.2f}/{budget:g}", "AIU today",
            ui.budget_style_name(spent, budget),
        )
    else:
        aiu_fact = (f"{nano_aiu / 1e9:,.2f}" if nano_aiu > 0 else "-", "AIU")
    facts = [
        aiu_fact,
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

    1. **The menu is grouped.** Six captioned blocks are what makes forty
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
    # Headings go only when the menu gets at least two rows per heading
    # beyond a small floor — below that the captions would be most of what
    # is on screen.
    layout = _home_layout(shown, room >= heads * 2 + 4)
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
                      facts: list[tuple],
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
    for index, fact in enumerate(facts):
        value, label = fact[0], fact[1]
        style = theme.get(fact[2], theme["credits"]) if len(fact) > 2 else theme["credits"]
        if column + len(value) + len(label) + 3 > edge:
            break
        if index:
            _addstr(screen, row, column, "·", width, theme["separator"])
            column += 2
        _addstr(screen, row, column, value, width, style)
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
        if not asks or asks == "budget":
            return index
        if asks == "theme":
            activate_theme(_theme_picker(screen, active_theme))
            return None
        if asks == "term":
            term = _prompt(screen, theme, height - 1, width,
                           _TERM_PROMPTS.get(items[index][1], " search: "), "")
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
                # A row whose name matches beats one whose description does:
                # typing "sub-agents" means the Sub-agents row, not the
                # Delegation row above it that mentions sub-agents.
                cursor = next((index for index in shown
                               if query.lower() in items[index][1].lower()),
                              shown[0])
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
                    # Two cells short of the edge: room for the scroll marks,
                    # and a description that does not fit ends in '…' rather
                    # than stopping mid-word.
                    _addstr(screen, line, 24, ui.trunc(description, width - 27),
                            width - 25, style or theme["repo"])
            # On a short window the menu scrolls, and without a mark the rows
            # below the fold — Theme and Help, at 24 lines — simply did not
            # exist as far as anyone could see.
            if reveal is None and offset > 0:
                _addstr(screen, top, width - 2, "↑", 1, theme["title"])
            if reveal is None and offset + visible < len(layout):
                _addstr(screen, top + visible - 1, width - 2, "↓", 1, theme["title"])
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
            elif key in (curses.KEY_LEFT, curses.KEY_RIGHT) and (
                    items[cursor][4] == "budget"):
                # On the Budget row the arrows move the limit, not the window:
                # the row is the setting, and it is saved as it changes. The
                # header re-reads so it shows the new limit straight away;
                # the refresh deadline is left where it was.
                ui.step_budget(1 if key == curses.KEY_RIGHT else -1)
                _refresh_home(state)
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
            # Rows that open on Enter alone take no argument; the rest were
            # handed one by the menu (a window, a term, a session).
            takes = asks not in ("", "budget", "theme")
            if takes and given is None:
                continue  # nothing chosen — straight back to the menu
            try:
                # `is None`, not falsy: 0 is a window, and it means all time.
                waited = action(given) if takes else action()
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
    print(f"""
  {ui.BOLD}cs — Copilot Sessions{ui.RST}  v{__version__}

  {ui.BOLD}Start here{ui.RST}
    cs                    Landing screen — every view a keypress away
    cs home               The same, by name

  {ui.BOLD}Today{ui.RST}
    cs next [N|all]       What to pick up: open handoffs, cut-off endings,
                          stuck loops, wip tags and pins — each with its reason
                          {ui.DIM}default: the last 14 days; pins and wip always{ui.RST}
    cs eod [--md]         End of day: commits, PRs, handoffs, spend against the
                          budget and tool failures since midnight
    cs weekly [--md]      Last 7 days against the 7 before: spend, dearest
                          sessions, repeated failures, top 3 habits
                          {ui.DIM}--md prints Markdown ready to paste; text is masked{ui.RST}

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
    cs hooks [event]      Commands Copilot runs on the session lifecycle, how
                          often each event ran and failed, and which point at
                          a script that is gone
    cs subagents [N|all]  Sub-agents run: models, overrides, tools, tokens, time
    cs switches [N|all]   Model or effort changed mid-run, and the AIU either side

  {ui.BOLD}Improve{ui.RST}
    cs standup [N|all]    Today's brief: activity, what moved, handoffs, risks
                          {ui.DIM}'daily' is an alias · default is the last day{ui.RST}
    cs coach [N|all]      Habits the record shows, scored and worst first
    cs rhythm [N|all]     When the work happens: hours, days, streaks
    cs context            What this repo hands the agent before you type
    cs cleanup [N]        Stale pins (quiet N days, default 14), wip quiet 7+
                          days, handoffs nobody took — suggests, never removes
                          {ui.DIM}standup, coach and rhythm read the sessions; context and
                          hooks read disk. Scheduled runs hidden by
                          .cs-ignore are left out.{ui.RST}

  {ui.BOLD}Govern{ui.RST}
    cs yolo [--all]       Which sessions ran unattended, and on what evidence
    cs handoff            Sessions that wrote or picked up a handoff
    cs handoff <N|id>     The chain of sessions that one belongs to
    cs audit [N|all|id]   Credential-shaped text found in sessions
                          {ui.DIM}Default last 30 days; 'all' scans the whole store.
                          Names and prefixes only — never the value itself.{ui.RST}
    cs failures [N|all]   Tool calls that failed: by tool, by repo, worst sessions
    cs failures --loops   Stuck loops: one tool failing 3+ times in a row, with
                          its turns ('cs loops' is the same)
    cs endings [N|all]    Sessions whose last call ended in an error, the length
                          limit or a content filter
                          {ui.DIM}failures, loops, sub-agents, switches and hook runs
                          read session-state/*/events.jsonl; endings read the
                          store. Default last 30 days.{ui.RST}

  {ui.BOLD}Pins & budget{ui.RST}
    cs pin <ref>          Keep a session handy on the home screen
    cs unpin <ref>        Drop a pin
    cs pins               List pinned sessions
    cs note <ref> [text]  Attach a note (omit text to show; empty clears)
    cs tag <ref> <tag>    Add a tag to a session
    cs untag <ref> <tag>  Remove a tag
    cs budget [N|clear]   Daily AIU budget — show, set, or clear
    cs budget --check     One line, exit 1 when over budget — for hooks/scripts
                          {ui.DIM}Stored in ~/.config/cs/settings.json · home header
                          colours amber at 70% and rose when over.{ui.RST}

  {ui.BOLD}Find{ui.RST}
    cs search <words>     Full-text search, best match first
                          {ui.DIM}Searches names, summaries, repos, both sides of every turn and
                          session checkpoints. Supports AND / OR / NEAR and "phrases".{ui.RST}
    cs search --save <name> <words>   Save a search under a name, and run it
    cs saved [name]       List saved searches, or run one
    cs similar <words>    The same search, sessions that shipped a commit or
                          PR first, each with its outcome
    cs asks [--repo .] [N|all]  Your opening request in each session, one line,
                          with outcome and cost ('c' copies one in a listing)
    cs files <path> --history   Every touch of a file: session, agent, turn,
                          and what was asked

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
                          agents, repos, skills, profiles, standup, failures,
                          loops, subagents, switches, endings, next, eod,
                          weekly, similar, asks, saved, cleanup, budget,
                          files --history{ui.RST}
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
    · Theme, pins, notes/tags and the daily budget live in
      ~/.config/cs/settings.json; CS_THEME=<name> overrides the theme for
      one run, CS_CONFIG_HOME moves the file. Never written into COPILOT_HOME.
    · The home screen and the session listings re-read the store every
      {_REFRESH_SECONDS} seconds, so a session started in another window
      arrives on its own. A #N always keeps its session: a new arrival takes
      the next free number rather than shifting the ones you already read.
    · CS_GLYPHS=ascii swaps every emoji for a plain marker.
    · Per-session event logs are read, never written, and reduced to counts
      cached in ~/.cache/cs/events-digest.json (XDG_CACHE_HOME moves it).
    · Empty & automated sessions hidden by default; 'cs all' shows them.
    · Hide extra summaries by prefix in $COPILOT_HOME/.cs-ignore.
    · Credentials are masked in every view; CS_REDACT=0 shows raw text.{ui.RST}

  {ui.BOLD}Interactive mode{ui.RST}
    {ui.DIM}'cs' opens the landing screen; every view returns to it:
      ↑/↓    move down the menu           Enter  open the highlighted view
      type   narrow the menu as you go    Esc    clear what you typed
      /      full-text search             q      quit (when nothing is typed)
      t      open the live-preview theme picker
      ←/→    the window the counting views use — Stats, AI spend, Delegation,
             Security, Tool failures and the rest. Those rows say which
             window they will use, and Enter opens them with it.
      click  open, wheel scrolls

    'cs recent' and 'cs search' run full-screen in a terminal:
      ↑/↓    move the row cursor        ←/→    sort by prev/next column
      Enter  resume the session         v      brief: goal, outcome, open
      o      show: cost, files, turns    t      transcript: the conversation
      s      reverse the sort order     /      filter as you type
      p      pin / unpin the row        g/G    jump to first/last
      Esc    clear filter, then quit    q      back to the menu, or quit
    Key hints shrink to fit a narrow window rather than being cut off, so
    the keys you cannot guess stay on screen.
    The mouse works too: click a row to select it (Enter then resumes),
    double-click a row to resume it straight away, click a column header
    to sort by it, and scroll with the wheel.
    Sorting keeps the highlight on your session, so it never moves out
    from under you.
    Reports and transcripts opened from the menu wrap long lines:
      /      find text                  n/N    next/previous matching row
    An empty find clears the highlights; Esc cancels the find prompt.{ui.RST}

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
