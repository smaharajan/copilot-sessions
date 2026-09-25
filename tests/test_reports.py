"""Reports and their shape: sorting, spend windows, efficiency and the machine-readable output."""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
from contextlib import redirect_stderr
from datetime import datetime, timedelta
from pathlib import Path

from support import CALM, PARENT, StoreTest, _add_governance_rows

from cs.cli import _COST_DAYS
from cs.ui import cells as ui_cells


class ReportSortTest(StoreTest):
    """Every report table can be re-ordered, and says how."""

    def setUp(self):
        super().setUp()
        _add_governance_rows(Path(self._tmp.name))
        conn = sqlite3.connect(Path(self._tmp.name) / "session-store.db")
        # A second repo, so 'sorted by repo' has something to order.
        conn.executemany(
            "INSERT INTO session_files (session_id, file_path, tool_name) VALUES (?,?,?)",
            [(CALM, "/tmp/c/aaa-first.py", "edit"),
             (CALM, "/tmp/c/zzz-last.py", "create"),
             (PARENT, "/tmp/c/zzz-last.py", "edit")],
        )
        conn.commit()
        conn.close()

    def _rows(self, out: str, marker: str) -> list[str]:
        """A report's data lines: after its header, before its footer."""
        lines = [line.rstrip() for line in out.split("\n") if line.strip()]
        start = next(i for i, line in enumerate(lines) if line.strip().startswith(marker))
        rows = []
        for line in lines[start + 1:]:
            if line.lstrip().startswith("─"):
                continue
            if any(stop in line for stop in ("Sorted by", "--sort", "←/→", "cs files")):
                break
            rows.append(line)
        return rows

    def test_repos_sorts_by_each_of_its_columns(self):
        code, out = self._run("repos", "--sort", "repo", "--asc")
        self.assertEqual(code, 0)
        names = [row.split()[0] for row in self._rows(out, "repository")]
        self.assertEqual(names, sorted(names, key=str.lower))

        _, out = self._run("repos", "--sort", "turns")
        turns = [int(row.split()[-4]) for row in self._rows(out, "repository")]
        self.assertEqual(turns, sorted(turns, reverse=True))

    def test_direction_can_be_reversed(self):
        _, down = self._run("repos", "--sort", "repo", "--desc")
        _, up = self._run("repos", "--sort", "repo", "--asc")
        self.assertNotEqual(
            self._rows(down, "repository")[0], self._rows(up, "repository")[0]
        )

    def test_audit_sorts_by_turn_as_well_as_count(self):
        code, out = self._run("audit", "--sort", "turn")
        self.assertEqual(code, 0)
        self.assertIn("Sorted by turn", out)

    def test_yolo_sorts_riskiest_first_by_default(self):
        _, out = self._run("yolo")
        self.assertIn("Sorted by risk ↓", out)
        self.assertLess(out.index("Try it unsupervised"), out.index("One prompt, long run"))

    def test_handoff_sorts_by_role(self):
        code, out = self._run("handoff", "--sort", "role", "--asc")
        self.assertEqual(code, 0)
        self.assertLess(out.index("emitted"), out.index("received"))

    def test_every_sortable_report_states_its_columns(self):
        for command, column in (
            # skills/profiles are left out: the fixture has no assets on disk,
            # and a report with nothing in it has nothing to sort.
            ("repos", "sessions"), ("timeline", "day"),
            ("yolo", "risk"), ("handoff", "active"), ("audit", "risk"),
            ("cost", "spend"),
        ):
            with self.subTest(command=command):
                code, out = self._run(command)
                self.assertEqual(code, 0)
                self.assertIn(f"Sorted by {column}", out)
                self.assertIn("--sort", out)

    def test_an_unknown_column_names_the_real_ones(self):
        """A typo gets the list, not a stack trace."""
        cases = {
            "repos": "credits", "yolo": "steps", "cost": "calls",
            "audit": "found", "handoff": "chain", "timeline": "day",
        }
        for command, offered in cases.items():
            with self.subTest(command=command):
                buf = io.StringIO()
                with redirect_stderr(buf):
                    code, _ = self._run(command, "--sort", "nonsense")
                self.assertEqual(code, 1)
                self.assertIn(offered, buf.getvalue())

    def test_files_is_a_listing_and_takes_a_listing_s_columns(self):
        """`cs files <path>` returns sessions, so it sorts like any listing —
        and `edits` was only ever a column of the leaderboard that went."""
        code, _ = self._run("files", "*.py", "--sort", "credits")
        self.assertEqual(code, 0)
        code, _ = self._run("files", "*.py", "--sort", "edits")
        self.assertEqual(code, 1)
        code, _ = self._run("files", "--sort", "credits")   # no path to look up
        self.assertEqual(code, 1)

    def test_flags_and_sorting_can_be_combined(self):
        code, out = self._run("yolo", "--all", "--sort", "turns")
        self.assertEqual(code, 0)
        self.assertIn("A supervised session", out)
        self.assertIn("Sorted by turns", out)

    def test_conflicting_directions_are_refused(self):
        code, _ = self._run("repos", "--asc", "--desc")
        self.assertEqual(code, 1)

    def test_a_missing_sort_value_is_refused(self):
        code, _ = self._run("repos", "--sort")
        self.assertEqual(code, 1)

    def test_the_footer_fits_a_narrow_terminal(self):
        import re
        import shutil
        from unittest import mock

        from cs import cli

        for columns in (46, 60, 100):
            with self.subTest(columns=columns):
                size = os.terminal_size((columns, 30))
                with mock.patch.object(shutil, "get_terminal_size", return_value=size):
                    note = re.sub(
                        r"\x1b\[[0-9;]*m", "",
                        cli._sort_note("repos", "sessions", True, min(columns, 96) - 4),
                    )
                for line in note.split("\n"):
                    self.assertLessEqual(len(line), columns)
                self.assertIn("--sort", note)  # never dropped, only wrapped


