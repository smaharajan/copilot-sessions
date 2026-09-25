"""Analysis views: compare two sessions, replay one, spend anomalies, repo
health, prompt patterns, and how agent profiles and skills are really used.

The same shape as the other view modules: a `_*_data` reading, masked, that
`--json` returns, and a renderer that draws it. Every flag carries what
triggered it — a spend spike names the turns that drove it — and every
comparison of habits against outcomes says how many sessions it rests on.
"""

from __future__ import annotations

import os
import re
import shutil
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from .. import (
    context,
    db,
    events,
    signals,
    ui,
)
from ._common import (
    _capture,
    _cell,
    _note,
    _page,
    _resolve_ref,
    _short_path,
    _user_text,
    _visible,
    _when,
)
from .evidence import (
    _TOP,
    _clean,
    _frame,
    _headline,
    _number,
    _plural,
    _rate,
    _session_fields,
    _table,
)
from .inventory import _asset_inventory
from .session import _read_in_place, _transcript_turn
from .today import _repo_match

# A day or a session is a spike when it cost more than this many times the
# median of the fourteen days before it.
ANOMALY_FACTOR = 2.0
BASELINE_DAYS = 14
# Fewer comparable days or sessions than this and there is no baseline.
_BASELINE_MIN = 3


def _cache(rate: float | None) -> str:
    return "—" if rate is None else f"{rate:.0%}"


def _duration(first: str, last: str) -> str:
    try:
        span = datetime.fromisoformat(last) - datetime.fromisoformat(first)
    except ValueError:
        return "—"
    minutes = span.total_seconds() / 60
    if minutes >= 90:
        return f"{minutes / 60:.1f}h"
    return f"{minutes:.0f}m"


# ── Compare ──────────────────────────────────────────────────────────

def _diff_side(conn, session_id: str) -> dict:
    detail = db.session_detail(conn, session_id)
    if detail is None:
        print(f"error: session not found: {session_id}", file=sys.stderr)
        sys.exit(1)
    metrics = db.session_metrics(conn, session_id)
    refs = db.session_refs(conn, session_id)
    digest = events.session_digest(session_id)
    first = metrics["first"] or (detail[4] or "")[:19].replace(" ", "T")
    last = metrics["last"] or (detail[5] or "")[:19].replace(" ", "T")
    return {
        "id": session_id,
        "summary": _clean(detail[0]),
        "repository": _clean(detail[1]) if detail[1] != "-" else None,
        "turns": db.session_turn_count(conn, session_id),
        "nano_aiu": metrics["nano_aiu"],
        "calls": metrics["calls"],
        "cache_hit": metrics["cache_hit"],
        "models": [_clean(m["model"]) for m in metrics["models"]],
        "tool_calls": digest["calls"] if digest else None,
        "tool_failures": digest["failures"] if digest else None,
        "files": len(db.session_files(conn, session_id)),
        "commits": sum(1 for kind, _ in refs if kind == "commit"),
        "prs": sum(1 for kind, _ in refs if kind == "pr"),
        "duration": _duration(first, last),
    }


def _diff_data(first: str, second: str) -> dict:
    a, b = _resolve_ref(first), _resolve_ref(second)
    conn = db.connect()
    try:
        return {"a": _diff_side(conn, a), "b": _diff_side(conn, b)}
    finally:
        conn.close()


_DIFF_ROWS = (
    ("cost", lambda s: f"{s['nano_aiu'] / 1e9:,.2f} AIU"),
    ("turns", lambda s: f"{s['turns']:,}"),
    ("model calls", lambda s: f"{s['calls']:,}"),
    ("models", lambda s: ", ".join(s["models"]) or "—"),
    ("cache hit", lambda s: _cache(s["cache_hit"])),
    ("tool calls", lambda s: "no log" if s["tool_calls"] is None
     else f"{s['tool_calls']:,} · {s['tool_failures']} failed"),
    ("files", lambda s: f"{s['files']:,}"),
    ("shipped", lambda s: f"{_plural(s['commits'], 'commit')} · "
                          f"{_plural(s['prs'], 'PR')}"),
    ("duration", lambda s: s["duration"]),
)


def cmd_diff(first: str, second: str) -> bool:
    """Two sessions side by side — what each cost, used and produced."""
    return _page(_capture(lambda: _render_diff(_diff_data(first, second))))


