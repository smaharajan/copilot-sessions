"""Today, whole: everything since midnight on one page that keeps current.

`cs live` is the sessions running now and `cs today` is where you are. This
is the day's ledger, drawn as one dashboard: spend against yesterday at the
same hour, sessions, asks, repositories, models, what shipped, files,
tokens, cache and latency, tool calls, and who did the work. Today rereads
itself every few seconds; ←/→ step back through earlier days, which hold
still.

Every figure is cut to the local day by its own timestamp, not by when its
session began: a session opened last night counts only what it did today.
A figure the store cannot time is left off the page rather than shown as a
zero, and the inferred ones — active time, the comparison with yesterday —
say what they were worked out from.
"""

from __future__ import annotations

import os
import sqlite3
import sys
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
    _live_line,
    _live_lit,
    _live_panel,
    _live_right,
    _live_short,
    _live_span,
    _live_theme,
)
from .session import cmd_show

# How often today rereads the store and the logs. Earlier days hold still.
DAY_REFRESH_SECONDS = 5
# How far back ← goes.
_DAY_BACK = 366
# Active time is the five-minute slots that held an ask or a model call.
_DAY_SLOT_MINUTES = 5
# Rows in each ranked panel before the rest are summed or counted.
_DAY_MODELS = 6
_DAY_REPOS = 6
_DAY_TOOLS = 6
_DAY_SHIPPED = 6
# The panels deal in one after another as the page opens; each takes this
# long to draw its border and grow its bars.
_DAY_DEAL_SECONDS = 0.07
_DAY_PANEL_SECONDS = 0.45
# Each tile's count starts a beat after the one before it.
_DAY_TILE_STAGGER = 0.05
# Who started each billed call, in the order the split draws them.
_DAY_WHO = (("user", "you", "title"), ("agent", "agent", "active"),
            ("sub-agent", "sub-agents", "turns"), ("compaction", "compaction", "warn"),
            ("", "other", "separator"))
