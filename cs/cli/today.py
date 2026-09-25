"""Day-to-day views: what to pick up, what the day and week came to, and finding
past work — next up, end of day, weekly review, similar work, my asks, saved
searches, file history and clean-up.

As in `evidence.py`, each reading is a `_*_data` function returning plain,
already-masked data (what `--json` hands back) and the page draws that same
data. Every reason a session is put in front of you is printed with it.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
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
    _resolve_ref,
    _save_index,
    _user_text,
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
        reasons[sid].append(("stuck", f"stuck: {_clean(loop['tool'])} failed "
                                      f"{loop['run']}× in a row · turns {turns}"))
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
        print(f"  {ui.SKY}{n:>3}{ui.RST}  {ui.BOLD}"
              f"{ui._fit(session['summary'] or '(untitled)', inner - 8)}{ui.RST}")
        for reason in session["reasons"]:
            colour = ui.ROSE if reason["kind"] in ("ended", "stuck") else ui.AMBER
            print(f"       {colour}·{ui.RST} {ui._fit(reason['why'], inner - 9)}")
        print(f"       {ui.MUTED}cs resume {n}{ui.RST}")
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
    print(ui.field("sessions", f"{len(data['sessions']):,}"))
    colour = ui.ROSE if data["over_budget"] else ""
    print(ui.field("spend", f"{colour}{_spend_line(data['nano_aiu'], data['budget_aiu'])}"
                            f"{ui.RST if colour else ''}"))
    print(ui.field("shipped", f"{_plural(len(data['commits']), 'commit')} · "
                              f"{_plural(len(data['prs']), 'PR')}"))
    if data["tool_calls"]:
        print(ui.field("tools", f"{data['tool_calls']:,} calls · "
                                f"{data['tool_failures']} failed · "
                                f"{_plural(data['stuck_loops'], 'stuck loop')}"))
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
    _hint("cs eod --md — the same as Markdown to paste · cs next — what to pick "
          "up tomorrow", inner)
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
            "turns": session["turns"],
            "nano_aiu": session["nano_aiu"],
            "burn_per_minute": round(session["burn_per_minute"], 4),
            "today_nano_aiu": session["today_nano_aiu"],
            "budget_aiu": session["budget"],
            "left_aiu": None if session["left"] is None else round(session["left"], 4),
            "last_tool": session["last_tool"] or None,
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


def _render_today(data: dict) -> None:
    """One screenful. Sections with nothing to say are not drawn."""
    width = min(shutil.get_terminal_size().columns, 100)
    inner = max(width - 4, 16)
    print()
    print(ui.rule(inner, "Today"))
    if not data:
        _note("Nothing recorded yet, and nothing waiting.", inner)
        print()
        return
    if "now" in data:
        now = data["now"]
        title = now["summary"] or now["id"][:8]
        print(ui.heading("Now", ui.MINT, inner))
        print(f"    {ui.BOLD}{ui._fit(title, inner - 4)}{ui.RST}")
        bits = [f"{now['burn_per_minute']:,.2f} AIU/min",
                _spend_line(now["today_nano_aiu"], now["budget_aiu"]) + " today"]
        if now.get("last_tool"):
            bits.append(now["last_tool"])
        _note(" · ".join(bits), inner, indent=4)
    pickup = data.get("pick_up") or []
    if pickup:
        show = pickup[:3]
        print(ui.heading(f"Pick up · {len(show)}", ui.SKY, inner))
        for index, session in enumerate(show, 1):
            why = session["reasons"][0]["why"] if session["reasons"] else ""
            print(f"    {ui.SKY}{index}{ui.RST}  "
                  f"{ui._fit(session['summary'] or '(untitled)', inner - 8)}")
            if why:
                _note(why, inner, indent=7)
            print(f"       {ui.MUTED}cs resume {index}{ui.RST}")
    since = data.get("since_midnight")
    if since:
        bits = []
        if "sessions" in since:
            bits.append(_plural(since["sessions"], "session"))
        if "commits" in since or "prs" in since:
            bits.append(f"{_plural(since.get('commits', 0), 'commit')} · "
                        f"{_plural(since.get('prs', 0), 'PR')}")
        if "handoffs" in since:
            bits.append(_plural(since["handoffs"], "handoff"))
        if "tool_failures" in since:
            bits.append(_plural(since["tool_failures"], "tool failure"))
        print(ui.heading("Since midnight", ui.VIOLET, inner))
        _note(" · ".join(bits), inner, indent=4)
    week = data.get("this_week")
    if week and (week["sessions"] or week["nano_aiu"] or week["by_day"]):
        print(ui.heading("This week", ui.ACCENT, inner))
        change = week["change"]
        trend = ("no earlier week to compare" if change is None else
                 f"{'up' if change > 0 else 'down'} {abs(change):.0%}")
        spend = f"{week['nano_aiu'] / 1e9:,.2f} AIU · {trend}"
        spark = ui.sparkline([day["nano_aiu"] for day in week["by_day"]]).rstrip()
        if spark and inner >= 28:
            room = max(6, min(len(spark), inner - 6 - len(spend)))
            print(f"    {ui.VIOLET}{spark[-room:]}{ui.RST}  "
                  f"{ui._fit(spend, inner - room - 6)}")
        else:
            _note(spend, inner, indent=4)
    print()


# ── Similar work ─────────────────────────────────────────────────────

_SIMILAR_STOP = frozenset(
    "a an the and or to of for in on with from this that your you we our it is "
    "are was were be as at by if then else not into over about just like make "
    "using use can will would should could please also than them they their "
    "its have has had but not for all any out get got let".split()
)


def _opening_terms(text: str) -> set[str]:
    """Distinctive words of an ask, after masking. Secrets do not become terms."""
    cleaned = _user_text(text).lower()
    return {word for word in re.findall(r"[a-z][a-z0-9_-]{3,}", cleaned)
            if word not in _SIMILAR_STOP and "redact" not in word}


def _similar_data(ref: str) -> dict:
    """Up to ten sessions that share files, repository or opening terms.

    The evidence on each row is the overlap that put it here. A session that
    shares none of those is not similar, however many times the words appear
    in a search.
    """
    source = _resolve_ref(ref)
    conn = db.connect()
    try:
        rows = {row[0]: row for row in db.recent_sessions(conn, 0)}
        mine = rows.get(source)
        if mine is None:
            return {"session": source, "count": 0, "sessions": []}
        files = [path for path, _tool in db.session_files(conn, source)]
        prompts = db.opening_prompts(conn, list(rows), first_only=True)
        shared: dict[str, set[str]] = defaultdict(set)
        if files and db.has_files(conn):
            for chunk in db._chunks(files):
                marks = ",".join("?" * len(chunk))
                for sid, path in conn.execute(
                        f"SELECT session_id, file_path FROM session_files "
                        f"WHERE file_path IN ({marks})", chunk):
                    if sid != source and isinstance(path, str):
                        shared[sid].add(path)
    finally:
        conn.close()
    openings = {sid: _opening_terms(pairs[0][1]) if pairs else set()
                for sid, pairs in prompts.items()}
    frequency: Counter = Counter()
    for terms in openings.values():
        frequency.update(terms)
    ceiling = max(3, len(rows) // 3)
    distinctive = {term for term in openings.get(source, set())
                   if frequency[term] <= ceiling}
    repo = mine[3] or ""
    scored = []
    for sid, row in rows.items():
        if sid == source:
            continue
        reasons = []
        score = 0
        if repo and row[3] == repo:
            score += 4
            reasons.append("same repository")
        overlap = shared.get(sid) or set()
        if overlap:
            score += min(9, 3 * len(overlap))
            reasons.append(_plural(len(overlap), "shared file"))
        words = sorted(distinctive & openings.get(sid, set()))
        if words:
            score += min(6, len(words))
            reasons.append("opening: " + ", ".join(words[:4]))
        if score <= 0:
            continue
        scored.append({**_session_fields(row), "score": score,
                       "evidence": " · ".join(reasons)})
    scored.sort(key=lambda item: (-item["score"], item["last_active"]), reverse=False)
    scored.sort(key=lambda item: -item["score"])
    top = scored[:10]
    return {"session": source, "count": len(top), "sessions": top}


def cmd_similar(ref: str) -> bool:
    """Sessions that share a chosen session's files, repository or opening ask."""
    return _page(_capture(lambda: _render_similar(_similar_data(ref))))