def _render_diff(data: dict) -> None:
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    a, b = data["a"], data["b"]
    print()
    print(ui.rule(inner, "Compare sessions"))
    print()
    label = 12
    if inner < 60:
        # Too narrow for two columns of values: one session, then the other.
        for tag, side in (("A", a), ("B", b)):
            print(ui.heading(ui._fit(f"{tag} · {side['id'][:8]} · "
                                     f"{side['summary'] or '(untitled)'}", inner - 4),
                             ui.SKY, inner))
            for name, value in _DIFF_ROWS:
                print(f"    {ui.MUTED}{name:<{label}}{ui.RST}"
                      f"{ui._fit(value(side), inner - label - 4)}")
            print()
        return
    span = (inner - 4 - label - 2) // 2
    for tag, side in (("A", a), ("B", b)):
        print(f"    {ui.SKY}{tag} {side['id'][:8]}{ui.RST}  "
              f"{ui._fit(side['summary'] or '(untitled)', inner - 16)}")
    print()
    print(f"    {' ' * label}  {ui.BOLD}{'A':<{span}}{ui.RST}{ui.BOLD}B{ui.RST}")
    print(f"    {ui.MUTED}{'─' * (label + 2 + span * 2)}{ui.RST}")
    for name, value in _DIFF_ROWS:
        left, right = value(a), value(b)
        mark = ui.AMBER if left != right else ""
        print(f"    {ui.MUTED}{name:<{label}}{ui.RST}  "
              f"{mark}{ui._fit(left, span - 1):<{span}}{ui.RST if mark else ''}"
              f"{mark}{ui._fit(right, span)}{ui.RST if mark else ''}")
    print()
    _note("Values that differ are highlighted. In a listing, d marks one "
          "session and d on another compares them.", inner)
    print()


# ── Replay ───────────────────────────────────────────────────────────

def _replay_data(session_id: str) -> dict:
    """Everything the replay pages need, gathered once."""
    conn = db.connect()
    try:
        detail = db.session_detail(conn, session_id)
        if detail is None:
            print(f"error: session not found: {session_id}", file=sys.stderr)
            sys.exit(1)
        turns = db.session_transcript(conn, session_id)
        spend = db.turn_spend(conn, session_id)
        files: dict[int, list[str]] = defaultdict(list)
        if db.has_files(conn) and db._has_columns(conn, "session_files", "turn_index"):
            for path, turn in conn.execute(
                    "SELECT file_path, turn_index FROM session_files "
                    "WHERE session_id = ? AND turn_index IS NOT NULL", (session_id,)):
                files[turn].append(path)
        times = db.turn_times(conn, [session_id])[session_id]
    finally:
        conn.close()
    tools: dict[object, Counter] = defaultdict(Counter)
    names: dict[str, str] = {}
    for event in events.iter_events(session_id, ["tool.execution_start",
                                                 "tool.execution_complete"]):
        data = event["data"]
        call = data.get("toolCallId") if isinstance(data.get("toolCallId"), str) else ""
        if event["type"] == "tool.execution_start":
            names[call] = data.get("toolName") if isinstance(
                data.get("toolName"), str) else "unknown"
            continue
        stamp = event.get("timestamp")
        turn = events.turn_of(stamp[:19] if isinstance(stamp, str) else "", times)
        tool = _clean(names.pop(call, "unknown"))
        tools[turn][(tool, data.get("success") is not False)] += 1
    return {"id": session_id, "detail": detail, "turns": turns, "spend": spend,
            "files": files, "tools": tools, "cwd": detail[2]}