class DataFormatTest(StoreTest):
    """--json, --csv, export and completion: the machine-readable surface."""

    def test_every_advertised_view_emits_json(self):

        from cs import export
        # Most views are reached by typing their own name. Three are not:
        # `sessions` is `recent`, `search` needs an argument before it has an
        # answer, and `skills-by-repo` is a flag on `skills` rather than a
        # command of its own.
        reach = {"sessions": ("recent",), "search": ("search", "portal"),
                 "skills-by-repo": ("skills", "--by-repo"),
                 "similar": ("similar", "portal"),
                 "file-history": ("files", "globe.js", "--history"),
                 "diff": ("diff", "sess-alpha", "sess-empty")}
        for view in export.VIEWS:
            args = reach.get(view, (view,))
            code, out = self._run(*args, "--json")
            self.assertEqual(code, 0, view)
            payload = json.loads(out)
            self.assertIn("view", payload, view)
            self.assertIn("generated", payload, view)
            self.assertTrue(payload["tool"].startswith("cs "), view)

    def test_json_sessions_carry_the_kit_counts(self):
        code, out = self._run("recent", "--json")
        self.assertEqual(code, 0)
        rows = json.loads(out)["sessions"]
        alpha = next(r for r in rows if r["id"] == "sess-alpha")
        self.assertEqual(alpha["subagents_run"], 1)
        self.assertEqual(alpha["skills_referenced"], 0)

    def test_csv_has_a_header_and_a_row_per_session(self):
        import csv
        code, out = self._run("recent", "--csv")
        self.assertEqual(code, 0)
        rows = list(csv.reader(io.StringIO(out)))
        self.assertIn("id", rows[0])
        self.assertTrue(any(r[0] == "sess-alpha" for r in rows[1:]))

    def test_an_unsupported_view_says_so_rather_than_printing_a_screen(self):
        code, err = self._run_err("audit", "--json")
        self.assertNotEqual(code, 0)
        self.assertIn("--json", err)

    def test_export_writes_markdown_with_both_sides_of_the_turn(self):
        code, out = self._run("export", "sess-alpha")
        self.assertEqual(code, 0)
        self.assertIn("# Build Three.js portal", out)
        self.assertIn("make a portal", out)
        self.assertIn("sure, here it is", out)

    def test_export_json_is_structured_turns(self):
        code, out = self._run("export", "sess-alpha", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["id"], "sess-alpha")
        self.assertEqual(payload["view"], "session")
        self.assertEqual(len(payload["turns"]), 2)

    def test_completion_covers_the_three_shells_and_refuses_the_rest(self):
        for shell, marker in (("bash", "complete"), ("zsh", "compdef"),
                              ("fish", "complete -c cs")):
            code, out = self._run("completion", shell)
            self.assertEqual(code, 0, shell)
            self.assertIn(marker, out)
            self.assertIn("efficiency", out, shell)
        code, err = self._run_err("completion", "tcsh")
        self.assertNotEqual(code, 0)
        self.assertIn("bash", err)

    def test_every_shell_offers_the_same_flags(self):
        # bash and zsh each need the flag list spelled out in full, and for a
        # while they held two hand-written copies. A flag added to one and
        # missed in the other is invisible: completion still works, it just
        # silently stops mentioning the flag on that shell.
        from cs.cli import _COMPLETION_FLAGS
        scripts = {shell: self._run("completion", shell)[1]
                   for shell in ("bash", "zsh", "fish")}
        for flag in _COMPLETION_FLAGS:
            for shell, script in scripts.items():
                with self.subTest(flag=flag, shell=shell):
                    # fish spells each flag as '-l json' rather than '--json'.
                    needle = f"-l {flag[2:]}" if shell == "fish" else flag
                    self.assertIn(needle, script)

    def test_every_completable_command_is_a_real_command(self):
        # The completion list is written by hand; a command renamed in the
        # dispatcher and not here would offer the user a word that errors.
        from cs.cli import _COMPLETION_COMMANDS
        for name in _COMPLETION_COMMANDS:
            with self.subTest(command=name):
                _, err = self._run_err(name, "--json")
                self.assertNotIn("unknown command", err.lower())

    def test_exported_text_is_redacted_like_the_screen_is(self):
        import sqlite3
        secret = "ghp_" + "b" * 36
        conn = sqlite3.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        conn.execute(
            "INSERT INTO turns VALUES (98,'sess-alpha',2,?,'ok','z')",
            (f"the token is {secret}",),
        )
        conn.commit()
        conn.close()
        code, out = self._run("export", "sess-alpha")
        self.assertEqual(code, 0)
        self.assertNotIn(secret, out)

    def test_help_lists_every_view_that_can_be_piped(self):
        """The advertised list and the working list must not drift apart."""
        from cs import export
        code, out = self._run("help")
        self.assertEqual(code, 0)
        for cmd in export.DATA_COMMANDS:
            self.assertIn(cmd, out, cmd)
            if cmd in ("export", "search", "similar", "diff"):
                continue
            self.assertEqual(self._run(cmd, "--json")[0], 0, cmd)


