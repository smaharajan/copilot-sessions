"""Evidence views: what the event log and the last billed call recorded.

Tool failures, stuck loops, sub-agents, model switches and unclean endings.
Each has two halves: a `_*_data` function that returns the reading as plain,
already-masked data — which is exactly what `--json` hands back — and a
renderer that draws that same reading. One computation, so the page and the
export cannot disagree.

Every verdict here carries the thing that triggered it: a loop names its
tool, how many failures in a row and at which turns; an unclean ending names
the finish reason and the turn it happened on.
"""

from __future__ import annotations

import shutil
import statistics
from collections import Counter, defaultdict

from .. import (
    context,
    db,
    events,
    redact,
    ui,
)
from ._common import (
    _capture,
    _cell,
    _extra_gaps,
    _fit_columns,
    _head_rule,
    _hint,
    _note,
    _page,
    _row,
    _save_index,
    _thousands,
    _visible,
    _when,
    _window_label,
)

# The finish reasons that mean the model or the service stopped the session,
# rather than the session answering or being stopped by you.
_UNCLEAN = {"error": "error", "length": "length", "content_filter": "content filter",
            "": "unknown"}

# How many rows the ranked tables keep. A table of every session is a
# listing, and `cs recent` is already that.
_TOP = 10


def _clean(text) -> str:
    """Stored or logged text, masked and on one line. Every string goes here."""
    return redact.one_line(redact.redact(text if isinstance(text, str) else ""))


def _rate(failures: int, calls: int) -> float:
    return failures / calls if calls else 0.0


def _window(days: int) -> tuple[dict[str, tuple], dict[str, dict]]:
    """The sessions in the window, and the digests of the ones with a log.

    The window is the store's own "last active", as every other report uses,
    and only those sessions' logs are read.
    """
    conn = db.connect()
    try:
        rows = _visible(db.recent_sessions(conn, days), show_all=False)
    finally:
        conn.close()
    meta = {row[0]: row for row in rows}
    return meta, events.digests(list(meta))


def _session_fields(row: tuple) -> dict:
    sid, active, summary, repo, _cwd, turns, nano = row[:7]
    return {"id": sid, "last_active": active, "summary": _clean(summary),
            "repository": _clean(repo) or None, "turns": turns, "nano_aiu": nano}


def _turn_range(first: int | None, last: int | None) -> str:
    if first is None and last is None:
        return "—"
    if first == last or last is None:
        return str(first if first is not None else last)
    if first is None:
        return str(last)
    return f"{first}–{last}"


def _number(rows: list[dict]) -> None:
    """Hand the table's sessions a #N, as a listing does, so `cs show 3` works."""
    index = {}
    for n, row in enumerate(rows, 1):
        row["n"] = n
        index[n] = row["id"]
    if index:
        _save_index(index)


def _table(columns: list[tuple[str, str, str]], rows: list[dict],
           inner: int, fixed: dict[str, int], optional: list[tuple[str, int]],
           flex: str = "summary", least: int = 16) -> None:
    """A ruled table in the house shape, sized to the window.

    `fixed` columns always show; `optional` give way, first first, until the
    flexible column keeps `least` cells. Rows are {column: (text, colour)}.
    """
    cost = sum(span + 1 for span in fixed.values())
    spans = _fit_columns(inner - 2, cost, optional, least=least, flex=flex,
                         gaps=_extra_gaps(columns))
    spans.update(fixed)
    shown = [spec for spec in columns if spans.get(spec[0])]
    _head_rule(_row(shown, spans), 4)
    for values in rows:
        print("    " + _row(shown, spans, values).rstrip())
    print()


def _headline(text: str, inner: int, colour: str = "") -> None:
    """The one bold sentence a view opens with, wrapped rather than cut."""
    import textwrap

    for line in textwrap.wrap(text, max(20, inner - 4)):
        print(f"    {colour}{ui.BOLD}{line}{ui.RST}")


def _plural(count: int, word: str, many: str = "") -> str:
    return f"{count:,} {word if count == 1 else (many or word + 's')}"


def _frame(title: str, days: int) -> int:
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    print()
    print(ui.rule(inner, f"{title} · {_window_label(days)}"))
    print()
    return inner


# ── Tool failures ────────────────────────────────────────────────────