def _replay_page(data: dict, turn: int) -> str:
    width = min(shutil.get_terminal_size().columns, 100)
    inner = width - 4
    turns = {row[0]: row for row in data["turns"]}
    index, prompt, reply, when = turns[turn]
    lines = [""]
    title = f"Replay · {_clean(data['detail'][0]) or data['id'][:8]}"
    lines.append(ui.rule(inner, title, note=f"turn {turn} of {max(turns)}"))
    lines.append("")
    peak = max(data["spend"].values(), default=0)
    spent = data["spend"].get(turn, 0)
    bar = max(8, min(24, inner - 30))
    lines.append(f"    {ui.MUTED}{'credits':<8}{ui.RST}"
                 f"{ui.bar(spent, peak or 1, bar, colour=ui.VIOLET, track=True)} "
                 f"{ui.VIOLET}{ui.fmt_aiu(spent)} AIU{ui.RST}")
    used = data["tools"].get(turn)
    if used:
        by_tool: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for (tool, ok), count in used.items():
            by_tool[tool][0 if ok else 1] += count
        parts = []
        for tool, (ok, failed) in sorted(by_tool.items(), key=lambda kv: -sum(kv[1])):
            part = f"{tool} {ok}"
            if failed:
                part += f" {ui.ROSE}✗{failed}{ui.RST}"
            parts.append(part)
        lines.append(f"    {ui.MUTED}{'tools':<8}{ui.RST}" + " · ".join(parts))
    touched = data["files"].get(turn, [])
    for number, path in enumerate(touched[:6]):
        label = "files" if not number else ""
        lines.append(f"    {ui.MUTED}{label:<8}{ui.RST}"
                     f"{ui._fit(_clean(_short_path(path, data['cwd'])), inner - 12)}")
    if len(touched) > 6:
        lines.append(f"    {' ' * 8}{ui.MUTED}… and {len(touched) - 6} more{ui.RST}")
    lines.append("")
    lines.extend(_transcript_turn(index, prompt, reply, when, inner))
    return "\n".join(lines)


def cmd_replay(ref: str) -> bool:
    """Step through a session a turn at a time: ←/→ between turns."""
    session_id = _resolve_ref(ref)
    data = _replay_data(session_id)
    order = [row[0] for row in data["turns"]]
    if not order:
        print(f"  {ui.MUTED}No turns recorded in {session_id[:8]}.{ui.RST}")
        return False
    if sys.stdout.isatty():
        sort = {"steps": "turn", "columns": order, "column": order[0],
                "descending": False, "defaults": {},
                "render": lambda turn, _down: _replay_page(data, turn),
                "label": lambda turn: f"turn {turn}/{order[-1]}"}
        if _read_in_place(_replay_page(data, order[0]), sort):
            return True
    print("\n".join(_replay_page(data, turn) for turn in order))
    return False


# ── Spend anomalies ──────────────────────────────────────────────────

def _anomalies_data(days: int) -> dict:
    conn = db.connect()
    try:
        lookback = days + BASELINE_DAYS if days else 0
        by_day = {day: nano for day, nano, _calls in db.cost_by_day(conn, lookback)}
        sessions = db.session_spend(conn, lookback)
        rows = {row[0]: row for row in db.recent_sessions(conn, 0)}
        since = (date.today() - timedelta(days=days)).isoformat() if days else ""
        spiky_days = []
        for day, nano in sorted(by_day.items()):
            if day < since:
                continue
            start = (date.fromisoformat(day) - timedelta(days=BASELINE_DAYS)).isoformat()
            before = [spent for other, spent in by_day.items()
                      if start <= other < day and spent]
            if len(before) < _BASELINE_MIN:
                continue
            baseline = statistics.median(before)
            if baseline and nano > ANOMALY_FACTOR * baseline:
                spiky_days.append({
                    "day": day, "nano_aiu": nano, "baseline_nano_aiu": int(baseline),
                    "factor": round(nano / baseline, 2),
                    "turns": _evidence(db.turn_costs(conn, day=day), rows),
                })
        spiky_sessions = []
        for sid, day, nano in sessions:
            if day < since or sid not in rows:
                continue
            start = (date.fromisoformat(day) - timedelta(days=BASELINE_DAYS)).isoformat()
            before = [spent for other, when, spent in sessions
                      if start <= when < day and other != sid and spent]
            if len(before) < _BASELINE_MIN:
                continue
            baseline = statistics.median(before)
            if baseline and nano > ANOMALY_FACTOR * baseline:
                spiky_sessions.append({
                    **_session_fields(rows[sid]), "day": day, "session_nano_aiu": nano,
                    "baseline_nano_aiu": int(baseline),
                    "factor": round(nano / baseline, 2),
                    "turns": _evidence(db.turn_costs(conn, session_id=sid), rows),
                })
    finally:
        conn.close()
    spiky_sessions.sort(key=lambda s: -s["factor"])
    return {"window_days": days, "factor": ANOMALY_FACTOR,
            "baseline_days": BASELINE_DAYS, "days": spiky_days,
            "sessions": spiky_sessions[:_TOP * 2]}


