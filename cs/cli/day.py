"""Today, whole: everything since midnight on one live dashboard.

`cs live` is the sessions running now and `cs today` is where you are. This
is the day's ledger, drawn as a dashboard in four tabs that each fit one
screen: an Overview (spend in large figures against yesterday, six cards
with their hour-by-hour shape, and the day's spend as a running total beside
yesterday's), every Session on a 24-hour timeline, a Breakdown of models —
spend against the time each one ran — repositories and how the calls ran,
and the day's Activity, ending in a live feed of what the agents are doing.

Every figure is cut to the local day by its own timestamp, not by when its
session began: a session opened last night counts only what it did today.
A figure the store cannot time is left off the page rather than shown as a
zero, and the inferred ones — active time, the comparison with yesterday —
say what they were worked out from.

Today rereads itself in the background, so its motion never stalls: values
roll to their new figure, a running session's spinner turns, the newest
event types into the ticker. Earlier days hold still.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from .. import db, events, ui
from ._common import (
    _addstr,
    _curses_wrapper,
    _disable_mouse,
    _enable_mouse,
    _mouse_event,
)
from .evidence import _clean
from .live import (
    _live_feed,
    _live_line,
    _live_lit,
    _live_panel,
    _live_right,
    _live_short,
    _live_span,
    _live_status,
    _live_status_mark,
    _live_theme,
    _live_wave,
    _LiveTail,
)
from .session import cmd_show

# How often today rereads the store and the logs — in the background, so
# nothing on screen waits for it. Earlier days hold still.
DAY_REFRESH_SECONDS = 3
# How far back ← goes.
_DAY_BACK = 366
# Active time is the five-minute slots that held an ask or a model call.
_DAY_SLOT_MINUTES = 5
# The running total and the session timelines are drawn in ten-minute steps.
_DAY_CURVE_MINUTES = 10
# Rows in each ranked panel before the rest are summed or counted.
_DAY_MODELS = 6
_DAY_REPOS = 8
_DAY_TOOLS = 6
_DAY_SHIPPED = 6
# Events kept for the ticker and the live feed.
_DAY_FEED = 30
_DAY_TABS = (("overview", "Overview"), ("sessions", "Sessions"),
             ("breakdown", "Breakdown"), ("activity", "Activity"))

# ── Motion ───────────────────────────────────────────────────────────
# The first open plays an entrance on one clock, in seconds: the title
# types, the spend rolls up like an odometer, the cards deal in, the running
# total draws itself across the day, and the panels below wipe in. A tab or
# a day replays its own panels faster. Each panel takes this long to wipe in.
_DAY_PANEL_SECONDS = 0.45
_DAY_INTRO_SECONDS = 2.1
_DAY_SWITCH_PACE = 2.4
# A tab's content slides in from the side it was chosen from, and the
# underline glides from the old tab to the new one.
_DAY_SLIDE_SECONDS = 0.28
_DAY_GLIDE_SECONDS = 0.25
# A figure a reread changed rolls from its old value to its new one.
_DAY_ROLL_SECONDS = 0.8
# The newest event types into the ticker this fast.
_DAY_TICK_SECONDS = 0.6
# The spinner's step while a session is running.
_DAY_SPIN_MS = 110

# Who started each billed call, in the order the split draws them.
_DAY_WHO = (("user", "you", "title"), ("agent", "agent", "active"),
            ("sub-agent", "sub-agents", "turns"), ("compaction", "compaction", "warn"),
            ("", "other", "separator"))
# Roles whose text is a number that counts up as its panel deals in.
_DAY_COUNTED = frozenset({"number", "credits"})
_DAY_EIGHTHS = " ▏▎▍▌▋▊▉█"
_DAY_SPARKS = " ▁▂▃▄▅▆▇█"
_DAY_SHADES = " ░▒▓█"
# Large figures, three rows tall, from the half and full blocks the bars
# already use. The comma drops below the line, the point sits on it.
_DAY_FONT = {
    "0": ("█▀█", "█ █", "▀▀▀"), "1": ("▀█ ", " █ ", "▀▀▀"),
    "2": ("▀▀█", "█▀▀", "▀▀▀"), "3": ("▀▀█", " ▀█", "▀▀▀"),
    "4": ("█ █", "▀▀█", "  ▀"), "5": ("█▀▀", "▀▀█", "▀▀▀"),
    "6": ("█▀▀", "█▀█", "▀▀▀"), "7": ("▀▀█", "  █", "  ▀"),
    "8": ("█▀█", "█▀█", "▀▀▀"), "9": ("█▀█", "▀▀█", "▀▀▀"),
    ".": (" ", " ", "▀"), ",": (" ", " ", "▄"), "—": ("  ", "▀▀", "  "),
    "k": ("█  ", "█▄▀", "▀ ▀"), "M": ("█▄ ▄█", "█ ▀ █", "▀   ▀"), " ": (" ", " ", " "),
}
# Braille dots by (row, column) within a cell: two columns of four.
_DAY_DOTS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))


# ── The day's window ─────────────────────────────────────────────────

def _day_bounds(day: date) -> tuple[datetime, datetime]:
    """Local midnight at the start of `day` and at the start of the next.

    Built from the calendar date rather than by subtracting hours from now,
    so a day that crosses a clock change is 23 or 25 hours long, as it was.
    """
    after = day + timedelta(days=1)
    return (datetime(day.year, day.month, day.day).astimezone(),
            datetime(after.year, after.month, after.day).astimezone())


def _day_utc(moment: datetime) -> str:
    """A moment as the store's UTC stamp shape."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _day_local(stamp: str) -> datetime | None:
    """A stored UTC stamp as a local moment, or None if it is not a time."""
    try:
        return datetime.fromisoformat(stamp[:19]).replace(
            tzinfo=timezone.utc).astimezone()
    except (TypeError, ValueError):
        return None


def _day_offset(word: str | None) -> int | None:
    """`today`, `yesterday`, a number of days back, or a date — as days back.

    None for anything else, which the caller turns into a usage line.
    """
    if word is None or word in ("", "today"):
        return 0
    if word == "yesterday":
        return 1
    if word.isdigit():
        return min(int(word), _DAY_BACK)
    if word.startswith("-") and word[1:].isdigit():
        return min(int(word[1:]), _DAY_BACK)
    try:
        back = (date.today() - date.fromisoformat(word)).days
    except ValueError:
        return None
    return back if 0 <= back <= _DAY_BACK else None


def _day_names(day: date, offset: int) -> tuple[str, str]:
    """What the page calls the day, and the date written out."""
    written = f"{day:%A} {day.day} {day:%B}"
    if offset == 0:
        return "Today", written
    if offset == 1:
        return "Yesterday", written
    if day.year != date.today().year:
        written += f" {day.year}"
    return f"{day:%A}", written


# ── Reading it ───────────────────────────────────────────────────────

