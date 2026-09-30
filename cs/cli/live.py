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
import time
from collections import Counter
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
# Burn is spend over the last ten minutes; the sparkline shows thirty.
_LIVE_BURN_MINUTES = 10
_LIVE_SPARK_MINUTES = 30
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
# Cards deal onto the page one after another when it opens.
_LIVE_DEAL_SECONDS = 0.06

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
    b"session.resume", b"session.compaction_complete",
    b"session.usage_checkpoint", b"permission.requested",
    b"permission.completed", b"session.error", b"abort", b"session.shutdown",
})

# What a tool call is about, by the argument that says so best.
_LIVE_DETAIL_KEYS = ("description", "intent", "path", "pattern", "query",
                     "skill", "url", "name")


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
        self.calls = self.failures = self.streak = self.compactions = 0
        self.nano = 0
        self.tools: Counter = Counter()

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
            elif name[2] >= 0:
                self.streak = 0
            return
        if kind == "subagent.started" and isinstance(agent, str):
            label = data.get("agentDisplayName") or data.get("agentName") or "agent"
            self.agents[agent] = (str(label), at)
            return
        if kind in ("subagent.completed", "subagent.failed"):
            self.agents.pop(agent, None)
            self.agent_tool.pop(agent, None)
            return
        if agent:
            return  # a sub-agent's own turns say nothing about the session
        if kind == "permission.requested":
            self.permissions[str(data.get("requestId"))] = at
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
            self.kind = kind
        elif kind == "session.compaction_complete":
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
        elif kind == "assistant.message":
            if isinstance(data.get("content"), str) and data["content"].strip():
                self.said = data["content"]
            self.kind = kind
        elif kind == "session.error":
            self.error = str(data.get("message") or "error")
            self.kind = kind
        elif kind == "abort":
            self.open = {k: v for k, v in self.open.items() if v[2] < 0}
            self.permissions.clear()
            self.kind = kind
        elif kind in ("assistant.turn_start", "assistant.turn_end",
                      "session.shutdown"):
            self.kind = kind


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
    quiet = now - tail.at if tail.at else _LIVE_SETTLE_SECONDS
    if tail.kind in ("assistant.turn_end", "abort", "session.shutdown",
                     "session.start", "session.resume", "") \
            and quiet >= _LIVE_SETTLE_SECONDS or quiet >= _LIVE_IDLE_SECONDS:
        if quiet >= _LIVE_IDLE_SECONDS:
            return "idle", "no activity", tail.at
        what = "interrupted · your turn" if tail.kind == "abort" else "your turn"
        return "waiting", what, tail.at
    return "thinking", "model is working on a reply", tail.at


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
    bins = [0] * _LIVE_SPARK_MINUTES
    burn = spent = 0
    for when, nano in stamps:
        spent += nano
        at = _live_when(when.replace(" ", "T"))
        if at >= burn_since:
            burn += nano
        minute = int((now - at) // 60)
        if 0 <= minute < _LIVE_SPARK_MINUTES:
            bins[_LIVE_SPARK_MINUTES - 1 - minute] += nano
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
        "intent": _clean(tail.intent),
        "asked": _clean(tail.asked), "said": _clean(tail.said),
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
    limit = ui.daily_budget_aiu()
    state["snapshot"] = {
        "sessions": sessions,
        "counts": Counter(item["status"] for item in sessions),
        "burn_per_minute": sum(item["burn_per_minute"] for item in sessions),
        "bins": [sum(column) for column in
                 zip(*(item["bins"] for item in sessions), strict=True)] if sessions else [],
        "today_nano_aiu": today, "budget": limit,
        "calls": sum(item["calls"] for item in sessions),
        "failures": sum(item["failures"] for item in sessions),
        "repos": len({item["repo"] for item in sessions if item["repo"]}),
        "agents": sum(item["agents"] for item in sessions),
        "longest": max((item["up"] for item in sessions), default=0),
        "refreshed": time.strftime("%H:%M:%S"),
    }


# ── Drawing ──────────────────────────────────────────────────────────
# A page is rows of (x, text, role) segments, so the screen, the printed
# form and the width tests all read the same thing.

def _live_flow(parts: list[tuple[str, str]], width: int,
               indent: int = 1) -> list[list[tuple[int, str, str]]]:
    """Segments packed onto as many rows as `width` needs, never split."""
    rows: list[list[tuple[int, str, str]]] = []
    row: list[tuple[int, str, str]] = []
    x = indent
    room = width - 1
    for text, role in parts:
        if not text:
            continue
        if row and x + 3 + ui.cells(text) > room:
            rows.append(row)
            row, x = [], indent
        if row:
            row.append((x, " · ", "separator"))
            x += 3
        text = ui.clip(text, max(room - x, 0))
        row.append((x, text, role))
        x += ui.cells(text)
    if row:
        rows.append(row)
    return rows


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


def _live_bar(done: int, total: int, cells: int) -> str:
    filled = round(cells * done / total) if total else 0
    return "█" * filled + "░" * (cells - filled)


def _live_card(item: dict, width: int, now: float, *, chosen: bool,
               flash: str | None, pulse: str) -> list[list[tuple[int, str, str]]]:
    """One session as a box `width` cells wide and eight rows tall."""
    mark, label, role = _LIVE_STATES[item["status"]]
    edge = "active" if chosen else role
    tl, tr, bl, br, h, v = (("┏", "┓", "┗", "┛", "━", "┃") if chosen
                            else ("╭", "╮", "╰", "╯", "─", "│"))
    inner = max(width - 4, 1)
    dot = pulse if item["status"] in ("working", "thinking") else role
    right = f" {_live_span(item['for'])} " if item["for"] else " "
    head = [(f"{tl}{h} ", edge), (mark, dot), (f" {label} ", role), (f"{h} ", edge)]
    room = width - sum(ui.cells(text) for text, _ in head) - ui.cells(right) - 2
    title = ui.trunc(item["title"], max(room - 1, 1))
    fill = max(room - 1 - ui.cells(title), 0)
    top = _live_line([*head, (title, flash or "title"), (" " + h * fill, edge),
                      (right, "number"), (h + tr, edge)], width)

    def boxed(parts: list[tuple[str, str]]) -> list[tuple[int, str, str]]:
        body = _live_line(parts, inner, 2)
        used = sum(ui.cells(text) for _x, text, _r in body)
        return [(0, v, edge), *body, (2 + used, " " * (inner - used), "summary"),
                (width - 1, v, edge)]

    sep = ("  ·  " if inner >= 60 else " · ", "separator")
    meta = [(item["repo"] or "no repository", "repo")]
    for text, part in ((item["branch"], "repo"),
                       (" ".join(filter(None, (item["model"], item["effort"]))), "turns"),
                       (f"pid {item['pid']}", "number"),
                       (f"up {_live_span(item['up'])}" if item["up"] else "", "number")):
        if text:
            meta += [sep, (text, part)]
    doing = [("now   ", "label"), (item["doing"], role if item["status"] != "working"
                                    else "active")]
    if item["quiet"] is not None and item["status"] == "working" and item["quiet"] > 60:
        doing += [sep, (f"quiet {_live_span(item['quiet'])}", "warn")]
    you = [("you   ", "label"), (item["asked"] or "—", "summary")]
    said = [("agent ", "label"), (item["said"] or "—", "help")]
    done, total, current = item["todos"]
    if total:
        cells = max(min(inner // 5, 12), 4)
        plan = [("plan  ", "label"), (_live_bar(done, total, cells),
                                      "turns" if done == total else "active"),
                (f" {done}/{total}", "number")]
        if current:
            plan += [sep, (current, "summary")]
    elif item["intent"]:
        plan = [("plan  ", "label"), (item["intent"], "summary")]
    else:
        plan = [("plan  ", "label"), ("no todo list", "separator")]
    spark = ui.sparkline(item["bins"][-max(min(inner // 4, _LIVE_SPARK_MINUTES), 6):])
    stats = [(spark if spark.strip() else "", "credits")]
    for text, part in (
            (f"{item['burn_per_minute']:,.2f} AIU/min", "credits"),
            (f"{ui.fmt_aiu(item['nano'])} AIU" if item["nano"] else "", "credits"),
            (f"{item['turns']} turns" if item["turns"] else "", "turns"),
            (f"{item['calls']} calls", "number"),
            (f"{item['failures']} failed" if item["failures"] else "", "danger"),
            (f"{item['agents']} agents" if item["agents"] else "", "active"),
            (f"{item['compactions']} compacted" if item["compactions"] else "", "warn"),
            (" ".join(f"{name} {n}" for name, n in item["tools"]), "help")):
        if text:
            stats += [sep, (text, part)] if stats[-1][0] else [(text, part)]
    if not stats[0][0]:
        stats = stats[1:]
    bottom = [(0, bl + h * (width - 2) + br, edge)]
    return [top, boxed(meta), boxed(doing), boxed(you), boxed(said),
            boxed(plan), boxed(stats), bottom]


_LIVE_CARD_ROWS = 8


def _live_columns(width: int) -> int:
    return 2 if width >= 120 else 1


def _live_band(snap: dict, width: int, grow: float = 1.0
               ) -> list[list[tuple[int, str, str]]]:
    """The overview across the top: counts by status, burn, spend, calls."""
    sessions = snap["sessions"]
    title = f"{ui.menu_icon('live')} Live sessions"
    stamp = f"updated {snap['refreshed']} · every {LIVE_REFRESH_SECONDS}s"
    rows = [_live_line([(" " + title, "title")], width - 1)]
    if ui.cells(title) + ui.cells(stamp) + 4 <= width - 1:
        rows[0].append((width - 1 - ui.cells(stamp), stamp, "help"))
    counts = [(f"● {len(sessions)} live", "active")]
    for status in _LIVE_ORDER:
        if snap["counts"].get(status):
            mark, label, role = _LIVE_STATES[status]
            counts.append((f"{mark} {snap['counts'][status]} {label}", role))
    rows += _live_flow(counts, width)
    spend = snap["today_nano_aiu"] / 1e9
    figures = [(f"burn {snap['burn_per_minute']:,.2f} AIU/min", "credits"),
               (ui.sparkline(snap["bins"][-24:], grow).strip() and
                ui.sparkline(snap["bins"][-24:], grow), "credits")]
    if snap["budget"]:
        share = spend / snap["budget"]
        role = "danger" if share >= 1 else "warn" if share >= 0.8 else "turns"
        figures += [(f"today {spend:,.2f} of {snap['budget']:g} AIU", "credits"),
                    (f"{_live_bar(min(share, 1), 1, 10)} {share:.0%}", role)]
    else:
        figures.append((f"today {spend:,.2f} AIU", "credits"))
    figures += [(f"{snap['calls']:,} calls", "number"),
                (f"{snap['failures']:,} failed" if snap["failures"] else "",
                 "danger"),
                (f"{snap['agents']} sub-agents" if snap["agents"] else "", "active"),
                (f"{snap['repos']} repos" if snap["repos"] else "", "repo"),
                (f"longest up {_live_span(snap['longest'])}"
                 if snap["longest"] else "", "help")]
    rows += _live_flow(figures, width)
    rows.append([(0, "─" * (width - 1), "separator")])
    return rows


def _live_page(snap: dict, width: int, now: float, *, cursor: int = 0,
               marks: dict | None = None, dealt: int | None = None,
               pulse: str = "active", grow: float = 1.0
               ) -> tuple[list[list[tuple[int, str, str]]],
                          list[list[tuple[int, str, str]]],
                          list[tuple[int, int, int, int]]]:
    """(band rows, card rows, where each card sits: top, left, bottom, right)."""
    band = _live_band(snap, width, grow)
    sessions = snap["sessions"]
    if not sessions:
        empty = [[], [(2, ui.clip("No Copilot CLI is running right now.",
                                  width - 3), "summary")],
                 [(2, ui.clip("Start one with `copilot` and it appears here "
                              f"within {LIVE_REFRESH_SECONDS}s.", width - 3),
                   "help")]]
        return band, empty, []
    columns = _live_columns(width)
    usable = width - 1
    card = (usable - (columns - 1)) // columns
    rows: list[list[tuple[int, str, str]]] = []
    boxes = []
    marks = marks or {}
    for index, item in enumerate(sessions):
        if dealt is not None and index >= dealt:
            break
        top = (index // columns) * _LIVE_CARD_ROWS
        left = (index % columns) * (card + 1)
        while len(rows) < top + _LIVE_CARD_ROWS:
            rows.append([])
        age = now - marks[item["id"]] if item["id"] in marks else None
        drawn = _live_card(item, card, now, chosen=index == cursor,
                           flash=ui.flash_role(age) if age is not None else None,
                           pulse=pulse)
        for offset, line in enumerate(drawn):
            rows[top + offset] += [(left + x, text, role) for x, text, role in line]
        boxes.append((top, left, top + _LIVE_CARD_ROWS, left + card))
    return band, rows, boxes


def _live_hint(width: int) -> str:
    for hint in (" ↑↓ ←→ choose · ↵ open session · r refresh · q back ",
                 " ↑↓ choose · ↵ open · r refresh · q back ",
                 " ↑↓ · ↵ open · q back ", " ↵ · q "):
        if ui.cells(hint) <= width - 1:
            return hint
    return ""


def _live_tui(screen, state: dict):
    """The page. Returns the id of a session to open, or None to go back."""
    import curses

    screen.keypad(True)
    theme = ui.tui_theme(curses)
    try:
        curses.curs_set(0)
        screen.bkgd(" ", theme["background"])
    except curses.error:
        pass
    mouse = _enable_mouse(curses)
    last_click = [0.0, -1]
    pending: list[int] = []
    opened = time.monotonic() if not state.get("dealt") else None
    read_at = 0.0
    scroll = 0
    try:
        while True:
            clock = time.monotonic()
            if clock - read_at >= LIVE_REFRESH_SECONDS or "snapshot" not in state:
                _live_read(state)
                read_at = time.monotonic()
            snap = state["snapshot"]
            sessions = snap["sessions"]
            cursor = state["cursor"] = min(max(state.get("cursor", 0), 0),
                                           max(len(sessions) - 1, 0))
            height, width = screen.getmaxyx()
            dealt = None
            grow = 1.0
            if opened is not None:
                elapsed = clock - opened
                dealt = int(elapsed / _LIVE_DEAL_SECONDS) + 1
                grow = ui.launch_progress(elapsed, 0.0, 0.6)
                if dealt > len(sessions) and grow >= 1:
                    opened, dealt = None, None
                    state["dealt"] = True
            marks = state.setdefault("marks", {})
            for sid in [sid for sid, at in marks.items()
                        if clock - at >= ui.FLASH_SECONDS]:
                del marks[sid]
            band, rows, boxes = _live_page(
                snap, width, clock, cursor=cursor, marks=marks, dealt=dealt,
                pulse=ui.pulse_role(clock), grow=grow)
            top = len(band)
            room = max(height - top - 1, 1)
            if boxes and cursor < len(boxes):
                first, _l, last, _r = boxes[cursor]
                if last - scroll > room:
                    scroll = last - room
                if first < scroll:
                    scroll = first
            scroll = min(scroll, max(len(rows) - room, 0))
            screen.erase()
            for y, line in enumerate(band[:height - 1]):
                for x, text, role in line:
                    _addstr(screen, y, x, text, width, theme[role])
            for y, line in enumerate(rows[scroll:scroll + room], top):
                for x, text, role in line:
                    _addstr(screen, y, x, text, width, theme[role])
            hint = _live_hint(width)
            more = len(rows) - scroll - room
            if more > 0 and ui.cells(hint) + 12 <= width - 1:
                hint += f"· {more} rows below "
            _addstr(screen, height - 1, 0, hint, width, theme["status"])
            screen.refresh()
            motion = opened is not None or bool(marks)
            busy = any(item["status"] in ("working", "thinking") for item in sessions)
            screen.timeout(ui.MOTION_MS if motion else ui.PULSE_MS if busy else 1000)
            try:
                key = pending.pop(0) if pending else screen.getch()
            except KeyboardInterrupt:
                return None
            if key == -1:
                continue
            opened = None
            state["dealt"] = True
            event = _mouse_event(screen, curses, key, last_click, pending)
            if event:
                kind, x, y = event
                if kind in ("click", "double"):
                    for index, (b_top, b_left, b_bottom, b_right) in enumerate(boxes):
                        if (b_top <= y - top + scroll < b_bottom
                                and b_left <= x < b_right):
                            state["cursor"] = index
                            if kind == "double":
                                return sessions[index]["id"]
                elif kind == "wheel-up":
                    state["cursor"] = cursor - 1
                elif kind == "wheel-down":
                    state["cursor"] = cursor + 1
                continue
            columns = _live_columns(width)
            if key in (27, ord("q"), ord("Q")):
                return None
            if key in (10, 13, curses.KEY_ENTER) and sessions:
                return sessions[cursor]["id"]
            if key in (ord("r"), ord("R")):
                read_at = 0.0
            elif key in (curses.KEY_UP, ord("k")):
                state["cursor"] = cursor - columns if cursor >= columns else cursor
            elif key in (curses.KEY_DOWN, ord("j")):
                state["cursor"] = (cursor + columns
                                   if cursor + columns < len(sessions) else cursor)
            elif key in (curses.KEY_LEFT, ord("h")):
                state["cursor"] = cursor - 1
            elif key in (curses.KEY_RIGHT, ord("l")):
                state["cursor"] = cursor + 1
            elif key == curses.KEY_HOME:
                state["cursor"] = 0
            elif key == curses.KEY_END:
                state["cursor"] = len(sessions) - 1
    finally:
        if mouse:
            _disable_mouse()


def _live_text(width: int) -> str:
    """The page as plain text, for a pipe or a terminal with no curses."""
    state: dict = {}
    _live_read(state)
    band, rows, _boxes = _live_page(state["snapshot"], width, time.monotonic())
    out = []
    for line in [*band, *rows]:
        text = ""
        for x, part, _role in sorted(line):
            text = text.ljust(x) if ui.cells(text) < x else text
            text += part
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
                "failures", "repos", "agents", "longest")}}


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