def _evidence(turns: list[dict], rows: dict) -> list[dict]:
    return [{
        "id": turn["session_id"],
        "summary": _clean(rows[turn["session_id"]][2]) if turn["session_id"] in rows
        else "",
        "turn": turn["turn"], "nano_aiu": turn["nano_aiu"],
        "models": [_clean(m) for m in turn["models"]],
        "effort": _clean(turn["effort"]) or None,
        "cache_hit": turn["cache_hit"],
    } for turn in turns]


def cmd_anomalies(days: int = 30) -> bool:
    """Days and sessions that cost far more than the fortnight before them."""
    return _page(_capture(lambda: _render_anomalies(_anomalies_data(days))))


def _evidence_lines(turns: list[dict], inner: int, indent: int = 6) -> None:
    for turn in turns:
        where = f"{turn['id'][:8]} turn {turn['turn']}" if turn["turn"] is not None \
            else turn["id"][:8]
        bits = [", ".join(turn["models"]) or "model not recorded"]
        if turn["effort"]:
            bits.append(f"effort {turn['effort']}")
        bits.append(f"cache {_cache(turn['cache_hit'])}")
        text = f"{ui.fmt_aiu(turn['nano_aiu'])} AIU · {where} · {' · '.join(bits)}"
        _note(text, inner, indent=indent)


def _render_anomalies(data: dict) -> None:
    inner = _frame("Spend anomalies", data["window_days"])
    _note(f"A day or a session is flagged when it cost more than "
          f"{data['factor']:g}× the median of the {data['baseline_days']} days "
          f"before it. The turns that drove each are listed with their model, "
          f"effort and cache hit rate.", inner, indent=4)
    print()
    if not data["days"] and not data["sessions"]:
        _note("Nothing stood out in this window.", inner)
        print()
        return
    if data["days"]:
        print(ui.heading(f"Days · {len(data['days'])}", ui.VIOLET, inner))
        for day in data["days"]:
            print(f"    {ui.BOLD}{day['day']}{ui.RST}  {ui.VIOLET}"
                  f"{day['nano_aiu'] / 1e9:,.2f} AIU{ui.RST}  {ui.MUTED}"
                  f"{day['factor']:g}× the median of "
                  f"{day['baseline_nano_aiu'] / 1e9:,.2f}{ui.RST}")
            _evidence_lines(day["turns"], inner)
        print()
    if data["sessions"]:
        _number(data["sessions"])
        print(ui.heading(f"Sessions · {len(data['sessions'])}", ui.VIOLET, inner))
        for session in data["sessions"]:
            print(f"    {ui.SKY}{session['n']:>3}{ui.RST}  "
                  f"{ui._fit(session['summary'] or '(untitled)', inner - 10)}")
            _note(f"{session['session_nano_aiu'] / 1e9:,.2f} AIU · "
                  f"{session['factor']:g}× the median session of "
                  f"{session['baseline_nano_aiu'] / 1e9:,.2f}", inner, indent=9)
            _evidence_lines(session["turns"], inner, indent=9)
        print()
    _note("cs replay N — step through the session · cs efficiency — cache and "
          "effort across the window", inner)
    print()


# ── Repo health ──────────────────────────────────────────────────────

