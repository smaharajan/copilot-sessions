"""Day-to-day views: what to pick up, what the day and week came to, saved
searches and clean-up.

As in `evidence.py`, each reading is a `_*_data` function returning plain,
already-masked data (what `--json` hands back) and the page draws that same
data. Every reason a session is put in front of you is printed with it.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

from .. import (
    db,
    events,
    practice,
    signals,
    ui,
)
from ._common import (
    _HOME_ACTIVE,
    _addstr,
    _capture,
    _curses_wrapper,
    _disable_mouse,
    _enable_mouse,
    _hint,
    _mouse_event,
    _note,
    _page,
    _save_index,
    _visible,
    _with_assets,
)
from .evidence import (
    _TOP,
    _UNCLEAN,
    _clean,
    _frame,
    _headline,
    _plural,
    _session_fields,
    _table,
    _turn_range,
)
from .listing import _interactive_listing, cmd_search
from .ops import _watch_read

# How much each reason to go back to a session weighs in Next up. An open
# handoff is work someone is waiting on; a pin is only a bookmark.
_NEXT_WEIGHTS = {"handoff": 5, "ended": 4, "stuck": 3, "wip": 2, "pinned": 1}
_NEXT_DAYS = 14
_WIP = "wip"


def _local_midnight() -> str:
    """The start of today, where you are, as the store's UTC stamp shape."""
    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _stamp(active: str) -> str:
    return (active or "").replace(" ", "T")


def _outcome(refs: dict[str, int] | None) -> str:
    if not refs or not (refs.get("commit") or refs.get("pr")):
        return "no commit or PR"
    parts = []
    if refs.get("commit"):
        parts.append(_plural(refs["commit"], "commit"))
    if refs.get("pr"):
        parts.append(_plural(refs["pr"], "PR"))
    return " · ".join(parts)


def _tagged(tag: str) -> set[str]:
    wanted = tag.lower()
    return {sid for sid, entry in ui._annotations_map().items()
            if isinstance(entry, dict) and any(
                isinstance(t, str) and t.lower() == wanted
                for t in entry.get("tags") or [])}


# ── Next up ──────────────────────────────────────────────────────────

def _next_data(days: int = _NEXT_DAYS) -> dict:
    conn = db.connect()
    try:
        everything = {row[0]: row for row in db.recent_sessions(conn, 0)}
        window = ([row[0] for row in db.recent_sessions(conn, days)] if days
                  else list(everything))
        opened = signals.open_handoffs(conn)
        last = db.last_calls(conn)
        digests = events.digests(window)
        stuck = [sid for sid, d in digests.items() if d["loops"]]
        times = db.turn_times(conn, stuck)
    finally:
        conn.close()
    inside = set(window)
    reasons: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in opened:
        if row["id"] in inside:
            reasons[row["id"]].append(("handoff", "open handoff: " + row["evidence"]))
    for sid in window:
        # Only a recorded reason earns a place here: a call with no reason
        # is listed by `cs endings` as unknown, but there is nothing in it to
        # go back and act on.
        if sid in last and last[sid][1] in _UNCLEAN and last[sid][1]:
            turn, reason = last[sid][0], _UNCLEAN[last[sid][1]]
            where = f" at turn {turn}" if turn is not None else ""
            reasons[sid].append(("ended", f"last call ended: {reason}{where}"))
    for sid in stuck:
        loop = digests[sid]["loops"][0]
        turns = _turn_range(events.turn_of(loop["start"], times[sid]),
                            events.turn_of(loop["end"], times[sid]))
        where = ("" if turns == "—" else
                 f" · turns {turns}" if "–" in turns else f" · turn {turns}")
        reasons[sid].append(("stuck", f"stuck: {_clean(loop['tool'])} failed "
                                      f"{loop['run']}× in a row{where}"))
    for sid in _tagged(_WIP):
        reasons[sid].append(("wip", "tagged wip"))
    for sid in ui.pinned_ids():
        reasons[sid].append(("pinned", "pinned"))
    ranked = []
    for sid, why in reasons.items():
        if sid not in everything:
            continue
        ranked.append({
            **_session_fields(everything[sid]),
            "score": sum(_NEXT_WEIGHTS[kind] for kind, _ in why),
            "reasons": [{"kind": kind, "why": _clean(text)} for kind, text in why],
            "resume": f"cs resume {sid[:8]}",
        })
    # Most urgent first; among equals, the one you touched last.
    ranked.sort(key=lambda r: _stamp(r["last_active"]), reverse=True)
    ranked.sort(key=lambda r: -r["score"])
    return {"window_days": days, "sessions": ranked}


def cmd_next(days: int = _NEXT_DAYS) -> bool:
    """What to pick up next, most urgent first, and why each is here."""
    data = _next_data(days)
    if data["sessions"] and sys.stdin.isatty() and sys.stdout.isatty():
        conn = db.connect()
        try:
            rows = {row[0]: row for row in db.recent_sessions(conn, 0)}
        finally:
            conn.close()
        ordered = _with_assets([rows[s["id"]] for s in data["sessions"]
                                if s["id"] in rows])
        hits = {s["id"]: ("next", " · ".join(r["why"] for r in s["reasons"]))
                for s in data["sessions"]}
        title = f"Next up · {_plural(len(ordered), 'session')} · most urgent first"
        return _interactive_listing(ordered, title, show_all=True,
                                    default_sort="relevance", hits=hits)
    return _page(_capture(lambda: _render_next(data)))


