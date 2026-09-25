"""Practice rules — inferences drawn across a window of sessions."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from support import StoreTest, _add_practice_rows, _build_store


class PracticeTest(StoreTest):
    """`cs coach` reads habits out of the record; these plant the habits."""

    def setUp(self):
        super().setUp()
        _add_practice_rows(Path(os.environ["COPILOT_HOME"]))

    def _findings(self, days: int = 0):
        from cs import db, practice

        conn = db.connect()
        try:
            snap = practice.snapshot(conn, days)
            return {f.rule: f for f in practice.review(snap)[0]}
        finally:
            conn.close()

    def test_thin_prompts_are_found_with_their_examples(self):
        found = self._findings()
        self.assertIn("thin-prompts", found)
        thin = found["thin-prompts"]
        self.assertGreater(thin.count, 20)
        self.assertTrue(thin.evidence, "a finding with no evidence is an accusation")

    def test_a_runaway_session_is_found(self):
        found = self._findings()
        self.assertIn("mega-sessions", found)
        self.assertEqual(found["mega-sessions"].severity, "high")

    def test_the_same_ask_three_times_is_a_skill(self):
        found = self._findings()
        self.assertIn("repeated-prompts", found)
        self.assertIn("skills", found["repeated-prompts"].fix)

    def test_frustration_is_recognised(self):
        found = self._findings()
        self.assertIn("frustration", found)

    def test_scores_are_the_findings_and_nothing_else(self):
        """The number has to be recomputable from the list printed under it."""
        from cs import db, practice

        conn = db.connect()
        try:
            snap = practice.snapshot(conn, 0)
            findings, scores = practice.review(snap)
        finally:
            conn.close()
        for group in practice.GROUPS:
            cost = sum(practice.COST[f.severity]
                       for f in findings if f.group == group)
            self.assertEqual(scores[group], max(0, 100 - cost), group)

    def test_harness_injected_text_is_not_a_prompt(self):
        from cs import practice

        typed = practice.clean(
            "<system-reminder>do not do that</system-reminder>real question"
        )
        self.assertEqual(typed, "real question")
        self.assertEqual(practice.clean("<skill-context name='x'>blah"), "")

    def test_scheduled_runs_are_left_out(self):
        """.cs-ignore already hides them from listings; practice agrees."""
        from cs import db, practice

        base = Path(os.environ["COPILOT_HOME"])
        (base / ".cs-ignore").write_text("Repeat\n")
        conn = db.connect()
        try:
            snap = practice.snapshot(conn, 0)
        finally:
            conn.close()
        self.assertFalse([s for s in snap.sessions.values()
                          if s["summary"].startswith("Repeat")])

    def test_a_rule_stays_quiet_below_its_sample(self):
        """Silence must mean 'not enough to say', never a clean bill."""
        from cs import db, practice

        tiny = Path(self._tmp.name) / "tiny"
        tiny.mkdir()
        _build_store(tiny)
        os.environ["COPILOT_HOME"] = str(tiny)
        conn = db.connect()
        try:
            findings, scores = practice.review(practice.snapshot(conn, 0))
        finally:
            conn.close()
            os.environ["COPILOT_HOME"] = self._tmp.name
        self.assertEqual(findings, [])
        self.assertTrue(all(score == 100 for score in scores.values()))

    def test_the_report_prints_its_findings(self):
        code, out = self._run("coach", "all")
        self.assertEqual(code, 0)
        self.assertIn("Scores", out)
        self.assertIn("What to change", out)
        self.assertIn("prompt quality", out)

    def test_the_report_masks_a_credential_in_its_evidence(self):
        """Evidence is raw prompt text, so the render edge has to mask it."""
        secret = "ghp_" + "B" * 36
        conn = sqlite3.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        for n in range(6):
            conn.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                (f"leak-{n}", "/tmp/p", "acme/app", "local", "main",
                 f"Leak {n}", "2026-08-03T12:00:00.000Z",
                 "2026-08-03T12:00:00.000Z"),
            )
            conn.execute(
                "INSERT INTO turns (session_id, turn_index, user_message,"
                " assistant_response, timestamp) VALUES (?,?,?,?,?)",
                (f"leak-{n}", 0, f"token={secret}", "ok",
                 "2026-08-03T12:00:00.000Z"),
            )
        conn.commit()
        conn.close()
        code, out = self._run("coach", "all")
        self.assertEqual(code, 0)
        self.assertNotIn(secret, out)

    def test_rhythm_describes_the_week(self):
        code, out = self._run("rhythm", "all")
        self.assertEqual(code, 0)
        self.assertIn("Hour of day", out)
        self.assertIn("Day of week", out)
        self.assertIn("Mon", out)

    def test_rhythm_counts_the_hour_the_work_happened(self):
        from cs import db, practice

        conn = db.connect()
        try:
            beat = practice.rhythm(practice.snapshot(conn, 0))
        finally:
            conn.close()
        self.assertGreater(beat["turns"], 0)
        self.assertEqual(sum(beat["hours"].values()), beat["turns"])
        self.assertEqual(sum(beat["weekdays"].values()), beat["turns"])

    def test_an_unknown_coach_sort_column_is_refused(self):
        code, _out = self._run("coach", "--sort", "nope")
        self.assertEqual(code, 1)

    def test_a_credential_the_cut_lands_inside_is_still_masked(self):
        """Evidence lines are cut to 70 characters. Cutting first leaves a
        fragment no pattern recognises, so the prefix printed in clear."""
        from cs import practice, redact

        token = "ghp_" + "B" * 36
        leaked = []
        for pad in range(0, 70):
            turn = practice.Turn(
                session="abcdef1234567890", index=1,
                prompt="x" * pad + " deploy with " + token + " now",
                prompt_len=1, reply_len=10, when=None,
            )
            # Masked again at the render edge, exactly as the caller does.
            if "ghp_B" in redact.redact(practice._example(turn)):
                leaked.append(pad)
        self.assertEqual(leaked, [], "a sliced credential printed in clear")

    def test_sessions_that_recorded_nothing_are_not_counted_as_practice(self):
        """The headline said 472 sessions while every finding beneath it was
        measured against 265 — the rest were launches, not work."""
        from cs import db, practice

        store = Path(os.environ["COPILOT_HOME"]) / "session-store.db"
        with sqlite3.connect(store) as writer:
            writer.execute(
                "INSERT INTO sessions (id, summary, cwd, repository,"
                " created_at, updated_at) VALUES"
                " ('empty-launch-row', '', '/tmp', '', datetime('now'),"
                " datetime('now'))"
            )
        conn = db.connect()
        try:
            snap = practice.snapshot(conn, 0)
        finally:
            conn.close()
        self.assertNotIn("empty-launch-row", snap.sessions)
        self.assertEqual(set(snap.sessions), set(snap.turns_by_session()),
                         "the headline counts sessions the rules never see")


class StandupTest(StoreTest):
    """Offline daily brief — no network, no LLM, fixture store only."""

    def test_standup_exits_cleanly_on_the_fixture_store(self):
        code, out = self._run("standup")
        self.assertEqual(code, 0)
        self.assertIn("Standup", out)
        self.assertIn("last 24 hours", out)
        self.assertNotIn("Traceback", out)

    def test_standup_mentions_session_activity_in_the_window(self):
        code, out = self._run("standup")
        self.assertEqual(code, 0)
        # Fixture sessions are dated `now`, so the default 1-day window sees them.
        self.assertTrue(
            "Build Three.js portal" in out or "AIU" in out or "sessions" in out,
            out,
        )
        self.assertIn("Build Three.js portal", out)

    def test_standup_json_carries_the_brief(self):
        import json

        code, out = self._run("standup", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["view"], "standup")
        self.assertEqual(payload["window_days"], 1)
        for key in ("sessions", "turns", "nano_aiu", "moved", "handoffs"):
            self.assertIn(key, payload)
        self.assertGreaterEqual(payload["sessions"], 1)
        summaries = [row["summary"] for row in payload["moved"]]
        self.assertTrue(any("portal" in (s or "").lower() for s in summaries),
                        summaries)

    def test_daily_is_an_alias_and_all_widens_the_window(self):
        code, out = self._run("daily")
        self.assertEqual(code, 0)
        self.assertIn("Standup", out)
        code, out = self._run("standup", "all")
        self.assertEqual(code, 0)
        self.assertIn("all time", out)
