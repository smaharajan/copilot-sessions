"""Analysis views: compare, replay, spend anomalies, repo health, prompt
patterns, and how skills and agent profiles are really used.

A flag must carry what triggered it, a comparison of habits must carry its
sample sizes, and nothing from the store may reach the page or `--json`
unmasked.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from support import StoreTest, _alpha_events, _event, _write_events

SECRET = "ghp_" + "M" * 36  # gitleaks:allow


def _today(second: int) -> str:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{day}T00:00:{second:02d}"


class AnalysisTest(StoreTest):
    def setUp(self):
        super().setUp()
        self.base = Path(self._tmp.name)
        _write_events(self.base, "sess-alpha", _alpha_events())
        self.conn = sqlite3.connect(self.base / "session-store.db")
        self.conn.execute("UPDATE turns SET timestamp = ? WHERE session_id = "
                          "'sess-alpha' AND turn_index = 0", (_today(0) + ".000Z",))
        self.conn.execute("UPDATE turns SET timestamp = ? WHERE session_id = "
                          "'sess-alpha' AND turn_index = 1", (_today(10) + ".000Z",))
        self.conn.execute("UPDATE session_files SET turn_index = 1 WHERE "
                          "session_id = 'sess-alpha'")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def _json(self, *args: str) -> dict:
        code, out = self._run(*args, "--json")
        self.assertEqual(code, 0, out)
        return json.loads(out)

    def _columns(self, width: int):
        import shutil

        return mock.patch.object(shutil, "get_terminal_size",
                                 return_value=os.terminal_size((width, 40)))

    # ── Spend anomalies ──────────────────────────────────────────────

    def _spend(self, sid: str, day: date, nano: int, turn: int = 0,
               model: str = "gpt-5.5") -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO sessions VALUES (?,?,?,'local','main',?,?,?)",
            (sid, "/tmp/s", "acme/portal", f"Session {sid}",
             f"{day.isoformat()}T09:00:00.000Z", f"{day.isoformat()}T10:00:00.000Z"))
        self.conn.execute(
            "INSERT INTO assistant_usage_events (session_id, turn_index, model, "
            "total_nano_aiu, input_tokens, cache_read_tokens, created_at) "
            "VALUES (?,?,?,?,100,300,?)",
            (sid, turn, model, nano, f"{day.isoformat()}T09:30:00.000Z"))

    def test_a_spike_is_flagged_with_the_turns_behind_it(self):
        today = date.today()
        for back in range(8, 2, -1):
            self._spend(f"calm-{back}", today - timedelta(days=back), 1_000_000_000)
        self._spend("spike", today - timedelta(days=1), 9_000_000_000, turn=4,
                    model="big-model")
        self.conn.commit()
        data = self._json("anomalies", "7")
        day = next(d for d in data["days"] if d["day"] == (today - timedelta(days=1))
                   .isoformat())
        self.assertGreater(day["factor"], 2)
        self.assertEqual(day["turns"][0]["id"], "spike")
        self.assertEqual(day["turns"][0]["turn"], 4)
        self.assertEqual(day["turns"][0]["models"], ["big-model"])
        self.assertAlmostEqual(day["turns"][0]["cache_hit"], 0.75)
        session = next(s for s in data["sessions"] if s["id"] == "spike")
        self.assertEqual(session["baseline_nano_aiu"], 1_000_000_000)
        code, out = self._run("anomalies", "7")
        self.assertIn("big-model", out)
        self.assertIn("cache 75%", out)
        self.assertNotIn("calm-3", [s["id"] for s in data["sessions"]])

    def test_a_day_and_its_session_are_one_card_and_the_rest_are_counted(self):
        from cs import cli

        def turn(sid, nano):
            return {"id": sid, "summary": sid, "turn": 1, "nano_aiu": nano,
                    "models": ["m"], "effort": None, "cache_hit": 0.5}

        def day(n, sid, nano):
            return {"day": f"2026-01-{n:02d}", "nano_aiu": nano,
                    "baseline_nano_aiu": nano // 4, "factor": 4.0,
                    "turns": [turn(sid, nano)]}

        def session(n, sid):
            return {"id": sid, "summary": sid, "n": n, "day": f"2026-01-{n:02d}",
                    "session_nano_aiu": 4_000, "baseline_nano_aiu": 1_000,
                    "factor": 4.0, "turns": [turn(sid, 4_000)]}

        days = [day(i, f"s{i}", 4_000) for i in range(1, 8)]
        sessions = [session(i, f"s{i}") for i in range(1, 8)]
        data = {"window_days": 30, "factor": 2.0, "baseline_days": 14,
                "series": [{"day": d["day"], "nano_aiu": d["nano_aiu"]} for d in days],
                "days": days, "sessions": sessions}
        text = cli._capture(lambda: cli._render_anomalies(data))
        # The first five days each merge with their session, so those ids
        # appear once. The other two are the "+2 more", not a second card.
        self.assertEqual(text.count("s1"), 1)
        self.assertNotIn("s6", text.split("+2 more")[0])
        self.assertIn("+2 more", text)

    def test_no_baseline_means_no_flag(self):
        self._spend("lonely", date.today(), 50_000_000_000)
        self.conn.commit()
        data = self._json("anomalies")
        self.assertNotIn("lonely", [s["id"] for s in data["sessions"]])

    # ── Repo health ──────────────────────────────────────────────────

    def test_health_for_a_named_repo(self):
        data = self._json("health", "--repo", "portal")
        self.assertEqual(data["repo"], "portal")
        self.assertEqual((data["tool_calls"], data["tool_failures"]), (6, 4))
        self.assertEqual(data["hooks_failing"], {"preToolUse": 1})
        self.assertIn("globe.js", " ".join(f["path"] for f in data["top_files"]))
        self.assertIsNone(data["instructions_over_limit"])   # not this checkout

    def test_health_here_reads_the_checkout(self):
        with tempfile.TemporaryDirectory() as here:
            (Path(here) / "AGENTS.md").write_text("x" * 5000)
            before = os.getcwd()
            os.chdir(here)
            try:
                data = self._json("health")
            finally:
                os.chdir(before)
        self.assertEqual([i["chars"] for i in data["instructions_over_limit"]], [5000])
        self.assertEqual(data["sessions"], 0)

    # ── Prompt patterns ──────────────────────────────────────────────

    def test_patterns_compare_features_with_sample_sizes(self):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        for n in range(3):
            sid = f"good-{n}"
            self.conn.execute("INSERT INTO sessions VALUES (?,?,?,'local','main',?,?,?)",
                              (sid, "/tmp/g", "acme/app", "Good", now, now))
            self.conn.execute(
                "INSERT INTO turns (session_id, turn_index, user_message, "
                "assistant_response) VALUES (?,0,?,'ok')",
                (sid, "fix src/parser.py so that pytest passes"))
            self.conn.execute("INSERT INTO session_refs (session_id, ref_type, "
                              "ref_value) VALUES (?, 'commit', 'c0ffee')", (sid,))
        self.conn.commit()
        data = self._json("patterns")
        self.assertEqual(data["note"], "correlation, not causation")
        path = next(f for f in data["features"] if f["feature"] == "names a file or path")
        self.assertEqual(path["with"]["sessions"], 3)
        self.assertEqual(path["with"]["shipped_rate"], 1.0)
        criteria = next(f for f in data["features"]
                        if f["feature"].startswith("has acceptance criteria"))
        self.assertEqual(criteria["with"]["sessions"], 3)
        code, out = self._run("patterns")
        flat = " ".join(out.split())
        self.assertIn("Correlation, not causation", flat)
        self.assertIn("under 5 is too few", flat)

    # ── Playbook ─────────────────────────────────────────────────────

    def _shipped(self, sid: str, ask: str, ending: str = "stop",
                 ref: str | None = "commit") -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.conn.execute("INSERT INTO sessions VALUES (?,?,?,'local','main',?,?,?)",
                          (sid, "/tmp/g", "acme/app", f"Work {sid}", now, now))
        self.conn.execute("INSERT INTO turns (session_id, turn_index, user_message, "
                          "assistant_response) VALUES (?,0,?,'ok')", (sid, ask))
        self.conn.execute("INSERT INTO assistant_usage_events (session_id, "
                          "turn_index, model, total_nano_aiu, finish_reason) "
                          "VALUES (?,0,'gpt-5.5',1000000000,?)", (sid, ending))
        if ref:
            self.conn.execute("INSERT INTO session_refs (session_id, ref_type, "
                              "ref_value) VALUES (?,?,'c0ffee')", (sid, ref))

    def test_playbook_keeps_only_what_shipped_cleanly_and_says_why(self):
        code, out = self._run("playbook")
        self.assertEqual(code, 0)
        self.assertIn("None met all of", " ".join(out.split()))
        ask = f"fix src/parser.py so that pytest passes, token {SECRET}"
        self._shipped("good", ask)
        self._shipped("thin", "resume the work")               # no brief to reuse
        self._shipped("cut-off", ask, ending="length")          # ended badly
        self._shipped("unknown", ask, ending="")                # no proof it ended well
        self._shipped("unshipped", ask, ref=None)               # nothing shipped
        self.conn.commit()
        data = self._json("playbook")
        # sess-alpha shipped too, but a tool got stuck and its last call erred.
        self.assertEqual([s["id"] for s in data["sessions"]], ["good"])
        self.assertEqual(data["shipped"], 5)
        good = data["sessions"][0]
        self.assertEqual((good["work"], good["commits"], good["prs"], good["ending"]),
                         ("fix", 1, 0, "stop"))
        self.assertIn("src/parser.py", good["opening"])
        code, out = self._run("playbook")
        self.assertIn("Playbook", out)
        self.assertIn("1 commit · fix src/parser.py", out)
        for text in (out, json.dumps(data)):
            self.assertNotIn(SECRET, text)

    def test_playbook_opens_as_a_listing_in_a_terminal(self):
        from cs import cli

        self._shipped("good", "add a retry to the SFTP poller with a unit test",
                      ref="pr")
        self.conn.commit()
        seen = []
        with mock.patch.object(cli, "_interactive_listing",
                               side_effect=lambda *a, **k: seen.append((a, k)) or True), \
                mock.patch("sys.stdin", mock.Mock(isatty=lambda: True)), \
                mock.patch("sys.stdout", mock.Mock(isatty=lambda: True)):
            self.assertTrue(cli.cmd_playbook())
        rows, title = seen[0][0][:2]
        self.assertEqual([row[0] for row in rows], ["good"])
        self.assertIn("Playbook", title)
        self.assertEqual(seen[0][1]["hits"]["good"],
                         ("build", "1 PR · add a retry to the SFTP poller with a "
                                   "unit test"))
        # A session that ships while the listing is open arrives on the
        # heartbeat, and with its line under it.
        self._shipped("later", "fix the flaky checkout test in the cart module")
        self.conn.commit()
        rows, title = seen[0][1]["reload"]()
        self.assertEqual({row[0] for row in rows}, {"good", "later"})
        self.assertIn("2 sessions", title)
        self.assertIn("later", seen[0][1]["hits"])

    # ── Agent config ─────────────────────────────────────────────────

    def test_skills_and_profiles_gain_invoked_last_used_and_model_kept(self):
        agents = self.base / "agents"
        agents.mkdir()
        (agents / "explore.agent.md").write_text(
            "---\nname: explore\nmodel: gpt-5.5\n---\nLook.\n")
        profiles = {a["name"]: a for a in self._json("profiles")["assets"]}
        self.assertEqual(profiles["explore"]["invoked"], 1)
        self.assertEqual(profiles["explore"]["override_honoured"], "1/1")
        self.assertTrue(profiles["explore"]["last_used"])
        code, out = self._run("profiles")
        self.assertIn("model kept", out)
        self.assertIn("1/1", out)
        code, out = self._run("skills")
        self.assertIn("Invoked · from the event logs", out)
        self.assertIn("docs", out)

    # ── Masking ──────────────────────────────────────────────────────

    def test_nothing_from_the_store_leaks_through_the_analysis_views(self):
        self.conn.execute("UPDATE sessions SET summary = ? WHERE id = 'sess-alpha'",
                          (f"deploy {SECRET}",))
        self.conn.execute("UPDATE turns SET user_message = ? WHERE session_id = "
                          "'sess-alpha' AND turn_index = 0", (f"use {SECRET}",))
        self.conn.execute("UPDATE assistant_usage_events SET model = ? WHERE "
                          "session_id = 'sess-alpha' AND turn_index = 1",
                          (f"m-{SECRET}",))
        self.conn.commit()
        _write_events(self.base, "sess-alpha", [
            *_alpha_events(),
            _event("skill.invoked", _today(30), {"name": f"s-{SECRET}"}),
        ])
        for args in (("anomalies",), ("health", "--repo", "portal"),
                     ("patterns",), ("skills",)):
            with self.subTest(view=args[0]):
                code, out = self._run(*args)
                self.assertEqual(code, 0, out)
                self.assertNotIn(SECRET, out)
                code, out = self._run(*args, "--json")
                self.assertEqual(code, 0)
                self.assertNotIn(SECRET, out)


class AnalysisHomeTest(StoreTest):
    def test_rows_sit_where_the_brief_puts_them(self):
        from cs import cli

        items = cli._home_items(7)
        labels = [item[1] for item in items]
        group = {label: cli._home_group(i) for i, label in enumerate(labels)}
        asks = {item[1]: item[4] for item in items}
        self.assertNotIn("Replay", labels)
        self.assertNotIn("Compare sessions", labels)
        self.assertNotIn("Spend anomalies", labels)
        self.assertNotIn("Similar work", labels)
        self.assertNotIn("Improve", set(group.values()))
        self.assertEqual(asks["AI spend"], "period")
        self.assertEqual(group["Playbook"], "Find")

    def test_the_commands_off_the_menu_still_open(self):
        from cs import cli

        with mock.patch.object(cli, "_page", return_value=True), \
                redirect_stdout(io.StringIO()):
            for name, action in (("anomalies", lambda: cli.cmd_anomalies(7)),
                                 ("health", cli.cmd_health),
                                 ("patterns", lambda: cli.cmd_patterns(7))):
                with self.subTest(command=name):
                    action()