def _render_next(data: dict) -> None:
    inner = _frame("Next up", data["window_days"])
    sessions = data["sessions"]
    if not sessions:
        _note("Nothing is waiting: no open handoff, cut-off ending, stuck loop, "
              "wip tag or pin.", inner)
        print()
        return
    _headline(f"{_plural(len(sessions), 'session')} to pick up, most urgent "
              f"first", inner)
    print()
    index = {}
    for n, session in enumerate(sessions[:_TOP * 2], 1):
        index[n] = session["id"]
        # The command rides the title line, right-aligned, so each session
        # costs its reasons and nothing more.
        command = f"cs resume {n}"
        title = session["summary"] or "(untitled)"
        room = inner - 7 - len(command) - 2
        if room >= 16:
            title = ui._fit(title, room)
            gap = " " * (room - ui.cells(title) + 2)
            print(f"  {ui.SKY}{n:>3}{ui.RST}  {ui.BOLD}{title}{ui.RST}"
                  f"{gap}{ui.MUTED}{command}{ui.RST}")
        else:
            print(f"  {ui.SKY}{n:>3}{ui.RST}  {ui.BOLD}"
                  f"{ui._fit(title, inner - 7)}{ui.RST}")
        for reason in session["reasons"]:
            colour = ui.ROSE if reason["kind"] in ("ended", "stuck") else ui.AMBER
            print(f"       {colour}·{ui.RST} {ui._fit(reason['why'], inner - 9)}")
        if room < 16:
            print(f"       {ui.MUTED}{command}{ui.RST}")
    _save_index(index)
    print()
    _hint("Enter on the home row opens these as a listing, where r resumes", inner)
    print()


# ── End of day ───────────────────────────────────────────────────────

def _eod_data() -> dict:
    since = _local_midnight()
    conn = db.connect()
    try:
        rows = [row for row in _visible(db.recent_sessions(conn, 2), False)
                if _stamp(row[1]) >= since[:16]]
        ids = [row[0] for row in rows]
        refs = db.refs_since(conn, since, ids)
        spent = db.spend_since(conn, since)
        wrote = [h for h in signals.handoffs(conn)
                 if h["id"] in set(ids) and h["role"] in ("emitted", "both")]
    finally:
        conn.close()
    digests = events.digests(ids)
    budget = ui.daily_budget_aiu()
    return {
        "since": since,
        "sessions": [_session_fields(row) for row in rows],
        "commits": [{"id": sid, "value": _clean(value)}
                    for sid, kind, value in refs if kind == "commit"],
        "prs": [{"id": sid, "value": _clean(value)}
                for sid, kind, value in refs if kind == "pr"],
        "handoffs": [{"id": h["id"], "summary": _clean(h["summary"])} for h in wrote],
        "nano_aiu": spent,
        "budget_aiu": budget,
        "over_budget": bool(budget is not None and spent is not None
                            and spent / 1e9 > budget),
        "tool_calls": sum(d["calls"] for d in digests.values()),
        "tool_failures": sum(d["failures"] for d in digests.values()),
        "stuck_loops": sum(len(d["loops"]) for d in digests.values()),
    }


def _spend_line(nano: int | None, budget: float | None) -> str:
    if nano is None:
        return "not recorded"
    line = f"{nano / 1e9:,.2f} AIU"
    if budget is not None:
        line += f" of {budget:g} ({nano / 1e9 / budget:.0%})" if budget else ""
    return line


def _eod_markdown(data: dict) -> str:
    day = datetime.now().astimezone().strftime("%A %d %B %Y")
    lines = [f"## End of day · {day}", ""]
    lines.append(f"- **Sessions:** {len(data['sessions'])}")
    lines.append(f"- **Spend:** {_spend_line(data['nano_aiu'], data['budget_aiu'])}")
    if data["tool_calls"]:
        lines.append(f"- **Tool calls:** {data['tool_calls']:,}, "
                     f"{data['tool_failures']} failed, "
                     f"{_plural(data['stuck_loops'], 'stuck loop')}")
    if data["commits"] or data["prs"]:
        lines.append(f"- **Shipped:** {_plural(len(data['commits']), 'commit')}, "
                     f"{_plural(len(data['prs']), 'PR')}")
    lines.append("")
    if data["sessions"]:
        lines.append("### What moved")
        lines += [f"- {s['summary'] or '(untitled)'} (`{s['id'][:8]}`)"
                  for s in data["sessions"][:_TOP]]
        lines.append("")
    if data["commits"] or data["prs"]:
        lines.append("### Commits and PRs")
        lines += [f"- commit `{c['value']}`" for c in data["commits"][:_TOP]]
        lines += [f"- PR #{p['value'].lstrip('#')}" for p in data["prs"][:_TOP]]
        lines.append("")
    if data["handoffs"]:
        lines.append("### Handoffs written")
        lines += [f"- {h['summary'] or '(untitled)'} (`{h['id'][:8]}`)"
                  for h in data["handoffs"]]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def cmd_eod(markdown: bool = False) -> bool:
    """Today's work in one page — or as Markdown to paste somewhere."""
    data = _eod_data()
    if markdown:
        sys.stdout.write(_eod_markdown(data))
        return False
    return _page(_capture(lambda: _render_eod(data)))