def _day_data(offset: int = 0, memo: dict | None = None, tools: bool = True,
              fatal: bool = True, tails: dict | None = None) -> dict:
    """The day's figures as plain, already-masked data — what `--json` returns
    and what the page draws.

    `memo` and `tails` are kept by a page between rereads, so the event logs
    are read on from where the last look stopped. `tools=False` skips the
    logs entirely, which is what lets the first frame paint before they are
    read.
    """
    offset = max(0, min(int(offset), _DAY_BACK))
    now = datetime.now().astimezone()
    day = now.date() - timedelta(days=offset)
    start, end = _day_bounds(day)
    since, until = _day_utc(start), _day_utc(end)
    live = offset == 0
    hours = max(1, round((end - start).total_seconds() / 3600))
    step = _DAY_CURVE_MINUTES * 60
    slots = max(1, round((end - start).total_seconds() / step))
    before_start, before_end = _day_bounds(day - timedelta(days=1))
    # Today is compared with yesterday up to the same time of day: a morning
    # set against a whole day always reads as a quiet one.
    before_cut = min(before_start + (now - start), before_end) if live else before_end
    week_start = _day_bounds(day - timedelta(days=7))[0]

    conn = db.connect(fatal=fatal)
    try:
        usage = db.day_usage(conn, since, until)
        turns = db.day_turns(conn, since, until)
        files = db.day_files(conn, since, until)
        refs = db.day_refs(conn, since, until)
        started = db.day_started(conn, since, until)
        timed = db.has_usage(conn) and db.usage_is_windowable(conn)
        delegation = db.has_delegation(conn)
        yesterday = db.spend_times(conn, _day_utc(before_start), _day_utc(before_end))
        week = (db.spend_windows(conn, [(_day_utc(week_start), since)]) or [None])[0]
        ids = list(dict.fromkeys([row[0] for row in usage] + [sid for sid, _ in turns]))
        # A commit can be recorded for a session that did nothing else today;
        # it still needs its title.
        rows = db.sessions_by_id(conn, list(dict.fromkeys(
            ids + [sid for sid, _kind, _value in refs or []])))
    finally:
        conn.close()
    running = [sid for sid, _pid in db.running_sessions()] if live else []

    by_hour = [{"hour": (start + timedelta(hours=h)).astimezone().strftime("%H:00"),
                "nano_aiu": 0, "calls": 0, "asks": 0, "sessions": 0,
                "active_minutes": 0} for h in range(hours)]
    hour_sessions: list[set] = [set() for _ in range(hours)]
    by_slot = [0] * slots
    cards: dict[str, dict] = {}
    active: set[int] = set()
    stamps: list[str] = []

    def card(sid: str) -> dict:
        if sid not in cards:
            row = rows.get(sid)
            folder = os.path.basename((row[4] if row else "").rstrip("/"))
            cards[sid] = {
                "id": sid,
                "title": (_clean(row[2] if row and row[2] else "")
                          or _clean((db.session_name(sid) or ("", False))[0])
                          or sid[:8]),
                "repository": _clean(row[3] if row else "") or None,
                # Where it ran, for a session with no repository: its folder.
                "place": (_clean(row[3] if row else "")
                          or (_clean(folder) + "/" if folder else "")),
                "asks": 0, "calls": 0, "nano_aiu": 0, "models": Counter(),
                "first": "", "last": "", "commits": 0, "prs": 0,
                "new": sid in started, "live": sid in running, "status": None,
                "by_hour": [0] * hours, "slots": [0] * slots, "asked": [0] * slots,
            }
        return cards[sid]

    def seen(entry: dict, stamp: str) -> tuple[int, int] | None:
        """Note the moment; return (hour, slot) for a real time."""
        moment = _day_local(stamp)
        if moment is None:
            return None
        stamps.append(stamp)
        seconds = (moment - start).total_seconds()
        five = int(seconds // (_DAY_SLOT_MINUTES * 60))
        hour = min(max(int(seconds // 3600), 0), hours - 1)
        if five not in active:
            active.add(five)
            by_hour[hour]["active_minutes"] += _DAY_SLOT_MINUTES
        hour_sessions[hour].add(entry["id"])
        entry["first"] = min(entry["first"] or stamp, stamp)
        entry["last"] = max(entry["last"], stamp)
        return hour, min(max(int(seconds // step), 0), slots - 1)

    totals = Counter()
    models: dict[str, list] = {}
    who: Counter = Counter()
    who_calls: Counter = Counter()
    first_token: list[int] = []
    for (sid, model, nano, fresh, out, read, written, reasoning, duration,
         ttft, initiator, stamp) in usage:
        entry = card(sid)
        entry["calls"] += 1
        entry["nano_aiu"] += nano
        name = _clean(model) or "unknown"
        entry["models"][name] += nano
        placed = seen(entry, stamp)
        if placed is not None:
            hour, slot = placed
            by_hour[hour]["nano_aiu"] += nano
            by_hour[hour]["calls"] += 1
            entry["by_hour"][hour] += nano
            entry["slots"][slot] += nano
            by_slot[slot] += nano
        totals.update(nano_aiu=nano, calls=1, input=fresh, output=out,
                      cache_read=read, cache_write=written, reasoning=reasoning,
                      duration_ms=duration)
        held = models.setdefault(name, [0, 0, 0, 0, 0])
        held[0] += 1
        held[1] += nano
        held[2] += duration
        if ttft > 0:
            held[3] += ttft
            held[4] += 1
            first_token.append(ttft)
        kind = initiator if initiator in ("user", "agent", "sub-agent", "compaction") else ""
        who[kind] += nano
        who_calls[kind] += 1
    for sid, stamp in turns:
        entry = card(sid)
        entry["asks"] += 1
        placed = seen(entry, stamp)
        if placed is not None:
            by_hour[placed[0]]["asks"] += 1
            entry["asked"][placed[1]] += 1
    for hour, present in enumerate(hour_sessions):
        by_hour[hour]["sessions"] = len(present)

    shipped = None
    if refs is not None:
        shipped = {"commits": 0, "prs": 0, "items": []}
        for sid, kind, value in refs:
            key = "prs" if kind == "pr" else "commits"
            shipped[key] += 1
            if sid in cards:
                cards[sid][key] += 1
            shipped["items"].append({
                "id": sid, "kind": "pr" if kind == "pr" else "commit",
                "value": _clean(value),
                "title": (cards[sid]["title"] if sid in cards
                          else _clean(rows[sid][2] if sid in rows else "") or sid[:8])})
    touched = None
    if files is not None:
        created = {(sid, path) for sid, path, tool in files if tool == "create"}
        edited = {(sid, path) for sid, path, tool in files} - created
        touched = {"created": len(created), "edited": len(edited)}

    places: dict[str, dict] = {}
    for entry in cards.values():
        name = entry["place"] or "(no repository)"
        place = places.setdefault(name, {"repository": name, "sessions": 0,
                                         "nano_aiu": 0, "asks": 0, "shipped": 0})
        place["sessions"] += 1
        place["nano_aiu"] += entry["nano_aiu"]
        place["asks"] += entry["asks"]
        place["shipped"] += entry["commits"] + entry["prs"]

    # Yesterday's spending, for the comparison and its curve. The cut is the
    # same time of day, so the comparison is like for like.
    cut = _day_utc(before_cut)
    before = whole = None
    before_slots = [0] * max(1, round((before_end - before_start).total_seconds() / step))
    if timed:
        before = sum(nano for at, nano in yesterday if at < cut)
        whole = sum(nano for _at, nano in yesterday)
        for at, nano in yesterday:
            moment = _day_local(at)
            if moment is not None:
                index = int((moment - before_start).total_seconds() // step)
                before_slots[min(max(index, 0), len(before_slots) - 1)] += nano
    now_slot = (min(int((now - start).total_seconds() // step), slots - 1)
                if live else None)

    def running_total(values: list[int], upto: int | None) -> list[int | None]:
        out: list[int | None] = []
        total = 0
        for index, value in enumerate(values):
            total += value
            out.append(total if upto is None or index <= upto else None)
        return out

    # What the running sessions are doing, from the tail of each one's log.
    statuses: dict[str, str] = {}
    feed: list[dict] = []
    if live and running:
        tails = {} if tails is None else tails
        for sid in [sid for sid in tails if sid not in running]:
            del tails[sid]
        clock = time.time()
        for sid in running:
            tail = tails.get(sid)
            if tail is None:
                tail = tails[sid] = _LiveTail(sid)
            tail.read()
            statuses[sid] = _live_status(tail, clock)[0]
            title = (cards[sid]["title"] if sid in cards else
                     _clean((db.session_name(sid) or ("", False))[0]) or sid[:8])
            for serial, at, mark, role, what, raw in list(tail.feed)[-_DAY_FEED:]:
                feed.append({"at": at, "id": sid, "title": title, "mark": mark,
                             "role": role, "what": _clean(what),
                             "detail": tail.clean(serial, raw), "key": (sid, serial)})
        feed.sort(key=lambda entry: -entry["at"])
        feed = feed[:_DAY_FEED]
    for sid, status in statuses.items():
        if sid in cards:
            cards[sid]["status"] = status

    spent = totals["nano_aiu"]
    offered = totals["input"] + totals["cache_read"] + totals["cache_write"]
    peak = max(range(hours), key=lambda h: by_hour[h]["nano_aiu"]) if spent else None
    asked = max(range(hours), key=lambda h: by_hour[h]["asks"]) if turns else None
    now_hour = (min(int((now - start).total_seconds() // 3600), hours - 1)
                if live else None)

    def label(hour: int) -> str:
        return (start + timedelta(hours=hour)).astimezone().strftime("%H:00")

    names = _day_names(day, offset)
    first = min(stamps) if stamps else ""
    last = max(stamps) if stamps else ""
    # Dearest first; among equals the latest, the one you are most likely
    # to want to open again.
    sessions = sorted(cards.values(), key=lambda s: s["last"], reverse=True)
    sessions.sort(key=lambda s: (-s["nano_aiu"], -s["asks"]))
    # Each session keeps one colour on every tab, and in the feed.
    chips = {s["id"]: n for n, s in enumerate(sessions)}
    for entry in feed:
        entry["chip"] = chips.setdefault(entry["id"], len(chips))
    model_ms = totals["duration_ms"]
    reading: dict = {
        "date": day.isoformat(),
        "label": names[0],
        "written": names[1],
        "offset": offset,
        "live": live,
        "since": since,
        "until": until,
        "hours": [label(h) for h in range(hours)],
        "now_hour": now_hour,
        "slot_minutes": _DAY_CURVE_MINUTES,
        "now_slot": now_slot,
        "spend": {
            "nano_aiu": spent if timed else None,
            "before_nano_aiu": before,
            "before_until": cut,
            "day_before_nano_aiu": whole,
            "week_average_nano_aiu": None if week is None else round(week / 7),
            "budget_aiu": ui.daily_budget_aiu() if live else None,
        },
        "curve": {"today": running_total(by_slot, now_slot),
                  "before": running_total(before_slots, None) if timed else []},
        "sessions": len(cards),
        "new_sessions": sum(1 for s in cards.values() if s["new"]),
        "live_sessions": sum(1 for s in cards.values() if s["live"]),
        "running": len(running),
        "asks": len(turns),
        "calls": totals["calls"],
        "repositories": len({s["repository"] for s in cards.values() if s["repository"]}),
        "shipped": shipped,
        "files": touched,
        "tokens": {
            "input": totals["input"], "output": totals["output"],
            "cache_read": totals["cache_read"], "cache_write": totals["cache_write"],
            "reasoning": totals["reasoning"],
            "cache_hit": totals["cache_read"] / offered if offered else None,
            "reasoning_share": (totals["reasoning"] / totals["output"]
                                if totals["output"] else None),
        },
        "model_ms": model_ms,
        "aiu_per_minute": (spent / 1e9 / (model_ms / 60_000)
                           if model_ms and timed else None),
        "first_token_ms": {"p50": db._percentile(first_token, 0.5),
                           "p95": db._percentile(first_token, 0.95)},
        "active_minutes": len(active) * _DAY_SLOT_MINUTES,
        "first": _day_clock(first),
        "last": _day_clock(last),
        "peak": ({"hour": label(peak), "nano_aiu": by_hour[peak]["nano_aiu"]}
                 if peak is not None else None),
        "busiest": ({"hour": label(asked), "asks": by_hour[asked]["asks"]}
                    if asked is not None else None),
        "by_hour": by_hour,
        "models": [
            {"model": name, "calls": calls, "nano_aiu": nano,
             "share": nano / spent if spent else 0.0,
             "time_ms": duration,
             "time_share": duration / model_ms if model_ms else 0.0,
             "aiu_per_minute": nano / 1e9 / (duration / 60_000) if duration else None,
             "average_ms": round(duration / calls) if calls else None,
             "first_token_ms": round(ttft / timed_calls) if timed_calls else None}
            for name, (calls, nano, duration, ttft, timed_calls)
            in sorted(models.items(), key=lambda item: (-item[1][1], -item[1][0]))],
        "repositories_by_spend": sorted(
            places.values(), key=lambda p: (-p["nano_aiu"], -p["asks"])),
        "by_session": [
            {**{key: s[key] for key in (
                "id", "title", "repository", "asks", "calls", "nano_aiu",
                "commits", "prs", "new", "live", "status", "by_hour", "slots",
                "asked")},
             "first": _day_clock(s["first"]), "last": _day_clock(s["last"]),
             "models": [name for name, _ in s["models"].most_common()]}
            for s in sessions],
        "split": ([{"who": shown, "nano_aiu": who[kind], "calls": who_calls[kind]}
                   for kind, shown, _role in _DAY_WHO if who_calls[kind]]
                  if delegation else None),
        "feed": feed,
        "tools": None,
    }
    if tools:
        reading["tools"] = _day_tools(list(cards), since, until, memo)
    else:
        # Whether there is a log to read at all — what decides if the page
        # holds a place for tool calls while it reads them.
        reading["tools_expected"] = any(
            events.events_path(sid).is_file() for sid in cards)
    return reading


def _day_tools(ids: list[str], since: str, until: str, memo: dict | None) -> dict | None:
    """Tool calls in the window from the event logs; None when none has a log."""
    counts = events.window_counts(ids, since, until, memo)
    if not counts["logs"]:
        return None
    return {
        "calls": counts["calls"],
        "failures": counts["failures"],
        "subagents": counts["subagents"],
        "by_tool": [{"tool": _clean(name), "calls": calls, "failures": failures}
                    for name, (calls, failures) in sorted(
                        counts["tools"].items(), key=lambda item: (-item[1][0], item[0]))],
        "skills": [{"skill": _clean(name), "count": count}
                   for name, count in sorted(counts["skills"].items(),
                                             key=lambda item: (-item[1], item[0]))],
    }


def _day_clock(stamp: str) -> str:
    """A stored UTC stamp as the local HH:MM, or '' when it is not a time."""
    moment = _day_local(stamp) if stamp else None
    return moment.strftime("%H:%M") if moment else ""


# ── Formatting ───────────────────────────────────────────────────────

def _day_aiu(nano: float | None) -> str:
    """Spend for a headline: 0.00, 4.20, 42.7, 512, 1.25k."""
    if nano is None:
        return "—"
    aiu = nano / 1e9
    if aiu >= 1e4:
        return _live_short(aiu)
    if aiu >= 100:
        return f"{aiu:,.0f}"
    if aiu >= 10:
        return f"{aiu:.1f}"
    return f"{aiu:.2f}"


def _day_count(value: float) -> str:
    value = int(round(value))
    return f"{value:,}" if value < 100_000 else _live_short(value)


def _day_ms(ms: float | None) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms:.0f}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return _live_span(ms / 1000)


def _day_pct(share: float | None) -> str:
    if share is None:
        return "—"
    if 0 < share < 0.005:
        return "<1%"
    return f"{share:.0%}"


def _day_rate(aiu_per_minute: float | None) -> str:
    """AIU per minute of model time: what a minute of a model costs."""
    if aiu_per_minute is None:
        return "—"
    return f"{aiu_per_minute:,.1f}" if aiu_per_minute < 1000 else _live_short(aiu_per_minute)


def _day_format(key: str, value: float | None) -> str:
    """A card's figure, from the raw number it carries."""
    if value is None:
        return "—"
    if key == "spend":
        return _day_aiu(value)
    if key == "active":
        return _live_span(round(value) * 60)
    if key == "tokens":
        return _live_short(value)
    return _day_count(value)


def _day_bar(share: float, cells: int) -> str:
    """A bar `share` of `cells` long in eighths, on a dotted track."""
    cells = max(cells, 0)
    eighths = round(min(max(share, 0.0), 1.0) * cells * 8)
    if share > 0 and not eighths:
        eighths = 1
    full, part = divmod(eighths, 8)
    drawn = "█" * full + (_DAY_EIGHTHS[part] if part else "")
    return drawn + "░" * (cells - ui.cells(drawn))


def _day_change(now: float | None, before: float | None) -> tuple[str, str] | None:
    """`▲18%` or `▼4%` and its colour, or None when there is nothing to compare."""
    if now is None or before is None:
        return None
    if not before:
        return ("new", "warn") if now else None
    share = (now - before) / before
    if abs(share) < 0.005:
        return "level", "help"
    return (f"{'▲' if share > 0 else '▼'}{abs(share):.0%}",
            "warn" if share > 0 else "active")


def _day_fit(options: list[list[tuple[str, str]]], room: int) -> list[tuple[str, str]]:
    """The first set of parts that fits `room`, or the last clipped to it."""
    for parts in options:
        if sum(ui.cells(text) for text, _ in parts) <= room:
            return parts
    return options[-1] if options else []


def _day_list(items: list[str], room: int, role: str, gap: str = " · "
              ) -> list[tuple[str, str]]:
    """Whole items joined while they fit, then how many did not."""
    parts: list[tuple[str, str]] = []
    used = 0
    for n, item in enumerate(items):
        more = f" +{len(items) - n - 1}" if n < len(items) - 1 else ""
        joint = gap if n else ""
        if used + ui.cells(joint + item + more) > room:
            if n:
                parts.append((f" +{len(items) - n}", "help"))
            else:
                parts.append((ui.trunc(item, room), role))
            break
        if joint:
            parts.append((joint, "separator"))
        parts.append((item, role))
        used += ui.cells(joint + item)
    return parts


def _day_runs(cells: list[tuple[str, str]], x: int = 0) -> list[tuple[int, str, str]]:
    """(char, role) cells as segments, a segment per run of one role."""
    out: list[tuple[int, str, str]] = []
    for n, (ch, role) in enumerate(cells):
        if ch == " " and role == "":
            continue
        if out and out[-1][2] == role and out[-1][0] + len(out[-1][1]) == x + n:
            out[-1] = (out[-1][0], out[-1][1] + ch, role)
        else:
            out.append((x + n, ch, role))
    return out


def _day_put(rows: list[list], panel: list[list], top: int, x: int) -> None:
    """Lay `panel` onto `rows` with its top-left corner at (top, x)."""
    while len(rows) < top + len(panel):
        rows.append([])
    for dy, line in enumerate(panel):
        rows[top + dy] += [(x + px, text, role) for px, text, role in line]


# ── Motion helpers ───────────────────────────────────────────────────
# Nothing below reads the clock. The page hands every frame a `motion` dict;
# `open` is how far into the entrance it is (None once it has played), and
# every part of the page works out its own progress from that.

def _day_ease(motion: dict, start: float, length: float) -> float:
    """Eased progress of one part of the entrance; 1.0 once it has ended."""
    elapsed = motion.get("open")
    return 1.0 if elapsed is None else ui.launch_progress(elapsed, start, length)


def _day_due(motion: dict, start: float, length: float) -> float:
    """Linear progress of one part of the entrance, for a panel's wipe."""
    elapsed = motion.get("open")
    if elapsed is None or not ui.MOTION:
        return 1.0
    return min(max((elapsed - start) / length, 0.0), 1.0)


def _day_reveal(panel: list[list], progress: float, width: int,
                contents: bool = True) -> list[list]:
    """A panel part way through its entrance.

    Each row wipes in from the left, a little behind the row above, so the
    border draws itself down the panel; behind the wipe the bars fill along
    their track and the figures count up. `contents=False` leaves the body
    to animate itself — the charts rise and sweep on their own.
    """
    if progress >= 1 or not ui.MOTION:
        return panel
    if progress <= 0:
        return [[] for _ in panel]
    out = []
    count = max(len(panel) - 1, 1)
    for y, line in enumerate(panel):
        share = min(max(progress * 1.5 - 0.5 * y / count, 0.0), 1.0)
        cut = round(width * (1 - (1 - share) ** 2))
        body = 0 < y < len(panel) - 1
        row = []
        for x, text, role in line:
            if x >= cut:
                continue
            if contents or not body or text == "│":
                grown = ui.grow(text, share)
                if grown != text:
                    text = grown
                elif role in _DAY_COUNTED and not any(ch in text for ch in ":#–"):
                    # A count rolls up; a clock time or a PR number does not.
                    text = ui.count_up(text, share)
            shown = ui.clip(text, cut - x)
            if shown:
                row.append((x, shown, role))
        out.append(row)
    return out


def _day_values(data: dict) -> dict[str, float | None]:
    """The raw figure behind each card, which a reread compares to roll it."""
    tools = data.get("tools")
    shipped = data.get("shipped")
    tokens = data["tokens"]
    return {
        "spend": data["spend"]["nano_aiu"],
        "sessions": data["sessions"], "asks": data["asks"], "calls": data["calls"],
        "tools": tools["calls"] if tools else None,
        "shipped": shipped["commits"] + shipped["prs"] if shipped else None,
        "active": data["active_minutes"],
        "files": (data["files"]["created"] + data["files"]["edited"]
                  if data["files"] else None),
        "tokens": (tokens["input"] + tokens["output"] + tokens["cache_read"]
                   + tokens["cache_write"]),
    }


def _day_shown(data: dict, motion: dict, key: str) -> float | None:
    """A figure as it stands this frame: rolling to a new value, or settled."""
    rolling = motion.get("roll", {})
    return rolling[key] if key in rolling else _day_values(data)[key]


# ── Widgets ──────────────────────────────────────────────────────────

def _day_big_width(text: str, scale: int = 1) -> int:
    glyphs = [_DAY_FONT.get(ch, _DAY_FONT[" "]) for ch in text]
    return sum(len(glyph[0]) * scale for glyph in glyphs) + max(len(glyphs) - 1, 0) * scale


def _day_big(text: str, x: int, grad: int, lit: str | None = None,
             scale: int = 1) -> list[list]:
    """`text` in the large figures, three rows, coloured along the gradient.

    `scale` 2 draws every column twice: on a wide card the figures are then
    about as wide as they are tall, which is what reads as a headline.
    """
    rows: list[list] = [[], [], []]
    digits = max(sum(ch.isdigit() for ch in text), 1)
    seen = 0
    for ch in text:
        glyph = _DAY_FONT.get(ch, _DAY_FONT[" "])
        role = lit or (f"g{min(seen * grad // digits, grad - 1)}" if grad > 1
                       else "credits")
        for r in range(3):
            drawn = "".join(part * scale for part in glyph[r])
            if drawn.strip():
                rows[r].append((x, drawn, role))
        x += (len(glyph[0]) + 1) * scale
        seen += ch.isdigit()
    return rows


def _day_strip(values: list[float], cells: int, upto: int | None, role: str,
               grow: float = 1.0) -> list[tuple[int, str, str]]:
    """An hour-by-hour sparkline `cells` wide; hours still to come are blank."""
    hours = len(values)
    if cells <= 0 or not hours:
        return []
    peak = max(values) or 0
    out: list[tuple[str, str]] = []
    for col in range(cells):
        hour = col * hours // cells
        value = values[hour]
        if upto is not None and hour > upto:
            out.append((" ", ""))
        elif value > 0 and peak:
            out.append((_DAY_SPARKS[max(1, round(value * grow / peak * 8))], role))
        else:
            out.append(("▁", "separator"))
    return _day_runs(out)


def _day_plot(today: list, before: list, cols: int, rows: int, peak: float,
              sweep: float = 1.0, trace: float = 1.0) -> list[list[tuple[str, str]]]:
    """A braille chart: today's running total as a filled area, the day
    before's as a line. (char, kind) per cell, top row first; kind is 'a',
    'b', 'h' (today's newest point) or ''.

    `trace` is how far the day-before line has drawn across, `sweep` how far
    today's area has — the entrance draws one, then the other.
    """
    width, height = cols * 2, rows * 4
    bits = [[0] * cols for _ in range(rows)]
    kinds = [[""] * cols for _ in range(rows)]

    def at(series: list, x: int) -> float | None:
        count = len(series)
        if not count:
            return None
        pos = x * (count - 1) / max(width - 1, 1)
        low = int(pos)
        high = min(low + 1, count - 1)
        if series[low] is None:
            return None
        nxt = series[high] if series[high] is not None else series[low]
        return series[low] + (nxt - series[low]) * (pos - low)

    def dot(x: int, y: int, kind: str) -> None:
        if 0 <= y < height and 0 <= x < width:
            r, c = y // 4, x // 2
            bits[r][c] |= _DAY_DOTS[y % 4][x % 2]
            if kind == "a" or not kinds[r][c]:
                kinds[r][c] = kind

    last = None
    for x in range(min(width, round(width * trace))):
        value = at(before, x)
        if value is None:
            continue
        y = height - 1 - round(value / peak * (height - 1))
        for yy in range(min(y, last if last is not None else y),
                        max(y, last if last is not None else y) + 1):
            dot(x, yy, "b")
        last = y
    head = None
    for x in range(min(width, round(width * sweep))):
        value = at(today, x)
        if value is None:
            break
        top = height - round(value / peak * height)
        for y in range(max(top, 0), height):
            dot(x, y, "a")
        head = (x, min(max(top, 0), height - 1))
    if head:
        kinds[head[1] // 4][head[0] // 2] = "h"
        dot(head[0], head[1], "h")
    return [[(chr(0x2800 + bits[r][c]) if bits[r][c] else " ", kinds[r][c])
             for c in range(cols)] for r in range(rows)]


def _day_track(slots: list[int], asked: list[int], cells: int, peak: float,
               upto: int | None, chip: str, now_cell: int | None
               ) -> list[tuple[int, str, str]]:
    """One session across the day: shaded by what it spent in each step, a
    light mark where you asked something and it billed nothing, and a line
    where now is."""
    count = len(slots)
    out: list[tuple[str, str]] = []
    for col in range(cells):
        low = col * count // cells
        high = max((col + 1) * count // cells, low + 1)
        spent = sum(slots[low:high])
        asks = sum(asked[low:high])
        if col == now_cell:
            out.append(("│", "title"))
        elif upto is not None and low > upto:
            out.append((" ", ""))
        elif spent > 0 and peak:
            level = min(max(-(-spent * 4 // max(peak, 1)), 1), 4)
            out.append((_DAY_SHADES[int(level)], chip))
        elif asks:
            out.append(("░", chip))
        elif (low * _DAY_CURVE_MINUTES) % 360 == 0:
            out.append(("·", "separator"))
        else:
            out.append((" ", ""))
    return _day_runs(out)


def _day_rank_rows(items: list[tuple], inner: int) -> list[list]:
    """Ranked rows: a chip, a name, a bar of its share, and its figures.

    `items` are (name, share, bar_role, figures, chip_role, short). The name
    is what you read, so it gets its room before the bar does; where even
    that is too tight the row falls back to `short` (a repository without
    its owner) before it truncates.
    """
    if not items:
        return []
    figures_w = max(sum(ui.cells(text) for text, _ in item[3]) for item in items)
    room = inner - 3 - figures_w - 1 - 6
    longest = max(ui.cells(item[0]) for item in items)
    if longest > room:
        longest = max(ui.cells(item[0] if ui.cells(item[0]) <= room else item[5])
                      for item in items)
    if longest > room and (longest <= inner - 3 - figures_w - 1
                           or min(longest, 28, room) < min(longest, 12)):
        # The bar gives way before the name does: a share without its name
        # is a bar of nothing.
        room = inner - 3 - figures_w - 1
    name_w = max(min(longest, 28, room), min(8, longest))
    bar_w = inner - 3 - name_w - 1 - figures_w
    rows = []
    for name, share, bar_role, figures, chip, short in items:
        shown = name if ui.cells(name) <= name_w else short
        row = [(0, "▌", chip), (2, ui.trunc(shown, name_w), "summary")]
        if bar_w >= 4:
            row.append((3 + name_w, _day_bar(share, bar_w), bar_role))
        if 3 + name_w + figures_w <= inner:
            row += _live_right(figures, inner)
        rows.append(row)
    return rows


def _day_mark(session: dict, motion: dict) -> tuple[str, str]:
    """A session's mark: spinning while it runs, + if it began today."""
    if session.get("status"):
        return _live_status_mark({"status": session["status"]}, motion)
    if session["live"]:
        return "●", motion.get("pulse", "active")
    if session["new"]:
        return "+", "active"
    return "·", "separator"


# ── The frame: title, tabs, ticker ───────────────────────────────────

def _day_tab_spans(usable: int) -> list[tuple[int, int]]:
    """Where each tab's label sits on the tab row: (x, width)."""
    spans = []
    x = 1
    for n, (_key, name) in enumerate(_DAY_TABS):
        label = f" {n + 1} {name} " if usable >= 60 else f" {name[:4]} "
        spans.append((x, ui.cells(label)))
        x += ui.cells(label) + (2 if usable >= 60 else 1)
    return spans


def _day_head(data: dict, usable: int, motion: dict, tab: int | None
              ) -> list[list]:
    """The title row, and — on screen — the tabs and their gliding underline."""
    progress = _day_ease(motion, 0.0, 0.35)
    label = ui.typed(data["label"], progress)
    left = [(f" {ui.menu_icon('today')} ", "title"), (label, "title")]
    if label and data["label"] != data["written"]:
        left += [("  ", "separator"), (ui.typed(data["written"], progress), "help")]
    title = _live_line(left, usable)
    used = sum(ui.cells(text) for _x, text, _r in title)
    if data["live"]:
        dot = ("●", motion.get("pulse", "active"))
        running = data.get("running") or 0
        count = (f" · {running} running", "help") if running else ("", "")
        options = []
        if motion.get("clock"):
            options.append([dot, (" LIVE", "active"), count,
                            ("   " + motion["clock"], "help")])
        options += [[dot, (" LIVE", "active"), count], [dot, (" LIVE", "active")]]
    else:
        back = data["offset"]
        options = [[(f"{back} days ago" if back > 1 else "a day ago", "help"),
                    ("   ← earlier · → later · t today", "separator")],
                   [(f"{back}d ago", "help")]]
    for parts in options:
        if used + sum(ui.cells(text) for text, _ in parts) + 2 <= usable:
            title += _live_right(parts, usable)
            break
    if tab is None:
        return [title]
    spans = _day_tab_spans(usable)
    shown = _day_ease(motion, 0.1, 0.35)
    tabs: list = []
    for n, ((_key, name), (x, width)) in enumerate(zip(_DAY_TABS, spans, strict=True)):
        if x + width > usable:
            break
        text = f" {n + 1} {name} " if usable >= 60 else f" {name[:4]} "
        chosen = n == tab
        if usable >= 60:
            tabs.append((x, ui.typed(f" {n + 1} ", shown), "separator"))
            tabs.append((x + 3, ui.typed(f"{name} ", shown),
                         "title" if chosen else "help"))
        else:
            tabs.append((x, ui.typed(text, shown), "title" if chosen else "help"))
    hint = (" ⇥ tabs  ←→ days " if data["live"] else " ⇥ tabs  t today ")
    end = spans[-1][0] + spans[-1][1]
    if end + ui.cells(hint) + 2 <= usable:
        tabs += _live_right([(hint, "separator")], usable)
    # The underline glides from the tab you left to the one you chose.
    rule = round(usable * _day_ease(motion, 0.05, 0.4))
    line = [(0, "─" * rule, "separator")] if rule else []
    glide = motion.get("glide", 1.0)
    old = spans[min(max(motion.get("tab_from", tab), 0), len(spans) - 1)]
    new = spans[min(max(tab, 0), len(spans) - 1)]
    ease = 1 - (1 - glide) ** 2
    x = round(old[0] + (new[0] - old[0]) * ease)
    width = round(old[1] + (new[1] - old[1]) * ease)
    if rule > x:
        line.append((x, "━" * max(min(width, rule - x), 0), "title"))
    return [title, tabs, line]


def _day_ticker(data: dict, usable: int, motion: dict) -> list:
    """One line of what is happening: the newest event, typed in as it lands."""
    if not data["live"]:
        return _live_line([("◆ ", "separator"),
                           (f"{data['written']} · a past day holds still · t returns "
                            "to today", "help")], usable - 1, 1)
    feed = data.get("feed") or []
    if not feed:
        if data.get("running"):
            return _live_line([("● ", motion.get("pulse", "active")),
                               (f"{data['running']} Copilot CLI running · waiting for "
                                "the next event", "help")], usable - 1, 1)
        last = f" · last activity at {data['last']}" if data["last"] else ""
        return _live_line([("○ ", "separator"),
                           (f"No Copilot CLI running{last}", "help")], usable - 1, 1)
    entry = feed[0]
    chip = f"c{entry['chip'] % max(motion.get('chips', 8), 1)}"
    # The newest event types in as it lands — and, on opening, last of all.
    progress = min(motion.get("ticker", 1.0), _day_ease(motion, 1.5, 0.5))
    if progress <= 0:
        return []
    detail = ui.typed(entry["detail"], progress)
    parts = [("▸ ", "active"),
             (time.strftime("%H:%M:%S", time.localtime(entry["at"])), "help"),
             ("  ", ""), ("▌ ", chip), (ui.trunc(entry["title"], 28), chip), ("   ", ""),
             (entry["mark"] + " ", entry["role"]), (entry["what"], entry["role"])]
    if detail:
        parts += [("  ", ""), (detail, "summary")]
    return _live_line([(text, role or "help") for text, role in parts], usable - 1, 1)


# ── Overview ─────────────────────────────────────────────────────────

def _day_story(data: dict, usable: int, motion: dict) -> list[list]:
    """The day in one sentence, numbers lit — what to read if nothing else."""
    return _day_words(_day_story_parts(data), usable - 2, _day_ease(motion, 0.15, 0.6))


def _day_words(parts: list[tuple[str, str]], room: int, progress: float = 1.0
               ) -> list[list]:
    """`parts` wrapped by words into rows `room` wide, from column 1, typed
    in to `progress` of their length."""
    tokens: list[tuple[str, str, bool]] = []
    gap = False
    for text, role in parts:
        for n, word in enumerate(text.split(" ")):
            gap = gap or n > 0
            if word:
                tokens.append((word, role, gap))
                gap = False
    rows: list[list] = [[]]
    used = 0
    for word, role, gap in tokens:
        space = 1 if gap and used else 0
        if used and used + space + ui.cells(word) > room:
            rows.append([])
            used = space = 0
        rows[-1].append((1 + used + space, ui.clip(word, room - used - space), role))
        used += space + ui.cells(rows[-1][-1][1])
    if progress >= 1 or not ui.MOTION:
        return rows
    budget = round(sum(len(t[1]) for row in rows for t in row) * max(progress, 0.0))
    typed: list[list] = []
    for row in rows:
        kept = []
        for x, text, role in row:
            if budget > 0:
                kept.append((x, text[:budget], role))
            budget -= len(text)
        typed.append(kept)
    return typed


def _day_story_parts(data: dict) -> list[tuple[str, str]]:
    spend = data["spend"]
    if not data["sessions"]:
        return [("Nothing recorded yet today — start a Copilot CLI session and it "
                 f"lands here within {DAY_REFRESH_SECONDS}s." if data["live"]
                 else "Nothing was recorded on this day.", "help")]
    parts: list[tuple[str, str]] = []
    if spend["nano_aiu"] is not None:
        parts += [(f"{_day_aiu(spend['nano_aiu'])} AIU", "credits"),
                  (" so far" if data["live"] else " spent", "help"), (" across ", "help")]
    parts += [(f"{data['sessions']} session{'s' if data['sessions'] != 1 else ''}", "title")]
    if data["repositories"]:
        count = data["repositories"]
        parts += [(" in ", "help"),
                  (f"{count} repositor{'ies' if count != 1 else 'y'}", "repo")]
    if data["model_ms"]:
        parts += [(", ", "help"), (_day_ms(data["model_ms"]), "number"),
                  (" of model time", "help")]
    change = _day_change(spend["nano_aiu"], spend["before_nano_aiu"])
    if change and change[0] not in ("new", "level"):
        more = change[0].startswith("▲")
        parts += [(" — ", "separator"), (change[0][1:], change[1]),
                  (" more" if more else " less", change[1]),
                  (" than by this time yesterday" if data["live"]
                   else " than the day before", "help")]
    elif change and change[0] == "new" and data["live"] and spend["day_before_nano_aiu"]:
        parts += [(" — nothing by this time yesterday, which came to ", "help"),
                  (f"{_day_aiu(spend['day_before_nano_aiu'])} AIU", "credits"),
                  (" in all", "help")]
    elif change and change[0] == "level":
        parts += [(" — level with " + ("yesterday" if data["live"] else "the day before"),
                   "help")]
    parts.append((".", "help"))
    return parts


def _day_compare(data: dict) -> list[list[tuple[str, str]]]:
    """The spend set against the day before, longest wording first."""
    spend = data["spend"]
    change = _day_change(spend["nano_aiu"], spend["before_nano_aiu"])
    against = "by this time yesterday" if data["live"] else "the day before"
    if change is None:
        return [[]]
    if change[0] == "new":
        whole = spend["day_before_nano_aiu"]
        options = ([[("nothing by this time yesterday · ", "help"),
                     (_day_aiu(whole), "number"), (" in all", "help")]]
                   if data["live"] and whole else [])
        return options + [[("nothing ", "help"), (against, "help")], [("none before", "help")]]
    if change[0] == "level":
        return [[("level with yesterday" if data["live"] else "level with the day before",
                  "help")]]
    arrow = (f" {change[0]} ", change[1])
    return [[arrow, (f"  vs {_day_aiu(spend['before_nano_aiu'])} {against}", "help")],
            [arrow, ("  vs yesterday" if data["live"] else "  vs the day before", "help")],
            [arrow]]


def _day_gauge(data: dict, room: int, motion: dict) -> list:
    """Today's spend as a share of what it is measured against: the budget,
    else all of yesterday, else the 7-day average."""
    spend = data["spend"]
    nano = spend["nano_aiu"]
    if nano is None or room < 20:
        return []
    budget = spend["budget_aiu"]
    if budget:
        reference, caption = budget * 1e9, f"of the {budget:g} AIU budget"
        role = ui.budget_style_name(nano / 1e9, budget)
    elif data["live"] and spend["day_before_nano_aiu"]:
        reference = spend["day_before_nano_aiu"]
        caption = f"of yesterday's {_day_aiu(reference)}"
        role = "credits"
    elif spend["week_average_nano_aiu"]:
        reference = spend["week_average_nano_aiu"]
        caption = f"of the 7-day average {_day_aiu(reference)}"
        role = "credits"
    else:
        return []
    share = nano / reference
    tail = f"  {_day_pct(share)} {caption}"
    if ui.cells(tail) + 10 > room:
        tail = f"  {_day_pct(share)}"
    cells = room - ui.cells(tail)
    bar = ui.grow(_day_bar(min(share, 1.0), cells), _day_ease(motion, 0.9, 0.7))
    return [(1, bar, "warn" if share > 1 and not budget else role),
            *_live_line([(tail, "help")], room - cells, 1 + cells)]


def _day_hero(data: dict, width: int, height: int | None, motion: dict) -> list[list]:
    """The day's spend in large figures, set against yesterday, with a gauge."""
    inner = width - 4
    spend = data["spend"]
    value = _day_shown(data, motion, "spend")
    text = _day_aiu(None if value is None else round(value))
    if text != "—":
        text = ui.count_up(text, _day_ease(motion, 0.3, 1.0))
    age = motion.get("flash", {}).get("spend")
    lit = ui.flash_role(age) if age is not None else None
    body: list[list] = [[]]
    # The widest figures that leave room for the unit beside them.
    scale = 2 if _day_big_width("8,888", 2) + 8 <= inner else 1
    figures = _day_big_width(text, scale)
    if figures + 6 <= inner:
        big = _day_big(text, 1, max(motion.get("grad", 1), 1), lit, scale)
        big[2].append((1 + figures + 2, "AIU", "help"))
        body += big
    else:
        body.append(_live_line([(text.strip(), lit or "credits"), (" AIU", "help")],
                               inner - 1, 1))
    body.append([])
    body.append(_live_line(_day_fit(_day_compare(data), inner - 1), inner - 1, 1))
    gauge = _day_gauge(data, inner - 1, motion)
    if gauge:
        body.append(gauge)
    room = None if height is None else height - 2
    if spend["week_average_nano_aiu"] is not None and (room is None or len(body) < room):
        rate = data.get("aiu_per_minute")
        parts = [("7-day average ", "help"),
                 (_day_aiu(spend["week_average_nano_aiu"]), "number"), (" a day", "help")]
        if rate is not None:
            parts = [(_day_rate(rate), "number"), (" AIU a model-minute", "help"),
                     ("  ·  ", "separator"), *parts]
        body.append(_live_line(_day_fit([parts, parts[-3:]], inner - 1), inner - 1, 1))
    if room is not None:
        # Spare rows go above and below the figures, so they sit centred.
        while len(body) < room:
            if len(body) % 2:
                body.insert(0, [])
            else:
                body.append([])
    note = ([("● ", motion.get("pulse", "active")), ("live", "active")]
            if data["live"] else [(data["written"], "help")])
    return _live_panel([("AI spend", "header")], note, body, width, height)


def _day_cards_of(data: dict, motion: dict) -> list[dict]:
    """Six headline cards, in order; a figure this store cannot give makes
    room for the next one."""
    values = {key: _day_format(key, _day_shown(data, motion, key))
              for key in _day_values(data)}
    hours = data["by_hour"]
    upto = data["now_hour"]
    tools = data["tools"]
    shipped = data["shipped"]
    cards = []
    detail = []
    if data["new_sessions"]:
        detail.append((f"{data['new_sessions']} new", "help"))
    if data["repositories"]:
        count = data["repositories"]
        detail.append((f"{count} repositor{'ies' if count != 1 else 'y'}", "help"))
    joined: list[tuple[str, str]] = []
    for part in detail:
        joined += [(" · ", "separator"), part] if joined else [part]
    cards.append({"key": "sessions", "label": "SESSIONS", "value": values["sessions"],
                  "role": "title",
                  "right": ([("● ", motion.get("pulse", "active")),
                             (f"{data['live_sessions']} live", "active")]
                            if data["live_sessions"] else []),
                  "strip": [h["sessions"] for h in hours], "strip_role": "active",
                  "sub": [joined, detail[:1]]})
    busy = data["busiest"]
    cards.append({"key": "asks", "label": "ASKS", "value": values["asks"], "role": "title",
                  "right": [("peak ", "help"), (busy["hour"], "number")] if busy else [],
                  "strip": [h["asks"] for h in hours], "strip_role": "turns",
                  "sub": [[(f"{busy['asks']} in the busiest hour", "help")]] if busy else [[]]})
    cache = data["tokens"]["cache_hit"]
    models = len(data["models"])
    cards.append({"key": "calls", "label": "MODEL CALLS", "value": values["calls"],
                  "role": "title",
                  "right": [(_day_ms(data["model_ms"]), "number")] if data["model_ms"] else [],
                  "strip": [h["calls"] for h in hours], "strip_role": "repo",
                  "sub": [[(f"{models} model{'s' if models != 1 else ''}", "help")]
                          + ([(" · ", "separator"), (_day_pct(cache), "number"),
                              (" from cache", "help")] if cache is not None else []),
                          [(f"{models} models", "help")]]})
    if tools is not None or (data.get("tools_pending") and data.get("tools_expected")):
        if tools is None:
            cards.append({"key": "tools", "label": "TOOL CALLS", "value": "…",
                          "role": "help", "right": [],
                          "line": [("reading the logs…", "help")], "sub": [[]]})
        else:
            failed = tools["failures"]
            agents, skills = tools["subagents"], len(tools["skills"])
            cards.append({
                "key": "tools", "label": "TOOL CALLS", "value": values["tools"],
                "role": "title",
                "right": [(f"{failed} failed", "danger" if failed else "help")],
                "items": [f"{t['tool']} {t['calls']:,}" for t in tools["by_tool"]],
                "item_role": "summary",
                "sub": [[(f"{agents} sub-agent run{'s' if agents != 1 else ''}", "help"),
                         (" · ", "separator"),
                         (f"{skills} skill{'s' if skills != 1 else ''}", "help")],
                        [(f"{agents} sub-agents", "help")]]})
    if shipped is not None:
        commits, prs = shipped["commits"], shipped["prs"]
        refs = [f"#{i['value'].lstrip('#')}" if i["kind"] == "pr" else i["value"][:7]
                for i in shipped["items"]]
        cards.append({"key": "shipped", "label": "SHIPPED", "value": values["shipped"],
                      "role": "active", "right": [],
                      "items": refs, "item_role": "number", "gap": "  ",
                      "line": [("nothing yet", "help")],
                      "sub": [[(f"{commits} commit{'s' if commits != 1 else ''} · "
                                f"{prs} PR{'s' if prs != 1 else ''}", "help")]]})
    cards.append({"key": "active", "label": "ACTIVE", "value": values["active"],
                  "role": "title",
                  "right": ([(data["first"], "number"), (" → ", "separator"),
                             (data["last"], "number")] if data["first"] else []),
                  "strip": [h["active_minutes"] for h in hours], "strip_role": "title",
                  "sub": [[(f"{_DAY_SLOT_MINUTES}-minute slots with an ask or a call",
                            "help")], [(f"{_DAY_SLOT_MINUTES}-min slots", "help")]]})
    if data["files"] is not None:
        made, edited = data["files"]["created"], data["files"]["edited"]
        cards.append({"key": "files", "label": "FILES", "value": values["files"],
                      "role": "title", "right": [],
                      "line": [(f"{made} created", "summary"), (" · ", "separator"),
                               (f"{edited} edited", "summary")], "sub": [[]]})
    tokens = data["tokens"]
    cards.append({"key": "tokens", "label": "TOKENS", "value": values["tokens"],
                  "role": "title",
                  "right": [(_day_pct(cache), "number"), (" cached", "help")]
                  if cache is not None else [],
                  "line": [("in ", "help"), (_live_short(tokens["input"]), "number"),
                           ("  out ", "help"), (_live_short(tokens["output"]), "number")],
                  "sub": [[]]})
    for card in cards:
        if card.get("strip") is not None:
            card["upto"] = upto
    return cards[:6]


def _day_card(card: dict, width: int, motion: dict, index: int) -> list[list]:
    """One headline card: its figure, its shape across the day, a line under."""
    inner = width - 4
    progress = _day_ease(motion, 0.35 + index * 0.07, 0.7)
    value = card["value"]
    shown = ui.count_up(value, progress).strip() or value
    age = motion.get("flash", {}).get(card["key"])
    lit = ui.flash_role(age) if age is not None else None
    top = [(shown, lit or card["role"])]
    step = motion.get("delta", {}).get(card["key"])
    if lit and step:
        top.append((" " + step, "warn" if step.startswith("▲") else "active"))
    row = _live_line(top, inner)
    used = sum(ui.cells(text) for _x, text, _r in row)
    right = card["right"]
    if right and used + sum(ui.cells(text) for text, _ in right) + 2 <= inner:
        row += _live_right(right, inner)
    if card.get("strip") is not None:
        middle = _day_strip(card["strip"], inner, card.get("upto"), card["strip_role"],
                            _day_ease(motion, 0.5 + index * 0.07, 0.8))
    elif card.get("items"):
        middle = _live_line(_day_list(card["items"], inner, card["item_role"],
                                      card.get("gap", " · ")), inner)
    else:
        middle = _live_line(card.get("line") or [], inner)
    return _live_panel([(card["label"], "label")], [],
                       [row, middle, _live_line(_day_fit(card["sub"], inner), inner)],
                       width, 5)


def _day_curve(data: dict, width: int, rows: int, motion: dict
               ) -> tuple[list, list, list]:
    """The day's spend as a running total beside the day before's, in braille."""
    inner = width - 4
    today = data["curve"]["today"]
    before = data["curve"]["before"]
    known = [value for value in today if value is not None]
    peak = max(known + list(before) + [0])
    title = [("Spend through the day", "header")]
    if not peak:
        return title, [], [_live_line([("Nothing billed on this day.", "help")], inner)]
    gutter = 7 if inner >= 50 else 0
    cols = max(inner - gutter, 8)
    grad = max(motion.get("grad", 1), 1)
    sweep = _day_ease(motion, 0.8, 1.0)
    trace = _day_ease(motion, 0.6, 0.6)
    body: list[list] = []
    if ui._ASCII_GLYPHS:
        lines, quiet = _live_wave([value or 0 for value in today], cols, rows, sweep)
        cells = [[(ch, "" if ch == " " else "a") for ch in line] for line in lines]
    else:
        cells = _day_plot(today, before, cols, rows, peak, sweep, trace)
    head = next(((r, c) for r, line in enumerate(cells) for c, (_ch, kind)
                 in enumerate(line) if kind == "h"), None)
    for r, line in enumerate(cells):
        row: list = []
        if gutter:
            label = (_day_aiu(peak) if r == 0 else _day_aiu(peak / 2) if r == rows // 2
                     and rows >= 5 else "0" if r == rows - 1 else "")
            if label:
                row.append((max(gutter - 2 - ui.cells(label), 0), label, "help"))
            row.append((gutter - 1, "┤" if label else "│", "separator"))
        painted = []
        for c, (ch, kind) in enumerate(line):
            role = ("" if not kind else "help" if kind == "b" else
                    motion.get("pulse", "title") if kind == "h" and data["live"] else
                    f"g{min(c * grad // cols, grad - 1)}")
            painted.append((ch, role))
        row += _day_runs(painted, gutter)
        if head and head[0] == r and sweep >= 1:
            value = known[-1] if known else 0
            text = f" {_day_aiu(value)}"
            x = gutter + head[1] + 1
            if x + ui.cells(text) <= inner:
                row.append((x, text, "credits"))
        body.append(row)
    hours = len(data["hours"])
    axis: list = []
    step = next(n for n in (1, 2, 3, 4, 6, 12, 24) if cols * n / hours >= 4)
    for hour in range(0, hours, step):
        x = gutter + round(hour * cols / hours)
        if x + 2 > inner:
            break
        axis.append((x, data["hours"][hour][:2],
                     "title" if hour == data["now_hour"] else "help"))
    body.append(axis)
    note = [("⣿ ", "g0" if grad > 1 else "credits"), ("today", "help")]
    if before and not ui._ASCII_GLYPHS:
        note += [("   ⠒⠒ ", "help"), ("yesterday" if data["live"] else "day before", "help")]
    return title, note, body


def _day_top(data: dict, width: int, motion: dict, limit: int
             ) -> tuple[list, list, list, list[int]]:
    """The day's dearest sessions, each with its share of the spend."""
    inner = width - 4
    sessions = data["by_session"][:limit]
    title = [("Top sessions", "header")]
    if not sessions:
        return title, [], [_live_line([("No sessions.", "help")], inner)], []
    spent = data["spend"]["nano_aiu"] or 0
    chips = max(motion.get("chips", 8), 1)
    items = [(s["title"], s["nano_aiu"] / spent if spent else 0.0, f"c{n % chips}",
              [(ui.fmt_aiu(s["nano_aiu"]).rjust(6), "credits")], f"c{n % chips}",
              s["title"]) for n, s in enumerate(sessions)]
    body = []
    cursor = motion.get("cursor")
    moved = motion.get("moved")
    for n, row in enumerate(_day_rank_rows(items, inner)):
        row[0] = (0, *_day_mark(sessions[n], motion))
        if n == cursor:
            sweep = 1.0 if moved is None else min(moved / ui.SWEEP_SECONDS, 1.0)
            row = _live_lit(row, round(inner * (1 - (1 - sweep) ** 2)))
        body.append(row)
    note = [(f"{len(data['by_session'])} today", "help")]
    return title, note, body, list(range(len(body)))


def _day_models_mini(data: dict, width: int, motion: dict, limit: int
                     ) -> tuple[list, list, list]:
    """Models by spend, with the time each one ran beside it."""
    inner = width - 4
    models = data["models"][:limit]
    chips = max(motion.get("chips", 8), 1)
    title = [("Models", "header")]
    if not models:
        return title, [], [_live_line([("No model calls.", "help")], inner)]
    items = [(m["model"], m["share"], f"c{n % chips}",
              [(_day_aiu(m["nano_aiu"]).rjust(6), "credits"),
               (f"{_day_ms(m['time_ms']) if m['time_ms'] else '—':>8}", "number")],
              f"c{n % chips}", m["model"]) for n, m in enumerate(models)]
    note = [("AIU · time", "help")]
    return title, note, _day_rank_rows(items, inner)


def _day_repos(data: dict, width: int, motion: dict, limit: int = _DAY_REPOS
               ) -> tuple[list, list, list]:
    inner = width - 4
    places = data["repositories_by_spend"]
    spent = data["spend"]["nano_aiu"] or 0
    chips = max(motion.get("chips", 8), 1)
    if not places:
        return ([("Repositories", "header")], [],
                [_live_line([("No repository worked in.", "help")], inner)])
    items = []
    for n, place in enumerate(places[:limit]):
        figures = [(_day_aiu(place["nano_aiu"]).rjust(6), "credits")]
        if inner >= 44:
            figures.append((f" {place['sessions']:>2} sess", "number"))
        if inner >= 56:
            figures.append((f" {place['asks']:>4} asks", "number"))
        if place["shipped"] and inner >= 52:
            figures.append((f" ✓{place['shipped']:<2}", "active"))
        elif inner >= 52:
            figures.append(("    ", "separator"))
        share = place["nano_aiu"] / spent if spent else 0.0
        items.append((place["repository"], share, "repo", figures, f"c{(n + 3) % chips}",
                      place["repository"].rstrip("/").rsplit("/", 1)[-1]))
    body = _day_rank_rows(items, inner)
    if len(places) > limit:
        body.append(_live_line([(f"+{len(places) - limit} more", "help")], inner, 2))
    return ([("Repositories", "header")], [(f"{len(places)}", "help")], body)


def _day_row_of(specs: list, usable: int, motion: dict, start: float,
                height: int | None = None) -> tuple[list[list], list]:
    """Panels side by side at one height, wiping in one after another.

    `specs` are (builder(width) -> (title, note, body[, lines]), share of
    the width). Returns the rows and, for panels that list sessions, where
    each session landed: (y, left, right, index).
    """
    gap = 1
    total = sum(share for _build, share in specs)
    widths = []
    left = usable - gap * (len(specs) - 1)
    for n, (_build, share) in enumerate(specs):
        widths.append(left - sum(widths) if n == len(specs) - 1
                      else round(left * share / total))
    built = [build(width) for (build, _share), width in zip(specs, widths, strict=True)]
    tallest = height or max(len(parts[2]) for parts in built) + 2
    rows: list[list] = []
    hits = []
    x = 0
    for n, (parts, width) in enumerate(zip(built, widths, strict=True)):
        title, note, body = parts[:3]
        panel = _live_panel(title, note, body[:tallest - 2], width, tallest)
        panel = _day_reveal(panel, _day_due(motion, start + n * 0.08, _DAY_PANEL_SECONDS),
                            width)
        _day_put(rows, panel, 0, x)
        if len(parts) > 3:
            hits += [(1 + line, x, x + width, index) for index, line in enumerate(parts[3])
                     if line < tallest - 2]
        x += width + gap
    return rows, hits


def _day_overview(data: dict, usable: int, room: int | None, motion: dict
                  ) -> tuple[list[list], list]:
    """The day at a glance, fitted to `room` rows when it is on a screen."""
    body: list[list] = [[]]
    body += _day_story(data, usable, motion)
    body.append([])
    if not data["sessions"]:
        return body, []
    cards = _day_cards_of(data, motion)
    gap = 1
    wide = usable >= 110
    top = len(body)
    if wide:
        hero_w = max(46, usable * 5 // 12)
        right = usable - hero_w - gap
        per = 3 if right >= 3 * 20 + 2 * gap else 2
        lines = -(-len(cards) // per)
        hero = _day_hero(data, hero_w, 5 * lines, motion)
        _day_put(body, _day_reveal(hero, _day_due(motion, 0.15, _DAY_PANEL_SECONDS), hero_w),
                 top, 0)
        x0 = hero_w + gap
    else:
        compact = room is not None and room < 40
        hero = _day_hero(data, usable, 9 if not compact else None, motion)
        _day_put(body, _day_reveal(hero, _day_due(motion, 0.15, _DAY_PANEL_SECONDS),
                                   usable), top, 0)
        top = len(body)
        per = 3 if usable >= 3 * 20 + 2 * gap else 2
        right, x0 = usable, 0
    each = (right - gap * (per - 1)) // per
    for n, card in enumerate(cards):
        line, col = divmod(n, per)
        width = each if col < per - 1 else right - (each + gap) * (per - 1)
        panel = _day_card(card, width, motion, n)
        panel = _day_reveal(panel, _day_due(motion, 0.25 + n * 0.07, _DAY_PANEL_SECONDS),
                            width)
        _day_put(body, panel, top + line * 5, x0 + col * (each + gap))
    body.append([])
    # The panels below get the rows they need, up to a point; the chart gets
    # what is left, so the tab fills the screen without running off it.
    limit = 5
    if room is not None:
        limit = min(max(room - len(body) - 4 - 6 - 2, 3), 8)
    if usable >= 110:
        specs = [(lambda w: _day_top(data, w, motion, limit), 4),
                 (lambda w: _day_models_mini(data, w, motion, limit), 3),
                 (lambda w: _day_repos(data, w, motion, limit), 3)]
    elif usable >= 70:
        specs = [(lambda w: _day_top(data, w, motion, limit), 1),
                 (lambda w: _day_models_mini(data, w, motion, limit), 1)]
    else:
        specs = [(lambda w: _day_top(data, w, motion, limit), 1)]
    bottom, hits = _day_row_of(specs, usable, motion, 1.05)
    chart_rows = 7
    if room is not None:
        chart_rows = min(max(room - len(body) - len(bottom) - 1 - 3, 4), 24)
    title, note, chart = _day_curve(data, usable, chart_rows, motion)
    panel = _live_panel(title, note, chart, usable)
    panel = _day_reveal(panel, _day_due(motion, 0.55, _DAY_PANEL_SECONDS), usable,
                        contents=False)
    _day_put(body, panel, len(body), 0)
    body.append([])
    start = len(body)
    _day_put(body, bottom, start, 0)
    return body, [(start + y, left, right_x, index) for y, left, right_x, index in hits]


# ── Sessions ─────────────────────────────────────────────────────────

def _day_sessions_tab(data: dict, usable: int, room: int | None, motion: dict
                      ) -> tuple[list[list], list]:
    """Every session of the day on one timeline, with what it asked and spent.

    On a screen the list scrolls inside its panel, so the hours stay put.
    """
    inner = usable - 4
    sessions = data["by_session"]
    rows: list[list] = [[]]
    if not sessions:
        rows.append(_live_line([("No session did anything on this day.", "help")],
                               usable - 1, 1))
        return rows, []
    cols: list[tuple[str, int]] = [("aiu", 7)]
    if inner >= 56:
        cols.append(("track", 0))
    if inner >= 64:
        cols.insert(0, ("asks", 5))
    if inner >= 90:
        cols.insert(0, ("repo", 16))
    if inner >= 112:
        cols.append(("span", 11))
    fixed = sum(width + 2 for key, width in cols if key != "track")
    track = 0
    if any(key == "track" for key, _w in cols):
        track = min(max(inner - 2 - 24 - fixed - 2, 24), 96)
    title_w = max(inner - 2 - fixed - (track + 2 if track else 0), 8)
    head: list = [(2, "SESSION", "label")]
    x = 2 + title_w + 2
    places = {}
    for key, width in cols:
        width = track if key == "track" else width
        places[key] = (x, width)
        x += width + 2
    for key, (x, width) in places.items():
        if key == "track":
            hours = len(data["hours"])
            for hour in range(0, hours, 6 if width < 48 else 3):
                at = x + round(hour * width / hours)
                if at + 2 <= x + width:
                    head.append((at, data["hours"][hour][:2], "label"))
        else:
            text = {"aiu": "AIU", "asks": "ASKS", "repo": "REPOSITORY",
                    "span": "ACTIVE"}[key]
            align = ">" if key in ("aiu", "asks") else "<"
            head.append((x + (width - len(text) if align == ">" else 0), text, "label"))
    body: list[list] = [head]
    chips = max(motion.get("chips", 8), 1)
    count = len(data["curve"]["today"])
    peak = 0
    if track:
        for s in sessions:
            for col in range(track):
                low = col * count // track
                high = max((col + 1) * count // track, low + 1)
                peak = max(peak, sum(s["slots"][low:high]))
    now_cell = (int(data["now_slot"] * track / count)
                if track and data["now_slot"] is not None else None)
    grow = _day_ease(motion, 0.3, 0.9)
    cursor = motion.get("cursor")
    moved = motion.get("moved")
    lines = []
    for index, s in enumerate(sessions):
        mark = _day_mark(s, motion)
        row: list = [(0, *mark), (2, ui.trunc(s["title"], title_w),
                                  "title" if s["live"] else "summary")]
        for key, (x, width) in places.items():
            if key == "repo":
                repo = (s["repository"] or "—").rsplit("/", 1)[-1]
                row.append((x, ui.trunc(repo, width), "repo"))
            elif key == "asks":
                row.append((x, f"{s['asks']:>{width}}", "number"))
            elif key == "aiu":
                row.append((x, f"{ui.fmt_aiu(s['nano_aiu']):>{width}}", "credits"))
            elif key == "track":
                # The timelines draw across from midnight as the tab opens.
                shown = max(1, round(width * grow))
                for sx, text, role in _day_track(s["slots"], s["asked"], width, peak,
                                                 data["now_slot"], f"c{index % chips}",
                                                 now_cell):
                    if sx < shown:
                        row.append((x + sx, text[:shown - sx], role))
            elif key == "span":
                span = f"{s['first']}–{s['last']}" if s["first"] else ""
                row.append((x, span, "help"))
        row = [seg for seg in row if seg[0] + ui.cells(seg[1]) <= inner]
        if index == cursor:
            sweep = 1.0 if moved is None else min(moved / ui.SWEEP_SECONDS, 1.0)
            row = _live_lit(row, round(inner * (1 - (1 - sweep) ** 2)))
        lines.append(len(body))
        body.append(row)
    legend = [("● ", "active"), ("running   ", "help"), ("+ ", "active"),
              ("started today   ", "help")]
    if track:
        legend += [("░▒▓█ ", "c0"), (f"spend in each {_DAY_CURVE_MINUTES} minutes   ", "help")]
        if now_cell is not None:
            legend += [("│ ", "title"), ("now", "help")]
    # On a screen the rows scroll inside the panel, keeping the cursor in view.
    view = len(body) - 1
    scroll = 0
    if room is not None:
        view = max(room - 1 - 2 - 2, 3)
        if cursor is not None:
            scroll = max(0, min(cursor - view + 1, len(sessions) - view))
            scroll = max(min(scroll, cursor), 0)
    listed = body[1:]
    shown_rows = [body[0], *listed[scroll:scroll + view], [],
                  _live_line(_day_fit([legend, legend[:4]], inner), inner)]
    live = sum(1 for s in sessions if s["live"])
    note = [(f"{len(sessions)} today", "help")]
    if live:
        note += [(" · ", "separator"), ("●", motion.get("pulse", "active")),
                 (f" {live} running", "active")]
    if scroll:
        note.insert(0, ("↑ ", "help"))
    if scroll + view < len(sessions):
        note.append((" ↓", "help"))
    panel = _live_panel([("Sessions", "header")], note, shown_rows, usable)
    panel = _day_reveal(panel, _day_due(motion, 0.1, 0.6), usable, contents=False)
    _day_put(rows, panel, 1, 0)
    hits = [(1 + 1 + 1 + n, 0, usable, index)
            for n, index in enumerate(range(scroll, min(scroll + view, len(sessions))))]
    return rows, hits


# ── Breakdown ────────────────────────────────────────────────────────

def _day_models_table(data: dict, width: int, motion: dict) -> tuple[list, list, list]:
    """Every model: what it cost, how long it ran, and so what a minute of
    it cost — then the same split drawn twice, by spend and by time."""
    inner = width - 4
    models = data["models"]
    title = [("Models", "header")]
    if not models:
        return title, [], [_live_line([("No model calls.", "help")], inner)]
    chips = max(motion.get("chips", 8), 1)
    cols = [("aiu", "AIU", 8), ("time", "TIME", 8)]
    if inner >= 52:
        cols.append(("rate", "AIU/MIN", 8))
    if inner >= 62:
        cols.insert(1, ("share", "AIU %", 6))
    if inner >= 70:
        cols.insert(3, ("tshare", "TIME %", 7))
    if inner >= 82:
        cols.append(("calls", "CALLS", 7))
    if inner >= 96:
        cols.append(("avg", "EACH", 7))
    if inner >= 108:
        cols.append(("ttft", "1ST TOKEN", 10))
    figures = sum(width + 1 for _k, _l, width in cols)
    name_w = min(max(ui.cells(m["model"]) for m in models[:_DAY_MODELS]) + 1, 24)
    bar_w = inner - 2 - name_w - figures - 1
    if bar_w < 6:
        bar_w = 0
        name_w = max(inner - 2 - figures - 1, 8)
    head: list = [(2, "MODEL", "label")]
    if bar_w:
        head.append((2 + name_w, "SPEND", "label"))
    x = inner - figures
    for _key, label, width in cols:
        x += 1
        head.append((x + width - len(label), label, "label"))
        x += width
    body: list[list] = [head]

    def cells_for(m: dict | None) -> dict[str, str]:
        source = m or {"nano_aiu": data["spend"]["nano_aiu"] or 0, "share": 1.0,
                       "time_share": 1.0 if data["model_ms"] else 0.0,
                       "time_ms": data["model_ms"], "aiu_per_minute": data["aiu_per_minute"],
                       "calls": data["calls"],
                       "average_ms": data["model_ms"] / data["calls"] if data["calls"] else None,
                       "first_token_ms": data["first_token_ms"]["p50"]}
        return {"aiu": _day_aiu(source["nano_aiu"]), "share": _day_pct(source["share"]),
                "tshare": _day_pct(source["time_share"]) if source["time_ms"] else "—",
                "time": _day_ms(source["time_ms"]) if source["time_ms"] else "—",
                "rate": _day_rate(source["aiu_per_minute"]),
                "calls": f"{source['calls']:,}",
                "avg": _day_ms(source["average_ms"]),
                "ttft": _day_ms(source["first_token_ms"])}

    roles = {"aiu": "credits", "time": "number", "rate": "warn", "share": "number",
             "tshare": "number", "calls": "number", "avg": "help", "ttft": "help"}
    for n, m in enumerate(models[:_DAY_MODELS] + [None]):
        chip = f"c{n % chips}"
        values = cells_for(m)
        row: list = []
        if m is None:
            row.append((2, ui.trunc("all models", name_w), "label"))
        else:
            row += [(0, "▌", chip), (2, ui.trunc(m["model"], name_w), "summary")]
            if bar_w:
                row.append((2 + name_w, _day_bar(m["share"], bar_w), chip))
        x = inner - figures
        for key, _label, width in cols:
            x += 1
            row.append((x, f"{values[key]:>{width}}", "label" if m is None else roles[key]))
            x += width
        if m is None:
            body.append([])
        body.append(row)
    # Spend and time as two stacked bars: a model dearer than its time is
    # the one to look at.
    if len(models) > 1 and inner >= 30:
        body.append([])
        for label, key in (("spend", "share"), ("time", "time_share")):
            bar_x = 8
            room = inner - bar_x
            row = [(2, label, "help")]
            x = 0
            shown = models[:_DAY_MODELS]
            for n, m in enumerate(shown):
                cells = (room - x if n == len(shown) - 1
                         else min(round(m[key] * room), room - x))
                if cells > 0:
                    row.append((bar_x + x, "█" * cells, f"c{n % chips}"))
                    x += cells
            body.append(row)
        # What each colour is, and its two shares side by side.
        legend: list[tuple[str, str]] = []
        used = 2
        for n, m in enumerate(models[:_DAY_MODELS]):
            entry = [("▌", f"c{n % chips}"), (f"{m['model']} ", "summary"),
                     (_day_pct(m["share"]), "credits"), (" of spend · ", "help"),
                     (_day_pct(m["time_share"]), "number"), (" of time", "help")]
            width = sum(ui.cells(text) for text, _ in entry) + 3
            if used + width > inner:
                break
            legend += [("   ", "")] + entry if legend else entry
            used += width
        if legend:
            body.append(_live_line([(t, r or "help") for t, r in legend], inner - 2, 2))
    rate = data.get("aiu_per_minute")
    note = [(_day_ms(data["model_ms"]), "number"), (" of model time", "help")]
    if rate is not None:
        note += [(" · ", "separator"), (_day_rate(rate), "warn"), (" AIU/min", "help")]
    return title, note, body


def _day_running(data: dict, width: int, motion: dict) -> tuple[list, list, list]:
    """How the day's calls ran: cache, reasoning, latency, tokens, model time."""
    inner = width - 4
    tokens = data["tokens"]
    wide = inner >= 34
    label_w = 12 if wide else 9
    body: list[list] = []
    shorter = {"cache hit": "cache", "reasoning": "reason", "first token": "1st tok",
               "model time": "model", "tokens in": "in", "tokens out": "out"}

    def named(label: str) -> tuple[int, str, str]:
        return 0, label if wide else shorter.get(label, label), "label"

    def meter(label: str, share: float | None, role: str, tail: str) -> None:
        if share is None:
            return
        tail_w = ui.cells(tail) + 1
        cells = inner - label_w - tail_w
        row = [named(label)]
        if cells >= 4:
            row.append((label_w, _day_bar(share, cells), role))
            row += _live_right([(tail, "number")], inner)
        else:
            row += _live_line([(tail, "number")], inner - label_w, label_w)
        body.append(row)

    def line(label: str, *options: list[tuple[str, str]]) -> None:
        body.append([named(label),
                     *_live_line(_day_fit(list(options), inner - label_w),
                                 inner - label_w, label_w)])

    meter("cache hit", tokens["cache_hit"], "active", _day_pct(tokens["cache_hit"]))
    meter("reasoning", tokens["reasoning_share"], "turns",
          _day_pct(tokens["reasoning_share"]))
    latency = data["first_token_ms"]
    if latency["p50"] is not None:
        line("first token", [("p50 ", "help"), (_day_ms(latency["p50"]), "number"),
                             ("  p95 ", "help"), (_day_ms(latency["p95"]), "number")])
    if tokens["input"] or tokens["output"] or tokens["cache_read"]:
        line("tokens in",
             [(_live_short(tokens["input"]), "number"), (" new · ", "help"),
              (_live_short(tokens["cache_read"]), "number"), (" from cache", "help")],
             [(_live_short(tokens["input"]), "number"), (" new · ", "help"),
              (_live_short(tokens["cache_read"]), "number"), (" cached", "help")],
             [(_live_short(tokens["input"]), "number"), (" new", "help")])
        line("tokens out", [(_live_short(tokens["output"]), "number")])
    if data["model_ms"]:
        each = data["model_ms"] / data["calls"] if data["calls"] else None
        line("model time",
             [(_day_ms(data["model_ms"]), "number"),
              (f"  · {_day_ms(each)} a call" if each else "", "help")],
             [(_day_ms(data["model_ms"]), "number")])
    if data["files"] is not None:
        line("files", [(f"{data['files']['created']}", "number"), (" created · ", "help"),
                       (f"{data['files']['edited']}", "number"), (" edited", "help")])
    if data["active_minutes"]:
        line("active",
             [(_live_span(data["active_minutes"] * 60), "number"),
              (f"  {data['first']} → {data['last']}", "help")],
             [(_live_span(data["active_minutes"] * 60), "number")])
    if not body:
        body = [_live_line([("No token counts on this store.", "help")], inner)]
    return [("How it ran", "header")], [], body


def _day_split(data: dict, width: int, motion: dict) -> tuple[list, list, list]:
    """Who started the billed calls: you, the agent, sub-agents, compaction."""
    inner = width - 4
    split = data["split"] or []
    total = sum(part["nano_aiu"] for part in split)
    calls = sum(part["calls"] for part in split)
    roles = {shown: role for _kind, shown, role in _DAY_WHO}
    body: list[list] = []
    if not split:
        return ([("Who did the work", "header")], [],
                [_live_line([("Not recorded on this store." if data["split"] is None
                              else "No calls to split.", "help")], inner)])
    # One bar, cut by share of spend — or of calls, where nothing was billed.
    weights = [part["nano_aiu"] if total else part["calls"] for part in split]
    whole = sum(weights) or 1
    stacked: list = []
    x = 0
    for n, (part, weight) in enumerate(zip(split, weights, strict=True)):
        cells = (inner - x if n == len(split) - 1
                 else min(round(weight / whole * inner), inner - x))
        if cells > 0:
            stacked.append((x, "█" * cells, roles[part["who"]]))
            x += cells
    body.append(stacked)
    for part, weight in zip(split, weights, strict=True):
        figures = [(_day_pct(weight / whole).rjust(5), "number")]
        if inner >= 34:
            figures.append(((_day_aiu(part["nano_aiu"]) if total else "").rjust(7), "credits"))
        if inner >= 46:
            figures.append((f"{part['calls']:>7,} calls", "help"))
        body.append([(0, "■", roles[part["who"]]), (2, part["who"], "summary"),
                     *_live_right(figures, inner)])
    tools = data["tools"]
    if tools and tools["subagents"]:
        body.append(_live_line([("» ", "turns"), (f"{tools['subagents']}", "number"),
                                (" sub-agent runs finished", "help")], inner))
    return ([("Who did the work", "header")],
            [(f"{calls:,} calls", "help")], body)


def _day_breakdown_tab(data: dict, usable: int, room: int | None, motion: dict
                       ) -> tuple[list[list], list]:
    rows: list[list] = [[]]
    if not data["sessions"]:
        rows.append(_live_line([("Nothing to break down on this day.", "help")],
                               usable - 1, 1))
        return rows, []
    title, note, body = _day_models_table(data, usable, motion)
    panel = _live_panel(title, note, body, usable)
    _day_put(rows, _day_reveal(panel, _day_due(motion, 0.1, _DAY_PANEL_SECONDS), usable),
             len(rows), 0)
    rows.append([])
    if usable >= 90:
        pair, _hits = _day_row_of([(lambda w: _day_running(data, w, motion), 1),
                                   (lambda w: _day_split(data, w, motion), 1)],
                                  usable, motion, 0.3)
        _day_put(rows, pair, len(rows), 0)
    else:
        for n, build in enumerate((_day_running, _day_split)):
            t, nt, b = build(data, usable, motion)
            panel = _live_panel(t, nt, b, usable)
            _day_put(rows, _day_reveal(panel, _day_due(motion, 0.3 + n * 0.08,
                                                       _DAY_PANEL_SECONDS), usable),
                     len(rows), 0)
            rows.append([])
    rows.append([])
    limit = _DAY_REPOS
    if room is not None:
        limit = min(max(room - len(rows) - 2 - 3, 2), _DAY_REPOS)
    t, nt, b = _day_repos(data, usable, motion, limit)
    panel = _live_panel(t, nt, b, usable)
    _day_put(rows, _day_reveal(panel, _day_due(motion, 0.5, _DAY_PANEL_SECONDS), usable),
             len(rows), 0)
    if room is None or len(rows) + 3 <= room:
        foot = ("Every figure is cut to this day by its own time. Active time counts "
                f"{_DAY_SLOT_MINUTES}-minute slots that held an ask or a model call. "
                "AIU/min is spend over the time the model spent answering.")
        if data["live"]:
            foot += " Spend is compared with yesterday up to the same time."
        rows.append([])
        rows += _day_words([(foot, "separator")], usable - 2)
    return rows, []


# ── Activity ─────────────────────────────────────────────────────────

def _day_chart(data: dict, width: int, motion: dict, tall: bool) -> tuple[list, list, list]:
    """Spend by local hour as columns in the gradient, asks beneath them."""
    inner = width - 4
    hours = len(data["by_hour"])
    spend = [h["nano_aiu"] for h in data["by_hour"]]
    asks = [h["asks"] for h in data["by_hour"]]
    gutter = 6 if inner >= 60 else 0
    per = max(1, (inner - gutter) // hours)
    bar = per - 1 if per >= 3 else 1
    rows = 5 if tall else 3
    grow = _day_ease(motion, 0.2, 0.8)
    sweep = _day_ease(motion, 0.15, 0.6)
    shown = hours if sweep >= 1 else int(hours * sweep)
    peak = max(spend, default=0)
    now_hour = data["now_hour"]
    grad = max(motion.get("grad", 1), 1)
    levels = rows * 8
    heights = [max(1, round(v * grow / peak * levels)) if v > 0 and peak else 0
               for v in spend]
    body: list[list] = []
    for level in range(rows - 1, -1, -1):
        row: list = []
        if gutter and level == 0:
            row.append((0, "AIU", "help"))
        for hour in range(min(shown, hours)):
            part = heights[hour] - level * 8
            future = now_hour is not None and hour > now_hour
            if part >= 8:
                glyph = "█"
            elif part > 0:
                glyph = _DAY_SPARKS[part]
            elif level == 0:
                glyph = "·" if future else "▁"
            else:
                continue
            role = ("separator" if part <= 0 else
                    motion.get("pulse", "title") if hour == now_hour and data["live"]
                    else f"g{hour * grad // hours}")
            row.append((gutter + hour * per, glyph * bar, role))
        body.append(row)
    top = max(asks, default=0)
    strip: list = [(0, "asks", "help")] if gutter else []
    for hour in range(min(shown, hours)):
        if asks[hour] and top:
            glyph = _DAY_SPARKS[max(1, round(asks[hour] * grow / top * 8))]
            strip.append((gutter + hour * per, glyph * bar, "turns"))
    if top:
        body.append(strip)
    step = next(n for n in (1, 2, 3, 4, 6, 12, 24) if n * per >= 3)
    axis: list = []
    for hour in range(0, hours, step):
        x = gutter + hour * per
        if x + 2 > inner:
            break
        axis.append((x, data["hours"][hour][:2], "title" if hour == now_hour else "help"))
    body.append(axis)
    note: list[tuple[str, str]] = []
    if data["peak"]:
        note = [("peak ", "help"), (data["peak"]["hour"], "number")]
        if width >= 60:
            note.append((f" · {_day_aiu(data['peak']['nano_aiu'])} AIU", "credits"))
    if not peak and not top:
        body = [_live_line([("No billed calls or asks on this day.", "help")], inner)]
    return [("Spend by hour", "header")], note, body


def _day_shipped(data: dict, width: int, motion: dict, limit: int = _DAY_SHIPPED
                 ) -> tuple[list, list, list]:
    inner = width - 4
    shipped = data["shipped"]
    if shipped is None:
        return ([("Shipped", "header")], [],
                [_live_line([("Commits and PRs carry no time on this store.", "help")],
                            inner)])
    items = shipped["items"]
    if not items:
        return ([("Shipped", "header")], [],
                [_live_line([("Nothing shipped yet." if data["live"]
                              else "Nothing shipped.", "help")], inner)])
    body = []
    for item in items[:limit]:
        pr = item["kind"] == "pr"
        mark = ("PR " if pr else "◆ ", "turns" if pr else "active")
        value = f"#{item['value'].lstrip('#')}" if pr else item["value"][:12]
        room = inner - ui.cells(mark[0]) - ui.cells(value) - 2
        body.append(_live_line([mark, (value, "number"), ("  ", "separator"),
                                (ui.trunc(item["title"], room), "help")], inner))
    if len(items) > limit:
        body.append(_live_line([(f"+{len(items) - limit} more", "help")], inner))
    note = f"{shipped['commits']} commits · {shipped['prs']} PRs"
    return [("Shipped", "header")], [(note, "help")], body


def _day_tool_panel(data: dict, width: int, motion: dict, limit: int = _DAY_TOOLS
                    ) -> tuple[list, list, list]:
    inner = width - 4
    tools = data["tools"]
    title = [("Tools", "header")]
    if tools is None:
        text = ("reading the event logs…"
                if data.get("tools_pending") and data.get("tools_expected")
                else "No event logs for these sessions.")
        return title, [], [_live_line([(text, "help")], inner)]
    if not tools["calls"]:
        return title, [], [_live_line([("No tool calls.", "help")], inner)]
    peak = max(t["calls"] for t in tools["by_tool"])
    items = []
    for tool in tools["by_tool"][:limit]:
        figures = [(f"{tool['calls']:>6,}", "number")]
        figures.append((f" ✗{tool['failures']:<3}" if tool["failures"] else "     ",
                        "danger" if tool["failures"] else "separator"))
        items.append((tool["tool"], tool["calls"] / peak, "turns", figures,
                      "danger" if tool["failures"] else "separator", tool["tool"]))
    body = _day_rank_rows(items, inner)
    if tools["skills"]:
        # Whole names only, then how many did not fit.
        parts: list[tuple[str, str]] = [("skills ", "label")]
        used = ui.cells("skills ")
        skills = tools["skills"]
        for n, skill in enumerate(skills):
            text = f"{skill['skill']} ×{skill['count']}"
            gap = " · " if n else ""
            more = f" +{len(skills) - n - 1}" if n < len(skills) - 1 else ""
            if used + ui.cells(gap + text + more) > inner:
                parts.append((f" +{len(skills) - n}" if n else
                              ui.trunc(text, inner - used), "help"))
                break
            if gap:
                parts.append((gap, "separator"))
            parts.append((text, "summary"))
            used += ui.cells(gap + text)
        body.append(_live_line(parts, inner))
    note = [(f"{tools['calls']:,} calls", "help")]
    if tools["failures"]:
        note = [(f"{tools['failures']} failed", "danger"), (" · ", "separator"), *note]
    return title, note, body


def _day_feed_panel(data: dict, width: int, rows: int, motion: dict
                    ) -> tuple[list, list, list]:
    """What the running sessions are doing, newest first, each line lit as it lands."""
    inner = width - 4
    feed = data.get("feed") or []
    title = [("Live activity", "header")]
    if not feed:
        text = ("Waiting for the next event…" if data.get("running") else
                "No Copilot CLI is running — what one does appears here as it happens.")
        return title, [], [_live_line([(text, "help")], inner)]
    note = [("●", motion.get("pulse", "active")), (f" {data['running']} running", "help")]
    body = _live_feed(feed, inner, rows, {"arrivals": motion.get("arrivals", {}),
                                          "chips": motion.get("chips", 8)})
    return title, note, body


def _day_activity_tab(data: dict, usable: int, room: int | None, motion: dict
                      ) -> tuple[list[list], list]:
    rows: list[list] = [[]]
    if not data["sessions"] and not data.get("feed"):
        rows.append(_live_line([("Nothing happened on this day.", "help")], usable - 1, 1))
        return rows, []
    tall = room is None or room >= 34
    title, note, body = _day_chart(data, usable, motion, tall)
    panel = _live_panel(title, note, body, usable)
    _day_put(rows, _day_reveal(panel, _day_due(motion, 0.1, _DAY_PANEL_SECONDS), usable,
                               contents=False), len(rows), 0)
    rows.append([])
    limit = _DAY_TOOLS
    if usable >= 80:
        pair, _hits = _day_row_of([(lambda w: _day_tool_panel(data, w, motion, limit), 1),
                                   (lambda w: _day_shipped(data, w, motion, limit), 1)],
                                  usable, motion, 0.3)
        _day_put(rows, pair, len(rows), 0)
    else:
        for n, build in enumerate((_day_tool_panel, _day_shipped)):
            t, nt, b = build(data, usable, motion)
            panel = _live_panel(t, nt, b, usable)
            _day_put(rows, _day_reveal(panel, _day_due(motion, 0.3 + n * 0.08,
                                                       _DAY_PANEL_SECONDS), usable),
                     len(rows), 0)
            rows.append([])
    if data["live"]:
        rows.append([])
        lines = 8 if room is None else max(room - len(rows) - 2, 3)
        lines = min(lines, _DAY_FEED)
        t, nt, b = _day_feed_panel(data, usable, lines, motion)
        panel = _live_panel(t, nt, b, usable, None if room is None else
                            max(min(lines, len(b)) + 2, 3))
        _day_put(rows, _day_reveal(panel, _day_due(motion, 0.5, _DAY_PANEL_SECONDS), usable),
                 len(rows), 0)
    return rows, []


# ── The page ─────────────────────────────────────────────────────────

def _day_body(data: dict, usable: int, room: int | None, motion: dict, tab: int
              ) -> tuple[list[list], list]:
    key = _DAY_TABS[tab][0]
    if key == "sessions":
        return _day_sessions_tab(data, usable, room, motion)
    if key == "breakdown":
        return _day_breakdown_tab(data, usable, room, motion)
    if key == "activity":
        return _day_activity_tab(data, usable, room, motion)
    return _day_overview(data, usable, room, motion)


def _day_screen(data: dict, width: int, height: int | None = None,
                motion: dict | None = None, tab: int = 0
                ) -> tuple[list, list[list], list, list[tuple[int, int, int, int]]]:
    """One tab of the page: the pinned rows above, the body, the ticker below,
    and where each session landed in the body (y, left, right, index).

    `height` None is the printed form, which carries every tab in turn.
    """
    motion = motion or {}
    usable = width - 1
    if height is None:
        head = _day_head(data, usable, motion, None)
        body, hits = _day_overview(data, usable, None, motion)
        for n, (_key, name) in enumerate(_DAY_TABS[1:], 1):
            if not data["sessions"]:
                break
            heading = f"── {name} "
            body += [[], [(1, heading, "header"),
                          (1 + ui.cells(heading), "─" * max(usable - 2 - ui.cells(heading), 0),
                           "separator")]]
            more, _more_hits = _day_body(data, usable, None, motion, n)
            body += more
        return head, body, [], hits
    head = _day_head(data, usable, motion, tab)
    room = max(height - len(head) - 2, 1)
    body, hits = _day_body(data, usable, room, motion, tab)
    foot = _day_ticker(data, usable, motion)
    return head, body, foot, hits


_DAY_HINTS = (" ⇥ tabs · ←→ day · ↑↓ session · ↵ open · r refresh · q back ",
              " ⇥ tabs · ←→ day · ↵ open · q back ",
              " ⇥ · ←→ · ↵ · q ", " q ")


def _day_hint(width: int) -> str:
    for hint in _DAY_HINTS:
        if ui.cells(hint) <= width - 1:
            return hint
    return ""


def _day_status(width: int, stamps: list[str]) -> tuple[str, str]:
    """The key hint and the stamp beside it: the longest pair that fits,
    giving up hint words before the scroll position."""
    for stamp in stamps:
        for hint in _DAY_HINTS:
            if ui.cells(hint) + ui.cells(stamp) + 2 <= width - 1:
                return hint, stamp
    return _day_hint(width), ""


def _day_ink(role: str) -> str:
    """The escape a role is printed in, on a terminal: the report palette,
    and the theme's ramp for the gradient (g…) and session colours (c…)."""
    ramp = ui._BAR_RAMP
    if role[:1] in ("g", "c") and role[1:].isdigit():
        step = int(role[1:])
        if role[0] == "c":
            step = (0, 4, 2, 6, 1, 5, 3, 7)[step % 8] * (len(ramp) - 1) // 7
        return ui.c256(ramp[min(step, len(ramp) - 1)])
    return {
        "title": ui.BOLD + ui.ACCENT, "header": ui.BOLD + ui.ACCENT,
        "help": ui.MUTED, "separator": ui.SLATE, "label": ui.BOLD,
        "credits": ui.BOLD + ui.VIOLET, "active": ui.MINT, "turns": ui.TEAL,
        "repo": ui.AZURE, "warn": ui.AMBER, "danger": ui.ROSE,
    }.get(role, "")


def _day_text(width: int, offset: int = 0) -> str:
    """The page as text, for a pipe or a terminal with no curses — every tab
    in turn, coloured only when it is going to a terminal."""
    data = _day_data(offset)
    motion = {"grad": len(ui._BAR_RAMP), "chips": 8,
              "clock": f"as of {time.strftime('%H:%M')}"}
    head, body, _foot, _hits = _day_screen(data, width, None, motion)
    out = []
    for line in [*head, *body]:
        text = ""
        used = 0
        for x, part, role in sorted(line, key=lambda seg: seg[0]):
            if x < used:
                continue
            ink = _day_ink(role) if ui._COLOR else ""
            text += " " * (x - used) + (f"{ink}{part}{ui.RST}" if ink else part)
            used = x + ui.cells(part)
        out.append(text.rstrip())
    return "\n".join(out).rstrip() + "\n"


# ── Interactive ──────────────────────────────────────────────────────

def _day_read(state: dict, tools: bool = True, fatal: bool = False) -> None:
    """A look at the day the page is on, now and in this thread. A store
    that cannot be read keeps what is already on screen."""
    # A background reread may be reading the same logs; let it finish first.
    worker = state.get("worker")
    if worker is not None and worker.is_alive():
        worker.join(10)
    offset = state.get("offset", 0)
    if state.get("memo_offset") != offset:
        state["memo"] = {}
        state["memo_offset"] = offset
    try:
        data = _day_data(offset, state["memo"], tools=tools, fatal=fatal,
                         tails=state.setdefault("tails", {}))
    except (OSError, sqlite3.Error):
        if "data" not in state:
            raise
        return
    data["tools_pending"] = not tools
    if not tools and state.get("data") and state["data"]["offset"] == offset:
        data["tools"] = state["data"].get("tools")
        data["tools_pending"] = data["tools"] is None
    state["data"] = data
    state["read_at"] = time.monotonic()
    state["read_clock"] = time.strftime("%H:%M:%S")


def _day_refresh(state: dict) -> None:
    """Start a reread in the background, unless one is already running.

    The result lands in `state["box"]` for the page to pick up between
    frames. `state["threaded"] = False` reads in place, which the tests use.
    """
    worker = state.get("worker")
    if worker is not None and worker.is_alive():
        return
    offset = state.get("offset", 0)
    if state.get("memo_offset") != offset:
        state["memo"] = {}
        state["memo_offset"] = offset
    memo, tails = state["memo"], state.setdefault("tails", {})
    box: dict = {"offset": offset}

    def work() -> None:
        try:
            box["data"] = _day_data(offset, memo, tools=True, fatal=False, tails=tails)
        except Exception as error:  # handed to the page, which decides
            box["error"] = error

    state["box"] = box
    state["read_at"] = time.monotonic()
    if state.get("threaded", True):
        worker = threading.Thread(target=work, daemon=True)
        state["worker"] = worker
        worker.start()
    else:
        work()


def _day_collect(state: dict) -> dict | None:
    """A finished background reread, or None. A store that could not be read
    is skipped; anything else that went wrong is raised here, on screen."""
    box = state.get("box")
    if not box or ("data" not in box and "error" not in box):
        return None
    del state["box"]
    if "error" in box:
        if isinstance(box["error"], (OSError, sqlite3.Error)):
            return None
        raise box["error"]
    if box["offset"] != state.get("offset", 0):
        return None
    return box["data"]


def _day_tui(screen, state: dict):
    """The page. Returns a session id to open, or None to go back."""
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
    if "data" not in state:
        _day_read(state, tools=False)
    opened = None if state.get("dealt") else time.monotonic()
    pace = 1.0
    # The store is read before the first frame and the logs after it, behind
    # the entrance, so the page is on screen before the slow part starts.
    painted = False
    rolls: dict[str, tuple[float, float, float]] = {}
    lit: dict[str, float] = {}
    deltas: dict[str, str] = {}
    arrivals: dict = {}
    tab = state.setdefault("tab", 0)
    tab_from, glide_at = tab, None
    slide_at, slide_dir = None, 0
    ticker_key, ticker_at = None, None
    moved_at = None
    try:
        while True:
            clock = time.monotonic()
            fresh = _day_collect(state)
            if fresh is not None:
                old = state.get("data")
                if old and old["offset"] == fresh["offset"]:
                    before, after = _day_values(old), _day_values(fresh)
                    for key, value in after.items():
                        prev = before.get(key)
                        if prev is not None and value is not None and prev != value:
                            rolls[key] = (prev, value, clock)
                            lit[key] = clock
                            step = value - prev
                            shown = (_day_aiu(abs(step)) if key == "spend" else
                                     _live_span(abs(step) * 60) if key == "active" else
                                     _day_format(key, abs(step)))
                            deltas[key] = f"{'▲' if step > 0 else '▼'}{shown}"
                    if old.get("feed") is not None and old["live"]:
                        known = {entry["key"] for entry in old.get("feed") or []}
                        for entry in fresh.get("feed") or []:
                            if entry["key"] not in known:
                                arrivals[entry["key"]] = clock
                fresh["tools_pending"] = False
                state["data"] = fresh
                state["read_clock"] = time.strftime("%H:%M:%S")
            data = state["data"]
            if painted and (data.get("tools_pending") or (
                    data["live"] and clock - state.get("read_at", 0) >= DAY_REFRESH_SECONDS)):
                _day_refresh(state)
            for table in (lit, arrivals):
                for key in [key for key, at in table.items()
                            if clock - at >= ui.FLASH_SECONDS]:
                    del table[key]
            rolling = {}
            for key, (old_value, new_value, at) in list(rolls.items()):
                share = (clock - at) / _DAY_ROLL_SECONDS
                if share >= 1 or not ui.MOTION:
                    del rolls[key]
                    continue
                ease = 1 - (1 - share) ** 3
                rolling[key] = old_value + (new_value - old_value) * ease
            feed = data.get("feed") or []
            newest = feed[0]["key"] if feed else None
            if newest != ticker_key:
                ticker_at = clock if ticker_key is not None else None
                ticker_key = newest
            sessions = data["by_session"]
            key_tab = _DAY_TABS[tab][0]
            listed = (min(len(sessions), 8) if key_tab == "overview" else len(sessions))
            cursor = state["cursor"] = min(max(state.get("cursor", 0), 0),
                                           max(listed - 1, 0))
            height, width = screen.getmaxyx()
            elapsed = None if opened is None else (clock - opened) * pace
            if elapsed is not None and elapsed >= _DAY_INTRO_SECONDS:
                opened = elapsed = None
                state["dealt"] = True
            glide = (1.0 if glide_at is None or not ui.MOTION
                     else min((clock - glide_at) / _DAY_GLIDE_SECONDS, 1.0))
            slide = (1.0 if slide_at is None or not ui.MOTION
                     else min((clock - slide_at) / _DAY_SLIDE_SECONDS, 1.0))
            ticker = (1.0 if ticker_at is None or not ui.MOTION
                      else min((clock - ticker_at) / _DAY_TICK_SECONDS, 1.0))
            moved = None if moved_at is None else clock - moved_at
            motion = {
                "open": elapsed,
                "roll": rolling,
                "flash": {key: clock - at for key, at in lit.items()},
                "delta": deltas,
                "arrivals": {key: clock - at for key, at in arrivals.items()},
                "cursor": cursor if sessions and key_tab in ("overview", "sessions") else None,
                "moved": moved,
                "spin": int(clock * 1000 / _DAY_SPIN_MS) if ui.MOTION else 0,
                "pulse": ui.pulse_role(clock) if ui.MOTION else "active",
                "clock": time.strftime("%H:%M:%S"),
                "glide": glide, "tab_from": tab_from,
                "ticker": ticker,
                "grad": grad, "chips": chips,
            }
            head, body, foot, hits = _day_screen(data, width, height, motion, tab)
            view = max(height - len(head) - 2, 1)
            most = max(len(body) - view, 0)
            scroll = state["scroll"] = min(max(state.get("scroll", 0), 0), most)
            offset_x = 0
            if slide < 1:
                offset_x = round(slide_dir * width * (1 - slide) ** 3)
            screen.erase()
            for y, line in enumerate(head):
                for x, text, role in line:
                    _addstr(screen, y, x, text, width, theme[role])
            top = len(head)
            for y, line in enumerate(body[scroll:scroll + view], top):
                for x, text, role in line:
                    x += offset_x
                    if x < 0:
                        text = text[-x:]
                        x = 0
                    if text and x < width - 1:
                        _addstr(screen, y, x, text, width, theme[role])
            for x, text, role in foot:
                _addstr(screen, height - 2, x, text, width, theme[role])
            where = ""
            if most:
                where = ("↓ more  " if scroll < most else "") + f"{round(100 * scroll / most)}% "
            stamps = ([f"{where}updated {state.get('read_clock', '')} · every "
                       f"{DAY_REFRESH_SECONDS}s "] if data["live"]
                      else [f"{where}{data['written']} "])
            hint, stamp = _day_status(width, [*stamps, where] if where else stamps)
            _addstr(screen, height - 1, 0, hint, width, theme["status"])
            if stamp:
                _addstr(screen, height - 1, width - 1 - ui.cells(stamp), stamp,
                        width, theme["help"])
            screen.refresh()
            painted = True
            moving = (opened is not None or rolls or lit or arrivals or glide < 1
                      or slide < 1 or ticker < 1
                      or (moved is not None and moved < ui.SWEEP_SECONDS))
            alive = data["live"] and (data.get("running") or data["live_sessions"])
            if moving and ui.MOTION:
                wait = ui.MOTION_MS
            elif alive and ui.MOTION:
                wait = _DAY_SPIN_MS
            else:
                wait = 1000
            if "box" in state or data.get("tools_pending"):
                wait = min(wait, 50)
            screen.timeout(wait)
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
                if kind in ("click", "double") and y == 1:
                    for n, (span_x, span_w) in enumerate(_day_tab_spans(width - 1)):
                        if span_x <= x < span_x + span_w and n != tab:
                            slide_dir = 1 if n > tab else -1
                            tab_from, tab = tab, n
                            glide_at = slide_at = time.monotonic()
                            state["tab"], state["scroll"] = tab, 0
                            opened, pace = time.monotonic(), _DAY_SWITCH_PACE
                elif kind in ("click", "double"):
                    for hit_y, left, right, index in hits:
                        if hit_y - scroll + top == y and left <= x < right:
                            if index != cursor:
                                moved_at = time.monotonic()
                            state["cursor"] = index
                            if kind == "double":
                                return sessions[index]["id"]
                elif kind == "wheel-up":
                    state["scroll"] = scroll - 3
                elif kind == "wheel-down":
                    state["scroll"] = scroll + 3
                continue
            if key in (27, ord("q"), ord("Q")):
                return None
            if key in (10, 13, curses.KEY_ENTER) and sessions and \
                    key_tab in ("overview", "sessions"):
                return sessions[cursor]["id"]
            chosen = None
            if key == 9:
                chosen = (tab + 1) % len(_DAY_TABS)
            elif key == getattr(curses, "KEY_BTAB", 353):
                chosen = (tab - 1) % len(_DAY_TABS)
            elif ord("1") <= key < ord("1") + len(_DAY_TABS):
                chosen = key - ord("1")
            if chosen is not None and chosen != tab:
                slide_dir = 1 if chosen > tab else -1
                tab_from, tab = tab, chosen
                glide_at = slide_at = time.monotonic()
                state["tab"], state["scroll"] = tab, 0
                opened, pace = time.monotonic(), _DAY_SWITCH_PACE
                continue
            step_day = {curses.KEY_LEFT: 1, ord("h"): 1, curses.KEY_RIGHT: -1,
                        ord("l"): -1}.get(key)
            if key in (ord("t"), ord("T")):
                step_day = -state.get("offset", 0)
            if step_day is not None:
                offset = min(max(state.get("offset", 0) + step_day, 0), _DAY_BACK)
                if offset != state.get("offset", 0):
                    state["offset"] = offset
                    state["cursor"] = state["scroll"] = 0
                    state.pop("box", None)
                    _day_read(state, tools=False)
                    rolls.clear()
                    lit.clear()
                    deltas.clear()
                    arrivals.clear()
                    opened, pace = time.monotonic(), _DAY_SWITCH_PACE
                    state["dealt"] = False
                continue
            if key in (ord("r"), ord("R")):
                state["read_at"] = 0.0
                if not data["live"]:
                    _day_refresh(state)
            elif key in (curses.KEY_UP, ord("k")):
                if key_tab in ("overview", "sessions") and sessions:
                    state["cursor"] = cursor - 1
                    moved_at = time.monotonic()
                else:
                    state["scroll"] = scroll - 1
            elif key in (curses.KEY_DOWN, ord("j")):
                if key_tab in ("overview", "sessions") and sessions:
                    state["cursor"] = cursor + 1
                    moved_at = time.monotonic()
                else:
                    state["scroll"] = scroll + 1
            elif key in (curses.KEY_NPAGE, ord(" "), ord("f")):
                state["scroll"] = scroll + view - 2
            elif key in (curses.KEY_PPAGE, ord("b")):
                state["scroll"] = scroll - view + 2
            elif key in (curses.KEY_HOME, ord("g")):
                state["scroll"] = 0
            elif key in (curses.KEY_END, ord("G")):
                state["scroll"] = most
    finally:
        if mouse:
            _disable_mouse()


def cmd_day(offset: int = 0) -> bool:
    """The whole day on one page: spend, sessions, repositories, models and
    more, since local midnight, kept current while it is today."""
    import curses
    import shutil

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.stdout.write(_day_text(min(shutil.get_terminal_size().columns, 140), offset))
        return False
    state: dict = {"offset": offset}
    # Read before curses starts, so a store that cannot be opened says so in
    # plain words rather than from inside a blanked screen.
    _day_read(state, tools=False, fatal=True)
    while True:
        try:
            chosen = _curses_wrapper(_day_tui, state)
        except KeyboardInterrupt:
            return True
        except curses.error:
            sys.stdout.write(_day_text(min(shutil.get_terminal_size().columns, 140),
                                       state.get("offset", offset)))
            return False
        if chosen is None:
            return True
        try:
            cmd_show(chosen)
        except SystemExit:
            pass
        # Back from a session: no entrance again, and today reread at once.
        state["dealt"] = True
        state["read_at"] = 0.0


def _day_export(offset: int = 0) -> dict:
    """The page's figures, for `cs day --json`."""
    data = _day_data(offset)
    data.pop("tools_pending", None)
    data.pop("tools_expected", None)
    for entry in data["feed"]:
        entry.pop("key", None)
        entry.pop("chip", None)
    return data