def _failures_data(days: int) -> dict:
    meta, digests = _window(days)
    by_tool: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_repo: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    worst = []
    calls = failures = 0
    for sid, digest in digests.items():
        row = meta[sid]
        calls += digest["calls"]
        failures += digest["failures"]
        for tool, (made, failed) in digest["tools"].items():
            by_tool[_clean(tool) or "unknown"][0] += made
            by_tool[_clean(tool) or "unknown"][1] += failed
        repo = _clean(row[3]) or "(no repository)"
        by_repo[repo][0] += 1
        by_repo[repo][1] += digest["calls"]
        by_repo[repo][2] += digest["failures"]
        if digest["failures"]:
            top = max(digest["tools"].items(), key=lambda kv: (kv[1][1], kv[0]))[0]
            worst.append({**_session_fields(row), "calls": digest["calls"],
                          "failures": digest["failures"],
                          "rate": round(_rate(digest["failures"], digest["calls"]), 4),
                          "top_tool": _clean(top),
                          "loops": len(digest["loops"])})
    worst.sort(key=lambda r: (-r["failures"], -r["rate"], r["id"]))
    return {
        "window_days": days,
        "sessions_with_logs": len(digests),
        "sessions_in_window": len(meta),
        "calls": calls,
        "failures": failures,
        "rate": round(_rate(failures, calls), 4),
        "by_tool": sorted(
            ({"tool": tool, "calls": made, "failures": failed,
              "rate": round(_rate(failed, made), 4)}
             for tool, (made, failed) in by_tool.items()),
            key=lambda r: (-r["failures"], -r["calls"], r["tool"])),
        "by_repo": sorted(
            ({"repository": repo, "sessions": n, "calls": made, "failures": failed,
              "rate": round(_rate(failed, made), 4)}
             for repo, (n, made, failed) in by_repo.items()),
            key=lambda r: (-r["failures"], -r["calls"], r["repository"])),
        "worst": worst[:_TOP],
    }


def cmd_failures(days: int = 30, loops: bool = False) -> bool:
    """Which tools fail, where, and in which sessions — or the stuck loops."""
    render = _render_loops if loops else _render_failures
    return _page(_capture(lambda: render(days)))


def _no_logs(inner: int, found: int) -> None:
    if found:
        _note("No tool call in this window failed.", inner)
    else:
        _note("No session in this window left an event log.", inner)
        print()
        _note("Tool calls are read from session-state/<id>/events.jsonl, "
              "which older Copilot releases do not write.", inner)
    print()


def _render_failures(days: int) -> None:
    data = _failures_data(days)
    inner = _frame("Tool failures", days)
    if not data["failures"]:
        _no_logs(inner, data["sessions_with_logs"])
        return
    rate = f"{data['rate'] * 100:.1f}%"
    _headline(f"{data['failures']:,} of {data['calls']:,} tool calls failed · "
              f"{rate}", inner)
    _note(f"across {_plural(data['sessions_with_logs'], 'session')} with an "
          f"event log", inner, indent=4)
    print()

    print(ui.heading("By tool", ui.ROSE, inner))
    peak = max(r["failures"] for r in data["by_tool"])
    _table(
        [("failures", "failed", ">"), ("calls", "calls", ">"), ("rate", "rate", ">"),
         ("bar", "", "<"), ("tool", "tool", "<")],
        [{"tool": (r["tool"], ""), "calls": (f"{r['calls']:,}", ui.MUTED),
          "failures": (f"{r['failures']:,}", ui.ROSE if r["failures"] else ui.MUTED),
          "rate": (f"{r['rate'] * 100:.1f}%", ""),
          "bar": (ui.bar(r["failures"], peak, 12, colour=ui.ROSE), "")}
         for r in data["by_tool"][:_TOP] if r["failures"]],
        inner, {"calls": 8, "failures": 7, "rate": 6}, [("bar", 12)],
        flex="tool", least=8)

    print(ui.heading("By repository", ui.AMBER, inner))
    _table(
        [("sessions", "sessions", ">"), ("calls", "calls", ">"),
         ("failures", "failed", ">"), ("rate", "rate", ">"),
         ("repository", "repository", "<")],
        [{"repository": (r["repository"], ""),
          "sessions": (str(r["sessions"]), ui.MUTED),
          "calls": (f"{r['calls']:,}", ui.MUTED),
          "failures": (f"{r['failures']:,}", ui.ROSE if r["failures"] else ui.MUTED),
          "rate": (f"{r['rate'] * 100:.1f}%", "")}
         for r in data["by_repo"][:_TOP]],
        inner, {"failures": 7, "rate": 6}, [("sessions", 8), ("calls", 8)],
        flex="repository", least=10)

    worst = data["worst"]
    _number(worst)
    print(ui.heading(f"Worst sessions · {len(worst)}", ui.ROSE, inner))
    _table(
        [("n", "#", ">"), ("session", "session", "<"), ("failures", "failed", ">"),
         ("rate", "rate", ">"), ("tool", "mostly", "<"), ("summary", "summary", "<")],
        [{"n": (str(r["n"]), ui.SKY), "session": (r["id"][:8], ui.SKY),
          "failures": (str(r["failures"]), ui.ROSE),
          "rate": (f"{r['rate'] * 100:.0f}%", ""),
          "tool": (r["top_tool"], ui.CODE),
          "summary": (r["summary"] or "(untitled)", "")}
         for r in worst],
        inner, {"n": 3, "failures": 6}, [("session", 8), ("rate", 5), ("tool", 10)])
    _hint("cs show N — the failures turn by turn · cs failures --loops — "
          "stuck loops", inner)
    print()