def _render_eod(data: dict) -> None:
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    print()
    print(ui.rule(inner, "End of day · since midnight"))
    print()
    if not (data["sessions"] or data["nano_aiu"] or data["commits"] or data["prs"]
            or data["handoffs"]):
        _note("Nothing yet today: no session has run since midnight.", inner,
              indent=4)
        print()
        _hint("cs weekly — the last 7 days · cs next — what to pick up", inner)
        print()
        return
    # The day in one bold line, and the tool calls muted under it.
    colour = ui.ROSE if data["over_budget"] else ""
    parts = [(_plural(len(data["sessions"]), "session"), ""),
             (_spend_line(data["nano_aiu"], data["budget_aiu"]), colour),
             (f"{_plural(len(data['commits']), 'commit')} · "
              f"{_plural(len(data['prs']), 'PR')}", "")]
    # One line when it fits; otherwise the shipped count takes the next.
    lines = [parts]
    if ui.cells(" · ".join(text for text, _c in parts)) > inner - 4:
        lines = [parts[:2], parts[2:]]
    for line in lines:
        print("    " + f"{ui.MUTED} · {ui.RST}".join(
            f"{colour}{ui.BOLD}{ui._fit(text, inner - 4)}{ui.RST}"
            for text, colour in line))
    if data["tool_calls"]:
        _note(f"{data['tool_calls']:,} tool calls · {data['tool_failures']:,} failed · "
              f"{_plural(data['stuck_loops'], 'stuck loop')}", inner, indent=4)
    print()
    if data["sessions"]:
        print(ui.heading("What moved", ui.MINT, inner))
        for session in data["sessions"][:_TOP]:
            print(f"    {ui.SKY}{session['id'][:8]}{ui.RST}  "
                  f"{ui._fit(session['summary'] or '(untitled)', inner - 14)}")
        print()
    if data["commits"] or data["prs"]:
        print(ui.heading("Commits and PRs", ui.ACCENT, inner))
        for commit in data["commits"][:_TOP]:
            print(f"    {ui.MUTED}commit{ui.RST}  {ui._fit(commit['value'], inner - 14)}")
        for pr in data["prs"][:_TOP]:
            print(f"    {ui.MUTED}PR{ui.RST}      #{ui._fit(pr['value'].lstrip('#'), inner - 15)}")
        print()
    if data["handoffs"]:
        print(ui.heading(f"Handoffs written · {len(data['handoffs'])}", ui.SKY, inner))
        for handoff in data["handoffs"]:
            print(f"    {ui.SKY}{handoff['id'][:8]}{ui.RST}  "
                  f"{ui._fit(handoff['summary'] or '(untitled)', inner - 14)}")
        print()
    _hint("cs eod --md — as Markdown to paste · cs next — what to pick up", inner)
    print()


# ── Weekly review ────────────────────────────────────────────────────

def _weekly_data() -> dict:
    conn = db.connect()
    try:
        this = (db.cost_totals(conn, 7) or {}).get("nano_aiu", 0)
        both = (db.cost_totals(conn, 14) or {}).get("nano_aiu", 0)
        by_day = db.cost_by_day(conn, 7)
        top = db.cost_top_sessions(conn, 7, 5)
        rows = _visible(db.recent_sessions(conn, 7), False)
        findings, _scores = practice.review(practice.snapshot(conn, 7))
    finally:
        conn.close()
    digests = events.digests([row[0] for row in rows])
    tools: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for digest in digests.values():
        for tool, (_made, failed) in digest["tools"].items():
            if failed:
                tools[_clean(tool) or "unknown"][0] += 1
                tools[_clean(tool) or "unknown"][1] += failed
    repeated = sorted(
        ({"tool": tool, "sessions": n, "failures": failed}
         for tool, (n, failed) in tools.items() if n >= 2),
        key=lambda r: (-r["sessions"], -r["failures"], r["tool"]))
    previous = max(both - this, 0)
    return {
        "window_days": 7,
        "sessions": len(rows),
        "nano_aiu": this,
        "previous_nano_aiu": previous,
        "change": round((this - previous) / previous, 4) if previous else None,
        "by_day": [{"day": day, "nano_aiu": nano} for day, nano, _calls in by_day],
        "top_sessions": [{"id": sid, "summary": _clean(summary), "nano_aiu": nano}
                         for sid, summary, nano in top],
        "repeated_failures": repeated[:_TOP],
        "stuck_loops": sum(len(d["loops"]) for d in digests.values()),
        "habits": [{"name": f.name, "severity": f.severity,
                    "headline": _clean(f.headline), "fix": _clean(f.fix)}
                   for f in findings[:3]],
    }


def _trend(data: dict) -> str:
    if data["change"] is None:
        return "no spend the week before to compare with"
    arrow = "up" if data["change"] > 0 else "down"
    return (f"{arrow} {abs(data['change']):.0%} on the week before "
            f"({data['previous_nano_aiu'] / 1e9:,.2f} AIU)")


def _weekly_markdown(data: dict) -> str:
    lines = ["## Weekly review · last 7 days", "",
             f"- **Sessions:** {data['sessions']}",
             f"- **Spend:** {data['nano_aiu'] / 1e9:,.2f} AIU, {_trend(data)}",
             f"- **Stuck loops:** {data['stuck_loops']}", ""]
    if data["top_sessions"]:
        lines.append("### Most expensive sessions")
        lines += [f"- {s['nano_aiu'] / 1e9:,.2f} AIU · {s['summary']} "
                  f"(`{s['id'][:8]}`)" for s in data["top_sessions"]]
        lines.append("")
    if data["repeated_failures"]:
        lines.append("### Failing in more than one session")
        lines += [f"- `{r['tool']}`: {r['failures']} failures across "
                  f"{r['sessions']} sessions" for r in data["repeated_failures"]]
        lines.append("")
    if data["habits"]:
        lines.append("### Habits to work on")
        lines += [f"- **{h['name']}** ({h['severity']}): {h['headline']}"
                  for h in data["habits"]]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def cmd_weekly(markdown: bool = False) -> bool:
    """The last seven days against the seven before them."""
    data = _weekly_data()
    if markdown:
        sys.stdout.write(_weekly_markdown(data))
        return False
    return _page(_capture(lambda: _render_weekly(data)))


