"""Spend and activity reports: cost, efficiency, stats, timeline, repos."""

from __future__ import annotations

import shutil
import sys

from .. import (
    db,
    redact,
    ui,
)
from ._common import (
    _REPOS_HEADS,
    _TIMELINE_HEADS,
    _bar,
    _capture,
    _cell,
    _chart_spans,
    _fit_columns,
    _item,
    _mark_column,
    _note,
    _page,
    _page_report,
    _sort_cost,
    _sort_note,
    _sort_report,
    _thousands,
    _weekday,
    _why,
    _why_hint,
    _window_label,
)


def cmd_repos(sort_by: str | None = None, descending: bool | None = None) -> bool:
    return _page_report(
        "repos",
        lambda column, down: _capture(lambda: _render_repos(column, down)),
        sort_by, descending,
    )


def _render_repos(column: str = "sessions", descending: bool = True) -> None:
    conn = db.connect()
    rows = db.repos(conn)
    conn.close()
    rows = _sort_report(rows, "repos", column, descending)
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    print()
    print(ui.rule(inner, f"Repositories · {len(rows)}"))
    print()
    if not rows:
        print(f"  {ui.MUTED}No repositories recorded.{ui.RST}")
        print()
        return
    # The shared table shape, like every other report: this one was laid out
    # by hand at a fixed 75 columns and ran off any window narrower than that.
    # 4 is the indent and the gap after the name column.
    spans = _fit_columns(inner - 2, 4,
                         [("share", 12), ("active", 11), ("turns", 7),
                          ("credits", 8), ("sessions", 8)],
                         least=14, flex="repo")
    columns = [("repo", "repository", "<"), ("sessions", "sessions", ">"),
               ("turns", "turns", ">"), ("credits", "credits", ">"),
               ("active", "last active", ">")]
    shown = [spec for spec in columns if spans[spec[0]]]
    heads = " ".join(_cell(head, spans[key], align) for key, head, align in shown)
    if spans["share"]:
        heads += " " + _cell("share", spans["share"])
    print(f"  {ui.MUTED}{_mark_column(heads, column, _REPOS_HEADS, descending)}{ui.RST}")
    print(f"  {ui.MUTED}{'─' * ui.cells(heads)}{ui.RST}")
    peak = max((row[1] or 0 for row in rows), default=0)
    for repo, count, total_turns, nano_aiu, last in rows:
        values = {
            "repo": (redact.one_line(redact.redact(repo)), ""),
            "sessions": (f"{count:,}", ui.BOLD),
            "turns": (f"{total_turns or 0:,}", ui.MUTED),
            "credits": (ui.fmt_aiu(nano_aiu), ui.VIOLET),
            "active": (last, ui.MUTED),
        }
        line = "  " + " ".join(
            _cell(values[key][0], spans[key], align, values[key][1])
            for key, _head, align in shown
        )
        if spans["share"]:
            line += " " + ui.bar(count, peak, spans["share"], track=True)
        print(line.rstrip())
    print()
    print(_sort_note("repos", column, descending, inner))
    print()


def cmd_stats(days: int | None = None) -> bool:
    """Everything the store can say about what the work produced."""
    return _page(_capture(lambda: _render_stats(days)))