def _health_data(repo: str = ".") -> dict:
    here = Path.cwd()
    conn = db.connect()
    try:
        rows = [row for row in _visible(db.recent_sessions(conn, 0), False)
                if _repo_match(row, repo)]
        ids = [row[0] for row in rows]
        touched: Counter = Counter()
        if db.has_files(conn):
            for chunk in db._chunks(ids):
                for (path,) in conn.execute(
                        f"SELECT file_path FROM session_files WHERE session_id IN "
                        f"({','.join('?' * len(chunk))})", chunk):
                    touched[path] += 1
        opened = [h for h in signals.open_handoffs(conn) if h["id"] in set(ids)]
    finally:
        conn.close()
    digests = events.digests(ids)
    calls = sum(d["calls"] for d in digests.values())
    failed = sum(d["failures"] for d in digests.values())
    hooks_failing: Counter = Counter()
    for digest in digests.values():
        for kind, (_ran, fails, _last) in digest["hooks"].items():
            if fails:
                hooks_failing[_clean(kind)] += fails
    # Instruction files and skills are read off the disk of the checkout you
    # stand in. For a repository named from elsewhere they would describe the
    # wrong one, so they are left out rather than guessed.
    local = repo == "."
    oversized = ([item for item in context.audit(here)["items"] if item.oversized]
                 if local else None)
    skills = _asset_inventory("skills") if local else None
    unused = [name for name in skills["names"]
              if not skills["counts"].get(name) and not skills["ran"].get(name.lower())
              and name.lower() in skills["on_disk"]
              and not skills["on_disk"][name.lower()].disabled] if skills else None
    name = os.path.basename(str(here)) if repo == "." else repo
    return {
        "repo": _clean(name),
        "sessions": len(rows),
        "nano_aiu": sum(row[6] or 0 for row in rows),
        "tool_calls": calls, "tool_failures": failed,
        "failure_rate": round(_rate(failed, calls), 4) if calls else None,
        "top_files": [{"path": _clean(_short_path(path, str(here))), "sessions": n}
                      for path, n in touched.most_common(5)],
        "instructions_over_limit": None if oversized is None else [
            {"file": _clean(item.label), "chars": item.chars} for item in oversized],
        "instruction_limit": context.INSTRUCTION_LIMIT,
        "skills_unused": None if unused is None else sorted(unused, key=str.lower),
        "skills_available": None if skills is None else len(skills["names"]),
        "hooks_failing": dict(hooks_failing.most_common()),
        "handoffs_open": [{"id": h["id"], "summary": _clean(h["summary"]),
                           "evidence": _clean(h["evidence"])} for h in opened],
    }


def cmd_health(repo: str = ".") -> bool:
    """One card for this repository: what working in it has been like."""
    return _page(_capture(lambda: _render_health(_health_data(repo))))


def _render_health(data: dict) -> None:
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    print()
    print(ui.rule(inner, f"Repo health · {data['repo']}"))
    print()
    if not data["sessions"]:
        _note("No session ran in this repository. Run it from a checkout, or "
              "name one: cs health --repo <name>.", inner)
        print()
        return
    rate = ("no event logs" if data["failure_rate"] is None else
            f"{data['failure_rate']:.1%} of {data['tool_calls']:,} tool calls")
    for label, value, colour in (
        ("sessions", f"{data['sessions']:,}", ""),
        ("spend", f"{data['nano_aiu'] / 1e9:,.2f} AIU", ui.VIOLET),
        ("failures", rate, ui.ROSE if (data["failure_rate"] or 0) > 0.05 else ""),
        ("instructions",
         "run from the checkout to read them"
         if data["instructions_over_limit"] is None else
         f"{len(data['instructions_over_limit'])} past "
         f"{data['instruction_limit']:,} characters", ui.AMBER
         if data["instructions_over_limit"] else ""),
        ("skills", "run from the checkout to read them"
         if data["skills_unused"] is None else
         f"{len(data['skills_unused'])} of {data['skills_available']} never used",
         ""),
        ("hooks", f"{sum(data['hooks_failing'].values())} failed runs"
         if data["hooks_failing"] else "none failing",
         ui.ROSE if data["hooks_failing"] else ""),
        ("handoffs", f"{len(data['handoffs_open'])} open", ui.SKY
         if data["handoffs_open"] else ""),
    ):
        print(ui.field(label, f"{colour}{value}{ui.RST if colour else ''}", 13))
    print()
    if data["top_files"]:
        print(ui.heading("Files agents edit most", ui.MINT, inner))
        for item in data["top_files"]:
            print(f"    {ui.MINT}{item['sessions']:>4}{ui.RST}  "
                  f"{ui._fit(item['path'], inner - 10)}")
        print()
    for title, entries, colour in (
        ("Instructions past the limit",
         [f"{i['file']} · {i['chars']:,} characters"
          for i in data["instructions_over_limit"] or []], ui.AMBER),
        ("Hooks failing", [f"{kind} · {n} failed" for kind, n in
                           data["hooks_failing"].items()], ui.ROSE),
        ("Handoffs open", [f"{h['summary'] or h['id'][:8]} · {h['evidence']}"
                           for h in data["handoffs_open"]], ui.SKY),
    ):
        if entries:
            print(ui.heading(f"{title} · {len(entries)}", colour, inner))
            for entry in entries[:_TOP]:
                _note(entry, inner, indent=4)
            print()
    if data["skills_unused"]:
        print(ui.heading(f"Skills never used · {len(data['skills_unused'])}",
                         ui.MUTED, inner))
        _note(", ".join(data["skills_unused"][:24])
              + (" …" if len(data["skills_unused"]) > 24 else ""), inner, indent=4)
        print()
    _note("Drill in: cs failures · cs instructions · cs skills · cs hooks · "
          "cs handoff", inner)
    print()


