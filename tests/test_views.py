"""What a view is allowed to claim: clarity, restored state, quiet
defaults and skill attribution.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from support import StoreTest


class ViewClarityTest(StoreTest):
    """brief, show and read have to be tellable apart at the point of use.

    Every one of these covers a way the three views were previously silent
    about themselves: which view you were in, which command gave you what,
    what the columns meant, and — in the brief — prose that turned out to be
    a markdown table wearing prose's clothes.
    """

    def test_each_view_names_itself_in_its_rule(self):
        """The three headers are deliberately identical, so the rule must differ."""
        for view in ("brief", "show", "read"):
            with self.subTest(view=view):
                _, out = self._run(view, "sess-alpha")
                self.assertIn(f"{view} · Build Three.js portal", out)

    def test_footers_say_what_each_sibling_gives_you(self):
        """A list of bare commands says what you can type, not what you want."""
        _, out = self._run("brief", "sess-alpha")
        self.assertIn("the full page", out)                 # show
        self.assertIn("the conversation itself", out)       # read
        self.assertIn("reopen it in Copilot CLI", out)      # resume
        # A view never advertises itself.
        self.assertNotIn("cs brief sess-alpha ", out.split("id  ")[-1])

    def test_the_asks_flag_is_offered_and_explained(self):
        """--asks existed but appeared in the footer with nothing to explain it."""
        _, out = self._run("brief", "sess-alpha")
        self.assertNotIn("--asks", out)      # only two requests: nothing hidden
        _, listed = self._run("brief", "sess-alpha", "--asks")
        self.assertIn("Every request · 2", listed)

    def test_show_short_is_a_prefix_of_show_not_a_different_page(self):
        """`--short` may stop early; it may not say anything different."""
        _, short = self._run("show", "sess-alpha", "--short")
        _, full = self._run("show", "sess-alpha")
        _, brief = self._run("brief", "sess-alpha")
        # brief is the same command under an older name, character for
        # character apart from the word in the rule.
        self.assertEqual(short.replace("show ·", "brief ·", 1), brief)
        # Everything the short form says, the long form says too — up to
        # the point where the long form carries on.
        for section in ("Still open", "First request", "What got done",
                        "Last request", "Shipped"):
            with self.subTest(section=section):
                self.assertIn(section, short)
                self.assertIn(section, full)
        for section in ("How the work was done", "Models", "Files touched"):
            with self.subTest(section=section):
                self.assertNotIn(section, short)
                self.assertIn(section, full)

    def test_the_short_form_says_where_the_rest_of_the_page_went(self):
        """Stopping early is only honest if it tells you it stopped."""
        _, short = self._run("show", "sess-alpha", "--short")
        self.assertIn("cs show sess-alpha", short)
        self.assertIn("the full page", short)
        # And the full page has nothing left to point at, so it doesn't.
        _, full = self._run("show", "sess-alpha")
        self.assertNotIn("cs show sess-alpha", full)

    def test_show_takes_the_flags_it_advertises(self):
        code, _ = self._run("show", "sess-alpha", "--short", "--asks")
        self.assertEqual(code, 0)
        code, err = self._run("show", "sess-alpha", "--nonsense")
        self.assertEqual(code, 1)

    def test_evidence_quotes_are_read_as_prose_not_markup(self):
        """A quote cut from a table is a row of pipes, not a sentence."""
        import sqlite3 as sq

        home = Path(os.environ["COPILOT_HOME"])
        (home / "skills" / "table-skill").mkdir(parents=True, exist_ok=True)
        (home / "skills" / "table-skill" / "SKILL.md").write_text("x")
        conn = sq.connect(home / "session-store.db")
        conn.execute(
            "INSERT INTO turns VALUES (77,'sess-alpha',3,'go',?,'z')",
            ("| Kit | Path |\n|---|---|\n| **skill table-skill** | `~/x/S.md` |",),
        )
        conn.commit()
        conn.close()
        _, out = self._run("show", "sess-alpha")
        self.assertIn("table-skill", out)
        self.assertNotIn("|---|", out)
        self.assertNotIn("**", out)

    def test_the_page_does_not_re_list_the_conversation(self):
        """`cs read` is the conversation, in full and both sides. The page
        used to end in a turn index as well — a third rendering of a list it
        already shows the ends of and `--asks` already shows entire — so on
        a hundred-turn session the summary closed with a hundred lines of
        truncated prompt."""
        _, out = self._run("show", "sess-alpha")
        self.assertNotIn("Conversation ·", out)
        self.assertNotIn("reply size", out)
        # And the way to the conversation is still on the page.
        self.assertIn("cs read sess-alpha", out)

    def test_the_header_counts_turns_and_not_requests(self):
        """A turn with no prompt is still a turn — it is compaction, or a
        tool loop. The header says 'turns', so it counts them; `--asks`
        says 'requests', so it drops the empties. The count behind the
        header used to come from a query that pulled 2,000 characters of
        every prompt purely to take its length."""
        import sqlite3

        store = Path(os.environ["COPILOT_HOME"]) / "session-store.db"
        with sqlite3.connect(store) as writer:
            writer.execute(
                "INSERT INTO turns (session_id, turn_index, user_message,"
                " assistant_response) VALUES ('sess-alpha', 9, '', 'compacted')"
            )
        _, out = self._run("show", "sess-alpha")
        self.assertIn("3 turns", out)
        _, asks = self._run("show", "sess-alpha", "--asks")
        self.assertIn("Every request · 2", asks)

    def test_the_numbered_list_says_how_to_open_a_turn(self):
        """A column of numbers you cannot act on is decoration. `--asks` is
        the only numbered list left, so it is where that has to be said."""
        _, out = self._run("show", "sess-alpha", "--asks")
        self.assertIn("Every request", out)
        self.assertIn("--turn N", out)
        # Not on the page that has no numbers on it.
        _, plain = self._run("show", "sess-alpha")
        self.assertNotIn("--turn N", plain)

    def test_help_documents_the_options_the_views_accept(self):
        """--asks, --turn and the transcript alias were all undocumented."""
        _, out = self._run("help")
        self.assertIn("--asks", out)
        self.assertIn("--turn N", out)
        self.assertIn("transcript", out)

    def test_markdown_tables_do_not_leak_into_prose(self):
        """A reply ending in a table printed '| Page | Version |' as its outcome."""
        from cs.cli import _plain

        rows = _plain(
            "Here is the result:\n\n| Page | Version |\n|------|:-------:|\n"
            "| Guidelines | v9 |\n"
        )
        self.assertEqual(
            rows, ["Here is the result:", "Page · Version", "Guidelines · v9"]
        )

    def test_short_ids_are_used_only_when_they_shorten(self):
        """Clipping 'sess-alpha' to 'sess-alp' loses the meaning to save two chars."""
        from cs.cli import _short_ref

        self.assertEqual(_short_ref("sess-alpha"), "sess-alpha")

    def test_the_transcript_marks_who_is_speaking(self):
        """Two bold words that differ only by the word are not a speaker label."""
        _, out = self._run("read", "sess-alpha", "--turn", "0")
        self.assertIn("👤", out)
        self.assertIn("🤖", out)
        self.assertIn("You", out)
        self.assertIn("Copilot", out)
        # The marks stand where the accent bar would be, never beside it.
        for line in out.splitlines():
            if "👤" in line or "🤖" in line:
                self.assertNotIn("▌", line)

    def test_speaker_marks_fall_back_to_ascii(self):
        """A terminal that cannot draw them should not be given hollow boxes."""
        previous = os.environ.get("CS_GLYPHS")
        os.environ["CS_GLYPHS"] = "ascii"
        try:
            import importlib

            from cs import ui

            importlib.reload(ui)
            self.assertEqual(ui.YOU_MARK, ">")
            self.assertEqual(ui.COPILOT_MARK, "*")
        finally:
            if previous is None:
                os.environ.pop("CS_GLYPHS", None)
            else:
                os.environ["CS_GLYPHS"] = previous
            import importlib

            from cs import ui

            importlib.reload(ui)

    def test_the_markdown_export_marks_speakers_too(self):
        """'Prompt' and 'Reply' are the two words a scroll goes straight past."""
        _, out = self._run("export", "sess-alpha")
        self.assertIn("### 👤 You", out)
        self.assertIn("### 🤖 Copilot", out)

    def test_read_is_reachable_as_transcript(self):
        """The alias is documented now, so it has to keep working."""
        code, out = self._run("transcript", "sess-alpha", "--turn", "0")
        self.assertEqual(code, 0)
        self.assertIn("Turn 0", out)
        self.assertNotIn("Turn 1", out)


class RestoredViewTest(StoreTest):
    """The views that were once taken off the menu, and what earns them back.

    Each was shelved for a real reason. Timeline counted sessions, which is
    the activity metric every productivity framework warns against reading
    as value. Rhythm reported shares off however few turns it found. Hooks
    looked like configuration the first-party CLI already prints. These
    cover the specific thing that changed in each case, so restoring them is
    a claim the suite can check rather than a preference.
    """

    def test_a_working_day_is_sessions_turns_and_spend(self):
        """Sessions alone was the vanity metric. Three numbers is a ledger."""
        self._stamp()
        code, out = self._run("timeline", "all")
        self.assertEqual(code, 0)
        for column in ("day", "sessions", "turns", "credits"):
            self.assertIn(column, out)
        # The store's whole spend, on the totals line — the arithmetic the
        # report is there to have already done. Singular, because "1 turns a
        # day" is the kind of seam that makes a report look unfinished.
        self.assertIn("4.00 AIU", out)
        self.assertIn("1 turn a day", out)

    def test_a_day_with_turns_but_no_session_start_still_gets_a_row(self):
        """Work carries over midnight. A session opened on Monday and worked
        in on Tuesday has Tuesday's turns and no Tuesday session start, and
        dropping that day would hide the work rather than the gap."""
        self._stamp("2025-03-08 14:00:00")
        code, out = self._run("timeline", "all")
        self.assertEqual(code, 0)
        # Sessions are stamped today; the turns are stamped in March. Both
        # days get a row, each with the half of the ledger it actually has.
        self.assertIn("Sat 08 Mar", out)
        self.assertIn("2 working days", out)

    def test_a_working_day_can_be_sorted_by_what_it_cost(self):
        """A ledger you cannot sort by its own columns is a picture."""
        for column in ("day", "sessions", "turns", "credits"):
            with self.subTest(column=column):
                code, out = self._run("timeline", "all", "--sort", column)
                self.assertEqual(code, 0)
                self.assertIn(f"Sorted by {column}", out)

    def _conn(self):
        return sqlite3.connect(
            Path(os.environ["COPILOT_HOME"]) / "session-store.db")

    def _stamp(self, when: str = "2025-03-08 14:00:00") -> None:
        """Give the fixture's turns a clock.

        The store fixture stamps turns 'x' and 'y' on purpose, so rhythm
        normally lands in its no-timestamp branch. These tests are about the
        branches past it, so they have to supply the thing that is missing.
        """
        conn = self._conn()
        conn.execute("UPDATE turns SET timestamp = ?", (when,))
        conn.commit()
        conn.close()

    def test_rhythm_reports_counts_not_shares_when_the_window_is_thin(self):
        """At two turns a single Saturday reads as '100% weekend work'.

        True, and completely misleading. Below the floor the histogram still
        draws — a count is a fact at any size — but nothing is phrased as a
        tendency.
        """
        self._stamp()
        code, out = self._run("rhythm", "all")
        self.assertEqual(code, 0)
        self.assertIn("too few to read as a pattern", out)
        self.assertNotIn("%", out.split("Hour of day")[0])

    def test_rhythm_reports_shares_once_there_are_enough_turns(self):
        """The floor has to lift, or it is just a permanent refusal."""
        conn = self._conn()
        conn.executemany(
            "INSERT INTO turns (session_id, turn_index, user_message, "
            "assistant_response, timestamp) VALUES ('sess-alpha',?,?,?,?)",
            [(100 + n, "ask", "ok", "2025-03-08 14:00:00") for n in range(30)],
        )
        conn.commit()
        conn.close()
        code, out = self._run("rhythm", "all")
        self.assertEqual(code, 0)
        self.assertNotIn("too few to read as a pattern", out)
        self.assertIn("Sat/Sun (100%)", out)

    def test_rhythm_blames_an_empty_window_on_the_window(self):
        """It used to blame the schema, and send people hunting a database
        bug that was not there."""
        conn = self._conn()
        conn.execute("DELETE FROM turns")
        conn.commit()
        conn.close()
        code, out = self._run("rhythm", "all")
        self.assertEqual(code, 0)
        self.assertIn("Nothing was asked in this window", out)
        self.assertNotIn("timestamp", out)
        self.assertIn("cs rhythm all", out)

    def test_rhythm_still_names_the_schema_when_that_is_the_reason(self):
        """The old message was not wrong, only over-applied. The fixture
        stamps its turns 'x', which is exactly the case it describes."""
        code, out = self._run("rhythm", "all")
        self.assertEqual(code, 0)
        self.assertIn("carries a timestamp", out)

    def test_one_day_is_not_a_streak(self):
        self._stamp()
        code, out = self._run("rhythm", "all")
        self.assertEqual(code, 0)
        self.assertNotIn("1 days, back to back", out)
        self.assertIn("no day followed another", out)

    def test_hooks_names_the_commands_whose_script_is_gone(self):
        """The one thing reading the config cannot tell you, and the reason
        this view is worth keeping next to `copilot plugins list`."""
        home = Path(os.environ["COPILOT_HOME"])
        (home / "settings.json").write_text(json.dumps({"hooks": {
            "SessionStart": [{"hooks": [
                {"type": "command", "command": "./scripts/gone.sh"}]}],
        }}))
        code, out = self._run("hooks")
        self.assertEqual(code, 0)
        self.assertIn("scripts/gone.sh", out)
        self.assertIn("not on disk", out)

    def test_the_working_day_ledger_pipes_somewhere(self):
        """A ledger of day, sessions, turns and spend is the one report in cs
        that people will want in a spreadsheet, which is what it was missing."""
        code, out = self._run("timeline", "all", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["view"], "timeline")
        self.assertEqual(payload["working_days"], len(payload["by_day"]))
        day = payload["by_day"][0]
        for key in ("day", "sessions", "turns", "nano_aiu"):
            self.assertIn(key, day)
        code, csv_out = self._run("timeline", "all", "--csv")
        self.assertEqual(code, 0)
        self.assertEqual(csv_out.splitlines()[0], "day,sessions,turns,nano_aiu")

    def test_a_one_day_window_is_not_called_one_days(self):
        """Every windowed report's title goes through one function, which is
        why this was worth fixing in one place and testing in one place."""
        from cs import cli

        self.assertEqual(cli._window_label(1), "last 24 hours")
        self.assertEqual(cli._window_label(30), "last 30 days")
        self.assertEqual(cli._window_label(0), "all time")
        self.assertEqual(cli._window_label(None), "all time")
        for view in ("timeline", "rhythm", "coach", "cost", "stats"):
            with self.subTest(view=view):
                code, out = self._run(view, "1")
                self.assertEqual(code, 0)
                self.assertNotIn("last 1 days", out)


class QuietByDefaultTest(StoreTest):
    """Explanations are printed when they are asked for, not on every run.

    Every report used to carry two lines under each section explaining how to
    read it. That is exactly right the first time and pure noise the
    fiftieth, and a tool aimed at people who run it daily should default to
    the fiftieth. The split is by what a sentence is *about*: a line about
    the numbers on screen is a finding and always prints; a line about how
    the view works is a lesson and waits to be asked for.
    """

    # `cs efficiency` is the densest report and holds back the most, which
    # makes it the one worth checking sentence by sentence. The config views
    # are covered separately: with nothing configured they never reach a
    # lesson at all, which is the behaviour that matters there.
    LESSONS = (
        "Cached input is the cheapest input there is",
        "The mean hides exactly the tail",
        "Reasoning tokens are output tokens you pay for",
        "comes from the store's own usage records",
    )

    def test_a_lesson_waits_to_be_asked_for(self):
        """Absent by default, present under --why — every sentence of it."""
        code, quiet = self._run("efficiency")
        self.assertEqual(code, 0)
        code, asked = self._run("efficiency", "--why")
        self.assertEqual(code, 0)
        for lesson in self.LESSONS:
            with self.subTest(lesson=lesson[:30]):
                self.assertNotIn(lesson, quiet)
                self.assertIn(lesson, asked)

    def test_hiding_the_lessons_is_what_makes_the_report_shorter(self):
        """The point of the split is height on screen, not tidiness. If the
        quiet report is not meaningfully shorter, the flag bought nothing."""
        _, quiet = self._run("efficiency")
        _, asked = self._run("efficiency", "--why")
        self.assertLess(len(quiet.splitlines()), len(asked.splitlines()) - 5)

    def test_the_findings_stay_whatever_happens(self):
        """A line about the numbers on screen is not a lesson and never
        moves behind a flag — that distinction is the whole design."""
        for args in (("efficiency",), ("efficiency", "--why")):
            with self.subTest(args=args):
                _, out = self._run(*args)
                self.assertIn("cs efficiency <days> narrows the range", out)

    def test_the_quiet_report_says_the_explanations_exist(self):
        """A compact report that never mentions the flag is not denser, it is
        missing something — and nobody finds an option they were never shown.
        The hint costs one line and it is the first thing to go once used."""
        _, quiet = self._run("efficiency")
        self.assertIn("--why", quiet)
        _, asked = self._run("efficiency", "--why")
        self.assertNotIn("--why  explains", asked)

    def test_the_hint_never_offers_an_explanation_there_is_none_of(self):
        """`cs hooks` on a machine with no hooks teaches inline, because a
        screen with nothing on it is the one moment the explanation *is* the
        report. Advertising a flag that would add nothing is worse than
        silence, so the hint tracks what was actually withheld."""
        for view in ("hooks", "mcp"):
            with self.subTest(view=view):
                _, out = self._run(view)
                self.assertIn("No ", out)
                self.assertNotIn("--why", out)

    def test_a_withheld_lesson_is_not_remembered_by_the_next_report(self):
        """The withheld flag is module state, so a report that hid nothing
        must not inherit the hint from whatever ran before it."""
        from cs import cli

        cli._WHY_WITHHELD = True
        _, out = self._run("hooks")
        self.assertNotIn("--why", out)

    def test_asking_why_never_changes_a_number(self):
        """--why adds prose to a report; it must not select, filter or round
        anything differently, or the flag would be a second code path."""

        for view in ("efficiency", "cost", "stats"):
            with self.subTest(view=view):
                _, quiet = self._run(view)
                _, asked = self._run(view, "--why")
                self.assertEqual(re.findall(r"\d[\d,.]*", quiet),
                                 re.findall(r"\d[\d,.]*", asked))

    def test_the_flag_works_wherever_it_is_typed(self):
        """It is stripped globally rather than parsed per command, so it has
        to survive sitting beside a window argument or a sort flag rather
        than being read as one of them."""
        for args in (("cost", "--why", "all"), ("cost", "all", "--why"),
                     ("hooks", "--why", "--sort", "when")):
            with self.subTest(args=args):
                code, out = self._run(*args)
                self.assertEqual(code, 0)
                self.assertNotIn("unknown", out)

    def test_the_environment_can_turn_explanations_on_for_good(self):
        """Someone learning the tool should be able to set it once rather
        than remember a flag, which is what an env var is for."""
        import os

        os.environ["CS_WHY"] = "1"
        try:
            code, out = self._run("efficiency")
            self.assertEqual(code, 0)
            self.assertIn(self.LESSONS[0], out)
        finally:
            del os.environ["CS_WHY"]


class SkillInvocationTest(StoreTest):
    """`<skill-context name="…">` — the one hard record of a skill running.

    Everything else `cs` says about skill usage is read out of transcript
    text and can be wrong. This marker is written by the CLI itself when it
    loads a skill, so a name found in it is a fact. The tests here exist to
    keep those two claims from being confused with one another, which is the
    only way the weaker one stays honest.
    """

    def _turn(self, text: str, index: int = 5) -> None:
        import sqlite3
        conn = sqlite3.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        conn.execute(
            "INSERT INTO turns (session_id, turn_index, user_message, "
            "assistant_response, timestamp) VALUES ('sess-alpha',?,?,'ok','t')",
            (index, text),
        )
        conn.commit()
        conn.close()

    def _skill(self, name: str) -> None:
        base = Path(os.environ["COPILOT_HOME"]) / "skills" / name
        base.mkdir(parents=True, exist_ok=True)
        (base / "SKILL.md").write_text(f"# {name}\n")

    def test_a_load_marker_is_read_as_a_record(self):
        from cs import db
        self._turn('<skill-context name="okr-planning">\nbody here\n')
        conn = db.connect()
        try:
            self.assertEqual(db.skills_invoked(conn, "sess-alpha"),
                             {"okr-planning": 5})
            self.assertEqual(
                db.skills_invoked_by_session(conn),
                {"sess-alpha": {"okr-planning"}},
            )
        finally:
            conn.close()

    def test_a_session_with_no_marker_claims_nothing(self):
        from cs import db
        conn = db.connect()
        try:
            self.assertEqual(db.skills_invoked(conn, "sess-alpha"), {})
            self.assertEqual(db.skills_invoked_by_session(conn), {})
        finally:
            conn.close()

    def test_ran_and_named_are_told_apart(self):
        from cs import db
        self._skill("okr-planning")
        self._skill("deploy-check")
        self._turn('<skill-context name="okr-planning">\nbody\n', 5)
        self._turn("please run the deploy-check skill next", 6)
        conn = db.connect()
        try:
            found = db.asset_evidence(
                conn, "sess-alpha", ["okr-planning", "deploy-check"]
            )
        finally:
            conn.close()
        how = {name: verdict for name, _turn, _quote, verdict in found}
        self.assertEqual(how, {"okr-planning": "ran", "deploy-check": "named"})
        # Certainty sorts first, whatever turn it happened in.
        self.assertEqual(found[0][0], "okr-planning")

    def test_a_mention_carries_the_words_it_was_read_from(self):
        from cs import db
        self._skill("deploy-check")
        self._turn("we should run the deploy-check skill before shipping", 6)
        conn = db.connect()
        try:
            (_name, turn, quote, verdict), = db.asset_evidence(
                conn, "sess-alpha", ["deploy-check"]
            )
        finally:
            conn.close()
        self.assertEqual((turn, verdict), (6, "named"))
        self.assertIn("deploy-check skill", quote)

    def test_a_load_beats_an_earlier_mention_of_the_same_skill(self):
        """Otherwise a chatty session downgrades its own hard evidence."""
        from cs import db
        self._skill("okr-planning")
        self._turn("thinking about the okr-planning skill", 3)
        self._turn('<skill-context name="okr-planning">\nbody\n', 9)
        conn = db.connect()
        try:
            (_name, turn, _quote, verdict), = db.asset_evidence(
                conn, "sess-alpha", ["okr-planning"]
            )
        finally:
            conn.close()
        self.assertEqual((turn, verdict), (9, "ran"))

    def test_show_says_which_skills_actually_ran(self):
        self._skill("okr-planning")
        self._turn('<skill-context name="okr-planning">\nbody\n')
        code, out = self._run("show", "sess-alpha")
        self.assertEqual(code, 0)
        self.assertIn("okr-planning", out)
        self.assertIn("ran", out)
        self.assertIn("recorded, not inferred", out)

    def test_the_skills_view_separates_run_from_referenced(self):
        self._skill("okr-planning")
        self._turn('<skill-context name="okr-planning">\nbody\n')
        code, out = self._run("skills")
        self.assertEqual(code, 0)
        self.assertIn("run by the CLI", out)
        self.assertIn("1 ran", out)

    def test_a_referenced_skill_with_no_load_says_none(self):
        """A blank reads as a broken column, not as a zero.

        Both skills are referenced; only one has a load marker. The row
        without one used to print nothing at all in that column, which is
        indistinguishable from the count having failed to render — the
        reading that was actually reported.
        """
        self._skill("okr-planning")
        self._skill("deploy-check")
        self._turn('<skill-context name="okr-planning">\nbody\n', 5)
        self._turn("please run the deploy-check skill next", 6)
        code, out = self._run("skills")
        self.assertEqual(code, 0)
        rows = {line.split()[1]: line for line in out.splitlines()
                if " deploy-check " in f" {line} " or " okr-planning " in f" {line} "}
        self.assertIn("1 ran", rows["okr-planning"])
        self.assertIn("none", rows["deploy-check"])

    def test_the_load_column_does_not_run_off_a_narrow_window(self):
        """The note was printed but never budgeted, so it overflowed."""
        from cs import ui
        self._skill("okr-planning")
        self._turn('<skill-context name="okr-planning">\nbody\n')
        for width in (46, 60, 72, 100):
            os.environ["COLUMNS"] = str(width)
            try:
                code, out = self._run("skills")
            finally:
                del os.environ["COLUMNS"]
            self.assertEqual(code, 0)
            for line in out.splitlines():
                self.assertLessEqual(
                    ui.cells(ui._strip(line)), width,
                    f"row wider than {width}: {line!r}",
                )

    def test_profiles_make_no_claim_they_cannot_support(self):
        """Only skills leave a load marker, so profiles must not imply one."""
        base = Path(os.environ["COPILOT_HOME"]) / "agents"
        base.mkdir(parents=True, exist_ok=True)
        (base / "reviewer.agent.md").write_text("# reviewer\n")
        self._turn("handing this to agents/reviewer")
        code, out = self._run("profiles")
        self.assertEqual(code, 0)
        self.assertIn("reviewer", out)
        self.assertNotIn("actually run by the CLI", out)

    def test_export_carries_names_not_just_counts(self):
        """A count in a pipe is a number someone must come back to explain."""
        import json
        self._skill("okr-planning")
        self._turn('<skill-context name="okr-planning">\nbody\n')
        code, out = self._run("export", "sess-alpha", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["skills"][0]["name"], "okr-planning")
        self.assertEqual(payload["skills"][0]["evidence"], "ran")
        self.assertEqual(payload["subagents"][0]["model"], "claude-opus-4.8")
        self.assertEqual(payload["subagents"][0]["calls"], 1)

    def test_markdown_export_leads_with_the_kit_it_used(self):
        self._skill("okr-planning")
        self._turn('<skill-context name="okr-planning">\nbody\n')
        code, out = self._run("export", "sess-alpha")
        self.assertEqual(code, 0)
        head = out.split("## Turn 0")[0]
        self.assertIn("## Skills & agents used", head)
        self.assertIn("| Skill | `okr-planning` | ran | turn 5 |", head)
        self.assertIn("Sub-agent", head)

    def test_a_session_with_no_kit_gets_no_empty_table(self):
        code, out = self._run("export", "sess-empty")
        self.assertEqual(code, 0)
        self.assertNotIn("Skills & agents used", out)