def _render_stats(days: int | None) -> None:
    from datetime import date

    conn = db.connect()
    basics = db.stats(conn)
    made = db.impact(conn, days)
    repos = db.top_repos_by_output(conn)
    conn.close()

    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    span = 1
    if basics["oldest"] and basics["newest"]:
        try:
            span = max(
                (
                    date.fromisoformat(basics["newest"])
                    - date.fromisoformat(basics["oldest"])
                ).days,
                1,
            )
        except ValueError:
            pass

    scope = _window_label(days)
    print()
    print(ui.rule(inner, f"Copilot sessions · {scope}"))
    print()

    print(ui.heading("Activity", ui.ACCENT))
    print(ui.field("sessions", f"{basics['total']:,} ({basics['interactive']:,} interactive)"))
    print(ui.field("turns", f"{basics['total_turns']:,} · {basics['avg_turns']} avg per session"))
    print(ui.field("repos", f"{made['repos']} · {made['days_active']} days with activity"))
    print(ui.field("pace", f"{basics['total'] / span:.1f} sessions/day over {span} days"))
    print(ui.field("range", f"{basics['oldest']} → {basics['newest']}"))
    print()

    print(ui.heading("What it produced", ui.MINT))
    print(ui.field("commits",
                   f"{made['commits']:,} recorded ({made['unique_commits']:,} distinct)"))
    print(ui.field("PRs", f"{made['prs']:,} recorded ({made['unique_prs']:,} distinct)"))
    print(ui.field("files", f"{made['files_created']:,} created · {made['files_edited']:,} edited"))
    print(ui.field("handoffs", f"{made['checkpoints']:,} checkpoints written"))
    print()

    if made["nano_aiu"]:
        sent = made["input_tokens"] + made["cache_read_tokens"]
        cache_share = f"{made['cache_read_tokens'] / sent * 100:.0f}%" if sent else "-"
        print(ui.heading("What it cost", ui.VIOLET))
        print(ui.field("credits", f"{ui.fmt_aiu(made['nano_aiu'])} AIU"))
        print(ui.field("tokens", f"{_thousands(made['input_tokens'])} in · "
                                 f"{_thousands(made['output_tokens'])} out · "
                                 f"{_thousands(made['reasoning_tokens'])} reasoning"))
        print(ui.field("cache", f"{_thousands(made['cache_read_tokens'])} read "
                                f"({cache_share} of tokens sent)"))
        print(ui.field("time", f"{made['model_hours']}h of model time"))
        print()

    if made["delegated_calls"] or made["compactions"]:
        print(ui.heading("How it was done", ui.AMBER))
        # Labels stay inside the 9-column gutter; a longer one pushes its
        # value right and breaks the alignment of the whole view.
        print(ui.field("agents", f"{made['delegated_calls']:,} sub-agent calls "
                                 f"over {made['delegated_tasks']:,} delegated tasks"))
        print(ui.field("context", f"{made['compactions']:,} re-summarisations"))
        print()

    if repos:
        print(ui.heading("Where it landed", ui.ACCENT, inner))
        peak = repos[0][1]
        span, gauge = _chart_spans(inner, 20)
        share = f"{'share':<{gauge}} " if gauge else ""
        print(f"    {ui.MUTED}{'refs':>4}  {'repository':<{span}} "
              f"{share}{'sessions':>8}{ui.RST}".rstrip())
        for repo, refs, sessions in repos:
            name = redact.one_line(repo.split("/")[-1] if "/" in repo else repo)
            print(f"    {ui.MINT}{refs:>4}{ui.RST}  {ui._fit(name, span):<{span}}"
                  f" {ui.bar(refs, peak, gauge, pad=True)} "
                  f"{ui.MUTED}{sessions:>8,}{ui.RST}")
        print()

    if basics.get("top_model") and basics["top_model"][1]:
        model, nano = basics["top_model"]
        model = redact.one_line(model)
        print(ui.field("model", f"{model} ({ui.fmt_aiu(nano)} AIU)"))
    if basics["busiest_day"]:
        day, count = basics["busiest_day"]
        print(ui.field("busiest", f"{day} ({count} sessions)"))
    print(ui.field("detail", "cs cost · cs agents · cs skills · cs repos"))
    print()


def cmd_timeline(days: int = 30, sort_by: str | None = None,
                 descending: bool | None = None) -> bool:
    return _page_report(
        "timeline",
        lambda column, down: _capture(lambda: _render_timeline(days, column, down)),
        sort_by, descending,
    )


