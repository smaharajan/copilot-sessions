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

from support import Screen, StoreTest, _alpha_events, _event, _tool, _write_events

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

    # ── Compare ──────────────────────────────────────────────────────

    def test_diff_puts_two_sessions_side_by_side(self):
        data = self._json("diff", "sess-alpha", "sess-empty")
        a, b = data["a"], data["b"]
        self.assertEqual((a["turns"], a["nano_aiu"], a["commits"], a["prs"]),
                         (2, 4_000_000_000, 1, 1))
        self.assertEqual((a["tool_calls"], a["tool_failures"]), (6, 4))
        self.assertAlmostEqual(a["cache_hit"], 8000 / 11000, places=3)
        self.assertEqual(sorted(a["models"]), ["claude-opus-4.8", "gpt-5.5"])
        self.assertIsNone(b["tool_calls"])
        with self._columns(100):
            code, out = self._run("diff", "sess-alpha", "sess-empty")
        self.assertIn("cache hit", out)
        row = next(line for line in out.splitlines() if "tool calls" in line)
        self.assertIn("6 · 4 failed", row)
        self.assertIn("no log", row)          # both sides on one line

    def test_diff_stacks_at_forty_columns(self):
        with self._columns(40):
            code, out = self._run("diff", "sess-alpha", "sess-empty")
        self.assertEqual(code, 0)
        self.assertIn("A · sess-alp", out)
        self.assertIn("B · sess-emp", out)
        self.assertEqual(out.count("tool calls"), 2)

    def test_the_listing_marks_then_compares(self):
        from cs import cli

        rows = [("sess-alpha", "2026-09-01T12:00", "One", "r/a", "/tmp", 1, 0),
                ("sess-empty", "2026-09-01T11:00", "Two", "r/b", "/tmp", 1, 0)]
        import curses
        screen = Screen([ord("d"), curses.KEY_DOWN, ord("d")])
        state: dict = {}
        self.assertEqual(cli._listing_tui(screen, rows, "Sessions", state=state),
                         ("diff", ("sess-alpha", "sess-empty")))
        marked = [text for text in screen.frames[1].values() if "marked" in text]
        self.assertTrue(marked, "the status line never said a session was marked")
        screen = Screen([ord("e")])
        self.assertEqual(cli._listing_tui(screen, rows, "Sessions"),
                         ("replay", "sess-alpha"))

    def test_the_listing_opens_the_comparison_and_the_replay(self):
        """What the listing hands back has to reach the views — through the
        package, where `_page` can find its reader. An import inside the
        function once handed back an un-lifted copy that raised KeyError."""
        from cs import cli

        rows = [("sess-alpha", "2026-09-01T12:00", "One", "r/a", "/tmp", 1, 0)]
        for verb, target, shown in (("diff", ("sess-alpha", "sess-empty"),
                                     "Compare sessions"),
                                    ("replay", "sess-alpha", "Replay · ")):
            with self.subTest(verb=verb):
                with mock.patch.object(cli, "_curses_wrapper",
                                       side_effect=[(verb, target), None]), \
                        mock.patch.object(cli, "_pause", return_value=True), \
                        redirect_stdout(io.StringIO()) as out:
                    self.assertTrue(cli._interactive_listing(rows, "S", show_all=True))
                self.assertIn(shown, out.getvalue())

    # ── Replay ───────────────────────────────────────────────────────

    def test_replay_pages_turn_by_turn_with_tools_files_and_credits(self):
        from cs import cli

        _write_events(self.base, "sess-alpha", [
            *_tool("r1", "bash", _today(3), False),
            *_tool("r2", "view", _today(12), True),
        ])
        data = cli._replay_data("sess-alpha")
        first = cli._replay_page(data, 0)
        second = cli._replay_page(data, 1)
        self.assertIn("turn 0 of 1", first)
        self.assertIn("1.50 AIU", first)
        self.assertIn("bash 0", first)
        self.assertIn("✗1", first)
        self.assertIn("make a portal", first)
        self.assertIn("2.50 AIU", second)
        self.assertIn("view 1", second)
        self.assertIn("globe.js", second)          # files touched in turn 1
        self.assertIn("add charts", second)

    def test_replay_steps_with_the_arrows_in_the_reader(self):
        import curses

        from cs import cli

        data = cli._replay_data("sess-alpha")
        sort = {"steps": "turn", "columns": [0, 1], "column": 0, "descending": False,
                "defaults": {}, "render": lambda turn, _d: cli._replay_page(data, turn),
                "label": lambda turn: f"turn {turn}/1"}
        screen = Screen([ord("x"), curses.KEY_RIGHT, curses.KEY_RIGHT, ord("q")])
        with mock.patch.object(cli.ui, "sgr_palette", return_value={}), \
                mock.patch.object(cli.ui, "tui_theme",
                                  return_value=__import__("collections").defaultdict(int)):
            cli._reader_tui(screen, cli._replay_page(data, 0).split("\n"), False, sort)
        text = " ".join(screen.frames[-1].values())
        self.assertIn("add charts", text)
        self.assertIn("turn 1/1", text)
        self.assertEqual(sort["column"], 1, "→ past the last turn must stay put")

    def test_replay_prints_every_turn_when_piped_and_masks_them(self):
        self.conn.execute("UPDATE turns SET assistant_response = ? WHERE "
                          "session_id = 'sess-alpha' AND turn_index = 1",
                          (f"use {SECRET}",))
        self.conn.commit()
        code, out = self._run("replay", "sess-alpha")
        self.assertEqual(code, 0)
        self.assertIn("turn 0 of 1", out)
        self.assertIn("turn 1 of 1", out)
        self.assertNotIn(SECRET, out)

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
        for args in (("diff", "sess-alpha", "sess-empty"), ("replay", "sess-alpha"),
                     ("anomalies",), ("health", "--repo", "portal"), ("patterns",),
                     ("skills",)):
            with self.subTest(view=args[0]):
                code, out = self._run(*args)
                self.assertNotIn(SECRET, out)
                if args[0] != "replay":
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
        self.assertEqual((group["Replay"], asks["Replay"]), ("Find", "ref"))
        self.assertEqual((group["Compare sessions"], asks["Compare sessions"]),
                         ("Measure", "pair"))
        self.assertEqual((group["Spend anomalies"], asks["Spend anomalies"]),
                         ("Measure", "period"))
        improve = [label for label in labels if group[label] == "Improve"]
        self.assertEqual(improve, ["Practice", "Rhythm", "Context", "Repo health",
                                   "Prompt patterns", "Clean-up"])

    def test_ref_and_pair_rows_ask_then_open(self):
        import curses

        from cs import cli

        labels = [item[1] for item in cli._home_items()]
        replay = labels.index("Replay")
        screen = Screen([curses.KEY_DOWN] * replay + [10, *map(ord, "#3"), 10])
        self.assertEqual(cli._home_tui(screen, {"revealed": True}), (replay, "#3"))
        compare = labels.index("Compare sessions")
        screen = Screen([curses.KEY_DOWN] * compare + [10, *map(ord, "1 2"), 10])
        self.assertEqual(cli._home_tui(screen, {"revealed": True}),
                         (compare, ("1", "2")))
        # One session where two are needed is not a comparison.
        screen = Screen([curses.KEY_DOWN] * compare + [10, *map(ord, "1"), 10,
                                                        ord("q")])
        self.assertIsNone(cli._home_tui(screen, {"revealed": True}))

    def test_every_new_row_opens(self):
        from cs import cli

        items = {item[1]: item for item in cli._home_items(7)}
        with mock.patch.object(cli, "_page", return_value=True), \
                redirect_stdout(io.StringIO()):
            for label, given in (("Replay", "sess-alpha"),
                                 ("Compare sessions", ("sess-alpha", "sess-empty")),
                                 ("Spend anomalies", 7), ("Repo health", None),
                                 ("Prompt patterns", 7)):
                with self.subTest(row=label):
                    action = items[label][3]
                    action(given) if given is not None else action()