class EfficiencyTest(StoreTest):
    """The efficiency view, and its behaviour on a store missing columns.

    The test store predates cache_write_tokens, request_multiplier and
    reasoning_effort, which is exactly the shape a real older install has.
    Nothing here may raise; the readings that cannot be taken must simply
    be absent.
    """

    def test_it_reads_what_it_can_and_omits_what_it_cannot(self):
        from cs import db
        conn = db.connect()
        try:
            eff = db.efficiency(conn, 0)
        finally:
            conn.close()
        self.assertTrue(eff["cache"])
        # 8000 cached of 3000+8000 sent
        self.assertAlmostEqual(eff["cache"]["hit_rate"], 8000 / 11000, places=3)
        self.assertEqual(eff["latency"]["p50"], 800)
        self.assertEqual(eff["latency"]["p95"], 900)
        self.assertEqual(dict(eff["finish"]).get("error"), 1)
        self.assertEqual(len(eff["by_model"]), 2)

    def test_the_view_renders_on_the_reduced_schema(self):
        code, out = self._run("efficiency", "all")
        self.assertEqual(code, 0)
        self.assertIn("cache", out.lower())
        self.assertIn("first token", out.lower())

    def test_it_is_offered_on_the_home_screen(self):
        from cs import cli
        titles = [item[1] for item in cli._home_items()]
        self.assertIn("Efficiency", titles)
        # Commented out of the menu, but still reachable by name.
        self.assertNotIn("Timeline", titles)
        self.assertEqual(self._run("timeline", "30")[0], 0)