def _render_timeline(days: int, column: str = "day",
                     descending: bool = False) -> None:
    conn = db.connect()
    rows = db.timeline(conn, days)
    conn.close()
    rows = _sort_report(rows, "timeline", column, descending)
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    print()
    print(ui.rule(inner, f"Working days · {_window_label(days)}"))
    print()
    if not rows:
        print(f"  {ui.MUTED}No sessions in range.{ui.RST}")
        print()
        return
    # The bar tracks turns, not sessions. Turns are the unit of work asked
    # for; sessions are the unit of *filing* it, and a day spent in one long
    # session would otherwise draw as the emptiest bar on the chart. When a
    # store has no per-turn timestamps at all the series is flat zero, and
    # the chart falls back to sessions rather than drawing nothing.
    by_turns = any(r[2] for r in rows)
    peak = max((r[2] if by_turns else r[1]) for r in rows) or 1
    # The shared table shape, like every other report. Credits are dropped
    # first because the AI-spend view reports them in far more detail anyway;
    # the bar absorbs what is left, because four numbers with nothing to
    # compare them against is a table, not a chart. 14 is the indent plus the
    # day column, which is the one thing every row must keep.
    spans = _fit_columns(inner - 2, 14,
                         [("credits", 9), ("turns", 7), ("sessions", 8)],
                         least=8, flex="share")
    spans["day"] = 12
    columns = [("day", "day", "<"), ("sessions", "sessions", ">"),
               ("turns", "turns", ">"), ("credits", "credits", ">")]
    shown = [spec for spec in columns if spans[spec[0]]]
    heads = " ".join(_cell(head, spans[key], align) for key, head, align in shown)
    heads += " " + _cell("share of turns" if by_turns else "share",
                         spans["share"])
    print(f"  {ui.MUTED}{_mark_column(heads, column, _TIMELINE_HEADS, descending)}{ui.RST}")
    print(f"  {ui.MUTED}{'─' * ui.cells(heads)}{ui.RST}")
    for day, sessions, turns, nano in rows:
        values = {
            "day": (_weekday(day), ui.MUTED),
            "sessions": (f"{sessions:,}", ui.PAPER),
            "turns": (f"{turns:,}", ui.PAPER),
            "credits": (ui.fmt_aiu(nano), ui.VIOLET),
        }
        line = "  " + " ".join(
            _cell(values[key][0], spans[key], align, values[key][1])
            for key, _head, align in shown
        )
        print(f"{line} "
              f"{ui.bar(turns if by_turns else sessions, peak, spans['share'], track=True)}")
    print()
    # Totals, because the question after "which day was busiest" is always
    # "and what did the window come to" — and adding a column of bars in your
    # head is exactly the arithmetic a report is meant to have already done.
    # Written through _note so it wraps rather than running off a narrow
    # window: it is the one line here whose length depends on the data.
    #
    # The spend total is decided by the data, not by whether the credits
    # column survived _fit_columns. A narrow window is a reason to drop a
    # column, not a reason to stop reporting what the window cost.
    active = len(rows)
    turns_total = sum(row[2] for row in rows)
    spend_total = sum(row[3] for row in rows)
    parts = [f"{active} working day{'' if active == 1 else 's'}",
             f"{sum(row[1] for row in rows):,} sessions",
             f"{turns_total:,} turns"]
    if spend_total:
        parts.append(f"{ui.fmt_aiu(spend_total)} AIU")
    if turns_total:
        rate = turns_total / active
        parts.append(f"{rate:.0f} turn{'' if rate < 1.5 else 's'} a day")
    _note(" · ".join(parts), inner)
    print()
    print(_sort_note("timeline", column, descending, inner))
    print()


_SPEND_NAME = 22


_SPEND_BAR = 18


_COST_DAYS = 14


def _spend_spans(inner: int, note: int) -> tuple[int, int]:
    """(name, bar) for a spend section, given what its numbers need.

    The name and the bar are the only two elastic things in the row, and the
    bar yields first: which model spent it is the answer, the bar is how the
    answer looks. Below about fifty columns there is no bar at all, which is
    the same trade every other table here makes.

    Fixed cost is 4 indent + 8 amount + three 2-column gaps + the note.
    """
    spare = inner - 18 - note
    name = max(10, min(_SPEND_NAME, spare - 8))
    gauge = spare - name
    return name, gauge if gauge >= 6 else 0


def _spend_row(amount: str, name: str, bar: str, note: str,
               span: int = _SPEND_NAME) -> str:
    # `name` is a model or a repository, both recorded from the environment
    # rather than typed here, so it is stripped before it is measured into
    # the column — every spend section shares this row, and so shares the
    # guarantee. `bar` arrives already the width of its column: see _bar.
    name = redact.one_line(name)
    gap = f"  {bar}" if bar else ""
    return (
        f"    {ui.VIOLET}{amount:>8}{ui.RST}  "
        f"{ui._fit(name, span):<{span}}"
        f"{gap}  {ui.DIM}{note}{ui.RST}"
    )


def _spend_header(name: str, note: str, span: int = _SPEND_NAME,
                  gauge: int = _SPEND_BAR) -> str:
    """Name the columns once, so the numbers under them need no explaining."""
    share = f"  {'share':<{gauge}}" if gauge else ""
    return (
        f"    {ui.MUTED}{'spend':>8}  {name:<{span}}{share}  {note}{ui.RST}"
    )


def cmd_cost(days: int = 30, sort_by: str | None = None,
             descending: bool | None = None) -> bool:
    return _page_report(
        "cost",
        lambda column, down: _capture(lambda: _render_cost(days, column, down)),
        sort_by, descending,
    )


