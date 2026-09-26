"""Operations and trust: watch, doctor, the schema drift guard, team rollup.

Watch must keep the home screen's timer discipline and tail the log from
where it stopped. Doctor must say what is wrong and how to fix it without
writing anything. The drift guard must never crash. And the rollup must
carry counts and rates only — the test collects every string the fixture
store holds and proves none of them reaches it.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from support import (
    Screen,
    StoreTest,
    _add_governance_rows,
    _alpha_events,
    _event,
    _tool,
    _write_events,
)


def _now(second: int = 0) -> str:
    return datetime.now(timezone.utc).strftime(f"%Y-%m-%dT%H:%M:{second:02d}")


class WatchTest(StoreTest):
    def setUp(self):
        super().setUp()
        self.base = Path(self._tmp.name)
        conn = sqlite3.connect(self.base / "session-store.db")
        # sess-alpha is the most recently active session.
        conn.execute("UPDATE sessions SET updated_at = datetime('now', '+1 minute') "
                     "WHERE id = 'sess-alpha'")
        conn.commit()
        conn.close()
        self.log = _write_events(self.base, "sess-alpha", _alpha_events())

    def test_a_tick_reads_spend_burn_budget_and_the_log_tail(self):
        from cs import cli, ui

        ui.set_daily_budget(10)
        state: dict = {}
        cli._watch_read(state)
        session = state["session"]
        self.assertEqual(session["id"], "sess-alpha")
        self.assertEqual(session["turns"], 2)
        # Both calls were billed just now, inside the ten-minute burn window.
        self.assertAlmostEqual(session["burn_per_minute"], 0.4)
        self.assertAlmostEqual(session["left"], 6.0)
        self.assertEqual(session["last_tool"], "edit")
        self.assertEqual(session["last_failure"][0], "edit")
        lines = " ".join(text for text, _ in cli._watch_lines(state, 80))
        self.assertIn("0.40 AIU/min", lines)
        self.assertIn("6.00 of 10 AIU left today", lines)

    def test_the_tail_reads_only_what_was_added(self):
        from cs import cli

        state: dict = {}
        cli._watch_read(state)
        tail = state["tail"]
        seen, offset = tail.calls, tail.offset
        with open(self.log, "a", encoding="utf-8") as handle:
            for event in _tool("n1", "grep", _now(40), True):
                handle.write(json.dumps(event, separators=(",", ":")) + "\n")
            handle.write('{"type":"tool.execution_complete","data":{"toolCa')  # half
        cli._watch_read(state)
        self.assertEqual(tail.calls, seen + 1)
        self.assertEqual(tail.last_tool, "grep")
        self.assertGreater(tail.offset, offset)
        # The half line is left for the next tick, not parsed as garbage.
        self.assertEqual(tail.offset, self.log.stat().st_size - len(
            '{"type":"tool.execution_complete","data":{"toolCa'))

    def test_a_quiet_session_is_not_drawn(self):
        from cs import cli

        conn = sqlite3.connect(self.base / "session-store.db")
        conn.execute("UPDATE sessions SET updated_at = '2020-01-01 00:00:00', "
                     "created_at = '2020-01-01 00:00:00'")
        conn.commit()
        conn.close()
        state: dict = {}
        cli._watch_read(state)
        self.assertIsNone(state["session"])
        self.assertEqual(cli._live_lines(state, 100), [])
        self.assertEqual(cli._live_lines(state, 40), [])

    def test_the_home_strip_ticks_without_moving_the_refresh_deadline(self):
        from cs import cli, ui

        class ClockScreen(Screen):
            now = 0.0
            delay = -1

            def timeout(self, milliseconds):
                super().timeout(milliseconds)
                self.delay = milliseconds

            def getch(self):
                delay = self.delay
                if not 1 <= delay <= 1000:
                    raise AssertionError(f"unbounded timeout {delay}")
                self.now += delay / 1000
                return super().getch()

        screen = ClockScreen([-1] * 6 + [ord("q")])
        live, home = [], []
        real_live = cli._refresh_live

        def track_live(state):
            live.append(screen.now)
            real_live(state)

        def track_home(state):
            home.append(screen.now)
            return True

        with (
            mock.patch.object(ui, "PACE_FRAMES", 0),
            mock.patch.object(cli.time, "monotonic",
                              side_effect=lambda: screen.now),
            mock.patch.object(cli, "_refresh_live", side_effect=track_live),
            mock.patch.object(cli, "_refresh_home", side_effect=track_home),
        ):
            cli._home_tui(screen, {"revealed": True, "facts": [("1", "sessions")]})
        self.assertEqual(home, [], "a live tick moved the 60-second refresh")
        self.assertGreaterEqual(len(live), 2)
        self.assertEqual(live[1] - live[0], cli.WATCH_SECONDS)


class DoctorTest(StoreTest):
    def _checks(self) -> dict:
        code, out = self._run("doctor", "--json")
        self.assertEqual(code, 0, out)
        return {c["check"]: c for c in json.loads(out)["checks"]}

    def test_every_check_reports_status_and_fix(self):
        checks = self._checks()
        for name in ("python", "store", "schema", "session-state", "config dir",
                     "cache dir", "terminal", "mouse", "glyphs"):
            self.assertIn(name, checks)
            self.assertIn(checks[name]["status"], ("pass", "warn", "fail"))
        self.assertEqual(checks["store"]["status"], "pass")
        self.assertIn("mode=ro", checks["store"]["detail"])
        # The fixture store has no schema_version: an older Copilot.
        self.assertEqual(checks["schema"]["status"], "warn")
        self.assertIn("older Copilot", checks["schema"]["detail"])
        self.assertEqual(checks["session-state"]["status"], "warn")
        self.assertTrue(checks["session-state"]["fix"])
        self.assertIn("compatibility wrapper", checks["mouse"]["detail"])

    def test_a_missing_store_fails_with_the_fix(self):
        os.environ["COPILOT_HOME"] = str(Path(self._tmp.name) / "nowhere")
        checks = self._checks()
        self.assertEqual(checks["store"]["status"], "fail")
        self.assertIn("COPILOT_HOME", checks["store"]["fix"])

    def test_doctor_writes_nothing(self):
        from cs import events, ui

        self._run("doctor")
        self.assertFalse(events.cache_path().parent.exists())
        self.assertFalse(ui.settings_path().exists())

    def test_the_page_names_each_check(self):
        code, out = self._run("doctor")
        self.assertEqual(code, 0)
        self.assertIn("Doctor · ", out)
        self.assertIn("schema", out)
        self.assertIn("→ ", out)


class SchemaDriftTest(StoreTest):
    def _version(self, version: int, drop: str | None = None) -> None:
        conn = sqlite3.connect(Path(self._tmp.name) / "session-store.db")
        conn.execute("CREATE TABLE schema_version (version INTEGER)")
        conn.execute("INSERT INTO schema_version VALUES (?)", (version,))
        if drop:
            conn.execute(f"DROP TABLE {drop}")
        conn.commit()
        conn.close()

    def test_an_unknown_version_is_drift_and_doctor_says_so(self):
        from cs import cli

        self._version(99)
        self.assertTrue(cli.schema_notice())
        code, out = self._run("doctor", "--json")
        schema = next(c for c in json.loads(out)["checks"] if c["check"] == "schema")
        self.assertIn("newer than cs knows", schema["detail"])

    def test_a_known_version_missing_a_table_is_drift(self):
        from cs import cli, db

        self._version(8, drop="session_refs")
        self.assertTrue(cli.schema_notice())
        conn = db.connect()
        self.assertIn("session_refs", db.schema_drift(conn)["missing_tables"])
        conn.close()

    def test_an_older_store_is_not_called_drifted(self):
        from cs import cli

        self.assertFalse(cli.schema_notice())

    def test_the_drift_guard_never_raises(self):
        from cs import cli

        os.environ["COPILOT_HOME"] = str(Path(self._tmp.name) / "nowhere")
        self.assertFalse(cli.schema_notice())
        with mock.patch("cs.db.connect", side_effect=sqlite3.DatabaseError("bad")):
            self.assertFalse(cli.schema_notice())

    def test_home_says_schema_changed_and_keeps_the_refresh_time(self):
        from cs import cli, ui

        for width in (40, 60, 80, 100, 140):
            with self.subTest(width=width):
                line = cli._home_status("", 0, 40, width, refreshed="12:34:56",
                                        schema=True)
                self.assertLessEqual(ui.cells(line), width)
                self.assertIn("schema", line)
                self.assertIn("12:34:56", line)
        self._version(99)
        screen = Screen([ord("q")])
        state = {"revealed": True, "refreshed": "12:34:56",
                 "schema_drift": cli.schema_notice()}
        cli._home_tui(screen, state)
        self.assertIn("schema changed · cs doctor", " ".join(screen.frames[-1].values()))


class RollupTest(StoreTest):
    def setUp(self):
        super().setUp()
        self.base = Path(self._tmp.name)
        _add_governance_rows(self.base)
        _write_events(self.base, "sess-alpha", [
            *_alpha_events(),
            _event("subagent.completed", _now(50),
                   {"agentName": "explore", "model": "gpt-5.5"}),
        ])

    def _fixture_strings(self) -> set[str]:
        """Every piece of text the store holds that could identify anything.

        Enumerations the schema defines (finish reasons, initiators, ref and
        source types, the tool that touched a file) are left out: they are
        vocabulary, not content. Everything else — ids, titles, repositories,
        directories, prompts, replies, paths, ref values, models — is in.
        """
        skip = {("assistant_usage_events", "finish_reason"),
                ("assistant_usage_events", "initiator"),
                ("sessions", "host_type"), ("sessions", "branch"),
                ("session_refs", "ref_type"), ("search_index", "source_type"),
                ("session_files", "tool_name")}
        conn = sqlite3.connect(self.base / "session-store.db")
        found: set[str] = set()
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE "
                                     "type='table' AND name NOT LIKE 'search_index_%' "
                                     "AND name NOT LIKE 'sqlite_%'"):
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info('{table}')")]
            for column in columns:
                if (table, column) in skip or column.endswith(("_at", "timestamp")):
                    continue
                for (value,) in conn.execute(f"SELECT DISTINCT {column} FROM {table}"):
                    if isinstance(value, str) and len(value.strip()) >= 3:
                        found.add(value.strip())
        conn.close()
        return found | {"explore", "gpt-5.5", "claude-opus-4.8"}

    def test_no_fixture_string_reaches_the_rollup(self):
        strings = self._fixture_strings()
        self.assertIn("acme/portal", strings)          # the test is looking
        self.assertIn("sess-alpha", strings)
        for args in (("rollup", "--json"), ("rollup", "all", "--json"), ("rollup",)):
            with self.subTest(args=args):
                code, out = self._run(*args)
                self.assertEqual(code, 0)
                leaked = sorted(s for s in strings if s in out)
                self.assertEqual(leaked, [], f"fixture text in the rollup: {leaked}")

    def test_the_rollup_counts_and_hashes_repositories_stably(self):
        from cs import ui

        data = json.loads(self._run("rollup", "--json")[1])
        self.assertEqual(data["tool_calls"], 6)
        self.assertEqual(data["tool_failures"], 4)
        self.assertEqual(data["stuck_loops"], 1)
        self.assertEqual(data["unclean_endings"]["error"], 1)
        self.assertEqual((data["commits"], data["prs"]), (1, 1))
        hashes = {repo["repo"] for repo in data["repos"]}
        self.assertTrue(all(len(h) == 12 or h == "none" for h in hashes))
        again = json.loads(self._run("rollup", "--json")[1])
        self.assertEqual({r["repo"] for r in again["repos"]}, hashes)
        salt = ui.rollup_salt()
        self.assertEqual(len(salt), 32)
        settings = ui._load_settings()
        settings["rollup_salt"] = "0" * 32
        ui._save_settings(settings)
        other = json.loads(self._run("rollup", "--json")[1])
        self.assertNotEqual({r["repo"] for r in other["repos"]} - {"none"},
                            hashes - {"none"})


class OpsHomeTest(StoreTest):
    def test_rows_sit_where_the_brief_puts_them(self):
        from cs import cli

        items = cli._home_items(7)
        labels = [item[1] for item in items]
        group = {label: cli._home_group(i) for i, label in enumerate(labels)}
        self.assertNotIn("Today", group.values())
        self.assertNotIn("Team rollup", labels)
        reference = [label for label in labels if group[label] == "Reference"]
        self.assertEqual(reference[-2:], ["Theme", "Help"])
        self.assertNotIn("Doctor", reference)

    def test_the_rollup_command_opens(self):
        from cs import cli

        with mock.patch.object(cli, "_page", return_value=True) as page:
            self.assertTrue(cli.cmd_rollup(7))
        self.assertNotIn('"view": "rollup"', page.call_args.args[0])
        self.assertIn("Team rollup", page.call_args.args[0])
        self.assertIn("session", page.call_args.args[0])