class SpendScopeTest(StoreTest):
    """A session's spend has to say what span it covers.

    `cs` sums every call the store ever billed to a session id. The Copilot
    CLI's own status line counts only the run it is sitting in. Put the two
    on screen together — which is what happens the moment anyone reads this
    tool beside the terminal it describes — and the totals disagree by
    whatever the earlier runs cost, plus compaction, plus sub-agents.

    Neither number is wrong. But a bare '8.8k AIU' cannot say so, and the
    reader's first conclusion is that one of the two is lying. These tests
    pin the two things that stop that: the indirect slice printed as a
    measured finding, and the explanation of the span available on request.
    """

    SPAN = "whole life of the session"

    def _split(self, out: str) -> str:
        """Just the work-split block, so a match cannot come from elsewhere."""
        self.assertIn("How the work was done", out)
        return out.split("How the work was done", 1)[1].split("Models", 1)[0]

    def test_indirect_spend_is_named_and_measured(self):
        """The slice nobody typed a prompt for gets a number, not an inference.

        The fixture session spends 1.5 AIU as the user and 2.5 as a sub-agent,
        so indirect is 2.5 of 4.0 — 62%. Asserting the arithmetic rather than
        just the label is the point: a percentage that is merely present can
        still be nonsense.
        """
        code, out = self._run("show", "sess-alpha")
        self.assertEqual(code, 0)
        split = self._split(out)
        self.assertIn("indirect", split)
        self.assertIn("no prompt behind it", split)
        self.assertIn("62% of spend", split)

    def test_indirect_row_stays_inside_the_block_it_joins(self):
        """It must not become the line that decides how wide the block is.

        The chart above it is a fixed 59 cells. A sentence wider than that
        would push the whole section past a window the chart itself fits.
        """
        strip = re.compile(r"\x1b\[[0-9;]*m")
        code, out = self._run("show", "sess-alpha")
        self.assertEqual(code, 0)
        rows = [strip.sub("", line)
                for line in self._split(out).splitlines() if line.strip()]
        indirect = next(line for line in rows if "indirect" in line)
        widest = max(ui_cells(line) for line in rows if "indirect" not in line)
        self.assertLessEqual(ui_cells(indirect), widest)

    def test_no_indirect_row_when_nothing_was_indirect(self):
        """A session whose spend was all direct says nothing about overhead.

        A row reading '0 AIU · 0% of spend' is not reassurance, it is a line
        to skip on every future read.
        """
        import sqlite3 as sq

        path = Path(os.environ["COPILOT_HOME"]) / "session-store.db"
        conn = sq.connect(path)
        conn.execute(
            "UPDATE assistant_usage_events SET initiator='agent' "
            "WHERE initiator IN ('sub-agent','compaction')"
        )
        conn.commit()
        conn.close()
        code, out = self._run("show", "sess-alpha")
        self.assertEqual(code, 0)
        self.assertNotIn("indirect", self._split(out))

    def test_the_span_is_explained_only_on_request(self):
        """The lesson obeys the same rule every other lesson does."""
        code, quiet = self._run("show", "sess-alpha")
        self.assertEqual(code, 0)
        code, asked = self._run("show", "sess-alpha", "--why")
        self.assertEqual(code, 0)
        self.assertNotIn(self.SPAN, quiet)
        self.assertIn(self.SPAN, asked)
        # The comparison that prompts the question in the first place.
        self.assertIn("status line", asked)

    def test_show_offers_the_flag_it_withheld(self):
        """`show` holds back a lesson, so it must advertise that it did.

        Without this the page is not denser, it is just missing something —
        and the flag that restores it is one nobody has been shown.
        """
        code, quiet = self._run("show", "sess-alpha")
        self.assertEqual(code, 0)
        self.assertIn("--why  explains how to read this", quiet)
        code, asked = self._run("show", "sess-alpha", "--why")
        self.assertEqual(code, 0)
        self.assertNotIn("explains how to read this", asked)

    def test_explaining_the_span_does_not_move_a_number(self):
        """--why adds sentences. It must never touch the arithmetic."""
        import re

        digits = re.compile(r"\d[\d,.]*")
        code, quiet = self._run("show", "sess-alpha")
        self.assertEqual(code, 0)
        code, asked = self._run("show", "sess-alpha", "--why")
        self.assertEqual(code, 0)
        self.assertEqual(digits.findall(self._split(quiet)),
                         digits.findall(self._split(asked)))