def _render_cost(days: int = 30, column: str = "spend",
                 descending: bool = True) -> None:
    conn = db.connect()
    if not db.has_usage(conn):
        conn.close()
        print(
            "error: this session store records no AI usage "
            "(no assistant_usage_events table)",
            file=sys.stderr,
        )
        sys.exit(1)
    totals = db.cost_totals(conn, days)
    by_model = db.cost_by_model(conn, days)
    by_repo = db.cost_by_repo(conn, days)
    by_day = db.cost_by_day(conn, days)
    top = db.cost_top_sessions(conn, days)
    conn.close()

    # Every section shares a shape — spend, a name, a count — so one choice
    # sorts all four the same way.
    by_model = _sort_cost(by_model, "model", column, descending)
    by_repo = _sort_cost(by_repo, "repo", column, descending)
    top = _sort_cost(top, "session", column, descending)
    # Per day is sorted *before* it is cut, which it was not. Trimming to the
    # trailing fortnight first meant the sort only ever reordered the last
    # fourteen days: the header said "sorted by spend ↓" and the dearest day
    # on record could not appear under it — not in a year, not in all time,
    # because it was dropped before the sort ever saw it. Every window wider
    # than about a fortnight showed the same fortnight.
    recorded = len(by_day)
    by_day = _sort_cost(by_day, "day", column, descending)[:_COST_DAYS]

    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    print()
    print(ui.rule(inner, f"AI spend · {_window_label(days)}"))
    print()
    if not totals.get("calls"):
        print(f"  {ui.DIM}No AI usage recorded in this range.{ui.RST}")
        print()
        return

    # Cache reads are counted separately from input tokens, so the ratio is
    # over what was sent in total.
    cache = totals["cache_read_tokens"]
    sent = totals["input_tokens"] + cache
    cache_pct = f"{cache / sent * 100:.0f}%" if sent else "-"
    seconds = totals["duration_ms"] / 1000
    avg_call = seconds / totals["calls"] if totals["calls"] else 0

    print(ui.heading("Totals", ui.ACCENT, inner))
    print(ui.field("spend", f"{ui.VIOLET}{ui.fmt_aiu(totals['nano_aiu'])} AIU{ui.RST}"))
    print(ui.field("calls", f"{totals['calls']:,} across {totals['sessions']:,} sessions"))
    print(ui.field("tokens", f"{_thousands(totals['input_tokens'])} in · "
                             f"{_thousands(totals['output_tokens'])} out · "
                             f"{_thousands(totals['reasoning_tokens'])} reasoning"))
    print(ui.field("cache", f"{_thousands(cache)} read ({cache_pct} of tokens sent)"))
    print(ui.field("time", f"{seconds / 3600:.1f}h of model time · {avg_call:.1f}s per call"))
    # No `issues` row. It read "0 errors · 7 filtered" — thirteen events out of
    # thirty-nine thousand, on a page about where three hundred thousand
    # credits went, and a content filter is not a spend fact at all. Every
    # other line in this block is a quantity you can act on. `cs efficiency`
    # already breaks the same two numbers out by finish reason, under "Calls
    # that ended badly", which is the page whose question they answer.
    print()

    # The timing columns are the first thing a narrow window loses: which
    # model, and what it cost, is what this section is for — how long its
    # calls took is a second question, and `cs stats` also answers it.
    print(ui.heading("By model", ui.VIOLET, inner))
    timings = inner >= 62
    span, gauge = _spend_spans(inner, 23 if timings else 9)
    print(_spend_header("model",
                        f"{'calls':>9}{'avg':>7}{'first':>7}" if timings
                        else f"{'calls':>9}", span, gauge))
    peak = max((m[2] for m in by_model), default=0)
    for model, calls, nano, avg_ms, ttft_ms in by_model:
        avg = f"{(avg_ms or 0) / 1000:.1f}s"
        first = f"{ttft_ms / 1000:.1f}s" if ttft_ms else "-"
        print(_spend_row(
            ui.fmt_aiu(nano), model, _bar(nano, peak, gauge, ui.SKY),
            f"{calls:>9,}{avg:>7}{first:>7}" if timings else f"{calls:>9,}",
            span,
        ))
    print()

    print(ui.heading("By repository", ui.MINT, inner))
    span, gauge = _spend_spans(inner, 9)
    print(_spend_header("repository", f"{'sessions':>9}", span, gauge))
    peak = max((r[2] for r in by_repo), default=0)
    for place, sessions, nano in by_repo:
        name = place.split("/")[-1] if "/" in place else place
        print(_spend_row(
            ui.fmt_aiu(nano), name, _bar(nano, peak, gauge, ui.MINT),
            f"{sessions:>9,}", span,
        ))
    print()

    print(ui.heading("Per day" if len(by_day) == recorded
                     else f"Per day · {len(by_day)} of {recorded}",
                     ui.ACCENT, inner))
    span, gauge = _spend_spans(inner, 9)
    print(_spend_header("day", f"{'calls':>9}", span, gauge))
    peak = max((d[1] for d in by_day), default=0)
    for day, nano, calls in by_day:
        print(_spend_row(
            ui.fmt_aiu(nano), _weekday(day), _bar(nano, peak, gauge, ui.VIOLET),
            f"{calls:>9,}", span,
        ))
    print()

    print(ui.heading("Dearest sessions", ui.VIOLET, inner))
    peak = max((t[2] for t in top), default=0)
    gauge = max(0, min(_SPEND_BAR, inner - 34))
    for _sid, summary, nano in top:
        # The summary is the one field with no natural width, so it goes last
        # and the columns before it stay put.
        share = f"{_bar(nano, peak, gauge, ui.SKY)}  " if gauge else ""
        print(f"    {ui.VIOLET}{ui.fmt_aiu(nano):>8}{ui.RST}  {share}"
              f"{ui._fit(redact.one_line(redact.redact(summary)), max(10, inner - 16 - gauge))}")
    print()
    _note("Credits = AI units (AIU) spent · avg = per call · first = to "
          "first token. 'cs cost <days>' narrows the range; 'cs cost all' "
          "widens it. Per day is the top "
          f"{_COST_DAYS} for the sort in force — 'cs timeline <days>' lists "
          "every day.", inner)
    print(_sort_note("cost", column, descending, width - 4))
    print()


