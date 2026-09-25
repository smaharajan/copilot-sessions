"""Practice / Improve commands (``standup``, ``coach``, ``rhythm``)."""

from __future__ import annotations

import shutil
from collections import Counter

from .. import (
    db,
    practice,
    redact,
    signals,
    ui,
)
from ._common import (
    _LEVELS,
    _PERIODS,
    _capture,
    _cell,
    _fit_columns,
    _item,
    _note,
    _page,
    _page_report,
    _sort_note,
    _sort_report,
    _visible,
    _window_label,
)

_STANDUP_MOVED = 8
_STANDUP_HANDOFFS = 6
_STANDUP_RISK_CAP = 40


def cmd_standup(days: int = 1) -> bool:
    """Offline daily brief — what moved, handoffs, and light risks.

    No network and no LLM: everything comes from the store the same way the
    other Improve views do. Default window is one day (`last 24 hours`), which
    is what a standup is for; `cs standup 7` or `all` widens it.
    """
    return _page(_capture(lambda: _render_standup(days)))


def _render_standup(days: int) -> None:
    conn = db.connect()
    try:
        rows = db.recent_sessions(conn, days)
        timeline = db.timeline(conn, days)
        totals = db.cost_totals(conn, days)
        handoff_rows = signals.handoffs(conn)
        # Autonomy is a full-store scan; only ask when the window is small
        # enough that the answer is worth the cost of reading every session.
        window_ids = {row[0] for row in rows}
        risk_rows: list[dict] = []
        if 0 < len(window_ids) <= _STANDUP_RISK_CAP:
            risk_rows = [
                row for row in signals.autonomy(conn)
                if row["id"] in window_ids and row["verdict"] in ("yes", "high")
            ]
    finally:
        conn.close()

    visible = _visible(rows, show_all=False)
    session_count = len(visible)
    turns = sum(day_turns for _day, _sessions, day_turns, _spend in timeline)
    spend = totals.get("nano_aiu", 0) if totals else sum(
        day_spend for _day, _sessions, _turns, day_spend in timeline
    )
    handoffs_in = [row for row in handoff_rows if row["id"] in window_ids]

    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4

    print()
    print(ui.rule(inner, f"Standup · {_window_label(days)} · "
                         f"{session_count:,} session"
                         f"{'' if session_count == 1 else 's'}"))
    print()

    if not visible and not turns and not spend and not handoffs_in:
        print(f"  {ui.MUTED}Nothing recorded in this window.{ui.RST}")
        print()
        _standup_footer(inner)
        print()
        return

    # Activity — omit empty lines rather than printing zeros as findings.
    print(ui.heading("Activity", ui.ACCENT, inner))
    print(ui.field("sessions", f"{session_count:,}"))
    if turns:
        print(ui.field("turns", f"{turns:,}"))
    if spend:
        print(ui.field("spend", f"{ui.fmt_aiu(spend)} AIU"))
    if not turns and not spend:
        print(f"  {ui.MUTED}No turns or spend in this window.{ui.RST}")
    print()

    moved = [
        row for row in visible
        if (row[2] or "").strip()
    ][:_STANDUP_MOVED]
    if moved:
        print(ui.heading("What moved", ui.MINT, inner))
        for row in moved:
            sid, active, summary, repo, _cwd, turns_n, nano = row[:7]
            title = redact.one_line(redact.redact(summary or "")) or "(untitled)"
            bits = []
            if repo:
                bits.append(redact.one_line(redact.redact(repo)))
            if turns_n:
                bits.append(f"{turns_n} turns")
            if nano:
                bits.append(f"{ui.fmt_aiu(nano)} AIU")
            if not bits:
                bits.append(active)
            print(f"    {ui.SKY}{sid[:8]}{ui.RST}  "
                  f"{ui.trunc(title, max(18, inner - 12))}")
            print(f"      {ui.MUTED}{ui.trunc(' · '.join(bits), inner - 6)}"
                  f"{ui.RST}")
        print()

    if handoffs_in:
        print(ui.heading("Handoffs", ui.SKY, inner))
        for row in handoffs_in[:_STANDUP_HANDOFFS]:
            title = redact.one_line(redact.redact(row["summary"] or "")) or "(untitled)"
            role = row["role"]
            print(f"    {ui.SKY}{row['id'][:8]}{ui.RST}  {role:<9} "
                  f"{ui.trunc(title, max(16, inner - 22))}")
        if len(handoffs_in) > _STANDUP_HANDOFFS:
            extra = len(handoffs_in) - _STANDUP_HANDOFFS
            print(f"    {ui.MUTED}+{extra} more · cs handoff{ui.RST}")
        print()

    if risk_rows:
        print(ui.heading("Risks", ui.AMBER, inner))
        print(ui.field(
            "yolo",
            f"{len(risk_rows):,} session"
            f"{'' if len(risk_rows) == 1 else 's'} ran unattended",
        ))
        for row in risk_rows[:5]:
            title = redact.one_line(redact.redact(row["summary"] or "")) or "(untitled)"
            print(f"    {ui.AMBER}{row['id'][:8]}{ui.RST}  "
                  f"{ui.trunc(title, max(16, inner - 14))}")
        print()

    _standup_footer(inner)
    print()


