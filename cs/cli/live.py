"""Live sessions: every Copilot CLI running now, on one page.

A running CLI holds a lock file in its session folder, so the page counts
processes rather than guessing from how recently the store moved. What each
one is doing comes from the tail of its event log, read from where the last
look stopped: the tool open now, whether it is waiting on you, what you last
asked and what it last said. Spend and burn come from the store.

Everything shown is an inference from events, and says which: "asking you"
is an open ask_user call or a permission prompt, "your turn" is a turn that
ended with nothing after it, "failing" is three failed calls in a row.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import textwrap
import time
from collections import Counter, deque
from datetime import datetime, timezone

from .. import db, events, ui
from ._common import (
    _addstr,
    _curses_wrapper,
    _disable_mouse,
    _enable_mouse,
    _mouse_event,
)
from .evidence import _clean
from .session import cmd_show

# How often the page re-reads every session.
LIVE_REFRESH_SECONDS = 2
# Burn is spend over the last ten minutes; the wave shows thirty, in
# half-minute steps.
_LIVE_BURN_MINUTES = 10
_LIVE_BINS = 60
_LIVE_BIN_SECONDS = 30
# How many recent events each session keeps for the activity feed.
_LIVE_FEED = 60
# Headline readings kept for each tile's trend line: two minutes at 2s.
_LIVE_TREND = 60
# Waiting on you for longer than this is idle rather than waiting.
_LIVE_IDLE_SECONDS = 30 * 60
# A turn that ended this recently may have another starting behind it.
_LIVE_SETTLE_SECONDS = 3
# Failed calls in a row that make a session "failing".
_LIVE_STREAK = 3
# The first look at a log starts this far back — far enough to find the
# last thing you asked — and one read takes at most _LIVE_TAIL_BYTES.
_LIVE_START = 1024 * 1024
_LIVE_TAIL_BYTES = 4 * 1024 * 1024
# Rows deal onto the page one after another when it opens, each typing in.
_LIVE_DEAL_SECONDS = 0.07
_LIVE_ROW_TYPE_SECONDS = 0.25
# A reply you have not seen yet types into the detail panel.
_LIVE_TYPE_SECONDS = 0.45
# The spinner's step while something is running.
_LIVE_SPIN_MS = 110
_LIVE_SPIN = "|/-\\" if ui._ASCII_GLYPHS else "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_LIVE_THINK = ".oOo" if ui._ASCII_GLYPHS else "◐◓◑◒"

# Most urgent first: the page sorts by this, then by the newest event.
_LIVE_STATES: dict[str, tuple[str, str, str]] = {
    # status: (mark, label, role)
    "asking": ("▲", "asking you", "warn"),
    "failing": ("■", "failing", "danger"),
    "waiting": ("◆", "your turn", "credits"),
    "working": ("●", "working", "active"),
    "thinking": ("◐", "thinking", "turns"),
    "idle": ("○", "idle", "separator"),
}
_LIVE_ORDER = tuple(_LIVE_STATES)

_LIVE_KINDS = frozenset({
    b"tool.execution_start", b"tool.execution_complete",
    b"assistant.turn_start", b"assistant.turn_end", b"assistant.message",
    b"user.message", b"subagent.started", b"subagent.completed",
    b"subagent.failed", b"session.model_change", b"session.start",
    b"session.resume", b"session.compaction_start",
    b"session.compaction_complete",
    b"session.usage_checkpoint", b"permission.requested",
    b"permission.completed", b"session.error", b"abort", b"session.shutdown",
})

# What a tool call is about, by the argument that says so best.
_LIVE_DETAIL_KEYS = ("description", "intent", "path", "pattern", "query",
                     "skill", "url", "name", "question", "message")


def _live_when(stamp) -> float:
    if not isinstance(stamp, str):
        return 0.0
    try:
        return datetime.fromisoformat(stamp[:19]).replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _live_detail(name: str, arguments) -> str:
    if not isinstance(arguments, dict):
        return ""
    for key in _LIVE_DETAIL_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return os.path.basename(value.rstrip("/")) if key == "path" else value
    return ""


class _LiveTail:
    """What one running session is doing, read incrementally from its log."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.path = events.events_path(session_id)
        self.offset: int | None = None
        # call id -> (tool, what it is about, when it started)
        self.open: dict[str, tuple[str, str, float]] = {}
        self.agents: dict[str, tuple[str, float]] = {}
        self.agent_tool: dict[str, str] = {}
        self.permissions: dict[str, float] = {}
        self.kind = ""
        self.at = 0.0
        self.model = self.effort = self.intent = ""
        self.asked = self.said = self.error = self.last_failure = ""
        self.last_tool = ""
        self.calls = self.failures = self.streak = self.compactions = 0
        self.nano = 0
        self.compacting = False
        self.tools: Counter = Counter()
        # (serial, when, mark, role, what, raw text) — cleaned only if shown.
        self.feed: deque = deque(maxlen=_LIVE_FEED)
        self.serial = 0
        self.cleaned: dict[int, str] = {}

    def note(self, at: float, mark: str, role: str, what: str, text="") -> None:
        self.serial += 1
        self.feed.append((self.serial, at, mark, role, what,
                          text if isinstance(text, str) else ""))

    def clean(self, serial: int, raw: str) -> str:
        if serial not in self.cleaned:
            if len(self.cleaned) > 2 * _LIVE_FEED:
                keep = {entry[0] for entry in self.feed}
                self.cleaned = {k: v for k, v in self.cleaned.items() if k in keep}
            self.cleaned[serial] = _live_plain(_clean(raw))
        return self.cleaned[serial]

    def read(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        skip_partial = False
        if self.offset is None or size < self.offset:
            self.offset = max(0, size - _LIVE_START)
            skip_partial = self.offset > 0
        if size == self.offset:
            return
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read(min(size - self.offset, _LIVE_TAIL_BYTES))
        except OSError:
            return
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        body = chunk[:end + 1]
        self.offset += len(body)
        lines = body.split(b"\n")
        for line in lines[1:] if skip_partial else lines:
            if line[:9] != b'{"type":"':
                continue
            if line[9:line.find(b'"', 9)] not in _LIVE_KINDS:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                self._take(event)

    def _take(self, event: dict) -> None:
        kind = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        at = _live_when(event.get("timestamp")) or self.at
        agent = event.get("agentId")
        self.at = max(self.at, at)
        if isinstance(data.get("model"), str) and data["model"] and not agent:
            self.model = data["model"]
        call = data.get("toolCallId") if isinstance(data.get("toolCallId"), str) else ""
        if kind == "tool.execution_start":
            name = data.get("toolName") if isinstance(data.get("toolName"), str) else "tool"
            detail = _live_detail(name, data.get("arguments"))
            if agent:
                self.agent_tool[agent] = name
            if name == "report_intent" and detail:
                self.intent = detail
            if not agent:
                self.open[call] = (name, detail, at)
                if name == "ask_user":
                    self.note(at, "▲", "warn", "asks you", detail)
                elif name == "report_intent":
                    self.note(at, "◇", "turns", "intent", detail)
                else:
                    self.note(at, "▸", "active", name, detail)
            else:
                self.open[call] = (name, detail, -1.0)  # a sub-agent's call
            return
        if kind == "tool.execution_complete":
            name = self.open.pop(call, ("tool", "", 0.0))
            self.calls += 1
            self.tools[name[0]] += 1
            if data.get("success") is False:
                self.failures += 1
                self.last_failure = name[0]
                if name[2] >= 0:
                    self.streak += 1
                    self.note(at, "✗", "danger", name[0], "failed")
            elif name[2] >= 0:
                self.streak = 0
            if name[2] >= 0 and name[0] != "report_intent":
                self.last_tool = f"{name[0]} · {name[1]}" if name[1] else name[0]
            return
        if kind == "subagent.started" and isinstance(agent, str):
            label = data.get("agentDisplayName") or data.get("agentName") or "agent"
            self.agents[agent] = (str(label), at)
            self.note(at, "»", "turns", "sub-agent", str(label))
            return
        if kind in ("subagent.completed", "subagent.failed"):
            label = self.agents.get(agent, ("agent", 0))[0]
            if kind == "subagent.failed":
                self.note(at, "✗", "danger", "agent failed", label)
            else:
                self.note(at, "✓", "turns", "agent done", label)
            self.agents.pop(agent, None)
            self.agent_tool.pop(agent, None)
            return
        if agent:
            return  # a sub-agent's own turns say nothing about the session
        if kind == "permission.requested":
            self.permissions[str(data.get("requestId"))] = at
            self.note(at, "▲", "warn", "permission",
                      str(data.get("toolName") or data.get("kind") or ""))
        elif kind == "permission.completed":
            self.permissions.pop(str(data.get("requestId")), None)
        elif kind == "session.model_change":
            self.model = str(data.get("newModel") or self.model)
            self.effort = str(data.get("reasoningEffort") or "")
        elif kind in ("session.start", "session.resume"):
            self.model = str(data.get("selectedModel") or self.model)
            self.effort = str(data.get("reasoningEffort") or self.effort)
            self.open.clear()
            self.agents.clear()
            self.permissions.clear()
            self.compacting = False
            self.kind = kind
            self.note(at, "○", "help",
                      "started" if kind == "session.start" else "resumed")
        elif kind == "session.compaction_start":
            self.compacting = True
            self.note(at, "◐", "warn", "compacting", "the conversation")
        elif kind == "session.compaction_complete":
            self.compacting = False
            self.compactions += 1
        elif kind == "session.usage_checkpoint":
            try:
                self.nano = int(data.get("totalNanoAiu") or 0)
            except (TypeError, ValueError):
                pass
        elif kind == "user.message":
            self.asked = str(data.get("content") or "")
            self.error = ""
            self.kind = kind
            self.note(at, "●", "credits", "you", self.asked)
        elif kind == "assistant.message":
            if isinstance(data.get("content"), str) and data["content"].strip():
                self.said = data["content"]
                self.note(at, "◆", "title", "agent", self.said)
            self.kind = kind
        elif kind == "session.error":
            self.error = str(data.get("message") or "error")
            self.kind = kind
            self.note(at, "■", "danger", "error", self.error)
        elif kind == "abort":
            self.open = {k: v for k, v in self.open.items() if v[2] < 0}
            self.permissions.clear()
            self.kind = kind
            self.note(at, "✗", "warn", "interrupted")
        elif kind in ("assistant.turn_start", "assistant.turn_end",
                      "session.shutdown"):
            self.kind = kind


def _live_plain(text: str) -> str:
    """One line of prose without the markdown it was written in."""
    return " ".join(text.replace("**", "").replace("`", "").split())


def _live_span(seconds: float) -> str:
    """12s, 4m, 1h 02m, 3d 4h — the shortest honest length of time."""
    seconds = max(int(seconds), 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h"


def _live_status(tail: _LiveTail, now: float) -> tuple[str, str, float]:
    """(status, what it is doing, since when) — and the evidence is the text."""
    main = {call: item for call, item in tail.open.items() if item[2] >= 0}
    asking = [item for item in main.values() if item[0] == "ask_user"]
    if asking or tail.permissions:
        since = min([item[2] for item in asking] + list(tail.permissions.values()))
        return ("asking", "open question for you" if asking
                else "wants permission to run a tool", since)
    if tail.streak >= _LIVE_STREAK:
        return ("failing", f"{tail.streak} failed calls in a row · last "
                           f"{tail.last_failure}", tail.at)
    if tail.kind == "session.error" and tail.error:
        return "failing", f"error: {tail.error}", tail.at
    if main:
        name, detail, since = max(main.values(), key=lambda item: item[2])
        return "working", f"{name} · {detail}" if detail else name, since
    if tail.agents:
        names = Counter(label for label, _at in tail.agents.values())
        text = ", ".join(f"{label} ×{n}" if n > 1 else label
                         for label, n in names.most_common())
        count = len(tail.agents)
        return ("working",
                f"{count} sub-agent{'s' if count > 1 else ''}: {text}",
                min(at for _label, at in tail.agents.values()))
    if tail.compacting:
        return "thinking", "compacting the conversation", tail.at
    quiet = now - tail.at if tail.at else _LIVE_SETTLE_SECONDS
    if tail.kind in ("assistant.turn_end", "abort", "session.shutdown",
                     "session.start", "session.resume", "") \
            and quiet >= _LIVE_SETTLE_SECONDS or quiet >= _LIVE_IDLE_SECONDS:
        if quiet >= _LIVE_IDLE_SECONDS:
            return "idle", "no activity", tail.at
        what = "interrupted · your turn" if tail.kind == "abort" else "your turn"
        return "waiting", what, tail.at
    return "thinking", "writing a reply", tail.at


def _live_session(sid: str, pid: int, row: tuple | None,
                  stamps: list[tuple[str, int]], tail: _LiveTail,
                  now: float) -> dict:
    """One card's worth of facts. All text arrives here already cleaned."""
    fields = db.session_workspace(
        sid, ("name", "repository", "branch", "cwd", "created_at"))
    title = (row[2] if row and row[2] else "") or fields.get("name", "")
    try:
        lock = db._session_state() / sid / f"inuse.{pid}.lock"
        up = now - lock.stat().st_mtime
    except OSError:
        up = 0.0
    burn_since = now - _LIVE_BURN_MINUTES * 60
    bins = [0] * _LIVE_BINS
    burn = spent = 0
    for when, nano in stamps:
        spent += nano
        at = _live_when(when.replace(" ", "T"))
        if at >= burn_since:
            burn += nano
        step = int((now - at) // _LIVE_BIN_SECONDS)
        if 0 <= step < _LIVE_BINS:
            bins[_LIVE_BINS - 1 - step] += nano
    todos = db.session_todos(sid)
    done = sum(status == "done" for status, _ in todos)
    current = next((title for status, title in todos if status == "in_progress"),
                   next((title for status, title in todos
                         if status not in ("done",)), ""))
    status, doing, since = _live_status(tail, now)
    return {
        "id": sid, "pid": pid,
        "title": _clean(title) or sid[:8],
        "repo": _clean(row[3] if row and row[3] else fields.get("repository", "")),
        "branch": _clean(fields.get("branch", "")),
        "cwd": _clean(row[4] if row and row[4] else fields.get("cwd", "")),
        "model": _clean(tail.model), "effort": _clean(tail.effort),
        "status": status, "doing": _clean(doing),
        "for": max(now - since, 0) if since else 0.0,
        "quiet": max(now - tail.at, 0) if tail.at else None,
        "last": tail.at, "up": up,
        "turns": row[5] if row else 0,
        "nano": max(row[6] if row else 0, spent, tail.nano),
        "burn_per_minute": burn / 1e9 / _LIVE_BURN_MINUTES,
        "bins": bins,
        "calls": tail.calls, "failures": tail.failures,
        "tools": [(_clean(name), n) for name, n in tail.tools.most_common(3)],
        "agents": len(tail.agents), "compactions": tail.compactions,
        "intent": _clean(tail.intent), "after": _clean(tail.last_tool),
        "asked": _live_plain(_clean(tail.asked)),
        "said": _live_plain(_clean(tail.said)),
        "todos": (done, len(todos), _clean(current)),
    }


def _live_read(state: dict) -> None:
    """One refresh: which CLIs are running, and what each one is doing."""
    running = db.running_sessions()
    ids = [sid for sid, _pid in running]
    rows: dict[str, tuple] = {}
    stamps: dict[str, list] = {}
    asks: dict[str, str] = {}
    today = 0
    try:
        conn = db.connect(fatal=False)
    except (OSError, sqlite3.Error):
        conn = None
    if conn is not None:
        try:
            rows = db.sessions_by_id(conn, ids)
            stamps = db.usage_stamps(conn, ids)
            asks = db.last_asks(conn, ids)
            today = (db.cost_totals(conn, 1) or {}).get("nano_aiu", 0) or 0
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    tails = state.setdefault("tails", {})
    for sid in [sid for sid in tails if sid not in ids]:
        del tails[sid]
    now = time.time()
    sessions = []
    for sid, pid in running:
        tail = tails.get(sid)
        if tail is None:
            tail = tails[sid] = _LiveTail(sid)
        tail.read()
        if not tail.asked:
            tail.asked = asks.get(sid, "")
        sessions.append(_live_session(sid, pid, rows.get(sid),
                                      stamps.get(sid, []), tail, now))
    sessions.sort(key=lambda item: (_LIVE_ORDER.index(item["status"]),
                                    -item["last"]))
    before = state.get("statuses")
    statuses = {item["id"]: item["status"] for item in sessions}
    marks = state.setdefault("marks", {})
    if before is not None:
        for sid, status in statuses.items():
            if before.get(sid) != status:
                marks[sid] = time.monotonic()
    state["statuses"] = statuses
    # Each session keeps one colour for as long as it runs, so its row, its
    # panel and its lines in the feed can be told apart at a glance.
    chips = {sid: n for sid, n in state.get("chips", {}).items() if sid in statuses}
    for item in sessions:
        if item["id"] not in chips:
            chips[item["id"]] = next(n for n in range(len(chips) + 1)
                                     if n not in chips.values())
        item["chip"] = chips[item["id"]]
    state["chips"] = chips
    feed = []
    for item in sessions:
        tail = tails[item["id"]]
        for serial, at, mark, role, what, raw in list(tail.feed)[-_LIVE_FEED // 2:]:
            feed.append({"at": at, "id": item["id"], "title": item["title"],
                         "chip": item["chip"], "mark": mark, "role": role,
                         "what": _clean(what), "detail": tail.clean(serial, raw),
                         "key": (item["id"], serial)})
    feed.sort(key=lambda entry: -entry["at"])
    feed = feed[:_LIVE_FEED]
    # What arrived since the last look lights up as it lands; the first look
    # is history, not news.
    arrivals = state.setdefault("arrivals", {})
    keys = {entry["key"] for entry in feed}
    if "feed_keys" in state:
        for key in keys - state["feed_keys"]:
            arrivals[key] = time.monotonic()
    state["feed_keys"] = keys
    limit = ui.daily_budget_aiu()
    counts = Counter(item["status"] for item in sessions)
    trend = state.setdefault("trend", {})
    for key, value in (
            ("live", len(sessions)),
            ("need", counts["asking"] + counts["waiting"]),
            ("busy", counts["working"] + counts["thinking"]),
            ("failing", counts["failing"]), ("today", today / 1e9),
            ("calls", sum(item["calls"] for item in sessions)),
            ("agents", sum(item["agents"] for item in sessions))):
        trend.setdefault(key, deque(maxlen=_LIVE_TREND)).append(value)
    state["snapshot"] = {
        "sessions": sessions,
        "counts": counts,
        "trend": {key: list(values) for key, values in trend.items()},
        "burn_per_minute": sum(item["burn_per_minute"] for item in sessions),
        "bins": [sum(column) for column in
                 zip(*(item["bins"] for item in sessions), strict=True)] if sessions else [],
        "today_nano_aiu": today, "budget": limit,
        "calls": sum(item["calls"] for item in sessions),
        "failures": sum(item["failures"] for item in sessions),
        "repos": len({item["repo"] for item in sessions if item["repo"]}),
        "agents": sum(item["agents"] for item in sessions),
        "longest": max((item["up"] for item in sessions), default=0),
        "feed": feed,
        "refreshed": time.strftime("%H:%M:%S"),
    }


# ── Drawing ──────────────────────────────────────────────────────────
# The page is rows of (x, text, role) segments, so the screen, the printed
# form and the width tests all read the same thing. Nothing here reads the
# clock: everything that moves arrives in `motion`, which is what lets the
# tests play the page frame by frame.

_LIVE_SPARKS = " ▁▂▃▄▅▆▇█"


def _live_line(parts: list[tuple[str, str]], width: int,
               x: int = 0) -> list[tuple[int, str, str]]:
    """Segments laid end to end from `x`, clipped to `width` cells in all."""
    out = []
    used = 0
    for text, role in parts:
        if used >= width:
            break
        text = ui.clip(text, width - used)
        if text:
            out.append((x + used, text, role))
            used += ui.cells(text)
    return out


def _live_right(parts: list[tuple[str, str]], right: int
                ) -> list[tuple[int, str, str]]:
    """Segments laid end to end so the last one ends at column `right`."""
    total = min(sum(ui.cells(text) for text, _ in parts), max(right, 0))
    return _live_line(parts, total, right - total)


def _live_bar(done: int, total: int, cells: int) -> str:
    filled = round(cells * done / total) if total else 0
    return "█" * filled + "░" * (cells - filled)


def _live_short(value: float) -> str:
    """A headline number: 8, 46.0, 1.25k, 15.7k."""
    if value >= 1e6:
        return f"{value / 1e6:.1f}M"
    if value >= 1e4:
        return f"{value / 1e3:.1f}k"
    if value >= 1e3:
        return f"{value / 1e3:.2f}k"
    return f"{value:.0f}" if value >= 100 or value == int(value) else f"{value:.1f}"


def _live_clock(seconds: float) -> str:
    """A running timer: 0:07, 4:12, then 1h 02m."""
    seconds = max(int(seconds), 0)
    if seconds < 3600:
        return f"{seconds // 60}:{seconds % 60:02d}"
    return _live_span(seconds)


def _live_pool(values: list[int], cells: int) -> list[float]:
    """`values` stretched or summed into exactly `cells` columns."""
    count = len(values)
    if not count or cells <= 0:
        return [0] * max(cells, 0)
    if cells >= count:
        if count == 1 or cells == 1:
            return [values[-1]] * cells
        # Stretched by drawing the line between neighbours, not by repeating
        # each one: a repeated column reads as a staircase.
        out = []
        for column in range(cells):
            at = column * (count - 1) / (cells - 1)
            low = int(at)
            high = min(low + 1, count - 1)
            out.append(values[low] + (values[high] - values[low]) * (at - low))
        return out
    return [sum(values[i * count // cells:(i + 1) * count // cells])
            for i in range(cells)]


def _live_wave(values: list[int], cells: int, rows: int = 1,
               grow: float = 1.0) -> tuple[list[str], set[int]]:
    """A sparkline `rows` tall, top row first, and the columns with nothing.

    A column with nothing gets the baseline rather than a gap, so a quiet
    minute reads as quiet and not as a hole in the chart.
    """
    pooled = _live_pool(values, cells)
    peak = max(pooled, default=0)
    levels = rows * 8
    heights = [max(1, round(value * grow / peak * levels)) if value > 0 and peak
               else 0 for value in pooled]
    lines = []
    for row in range(rows - 1, -1, -1):
        line = ""
        for height in heights:
            part = height - row * 8
            line += ("█" if part >= 8 else _LIVE_SPARKS[part] if part > 0
                     else "▁" if row == 0 else " ")
        lines.append(line)
    return lines, {n for n, height in enumerate(heights) if not height}


def _live_tint(text: str, colours: int, x: int = 0,
               dim: set[int] | frozenset = frozenset()) -> list[tuple[int, str, str]]:
    """`text` in the gradient, left to right, with the `dim` columns muted."""
    out: list[tuple[int, str, str]] = []
    count = max(len(text), 1)
    for at, ch in enumerate(text):
        role = "separator" if at in dim else f"g{at * max(colours, 1) // count}"
        if out and out[-1][2] == role:
            out[-1] = (out[-1][0], out[-1][1] + ch, role)
        else:
            out.append((x + at, ch, role))
    return out


def _live_status_mark(item: dict, motion: dict) -> tuple[str, str]:
    """The status glyph: spinning while it runs, blinking while it needs you."""
    status = item["status"]
    mark, _label, role = _LIVE_STATES[status]
    spin = motion.get("spin", 0)
    if status == "working":
        return _LIVE_SPIN[spin % len(_LIVE_SPIN)], role
    if status == "thinking":
        return _LIVE_THINK[spin // 2 % len(_LIVE_THINK)], role
    if status in ("asking", "failing") and spin // 4 % 2:
        return mark, "title"
    return mark, role


def _live_timer(item: dict, motion: dict) -> str:
    if not item["for"]:
        return ""
    seconds = item["for"] + motion.get("since", 0.0)
    if item["status"] in ("working", "thinking", "asking", "failing"):
        return _live_clock(seconds)
    return _live_span(seconds)


def _live_now(item: dict) -> tuple[str, str]:
    """The one line that says what a session is doing, or what it last said."""
    status = item["status"]
    role = _LIVE_STATES[status][2]
    if status == "working":
        return f"▸ {item['doing']}", "active"
    if status == "thinking":
        if item["doing"] == "writing a reply" and item["intent"]:
            return f"◇ {item['intent']} · writing", "turns"
        if item["doing"] == "writing a reply" and item.get("after"):
            return f"writing · after {item['after']}", "turns"
        return item["doing"], "turns"
    if status in ("asking", "failing"):
        return item["doing"], role
    if status == "waiting":
        return (f"↳ {item['said']}" if item["said"] else "waiting for you"), "help"
    return item["said"] or "no activity", "separator"


def _live_lit(row: list[tuple[int, str, str]], cut: int
              ) -> list[tuple[int, str, str]]:
    """`row` under the cursor bar, which has swept `cut` cells across it."""
    out = [(1, " " * (cut - 1), "cursor")] if cut > 1 else []
    for x, text, role in row:
        if x == 0:
            out.append((x, text, role))  # the colour chip stays itself
            continue
        lit = "selected" if role == "title" else "cursor"
        width = ui.cells(text)
        if x + width <= cut:
            out.append((x, text, lit))
        elif x >= cut:
            out.append((x, text, role))
        else:
            head = ui.clip(text, cut - x)
            if head:
                out.append((x, head, lit))
            if text[len(head):]:
                out.append((x + ui.cells(head), text[len(head):], role))
    return out


def _live_list(sessions: list[dict], inner: int, motion: dict,
               gap: int = 1) -> tuple[list[list], list[tuple[int, int]]]:
    """Two rows a session: what and how long; then what now, and where."""
    rows: list[list] = []
    spans = []
    cursor = motion.get("cursor")
    opened = motion.get("open")
    marks = motion.get("marks", {})
    chips = max(motion.get("chips", 8), 1)
    moved = motion.get("moved")
    for index, item in enumerate(sessions):
        if index and gap:
            rows += [[] for _ in range(gap)]
        first = len(rows)
        spans.append((first, first + 2))
        progress = 1.0
        if opened is not None:
            progress = (opened - index * _LIVE_DEAL_SECONDS) / _LIVE_ROW_TYPE_SECONDS
            if progress < 0:
                rows += [[], []]
                continue
        chip = f"c{item['chip'] % chips}"
        mark, mark_role = _live_status_mark(item, motion)
        _mark, label, role = _LIVE_STATES[item["status"]]
        gutter = ("  ", "separator")
        right: list[tuple[str, str]] = []
        if inner >= 50:
            right += [(label.ljust(10), role), gutter]
        right.append((_live_timer(item, motion).rjust(7), "number"))
        if inner >= 76:
            spark = _live_wave(item["bins"], 10)[0][0]
            right += [gutter, (spark, chip)]
        if inner >= 60:
            burn = item["burn_per_minute"]
            right += [gutter, (f"{burn:5.1f}/m", "credits" if burn >= 0.05
                               else "separator")]
        wide = sum(ui.cells(text) for text, _ in right)
        title = ui.trunc(item["title"], max(inner - 4 - wide - 2, 4))
        age = marks.get(item["id"])
        lit = ui.flash_role(age) if age is not None else None
        head = [(0, "▌", chip), (2, mark, mark_role),
                (4, ui.typed(title, progress),
                 lit or ("help" if item["status"] == "idle" else "title"))]
        if progress >= 1:
            head += _live_right(right, inner)
        now, now_role = _live_now(item)
        repo = item["repo"].rsplit("/", 1)[-1]
        where = " · ".join(filter(None, (repo, item["branch"])))
        tail = [(0, "▌", chip)]
        room = inner - 4
        if inner >= 64 and where:
            where = ui.trunc(where, min(32, inner // 3))
            room -= ui.cells(where) + 2
            if progress >= 1:
                tail += _live_right([(where, "repo")], inner)
        tail += _live_line([(ui.trunc(ui.typed(now, progress), room), now_role)],
                           room, 4)
        if index == cursor:
            sweep = 1.0 if moved is None else min(moved / ui.SWEEP_SECONDS, 1.0)
            cut = round(inner * (1 - (1 - sweep) ** 2))
            head, tail = _live_lit(head, cut), _live_lit(tail, cut)
        rows += [head, tail]
    return rows, spans


def _live_detail_rows(item: dict, inner: int, motion: dict
                      ) -> list[tuple[int, list]]:
    """The chosen session, as (priority, row): short panels drop the highest."""
    out: list[tuple[int, list]] = []
    pad = 7
    chips = max(motion.get("chips", 8), 1)
    chip = f"c{item['chip'] % chips}"
    since = motion.get("since", 0.0)

    def labelled(label: str, parts: list[tuple[str, str]], priority: int) -> None:
        out.append((priority, [(0, label, "label"),
                               *_live_line(parts, inner - pad, pad)]))

    def split(left: list[tuple[str, str]], right: str, priority: int) -> None:
        room = inner - ui.cells(right) - 2
        row = _live_line(left, room if room > 20 else inner)
        if room > 20 and right:
            row += _live_right([(right, "help")], inner)
        out.append((priority, row))

    where = [(item["repo"] or "no repository", "repo")]
    if item["branch"]:
        where += [(" · ", "separator"), (item["branch"], "repo")]
    split(where, f"up {_live_span(item['up'] + since)}" if item["up"] else "", 0)
    model = " ".join(filter(None, (item["model"], item["effort"]))) or "model not seen yet"
    split([(model, "turns"), (" · ", "separator"), (f"pid {item['pid']}", "number")],
          f"{item['turns']} turns · {item['calls']} calls", 1)
    out.append((6, []))
    you = textwrap.wrap(item["asked"] or "—", max(inner - pad, 8),
                        max_lines=2, placeholder=" …")
    said = textwrap.wrap(item["said"] or "—", max(inner - pad, 8),
                         max_lines=4, placeholder=" …")
    budget = round(sum(map(len, you + said)) * motion.get("typed", 1.0))
    for n, line in enumerate(you):
        labelled("" if n else "YOU", [(line[:max(budget, 0)], "summary")], 2 + 5 * n)
        budget -= len(line)
    for n, line in enumerate(said):
        labelled("" if n else "AGENT", [(line[:max(budget, 0)], "help")],
                 (2, 4, 8, 9)[n])
        budget -= len(line)
    out.append((6, []))
    done, total, current = item["todos"]
    if total:
        plan = [(_live_bar(done, total, max(min(12, inner // 5), 4)),
                 "turns" if done == total else "active"), (f" {done}/{total}", "number")]
        if current:
            plan += [(" · ", "separator"), (current, "summary")]
        labelled("PLAN", plan, 3)
    elif item["intent"]:
        labelled("INTENT", [(item["intent"], "summary")], 3)
    spend = [(f"{ui.fmt_aiu(item['nano'])} AIU", "credits"), (" · ", "separator"),
             (f"{item['burn_per_minute']:.2f} AIU/min", "credits")]
    cells = inner - pad - sum(ui.cells(text) for text, _ in spend) - 3
    if cells >= 8:
        spend += [("   ", "separator"), (_live_wave(item["bins"], min(cells, 30))[0][0], chip)]
    labelled("SPEND", spend, 3)
    if item["tools"]:
        peak = max(n for _name, n in item["tools"])
        tools: list[tuple[str, str]] = []
        for name, n in item["tools"]:
            tools += [(f"{name} ", "summary"), ("▆" * max(1, round(n / peak * 8)), chip),
                      (f" {n}   ", "number")]
        labelled("TOOLS", tools, 5)
    flags = [(text, role) for text, role in (
        (f"✗ {item['failures']} failed" if item["failures"] else "", "danger"),
        (f"» {item['agents']} sub-agents running" if item["agents"] else "", "turns"),
        (f"◐ {item['compactions']} compacted" if item["compactions"] else "", "warn"))
        if text]
    if flags:
        parts: list[tuple[str, str]] = []
        for text, role in flags:
            parts += [("   ", "separator"), (text, role)] if parts else [(text, role)]
        labelled("FLAGS", parts, 7)
    return out


def _live_keep(rows: list[tuple[int, list]], room: int) -> list[list]:
    """The rows that fit in `room`, dropping the least important first."""
    rows = list(rows)
    while len(rows) > max(room, 0):
        del rows[max(range(len(rows)), key=lambda n: (rows[n][0], n))]
    return [row for _priority, row in rows]


def _live_feed(feed: list[dict], inner: int, count: int, motion: dict) -> list[list]:
    """The newest events across every session, a line each; news lights up."""
    if not feed:
        return [[(0, ui.clip("Nothing yet — events appear here as they happen.",
                             inner), "help")]]
    arrivals = motion.get("arrivals", {})
    chips = max(motion.get("chips", 8), 1)
    out = []
    for entry in feed[:max(count, 0)]:
        age = arrivals.get(entry["key"])
        lit = ui.flash_role(age) if age is not None else None
        progress = min(age / 0.3, 1.0) if age is not None else 1.0
        chip = f"c{entry['chip'] % chips}"
        row = []
        x = 0
        if inner >= 54:
            row.append((0, time.strftime("%H:%M:%S", time.localtime(entry["at"])),
                        lit or "help"))
            x = 10
        row.append((x, "▌", chip))
        x += 2
        if inner >= 44:
            name = 24 if inner >= 110 else 14
            row.append((x, ui.trunc(entry["title"], name), lit or chip))
            x += name + 2
        row += [(x, entry["mark"], entry["role"]),
                (x + 2, ui.trunc(entry["what"], 11), lit or entry["role"])]
        x += 14
        detail = ui.typed(entry["detail"], progress)
        row += _live_line([(ui.trunc(detail, inner - x), lit or "summary")],
                          inner - x, x)
        out.append([seg for seg in row if seg[0] + ui.cells(seg[1]) <= inner])
    return out


def _live_panel(title: list[tuple[str, str]], note: list[tuple[str, str]],
                body: list[list], width: int, height: int | None = None,
                edge: str = "separator") -> list[list]:
    """`body` in a rounded box `width` cells wide, its title in the border."""
    height = len(body) + 2 if height is None else height
    tail = [(" ", edge), *note, (" ─╮", edge)] if note else [("─╮", edge)]
    tail_w = sum(ui.cells(text) for text, _ in tail)
    if tail_w + 12 > width:
        tail, tail_w = [("─╮", edge)], 2
    top = [(0, "╭─ ", edge), *_live_line(title, max(width - 5 - tail_w, 0), 3)]
    x = max(3 + sum(ui.cells(text) for _x, text, _r in top[1:]), 3)
    fill = width - tail_w - x
    if fill > 0:
        top.append((x, " " + "─" * (fill - 1), edge))
    top += _live_line(tail, tail_w, width - tail_w)
    rows = [top]
    for line in body[:max(height - 2, 0)]:
        rows.append([(0, "│", edge), *[(2 + x, text, role) for x, text, role in line],
                     (width - 1, "│", edge)])
    while len(rows) < height - 1:
        rows.append([(0, "│", edge), (width - 1, "│", edge)])
    rows.append([(0, "╰" + "─" * (width - 2) + "╯", edge)])
    return rows


def _live_tiles_of(snap: dict) -> list[tuple[str, str, str, str]]:
    """(key, label, value, role) for each headline number, most urgent first."""
    counts = snap["counts"]
    tiles = [("live", "● LIVE", str(len(snap["sessions"])), "title"),
             ("need", "◆ NEED YOU",
              str(counts.get("asking", 0) + counts.get("waiting", 0)),
              "warn" if counts.get("asking") else "credits"),
             ("busy", "◐ WORKING",
              str(counts.get("working", 0) + counts.get("thinking", 0)), "active")]
    if counts.get("failing"):
        tiles.append(("failing", "■ FAILING", str(counts["failing"]), "danger"))
    tiles.append(("burn", "BURN/MIN", f"{snap['burn_per_minute']:.1f}", "credits"))
    spend = snap["today_nano_aiu"] / 1e9
    if snap["budget"]:
        share = spend / snap["budget"]
        role = "danger" if share >= 1 else "warn" if share >= 0.8 else "turns"
        tiles.append(("today", f"TODAY · {share:.0%} OF {snap['budget']:g}",
                      _live_short(spend), role))
    else:
        tiles.append(("today", "TODAY AIU", _live_short(spend), "credits"))
    tiles.append(("calls", "TOOL CALLS", str(snap["calls"]), "summary"))
    if snap["agents"]:
        tiles.append(("agents", "SUB-AGENTS", str(snap["agents"]), "turns"))
    return tiles


def _live_tiles(snap: dict, usable: int, motion: dict) -> list[list]:
    """The headline numbers: each with its trend and, as it changes, by how much."""
    tiles = _live_tiles_of(snap)
    per = max(1, min(len(tiles), usable // 16))
    progress = motion.get("count", 1.0)
    flashes = motion.get("tile_flash", {})
    trend = dict(snap.get("trend", {}))
    trend["burn"] = snap["bins"][-24:]
    out: list[list] = []
    for start in range(0, len(tiles), per):
        group = tiles[start:start + per]
        each = usable // per
        label_row: list = []
        value_row: list = []
        for n, (key, label, value, role) in enumerate(group):
            x = n * each
            room = each - 3
            if n:
                label_row.append((x, "│", "separator"))
                value_row.append((x, "│", "separator"))
            label_row += _live_line([(label, "help")], room, x + 2)
            shown = ui.count_up(value, progress).strip()
            age = flashes.get(key)
            lit = ui.flash_role(age) if age is not None else None
            parts = [(shown, lit or role)]
            series = trend.get(key) or []
            spare = room - ui.cells(shown) - 2
            # Burn's trend is per-minute bins, not the rate on the tile.
            if (lit and key != "burn" and len(series) >= 2
                    and series[-1] != series[-2]):
                delta = series[-1] - series[-2]
                step = f"{abs(delta):.0f}" if key != "today" else _live_short(abs(delta))
                arrow = f" {'▲' if delta > 0 else '▼'}{step}"
                if ui.cells(arrow) + 2 <= spare:
                    parts.append((arrow, "warn" if delta > 0 else "active"))
                    spare -= ui.cells(arrow)
            if spare >= 4 and len(series) >= 2:
                cells = min(spare, 12, len(series))
                # Measured from the window's low, so a steady count draws a
                # flat baseline and any change stands out.
                low = min(series[-cells:])
                rise = [value - low for value in series[-cells:]]
                spark = (_live_wave(rise, cells, 1, motion.get("grow", 1.0))[0][0]
                         if any(rise) else "▁" * cells)
                parts += [("  ", "separator"), (spark, role)]
            value_row += _live_line(parts, room, x + 2)
        out += [label_row, value_row]
    return out


def _live_header(snap: dict, usable: int, motion: dict) -> list:
    count = len(snap["sessions"])
    row = _live_line([(f" {ui.menu_icon('live')} Live sessions", "title")], usable)
    used = sum(ui.cells(text) for _x, text, _r in row)
    live = [("●" if count else "○", motion.get("pulse", "active") if count
             else "separator"), (f" {count} live", "active" if count else "help")]
    for parts in (live + [("   " + (motion.get("clock") or snap["refreshed"]), "help")],
                  live):
        if used + sum(ui.cells(text) for text, _ in parts) + 2 <= usable:
            return row + _live_right(parts, usable)
    return row


def _live_wave_rows(snap: dict, usable: int, rows: int, motion: dict) -> list[list]:
    """Burn across every live session over thirty minutes, in the gradient."""
    cells = max(usable - 10, 4)
    lines, quiet = _live_wave(snap["bins"], cells, rows, motion.get("grow", 1.0))
    out = []
    for n, line in enumerate(lines):
        last = n == len(lines) - 1
        row = _live_tint(line, motion.get("grad", 1), 5, quiet if last else frozenset())
        if last:
            row = [(1, "30m", "help"), *row, (6 + cells, "now", "help")]
        out.append(row)
    return out


def _live_open_seconds(count: int) -> float:
    return max(count * _LIVE_DEAL_SECONDS + _LIVE_ROW_TYPE_SECONDS,
               0.1 + ui.COUNT_SECONDS, 0.8)


def _live_screen(snap: dict, width: int, height: int | None = None,
                 motion: dict | None = None
                 ) -> tuple[list[list], list[tuple[int, int, int, int]]]:
    """The whole page as rows, and where each session's rows landed.

    `height` None is the printed form: every panel at its natural size. The
    hits are (y, left, right, session index) for the mouse.
    """
    motion = motion or {}
    usable = width - 1
    sessions = snap["sessions"]
    tall = height is not None and width >= 100 and height >= 34
    rows: list[list] = [_live_header(snap, usable, motion)]
    if tall:
        rows.append([])
    rows += _live_tiles(snap, usable, motion)
    rows.append([])
    rows += _live_wave_rows(snap, usable, 2 if tall else 1, motion)
    rows.append([])
    top = len(rows)
    room = None if height is None else max(height - 1 - top, 0)
    cursor = motion.get("cursor")
    chosen = (sessions[min(max(cursor or 0, 0), len(sessions) - 1)]
              if sessions else None)
    two = room is not None and width >= 120 and bool(sessions) and room >= 14
    left = usable * 3 // 5 if two else usable
    side = usable - left - 1 if two else usable
    feed = snap.get("feed", [])

    if sessions:
        spaced = _live_list(sessions, left - 4, motion, gap=1)
        tight = _live_list(sessions, left - 4, motion, gap=0)
    else:
        empty = [[], [(1, ui.clip("No Copilot CLI is running right now.",
                                  left - 6), "summary")],
                 [(1, ui.clip("Start one with `copilot` and it appears here "
                              f"within {LIVE_REFRESH_SECONDS}s.", left - 6), "help")], []]
        spaced = tight = (empty, [])
    detail = _live_detail_rows(chosen, side - 4, motion) if chosen else []

    if room is None:
        (body, spans), list_h = spaced, len(spaced[0]) + 2
        detail_h = len(detail) + 2 if detail else 0
        feed_h = min(len(feed), 12) + 2
    elif two:
        # Sessions and the chosen one side by side at one height, and the
        # feed across the full width below, where its lines have room.
        body, spans = spaced if len(spaced[0]) + 2 <= room - 7 else tight
        list_h = detail_h = max(min(max(len(body), len(detail)) + 2, room - 7), 10)
        if list_h + 5 > room:
            list_h = detail_h = room
        feed_h = room - list_h
    else:
        for choice in (spaced, tight):
            body, spans = choice
            list_h = len(body) + 2
            if list_h + min(len(detail) + 2, 10) + 5 <= room:
                break
        list_h = min(list_h, room)
        rest = room - list_h
        detail_h = min(len(detail) + 2, rest) if detail and rest >= 7 else 0
        feed_h = rest - detail_h
        if feed_h < 5:
            detail_h, feed_h = (detail_h + feed_h if detail_h else 0), 0

    view = max(list_h - 2, 0)
    scroll = 0
    if spans and cursor is not None:
        first, last = spans[min(max(cursor, 0), len(spans) - 1)]
        scroll = max(0, min(first, last - view))
    note = f"{len(sessions)} running"
    if scroll:
        note = "↑ " + note
    if scroll + view < len(body):
        note += " ↓"
    panels = [(_live_panel([("Sessions", "header")], [(note, "help")] if sessions else [],
                           body[scroll:scroll + view], left, list_h), top, 0)]
    hits = [(top + 1 + line - scroll, 0, left, index)
            for index, (first, last) in enumerate(spans)
            for line in range(first, last) if scroll <= line < scroll + view]
    side_x, side_y = (left + 1, top) if two else (0, top + list_h)
    if detail_h:
        mark, mark_role = _live_status_mark(chosen, motion)
        label, role = _LIVE_STATES[chosen["status"]][1:]
        panels.append((_live_panel(
            [(mark, mark_role), (" " + chosen["title"], "title")], [(label, role)],
            _live_keep(detail, detail_h - 2), side, detail_h,
            f"c{chosen['chip'] % max(motion.get('chips', 8), 1)}"), side_y, side_x))
    if feed_h:
        feed_x, feed_y, feed_w = ((0, top + list_h, usable) if two
                                  else (side_x, side_y + detail_h, side))
        panels.append((_live_panel(
            [("Activity", "header")],
            [("●", motion.get("pulse", "active")), (" live", "help")] if sessions else [],
            _live_feed(feed, feed_w - 4, feed_h - 2, motion), feed_w, feed_h),
            feed_y, feed_x))
    for panel, y, x in panels:
        while len(rows) < y + len(panel):
            rows.append([])
        for dy, line in enumerate(panel):
            rows[y + dy] += [(x + px, text, role) for px, text, role in line]
    return rows, hits


def _live_ages(state: dict, clock: float) -> tuple[dict, dict]:
    """Seconds since each status change and each feed arrival, once expired
    ones are dropped — what the flashes are drawn from."""
    ages = []
    for name in ("marks", "arrivals"):
        table = state.setdefault(name, {})
        for key in [key for key, at in table.items() if clock - at >= ui.FLASH_SECONDS]:
            del table[key]
        ages.append({key: clock - at for key, at in table.items()})
    return ages[0], ages[1]


def _live_home_rows(snap: dict, width: int, room: int, motion: dict) -> list[list]:
    """The home screen's live roster, at most `room` rows: a summary line,
    a row for each running session, and the newest event as it lands."""
    sessions = snap["sessions"]
    if not sessions or room < 1 or width < 20:
        return []
    usable = width - 1
    counts = snap["counts"]
    need = counts.get("asking", 0) + counts.get("waiting", 0)
    busy = counts.get("working", 0) + counts.get("thinking", 0)
    ticker = room >= 3 and bool(snap.get("feed"))
    slots = min(len(sessions), room - 1 - ticker)
    hidden = len(sessions) - slots
    parts = [("●", motion.get("pulse") or "active"), (f" {len(sessions)} live", "active")]
    for count, text, role in ((need, "need you", "warn"), (busy, "working", "turns"),
                              (counts.get("failing", 0), "failing", "danger")):
        if count:
            parts += [("  ·  ", "separator"), (f"{count} {text}", role)]
    parts += [("  ·  ", "separator"),
              (f"{snap['burn_per_minute']:.1f} AIU/min", "credits")]
    tail = "↵ Live sessions" if usable >= 90 else "↵"
    right = [(f"+{hidden} more · {tail}", "help")] if hidden else []
    right_w = sum(ui.cells(text) for text, _ in right)
    budget = usable - 2 - (right_w + 2 if right else 0)
    # Whole readings fall off the end rather than one being cut mid-word.
    while len(parts) > 2 and sum(ui.cells(text) for text, _ in parts) > budget:
        parts = parts[:-2]
    summary = _live_line(parts, budget, 2)
    wave = _live_wave(snap["bins"][-20:], 20, 1, motion.get("grow", 1.0))[0][0]
    used = 2 + sum(ui.cells(text) for _x, text, _r in summary)
    if used + 24 + (right_w + 2 if right else 0) <= usable:
        summary += _live_tint(wave, motion.get("grad", 1), used + 2)
    if right and used + right_w + 2 <= usable:
        summary += _live_right(right, usable)
    rows = [summary]
    chips = max(motion.get("chips", 8), 1)
    marks = motion.get("marks", {})
    opened = motion.get("open")
    title_w = max(min(30, usable // 4), 10)
    for index, item in enumerate(sessions[:slots]):
        progress = 1.0
        if opened is not None:
            progress = (opened - index * _LIVE_DEAL_SECONDS) / _LIVE_ROW_TYPE_SECONDS
            if progress <= 0:
                rows.append([])
                continue
        mark, mark_role = _live_status_mark(item, motion)
        _mark, label, role = _LIVE_STATES[item["status"]]
        age = marks.get(item["id"])
        lit = ui.flash_role(age) if age is not None else None
        row = [(2, "▌", f"c{item['chip'] % chips}"), (4, mark, mark_role)]
        title = ui.trunc(item["title"], title_w if usable >= 60 else usable - 18)
        row.append((6, ui.typed(title, progress).ljust(title_w)
                    if usable >= 60 else ui.typed(title, progress),
                    lit or ("help" if item["status"] == "idle" else "title")))
        x = 6 + (title_w if usable >= 60 else ui.cells(title)) + 2
        if usable >= 60 and progress >= 1:
            row += _live_line([(label.ljust(10), role),
                               (_live_timer(item, motion).rjust(6), "number")],
                              usable - x, x)
            x += 18
            now, now_role = _live_now(item)
            if usable - x >= 12:
                row += _live_line([(ui.trunc(now, usable - x), now_role)],
                                  usable - x, x)
        elif progress >= 1 and usable - x >= 6:
            row.append((usable - 6, _live_timer(item, motion).rjust(6), "number"))
        rows.append(row)
    if ticker:
        line = _live_feed(snap["feed"][:1], usable - 4, 1, motion)[0]
        rows.append([(2, "↯", motion.get("pulse") or "active"),
                     *[(x + 4, text, role) for x, text, role in line]])
    return rows


def _live_hint(width: int) -> str:
    for hint in (" ↑↓ choose · ↵ open session · r refresh · q back ",
                 " ↑↓ choose · ↵ open · r refresh · q back ",
                 " ↑↓ · ↵ open · q back ", " ↵ · q "):
        if ui.cells(hint) <= width - 1:
            return hint
    return ""


def _live_theme(curses) -> tuple[dict, int, int]:
    """The view's styles, plus a gradient (g0…) and a colour per session (c0…)."""
    theme = dict(ui.tui_theme(curses))
    sweep = ui.banner_palette(curses)
    for n, attr in enumerate(sweep or [theme["credits"]]):
        theme[f"g{n}"] = attr
    if len(sweep) >= 2:
        chips = [sweep[round(k * (len(sweep) - 1) / 7)] for k in (0, 4, 2, 6, 1, 5, 3, 7)]
    else:
        chips = [theme[role] for role in ("active", "turns", "credits", "repo",
                                          "warn", "title", "header", "label")]
    for n, attr in enumerate(chips):
        theme[f"c{n}"] = attr
    return theme, max(len(sweep), 1), len(chips)


def _live_tui(screen, state: dict):
    """The page. Returns the id of a session to open, or None to go back."""
    import curses

    screen.keypad(True)
    theme, grad, chips = _live_theme(curses)
    try:
        curses.curs_set(0)
        screen.bkgd(" ", theme["background"])
    except curses.error:
        pass
    mouse = _enable_mouse(curses)
    last_click = [0.0, -1]
    pending: list[int] = []
    opened = time.monotonic() if not state.get("dealt") else None
    read_at: float | None = None
    shown = typed_key = None
    moved_at = typed_at = None
    values = state.setdefault("tile_values", {})
    lit: dict[str, float] = {}
    try:
        while True:
            clock = time.monotonic()
            if read_at is None or clock - read_at >= LIVE_REFRESH_SECONDS:
                _live_read(state)
                read_at = time.monotonic()
                for key, _label, value, _role in _live_tiles_of(state["snapshot"]):
                    if key in values and values[key] != value:
                        lit[key] = read_at
                    values[key] = value
            snap = state["snapshot"]
            sessions = snap["sessions"]
            cursor = state["cursor"] = min(max(state.get("cursor", 0), 0),
                                           max(len(sessions) - 1, 0))
            height, width = screen.getmaxyx()
            if sessions:
                chosen = sessions[cursor]
                if shown != chosen["id"]:
                    moved_at = clock if shown is not None else None
                    shown = chosen["id"]
                key = (chosen["id"], chosen["asked"], chosen["said"])
                if key != typed_key:
                    typed_key, typed_at = key, clock
            marks, arrivals = _live_ages(state, clock)
            for key in [key for key, at in lit.items() if clock - at >= ui.FLASH_SECONDS]:
                del lit[key]
            elapsed = None if opened is None else clock - opened
            if elapsed is not None and elapsed >= _live_open_seconds(len(sessions)):
                opened = elapsed = None
                state["dealt"] = True
            typed = (1.0 if typed_at is None
                     else min((clock - typed_at) / _LIVE_TYPE_SECONDS, 1.0))
            moved = None if moved_at is None else clock - moved_at
            motion = {
                "cursor": cursor, "since": clock - read_at, "open": elapsed,
                "count": ui.launch_progress(elapsed, 0.1, ui.COUNT_SECONDS),
                "grow": ui.launch_progress(elapsed, 0.0, 0.8),
                "marks": marks, "arrivals": arrivals,
                "tile_flash": {key: clock - at for key, at in lit.items()},
                "moved": moved, "typed": typed,
                "spin": int(clock * 1000 / _LIVE_SPIN_MS),
                "pulse": ui.pulse_role(clock),
                "clock": time.strftime("%H:%M:%S"),
                "grad": grad, "chips": chips,
            }
            rows, hits = _live_screen(snap, width, height, motion)
            screen.erase()
            for y, line in enumerate(rows[:height - 1]):
                for x, text, role in line:
                    _addstr(screen, y, x, text, width, theme[role])
            hint = _live_hint(width)
            _addstr(screen, height - 1, 0, hint, width, theme["status"])
            stamp = f"updated {snap['refreshed']} · every {LIVE_REFRESH_SECONDS}s "
            if ui.cells(hint) + ui.cells(stamp) + 2 <= width - 1:
                _addstr(screen, height - 1, width - 1 - ui.cells(stamp), stamp,
                        width, theme["help"])
            screen.refresh()
            moving = (opened is not None or marks or arrivals or lit or typed < 1
                      or (moved is not None and moved < ui.SWEEP_SECONDS))
            busy = any(item["status"] in ("working", "thinking", "asking", "failing")
                       for item in sessions)
            screen.timeout(ui.MOTION_MS if moving else _LIVE_SPIN_MS if busy else 1000)
            try:
                key = pending.pop(0) if pending else screen.getch()
            except KeyboardInterrupt:
                return None
            if key == -1:
                continue
            if opened is not None:
                opened = None
                state["dealt"] = True
            event = _mouse_event(screen, curses, key, last_click, pending)
            if event:
                kind, x, y = event
                if kind in ("click", "double"):
                    for hit_y, hit_left, hit_right, index in hits:
                        if hit_y == y and hit_left <= x < hit_right:
                            state["cursor"] = index
                            if kind == "double":
                                return sessions[index]["id"]
                elif kind == "wheel-up":
                    state["cursor"] = cursor - 1
                elif kind == "wheel-down":
                    state["cursor"] = cursor + 1
                continue
            if key in (27, ord("q"), ord("Q")):
                return None
            if key in (10, 13, curses.KEY_ENTER) and sessions:
                return sessions[cursor]["id"]
            if key in (ord("r"), ord("R")):
                read_at = None
            elif key in (curses.KEY_UP, ord("k")):
                state["cursor"] = cursor - 1
            elif key in (curses.KEY_DOWN, ord("j")):
                state["cursor"] = cursor + 1
            elif key in (curses.KEY_HOME, ord("g")):
                state["cursor"] = 0
            elif key in (curses.KEY_END, ord("G")):
                state["cursor"] = len(sessions) - 1
    finally:
        if mouse:
            _disable_mouse()


def _live_text(width: int) -> str:
    """The page as plain text, for a pipe or a terminal with no curses."""
    state: dict = {}
    _live_read(state)
    rows, _hits = _live_screen(state["snapshot"], width)
    out = []
    for line in rows:
        text = ""
        for x, part, _role in sorted(line):
            text += " " * max(x - ui.cells(text), 0) + part
        out.append(text.rstrip())
    return "\n".join(out)


def _live_data() -> dict:
    """The page's figures, for `cs live --json`."""
    state: dict = {}
    _live_read(state)
    snap = state["snapshot"]
    return {"sessions": snap["sessions"],
            "counts": dict(snap["counts"]),
            **{key: snap[key] for key in (
                "burn_per_minute", "today_nano_aiu", "budget", "calls",
                "failures", "repos", "agents", "longest")},
            "activity": [{key: entry[key] for key in (
                "at", "id", "title", "what", "detail")} for entry in snap["feed"]]}


def cmd_live() -> bool:
    """Every Copilot CLI running now, on one page that keeps itself current."""
    import curses
    import shutil

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(_live_text(min(shutil.get_terminal_size().columns, 140)))
        return False
    state: dict = {}
    while True:
        try:
            chosen = _curses_wrapper(_live_tui, state)
        except KeyboardInterrupt:
            return True
        except curses.error:
            print(_live_text(min(shutil.get_terminal_size().columns, 140)))
            return False
        if chosen is None:
            return True
        cmd_show(chosen)