# ── Prompt patterns ──────────────────────────────────────────────────

_PATH = re.compile(r"(?:[\w.-]+/)+[\w.-]+|\b[\w-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|"
                   r"kt|rb|cs|cpp|c|h|md|json|ya?ml|toml|sql|sh|tf|html|css)\b")
_CRITERIA = re.compile(
    r"acceptance criteria|definition of done|done when|must pass|should pass|"
    r"\btests? (?:must|should) pass|\b(?:pytest|unittest|npm (?:run )?test|"
    r"go test|cargo test|make test|mvn test|gradle test|jest|vitest)\b", re.I)
_SHORT, _LONG = 80, 400


def _features(ask: str, skill: bool) -> dict[str, bool]:
    return {
        f"short ask (under {_SHORT} characters)": len(ask) < _SHORT,
        f"long ask ({_LONG}+ characters)": len(ask) >= _LONG,
        "names a file or path": bool(_PATH.search(ask)),
        "has acceptance criteria or a test command": bool(_CRITERIA.search(ask)),
        "a skill was invoked": skill,
    }


def _side(group: list[dict]) -> dict:
    return {
        "sessions": len(group),
        "shipped_rate": round(sum(1 for s in group if s["shipped"]) / len(group), 4)
        if group else None,
        "median_turns": statistics.median(s["turns"] for s in group) if group else None,
        "median_nano_aiu": int(statistics.median(s["nano"] for s in group))
        if group else None,
    }


def _patterns_data(days: int) -> dict:
    conn = db.connect()
    try:
        rows = _visible(db.recent_sessions(conn, days), False)
        ids = [row[0] for row in rows]
        prompts = db.opening_prompts(conn, ids, first_only=True)
        refs = db.refs_by_session(conn, ids)
        loaded = db.skills_invoked_by_session(conn)
    finally:
        conn.close()
    digests = events.digests(ids)
    sessions = []
    for row in rows:
        opening = next((cleaned for _i, text in prompts[row[0]]
                        if (cleaned := _user_text(text)).strip()), "")
        if not opening:
            continue
        made = refs.get(row[0]) or {}
        skill = bool(loaded.get(row[0])) or bool(
            digests.get(row[0], {}).get("skills"))
        sessions.append({"features": _features(opening, skill), "turns": row[5],
                         "nano": row[6] or 0,
                         "shipped": bool(made.get("commit") or made.get("pr"))})
    features = []
    for name in (_features("", False) if sessions else {}):
        with_it = [s for s in sessions if s["features"][name]]
        without = [s for s in sessions if not s["features"][name]]
        features.append({"feature": name, "with": _side(with_it),
                         "without": _side(without)})
    return {"window_days": days, "sessions": len(sessions), "features": features,
            "note": "correlation, not causation"}


def cmd_patterns(days: int = 30) -> bool:
    """How the way a session opens lines up with how it turns out."""
    return _page(_capture(lambda: _render_patterns(_patterns_data(days))))


def _pct(rate: float | None) -> str:
    return "—" if rate is None else f"{rate:.0%}"