def _standup_footer(inner: int) -> None:
    _note("Drill in: cs timeline · cs coach · cs handoff · cs yolo · cs audit",
          inner)


# How loudly a finding is worth reading. The bar is not decoration: it is the
# share of the sample the finding covers, which is the only thing that tells
# "nine sessions did this" apart from "nine sessions did this, out of eleven".

def cmd_coach(days: int = 30, sort_by: str | None = None,
              descending: bool | None = None) -> bool:
    """What the record says about how the work is being done.

    The one view in cs that grades the person rather than the setup. Every
    finding names the sample it came from and shows real examples, because a
    habit you cannot see an instance of is an accusation rather than a
    finding.
    """
    return _page_report(
        "coach",
        lambda column, down: _capture(lambda: _render_coach(days, column, down)),
        sort_by, descending,
    )


def _render_coach(days: int, column: str = "severity",
                  descending: bool = False) -> None:
    conn = db.connect()
    snap = practice.snapshot(conn, days)
    conn.close()
    findings, scores = practice.review(snap)
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4

    print()
    print(ui.rule(inner, f"Practice · {_window_label(days)} · "
                         f"{len(snap.sessions):,} sessions"))
    print()
    if not snap.sessions:
        print(f"  {ui.MUTED}No sessions in this window.{ui.RST}")
        print()
        return

    counts = Counter(found.severity for found in findings)
    print(ui.field("read", f"{len(snap.sessions):,} sessions · "
                           f"{len(snap.turns):,} turns"))
    if snap.calls():
        print(ui.field("calls", f"{snap.calls():,} to the models"))
    if findings:
        spread = " · ".join(f"{counts[level]} {level}"
                            for level in ("high", "medium", "low")
                            if counts[level])
        summary = f"{len(findings)} habits · {spread}"
        print(ui.field("found", ui.trunc(summary, inner - 11)))
    else:
        print(ui.field("found", "nothing worth naming here"))
    print()

    print(ui.heading("Scores", ui.ACCENT, inner))
    # The bar is the last thing to keep its size: the number is the answer,
    # and on a narrow window the bar becomes a hint rather than a gauge.
    # Coloured by the score rather than by the ramp — a meter that goes red
    # is the whole point of a meter.
    steps = max(4, min(20, inner - 26))
    for group in practice.GROUPS:
        score = scores[group]
        colour = ui.MINT if score >= 80 else ui.AMBER if score >= 55 else ui.ROSE
        print(f"    {colour}{score:>4}{ui.RST}  {group:<17} "
              f"{ui.meter(score / 100, steps, colour)}")
    print()

    if not findings:
        print(f"  {ui.MINT}Every rule came back clean for this window.{ui.RST}")
        print()
        _coach_footnote(inner)
        print(_sort_note("coach", column, descending, inner))
        print()
        return

    findings = _sort_report(findings, "coach", column, descending)
    print(ui.heading(f"What to change · {len(findings)}", ui.ACCENT))
    print()
    # The group and the share are worth having and worth losing: on a narrow
    # window the name of the habit is the only part that cannot go.
    spans = _fit_columns(inner - 2, 11, [("group", 16), ("share", 11)],
                         least=18, flex="name")
    for found in findings:
        colour, _ = _LEVELS[found.severity]
        tail = ""
        if spans["group"]:
            tail += " " + _cell(found.group, spans["group"])
        if spans["share"]:
            tail += " " + _cell(f"{found.count:,}/{found.total:,}",
                                spans["share"], ">")
        head = _cell(found.name, spans["name"])
        print(f"    {colour}● {found.severity:<7}{ui.RST}{ui.BOLD}{head}{ui.RST}"
              f"{ui.MUTED}{tail}{ui.RST}".rstrip())
        _item(redact.redact(found.headline), inner - 6, indent=6)
        _item(redact.redact(found.fix), inner - 6, marker="→", colour=colour,
              indent=6)
        for line in found.evidence:
            print(f"        {ui.MUTED}"
                  f"{ui.trunc(redact.redact(line), inner - 8)}{ui.RST}")
        print()
    _coach_footnote(inner)
    print(_sort_note("coach", column, descending, inner))
    print()


def _coach_footnote(inner: int) -> None:
    _note(
        f"Each group starts at 100 and every finding costs its severity — "
        f"high {practice.COST['high']}, medium {practice.COST['medium']}, "
        f"low {practice.COST['low']}. Nothing else moves the number, so the "
        f"list above is the whole calculation.", inner
    )
    _note(
        "Rules stay quiet below their minimum sample: silence here means "
        "'not enough to say', never 'nothing to find'.", inner
    )


def cmd_rhythm(days: int = 30) -> bool:
    """When the work happens — described, not judged.

    Late nights and weekends are counted and shown without comment. A run at
    23:00 may be a deadline, a timezone or a scheduled job, and the store
    cannot tell which, so this view declines to guess.
    """
    return _page(_capture(lambda: _render_rhythm(days)))


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


_RHYTHM_FLOOR = 25