class CostPerDayTest(StoreTest):
    """The Per day section of `cs cost`, and the window it claims to cover.

    It was cut to the trailing fourteen days *before* it was sorted, so the
    sort could only ever reorder that fortnight. The header said "sorted by
    spend ↓" and the dearest day on record did not appear under it — in any
    window, at any width, because it had been dropped before the sort ran.
    """

    DAYS = 20          # more than the section draws, so the cut is exercised

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(Path(self._tmp.name) / "session-store.db")
        # The dearest day is also the oldest, which is the whole point: it
        # sits outside the trailing fortnight, so it is reachable only if
        # the sort runs before the cut.
        conn.executemany(
            """INSERT INTO assistant_usage_events
               (session_id, model, total_nano_aiu, input_tokens, output_tokens,
                cache_read_tokens, reasoning_tokens, duration_ms,
                time_to_first_token_ms, finish_reason, content_filter_triggered,
                initiator, created_at)
               VALUES ('sess-alpha','gpt-5.5',?,10,10,10,0,100,10,'stop',0,'user',
                       datetime('now', ?))""",
            [(9_000_000_000_000 if day == self.DAYS - 1 else 1_000_000_000_000,
              f"-{day} days") for day in range(self.DAYS)],
        )
        conn.commit()
        conn.close()

    def _per_day(self, out: str) -> str:
        self.assertIn("Per day", out)
        return out.split("Per day", 1)[1].split("Dearest", 1)[0]

    def test_the_dearest_day_appears_when_sorted_by_spend(self):
        """The bug in one assertion: the top row of a section sorted by spend
        has to be the dearest day in the window, not the dearest of the last
        fortnight."""
        code, out = self._run("cost", "all", "--sort", "spend", "--desc")
        self.assertEqual(code, 0)
        oldest = (datetime.now() - timedelta(days=self.DAYS - 1)).strftime("%d %b")
        rows = [line for line in self._per_day(out).splitlines() if "9.0k" in line]
        self.assertTrue(rows, "the dearest day is missing from a spend sort")
        self.assertIn(oldest.lstrip("0"), rows[0])

    def test_the_section_says_when_it_is_showing_a_top_slice(self):
        """A table holding 14 of 20 days must not read as all of them —
        that is what made a capped section look like a complete one."""
        code, out = self._run("cost", "all")
        self.assertEqual(code, 0)
        head = self._per_day(out).splitlines()[0]
        self.assertIn(f"{_COST_DAYS} of ", head)
        drawn = [line for line in self._per_day(out).splitlines()
                 if re.search(r"\d\d \w\w\w", line)]
        self.assertEqual(len(drawn), _COST_DAYS)

    def test_a_window_that_fits_is_not_labelled_a_slice(self):
        """Nothing was cut, so nothing should claim to have been."""
        code, out = self._run("cost", "3")
        self.assertEqual(code, 0)
        self.assertNotIn(" of ", self._per_day(out).splitlines()[0])

    def test_sorting_by_day_ascending_reaches_the_oldest_day(self):
        """The other direction of the same defect: the earliest day in the
        window was unreachable however the report was sorted."""
        code, out = self._run("cost", "all", "--sort", "name", "--asc")
        self.assertEqual(code, 0)
        oldest = (datetime.now() - timedelta(days=self.DAYS - 1)).strftime("%d %b")
        first = next(line for line in self._per_day(out).splitlines()
                     if re.search(r"\d\d \w\w\w", line))
        self.assertIn(oldest.lstrip("0"), first)