# ── Stuck loops ──────────────────────────────────────────────────────

def _loops_data(days: int) -> dict:
    meta, digests = _window(days)
    stuck = {sid: d for sid, d in digests.items() if d["loops"]}
    conn = db.connect()
    try:
        times = db.turn_times(conn, list(stuck))
    finally:
        conn.close()
    loops = []
    for sid, digest in stuck.items():
        for loop in digest["loops"]:
            first = events.turn_of(loop["start"], times[sid])
            last = events.turn_of(loop["end"], times[sid])
            loops.append({**_session_fields(meta[sid]), "tool": _clean(loop["tool"]),
                          "run": loop["run"], "agent": loop["agent"],
                          "first_turn": first, "last_turn": last,
                          "started": loop["start"], "ended": loop["end"]})
    loops.sort(key=lambda r: (-r["run"], r["id"]))
    return {"window_days": days, "threshold": events.LOOP_MIN,
            "sessions": len(stuck), "loops": loops}


def loop_ids(session_ids: list[str]) -> set[str]:
    """Sessions the cache already knows got stuck — for a listing's marker.

    Cache only, never a log read: a listing redraws on a heartbeat and must
    not stall on a cold store. The Stuck loops view is the full answer.
    """
    return {sid for sid, digest in
            events.digests(session_ids, compute=False).items() if digest["loops"]}


def _render_loops(days: int) -> None:
    data = _loops_data(days)
    inner = _frame("Stuck loops", days)
    loops = data["loops"]
    if not loops:
        _note(f"No tool failed {events.LOOP_MIN} or more times in a row in this "
              f"window.", inner)
        print()
        return
    _headline(f"{_plural(len(loops), 'loop')} in "
              f"{_plural(data['sessions'], 'session')}", inner)
    _note(f"A loop is {events.LOOP_MIN} or more failures of one tool in a row, "
          "by one agent, with no success of that tool between them. The turns "
          "are the store's, joined on time.", inner, indent=4)
    print()
    seen = [{"id": sid} for sid in dict.fromkeys(loop["id"] for loop in loops)]
    _number(seen)
    numbers = {row["id"]: row["n"] for row in seen}
    _table(
        [("n", "#", ">"), ("session", "session", "<"), ("tool", "tool", "<"),
         ("run", "run", ">"), ("turns", "turns", "<"), ("agent", "by", "<"),
         ("summary", "summary", "<")],
        [{"n": (str(numbers[r["id"]]), ui.SKY), "session": (r["id"][:8], ui.SKY),
          "tool": (r["tool"], ui.CODE), "run": (f"×{r['run']}", ui.ROSE),
          "turns": (_turn_range(r["first_turn"], r["last_turn"]), ""),
          "agent": (r["agent"], ui.MUTED),
          "summary": (r["summary"] or "(untitled)", "")}
         for r in loops[:_TOP * 3]],
        inner, {"n": 3, "tool": 10, "run": 4}, [("agent", 9), ("session", 8),
                                               ("turns", 7)])
    _hint("cs show N — the failing turns · cs read N --turn T — the turn itself",
          inner)
    print()