def _render_similar(data: dict) -> None:
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    print()
    print(ui.rule(inner, "Similar work"))
    print()
    sessions = data["sessions"]
    if not sessions:
        _note("Nothing else shares that session's repository, files or the "
              "distinctive words of its opening ask.", inner)
        print()
        return
    _headline(f"{_plural(len(sessions), 'session')} like "
              f"{data['session'][:8]}, closest first", inner)
    print()
    index = {}
    for number, session in enumerate(sessions, 1):
        index[number] = session["id"]
        print(f"  {ui.SKY}{number:>3}{ui.RST}  {ui.BOLD}"
              f"{ui._fit(session['summary'] or '(untitled)', inner - 8)}{ui.RST}")
        _note(session["evidence"], inner, indent=7)
        print(f"       {ui.MUTED}cs resume {number}{ui.RST}")
    _save_index(index)
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
    _note("Suggestions only. cs never unpins, untags or deletes anything on its "
          "own. The commands are together at the end, to copy.", inner)
    print()
    commands = []
    for title, items, colour in groups:
        if not items:
            continue
        print(ui.heading(f"{title} · {len(items)}", colour, inner))
        for item in items:
            print(f"    {ui._fit(item['summary'] or item['id'][:8], inner - 4)}")
            print(f"      {ui.MUTED}{ui._fit(item['why'], inner - 6)}{ui.RST}")
            commands.append(item["command"])
        print()
    if commands:
        print(ui.heading("Commands", ui.CODE, inner))
        for command in commands:
            print(f"    {ui.CODE}{ui._fit(command, inner - 4)}{ui.RST}")
        print()