# Roles whose text is a number that counts up as its panel deals in.
_DAY_COUNTED = frozenset({"number", "credits"})
_DAY_EIGHTHS = " ▏▎▍▌▋▊▉█"
_DAY_SPARKS = " ▁▂▃▄▅▆▇█"


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
              fatal: bool = True) -> dict:
    """The day's figures as plain, already-masked data — what `--json` returns
    and what the page draws.

    `memo` is kept by a page between rereads, so the event logs are read on
    from where the last look stopped. `tools=False` skips the logs entirely,
    which is what lets the first frame paint before they are read.
    """
    offset = max(0, min(int(offset), _DAY_BACK))
    now = datetime.now().astimezone()
    day = now.date() - timedelta(days=offset)
    start, end = _day_bounds(day)
    since, until = _day_utc(start), _day_utc(end)
    live = offset == 0
    hours = max(1, round((end - start).total_seconds() / 3600))
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
        before, whole, week = db.spend_windows(conn, [
            (_day_utc(before_start), _day_utc(before_cut)),
            (_day_utc(before_start), _day_utc(before_end)),
            (_day_utc(week_start), since)]) or (None, None, None)
        ids = list(dict.fromkeys([row[0] for row in usage] + [sid for sid, _ in turns]))
        # A commit can be recorded for a session that did nothing else today;
        # it still needs its title.
        rows = db.sessions_by_id(conn, list(dict.fromkeys(
            ids + [sid for sid, _kind, _value in refs or []])))
    finally:
        conn.close()
    running = {sid for sid, _pid in db.running_sessions()} if live else set()

    def hour_of(stamp: str) -> int | None:
        moment = _day_local(stamp)
        if moment is None:
            return None
        return min(max(int((moment - start).total_seconds() // 3600), 0), hours - 1)

    by_hour = [{"hour": (start + timedelta(hours=h)).astimezone().strftime("%H:00"),
                "nano_aiu": 0, "calls": 0, "asks": 0} for h in range(hours)]
    cards: dict[str, dict] = {}
    slots: set[int] = set()
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
                "new": sid in started, "live": sid in running,
                "by_hour": [0] * hours,
            }
        return cards[sid]

    def seen(entry: dict, stamp: str) -> None:
        moment = _day_local(stamp)
        if moment is None:
            return
        stamps.append(stamp)
        slots.add(int((moment - start).total_seconds() // (_DAY_SLOT_MINUTES * 60)))
        entry["first"] = min(entry["first"] or stamp, stamp)
        entry["last"] = max(entry["last"], stamp)

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
        seen(entry, stamp)
        hour = hour_of(stamp)
        if hour is not None:
            by_hour[hour]["nano_aiu"] += nano
            by_hour[hour]["calls"] += 1
            entry["by_hour"][hour] += nano
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
        seen(entry, stamp)
        hour = hour_of(stamp)
        if hour is not None:
            by_hour[hour]["asks"] += 1

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
        "spend": {
            "nano_aiu": spent if timed else None,
            "before_nano_aiu": before,
            "before_until": _day_utc(before_cut),
            "day_before_nano_aiu": whole,
            "week_average_nano_aiu": None if week is None else round(week / 7),
            "budget_aiu": ui.daily_budget_aiu() if live else None,
        },
        "sessions": len(cards),
        "new_sessions": sum(1 for s in cards.values() if s["new"]),
        "live_sessions": sum(1 for s in cards.values() if s["live"]),
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
        "model_ms": totals["duration_ms"],
        "first_token_ms": {"p50": db._percentile(first_token, 0.5),
                           "p95": db._percentile(first_token, 0.95)},
        "active_minutes": len(slots) * _DAY_SLOT_MINUTES,
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
             "average_ms": round(duration / calls) if calls else None,
             "first_token_ms": round(ttft / timed_calls) if timed_calls else None}
            for name, (calls, nano, duration, ttft, timed_calls)
            in sorted(models.items(), key=lambda item: (-item[1][1], -item[1][0]))],
        "repositories_by_spend": sorted(
            places.values(), key=lambda p: (-p["nano_aiu"], -p["asks"])),
        "by_session": [
            {**{key: s[key] for key in (
                "id", "title", "repository", "asks", "calls", "nano_aiu",
                "commits", "prs", "new", "live", "by_hour")},
             "first": _day_clock(s["first"]), "last": _day_clock(s["last"]),
             "models": [name for name, _ in s["models"].most_common()]}
            for s in sessions],
        "split": ([{"who": shown, "nano_aiu": who[kind], "calls": who_calls[kind]}
                   for kind, shown, _role in _DAY_WHO if who_calls[kind]]
                  if delegation else None),
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

def _day_aiu(nano: int | None) -> str:
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


def _day_count(value: int) -> str:
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


def _day_bar(share: float, cells: int) -> str:
    """A bar `share` of `cells` long in eighths, on a dotted track."""
    cells = max(cells, 0)
    eighths = round(min(max(share, 0.0), 1.0) * cells * 8)
    if share > 0 and not eighths:
        eighths = 1
    full, part = divmod(eighths, 8)
    drawn = "█" * full + (_DAY_EIGHTHS[part] if part else "")
    return drawn + "░" * (cells - ui.cells(drawn))


def _day_change(now: int | None, before: int | None) -> tuple[str, str] | None:
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


# ── The page ─────────────────────────────────────────────────────────
# Rows of (x, text, role), the same shape as the live page, so the screen,
# the printed form and the width tests read the same thing. Nothing here
# reads the clock: whatever moves arrives in `motion`.

def _day_header(data: dict, usable: int, motion: dict) -> list:
    progress = motion.get("title", 1.0)
    label = ui.typed(data["label"], progress)
    left = [(f" {ui.menu_icon('today')} ", "title"), (label, "title")]
    if data["label"] != data["written"] and label:
        left += [(" · ", "separator"), (ui.typed(data["written"], progress), "help")]
    row = _live_line(left, usable)
    used = sum(ui.cells(text) for _x, text, _r in row)
    if data["live"]:
        live = [("●", motion.get("pulse", "active")), (" live", "active")]
        options = ([[*live, ("   " + motion["clock"], "help")]]
                   if motion.get("clock") else []) + [live]
    else:
        back = data["offset"]
        options = [[(f"{back} days ago" if back > 1 else "a day ago", "help"),
                    ("  ·  → newer  t today", "separator")],
                   [(f"{back}d ago", "help")]]
    for parts in options:
        if used + sum(ui.cells(text) for text, _ in parts) + 2 <= usable:
            return row + _live_right(parts, usable)
    return row


def _day_story(data: dict, usable: int, motion: dict) -> list[list]:
    """The day in one sentence, numbers lit — what to read if nothing else."""
    return _day_words(_day_story_parts(data), usable - 2, motion.get("story", 1.0))


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


def _day_tiles_of(data: dict) -> list[tuple]:
    """(key, label, value, unit, role, raw, subs) per headline number.

    `subs` are the tile's second line, longest first; the page draws the
    first that fits. `raw` is the number a refresh compares to light a change.
    """
    spend = data["spend"]
    tiles: list[tuple] = []
    change = _day_change(spend["nano_aiu"], spend["before_nano_aiu"])
    against = "by now yesterday" if data["live"] else "the day before"
    if change and change[0] == "new":
        whole = spend["day_before_nano_aiu"]
        subs = [[("nothing by now · ", "help"), (_day_aiu(whole), "number"),
                 (" all day", "help")]] if data["live"] and whole else []
        subs += [[("nothing ", "help"), (against, "help")],
                 [("none by now" if data["live"] else "none the day before", "help")],
                 [("none before", "help")]]
    elif change:
        subs = [[change, (f" vs {_day_aiu(spend['before_nano_aiu'])} {against}", "help")],
                [change, (" vs yesterday" if data["live"] else " vs day before", "help")],
                [change]]
    elif spend["week_average_nano_aiu"] is not None:
        subs = [[("avg ", "help"), (_day_aiu(spend["week_average_nano_aiu"]), "number"),
                 ("/day over 7 days", "help")], []]
    else:
        subs = [[]]
    tiles.append(("spend", "AI SPEND", _day_aiu(spend["nano_aiu"]), " AIU", "credits",
                  spend["nano_aiu"] or 0, subs))
    detail = []
    if data["new_sessions"]:
        detail.append((f"{data['new_sessions']} new", "help"))
    if data["live_sessions"]:
        detail.append((f"● {data['live_sessions']} live", "active"))
    joined: list[tuple[str, str]] = []
    for part in detail:
        joined += [(" · ", "separator"), part] if joined else [part]
    tiles.append(("sessions", "SESSIONS", str(data["sessions"]), "", "title",
                  data["sessions"], [joined, detail[:1]]))
    busy = data["busiest"]
    tiles.append(("asks", "ASKS", _day_count(data["asks"]), "", "summary", data["asks"],
                  [[("peak ", "help"), (busy["hour"], "number"),
                    (f" · {busy['asks']}", "help")],
                   [("peak ", "help"), (busy["hour"], "number")]] if busy else [[]]))
    shipped = data["shipped"]
    if shipped is not None:
        commits, prs = shipped["commits"], shipped["prs"]
        said = [f"{commits} commit{'s' if commits != 1 else ''}" if commits else "",
                f"{prs} PR{'s' if prs != 1 else ''}" if prs else ""]
        tiles.append(("shipped", "SHIPPED", str(commits + prs), "", "active", commits + prs,
                      [[(" · ".join(filter(None, said)) or "nothing yet", "help")],
                       [(f"{commits}c · {prs} PR", "help")]]))
    places = data["repositories_by_spend"]
    top = next((p["repository"] for p in places
                if p["repository"] != "(no repository)" and not p["repository"].endswith("/")),
               "")
    tiles.append(("repos", "REPOSITORIES", str(data["repositories"]), "", "repo",
                  data["repositories"],
                  [[(top.rsplit("/", 1)[-1], "help")]] if top else [[]]))
    tiles.append(("calls", "MODEL CALLS", _day_count(data["calls"]), "", "summary",
                  data["calls"],
                  [[(_day_ms(data["model_ms"]), "number"), (" of model time", "help")],
                   [(_day_ms(data["model_ms"]), "number"), (" model", "help")]]
                  if data["model_ms"] else [[]]))
    tokens = data["tokens"]
    total = (tokens["input"] + tokens["output"] + tokens["cache_read"]
             + tokens["cache_write"])
    if total:
        tiles.append(("tokens", "TOKENS", _live_short(total), "", "turns", total,
                      [[(_day_pct(tokens["cache_hit"]), "number"),
                        (" from cache", "help")],
                       [(_day_pct(tokens["cache_hit"]), "number"), (" cached", "help")]]
                      if tokens["cache_hit"] is not None else [[]]))
    tools = data["tools"]
    if tools is not None:
        failed = tools["failures"]
        rate = failed / tools["calls"] if tools["calls"] else 0.0
        tiles.append(("tools", "TOOL CALLS", _day_count(tools["calls"]), "",
                      "summary", tools["calls"],
                      [[(f"{failed} failed", "danger" if failed else "help"),
                        (f" · {_day_pct(rate)}", "help")],
                       [(f"{failed} failed", "danger" if failed else "help")]]))
    elif data.get("tools_pending") and data.get("tools_expected"):
        tiles.append(("tools", "TOOL CALLS", "…", "", "help", 0,
                      [[("reading logs", "help")]]))
    if data["files"] is not None:
        made, edited = data["files"]["created"], data["files"]["edited"]
        tiles.append(("files", "FILES", str(made + edited), "", "summary", made + edited,
                      [[(f"{made} new · {edited} edited", "help")],
                       [(f"{made} new", "help")]]))
    if data["active_minutes"]:
        span = [(data["first"], "number"), (" → ", "separator"), (data["last"], "number")]
        tiles.append(("active", "ACTIVE", _live_span(data["active_minutes"] * 60), "",
                      "title", data["active_minutes"], [span]))
    return tiles


def _day_tiles(data: dict, usable: int, motion: dict) -> list[list]:
    """The headline numbers in rows of tiles, counting up as the page opens."""
    tiles = _day_tiles_of(data)
    fits = max(1, min(len(tiles), usable // 18))
    lines = -(-len(tiles) // fits)
    per = -(-len(tiles) // lines)
    each = usable // per
    flashes = motion.get("flash", {})
    deltas = motion.get("delta", {})
    out: list[list] = []
    for start in range(0, len(tiles), per):
        if start:
            out.append([])
        group = tiles[start:start + per]
        rows: list[list] = [[], [], []]
        for n, (key, label, value, unit, role, _raw, subs) in enumerate(group):
            index = start + n
            x = n * each
            room = each - 3
            if n:
                for row in rows:
                    row.append((x, "│", "separator"))
            lit = ui.light_role(motion.get("open", 99) - 0.1 - index * _DAY_TILE_STAGGER) \
                if motion.get("open") is not None else None
            rows[0] += _live_line([(label, lit or "help")], room, x + 2)
            progress = motion.get("count", {}).get(index, 1.0) \
                if isinstance(motion.get("count"), dict) else 1.0
            shown = ui.count_up(value, progress).strip() or value
            age = flashes.get(key)
            flash = ui.flash_role(age) if age is not None else None
            parts = [(shown, flash or role)]
            if unit and ui.cells(shown + unit) <= room:
                parts.append((unit, "help"))
            step = deltas.get(key)
            if flash and step and ui.cells(shown + unit + step) + 1 <= room:
                parts.append((" " + step, "warn" if step.startswith("▲") else "active"))
            rows[1] += _live_line(parts, room, x + 2)
            # The caption types in under its count, at the same pace.
            caption = _day_fit(subs, room)
            budget = round(sum(len(text) for text, _ in caption) * progress)
            typed = []
            for text, part_role in caption:
                if budget > 0:
                    typed.append((text[:budget] if ui.MOTION else text, part_role))
                budget -= len(text)
            rows[2] += _live_line(typed if progress < 1 else caption, room, x + 2)
        out += rows
    return out


def _day_budget(data: dict, usable: int, motion: dict) -> list[list]:
    """Today's spend against the daily budget, when one is set."""
    limit = data["spend"]["budget_aiu"]
    spent = data["spend"]["nano_aiu"]
    if not limit or spent is None:
        return []
    aiu = spent / 1e9
    share = aiu / limit
    role = ui.budget_style_name(aiu, limit)
    left = limit - aiu
    tail = (f" {share:.0%} of {limit:g} AIU · "
            + (f"{left:,.1f} left" if left >= 0 else f"{-left:,.1f} over"))
    if usable < 56:
        tail = f" {share:.0%} of {limit:g}"
    cells = usable - 9 - ui.cells(tail) - 2
    row = [(1, "BUDGET", "label")]
    if cells >= 6:
        bar = ui.grow(_day_bar(min(share, 1.0), cells), motion.get("bars", 1.0))
        row.append((9, bar, role))
        row += _live_line([(tail, role)], usable - 10 - cells, 9 + cells)
    else:
        row += _live_line([(tail.strip(), role)], usable - 9, 9)
    return [row]


def _day_chart(data: dict, width: int, motion: dict, tall: bool) -> tuple[list, list, list]:
    """Spend by local hour as columns in the gradient, asks beneath them.

    Returns (title, note, body) for a panel `width` cells wide. The bars rise
    from their baseline and sweep in from midnight as the page opens; the
    hour you are in breathes while today is live.
    """
    inner = width - 4
    hours = len(data["by_hour"])
    spend = [h["nano_aiu"] for h in data["by_hour"]]
    asks = [h["asks"] for h in data["by_hour"]]
    gutter = 6 if inner >= 60 else 0
    per = max(1, (inner - gutter) // hours)
    bar = per - 1 if per >= 3 else 1
    rows = 5 if tall else 3
    grow = motion.get("grow", 1.0)
    sweep = motion.get("sweep", 1.0)
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
    # Asks, one row of sparks on the same columns.
    top = max(asks, default=0)
    strip: list = [(0, "asks", "help")] if gutter else []
    for hour in range(min(shown, hours)):
        if asks[hour] and top:
            glyph = _DAY_SPARKS[max(1, round(asks[hour] * grow / top * 8))]
            strip.append((gutter + hour * per, glyph * bar, "turns"))
    if top:
        body.append(strip)
    # The hours along the bottom, as often as there is room to write them.
    step = next(n for n in (1, 2, 3, 4, 6, 12, 24) if n * per >= 3)
    axis: list = []
    for hour in range(0, hours, step):
        x = gutter + hour * per
        if x + 2 > inner:
            break
        role = "title" if hour == now_hour else "help"
        axis.append((x, data["hours"][hour][:2], role))
    body.append(axis)
    note: list[tuple[str, str]] = []
    if data["peak"]:
        note = [("peak ", "help"), (data["peak"]["hour"], "number")]
        if width >= 60:
            note.append((f" · {_day_aiu(data['peak']['nano_aiu'])} AIU", "credits"))
    if not peak and not top:
        body = [_live_line([("No billed calls or asks on this day.", "help")], inner)]
    return [("Spend by hour", "header")], note, body


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


def _day_models(data: dict, width: int, motion: dict) -> tuple[list, list, list]:
    inner = width - 4
    models = data["models"]
    chips = max(motion.get("chips", 8), 1)
    if not models:
        return ([("Models", "header")], [],
                [_live_line([("No model calls.", "help")], inner)])
    shown = models[:_DAY_MODELS]
    rest = models[_DAY_MODELS:]
    # The share is also the bar's length, so it is the first figure to give
    # way when a model's name would otherwise be cut.
    longest = max(ui.cells(m["model"]) for m in shown)
    percent = longest <= inner - 3 - 1 - 6 - 11

    def figures(nano: int, share: float) -> list[tuple[str, str]]:
        out = [(_day_aiu(nano).rjust(6), "credits")]
        if percent:
            out.append((f" {_day_pct(share):>4}", "number"))
        return out

    items = [(m["model"], m["share"], f"c{n % chips}", figures(m["nano_aiu"], m["share"]),
              f"c{n % chips}", m["model"]) for n, m in enumerate(shown)]
    if rest:
        nano = sum(m["nano_aiu"] for m in rest)
        share = sum(m["share"] for m in rest)
        items.append((f"{len(rest)} more", share, "separator", figures(nano, share),
                      "separator", f"{len(rest)} more"))
    body = []
    for n, row in enumerate(_day_rank_rows(items, inner)):
        body.append(row)
        if n < len(shown) and inner >= 40:
            m = shown[n]
            calls = [(f"{m['calls']:,} calls", "number")]
            each = ([(" · ", "separator"), (_day_ms(m["average_ms"]), "number"),
                     (" each", "help")] if m["average_ms"] else [])
            first = m["first_token_ms"]
            options = [calls + each + [(" · ", "separator"), (_day_ms(first), "number"),
                                       (" to first token", "help")]] if first else []
            options += [calls + each + [(" · ", "separator"), (_day_ms(first), "number"),
                                        (" first", "help")]] if first else []
            options += [calls + each, calls]
            body.append(_live_line(_day_fit(options, inner - 2), inner - 2, 2))
    return ([("Models", "header")],
            [(f"{len(models)} used", "help")], body)


def _day_repos(data: dict, width: int, motion: dict) -> tuple[list, list, list]:
    inner = width - 4
    places = data["repositories_by_spend"]
    spent = data["spend"]["nano_aiu"] or 0
    chips = max(motion.get("chips", 8), 1)
    if not places:
        return ([("Repositories", "header")], [],
                [_live_line([("No repository worked in.", "help")], inner)])
    items = []
    for n, place in enumerate(places[:_DAY_REPOS]):
        figures = [(_day_aiu(place["nano_aiu"]).rjust(6), "credits")]
        if inner >= 44:
            figures.append((f" {place['sessions']:>2} sess", "number"))
        if place["shipped"] and inner >= 52:
            figures.append((f" ✓{place['shipped']}", "active"))
        elif inner >= 52:
            figures.append(("   ", "separator"))
        share = place["nano_aiu"] / spent if spent else 0.0
        items.append((place["repository"], share, "repo", figures, f"c{(n + 3) % chips}",
                      place["repository"].rstrip("/").rsplit("/", 1)[-1]))
    body = _day_rank_rows(items, inner)
    if len(places) > _DAY_REPOS:
        body.append(_live_line([(f"+{len(places) - _DAY_REPOS} more", "help")], inner, 2))
    return ([("Repositories", "header")], [(f"{len(places)}", "help")], body)


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
                [_live_line([("No calls to split.", "help")], inner)])
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


def _day_shipped(data: dict, width: int, motion: dict) -> tuple[list, list, list]:
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
    for item in items[:_DAY_SHIPPED]:
        pr = item["kind"] == "pr"
        mark = ("PR " if pr else "◆ ", "turns" if pr else "active")
        value = f"#{item['value'].lstrip('#')}" if pr else item["value"][:12]
        room = inner - ui.cells(mark[0]) - ui.cells(value) - 2
        body.append(_live_line([mark, (value, "number"), ("  ", "separator"),
                                (ui.trunc(item["title"], room), "help")], inner))
    if len(items) > _DAY_SHIPPED:
        body.append(_live_line([(f"+{len(items) - _DAY_SHIPPED} more", "help")], inner))
    note = f"{shipped['commits']} commits · {shipped['prs']} PRs"
    return [("Shipped", "header")], [(note, "help")], body


def _day_tool_panel(data: dict, width: int, motion: dict) -> tuple[list, list, list]:
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
    for tool in tools["by_tool"][:_DAY_TOOLS]:
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


def _day_session_rows(data: dict, inner: int, motion: dict
                      ) -> tuple[list[list], list[int]]:
    """A row a session, and the body line each one landed on."""
    sessions = data["by_session"]
    hours = len(data["hours"])
    cursor = motion.get("cursor")
    moved = motion.get("moved")
    cols: list[tuple[str, int, str]] = []  # (key, width, align)
    if inner >= 46:
        cols.append(("asks", 5, ">"))
    cols.append(("aiu", 7, ">"))
    if inner >= 88:
        cols.append(("spark", min(hours, 24), "<"))
    if inner >= 104:
        cols.append(("span", 11, "<"))
    repo_w = min(24, inner // 5) if inner >= 64 else 0
    fixed = sum(w + 2 for _k, w, _a in cols) + (repo_w + 2 if repo_w else 0)
    title_w = max(inner - 2 - fixed, 6)
    heads = {"asks": "ASKS", "aiu": "AIU", "spark": "BY HOUR", "span": "ACTIVE"}
    head: list = [(2, "SESSION", "label")]
    x = 2 + title_w + 2
    if repo_w:
        head.append((x, "REPOSITORY", "label"))
        x += repo_w + 2
    for key, w, align in cols:
        text = heads[key][:w]
        head.append((x + (w - len(text) if align == ">" else 0), text, "label"))
        x += w + 2
    rows: list[list] = [[seg for seg in head if seg[0] + ui.cells(seg[1]) <= inner]]
    lines: list[int] = []
    for index, s in enumerate(sessions):
        mark = ("●", motion.get("pulse", "active")) if s["live"] else \
            ("+", "active") if s["new"] else ("·", "separator")
        row: list = [(0, mark[0], mark[1]),
                     (2, ui.trunc(s["title"], title_w), "title" if s["live"] else "summary")]
        x = 2 + title_w + 2
        if repo_w:
            repo = (s["repository"] or "—").rsplit("/", 1)[-1]
            row.append((x, ui.trunc(repo, repo_w), "repo"))
            x += repo_w + 2
        for key, w, _align in cols:
            if key == "asks":
                row.append((x, f"{s['asks']:>{w}}", "number"))
            elif key == "aiu":
                row.append((x, f"{ui.fmt_aiu(s['nano_aiu']):>{w}}", "credits"))
            elif key == "spark":
                values = s["by_hour"]
                if w < hours:
                    values = [sum(values[i * hours // w:(i + 1) * hours // w])
                              for i in range(w)]
                spark = ui.sparkline(values) or " " * w
                row.append((x, spark if spark.strip() else "·" * w,
                            "turns" if spark.strip() else "separator"))
            else:
                span = f"{s['first']}–{s['last']}" if s["first"] else ""
                row.append((x, span, "help"))
            x += w + 2
        row = [seg for seg in row if seg[0] + ui.cells(seg[1]) <= inner]
        if index == cursor:
            sweep = 1.0 if moved is None else min(moved / ui.SWEEP_SECONDS, 1.0)
            row = _live_lit(row, round(inner * (1 - (1 - sweep) ** 2)))
        lines.append(len(rows))
        rows.append(row)
    return rows, lines


def _day_sessions(data: dict, width: int, motion: dict
                  ) -> tuple[list, list, list, list[int]]:
    inner = width - 4
    if not data["by_session"]:
        return ([("Sessions", "header")], [],
                [_live_line([("No session did anything on this day.", "help")], inner)], [])
    body, lines = _day_session_rows(data, inner, motion)
    note = [(f"{len(data['by_session'])}", "number"), (" · ↵ opens", "help")]
    return [("Sessions", "header")], note, body, lines


def _day_reveal(panel: list[list], progress: float, width: int,
                contents: bool = True) -> list[list]:
    """A panel part way through its entrance.

    Each row wipes in from the left, a little behind the row above, so the
    border draws itself down the panel; behind the wipe the bars fill along
    their track and the figures count up. `contents=False` leaves the body
    to animate itself — the hour chart rises and sweeps on its own.
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


def _day_open_seconds(panels: int) -> float:
    return max(panels * _DAY_DEAL_SECONDS + _DAY_PANEL_SECONDS,
               0.15 + ui.COUNT_SECONDS + 10 * _DAY_TILE_STAGGER)


def _day_layout(width: int) -> list[list[str]]:
    """Which panels share a row, at this width."""
    if width >= 120:
        return [["chart"], ["models", "repos", "running"], ["sessions"],
                ["shipped", "tools", "split"]]
    if width >= 80:
        return [["chart"], ["models", "repos"], ["running", "split"], ["sessions"],
                ["shipped", "tools"]]
    return [["chart"], ["sessions"], ["models"], ["repos"], ["running"], ["split"],
            ["shipped"], ["tools"]]


def _day_screen(data: dict, width: int, height: int | None = None,
                motion: dict | None = None
                ) -> tuple[list, list[list], list[tuple[int, int, int, int]]]:
    """The page: the pinned title row, the body, and where each session landed.

    The body scrolls under the title. Hits are (body y, left, right, session
    index) for the mouse and for keeping the chosen session in view.
    """
    motion = motion or {}
    usable = width - 1
    header = _day_header(data, usable, motion)
    body: list[list] = [[]]
    body += _day_story(data, usable, motion)
    body.append([])
    if data["sessions"] or data["spend"]["nano_aiu"]:
        body += _day_tiles(data, usable, motion)
        budget = _day_budget(data, usable, motion)
        if budget:
            body.append([])
            body += budget
        body.append([])
    tall = height is None or (height >= 40 and width >= 100)
    builders = {
        "models": _day_models, "repos": _day_repos, "running": _day_running,
        "split": _day_split, "shipped": _day_shipped, "tools": _day_tool_panel,
    }
    hits: list[tuple[int, int, int, int]] = []
    opened = motion.get("open")
    order = 0
    if not data["sessions"]:
        return header, body, hits
    for group in _day_layout(width):
        if group == ["split"] and data["split"] is None:
            continue
        group = [name for name in group if name != "split" or data["split"] is not None]
        each = (usable - (len(group) - 1)) // len(group)
        widths = [each] * len(group)
        widths[-1] = usable - (each + 1) * (len(group) - 1)
        built = []
        for name, panel_w in zip(group, widths, strict=True):
            lines: list[int] = []
            if name == "chart":
                title, note, inner_rows = _day_chart(data, panel_w, motion, tall)
            elif name == "sessions":
                title, note, inner_rows, lines = _day_sessions(data, panel_w, motion)
            else:
                title, note, inner_rows = builders[name](data, panel_w, motion)
            built.append((name, panel_w, title, note, inner_rows, lines))
        tallest = max(len(rows) for *_rest, rows, _lines in built) + 2
        top = len(body)
        x = 0
        for name, panel_w, title, note, inner_rows, lines in built:
            panel = _live_panel(title, note, inner_rows, panel_w, tallest)
            if opened is not None:
                progress = (opened - 0.15 - order * _DAY_DEAL_SECONDS) / _DAY_PANEL_SECONDS
                panel = _day_reveal(panel, progress, panel_w, contents=name != "chart")
            order += 1
            while len(body) < top + len(panel):
                body.append([])
            for dy, line in enumerate(panel):
                body[top + dy] += [(x + px, text, role) for px, text, role in line]
            for index, line in enumerate(lines):
                hits.append((top + 1 + line, x, x + panel_w, index))
            x += panel_w + 1
        body.append([])
    foot = ("Every figure is cut to this day by its own time. Active time counts "
            f"{_DAY_SLOT_MINUTES}-minute slots that held an ask or a model call.")
    if data["live"]:
        foot += " Spend is compared with yesterday up to the same time."
    body += _day_words([(foot, "separator")], usable - 2)
    return header, body, hits


_DAY_HINTS = (" ←→ day · ↑↓ session · ↵ open · PgUp/PgDn scroll · r refresh · q back ",
              " ←→ day · ↑↓ · ↵ open · r refresh · q back ",
              " ←→ day · ↵ open · q back ", " ←→ · q ")


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
    """The page as text, for a pipe or a terminal with no curses — coloured
    only when it is going to a terminal."""
    data = _day_data(offset)
    motion = {"grad": len(ui._BAR_RAMP), "chips": 8,
              "clock": f"as of {time.strftime('%H:%M')}"}
    header, body, _hits = _day_screen(data, width, None, motion)
    out = []
    for line in [header, *body]:
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

def _day_tile_values(data: dict) -> dict[str, tuple[str, float]]:
    return {key: (value, raw) for key, _label, value, _unit, _role, raw, _subs
            in _day_tiles_of(data)}


def _day_read(state: dict, tools: bool = True, fatal: bool = False) -> None:
    """One look at the day the page is on. A store that cannot be read
    keeps what is already on screen."""
    offset = state.get("offset", 0)
    if state.get("memo_offset") != offset:
        state["memo"] = {}
        state["memo_offset"] = offset
    try:
        data = _day_data(offset, state["memo"], tools=tools, fatal=fatal)
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
    # The store is read before the first frame and the logs after it, behind
    # the entrance, so the page is on screen before the slow part starts.
    painted = False
    moved_at = None
    lit: dict[str, float] = {}
    deltas: dict[str, str] = {}
    try:
        while True:
            clock = time.monotonic()
            data = state["data"]
            if (data.get("tools_pending") and painted) or (
                    data["live"] and clock - state.get("read_at", 0) >= DAY_REFRESH_SECONDS):
                before = _day_tile_values(data)
                _day_read(state)
                data = state["data"]
                if not data.get("tools_pending"):
                    for key, (value, raw) in _day_tile_values(data).items():
                        old = before.get(key)
                        if old and old[0] != value and old[0] != "…":
                            lit[key] = time.monotonic()
                            step = raw - old[1]
                            if step:
                                shown = (_day_aiu(abs(step)) if key == "spend"
                                         else _live_span(abs(step) * 60) if key == "active"
                                         else _day_count(abs(int(step))))
                                deltas[key] = f"{'▲' if step > 0 else '▼'}{shown}"
            for key in [key for key, at in lit.items() if clock - at >= ui.FLASH_SECONDS]:
                del lit[key]
            sessions = data["by_session"]
            cursor = state["cursor"] = min(max(state.get("cursor", 0), 0),
                                           max(len(sessions) - 1, 0))
            height, width = screen.getmaxyx()
            elapsed = None if opened is None else clock - opened
            panels = sum(len(group) for group in _day_layout(width))
            if elapsed is not None and elapsed >= _day_open_seconds(panels):
                opened = elapsed = None
                state["dealt"] = True
            moved = None if moved_at is None else clock - moved_at
            motion = {
                "open": elapsed,
                "title": ui.launch_progress(elapsed, 0.0, 0.35),
                "story": ui.launch_progress(elapsed, 0.2, 0.6),
                "count": {n: ui.launch_progress(elapsed, 0.15 + n * _DAY_TILE_STAGGER,
                                                ui.COUNT_SECONDS) for n in range(12)},
                "bars": ui.launch_progress(elapsed, 0.3, 0.6),
                "grow": ui.launch_progress(elapsed, 0.2, 0.8),
                "sweep": ui.launch_progress(elapsed, 0.15, 0.6),
                "flash": {key: clock - at for key, at in lit.items()},
                "delta": deltas,
                "cursor": cursor if sessions else None,
                "moved": moved,
                "pulse": ui.pulse_role(clock) if ui.MOTION else "active",
                "clock": time.strftime("%H:%M:%S"),
                "grad": grad, "chips": chips,
            }
            header, body, hits = _day_screen(data, width, height, motion)
            view = max(height - 2, 1)
            most = max(len(body) - view, 0)
            if state.pop("follow", False) and hits:
                lines = [y for y, _l, _r, index in hits if index == cursor]
                if lines:
                    line = lines[0]
                    if line < state.get("scroll", 0):
                        state["scroll"] = max(line - 2, 0)
                    elif line >= state.get("scroll", 0) + view:
                        state["scroll"] = line - view + 2
            scroll = state["scroll"] = min(max(state.get("scroll", 0), 0), most)
            screen.erase()
            for x, text, role in header:
                _addstr(screen, 0, x, text, width, theme[role])
            for y, line in enumerate(body[scroll:scroll + view], 1):
                for x, text, role in line:
                    _addstr(screen, y, x, text, width, theme[role])
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
            moving = (opened is not None or lit
                      or (moved is not None and moved < ui.SWEEP_SECONDS)
                      or data.get("tools_pending"))
            screen.timeout(ui.MOTION_MS if moving and ui.MOTION else
                           0 if data.get("tools_pending") else 1000)
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
                    for hit_y, left, right, index in hits:
                        if hit_y - scroll + 1 == y and left <= x < right:
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
            if key in (10, 13, curses.KEY_ENTER) and sessions:
                return sessions[cursor]["id"]
            step_day = {curses.KEY_LEFT: 1, ord("h"): 1, curses.KEY_RIGHT: -1,
                        ord("l"): -1}.get(key)
            if key in (ord("t"), ord("T")):
                step_day = -state.get("offset", 0)
            if step_day is not None:
                offset = min(max(state.get("offset", 0) + step_day, 0), _DAY_BACK)
                if offset != state.get("offset", 0):
                    state["offset"] = offset
                    state["cursor"] = state["scroll"] = 0
                    _day_read(state, tools=False)
                    lit.clear()
                    deltas.clear()
                    opened = time.monotonic()
                    state["dealt"] = False
                continue
            if key in (ord("r"), ord("R")):
                state["read_at"] = 0.0
                if not data["live"]:
                    _day_read(state)
            elif key in (curses.KEY_UP, ord("k")) and sessions:
                state["cursor"] = cursor - 1
                moved_at = time.monotonic()
                state["follow"] = True
            elif key in (curses.KEY_DOWN, ord("j")) and sessions:
                state["cursor"] = cursor + 1
                moved_at = time.monotonic()
                state["follow"] = True
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
    return data