def cmd_efficiency(days: int = 30) -> bool:
    return _page(_capture(lambda: _render_efficiency(days)))


def _render_efficiency(days: int = 30) -> None:
    """Not what the work cost, but whether it had to cost that.

    `cs cost` is the bill: which model, which repository, which day. This is
    the verdict on it, and every section is a lever rather than a number to
    admire — the cache you are or aren't hitting, the rate multiplier you are
    paying, the latency you are waiting through, the reasoning you are buying.

    The readings are the ones the rest of the industry already agrees on
    (Claude Code's telemetry schema uses the same cache formula and the same
    p50/p95 latency split), so a number here means what it means anywhere
    else. Nothing is scored and nothing is graded: each section says what was
    measured and what it implies, and leaves the judgement to the reader.
    """
    conn = db.connect()
    if not db.has_usage(conn):
        conn.close()
        print(
            "error: this session store records no AI usage "
            "(no assistant_usage_events table)",
            file=sys.stderr,
        )
        sys.exit(1)
    reading = db.efficiency(conn, days)
    conn.close()

    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    print()
    print(ui.rule(inner, f"Efficiency · {_window_label(days)}"))
    print()
    if not reading.get("cache") and not reading.get("by_model"):
        print(f"  {ui.DIM}No AI usage recorded in this range.{ui.RST}")
        print()
        return
    if not reading.get("windowed"):
        _note("This store does not date its usage records, so every reading "
              "below covers all of time rather than the window asked for.", inner)
        print()

    cache = reading.get("cache") or {}
    if cache.get("hit_rate") is not None:
        rate = cache["hit_rate"]
        # Coloured by what the number means, not by how big it is: past about
        # half the input coming from cache a long session is behaving itself,
        # and under a quarter is the single cheapest thing left to fix.
        colour = ui.MINT if rate >= 0.5 else ui.AMBER if rate >= 0.25 else ui.ROSE
        print(ui.heading("Cache", ui.ACCENT, inner))
        print(ui.field("hit rate",
                       f"{colour}{rate * 100:.0f}%{ui.RST}  "
                       f"{ui.meter(rate, max(10, min(28, inner - 30)), colour)}"))
        print(ui.field("tokens", f"{_thousands(cache['read'])} read from cache · "
                                 f"{_thousands(cache['written'])} written to it · "
                                 f"{_thousands(cache['fresh'])} sent fresh"))
        _why("Cached input is the cheapest input there is, and the share of it "
              "is the biggest single lever on what a long session costs. Read = "
              "cache_read / (fresh + cache_read + cache_write), the same "
              "definition the published agent telemetry schemas use.", inner)
        print()

    multipliers = reading.get("multipliers") or []
    if len(multipliers) > 1:
        print(ui.heading("Rate multiplier", ui.VIOLET, inner))
        total = sum(nano for _rate, _calls, nano in multipliers) or 1
        span, gauge = _spend_spans(inner, 9)
        print(_spend_header("multiplier", f"{'calls':>9}", span, gauge))
        peak = max(nano for _rate, _calls, nano in multipliers)
        for rate, calls, nano in multipliers:
            colour = ui.ROSE if rate > 1 else ui.MINT
            print(_spend_row(
                ui.fmt_aiu(nano), f"{rate:g}× · {nano / total * 100:.0f}% of spend",
                _bar(nano, peak, gauge, colour), f"{calls:>9,}", span,
            ))
        _why("Every call is billed at a rate. A premium multiplier earning its "
              "keep on hard work is money well spent; the same multiplier on "
              "lookups and file reads is the most common way a bill runs away "
              "without anyone deciding that it should.", inner)
        print()

    latency = reading.get("latency") or {}
    if latency.get("p50") is not None:
        print(ui.heading("First token", ui.SKY, inner))
        p50, p95 = latency["p50"] / 1000, latency["p95"] / 1000
        print(ui.field("p50", f"{p50:.1f}s  {ui.DIM}half of calls answer faster{ui.RST}"))
        colour = ui.ROSE if p95 >= 15 else ui.AMBER if p95 >= 8 else ui.MINT
        print(ui.field("p95", f"{colour}{p95:.1f}s{ui.RST}  "
                              f"{ui.DIM}the slow tail, over {latency['calls']:,} "
                              f"calls{ui.RST}"))
        _why("The mean hides exactly the tail that makes a tool feel slow, so "
              "this is quoted the way latency is quoted everywhere else: the "
              "middle, and the bad end.", inner)
        print()

    if cache.get("reasoning_share") is not None and cache["reasoning"]:
        print(ui.heading("Reasoning", ui.INDIGO, inner))
        share = cache["reasoning_share"]
        print(ui.field("share", f"{share * 100:.0f}% of output tokens were "
                                f"reasoning ({_thousands(cache['reasoning'])})"))
        effort = reading.get("effort") or []
        if effort:
            print(ui.field("effort", " · ".join(
                f"{name} {count:,}" for name, count in effort[:5])))
        _why("Reasoning tokens are output tokens you pay for and never read. "
              "Worth it on genuinely hard work; on routine edits a lower effort "
              "setting buys the same answer for less.", inner)
        print()

    by_model = reading.get("by_model") or []
    if by_model:
        print(ui.heading("By model", ui.MINT, inner))
        timings = inner >= 62
        span, gauge = _spend_spans(inner, 21 if timings else 9)
        print(_spend_header("model",
                            f"{'calls':>9}{'cache':>6}{'first':>6}" if timings
                            else f"{'calls':>9}", span, gauge))
        peak = max(row[2] for row in by_model)
        for model, calls, nano, cached, offered, ttft in by_model:
            hit = f"{cached / offered * 100:.0f}%" if offered else "-"
            first = f"{ttft / 1000:.1f}s" if ttft else "-"
            print(_spend_row(
                ui.fmt_aiu(nano), model, _bar(nano, peak, gauge, ui.SKY),
                f"{calls:>9,}{hit:>6}{first:>6}" if timings else f"{calls:>9,}",
                span,
            ))
        print()

    finish = reading.get("finish") or []
    # 'tool_calls' and 'stop' are both a call that ended the way it meant to;
    # anything else is one that did not, and only that is worth a line.
    unhappy = [(name, count) for name, count in finish
               if name not in ("stop", "tool_calls", "(none)")]
    if unhappy:
        print(ui.heading("Calls that ended badly", ui.ROSE, inner))
        for name, count in unhappy:
            _item(f"{name}: {count:,}", inner, marker="·", colour=ui.ROSE)
        print()

    _why("Every reading here comes from the store's own usage records — "
         "nothing on this screen is inferred from the text of a turn.", inner)
    _note("cs efficiency <days> narrows the range · cs efficiency all "
          "widens it", inner)
    _why_hint(inner)
    print()