def _render_weekly(data: dict) -> None:
    inner = _frame("Weekly review", 7)
    _headline(f"{data['nano_aiu'] / 1e9:,.2f} AIU over "
              f"{_plural(data['sessions'], 'session')}", inner)
    _note(_trend(data), inner, indent=4)
    print()
    if data["by_day"]:
        print(ui.heading("Spend by day", ui.VIOLET, inner))
        peak = max(d["nano_aiu"] for d in data["by_day"]) or 1
        bar = max(8, min(30, inner - 26))
        for day in data["by_day"]:
            print(f"    {ui.MUTED}{day['day'][5:]}{ui.RST}  "
                  f"{ui.VIOLET}{day['nano_aiu'] / 1e9:>9,.2f}{ui.RST}  "
                  f"{ui.bar(day['nano_aiu'], peak, bar, colour=ui.VIOLET)}")
        print()
    if data["top_sessions"]:
        print(ui.heading("Most expensive", ui.VIOLET, inner))
        _table([("aiu", "AIU", ">"), ("session", "session", "<"),
                ("summary", "summary", "<")],
               [{"aiu": (f"{s['nano_aiu'] / 1e9:,.2f}", ui.VIOLET),
                 "session": (s["id"][:8], ui.SKY),
                 "summary": (s["summary"] or "(untitled)", "")}
                for s in data["top_sessions"]],
               inner, {"aiu": 9}, [("session", 8)])
    if data["repeated_failures"]:
        print(ui.heading("Failing in more than one session", ui.ROSE, inner))
        _table([("sessions", "sessions", ">"), ("failures", "failed", ">"),
                ("tool", "tool", "<")],
               [{"sessions": (str(r["sessions"]), ""),
                 "failures": (str(r["failures"]), ui.ROSE),
                 "tool": (r["tool"], ui.CODE)} for r in data["repeated_failures"]],
               inner, {"sessions": 8, "failures": 6}, [], flex="tool", least=8)
    if data["habits"]:
        print(ui.heading("Habits to work on", ui.AMBER, inner))
        for habit in data["habits"]:
            print(f"    {ui.AMBER}{habit['severity']:<7}{ui.RST}"
                  f"{ui.BOLD}{ui._fit(habit['name'], inner - 12)}{ui.RST}")
            _note(habit["headline"], inner, indent=11)
        print()
    _hint("cs weekly --md — as Markdown · cs coach 7 — every habit, with its "
          "evidence", inner)
    print()


# ── Today ────────────────────────────────────────────────────────────

def _today_data() -> dict:
    """Now, what to pick up, since midnight, and this week — one reading.

    Empty sections are left out. The three windows are the readings the
    separate commands already compute.
    """
    state: dict = {}
    try:
        _watch_read(state)
    except (OSError, sqlite3.Error):
        state = {}
    session = state.get("session")
    reading: dict = {}
    if session:
        reading["now"] = {
            "id": session["id"],
            "summary": session["summary"],
            "repo": session["repo"] or None,
            "turns": session["turns"],
            "nano_aiu": session["nano_aiu"],
            "burn_per_minute": round(session["burn_per_minute"], 4),
            "burn": list(session.get("burn") or []),
            "today_nano_aiu": session["today_nano_aiu"],
            "budget_aiu": session["budget"],
            "left_aiu": None if session["left"] is None else round(session["left"], 4),
            "last_tool": session["last_tool"] or None,
            "last_tool_at": session["last_tool_at"] or None,
            "last_failure": (
                {"tool": session["last_failure"][0], "at": session["last_failure"][1]}
                if session["last_failure"] else None),
        }
    pickup = _next_data()["sessions"][:5]
    if pickup:
        reading["pick_up"] = pickup
    day = _eod_data()
    since: dict = {}
    if day["sessions"]:
        since["sessions"] = len(day["sessions"])
    if day["commits"]:
        since["commits"] = len(day["commits"])
    if day["prs"]:
        since["prs"] = len(day["prs"])
    if day["handoffs"]:
        since["handoffs"] = len(day["handoffs"])
    if day["tool_failures"]:
        since["tool_failures"] = day["tool_failures"]
    if day["stuck_loops"]:
        since["stuck_loops"] = day["stuck_loops"]
    if day["nano_aiu"]:
        since["nano_aiu"] = day["nano_aiu"]
        if day["budget_aiu"] is not None:
            since["budget_aiu"] = day["budget_aiu"]
    if since:
        reading["since_midnight"] = since
    week = _weekly_data()
    if week["sessions"] or week["nano_aiu"] or week["by_day"]:
        reading["this_week"] = {
            "sessions": week["sessions"],
            "nano_aiu": week["nano_aiu"],
            "previous_nano_aiu": week["previous_nano_aiu"],
            "change": week["change"],
            "by_day": week["by_day"],
        }
    return reading


def cmd_today() -> bool:
    """Where you are: now, what to pick up, the day so far and the week."""
    return _page(_capture(lambda: _render_today(_today_data())))


# Ten one-minute bins. The burn track and the budget bar are this wide so
# their captions start in the same column.
_BINS = 10
_LABEL = 5


def _line(indent: int, text: str, width: int, colour: str = "") -> None:
    """One indented line, clipped to the window. Empty text draws nothing."""
    if not text:
        return
    shown = ui._fit(text, max(_edge(width) - indent, 1))
    if colour:
        shown = f"{colour}{shown}{ui.RST}"
    print(f"{' ' * indent}{shown}")


def _choose(options: list[str], room: int) -> str:
    """The first phrase that fits, else the last one clipped to `room`."""
    for option in options:
        if ui.cells(option) <= room:
            return option
    return ui._fit(options[-1] if options else "", max(room, 1))


def _session_facts(now: dict, room: int) -> str:
    """Repo, turns, this session's spend — dropping the spend, then the repo,
    before the line is allowed to run past `room`."""
    turns = _plural(now.get("turns") or 0, "turn")
    aiu = f"{(now.get('nano_aiu') or 0) / 1e9:,.2f} AIU"
    repo = now.get("repo") or ""
    full = [part for part in (repo, turns, aiu) if part]
    if ui.cells(" · ".join(full)) <= room:
        return " · ".join(full)
    shorter = [part for part in (repo, turns) if part]
    if shorter and ui.cells(" · ".join(shorter)) <= room:
        return " · ".join(shorter)
    tail = f" · {turns}"
    if repo and room > ui.cells(tail) + 4:
        return ui._fit(repo, room - ui.cells(tail)) + tail
    return ui._fit(turns, max(room, 1))


