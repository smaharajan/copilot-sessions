"""Machine-readable output — the same readings, without the drawing.

Every view in `cs` is built to be read by a person: it wraps, it colours, it
puts a rule over a heading and a footnote under a number. None of that
survives being piped, and a tool whose numbers can only be looked at is a
tool that stops at the edge of the terminal.

This module is the other edge. `--json` on a view hands back the figures it
would have drawn, as plain data, so the same store can feed a spreadsheet, a
dashboard, a weekly report or a CI check without anyone screen-scraping a
bar chart. It is deliberately a *separate* path rather than a flag threaded
through the renderers: a report that has to serve two audiences in one
function ends up serving neither, and the drawn views stay free to change
their layout without changing anyone's contract.

Two rules hold everywhere here:

* **Redaction is not optional.** Anything that can carry a credential goes
  out through the same masking the drawn views use. Piping is exactly when
  text stops being glanced at and starts being stored.
* **Absent is absent.** A reading this store cannot answer is left out
  rather than emitted as zero, matching the rule the rest of the codebase
  keeps: a missing column is a missing answer, not a nil result.
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import sys

from . import db, redact

# The views that can be asked for as data, and what each one is called on the
# command line. Kept as one table so `--json` cannot quietly support a view
# that `--help` never mentions.
VIEWS = (
    "sessions", "search", "stats", "cost", "efficiency",
    "delegation", "repos", "skills", "skills-by-repo", "profiles",
    "standup", "failures", "loops", "subagents", "switches", "endings",
    "next", "eod", "weekly", "today", "similar", "saved",
    "cleanup", "budget", "anomalies", "health", "patterns",
    "doctor", "rollup",
)

# What someone actually types to reach each of those. The names above are what
# a payload calls *itself*, which is not the same thing: `sessions` is reached
# as `recent` or `all`, and `delegation` as `agents`. An error message that
# lists payload names is an error message that suggests commands which do not
# exist, so the two lists are kept apart on purpose.
DATA_COMMANDS = (
    "recent", "all", "search", "stats", "timeline", "cost", "efficiency",
    "agents", "repos", "skills", "profiles", "standup", "export",
    "failures", "loops", "subagents", "switches", "endings",
    "next", "eod", "weekly", "today", "similar", "saved", "cleanup", "budget",
    "anomalies", "health", "patterns", "doctor", "rollup",
)


def _session_rows(rows: list[tuple]) -> list[dict]:
    """Listing rows as objects, with the two kit counts when they are there."""
    out = []
    for row in rows:
        sid, active, summary, repo, cwd, turns, nano = row[:7]
        item = {
            "id": sid,
            "last_active": active,
            "summary": redact.one_line(redact.redact(summary or "")),
            "repository": repo or None,
            "cwd": cwd or None,
            "turns": turns,
            "nano_aiu": nano,
        }
        if len(row) > 8:
            item["skills_referenced"] = row[7]
            item["subagents_run"] = row[8]
        out.append(item)
    return out


def sessions(days: int, show_all: bool, rows: list[tuple]) -> dict:
    return {
        "view": "sessions",
        "window_days": days,
        "include_quiet": show_all,
        "count": len(rows),
        "sessions": _session_rows(rows),
    }


def search(term: str, rows: list[tuple], hits: dict) -> dict:
    found = _session_rows(rows)
    for item in found:
        hit = hits.get(item["id"])
        if hit:
            source, snippet = hit
            item["matched_in"] = source
            item["match"] = redact.one_line(redact.redact(snippet))
    return {"view": "search", "term": term, "count": len(found), "sessions": found}


def stats(days: int | None) -> dict:
    conn = db.connect()
    try:
        basics = dict(db.stats(conn))
        out = {
            "view": "stats",
            "window_days": days,
            "store": {
                key: value
                for key, value in basics.items()
                if not isinstance(value, tuple)
            },
            "impact": dict(db.impact(conn, days)),
        }
        # The pairs are (name, count); a two-element tuple is a shape nobody
        # can read back reliably, so each becomes a named object.
        for key in ("top_repo", "busiest_day", "top_model"):
            pair = basics.get(key)
            if pair:
                out["store"][key] = {"name": pair[0], "value": pair[1]}
    finally:
        conn.close()
    return out


def timeline(days: int) -> dict:
    """The working-day ledger, one object per day that had any activity.

    Days with nothing are left out rather than filled with zeroes, the same
    shape the report draws. A run of empty days is not data anyone asked for,
    and in a spreadsheet it is a hundred rows to filter back out.
    """
    conn = db.connect()
    try:
        rows = db.timeline(conn, days)
    finally:
        conn.close()
    return {
        "view": "timeline",
        "window_days": days,
        "working_days": len(rows),
        "by_day": [
            {"day": day, "sessions": sessions_, "turns": turns,
             "nano_aiu": nano}
            for day, sessions_, turns, nano in rows
        ],
    }


def cost(days: int) -> dict:
    conn = db.connect()
    try:
        if not db.has_usage(conn):
            return {"view": "cost", "window_days": days, "usage_recorded": False}
        return {
            "view": "cost",
            "window_days": days,
            "usage_recorded": True,
            "totals": dict(db.cost_totals(conn, days)),
            "by_model": [
                {"model": m, "calls": c, "nano_aiu": n,
                 "avg_duration_ms": d, "avg_ttft_ms": t}
                for m, c, n, d, t in db.cost_by_model(conn, days)
            ],
            "by_repository": [
                {"repository": p, "sessions": s, "nano_aiu": n}
                for p, s, n in db.cost_by_repo(conn, days)
            ],
            "by_day": [
                {"day": d, "nano_aiu": n, "calls": c}
                for d, n, c in db.cost_by_day(conn, days)
            ],
        }
    finally:
        conn.close()


def efficiency(days: int) -> dict:
    conn = db.connect()
    try:
        if not db.has_usage(conn):
            return {"view": "efficiency", "window_days": days, "usage_recorded": False}
        reading = db.efficiency(conn, days)
    finally:
        conn.close()
    out: dict = {"view": "efficiency", "window_days": days, "usage_recorded": True}
    if "cache" in reading:
        out["cache"] = reading["cache"]
    if "latency" in reading:
        out["first_token_ms"] = reading["latency"]
    if reading.get("multipliers"):
        out["multipliers"] = [
            {"multiplier": rate, "calls": calls, "nano_aiu": nano}
            for rate, calls, nano in reading["multipliers"]
        ]
    if reading.get("effort"):
        out["reasoning_effort"] = dict(reading["effort"])
    if reading.get("finish"):
        out["finish_reason"] = dict(reading["finish"])
    if reading.get("by_model"):
        out["by_model"] = [
            {
                "model": model, "calls": calls, "nano_aiu": nano,
                "cache_read_tokens": cached, "tokens_sent": offered,
                "cache_hit_rate": (cached / offered) if offered else None,
                "avg_ttft_ms": ttft,
            }
            for model, calls, nano, cached, offered, ttft in reading["by_model"]
        ]
    return out


def delegation(days: int) -> dict:
    conn = db.connect()
    try:
        split = db.work_split(conn, days)
    finally:
        conn.close()
    by_initiator = [
        {"initiator": who, "calls": calls, "nano_aiu": nano, "sessions": seen}
        for who, calls, nano, seen in split.get("by_initiator", [])
    ]
    total = sum(row["nano_aiu"] for row in by_initiator)
    delegated = sum(
        row["nano_aiu"] for row in by_initiator if row["initiator"] == "sub-agent"
    )
    return {
        "view": "delegation",
        "window_days": days,
        "by_initiator": by_initiator,
        "sessions": split.get("sessions", 0),
        "delegated_tasks": split.get("delegated_tasks", 0),
        # The canonical delegation ratio: what share of the spend the
        # sub-agents accounted for.
        "delegation_ratio": (delegated / total) if total else None,
    }


def repos() -> dict:
    conn = db.connect()
    try:
        rows = db.repos(conn)
    finally:
        conn.close()
    return {
        "view": "repos",
        "count": len(rows),
        "repositories": [
            {"repository": name, "sessions": sessions_, "turns": turns,
             "nano_aiu": nano, "last_active": last}
            for name, sessions_, turns, nano, last in rows
        ],
    }


def assets(kind: str, names: list[str], counts: dict[str, int],
           scopes: dict[str, str] | None = None,
           states: dict[str, str] | None = None,
           ran: dict[str, int] | None = None,
           usage: dict[str, dict] | None = None) -> dict:
    """The skills or agent-profile inventory, with how many sessions used each.

    `scopes` and `states` say where a name lives and whether it is switched
    on; a name in neither is one the CLI is recorded as having run with
    nothing on this disk to point at, and it is marked `not installed`
    rather than left out. The original two keys are unchanged — a reader
    pinned to `name` and `sessions_referencing` keeps working.
    """
    scopes, states, ran, usage = scopes or {}, states or {}, ran or {}, usage or {}
    installed = [name for name in names if name in scopes]
    return {
        "view": "skills" if kind == "skills" else "profiles",
        "installed": len(installed),
        "referenced": sum(1 for name in names if counts.get(name)),
        "disabled": sum(1 for name in installed if states.get(name) == "disabled"),
        "ran": len(ran),
        "not_installed": len(names) - len(installed),
        "assets": [
            {
                "name": name,
                "sessions_referencing": counts.get(name, 0),
                "scope": scopes.get(name, "not installed"),
                "state": states.get(name, "unknown"),
                "sessions_loaded": ran.get(name.lower(), 0),
                # Recorded in the event logs: how often it was invoked, when
                # last, and for an agent profile whether its model held.
                "invoked": usage.get(name.lower(), {}).get("invoked", 0),
                "last_used": usage.get(name.lower(), {}).get("last_used") or None,
                **({"override_honoured":
                    usage.get(name.lower(), {}).get("override_honoured")}
                   if kind != "skills" else {}),
            }
            for name in sorted(names, key=lambda n: (-counts.get(n, 0), n.lower()))
        ],
    }


def assets_by_place(places: list[dict]) -> dict:
    """Where each skill was reached for, one entry per checkout.

    The same reading as `cs skills --by-repo` on screen. `borrowed` is the
    part worth exporting: a skill a checkout used but does not carry works
    for you and not for whoever clones it.
    """
    return {
        "view": "skills-by-repo",
        "places": len(places),
        "skills_used": sum(len(place["skills"]) for place in places),
        "borrowed": sum(1 for place in places for name in place["skills"]
                        if name not in place["ships"]),
        # One row per skill per checkout rather than a nested list. The same
        # tidy shape the other payloads use, and the only one `--csv` can
        # render: a cell holding a list of objects is a cell a spreadsheet
        # cannot do anything with.
        "usage": [
            {
                "checkout": place["name"],
                "checkout_sessions": place["sessions"],
                "skill": name,
                "sessions": count,
                "shipped_here": name in place["ships"],
            }
            for place in places
            for name, count in place["skills"].most_common()
        ],
    }


def standup(days: int) -> dict:
    """Daily brief as data — same readings `cs standup` draws."""
    from . import signals
    from .cli._common import _visible

    conn = db.connect()
    try:
        rows = db.recent_sessions(conn, days)
        timeline = db.timeline(conn, days)
        totals = db.cost_totals(conn, days)
        handoff_rows = signals.handoffs(conn)
        window_ids = {row[0] for row in rows}
        risk_rows = []
        if 0 < len(window_ids) <= 40:
            risk_rows = [
                {
                    "id": row["id"],
                    "verdict": row["verdict"],
                    "summary": redact.one_line(redact.redact(row["summary"] or "")),
                }
                for row in signals.autonomy(conn)
                if row["id"] in window_ids and row["verdict"] in ("yes", "high")
            ]
    finally:
        conn.close()

    visible = _visible(rows, show_all=False)
    turns = sum(day_turns for _day, _sessions, day_turns, _spend in timeline)
    spend = totals.get("nano_aiu", 0) if totals else 0
    moved = []
    for row in visible:
        if not (row[2] or "").strip():
            continue
        sid, active, summary, repo, _cwd, turns_n, nano = row[:7]
        moved.append({
            "id": sid,
            "last_active": active,
            "summary": redact.one_line(redact.redact(summary or "")),
            "repository": repo or None,
            "turns": turns_n,
            "nano_aiu": nano,
        })
        if len(moved) >= 8:
            break
    handoffs = [
        {
            "id": row["id"],
            "role": row["role"],
            "summary": redact.one_line(redact.redact(row["summary"] or "")),
        }
        for row in handoff_rows
        if row["id"] in window_ids
    ]
    out = {
        "view": "standup",
        "window_days": days,
        "sessions": len(visible),
        "turns": turns,
        "nano_aiu": spend,
        "moved": moved,
        "handoffs": handoffs,
    }
    if risk_rows:
        out["risks"] = {"yolo": risk_rows}
    return out


# ── Evidence views ───────────────────────────────────────────────────
# The readings are built once, in `cli.evidence`, already masked; the page
# draws them and these hand them back. One computation, so the two cannot
# disagree.

def failures(days: int) -> dict:
    from .cli.evidence import _failures_data
    return {"view": "failures", **_failures_data(days)}


def loops(days: int) -> dict:
    from .cli.evidence import _loops_data
    return {"view": "loops", **_loops_data(days)}


def subagents(days: int) -> dict:
    from .cli.evidence import _subagents_data
    return {"view": "subagents", **_subagents_data(days)}


def switches(days: int) -> dict:
    from .cli.evidence import _switches_data
    return {"view": "switches", **_switches_data(days)}


def endings(days: int) -> dict:
    from .cli.evidence import _endings_data
    return {"view": "endings", **_endings_data(days)}


# ── Today and Find ───────────────────────────────────────────────────

def next_up(days: int = 14) -> dict:
    from .cli.today import _next_data
    return {"view": "next", **_next_data(days)}


def eod() -> dict:
    from .cli.today import _eod_data
    return {"view": "eod", **_eod_data()}


def weekly() -> dict:
    from .cli.today import _weekly_data
    return {"view": "weekly", **_weekly_data()}


def today() -> dict:
    from .cli.today import _today_data
    return {"view": "today", **_today_data()}


def similar(ref: str) -> dict:
    from .cli.today import _similar_data
    return {"view": "similar", **_similar_data(ref)}


def saved() -> dict:
    from . import ui
    return {"view": "saved", "searches": [
        {"name": redact.one_line(redact.redact(name)),
         "term": redact.one_line(redact.redact(term))}
        for name, term in ui.saved_searches().items()]}


def cleanup(days: int = 14) -> dict:
    from .cli.today import _cleanup_data
    return {"view": "cleanup", **_cleanup_data(days)}


def budget() -> dict:
    from . import ui
    from .cli.workflow import _today_nano
    limit = ui.daily_budget_aiu()
    spent = _today_nano()
    return {"view": "budget", "window": "last 24 hours", "nano_aiu": spent,
            "limit_aiu": limit,
            "over": bool(limit is not None and spent / 1e9 > limit)}


# ── Analysis ─────────────────────────────────────────────────────────

def anomalies(days: int) -> dict:
    from .cli.analysis import _anomalies_data
    return {"view": "anomalies", **_anomalies_data(days)}


def health(repo: str = ".") -> dict:
    from .cli.analysis import _health_data
    return {"view": "health", **_health_data(repo)}


def patterns(days: int) -> dict:
    from .cli.analysis import _patterns_data
    return {"view": "patterns", **_patterns_data(days)}


# ── Operations ───────────────────────────────────────────────────────

def doctor() -> dict:
    from .cli.ops import _doctor_data
    return {"view": "doctor", **_doctor_data()}


def rollup(days: int) -> dict:
    """Counts and rates only — see `cli.ops._rollup_data` for what is left out."""
    from .cli.ops import _rollup_data
    return {"view": "rollup", **_rollup_data(days)}


def emit(payload: dict, fmt: str = "json") -> None:
    """Write a payload to stdout as JSON or as CSV of its one list."""
    if fmt == "csv":
        _emit_csv(payload)
        return
    json.dump(_stamped(payload), sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def _stamped(payload: dict) -> dict:
    """The payload with a note of when it was taken and by what.

    Exported figures outlive the terminal they were taken in: they land in a
    spreadsheet, get pasted into a review, and are read months later by
    someone who has no idea which day's store they came from. A reading
    without a date is a reading nobody can check, so every JSON document
    carries one — and the tool version with it, because the same view can
    learn to count something differently between releases.
    """
    from . import __version__

    stamped = {"view": payload.get("view")} if "view" in payload else {}
    stamped["generated"] = _dt.datetime.now().astimezone().isoformat(
        timespec="seconds")
    stamped["tool"] = f"cs {__version__}"
    stamped.update(payload)
    return stamped


def _emit_csv(payload: dict) -> None:
    """The payload's main table as CSV — the rows, not the summary around them.

    CSV has no way to express a document, so this picks the one list of
    objects a payload is *about* and writes that. A view whose answer is a
    handful of totals rather than rows has no useful CSV, and says so rather
    than inventing a one-row file that looks like data.
    """
    rows = next(
        (
            value
            for value in payload.values()
            if isinstance(value, list) and value and isinstance(value[0], dict)
        ),
        None,
    )
    if not rows:
        print(
            f"error: '{payload.get('view')}' has no table to write as CSV — "
            "use --json for this view",
            file=sys.stderr,
        )
        raise SystemExit(1)
    # Every key any row carries, in the order they were first seen: rows from
    # the same view can differ (a search hit has a snippet, a plain listing
    # does not) and a short row must not silently lose its columns.
    fields: dict[str, None] = {}
    for row in rows:
        fields.update(dict.fromkeys(row))
    writer = csv.DictWriter(sys.stdout, fieldnames=list(fields))
    writer.writeheader()
    writer.writerows(rows)


def transcript_markdown(
    detail: tuple, session_id: str, turns: list[tuple], nano: int | None,
    skills: list[tuple] | None = None, agents: list[tuple] | None = None,
    subagents: list[tuple] | None = None,
) -> str:
    """One session as Markdown — for sharing, archiving, or feeding to a model.

    The transcript reader is built for a terminal and a person. This is the
    same conversation as a document: portable, diffable, pasteable into a
    pull request or an incident write-up, and small enough to hand back to a
    model when you want a past session summarised.

    The kit it used goes in the header rather than being left for a reader
    to reconstruct from the turns. Which skills ran and what the delegated
    work cost is the first thing anyone reviewing a session asks, and a
    document that makes them scroll for it is a document that gets skimmed.

    Masked on the way out, exactly like every drawn view — a transcript
    written to a file is more exposed than one on a screen, not less.
    """
    summary, repo, cwd, branch, created, updated = detail
    lines = [
        f"# {redact.one_line(redact.redact(summary or '(no summary)'))}",
        "",
        f"- **Session**: `{session_id}`",
        f"- **Repository**: {repo or '-'}",
        f"- **Branch**: {branch or '-'}",
        f"- **Directory**: `{cwd or '-'}`",
        f"- **Started**: {created}",
        f"- **Last active**: {updated}",
        f"- **Turns**: {len(turns)}",
    ]
    if nano:
        lines.append(f"- **Credits**: {nano / 1_000_000_000:.2f} AIU")

    kit: list[str] = []
    for label, rows in (("Skill", skills or []), ("Agent", agents or [])):
        for name, turn, _quote, how in rows:
            verdict = "ran" if how == "ran" else "named only"
            kit.append(f"| {label} | `{name}` | {verdict} | turn {turn} |")
    for short_id, model, calls, agent_nano, ms, first, last in (subagents or []):
        span = f"turn {first}" if first == last else f"turns {first}–{last}"
        kit.append(
            f"| Sub-agent | `{short_id}` | {model}, {calls} calls, "
            f"{agent_nano / 1_000_000_000:.2f} AIU, {ms / 1000:.0f}s | {span} |"
        )
    if kit:
        lines += [
            "", "## Skills & agents used", "",
            "| Kind | Name | Evidence | Where |",
            "|---|---|---|---|",
            *kit,
            "",
            "*`ran` is recorded by the CLI. `named only` was inferred from the "
            "text. Sub-agents have no recorded name — the store keeps only the "
            "id of the call that launched them.*",
        ]

    lines += ["", "---", ""]
    for index, prompt, reply, stamp in turns:
        lines.append(f"## Turn {index}")
        if stamp:
            lines.append(f"*{stamp}*")
        # The same two marks the reader draws. A Markdown transcript is read
        # by scrolling, and "Prompt"/"Reply" are the two words a scroll goes
        # straight past — the point of a speaker label is to be findable
        # without being read.
        lines += ["", "### 👤 You", "",
                  redact.redact(prompt or "").strip() or "*(empty)*"]
        lines += ["", "### 🤖 Copilot", "",
                  redact.redact(reply or "").strip() or "*(no reply recorded)*", ""]
    return "\n".join(lines) + "\n"