# ── Sub-agents ───────────────────────────────────────────────────────

def _declared_models() -> dict[str, str]:
    """Agent name (lowered) → the model its profile declares, where it does."""
    out = {}
    for asset in context.assets("agents"):
        model = context.declared_model(asset.path)
        if model:
            out[asset.name.lower()] = model
    return out


def _subagents_data(days: int) -> dict:
    _meta, digests = _window(days)
    runs: dict[str, list[dict]] = defaultdict(list)
    for digest in digests.values():
        for run in digest["subagents"]:
            runs[_clean(run["name"]) or "unknown"].append(run)
    declared = _declared_models()
    agents = []
    for name, group in runs.items():
        durations = [run["duration_ms"] for run in group if run["duration_ms"]]
        overridden = sum(1 for run in group if run["override"])
        models = Counter(_clean(run["model"]) for run in group if run["model"])
        want = _clean(declared.get(name.lower(), ""))
        agents.append({
            "name": name,
            "runs": len(group),
            "failed": sum(1 for run in group if run["failed"]),
            "models": [model for model, _ in models.most_common()],
            "overridden": overridden,
            "tool_calls": sum(run["tool_calls"] for run in group),
            "tokens": sum(run["tokens"] for run in group),
            "duration_ms": sum(durations),
            "median_ms": int(statistics.median(durations)) if durations else 0,
            "declared_model": want or None,
            # The profile asks for a model and not one run was given it.
            "override_ignored": bool(want) and overridden == 0,
        })
    agents.sort(key=lambda a: (-a["runs"], a["name"]))
    return {"window_days": days, "runs": sum(a["runs"] for a in agents),
            "agents": agents}


def cmd_subagents(days: int = 30) -> bool:
    """Which sub-agents ran, on which models, and what they cost in time."""
    return _page(_capture(lambda: _render_subagents(days)))


def _seconds(ms: int) -> str:
    seconds = ms / 1000
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60:
        return f"{seconds / 60:.1f}m"
    return f"{seconds:.0f}s"


def _render_subagents(days: int) -> None:
    data = _subagents_data(days)
    inner = _frame("Sub-agents", days)
    agents = data["agents"]
    if not agents:
        _note("No sub-agent run was logged in this window.", inner)
        print()
        return
    _headline(f"{_plural(data['runs'], 'run')} of "
              f"{_plural(len(agents), 'agent')}", inner)
    print()
    _table(
        [("runs", "runs", ">"), ("name", "agent", "<"), ("override", "override", ">"),
         ("calls", "tools", ">"), ("tokens", "tokens", ">"), ("total", "total", ">"),
         ("median", "median", ">"), ("models", "models", "<")],
        [{"runs": (str(a["runs"]), ui.VIOLET), "name": (a["name"], ""),
          "override": (f"{a['overridden']}/{a['runs']}",
                       ui.AMBER if a["override_ignored"] else ui.MUTED),
          "calls": (f"{a['tool_calls']:,}", ui.MUTED),
          "tokens": (_thousands(a["tokens"]), ui.MUTED),
          "total": (_seconds(a["duration_ms"]), ""),
          "median": (_seconds(a["median_ms"]), ""),
          "models": (", ".join(a["models"]) or "—", ui.CODE)}
         for a in agents[:_TOP * 2]],
        inner, {"runs": 5, "name": 14}, [("tokens", 7), ("calls", 6),
                                        ("total", 6), ("override", 8),
                                        ("median", 6)],
        flex="models", least=10)
    ignored = [a for a in agents if a["override_ignored"]]
    if ignored:
        print(ui.heading(f"Declared model not applied · {len(ignored)}",
                         ui.AMBER, inner))
        for agent in ignored:
            print(f"    {ui.AMBER}{ui._fit(agent['name'], inner - 6)}{ui.RST}")
            evidence = (f"profile declares model: {agent['declared_model']} · "
                        f"0 of {agent['runs']} runs had an override · ran on "
                        f"{', '.join(agent['models']) or 'an unrecorded model'}")
            _note(evidence, inner, indent=6)
        print()
    _hint("cs profiles — the agents you have defined · cs agents — "
          "delegation by spend", inner)
    print()


# ── Model switches ───────────────────────────────────────────────────