def _edge(width: int) -> int:
    """The column a full line ends on. The page rule stops two short of the
    window, and a line that runs past it looks longer than the page."""
    return max(width - 2, 16)


def _masthead(left: str, right: str, width: int) -> None:
    """Facts on the left, the resume command on the right, one line.

    When the window cannot hold both with a gap between them, the command
    takes the next line whole. It is the thing on the page you copy.
    """
    room = max(_edge(width) - 4, 1)
    if left and right and ui.cells(left) + 3 + ui.cells(right) <= room:
        gap = room - ui.cells(left) - ui.cells(right)
        print(f"    {ui.MUTED}{left}{ui.RST}{' ' * gap}{ui.CODE}{right}{ui.RST}")
        return
    _line(4, left, width, ui.MUTED)
    _line(4, right, width, ui.CODE)


def _clock(stamp: str) -> str:
    """HH:MM from a tool stamp. Anything else is returned as it arrived."""
    text = (stamp or "").strip()
    if len(text) >= 8 and text[2] == ":" and text[5] == ":":
        return text[:5]
    return text


def _resample(values: list, count: int) -> list:
    """`count` buckets. A bucket keeps its peak, so a spike stays visible."""
    if count <= 0:
        return []
    if not values:
        return [0] * count
    if len(values) == count:
        return list(values)
    out = []
    last = len(values)
    for index in range(count):
        start = index * last // count
        end = max((index + 1) * last // count, start + 1)
        out.append(max(values[start:min(end, last)]))
    return out


def _paint_track(raw: str, hot: str) -> str:
    """A sparkline whose quiet minutes are a visible dot, not a blank."""
    parts: list[str] = []
    buf = ""
    colour: str | None = None

    def flush() -> None:
        nonlocal buf, colour
        if not buf:
            return
        parts.append(f"{colour}{buf}{ui.RST}" if colour else buf)
        buf = ""

    for char in raw:
        glyph, col = ("·", ui.SLATE) if char == " " else (char, hot)
        if col != colour:
            flush()
            colour = col
        buf += glyph
    flush()
    return "".join(parts)


def _meter_span(width: int) -> int:
    """How wide the shared graphic column is at this window.

    Seventeen cells stay free for the caption, which is what "4.00/20 · 16
    left" needs, and the row still ends on the rule.
    """
    return max(4, min(_BINS, _edge(width) - 4 - _LABEL - 2 - 2 - 17))


def _meter(label: str, graphic: str, span: int, options: list[str],
           width: int, colour: str = "") -> None:
    """Label, a fixed graphic, then the first caption that fits beside it."""
    room = max(_edge(width) - (4 + _LABEL + 2 + span + 2), 1)
    shown = _choose(options, room)
    if colour:
        shown = f"{colour}{shown}{ui.RST}"
    print(f"    {ui.MUTED}{label:<{_LABEL}}{ui.RST}  {graphic}  {shown}")


def _render_now(now: dict, width: int, inner: int) -> None:
    """The session running now: who it is, then what the last ten minutes
    and today's budget look like, on one shared column."""
    width = min(width, max(inner + 4, 16))
    title = now.get("summary") or now["id"][:8]
    print(f"  {ui.MINT}●{ui.RST} {ui.BOLD}"
          f"{ui._fit(title, max(_edge(width) - 4, 1))}{ui.RST}")
    command = f"cs resume {now['id'][:8]}"
    room = max(_edge(width) - 4, 1)
    # Share the line only when the facts are whole. A clipped repo with the
    # command jammed against it reads as a broken row, so the command drops
    # to the next line and the facts keep the width.
    facts = _session_facts(now, 10_000)
    if room - ui.cells(command) - 3 >= ui.cells(facts):
        _masthead(facts, command, width)
    else:
        _line(4, _session_facts(now, max(width - 4, 1)), width, ui.MUTED)
        _line(4, command, width, ui.CODE)
    room = max(width - 4, 1)
    failure = now.get("last_failure") or {}
    tool = now.get("last_tool") or ""
    tool_clock = _clock(now.get("last_tool_at") or "")
    fail_clock = _clock(failure.get("at") or "") if failure else ""
    same = bool(tool and failure.get("tool") == tool and fail_clock == tool_clock)
    if tool and not same:
        plain = f"{tool} · {tool_clock}" if tool_clock else tool
        if ui.cells(plain) <= room:
            tail = f"{ui.MUTED} · {tool_clock}{ui.RST}" if tool_clock else ""
            print(f"    {ui.CODE}{tool}{ui.RST}{tail}")
        else:
            _line(4, plain, width, ui.MUTED)
    if failure.get("tool"):
        detail = f"{failure['tool']} · {fail_clock}" if fail_clock else failure["tool"]
        _line(4, f"failed · {detail}", width, ui.ROSE)
    print()
    span = _meter_span(width)
    bins = _resample(list(now.get("burn") or []), span)
    rate = f"{now.get('burn_per_minute') or 0:,.2f} AIU/min"
    _meter("burn", _paint_track(ui.sparkline(bins) or (" " * span), ui.VIOLET),
           span, [f"{rate} · last 10 min", rate], width)
    spent = (now.get("today_nano_aiu") or 0) / 1e9
    limit = now.get("budget_aiu")
    if limit:
        colour = ui.budget_colour(spent, limit)
        bar = ui.bar(min(spent, limit), limit, span, colour=colour, track=True)
        if spent > limit:
            over = spent - limit
            options = [f"over {over:,.2f} AIU", f"over {over:,.2f}"]
        else:
            left = max(limit - spent, 0)
            options = [
                f"{spent:,.2f} of {limit:g} · {left:,.2f} left",
                f"{spent:,.2f}/{limit:g} · {left:,.0f} left",
                f"{spent:,.2f}/{limit:g}",
            ]
        _meter("today", bar, span, options, width, colour)
    else:
        # No limit means no share to draw. A hairline keeps the caption in
        # the same column as the burn rate, without looking like an empty bar.
        track = f"{ui.SLATE}{'─' * span}{ui.RST}"
        _meter("today", track, span, [
            f"{spent:,.2f} AIU · no limit",
            f"{spent:,.2f} · no limit",
            f"{spent:,.2f} AIU",
        ], width)


def _keep_command(why: str, command: str, room: int) -> str:
    """The reason, then the command. The command survives a narrow window."""
    if not why:
        return ui._fit(command, room)
    both = f"{why} · {command}"
    if ui.cells(both) <= room:
        return both
    glue = f" · {command}"
    if ui.cells(command) >= room or ui.cells(glue) > room:
        return ui._fit(command, room)
    return ui._fit(why, room - ui.cells(glue)) + glue


def _render_pickup(sessions: list, width: int, inner: int) -> None:
    """Up to three sessions worth going back to, each with why and how.

    One line when the title, the reason and the command all fit. Otherwise
    the reason keeps a line of its own and the command follows it, so a
    narrow window never cuts the evidence off mid-word.
    """
    show = sessions[:3]
    print(ui.heading(f"Pick up · {len(show)}", ui.SKY, inner))
    # 7 columns of "    N  " tuck the detail under the title. On a window
    # where that tuck would cut the reason, the detail moves out to the
    # card's own inset so the sentence stays whole and still ends on the rule.
    tucked = max(_edge(width) - 7, 1)
    inset = max(_edge(width) - 4, 1)
    for number, session in enumerate(show, 1):
        title = session.get("summary") or "(untitled)"
        reasons = session.get("reasons") or []
        why = reasons[0]["why"] if reasons else ""
        command = session.get("resume") or f"cs resume {session['id'][:8]}"
        tail = f"{why} · {command}" if why else command
        if ui.cells(title) + 2 + ui.cells(tail) <= tucked:
            print(f"    {ui.SKY}{number}{ui.RST}  {title}  {ui.MUTED}{tail}{ui.RST}")
            continue
        print(f"    {ui.SKY}{number}{ui.RST}  {ui._fit(title, tucked)}")
        if why and ui.cells(f"{why} · {command}") <= tucked:
            print(f"       {ui.MUTED}{why} · {command}{ui.RST}")
        elif why and ui.cells(why) <= inset:
            indent = next(n for n in (7, 6, 5, 4)
                          if ui.cells(why) <= _edge(width) - n)
            pad = " " * indent
            print(f"{pad}{ui.MUTED}{why}{ui.RST}")
            _line(indent, command, width, ui.CODE)
        else:
            # Cut at any inset, so moving out saves nothing and breaks the
            # column the other cards' details sit in.
            _line(7, _keep_command(why, command, tucked), width, ui.MUTED)


def _coloured_bits(bits: list[tuple[str, str]], width: int) -> None:
    """One line of ` · `-joined facts. A warm clause keeps its colour when
    the whole line fits; otherwise the line is muted and clipped."""
    plain = " · ".join(text for text, _colour in bits)
    room = max(_edge(width) - 4, 1)
    if ui.cells(plain) > room or not any(colour for _text, colour in bits):
        _line(4, plain, width, ui.MUTED)
        return
    parts = []
    for index, (text, colour) in enumerate(bits):
        if index:
            parts.append(f"{ui.MUTED} · {ui.RST}")
        parts.append(f"{colour}{text}{ui.RST}" if colour else text)
    print("    " + "".join(parts))


def _clauses(parts: list[str], room: int) -> list[str]:
    """As many clauses as fit, dropped from the end, never cut mid-phrase."""
    chosen = list(parts)
    while chosen and ui.cells(" · ".join(chosen)) > room:
        chosen.pop()
    return chosen


def _render_since(since: dict, width: int, inner: int, show_spend: bool) -> None:
    work: list[str] = []
    findings: list[tuple[str, str]] = []
    if "sessions" in since:
        work.append(_plural(since["sessions"], "session"))
    if show_spend and since.get("nano_aiu"):
        work.append(f"{since['nano_aiu'] / 1e9:,.2f} AIU")
    if "commits" in since or "prs" in since:
        work.append(f"{_plural(since.get('commits', 0), 'commit')} · "
                    f"{_plural(since.get('prs', 0), 'PR')}")
    if "handoffs" in since:
        work.append(_plural(since["handoffs"], "handoff"))
    if "tool_failures" in since:
        findings.append((_plural(since["tool_failures"], "tool failure"), ui.ROSE))
    if "stuck_loops" in since:
        findings.append((_plural(since["stuck_loops"], "stuck loop"), ui.ROSE))
    if not work and not findings:
        return
    print(ui.heading("Since midnight", ui.VIOLET, inner))
    room = max(_edge(width) - 4, 1)
    one = work + [text for text, _colour in findings]
    if ui.cells(" · ".join(one)) <= room:
        _coloured_bits([(text, "") for text in work] + findings, width)
        return
    kept = _clauses(work, room)
    if kept:
        _line(4, " · ".join(kept), width, ui.MUTED)
    kept_findings = []
    for text, colour in findings:
        trial = kept_findings + [(text, colour)]
        if ui.cells(" · ".join(item for item, _c in trial)) <= room:
            kept_findings = trial
    if kept_findings:
        _coloured_bits(kept_findings, width)


def _render_week(week: dict, width: int, inner: int) -> None:
    print(ui.heading("This week", ui.ACCENT, inner))
    change = week.get("change")
    if change is None:
        trend = "no earlier week to compare"
    elif change == 0:
        trend = "level with the week before"
    else:
        trend = f"{'up' if change > 0 else 'down'} {abs(change):.0%} on the week before"
    nano = week.get("nano_aiu") or 0
    spend = f"{ui.fmt_aiu(nano)} AIU"
    if week.get("sessions"):
        spend += f" over {_plural(week['sessions'], 'session')}"
    spend += f" · {trend}"
    by_day = week.get("by_day") or []
    days = [day["nano_aiu"] for day in by_day]
    # One day has no shape. The sentence is the whole story.
    if len(days) < 2:
        _line(4, spend, width)
        return
    span = min(len(days), max(4, width - 4 - 2 - 18))
    values = days if len(days) == span else _resample(days, span)
    graphic = _paint_track(ui.sparkline(values) or (" " * span), ui.VIOLET)
    caption = _clauses(spend.split(" · "), max(_edge(width) - 4 - span - 3, 1))
    print(f"    {graphic}   {' · '.join(caption) or ''}")
    # A letter under each bar says which day it is — only when every day has
    # a bar of its own, since a resampled track has no one day under a cell.
    if span == len(days):
        letters = "".join(_weekday_letter(day.get("day", "")) for day in by_day)
        print(f"    {ui.MUTED}{letters}{ui.RST}")


def _weekday_letter(day: str) -> str:
    try:
        return datetime.fromisoformat(day).strftime("%a")[0]
    except ValueError:
        return " "


def _render_today(data: dict) -> None:
    """One screen. The live session is the page; the day and the week follow
    only when they have something to say."""
    width = min(shutil.get_terminal_size().columns, 100)
    inner = max(width - 4, 16)
    print()
    print(ui.rule(inner, "Today", note="live" if data.get("now") else ""))
    print()
    if not data:
        _note("Nothing recorded yet, and nothing waiting.", inner)
        print()
        return
    if "now" in data:
        _render_now(data["now"], width, inner)
    else:
        _line(4, "Nothing running.", width, ui.MUTED)
    print()
    pickup = data.get("pick_up") or []
    if pickup:
        _render_pickup(pickup, width, inner)
        print()
    if data.get("since_midnight"):
        _render_since(data["since_midnight"], width, inner, "now" not in data)
        print()
    if data.get("this_week"):
        week = data["this_week"]
        if week.get("sessions") or week.get("nano_aiu") or week.get("by_day"):
            _render_week(week, width, inner)
            print()


def _repo_match(row: tuple, repo: str) -> bool:
    """Whether a session belongs to `repo` — '.' is the directory you are in."""
    cwd, name = (row[4] or ""), (row[3] or "")
    if repo == ".":
        here = os.getcwd()
        return bool(cwd) and (cwd == here or cwd.startswith(here.rstrip("/") + "/")
                              or bool(name) and name.rsplit("/", 1)[-1]
                              == os.path.basename(here))
    wanted = repo.lower()
    return wanted in name.lower() or wanted in cwd.lower()



# ── Saved searches ───────────────────────────────────────────────────

def cmd_saved(name: str | None = None) -> bool:
    """List saved searches, or run one by name."""
    saved = ui.saved_searches()
    if name:
        match = saved.get(name) or next(
            (term for key, term in saved.items() if key.lower() == name.lower()), None)
        if match is None:
            print(f"error: no saved search named '{name}' — 'cs saved' lists "
                  f"them", file=sys.stderr)
            sys.exit(1)
        return cmd_search(match)
    text = _capture(lambda: _render_saved(saved))
    if _HOME_ACTIVE:
        return _page(text)
    print(text)
    return False


def _render_saved(saved: dict[str, str]) -> None:
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    print()
    print(ui.rule(inner, f"Saved searches · {len(saved)}"))
    print()
    if not saved:
        _note("None yet. Save one with 'cs search --save <name> <words>'.", inner)
        print()
        return
    for name, term in saved.items():
        print(f"    {ui.SKY}{ui._fit(_clean(name), 18):<18}{ui.RST} "
              f"{ui._fit(_clean(term), max(inner - 24, 8))}")
    print()
    _hint("cs saved <name> — run one · on the home screen, Saved searches "
          "picks one", inner)
    print()


def cmd_saved_menu() -> bool:
    """The home row: pick a saved search and run it live."""
    saved = ui.saved_searches()
    if not saved:
        return cmd_saved()
    names = list(saved)
    try:
        chosen = _curses_wrapper(
            _pick, "Saved searches",
            [f"{_clean(name)} — {_clean(saved[name])}" for name in names])
    except Exception:  # noqa: BLE001 — no usable terminal: list them instead
        return cmd_saved()
    if chosen is None:
        return True
    return cmd_search(saved[names[chosen]])


def _pick(screen, title: str, options: list[str]) -> int | None:
    """A one-column chooser: arrows or the mouse move, Enter picks, Esc leaves."""
    import curses

    screen.keypad(True)
    theme = ui.tui_theme(curses)
    try:
        curses.curs_set(0)
        screen.bkgd(" ", theme["background"])
    except curses.error:
        pass
    mouse = _enable_mouse(curses, motion=True)
    cursor = offset = 0
    last_click = [0.0, -1]
    pending: list[int] = []
    screen.timeout(-1)
    try:
        while True:
            screen.erase()
            height, width = screen.getmaxyx()
            _addstr(screen, 0, 0, f"◆  {title}", width, theme["title"])
            visible = max(height - 3, 1)
            offset = min(max(offset, cursor - visible + 1), cursor)
            for row, index in enumerate(range(offset, min(offset + visible,
                                                          len(options))), 2):
                on = index == cursor
                if on:
                    _addstr(screen, row, 0, " " * width, width, theme["cursor"])
                _addstr(screen, row, 2, ui.trunc(options[index], width - 3),
                        width - 3, theme["cursor"] if on else theme["summary"])
            hint = " ↑↓ choose · ↵ run the search · Esc back "
            _addstr(screen, height - 1, 0,
                    hint if ui.cells(hint) <= width else " ↑↓ · ↵ · Esc ",
                    width, theme["status"])
            screen.refresh()
            try:
                key = pending.pop(0) if pending else screen.getch()
            except KeyboardInterrupt:
                return None
            event = _mouse_event(screen, curses, key, last_click, pending)
            if event:
                kind, _x, y = event
                if kind in ("move", "click", "double") and 2 <= y < 2 + visible:
                    if offset + y - 2 < len(options):
                        cursor = offset + y - 2
                        if kind == "double":
                            return cursor
                elif kind == "wheel-up":
                    cursor = max(cursor - 1, 0)
                elif kind == "wheel-down":
                    cursor = min(cursor + 1, len(options) - 1)
                continue
            if key in (27, ord("q"), ord("Q")):
                return None
            if key in (10, 13, curses.KEY_ENTER):
                return cursor
            if key == curses.KEY_UP:
                cursor = max(cursor - 1, 0)
            elif key == curses.KEY_DOWN:
                cursor = min(cursor + 1, len(options) - 1)
    finally:
        if mouse:
            _disable_mouse()


# ── Clean-up ─────────────────────────────────────────────────────────

_CLEANUP_PINS = 14
_CLEANUP_WIP = 7


def _cleanup_data(days: int = _CLEANUP_PINS) -> dict:
    now = datetime.now(timezone.utc)

    def age(active: str) -> int | None:
        try:
            when = datetime.fromisoformat(_stamp(active)[:16]).replace(
                tzinfo=timezone.utc)
        except ValueError:
            return None
        return (now - when).days

    conn = db.connect()
    try:
        rows = {row[0]: row for row in db.recent_sessions(conn, 0)}
        opened = signals.open_handoffs(conn)
    finally:
        conn.close()
    pins, wip, handoffs = [], [], []
    for sid in ui.pinned_ids():
        row = rows.get(sid)
        if row is None:
            pins.append({"id": sid, "summary": "", "last_active": None,
                         "quiet_days": None, "why": "no longer in the store",
                         "command": f"cs unpin {sid}"})
            continue
        quiet = age(row[1])
        if quiet is not None and quiet >= days:
            pins.append({**_session_fields(row), "quiet_days": quiet,
                         "why": f"pinned, quiet {quiet} days",
                         "command": f"cs unpin {sid[:8]}"})
    for sid in _tagged(_WIP):
        row = rows.get(sid)
        quiet = age(row[1]) if row else None
        if row and quiet is not None and quiet >= _CLEANUP_WIP:
            wip.append({**_session_fields(row), "quiet_days": quiet,
                        "why": f"tagged wip, quiet {quiet} days",
                        "command": f"cs untag {sid[:8]} {_WIP}"})
    for handoff in opened:
        quiet = age(handoff["active"])
        if quiet is not None and quiet >= _CLEANUP_WIP:
            handoffs.append({"id": handoff["id"], "summary": _clean(handoff["summary"]),
                             "last_active": handoff["active"], "quiet_days": quiet,
                             "why": _clean(handoff["evidence"]),
                             "command": f"cs handoff {handoff['id'][:8]}"})
    return {"pin_days": days, "wip_days": _CLEANUP_WIP, "pins": pins,
            "wip": wip, "handoffs": handoffs}


def cmd_cleanup(days: int = _CLEANUP_PINS) -> bool:
    """What has gone stale. Suggestions only — nothing is ever removed."""
    return _page(_capture(lambda: _render_cleanup(_cleanup_data(days))))


def _render_cleanup(data: dict) -> None:
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    print()
    print(ui.rule(inner, "Clean-up"))
    print()
    groups = (("Pins gone quiet", data["pins"], ui.AMBER),
              ("Work in progress gone quiet", data["wip"], ui.AMBER),
              ("Handoffs nobody picked up", data["handoffs"], ui.SKY))
    if not any(items for _t, items, _c in groups):
        _note(f"Nothing stale: no pin quiet {data['pin_days']}+ days, no wip "
              f"quiet {data['wip_days']}+ days, no handoff left waiting.", inner)
        print()
        return
    # Only unpin and untag change anything. A handoff has nothing to remove,
    # so its row is something to look at, not a command to copy.
    commands = [item["command"] for item in data["pins"] + data["wip"]]
    lead = "Suggestions only. cs never unpins, untags or deletes anything on its own."
    if commands:
        lead += " The commands are together at the end, to copy."
    _note(lead, inner, indent=4)
    print()
    for title, items, colour in groups:
        if not items:
            continue
        print(ui.heading(f"{title} · {len(items)}", colour, inner))
        _table(
            [("id", "session", "<"), ("quiet", "quiet", ">"),
             ("summary", "summary", "<"), ("why", "why", "<")],
            [{"id": (item["id"][:8], ui.SKY),
              "quiet": (_quiet(item.get("quiet_days")), ui.MUTED),
              "summary": (item["summary"] or "(no longer in the store)", ""),
              "why": (_cleanup_why(item["why"]), ui.MUTED)}
             for item in items[:_CLEANUP_ROWS]],
            inner, {"quiet": 5}, [("why", 30), ("id", 8)], least=20)
        if len(items) > _CLEANUP_ROWS:
            _note(f"+{len(items) - _CLEANUP_ROWS} more · cs cleanup --json lists "
                  "every one", inner, indent=4)
            print()
    if data["handoffs"]:
        _hint("cs handoff <session> — the chain a handoff belongs to", inner)
    if commands:
        print()
        print(ui.heading("Commands", ui.CODE, inner))
        for command in commands:
            print(f"    {ui.CODE}{ui._fit(command, inner - 4)}{ui.RST}")
    print()


# Rows per clean-up group. Past this the page is a scroll, not a suggestion.
_CLEANUP_ROWS = 10


def _quiet(days: int | None) -> str:
    return "—" if days is None else f"{days}d"


def _cleanup_why(why: str) -> str:
    """The reason without the part the group's heading already says."""
    for tail in ("; no later session picked it up", "; no later session opened it"):
        if why.endswith(tail):
            return why[: -len(tail)]
    return why