def _render_patterns(data: dict) -> None:
    inner = _frame("Prompt patterns", data["window_days"])
    if not data["sessions"]:
        _note("No session in this window has an opening request to read.", inner)
        print()
        return
    _headline(f"Opening requests of {_plural(data['sessions'], 'session')}", inner)
    _note("Correlation, not causation: a habit that lines up with shipping may "
          "only share a cause with it. n is how many sessions each side rests "
          "on; under 5 is too few to read anything into.", inner, indent=4)
    print()
    for feature in data["features"]:
        print(ui.heading(ui._fit(feature["feature"], inner - 4), ui.ACCENT, inner))
        rows = []
        for side in ("with", "without"):
            reading = feature[side]
            thin = reading["sessions"] < 5
            rows.append({
                "side": (side, ui.MUTED if thin else ""),
                "n": (str(reading["sessions"]), ui.AMBER if thin else ""),
                "shipped": (_pct(reading["shipped_rate"]), ui.MINT),
                "turns": ("—" if reading["median_turns"] is None
                          else f"{reading['median_turns']:g}", ""),
                "aiu": ("—" if reading["median_nano_aiu"] is None
                        else ui.fmt_aiu(reading["median_nano_aiu"]), ui.VIOLET),
            })
        # Fixed, narrow columns: five short numbers read best side by side,
        # not spread across the window. AIU is the one that gives way.
        spans = [("side", "", 8, "<"), ("n", "n", 4, ">"),
                 ("shipped", "shipped", 8, ">"), ("turns", "turns", 6, ">")]
        if inner >= 44:
            spans.append(("aiu", "AIU", 9, ">"))
        heads = " ".join(_cell(head, span, align) for _k, head, span, align in spans)
        print(f"    {ui.MUTED}{heads.rstrip()}{ui.RST}")
        print(f"    {ui.MUTED}{'─' * ui.cells(heads)}{ui.RST}")
        for row in rows:
            print("    " + " ".join(_cell(row[key][0], span, align, row[key][1])
                                    for key, _h, span, align in spans).rstrip())
        print()
    _note("shipped = the session recorded a commit or PR · turns and AIU are "
          "medians", inner)
    print()


# ── Agent config: how profiles and skills are really used ────────────

def _config_usage(kind: str) -> dict[str, dict]:
    """name (lowered) → {invoked, last_used, override_honoured} from the logs.

    Skills: `skill.invoked` events. Agent profiles: sub-agent runs by name,
    and — where the profile declares `model:` — how many runs ran on it.
    """
    out: dict[str, dict] = {}
    digests = events.digests()
    if kind == "skills":
        for digest in digests.values():
            for name, (count, last) in digest["skills"].items():
                entry = out.setdefault(name.lower(), {"invoked": 0, "last_used": ""})
                entry["invoked"] += count
                entry["last_used"] = max(entry["last_used"], last)
        return out
    declared = {}
    for asset in context.assets("agents"):
        model = context.declared_model(asset.path)
        if model:
            declared[asset.name.lower()] = model.lower()
    for digest in digests.values():
        for run in digest["subagents"]:
            name = (run["name"] or "").lower()
            entry = out.setdefault(name, {"invoked": 0, "last_used": "",
                                          "honoured": 0})
            entry["invoked"] += 1
            entry["last_used"] = max(entry["last_used"], run["at"])
            if declared.get(name) and (run["model"] or "").lower() == declared[name]:
                entry["honoured"] += 1
    for name, entry in out.items():
        entry["override_honoured"] = (f"{entry.pop('honoured')}/{entry['invoked']}"
                                      if name in declared else None)
    return out


def _print_config_usage(kind: str, names: list[str], inner: int) -> None:
    """The table `cs skills` / `cs profiles` gain: invoked, last used, honoured."""
    usage = _config_usage(kind)
    rows = [(name, usage[name.lower()]) for name in names if name.lower() in usage]
    known = {name.lower() for name in names}
    rows += [(name, entry) for name, entry in usage.items() if name not in known]
    if not rows:
        return
    rows.sort(key=lambda pair: (-pair[1]["invoked"], pair[0].lower()))
    print(ui.heading(f"Invoked · from the event logs · {len(rows)}", ui.MINT, inner))
    columns = [("invoked", "invoked", ">"), ("last", "last used", "<")]
    if kind == "agents":
        columns.append(("honoured", "model kept", ">"))
    columns.append(("name", "name", "<"))
    _table(columns,
           [{"invoked": (str(entry["invoked"]), ui.MINT),
             "last": (_when(entry["last_used"]) if entry["last_used"] else "—",
                      ui.MUTED),
             "honoured": (entry.get("override_honoured") or "—", ""),
             "name": (_clean(name), "")} for name, entry in rows[:_TOP * 2]],
           inner, {"invoked": 7}, [("last", 11), ("honoured", 10)],
           flex="name", least=12)
    if kind == "agents":
        _note("model kept = runs that ran on the model the profile declares, "
              "of all its runs; — when the profile declares none.", inner)
        print()
