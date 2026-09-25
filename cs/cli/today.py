"""Day-to-day views: what to pick up, what the day and week came to, and finding
past work — next up, end of day, weekly review, similar work, my asks, saved
searches, file history and clean-up.

As in `evidence.py`, each reading is a `_*_data` function returning plain,
already-masked data (what `--json` hands back) and the page draws that same
data. Every reason a session is put in front of you is printed with it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .. import (
    db,
    events,
    practice,
    redact,
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
    _hit_text,
    _mouse_event,
    _note,
    _page,
    _save_index,
    _short_path,
    _user_text,
    _visible,
    _when,
    _window_label,
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
from .listing import _interactive_listing, _render_listing, cmd_search

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


# ── Similar work ─────────────────────────────────────────────────────

def _similar_rows(term: str) -> tuple[list[tuple], dict[str, dict]]:
    """The search's rows, re-ranked shipped-first, and each one's reading."""
    conn = db.connect()
    try:
        rows, hits = db.search(conn, term)
        refs = db.refs_by_session(conn, [row[0] for row in rows])
    finally:
        conn.close()

    def shipped(row: tuple) -> bool:
        found = refs.get(row[0]) or {}
        return bool(found.get("commit") or found.get("pr"))

    # sorted() is stable, so the search's own ranking survives within each half.
    ranked = sorted(rows, key=lambda row: 0 if shipped(row) else 1)
    readings = {}
    for row in ranked:
        made = refs.get(row[0]) or {"commit": 0, "pr": 0}
        item = {**_session_fields(row), "commits": made["commit"],
                "prs": made["pr"], "outcome": _outcome(made)}
        if row[0] in hits:
            item["match"] = _hit_text(*hits[row[0]])
        readings[row[0]] = item
    return ranked, readings


def _similar_data(term: str) -> dict:
    _rows, readings = _similar_rows(term)
    return {"term": term, "count": len(readings), "sessions": list(readings.values())}


def cmd_similar(term: str) -> bool:
    """Sessions like this one, the ones that shipped something first."""
    ranked, readings = _similar_rows(term)
    ordered = _with_assets(ranked)
    hits = {sid: ("outcome", r["outcome"] + (f" · {r['match']}" if r.get("match")
                                             else ""))
            for sid, r in readings.items()}
    title = (f"Similar work · '{term}' · {_plural(len(ordered), 'session')} · "
             f"shipped first")
    if sys.stdin.isatty() and sys.stdout.isatty():
        return _interactive_listing(ordered, title, show_all=True,
                                    default_sort="relevance", hits=hits, term=term)
    _render_listing(ordered, title, show_all=True, default_sort="relevance",
                    hits=hits, term=term)
    return False


# ── My asks ──────────────────────────────────────────────────────────

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


def _asks_data(days: int, repo: str | None = None) -> dict:
    conn = db.connect()
    try:
        rows = _visible(db.recent_sessions(conn, days), False)
        if repo:
            rows = [row for row in rows if _repo_match(row, repo)]
        ids = [row[0] for row in rows]
        took_up = {h["id"] for h in signals.handoffs(conn)
                   if h["role"] in ("received", "both")}
        # Every prompt only where a handoff was picked up; the opener alone
        # everywhere else.
        prompts = db.opening_prompts(conn, [i for i in ids if i not in took_up],
                                     first_only=True)
        prompts.update(db.opening_prompts(conn, [i for i in ids if i in took_up]))
        refs = db.refs_by_session(conn, ids)
    finally:
        conn.close()
    asks = []
    for row in rows:
        mine = [(index, _user_text(text)) for index, text in prompts[row[0]]]
        mine = [(index, text) for index, text in mine if text.strip()]
        if not mine:
            continue
        chosen = [(mine[0][0], "opening", mine[0][1])]
        if row[0] in took_up:
            picked = next(((i, t) for i, t in mine[1:]
                           if signals._ASK_READ.search(t)), None)
            if picked:
                chosen.append((picked[0], "after handoff", picked[1]))
        for index, kind, text in chosen:
            # `_user_text` has already masked it (in `_plain`); masking a
            # long prompt twice was most of this view's time.
            asks.append({**_session_fields(row), "turn": index, "kind": kind,
                         "ask": redact.one_line(text[:300]),
                         "ask_full": text.strip(),
                         "outcome": _outcome(refs.get(row[0]))})
    return {"window_days": days, "repo": repo, "count": len(asks), "asks": asks}


def _clipboard(text: str) -> str | None:
    """Put text on the clipboard with whichever tool this machine has."""
    for tool, extra in (("pbcopy", []), ("wl-copy", []),
                        ("xclip", ["-selection", "clipboard"])):
        if shutil.which(tool):
            try:
                subprocess.run([tool, *extra], input=text, text=True,
                               check=False, timeout=5)
            except (OSError, subprocess.SubprocessError):
                return None
            return tool
    return None


def _copy_ask(text: str) -> None:
    """Copy a masked ask, or print it when there is no clipboard tool."""
    tool = _clipboard(text)
    print()
    if tool:
        print(f"  {ui.MINT}copied the ask ({len(text):,} characters) with "
              f"{tool}{ui.RST}")
    else:
        print(f"  {ui.MUTED}no clipboard tool found (pbcopy, wl-copy or xclip) — "
              f"here it is:{ui.RST}")
        print()
        for line in text.splitlines() or [""]:
            print(f"    {line}")
    print()


def cmd_asks(days: int = 30, repo: str | None = None) -> bool:
    """What you opened each session by asking for — one line each."""
    data = _asks_data(days, repo)
    if data["asks"] and sys.stdin.isatty() and sys.stdout.isatty():
        conn = db.connect()
        try:
            rows = {row[0]: row for row in db.recent_sessions(conn, days)}
        finally:
            conn.close()
        first: dict[str, dict] = {}
        extra: Counter = Counter()
        full: dict[str, list[str]] = defaultdict(list)
        for ask in data["asks"]:
            full[ask["id"]].append(ask["ask_full"])
            if ask["id"] in first:
                extra[ask["id"]] += 1
            else:
                first[ask["id"]] = ask
        listed = []
        for sid, ask in first.items():
            row = rows.get(sid)
            if row:
                listed.append((row[0], row[1], ask["ask"], *row[3:]))
        hits = {sid: ("ask", f"turn {ask['turn']} · {ask['outcome']}"
                      + (f" · +{extra[sid]} after handoff" if extra[sid] else ""))
                for sid, ask in first.items()}
        copies = {sid: "\n\n".join(texts) for sid, texts in full.items()}
        title = (f"My asks · {_window_label(days)} · "
                 f"{_plural(len(listed), 'session')} · c copies an ask")
        return _interactive_listing(_with_assets(listed), title, show_all=True,
                                    hits=hits, copy=copies)
    return _page(_capture(lambda: _render_asks(data)))


def _render_asks(data: dict) -> None:
    inner = _frame("My asks", data["window_days"])
    asks = data["asks"]
    if not asks:
        where = f" in '{data['repo']}'" if data["repo"] else ""
        _note(f"No opening request recorded{where} in this window.", inner)
        print()
        return
    _headline(f"{_plural(len(asks), 'ask')}", inner)
    print()
    _table([("turn", "turn", ">"), ("aiu", "AIU", ">"), ("outcome", "outcome", "<"),
            ("ask", "ask", "<")],
           [{"turn": (str(a["turn"]), ui.MUTED),
             "aiu": (ui.fmt_aiu(a["nano_aiu"]), ui.VIOLET),
             "outcome": (a["outcome"] if a["outcome"] != "no commit or PR" else "—",
                         ui.MINT if a["outcome"] != "no commit or PR" else ui.MUTED),
             "ask": (("↪ " if a["kind"] == "after handoff" else "") + a["ask"], "")}
            for a in asks],
           inner, {"turn": 4}, [("outcome", 16), ("aiu", 7)], flex="ask", least=16)
    _hint("cs asks --json has each ask in full, masked · in a terminal, c "
          "copies one", inner)
    print()


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


# ── File history ─────────────────────────────────────────────────────

_EDITING_TOOLS = ("edit", "create", "apply_patch", "write", "str_replace",
                  "str_replace_editor", "insert", "multi_edit")


def _agents_for(session_id: str, paths: set[str],
                turn_times: list[tuple[int, str]]) -> dict[tuple[str, int | None], str]:
    """Which agent touched each path, by turn: (path, turn) → main | sub-agent.

    Read from the session's event log on demand — never cached, because the
    arguments it compares against are text. Only the path is compared, and
    only the verdict is kept.
    """
    found: dict[tuple[str, int | None], str] = {}
    for event in events.iter_events(session_id, ["tool.execution_start"]):
        data = event["data"]
        if data.get("toolName") not in _EDITING_TOOLS:
            continue
        arguments = data.get("arguments")
        if not isinstance(arguments, dict):
            continue
        target = next((arguments[key] for key in ("path", "filePath", "file_path")
                       if isinstance(arguments.get(key), str)), "")
        if target not in paths:
            continue
        stamp = event.get("timestamp")
        turn = events.turn_of(stamp[:19] if isinstance(stamp, str) else "",
                              turn_times)
        who = "sub-agent" if event.get("agentId") else "main"
        found.setdefault((target, turn), who)
        found.setdefault((target, None), who)
    return found


def _file_history_data(pattern: str) -> dict:
    conn = db.connect()
    try:
        touches = db.file_touches(conn, pattern)
        ids = list(dict.fromkeys(sid for _p, sid, *_rest in touches))
        times = db.turn_times(conn, ids)
        prompts = {(sid, turn): db.turn_prompt(conn, sid, turn)
                   for _p, sid, _tool, turn, _a in touches if turn is not None}
    finally:
        conn.close()
    paths_by_session: dict[str, set[str]] = defaultdict(set)
    for path, sid, *_rest in touches:
        paths_by_session[sid].add(path)
    agents = {sid: _agents_for(sid, paths, times[sid])
              for sid, paths in list(paths_by_session.items())[:_TOP * 2]}
    files: dict[str, list[dict]] = defaultdict(list)
    for path, sid, tool, turn, active in touches:
        who = agents.get(sid, {})
        agent = who.get((path, turn)) or who.get((path, None)) or "—"
        files[path].append({
            "id": sid, "last_active": active, "turn": turn, "tool": _clean(tool),
            "agent": agent,
            "turn_summary": _clean(_user_text(prompts.get((sid, turn), ""))),
        })
    return {"pattern": pattern, "files": [
        {"path": _clean(_short_path(path, "")), "touches": rows}
        for path, rows in files.items()]}


def cmd_file_history(pattern: str) -> bool:
    """Every recorded touch of a file: session, agent, turn and what was asked."""
    return _page(_capture(lambda: _render_file_history(_file_history_data(pattern))))


def _render_file_history(data: dict) -> None:
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    print()
    print(ui.rule(inner, f"File history · '{_clean(data['pattern'])}'"))
    print()
    if not data["files"]:
        _note("No session recorded touching a file like that.", inner)
        print()
        return
    numbers: dict[str, int] = {}
    for file in data["files"]:
        print(ui.heading(ui._fit(file["path"], inner - 4), ui.MINT, inner))
        for touch in file["touches"]:
            numbers.setdefault(touch["id"], len(numbers) + 1)
        _table([("n", "#", ">"), ("when", "when", "<"), ("turn", "turn", ">"),
                ("tool", "tool", "<"), ("agent", "by", "<"),
                ("summary", "asked", "<")],
               [{"n": (str(numbers[t["id"]]), ui.SKY),
                 "when": (_when(t["last_active"]), ui.MUTED),
                 "turn": ("—" if t["turn"] is None else str(t["turn"]), ""),
                 "tool": (t["tool"] or "touched", ui.CODE),
                 "agent": (t["agent"], ui.VIOLET if t["agent"] == "sub-agent"
                           else ui.MUTED),
                 "summary": (t["turn_summary"] or "—", "")}
                for t in file["touches"]],
               inner, {"n": 3}, [("when", 11), ("tool", 8), ("agent", 9),
                                 ("turn", 4)])
    _save_index({n: sid for sid, n in numbers.items()})
    _hint("cs read N --turn T — the turn itself · by: who made the edit, from "
          "the session's event log", inner)
    print()


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
          "own; each line ends in the command that would.", inner)
    print()
    for title, items, colour in groups:
        if not items:
            continue
        print(ui.heading(f"{title} · {len(items)}", colour, inner))
        for item in items:
            print(f"    {ui._fit(item['summary'] or item['id'][:8], inner - 4)}")
            print(f"      {ui.MUTED}{ui._fit(item['why'], inner - 6)}{ui.RST}")
            print(f"      {ui.CODE}{item['command']}{ui.RST}")
        print()
