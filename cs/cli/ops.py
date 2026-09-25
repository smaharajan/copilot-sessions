"""Operations and trust: a live pane for the session running now, a doctor
that says whether cs can see what it needs, and a rollup safe to share.

`watch` follows the same timer discipline as the home screen: the timeout
is re-armed immediately before every read, so nothing an input handler did
can leave it blocking, and the store is re-read on a fixed cadence rather
than on every key. The session's event log is tailed from where the last
read stopped — never re-read from the top.

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
from pathlib import Path

from .. import (
    db,
    events,
    ui,
)
from ._common import (
    _CURSES_MOUSE_COMPAT,
    _addstr,
    _capture,
    _curses_wrapper,
    _note,
    _page,
    _visible,
    _window_label,
)
from .evidence import _UNCLEAN, _clean

# How often the watch pane re-reads the store and the log tail.
WATCH_SECONDS = 5
# The burn rate is spend over this many recent minutes.
BURN_MINUTES = 10
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


def _watch_read(state: dict) -> None:
    """One tick: the latest session, its spend and burn, and the log tail."""
    conn = db.connect(fatal=False)
    try:
        latest = db.recent_sessions(conn, 1) or db.recent_sessions(conn, 0)
        if not latest:
            state["session"] = None
            return
        row = latest[0]
        sid = row[0]
        since = time.strftime("%Y-%m-%dT%H:%M:%S",
                              time.gmtime(time.time() - BURN_MINUTES * 60))
        burn = 0
        for when, nano in db.usage_stamps(conn, [sid])[sid]:
            if when >= since:
                burn += nano
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
        "today_nano_aiu": today, "budget": limit,
        "left": None if limit is None else limit - today / 1e9,
        "last_tool": tail.last_tool, "last_tool_at": tail.last_tool_at,
        "last_failure": tail.last_failure,
        "calls": tail.calls, "failures": tail.failures,
    }


def _watch_lines(state: dict, width: int) -> list[tuple[str, str]]:
    """(text, theme role) per row of the pane — drawn and printed alike."""
    session = state.get("session")
    if not session:
        return [("◆  Watch · no session recorded yet", "title")]
    rows = [(f"◆  Watch · {session['summary'] or session['id'][:8]}", "title"), ("", "")]

    def field(label: str, value: str, role: str = "summary") -> None:
        rows.append((f"  {label:<10}{value}", role))

    field("session", f"{session['id'][:8]}"
          + (f" · {session['repo']}" if session["repo"] else ""), "repo")
    field("turns", f"{session['turns']:,}")
    field("burn", f"{session['burn_per_minute']:,.2f} AIU/min · last "
                  f"{BURN_MINUTES} min", "credits")
    field("spent", f"{session['nano_aiu'] / 1e9:,.2f} AIU this session", "credits")
    if session["budget"] is None:
        field("budget", "no daily limit set", "help")
    else:
        left = session["left"]
        field("budget", f"{max(left, 0):,.2f} of {session['budget']:g} AIU left today"
              if left >= 0 else f"over by {-left:,.2f} AIU today",
              "danger" if left < 0 else "warn" if left < session["budget"] * 0.3
              else "active")
    if session["last_tool"]:
        field("last tool", f"{session['last_tool']} · {session['last_tool_at']}")
    else:
        field("last tool", "none in the log's tail", "help")
    if session["last_failure"]:
        tool, at = session["last_failure"]
        field("last fail", f"{tool} · {at}", "danger")
    else:
        field("last fail", "none seen", "help")
    if session["calls"]:
        field("seen", f"{session['calls']:,} tool calls · {session['failures']} failed")
    return [(ui.trunc(text, width), role) for text, role in rows]


def _watch_tui(screen, state: dict) -> None:
    """The live pane. q, Esc: back. Re-reads every WATCH_SECONDS."""
    import curses

    screen.keypad(True)
    theme = ui.tui_theme(curses)
    try:
        curses.curs_set(0)
        screen.bkgd(" ", theme["background"])
    except curses.error:
        pass
    next_read = time.monotonic()
    while True:
        if time.monotonic() >= next_read:
            try:
                _watch_read(state)
                state["updated"] = time.strftime("%H:%M:%S")
                state.pop("error", None)
            except (OSError, sqlite3.Error):
                state["error"] = True
            next_read += WATCH_SECONDS
            if next_read <= time.monotonic():
                next_read = time.monotonic() + WATCH_SECONDS
        screen.erase()
        height, width = screen.getmaxyx()
        for row, (text, role) in enumerate(_watch_lines(state, width)[:height - 1]):
            _addstr(screen, row, 0, text, width, theme.get(role, theme["summary"]))
        status = (" read failed · retrying" if state.get("error") else
                  f" updated {state.get('updated', '')} · every {WATCH_SECONDS}s · q back ")
        _addstr(screen, height - 1, 0, ui.trunc(status, width), width, theme["status"])
        screen.refresh()
        # Re-armed before every read, like home: nothing can leave it blocking.
        try:
            screen.timeout(max(1, min(1000, round((next_read - time.monotonic())
                                                   * 1000))))
        except (AttributeError, curses.error):
            pass
        try:
            key = screen.getch()
        except KeyboardInterrupt:
            return
        if key in (ord("q"), ord("Q"), 27):
            return


def cmd_watch() -> bool:
    """A live pane for the session running now. q returns."""
    state: dict = {}
    if sys.stdin.isatty() and sys.stdout.isatty():
        import curses

        try:
            _curses_wrapper(_watch_tui, state)
            return True
        except curses.error:
            pass
    _watch_read(state)
    for text, _role in _watch_lines(state, min(shutil.get_terminal_size().columns, 96)):
        print(text)
    return False


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
    """The shareable rollup, as the JSON it is — paged when it is long."""
    from .. import export

    text = json.dumps(export._stamped({"view": "rollup", **_rollup_data(days)}),
                      indent=2, default=str)
    return _page(f"\n  {ui.DIM}Team rollup · {_window_label(days)} · counts and "
                 f"rates only — share with 'cs rollup --json'{ui.RST}\n\n{text}\n")
