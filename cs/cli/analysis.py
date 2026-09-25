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
from collections import Counter
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
    _note,
    _page,
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
    _plural,
    _rate,
    _session_fields,
    _table,
)
from .inventory import _asset_inventory
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
    window_days = [
        {"day": day, "nano_aiu": nano}
        for day, nano in sorted(by_day.items()) if day >= since
    ]
    return {"window_days": days, "factor": ANOMALY_FACTOR,
            "baseline_days": BASELINE_DAYS, "days": spiky_days,
            "sessions": spiky_sessions[:_TOP * 2], "series": window_days}


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
    """Which days and sessions cost far more than usual, and why."""
    inner = _frame("Spend anomalies", data["window_days"])
    flagged = {day["day"] for day in data["days"]}
    series = data.get("series") or []
    if series:
        spark = ui.sparkline([day["nano_aiu"] for day in series])
        marks = "".join("▴" if day["day"] in flagged else " "
                        for day in series[-len(spark):])
        if spark.strip():
            print(f"    {ui.VIOLET}{ui._fit(spark, inner - 4)}{ui.RST}")
            if flagged and len(marks.strip()):
                print(f"    {ui.AMBER}{ui._fit(marks, inner - 4)}{ui.RST}")
    if not data["days"] and not data["sessions"]:
        _note(f"Nothing over {data['factor']:g}× the median of the "
              f"{data['baseline_days']} days before it.", inner)
        print()
        return
    _note(f"Flagged at {data['factor']:g}× the median of the "
          f"{data['baseline_days']} days before. ▴ marks a flagged day.",
          inner, indent=4)
    print()
    # A day whose spend is one session is one story. Say it once.
    owned = {}
    for day in data["days"]:
        by_session: dict[str, int] = {}
        for turn in day["turns"]:
            by_session[turn["id"]] = by_session.get(turn["id"], 0) + turn["nano_aiu"]
        if by_session:
            owner = max(by_session, key=by_session.get)
            if by_session[owner] >= day["nano_aiu"] * 0.6:
                owned[day["day"]] = owner
    sessions = {session["id"]: session for session in data["sessions"]}
    cards = []
    seen_sessions = set()
    for day in sorted(data["days"], key=lambda item: -item["factor"])[:5]:
        owner = owned.get(day["day"])
        twin = sessions.get(owner) if owner else None
        if twin and twin["day"] == day["day"]:
            seen_sessions.add(owner)
            cards.append(("day", day, twin))
        else:
            cards.append(("day", day, None))
    for session in data["sessions"]:
        if session["id"] in seen_sessions:
            continue
        if len([card for card in cards if card[0] == "session" or card[2]]) >= 5:
            break
        cards.append(("session", None, session))
        if sum(1 for kind, _day, twin in cards if kind == "session" or twin) >= 5 \
                and len(cards) >= 5:
            break
    cards = cards[:5]
    extra = max(0, len(data["days"]) + len(data["sessions"]) - len(cards)
                - len(seen_sessions))
    for _kind, day, session in cards:
        if day and session:
            title = (f"{day['day']} · {session['summary'] or session['id'][:8]}")
            multiple = (f"{day['factor']:g}× usual · "
                        f"{day['nano_aiu'] / 1e9:,.2f} AIU")
            turns = day["turns"][:4]
            where = session
        elif day:
            title = day["day"]
            multiple = (f"{day['factor']:g}× usual · "
                        f"{day['nano_aiu'] / 1e9:,.2f} AIU")
            turns = day["turns"][:4]
            where = None
        else:
            title = session["summary"] or "(untitled)"
            multiple = (f"{session['factor']:g}× usual · "
                        f"{session['session_nano_aiu'] / 1e9:,.2f} AIU")
            turns = session["turns"][:4]
            where = session
        print(ui.heading(ui._fit(f"{title} · {multiple}", inner - 4),
                         ui.VIOLET, inner))
        _turn_table(turns, inner)
        if where and where.get("n"):
            top = turns[0]["turn"] if turns else None
            hint = f"cs read {where['n']}"
            if top is not None:
                hint += f" --turn {top}"
            _note(hint, inner, indent=4)
        print()
    if extra > 0:
        _note(f"+{extra} more", inner)
    _note("cs efficiency — cache and effort across the window", inner)
    print()


def _turn_table(turns: list[dict], inner: int) -> None:
    """The turns that drove a spike: model, effort and cache, in one block."""
    if not turns:
        return
    for turn in turns[:5]:
        where = f"turn {turn['turn']}" if turn["turn"] is not None else "turn"
        bits = [", ".join(turn["models"]) or "model not recorded"]
        if turn["effort"]:
            bits.append(turn["effort"])
        bits.append(f"cache {_cache(turn['cache_hit'])}")
        text = f"{ui.fmt_aiu(turn['nano_aiu'])}  {where}  {' · '.join(bits)}"
        _note(text, inner, indent=4)


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


def _judgement(kind: str) -> tuple[str, str]:
    """(word, colour) for a health number. good, watch, act."""
    return {
        "good": ("good", ui.MINT),
        "watch": ("watch", ui.AMBER),
        "act": ("act", ui.ROSE),
    }[kind]