def _switches_data(days: int) -> dict:
    meta, digests = _window(days)
    changed = {}
    for sid, digest in digests.items():
        mid = [change for change in digest["model_changes"]
               if change["from"] and (change["from"] != change["to"]
                                      or change["effort_from"] != change["effort_to"])]
        if mid:
            changed[sid] = mid
    conn = db.connect()
    try:
        times = db.turn_times(conn, list(changed))
        spend = db.usage_stamps(conn, list(changed))
    finally:
        conn.close()
    sessions = []
    for sid, mid in changed.items():
        calls = spend[sid]
        cuts = [change["at"] for change in mid]
        switches = []
        for index, change in enumerate(mid):
            start = cuts[index]
            end = cuts[index + 1] if index + 1 < len(cuts) else "~"
            previous = cuts[index - 1] if index else ""
            switches.append({
                "at": change["at"],
                "turn": events.turn_of(change["at"], times[sid]),
                "from": _clean(change["from"]), "to": _clean(change["to"]),
                "effort_from": _clean(change["effort_from"]) or None,
                "effort_to": _clean(change["effort_to"]) or None,
                "source": _clean(change["source"]) or "unrecorded",
                "nano_aiu_before": sum(n for at, n in calls if previous <= at < start),
                "nano_aiu_after": sum(n for at, n in calls if start <= at < end),
            })
        sessions.append({**_session_fields(meta[sid]), "switches": switches})
    sessions.sort(key=lambda s: s["last_active"], reverse=True)
    return {"window_days": days, "sessions": sessions,
            "switches": sum(len(s["switches"]) for s in sessions)}


def cmd_switches(days: int = 30) -> bool:
    """Sessions that changed model or effort part-way, and what each side cost."""
    return _page(_capture(lambda: _render_switches(days)))


def _render_switches(days: int) -> None:
    data = _switches_data(days)
    inner = _frame("Model switches", days)
    if not data["sessions"]:
        _note("No session changed model or effort part-way through in this "
              "window.", inner)
        print()
        return
    _headline(f"{_plural(data['switches'], 'switch', 'switches')} in "
              f"{_plural(len(data['sessions']), 'session')}", inner)
    _note("AIU before is what the session spent since the previous switch; "
          "after is what it spent until the next one.", inner, indent=4)
    print()
    _number(data["sessions"])
    rows = []
    for session in data["sessions"][:_TOP * 2]:
        for index, switch in enumerate(session["switches"]):
            effort = ""
            if switch["effort_from"] != switch["effort_to"]:
                effort = f" ({switch['effort_from'] or '—'}→{switch['effort_to'] or '—'})"
            move = (f"{switch['from']} → {switch['to']}"
                    if switch["from"] != switch["to"] else switch["to"])
            rows.append({
                "n": (str(session["n"]) if not index else "", ui.SKY),
                "when": (_when(switch["at"]), ui.MUTED),
                "turn": ("—" if switch["turn"] is None else str(switch["turn"]), ""),
                "before": (ui.fmt_aiu(switch["nano_aiu_before"]), ui.VIOLET),
                "after": (ui.fmt_aiu(switch["nano_aiu_after"]), ui.VIOLET),
                "source": (switch["source"], ui.MUTED),
                "move": (move + effort, ui.CODE),
            })
    _table(
        [("n", "#", ">"), ("when", "when", "<"), ("turn", "turn", ">"),
         ("before", "before", ">"), ("after", "after", ">"),
         ("source", "source", "<"), ("move", "from → to", "<")],
        rows, inner, {"n": 3, "before": 7, "after": 7},
        [("when", 11), ("turn", 4), ("source", 12)], flex="move", least=30)
    _hint("cs show N — the session · cs efficiency — cost by model", inner)
    print()


# ── Unclean endings ──────────────────────────────────────────────────

def _endings_data(days: int) -> dict:
    conn = db.connect()
    try:
        rows = _visible(db.recent_sessions(conn, days), show_all=False)
        last = db.last_calls(conn)
    finally:
        conn.close()
    checked = [row for row in rows if row[0] in last]
    endings = []
    for row in checked:
        turn, reason, model, at = last[row[0]]
        if reason not in _UNCLEAN:
            continue
        endings.append({**_session_fields(row), "turn": turn,
                        "reason": _UNCLEAN[reason], "finish_reason": reason or None,
                        "model": _clean(model), "at": at})
    counts = Counter(e["reason"] for e in endings)
    return {"window_days": days, "checked": len(checked),
            "recorded": bool(last), "by_reason": dict(counts.most_common()),
            "endings": endings}