class KitColumnTest(StoreTest):
    """Skills referenced and sub-agents run, counted per session.

    Two numbers that look alike on screen and are not alike underneath: the
    skill count is inferred from what the transcript mentions, the sub-agent
    count is read off the store's own billing records. The tests below hold
    both to the standard the view claims for them.
    """

    def _mention(self, text: str) -> None:
        import sqlite3
        conn = sqlite3.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        conn.execute(
            "INSERT INTO turns VALUES (99,'sess-alpha',2,?,'ok','z')", (text,)
        )
        conn.commit()
        conn.close()

    def test_listing_counts_sub_agents_exactly(self):
        from cs import db
        conn = db.connect()
        try:
            self.assertEqual(dict(db.subagents_by_session(conn)), {"sess-alpha": 1})
            self.assertEqual(db.subagent_runs(conn, "sess-alpha"), 1)
            self.assertEqual(db.subagent_runs(conn, "sess-empty"), 0)
        finally:
            conn.close()

    def test_a_skill_on_disk_is_only_counted_when_a_session_names_it(self):
        from cs import db
        names = ["deploy", "review"]

        conn = db.connect()
        try:
            self.assertEqual(db.assets_by_session(conn, names), {})
            self.assertEqual(db.assets_used(conn, names), 0)
        finally:
            conn.close()

        self._mention("run the deploy skill on this")
        conn = db.connect()
        try:
            self.assertEqual(db.assets_by_session(conn, names), {"sess-alpha": 1})
            self.assertEqual(db.assets_used(conn, names), 1)
        finally:
            conn.close()

    def test_reference_counts_cache_matches_fresh_scan(self):
        """Cached asset hits agree with an uncached pass on a small fixture."""
        from cs import db

        self._mention("run the deploy skill on this")
        # Clear any prior fingerprint so the first call builds the cache.
        db._TURN_HIT_CACHE.clear()
        db._SKILLS_INVOKED_CACHE.clear()

        conn = db.connect()
        try:
            first = db.reference_counts(conn, ["deploy", "review"])
            # Second call must hit the cache and return the same counts.
            second = db.reference_counts(conn, ["deploy", "review"])
            self.assertEqual(first, second)
            self.assertEqual(first["deploy"], 1)
            self.assertEqual(first["review"], 0)
            # Force a miss by clearing, then rebuild — still identical.
            db._TURN_HIT_CACHE.clear()
            db._SKILLS_INVOKED_CACHE.clear()
            third = db.reference_counts(conn, ["deploy", "review"])
            self.assertEqual(first, third)
            rows = db.sessions_for_asset(conn, "deploy")
            self.assertEqual([r[0] for r in rows], ["sess-alpha"])
        finally:
            conn.close()


    def test_merely_saying_a_word_is_not_a_reference(self):
        """The scan is deliberately narrow — a bare noun must not count."""
        from cs import db
        self._mention("we should deploy on friday")
        conn = db.connect()
        try:
            self.assertEqual(db.assets_by_session(conn, ["deploy"]), {})
        finally:
            conn.close()

    def test_the_columns_appear_and_sort(self):
        code, out = self._run("recent", "7")
        self.assertEqual(code, 0)
        self.assertIn("skills", out)
        self.assertIn("agents", out)
        code, out = self._run("recent", "7", "--sort", "agents")
        self.assertEqual(code, 0)
        self.assertIn("sorted by agents", out)

    def test_show_identifies_each_sub_agent_by_what_it_ran(self):
        """The store keeps no agent name, so the row has to earn its keep."""
        code, out = self._run("show", "sess-alpha")
        self.assertEqual(code, 0)
        self.assertIn("claude-opus-4.8", out)      # what it ran on
        self.assertIn("1 calls", out)              # how much it did
        self.assertIn("gent_1", out)               # which one it was
        self.assertIn("store keeps no", out)        # and why that is all

    def test_a_seven_field_row_still_renders(self):
        """Rows grew two fields; the renderers must not require them.

        Callers build listing rows by hand in a dozen places, and a widened
        tuple is the kind of change that breaks them quietly a month later.
        """
        from cs import cli
        row = ("sess-x", "A summary", "acme/portal", "2 hours ago", 3, 1.5, 12)
        self.assertEqual(cli._kit_of(row), (0, 0))
        self.assertEqual(cli._kit_of(row + (2, 1)), (2, 1))