def _widen_hint(days: int) -> str:
    """The next window or two worth suggesting when this one came back empty.

    Named windows only, so the suggestion is something the reader can also
    reach with a keypress on the landing screen rather than a number they
    would have to invent. All-time is left out because the caller offers it
    separately, and offering it twice in one sentence reads like a stutter.
    """
    wider = [d for _key, d, _label, _short in _PERIODS if d > days]
    return " or ".join(f"'cs rhythm {d}'" for d in wider[:2])


def _render_rhythm(days: int) -> None:
    conn = db.connect()
    snap = practice.snapshot(conn, days)
    conn.close()
    beat = practice.rhythm(snap)
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4

    print()
    print(ui.rule(inner, f"Rhythm · {_window_label(days)}"))
    print()
    if not beat["turns"]:
        # Two different nothings, and they want different answers. The old
        # text blamed the schema for both, which sent anyone with a simply
        # quiet month looking for a database problem that was not there.
        #
        # `snap.sessions` is already filtered to sessions that recorded a
        # turn (see practice.snapshot — a session with no turns is a launch,
        # not a habit), so empty means "nothing was asked in this window"
        # rather than "no session row exists".
        if not snap.sessions:
            print(f"  {ui.MUTED}Nothing was asked in this window.{ui.RST}")
            print()
            hint = _widen_hint(days)
            _note((f"Try a longer one — {hint} — or " if hint else "Try ")
                  + "'cs rhythm all' for every record.", inner)
        else:
            print(f"  {ui.MUTED}No turn in this window carries a "
                  f"timestamp.{ui.RST}")
            print()
            _note("Older stores predate turns.timestamp; without it there is "
                  "no hour to put the work at.", inner)
        print()
        return

    total = beat["turns"]
    # A percentage is a claim about a tendency, and a tendency needs enough
    # turns to be one. At nine turns a single Saturday afternoon reads as
    # "67% weekend work", which is arithmetically true and completely
    # misleading. Below the floor the view still draws — the histogram and
    # the counts are facts at any size — it just stops phrasing them as
    # shares and says why.
    thin = total < _RHYTHM_FLOOR
    active, run = beat["days_active"], beat["longest_streak"]
    print(ui.field("turns", f"{total:,} on {active} working "
                            f"day{'' if active == 1 else 's'}"))
    print(ui.field("span", f"{beat['first']} → {beat['last']}"))
    # "1 days, back to back" was describing a single day as a streak. One day
    # is not back to back with anything.
    print(ui.field("streak", f"{run} days, back to back" if run > 1
                             else "no day followed another"))
    busiest, busy_count = beat["busiest_day"]
    if busiest:
        print(ui.field("busiest", f"{busiest} · {busy_count} turns"))
    late = beat["late_night"]
    weekend = beat["weekend"]

    def share(count: int, when: str) -> str:
        body = f"{count:,} turns {when}"
        return body if thin else f"{body} ({count / total:.0%})"

    print(ui.field("late", share(late, "22:00–05:00")))
    print(ui.field("weekend", share(weekend, "Sat/Sun")))
    if beat["median_ms"]:
        print(ui.field("slowest", f"{beat['median_ms'] / 1000:.0f}s median · "
                                  f"{beat['p90_ms'] / 1000:.0f}s p90"))
    print()
    if thin:
        _note(f"{total} turns is too few to read as a pattern, so the counts "
              f"above are left as counts. Shares appear from "
              f"{_RHYTHM_FLOOR} turns.", inner)
        print()

    print(ui.heading("Hour of day", ui.ACCENT, inner))
    hours = beat["hours"]
    peak = max(hours.values()) if hours else 1
    span = max(12, inner - 18)
    for hour in range(24):
        count = hours.get(hour, 0)
        # Night hours are marked rather than coloured: the point is to make
        # the block of late work visible at a glance without claiming it is
        # bad, which is a judgement this report does not get to make.
        mark = f"{ui.MUTED}·{ui.RST}" if hour >= 22 or hour < 5 else " "
        print(f"    {ui.MUTED}{hour:02d}{ui.RST} {mark} "
              f"{ui.bar(count, peak, span, pad=True)} "
              f"{ui.MUTED}{count or ''}{ui.RST}")
    print()

    print(ui.heading("Day of week", ui.ACCENT, inner))
    weekdays = beat["weekdays"]
    peak = max(weekdays.values()) if weekdays else 1
    for index, name in enumerate(_WEEKDAYS):
        count = weekdays.get(index, 0)
        # The weekend keeps its own colour: it is the one distinction on this
        # chart that is not about size, and the ramp is already saying size.
        colour = ui.AMBER if index >= 5 else ""
        print(f"    {ui.MUTED}{name}{ui.RST} "
              f"{ui.bar(count, peak, span, colour=colour, pad=True)} "
              f"{ui.MUTED}{count or ''}{ui.RST}")
    print()
    _note("Times are local, converted from the UTC the store records. "
          "The slowest figure is the longest single call in a turn. "
          "Scheduled runs hidden by .cs-ignore are left out, so this is "
          "when you were working.", inner)
    print()