def _render_health(data: dict) -> None:
    """A card: a few numbers with a verdict, then the things worth doing."""
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
    rate = data["failure_rate"]
    tiles = []
    tiles.append(("sessions", f"{data['sessions']:,}", "good"))
    tiles.append(("spend", f"{data['nano_aiu'] / 1e9:,.1f} AIU", "good"))
    if rate is None:
        tiles.append(("failures", "no logs", "watch"))
    elif rate >= 0.05:
        tiles.append(("failures", f"{rate:.1%}", "act"))
    elif rate >= 0.02:
        tiles.append(("failures", f"{rate:.1%}", "watch"))
    else:
        tiles.append(("failures", f"{rate:.1%}", "good"))
    oversized = data["instructions_over_limit"]
    if oversized is not None:
        tiles.append(("instructions", str(len(oversized)),
                      "act" if oversized else "good"))
    unused = data["skills_unused"]
    if unused is not None and data["skills_available"]:
        share = len(unused) / data["skills_available"]
        tiles.append(("unused skills", str(len(unused)),
                      "act" if share > 0.5 else "watch" if unused else "good"))
    hook_fails = sum(data["hooks_failing"].values())
    tiles.append(("hook failures", str(hook_fails), "act" if hook_fails else "good"))
    open_n = len(data["handoffs_open"])
    tiles.append(("open handoffs", str(open_n), "watch" if open_n else "good"))
    tiles = tiles[:6]
    # Two columns when the window can hold them, one when it cannot.
    columns = 2 if inner >= 48 else 1
    for start in range(0, len(tiles), columns):
        row = tiles[start:start + columns]
        parts = []
        for label, value, kind in row:
            word, colour = _judgement(kind)
            cell = f"{colour}{word:<5}{ui.RST} {value} {ui.MUTED}{label}{ui.RST}"
            parts.append(cell)
        print("    " + "    ".join(parts))
    print()
    actions = []
    if rate is not None and rate >= 0.02:
        actions.append("cs failures — which tools fail here, and where")
    if oversized:
        actions.append("cs instructions — files past the length Copilot reads")
    if unused:
        actions.append("cs skills — installed skills this repo never reached for")
    if hook_fails:
        actions.append("cs hooks — events whose commands are failing")
    if open_n:
        actions.append("cs handoff — work passed on and not picked up")
    if actions:
        print(ui.heading("Worth doing", ui.ACCENT, inner))
        for action in actions[:3]:
            _note(action, inner, indent=4)
        print()
    if data["top_files"]:
        print(ui.heading("Files agents edit most", ui.MINT, inner))
        for item in data["top_files"][:3]:
            print(f"    {ui.MINT}{item['sessions']:>4}{ui.RST}  "
                  f"{ui._fit(item['path'], inner - 10)}")
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
    """One comparison: which opening habits line up with shipping."""
    inner = _frame("Prompt patterns", data["window_days"])
    if not data["sessions"]:
        _note("No session in this window has an opening request to read.", inner)
        print()
        return
    kept, hidden = [], 0
    for feature in data["features"]:
        if (feature["with"]["sessions"] < 5
                or feature["without"]["sessions"] < 5):
            hidden += 1
        else:
            kept.append(feature)
    _headline(f"Opening requests of {_plural(data['sessions'], 'session')}",
              inner)
    if kept:
        def gap(feature: dict) -> float:
            left, right = feature["with"]["shipped_rate"], feature["without"]["shipped_rate"]
            if left is None or right is None:
                return 0.0
            return abs(left - right)

        lead = max(kept, key=gap)
        left, right = lead["with"]["shipped_rate"], lead["without"]["shipped_rate"]
        if left is not None and right is not None and left != right:
            higher = "with" if left > right else "without"
            _note(f"Strongest: {lead['feature']} — shipped {higher} it "
                  f"{_pct(max(left, right))} against {_pct(min(left, right))}. "
                  f"Correlation, not causation.", inner, indent=4)
        print()
        # Features down the page, with and without as two shipped bars.
        if inner < 48:
            name_w, bar_w = 12, 4
        elif inner < 64:
            name_w, bar_w = 18, 6
        else:
            name_w, bar_w = 28, 10
        head = (f"{'feature':<{name_w}} {'with':>{bar_w + 5}}  "
                f"{'without':>{bar_w + 5}}")
        print(f"    {ui.MUTED}{ui._fit(head, inner - 4)}{ui.RST}")
        print(f"    {ui.MUTED}{'─' * min(ui.cells(head), inner - 4)}{ui.RST}")
        for feature in kept:
            label = ui._fit(feature["feature"], name_w)
            cells = []
            for side in ("with", "without"):
                reading = feature[side]
                rate = reading["shipped_rate"] or 0
                cells.append(f"{ui.meter(rate, bar_w, ui.MINT)} {_pct(reading['shipped_rate']):>4}")
            print(f"    {label} {'  '.join(cells)}")
        print()
    else:
        _note("Not enough sessions on both sides of any habit to compare.",
              inner)
        print()
    if hidden:
        _note(f"{hidden} hidden · under 5 is too few to read anything into. "
              f"Correlation, not causation.", inner)
    else:
        _note("Correlation, not causation. Shipped means a commit or a PR.",
              inner)
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
