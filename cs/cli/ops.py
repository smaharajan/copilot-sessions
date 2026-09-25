"""Operations and trust: the live reading the home screen draws, a doctor
that says whether cs can see what it needs, and a rollup safe to share.

The live reading follows the home screen's timer discipline. The session's
event log is tailed from where the last read stopped — never re-read from
the top, and never digested on the heartbeat.

`rollup` is counts and rates only. Repository names are replaced by a salted
hash whose salt never leaves your settings file; there are no ids, no paths,
no text and no model names in it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .. import (
    db,
    events,
    ui,
)
from ._common import (
    _CURSES_MOUSE_COMPAT,
    _capture,
    _note,
    _page,
    _visible,
)
from .evidence import _UNCLEAN, _clean

# How often the home screen re-reads the live strip.
WATCH_SECONDS = 5
# The burn rate is spend over this many recent minutes.
BURN_MINUTES = 10
# A session quiet for longer than this is not "running now".
LIVE_MINUTES = 15
# The most of a log one tick will read: a session writing faster than this
# is caught up over the next ticks rather than in one long stall.
_TAIL_BYTES = 4 * 1024 * 1024
# Where a first look at a log starts, back from its end.
_TAIL_START = 256 * 1024


# ── Watch ────────────────────────────────────────────────────────────

class _Tail:
    """New lines of one session's event log, read from where we left off."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.path = events.events_path(session_id)
        self.offset: int | None = None
        self.last_tool = ""
        self.last_tool_at = ""
        self.last_failure: tuple[str, str] | None = None
        self.calls = self.failures = 0
        self._names: dict[str, str] = {}

    def read(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if self.offset is None or size < self.offset:
            # First look, or the log was replaced: start near the end, so the
            # last tool is known without reading the whole file.
            self.offset = max(0, size - _TAIL_START)
            skip_partial = self.offset > 0
        else:
            skip_partial = False
        if size == self.offset:
            return
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read(min(size - self.offset, _TAIL_BYTES))
        except OSError:
            return
        end = chunk.rfind(b"\n")
        if end < 0:
            return  # half a line: wait for the rest
        body = chunk[:end + 1]
        self.offset += len(body)
        lines = body.split(b"\n")
        if skip_partial:
            lines = lines[1:]
        for line in lines:
            if not line.startswith((b'{"type":"tool.execution_start"',
                                    b'{"type":"tool.execution_complete"')):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            call = data.get("toolCallId") if isinstance(data.get("toolCallId"), str) else ""
            stamp = event.get("timestamp")
            stamp = stamp[11:19] if isinstance(stamp, str) else ""
            if event.get("type") == "tool.execution_start":
                name = data.get("toolName")
                self._names[call] = name if isinstance(name, str) else "unknown"
                continue
            tool = _clean(self._names.pop(call, "unknown"))
            self.calls += 1
            self.last_tool, self.last_tool_at = tool, stamp
            if data.get("success") is False:
                self.failures += 1
                self.last_failure = (tool, stamp)


def _running(stamp: str) -> bool:
    """Whether a session stamp is recent enough to call the session live."""
    text = (stamp or "").replace(" ", "T")[:16]
    try:
        when = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    age = (datetime.now(timezone.utc) - when).total_seconds()
    return age <= LIVE_MINUTES * 60


def _watch_read(state: dict) -> None:
    """One tick: the session running now, its burn, and the log tail.

    A quiet store is an empty reading, not an error. The tail is bounded:
    the first look starts 256 KB from the end, and a tick reads at most 4 MB.
    """
    conn = db.connect(fatal=False)
    try:
        latest = db.recent_sessions(conn, 1)
        if not latest or not _running(latest[0][1]):
            state["session"] = None
            return
        row = latest[0]
        sid = row[0]
        now = datetime.now(timezone.utc)
        since = time.strftime("%Y-%m-%dT%H:%M:%S",
                              time.gmtime(time.time() - BURN_MINUTES * 60))
        burn = 0
        bins = [0] * BURN_MINUTES
        for when, nano in db.usage_stamps(conn, [sid])[sid]:
            if when < since:
                continue
            burn += nano
            try:
                at = datetime.fromisoformat(when[:19]).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            minute = int((now - at).total_seconds() // 60)
            if 0 <= minute < BURN_MINUTES:
                bins[BURN_MINUTES - 1 - minute] += nano
        today = (db.cost_totals(conn, 1) or {}).get("nano_aiu", 0)
    finally:
        conn.close()
    tail = state.get("tail")
    if tail is None or tail.session_id != sid:
        tail = state["tail"] = _Tail(sid)
    tail.read()
    limit = ui.daily_budget_aiu()
    state["session"] = {
        "id": sid, "summary": _clean(row[2]), "repo": _clean(row[3]),
        "turns": row[5], "nano_aiu": row[6] or 0,
        "burn_per_minute": burn / 1e9 / BURN_MINUTES,
        "burn": bins,
        "today_nano_aiu": today, "budget": limit,
        "left": None if limit is None else limit - today / 1e9,
        "last_tool": tail.last_tool, "last_tool_at": tail.last_tool_at,
        "last_failure": tail.last_failure,
        "calls": tail.calls, "failures": tail.failures,
    }


def _budget_text(session: dict) -> str:
    if session["budget"] is None:
        return "no daily limit"
    left = session["left"]
    if left >= 0:
        return f"{max(left, 0):,.2f} of {session['budget']:g} AIU left today"
    return f"over by {-left:,.2f} AIU today"


def _live_lines(state: dict, width: int) -> list[tuple[str, str]]:
    """The home strip. Nothing — not an error — when no session is running.

    One line below 100 columns. At 100 and wider, a small panel: the title,
    the burn against the budget, and a sparkline of the last ten minutes.
    """
    session = state.get("session")
    if not session or width < 8:
        return []
    burn = f"{session['burn_per_minute']:,.2f} AIU/min"
    budget = _budget_text(session)
    title = session["summary"] or session["id"][:8]
    tool = session["last_tool"] or "no tool yet"
    fail = (f" · fail {session['last_failure'][0]}"
            if session["last_failure"] else "")
    spark = ui.sparkline(session.get("burn") or []).rstrip()
    if width >= 100:
        rows = [
            (f"  ● live  {title}", "active"),
            (f"    burn {burn} · {budget}", "credits"),
            (f"    {spark}  {tool}{fail}".rstrip() if spark else f"    {tool}{fail}",
             "summary"),
        ]
    elif width >= 72:
        rows = [(f"● {title} · {burn} · {budget}", "active")]
    else:
        left = ""
        if session["budget"] is not None and session["left"] is not None:
            left = f" · {max(session['left'], 0):,.0f} left"
        rows = [(f"● {title} · {session['burn_per_minute']:,.2f}/min{left}", "active")]
    return [(ui.trunc(text, width), role) for text, role in rows]


def _watch_lines(state: dict, width: int) -> list[tuple[str, str]]:
    """The live strip, under the name the width test and the tail tests use."""
    return _live_lines(state, width)


# ── Doctor ───────────────────────────────────────────────────────────

def _writable(path: Path) -> bool:
    """Whether cs could create `path` — checked, never tried."""
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return os.access(probe, os.W_OK)


def _terminal() -> dict:
    """TERM, colours, the mouse ABI and whether the compatibility path is on."""
    out = {"term": os.environ.get("TERM", ""), "colours": None,
           "mouse_abi": "", "compat": False}
    try:
        import curses
    except ImportError:  # pragma: no cover — POSIX builds have curses
        return out
    out["mouse_abi"] = ("current (wheel-down reported)"
                        if hasattr(curses, "BUTTON5_PRESSED") else "legacy")
    try:
        curses.setupterm(out["term"] or None, sys.__stdout__.fileno())
        out["colours"] = curses.tigetnum("colors")
        out["compat"] = (not hasattr(curses, "BUTTON5_PRESSED")
                         and curses.tigetstr("kmous") == b"\033[<")
    except Exception:  # noqa: BLE001 — no terminal to ask is an answer too
        out["compat"] = bool(_CURSES_MOUSE_COMPAT)
    return out


def _check(name: str, status: str, detail: str, fix: str = "") -> dict:
    return {"check": name, "status": status, "detail": detail, "fix": fix}


def _doctor_data() -> dict:
    checks = []
    version = sys.version_info
    checks.append(_check(
        "python", "pass" if version >= (3, 10) else "fail",
        f"{version.major}.{version.minor}.{version.micro}",
        "" if version >= (3, 10) else "install Python 3.10 or newer"))
    path = db.default_db_path()
    drift = None
    if not path.exists():
        checks.append(_check("store", "fail", "no session store found",
                             "set COPILOT_HOME to the directory Copilot writes"))
    else:
        try:
            conn = db.connect(fatal=False)
            try:
                count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                drift = db.schema_drift(conn)
            finally:
                conn.close()
            checks.append(_check("store", "pass",
                                 f"opened read-only (mode=ro) · {count:,} sessions"))
        except (OSError, sqlite3.Error) as error:
            checks.append(_check("store", "fail", f"cannot be read: {error}",
                                 "if Copilot is mid-write, try again; otherwise "
                                 "check COPILOT_HOME"))
    if drift is not None:
        # Grouped by table, so a long list wraps between words rather than
        # running `table.column` names off a narrow window.
        missing = [f"{table}: {', '.join(columns)}" for table, columns in
                   drift["missing_columns"].items()]
        missing += [f"the {table} table" for table in drift["missing_tables"]]
        if drift["version"] is None:
            status, detail = "warn", "no schema_version: an older Copilot"
        elif not drift["known_version"]:
            status, detail = "warn", (f"schema_version {drift['version']} is newer than "
                                      f"cs knows ({', '.join(map(str, db.KNOWN_SCHEMA_VERSIONS))})")
        else:
            status, detail = ("warn" if missing else "pass"), (
                f"schema_version {drift['version']}")
        if missing:
            detail += " · missing " + "; ".join(missing)
        checks.append(_check("schema", status, detail,
                             "" if status == "pass" else
                             "views that need what is missing will say less; "
                             "update cs, or report the change"))
    state = db._session_state()
    logs = len(events.session_ids()) if state.is_dir() else 0
    checks.append(_check(
        "session-state", "pass" if logs else "warn",
        f"{logs:,} event log{'' if logs == 1 else 's'}" if logs
        else "no event logs found",
        "" if logs else "tool failures, hooks and sub-agents need Copilot's "
                        "session-state/<id>/events.jsonl"))
    config = ui.settings_path().parent
    checks.append(_check(
        "config dir", "pass" if _writable(config) else "warn",
        "writable" if _writable(config) else "not writable",
        "" if _writable(config) else "set CS_CONFIG_HOME to a writable directory"))
    cache = events.cache_path().parent
    checks.append(_check(
        "cache dir", "pass" if _writable(cache) else "warn",
        "writable" if _writable(cache) else "not writable — digests kept in memory",
        "" if _writable(cache) else "set XDG_CACHE_HOME to a writable directory"))
    term = _terminal()
    colours = term["colours"]
    checks.append(_check(
        "terminal", "warn" if term["term"] in ("", "dumb") else "pass",
        f"TERM={term['term'] or '(unset)'} · "
        f"{'colours unknown' if colours is None else f'{colours} colours'}",
        "run in a terminal with TERM set for colour and the full-screen views"
        if term["term"] in ("", "dumb") else ""))
    checks.append(_check(
        "mouse", "pass",
        f"{term['mouse_abi'] or 'unknown'} ncurses mouse ABI · compatibility "
        f"wrapper {'on' if term['compat'] else 'off'}"))
    checks.append(_check(
        "glyphs", "pass",
        "ascii (CS_GLYPHS=ascii)" if ui._ASCII_GLYPHS else "emoji",
        ""))
    tally = Counter(check["status"] for check in checks)
    return {"checks": checks, "pass": tally["pass"], "warn": tally["warn"],
            "fail": tally["fail"], "schema": drift}


def cmd_doctor() -> bool:
    """Whether cs can see everything it needs, and what to do if not."""
    return _page(_capture(lambda: _render_doctor(_doctor_data())))


_MARKS = {"pass": ("ok", ui.MINT), "warn": ("warn", ui.AMBER), "fail": ("FAIL", ui.ROSE)}


def _render_doctor(data: dict) -> None:
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    print()
    print(ui.rule(inner, f"Doctor · {data['pass']} ok · {data['warn']} warn · "
                         f"{data['fail']} fail"))
    print()
    for check in data["checks"]:
        word, colour = _MARKS[check["status"]]
        print(f"  {colour}{word:<5}{ui.RST}{ui.BOLD}{check['check']}{ui.RST}")
        _note(check["detail"], inner, indent=7)
        if check["fix"]:
            _note(f"→ {check['fix']}", inner, indent=7)
    print()


def schema_notice() -> bool:
    """Whether home should say the schema changed. Cheap; never raises."""
    try:
        conn = db.connect(fatal=False)
    except (OSError, sqlite3.Error):
        return False
    try:
        return db.schema_drift(conn)["drifted"]
    finally:
        conn.close()


# ── Team rollup ──────────────────────────────────────────────────────

def _hash(salt: str, value: str) -> str:
    return hashlib.sha256(f"{salt}\0{value}".encode()).hexdigest()[:12]


def _rollup_data(days: int) -> dict:
    """Counts and rates for the window. No text, ids, names, paths or models."""
    salt = ui.rollup_salt()
    conn = db.connect()
    try:
        rows = _visible(db.recent_sessions(conn, days), False)
        ids = [row[0] for row in rows]
        refs = db.refs_by_session(conn, ids)
        last = db.last_calls(conn)
        totals = db.cost_totals(conn, days) or {}
        efficiency = db.efficiency(conn, days)
        by_day = db.cost_by_day(conn, days)
    finally:
        conn.close()
    digests = events.digests(ids)
    repos: dict[str, dict] = {}
    for row in rows:
        key = _hash(salt, row[3] or row[4] or "") if (row[3] or row[4]) else "none"
        entry = repos.setdefault(key, {"repo": key, "sessions": 0, "nano_aiu": 0,
                                       "tool_calls": 0, "tool_failures": 0})
        entry["sessions"] += 1
        entry["nano_aiu"] += row[6] or 0
        digest = digests.get(row[0])
        if digest:
            entry["tool_calls"] += digest["calls"]
            entry["tool_failures"] += digest["failures"]
    calls = sum(d["calls"] for d in digests.values())
    failures = sum(d["failures"] for d in digests.values())
    endings = Counter(_UNCLEAN[last[sid][1]].replace(" ", "_")
                      for sid in ids if sid in last and last[sid][1] in _UNCLEAN)
    cache = (efficiency or {}).get("cache") or {}
    return {
        "window_days": days,
        "sessions": len(rows),
        "turns": sum(row[5] or 0 for row in rows),
        "nano_aiu": totals.get("nano_aiu", 0),
        "model_calls": totals.get("calls", 0),
        "cache_hit_rate": round(cache["hit_rate"], 4) if cache.get("hit_rate")
        is not None else None,
        "sessions_with_logs": len(digests),
        "tool_calls": calls,
        "tool_failures": failures,
        "tool_failure_rate": round(failures / calls, 4) if calls else None,
        "stuck_loops": sum(len(d["loops"]) for d in digests.values()),
        "subagent_runs": sum(len(d["subagents"]) for d in digests.values()),
        "unclean_endings": dict(endings),
        "sessions_shipped": sum(1 for sid in ids if (refs.get(sid) or {}).get("commit")
                                or (refs.get(sid) or {}).get("pr")),
        "commits": sum((r or {}).get("commit", 0) for r in refs.values()),
        "prs": sum((r or {}).get("pr", 0) for r in refs.values()),
        "repos": sorted(repos.values(), key=lambda r: -r["nano_aiu"]),
        "days": [{"day": day, "nano_aiu": nano, "model_calls": n}
                 for day, nano, n in by_day],
        "privacy": "counts and rates only; repositories are salted sha256 "
                   "hashes (12 hex) whose salt stays in your settings",
    }


def cmd_rollup(days: int = 30) -> bool:
    """The shareable rollup as a report. `--json` is the export, unchanged."""
    return _page(_capture(lambda: _render_rollup(_rollup_data(days))))


def _render_rollup(data: dict) -> None:
    """A report of the rollup: headline, spend shape, repos, endings, privacy."""
    width = min(shutil.get_terminal_size().columns, 96)
    inner = max(width - 4, 20)
    print()
    print(ui.rule(inner, "Team rollup"))
    print()
    rate = data["tool_failure_rate"]
    rate_text = "no tool calls" if rate is None else f"{rate:.1%} tool failures"
    session_word = "session" if data["sessions"] == 1 else "sessions"
    bits = [f"{data['sessions']:,} {session_word}",
            f"{data['nano_aiu'] / 1e9:,.0f} AIU", rate_text]
    if data["stuck_loops"]:
        bits.append(f"{data['stuck_loops']} stuck loops")
    print(f"  {ui.BOLD}{ui._fit(' · '.join(bits), inner)}{ui.RST}")
    series = [day["nano_aiu"] for day in data["days"]]
    spark = ui.sparkline(series).rstrip()
    if spark:
        label = "spend "
        room = max(4, inner - len(label) - 2)
        print(f"  {ui.MUTED}{label}{ui.RST}{ui.VIOLET}{spark[-room:]}{ui.RST}")
    print()
    repos = data["repos"]
    if repos:
        peak = max(repo["nano_aiu"] for repo in repos) or 1
        print(ui.heading(f"Repositories · {len(repos)}", ui.VIOLET, inner))
        shown = repos[:8]
        for repo in shown:
            calls = repo["tool_calls"]
            fail = repo["tool_failures"] / calls if calls else 0
            plain = (f"{repo['repo']}  {repo['sessions']}  "
                     f"{repo['nano_aiu'] / 1e9:,.1f} AIU  {fail:.0%}")
            bar_room = inner - 4 - ui.cells(plain) - 1
            if bar_room >= 6:
                print(f"    {plain} {ui.bar(repo['nano_aiu'], peak, bar_room)}")
            else:
                print(f"    {ui._fit(plain, inner - 4)}")
        extra = len(repos) - len(shown)
        if extra:
            _note(f"+{extra} more", inner, indent=4)
        print()
    endings = data["unclean_endings"]
    if endings:
        print(ui.heading("Unclean endings", ui.AMBER, inner))
        _note(" · ".join(f"{count} {reason.replace('_', ' ')}"
                         for reason, count in endings.items()), inner, indent=4)
        print()
    _note(data["privacy"], inner)
    print()