def cmd_endings(days: int = 30) -> bool:
    """Sessions whose last model call was cut off rather than finished."""
    return _page(_capture(lambda: _render_endings(days)))


def _render_endings(days: int) -> None:
    data = _endings_data(days)
    inner = _frame("Unclean endings", days)
    if not data["recorded"]:
        _note("This store records no finish reasons.", inner)
        print()
        return
    endings = data["endings"]
    if not endings:
        _headline(f"All {data['checked']:,} sessions ended on a clean call.",
                  inner, ui.MINT)
        print()
        return
    _headline(f"{len(endings)} of {data['checked']:,} sessions ended on a "
              f"call that did not finish", inner)
    _note(" · ".join(f"{count} {reason}"
                     for reason, count in data["by_reason"].items()), inner, indent=4)
    _note("Read from each session's last billed call. 'stop' and 'tool_calls' "
          "are clean; error, length and content filter are the model or the "
          "service ending it; unknown means no reason was recorded.",
          inner, indent=4)
    print()
    _number(endings)
    _table(
        [("n", "#", ">"), ("session", "session", "<"), ("turn", "turn", ">"),
         ("reason", "reason", "<"), ("model", "model", "<"),
         ("summary", "summary", "<")],
        [{"n": (str(e["n"]), ui.SKY), "session": (e["id"][:8], ui.SKY),
          "turn": ("—" if e["turn"] is None else str(e["turn"]), ""),
          "reason": (e["reason"], ui.ROSE if e["reason"] != "unknown" else ui.AMBER),
          "model": (e["model"], ui.CODE),
          "summary": (e["summary"] or "(untitled)", "")}
         for e in endings[:_TOP * 3]],
        inner, {"n": 3, "reason": 14}, [("model", 14), ("session", 8), ("turn", 4)])
    _hint("cs read N --turn T — the turn it stopped on · cs resume N", inner)
    print()


# ── Pieces other views borrow ────────────────────────────────────────

def _print_session_failures(session_id: str, inner: int) -> None:
    """`cs show`'s tool-call block: failures per turn, and any stuck loop."""
    digest = events.session_digest(session_id)
    if not digest or not digest["calls"]:
        return
    conn = db.connect()
    try:
        times = db.turn_times(conn, [session_id])[session_id]
    finally:
        conn.close()
    title = f"Tool calls · {digest['calls']:,}"
    if digest["failures"]:
        title += f" · {digest['failures']} failed"
    print(ui.heading(title, ui.ROSE if digest["failures"] else ui.MINT))
    top = sorted(digest["tools"].items(), key=lambda kv: (-kv[1][0], kv[0]))[:6]
    print(f"    {ui.MUTED}"
          f"{ui._fit(' · '.join(f'{_clean(t)} {made}' for t, (made, _f) in top), inner - 4)}"
          f"{ui.RST}")
    if digest["failures"]:
        per_turn: dict[object, Counter] = defaultdict(Counter)
        for at, tool in digest["failed_at"]:
            per_turn[events.turn_of(at, times)][_clean(tool)] += 1
        known = sorted(k for k in per_turn if k is not None)
        for turn in known + ([None] if None in per_turn else []):
            tools = per_turn[turn]
            label = f"turn {turn}" if turn is not None else "unmatched"
            detail = ", ".join(f"{tool} ×{n}" if n > 1 else tool
                               for tool, n in tools.most_common())
            print(f"    {ui.ROSE}{sum(tools.values()):>3} failed{ui.RST}  "
                  f"{_cell(label, 10)} {ui.CODE}"
                  f"{ui._fit(detail, max(inner - 28, 8))}{ui.RST}")
        if len(digest["failed_at"]) < digest["failures"]:
            print(f"    {ui.MUTED}first {len(digest['failed_at'])} failures "
                  f"placed by turn{ui.RST}")
    for loop in digest["loops"][:3]:
        first = events.turn_of(loop["start"], times)
        last = events.turn_of(loop["end"], times)
        print(f"    {ui.AMBER}stuck loop{ui.RST}  {_clean(loop['tool'])} failed "
              f"{loop['run']}× in a row · turns {_turn_range(first, last)} · "
              f"{loop['agent']}")
    print()
