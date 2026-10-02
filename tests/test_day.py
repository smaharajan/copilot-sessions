"""The day dashboard: `cs day` and the Today row at the top of the home menu.

Every figure is cut to the local day by its own time, so a session that
began last night counts only what it did today. Today is compared with
yesterday up to the same time of day. The event logs are read on from where
the last look stopped. The page holds its shape at every width, masks what
it prints, opens with motion that stops, and stays still under CS_MOTION=off.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from support import Screen, StoreTest, _event, _write_events

# A credential-shaped value, built by concatenation so it never sits whole in
# the source. It is planted in a session summary and a commit value.
SECRET = "ghp_" + "D" * 36  # gitleaks:allow


def _stamp(moment: datetime) -> str:
    """A local moment as Copilot writes it: UTC, ISO, with a Z."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _log_stamp(moment: datetime) -> str:
    """The same, in the shape `support._event` adds its `.000Z` to."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def curses_key(name: str) -> int:
    import curses

    return getattr(curses, name)


class _ClockScreen(Screen):
    """A Screen whose clock moves by the wait the page asked for.

    A page that never hands back control once its keys run out fails the
    test rather than spinning it forever.
    """

    now = 0.0
    delay = 0
    idle = 0

    def timeout(self, milliseconds):
        super().timeout(milliseconds)
        self.delay = milliseconds

    def getch(self):
        key = super().getch()
        if key == -1:
            self.now += max(self.delay, 1) / 1000
            if not self.keys:
                self.idle += 1
                if self.idle > 2000:
                    raise AssertionError("the page did not return once its keys ran out")
        return key

    def getmaxyx(self):
        return 40, 120


class DayTest(StoreTest):
    def setUp(self):
        super().setUp()
        from cs import cli

        self.base = Path(self._tmp.name)
        self.today, self.tomorrow = cli._day_bounds(date.today())
        self.yesterday = cli._day_bounds(date.today() - timedelta(days=1))[0]

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.base / "session-store.db")

    def _session(self, sid: str, summary: str, created: datetime,
                 repo: str | None = "acme/api") -> None:
        conn = self._db()
        conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                     (sid, "/tmp/api", repo, "local", "main", summary,
                      _stamp(created), _stamp(created)))
        conn.commit()
        conn.close()

    def _spend(self, sid: str, at: datetime, aiu: float, model: str = "gpt-5.5",
               initiator: str = "agent") -> None:
        conn = self._db()
        conn.execute(
            """INSERT INTO assistant_usage_events
               (session_id, turn_index, model, total_nano_aiu, input_tokens,
                output_tokens, cache_read_tokens, reasoning_tokens, duration_ms,
                time_to_first_token_ms, finish_reason, initiator, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, 0, model, int(aiu * 1e9), 100, 10, 300, 0, 1000, 500, "stop",
             initiator, _stamp(at)))
        conn.commit()
        conn.close()

    def _ask(self, sid: str, index: int, at: datetime) -> None:
        conn = self._db()
        conn.execute("INSERT INTO turns (session_id, turn_index, user_message, "
                     "assistant_response, timestamp) VALUES (?,?,?,?,?)",
                     (sid, index, "do the thing", "done", _stamp(at)))
        conn.commit()
        conn.close()

    def _card(self, data: dict, sid: str) -> dict:
        return {s["id"]: s for s in data["by_session"]}[sid]

    # ── What counts as the day ───────────────────────────────────────

    def test_a_session_that_began_last_night_counts_only_what_it_did_today(self):
        from cs import cli

        self._session("sess-night", "Overnight migration",
                      self.yesterday + timedelta(hours=22))
        self._spend("sess-night", self.today - timedelta(minutes=30), 5)
        self._ask("sess-night", 0, self.today - timedelta(minutes=40))
        self._spend("sess-night", self.today + timedelta(minutes=30), 3)
        self._ask("sess-night", 1, self.today + timedelta(minutes=20))

        today = cli._day_data(0)
        card = self._card(today, "sess-night")
        self.assertEqual((card["nano_aiu"], card["asks"]), (3_000_000_000, 1))
        self.assertFalse(card["new"], "it was created yesterday")
        # The fixture's own calls (4 AIU) are today's too.
        self.assertEqual(today["spend"]["nano_aiu"], 7_000_000_000)
        self.assertEqual(today["by_hour"][0]["nano_aiu"], 3_000_000_000)
        self.assertEqual(today["by_hour"][0]["asks"], 1)

        yesterday = cli._day_data(1)
        card = self._card(yesterday, "sess-night")
        self.assertEqual((card["nano_aiu"], card["asks"]), (5_000_000_000, 1))
        self.assertEqual(yesterday["spend"]["nano_aiu"], 5_000_000_000)
        self.assertNotIn("sess-alpha", {s["id"] for s in yesterday["by_session"]})
        self.assertEqual((yesterday["label"], yesterday["live"]), ("Yesterday", False))

    def test_today_is_compared_with_yesterday_up_to_the_same_time(self):
        from cs import cli

        now = datetime.now().astimezone()
        elapsed = now - self.today
        self._session("sess-prev", "Yesterday's work", self.yesterday)
        # One call before this time of day yesterday, one after it.
        self._spend("sess-prev", self.yesterday + elapsed / 2, 1)
        self._spend("sess-prev", self.yesterday + elapsed + (self.tomorrow - now) / 2, 2)
        spend = cli._day_data(0)["spend"]
        self.assertEqual(spend["before_nano_aiu"], 1_000_000_000)
        self.assertEqual(spend["day_before_nano_aiu"], 3_000_000_000)
        # A finished day is compared with the whole day before it.
        self._spend("sess-prev", self.yesterday - timedelta(hours=1), 4)
        past = cli._day_data(1)["spend"]
        self.assertEqual(past["before_nano_aiu"], 4_000_000_000)
        text = cli._day_text(120)
        self.assertIn("300% more than by this time yesterday", " ".join(text.split()))

    def test_a_day_with_nothing_in_it_says_so(self):
        from cs import cli

        data = cli._day_data(3)
        self.assertEqual((data["sessions"], data["asks"], data["calls"]), (0, 0, 0))
        self.assertEqual(data["by_session"], [])
        self.assertIn("Nothing was recorded on this day.", cli._day_text(80, 3))

    def test_shipped_files_models_and_who_did_the_work(self):
        from cs import cli

        data = cli._day_data(0)
        self.assertEqual((data["shipped"]["commits"], data["shipped"]["prs"]), (1, 1))
        self.assertEqual(data["files"], {"created": 1, "edited": 2})
        self.assertEqual([m["model"] for m in data["models"]],
                         ["claude-opus-4.8", "gpt-5.5"])
        self.assertAlmostEqual(sum(m["share"] for m in data["models"]), 1.0)
        self.assertEqual({part["who"] for part in data["split"]}, {"you", "sub-agents"})
        self.assertEqual(data["repositories"], 1)
        self.assertGreater(data["tokens"]["cache_hit"], 0.5)
        self.assertEqual(data["first_token_ms"]["p50"], 800)

    def test_each_model_carries_the_time_it_ran_and_what_a_minute_of_it_cost(self):
        from cs import cli

        data = cli._day_data(0)
        models = {m["model"]: m for m in data["models"]}
        # claude: 2.5 AIU over 6 s of model time; gpt: 1.5 AIU over 4 s.
        self.assertEqual(models["claude-opus-4.8"]["time_ms"], 6000)
        self.assertAlmostEqual(models["claude-opus-4.8"]["time_share"], 0.6)
        self.assertAlmostEqual(models["claude-opus-4.8"]["aiu_per_minute"], 25.0)
        self.assertAlmostEqual(models["gpt-5.5"]["aiu_per_minute"], 22.5)
        self.assertEqual(data["model_ms"], 10_000)
        self.assertAlmostEqual(data["aiu_per_minute"], 24.0)
        text = cli._day_text(140)
        for heading in ("AIU %", "TIME %", "AIU/MIN", "all models"):
            self.assertIn(heading, text)
        self.assertIn("25.0", text)

    def test_the_running_total_stops_at_now_and_yesterday_runs_the_whole_day(self):
        from cs import cli

        self._session("sess-prev", "Yesterday's work", self.yesterday)
        self._spend("sess-prev", self.yesterday + timedelta(hours=1), 3)
        data = cli._day_data(0)
        today, before = data["curve"]["today"], data["curve"]["before"]
        now = data["now_slot"]
        self.assertEqual(today[now], 4_000_000_000)
        self.assertTrue(all(value is None for value in today[now + 1:]))
        self.assertEqual(before[-1], 3_000_000_000)
        self.assertEqual(len(today), 24 * 60 // data["slot_minutes"]
                         if len(today) % 24 == 0 else len(today))

    def test_a_commit_from_a_quiet_session_still_carries_its_title(self):
        from cs import cli

        self._session("sess-quiet", "Tag the release", self.yesterday)
        conn = self._db()
        conn.execute("INSERT INTO session_refs (session_id, ref_type, ref_value, "
                     "created_at) VALUES (?,?,?,?)",
                     ("sess-quiet", "commit", "f00dcafe",
                      _stamp(self.today + timedelta(minutes=1))))
        conn.commit()
        conn.close()
        items = {item["value"]: item for item in cli._day_data(0)["shipped"]["items"]}
        self.assertEqual(items["f00dcafe"]["title"], "Tag the release")

    def test_figures_a_store_cannot_time_are_left_out_not_zeroed(self):
        from cs import cli

        # Rebuilt rather than ALTERed: DROP COLUMN is newer than some SQLites.
        conn = self._db()
        conn.executescript("""
            ALTER TABLE session_refs RENAME TO timed_refs;
            CREATE TABLE session_refs (session_id TEXT, ref_type TEXT, ref_value TEXT);
            INSERT INTO session_refs SELECT session_id, ref_type, ref_value FROM timed_refs;
            DROP TABLE timed_refs;
            ALTER TABLE session_files RENAME TO timed_files;
            CREATE TABLE session_files (session_id TEXT, file_path TEXT, tool_name TEXT);
            INSERT INTO session_files SELECT session_id, file_path, tool_name FROM timed_files;
            DROP TABLE timed_files;
        """)
        conn.close()
        data = cli._day_data(0)
        self.assertIsNone(data["shipped"])
        self.assertIsNone(data["files"])
        keys = {card["key"] for card in cli._day_cards_of(data, {})}
        self.assertNotIn("shipped", keys)
        self.assertNotIn("files", keys)
        self.assertIn("carry no time", cli._day_text(120))

    # ── The event logs ───────────────────────────────────────────────

    def test_tool_calls_are_cut_to_the_day_and_read_on_from_where_they_stopped(self):
        from cs import cli, events

        before = self.today - timedelta(seconds=10)
        after = self.today + timedelta(seconds=10)
        path = _write_events(self.base, "sess-alpha", [
            # Started before midnight, finished after: today's, and named.
            _event("tool.execution_start", _log_stamp(before),
                   {"toolCallId": "c1", "toolName": "bash"}),
            _event("tool.execution_complete", _log_stamp(after),
                   {"toolCallId": "c1", "success": True}),
            # Wholly yesterday's.
            _event("tool.execution_start", _log_stamp(before),
                   {"toolCallId": "c2", "toolName": "view"}),
            _event("tool.execution_complete", _log_stamp(before),
                   {"toolCallId": "c2", "success": True}),
            _event("tool.execution_start", _log_stamp(after),
                   {"toolCallId": "c3", "toolName": "edit"}),
            _event("tool.execution_complete", _log_stamp(after),
                   {"toolCallId": "c3", "success": False}),
            _event("subagent.completed", _log_stamp(after), {"agentName": "explore"}),
            _event("skill.invoked", _log_stamp(after), {"name": "docs"}),
        ])
        since, until = cli._day_utc(self.today), cli._day_utc(self.tomorrow)
        memo: dict = {}
        counts = events.window_counts(["sess-alpha"], since, until, memo)
        self.assertEqual((counts["calls"], counts["failures"]), (2, 1))
        self.assertEqual(counts["tools"], {"bash": [1, 0], "edit": [1, 1]})
        self.assertEqual((counts["subagents"], counts["skills"]), (1, {"docs": 1}))
        self.assertEqual(memo["sess-alpha"]["offset"], path.stat().st_size)
        # A half-written line waits; once whole, only it is read.
        later = _log_stamp(after + timedelta(seconds=5))
        start = json.dumps(_event("tool.execution_start", later,
                                  {"toolCallId": "c4", "toolName": "grep"}),
                           separators=(",", ":")) + "\n"
        done = json.dumps(_event("tool.execution_complete", later,
                                 {"toolCallId": "c4", "success": True}),
                          separators=(",", ":"))
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(start + done[:20])
        counts = events.window_counts(["sess-alpha"], since, until, memo)
        self.assertEqual(counts["calls"], 2)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(done[20:] + "\n")
        with mock.patch.object(events, "_window_read",
                               wraps=events._window_read) as read:
            counts = events.window_counts(["sess-alpha"], since, until, memo)
        self.assertEqual(counts["tools"]["grep"], [1, 0])
        self.assertEqual(read.call_count, 1)
        # Nothing new: nothing read.
        with mock.patch.object(events, "_window_read") as read:
            events.window_counts(["sess-alpha"], since, until, memo)
        read.assert_not_called()

    def test_no_log_is_no_tool_figure_rather_than_zero(self):
        from cs import cli

        data = cli._day_data(0)
        self.assertIsNone(data["tools"])
        self.assertNotIn("tools", {card["key"] for card in cli._day_cards_of(data, {})})

    # ── What it prints ───────────────────────────────────────────────

    def test_what_it_prints_and_exports_is_masked(self):
        from cs import cli

        self._session("sess-leak", f"rotate {SECRET} today", self.today)
        self._spend("sess-leak", self.today + timedelta(minutes=5), 1)
        conn = self._db()
        conn.execute("INSERT INTO session_refs (session_id, ref_type, ref_value, "
                     "created_at) VALUES (?,?,?,?)",
                     ("sess-leak", "commit", SECRET, _stamp(self.today)))
        conn.commit()
        conn.close()
        text = cli._day_text(140)
        self.assertIn("rotate", text)
        self.assertNotIn(SECRET, text)
        code, out = self._run("day", "--json")
        self.assertEqual(code, 0)
        self.assertNotIn(SECRET, out)

    def test_the_page_holds_its_shape_at_every_width(self):
        from cs import cli, ui

        self._session("sess-wide", "A session with a long title " * 6, self.today,
                      repo="an-organisation-with-a-long-name/and-a-long-repository")
        self._spend("sess-wide", self.today + timedelta(minutes=1), 2,
                    model="a-model-with-an-unusually-long-name-indeed")
        data = cli._day_data(0)
        motion = {"grad": 8, "chips": 8, "clock": "12:00:00"}
        for width in (40, 60, 80, 100, 140):
            for height in (None, 24, 48):
                for tab in range(len(cli._DAY_TABS) if height else 1):
                    with self.subTest(width=width, height=height, tab=tab):
                        head, body, foot, hits = cli._day_screen(
                            data, width, height, motion, tab)
                        for line in [*head, *body, foot]:
                            for x, text, _role in line:
                                self.assertGreaterEqual(x, 0, text)
                                self.assertLessEqual(x + ui.cells(text), width - 1, text)
                        self.assertIn("Today", " ".join(t for _x, t, _r in head[0]))
                        if cli._DAY_TABS[tab][0] in ("overview", "sessions"):
                            self.assertEqual({index for *_rest, index in hits}, {0, 1})
                        # A tab fits a full-size window without scrolling.
                        if height and height >= 48 and width >= 100:
                            self.assertLessEqual(len(body), height - len(head) - 2)
            self.assertLessEqual(ui.cells(cli._day_hint(width)), width - 1)
            hint, stamp = cli._day_status(
                width, ["↓ more  40% updated 12:00:00 · every 5s ", "↓ more  40% "])
            self.assertLessEqual(ui.cells(hint) + ui.cells(stamp) + 2 * bool(stamp),
                                 width - 1)

    def test_it_runs_as_a_command_for_any_day_and_as_data(self):
        code, out = self._run("day")
        self.assertEqual(code, 0)
        self.assertIn("Today", out)
        self.assertIn("Spend by hour", out)
        self.assertIn("Build Three.js portal", out)
        code, out = self._run("day", "yesterday")
        self.assertEqual(code, 0)
        self.assertIn("Yesterday", out)
        code, out = self._run("day", "--json")
        payload = json.loads(out)
        self.assertEqual((payload["view"], payload["offset"]), ("day", 0))
        self.assertEqual([s["id"] for s in payload["by_session"]], ["sess-alpha"])
        self.assertEqual(payload["spend"]["nano_aiu"], 4_000_000_000)
        code, out = self._run("day", "2", "--json")
        self.assertEqual(json.loads(out)["offset"], 2)
        # As CSV, the day hour by hour.
        code, out = self._run("day", "--csv")
        self.assertEqual(code, 0)
        rows = out.splitlines()
        self.assertEqual(rows[0], "hour,nano_aiu,calls,asks,sessions,active_minutes")
        self.assertTrue(rows[1].startswith("00:00,"))
        self.assertIn(",4000000000,2,", out)
        code, err = self._run_err("day", "someday")
        self.assertEqual(code, 1)
        self.assertIn("not a day", err)

    def test_which_day_a_word_means(self):
        from cs import cli

        week_ago = (date.today() - timedelta(days=7)).isoformat()
        for word, expected in (("today", 0), (None, 0), ("yesterday", 1), ("3", 3),
                               ("-2", 2), (week_ago, 7), ("2001-01-01", None),
                               ("tomorrow", None), ("9999", 366)):
            with self.subTest(word=word):
                self.assertEqual(cli._day_offset(word), expected)

    # ── The page, live ───────────────────────────────────────────────

    def _play(self, keys: list, state: dict | None = None,
              motion: bool = True) -> tuple[_ClockScreen, object, dict]:
        from cs import cli, ui

        screen = _ClockScreen(keys)
        state = {"offset": 0} if state is None else state
        state.setdefault("threaded", False)
        # Not time.strftime: datetime formats through it, and the day's
        # window would be written as a clock. The clock rows are left out of
        # every comparison instead (see `_still`).
        with mock.patch.object(cli.time, "monotonic", side_effect=lambda: screen.now), \
                mock.patch.object(ui, "MOTION", motion), \
                mock.patch.object(ui, "PULSE_MS", 10**9):
            chosen = cli._day_tui(screen, state)
        return screen, chosen, state

    @staticmethod
    def _text(frame: dict) -> str:
        """A frame as the lines a person would read, in screen order."""
        rows: dict[int, list] = {}
        for (y, x), text in frame.items():
            rows.setdefault(y, []).append((x, text))
        lines = []
        for y in sorted(rows):
            line = ""
            for x, text in sorted(rows[y]):
                line += " " * max(x - len(line), 0) + text
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _still(frame: dict) -> tuple:
        """A frame without its title row and status line, which carry clocks."""
        return tuple(sorted((key, text) for key, text in frame.items()
                            if key[0] not in (0, 39)))

    def test_it_opens_with_motion_and_then_holds_still(self):
        screen, chosen, state = self._play([-1] * 80 + [ord("q")])
        self.assertIsNone(chosen)
        first, last = self._text(screen.frames[0]), self._text(screen.frames[-1])
        # The sentence types in, the cards deal in, the chart draws last.
        self.assertNotIn("4.00 AIU so far", first)
        self.assertNotIn("Spend through the day", first)
        self.assertNotIn("MODEL CALLS", first)
        self.assertIn("4.00 AIU so far", last)
        self.assertIn("Spend through the day", last)
        self.assertIn("MODEL CALLS", last)
        # Somewhere between, the counts were on their way up.
        middle = [self._text(f) for f in screen.frames[5:40]]
        self.assertTrue(any("MODEL CALLS" in text and "Spend through" not in text
                            for text in middle), "the cards did not deal in before the chart")
        self.assertTrue(state["dealt"])
        # Settled: redrawn once a second for the clock, and nothing moves.
        self.assertEqual(screen.delay, 1000)
        self.assertEqual(len({self._still(f) for f in screen.frames[-3:]}), 1)

    def test_with_motion_off_the_first_frame_is_the_last(self):
        screen, _chosen, _state = self._play([-1] * 3 + [ord("q")], motion=False)
        self.assertIn("4.00 AIU so far", self._text(screen.frames[0]))
        self.assertIn("Spend through the day", self._text(screen.frames[0]))
        self.assertEqual(self._still(screen.frames[0]), self._still(screen.frames[-1]))

    def test_the_logs_are_read_after_the_first_frame(self):
        from cs import cli

        _write_events(self.base, "sess-alpha", [
            _event("tool.execution_start", _log_stamp(datetime.now()),
                   {"toolCallId": "c1", "toolName": "bash"}),
            _event("tool.execution_complete", _log_stamp(datetime.now()),
                   {"toolCallId": "c1", "success": True})])
        seen: list[bool] = []
        real = cli._day_data

        def spy(*args, **kwargs):
            seen.append(kwargs.get("tools", True))
            return real(*args, **kwargs)

        with mock.patch.object(cli, "_day_data", side_effect=spy):
            screen, _chosen, _state = self._play([-1] * 40 + [ord("q")],
                                                 {"offset": 0, "dealt": True})
        self.assertEqual(seen[:2], [False, True])
        # The card holds its place, waiting, while the log is read…
        self.assertIn("reading the logs…", self._text(screen.frames[0]))
        # …and carries the count once the log has been read.
        self.assertRegex(self._text(screen.frames[-1]), r"0 failed")

    def test_arrows_step_through_days_and_enter_opens_a_session(self):
        screen, chosen, state = self._play([curses_key("KEY_LEFT"), *[-1] * 30, ord("t"),
                                            -1, 10], {"offset": 0, "dealt": True})
        self.assertEqual(chosen, "sess-alpha")
        self.assertEqual(state["offset"], 0)
        self.assertTrue(any("Yesterday" in self._text(f) for f in screen.frames))
        # → never goes past today.
        _screen, chosen, state = self._play([curses_key("KEY_RIGHT"), ord("q")],
                                            {"offset": 0, "dealt": True})
        self.assertEqual((chosen, state["offset"]), (None, 0))

    def test_a_reread_lights_the_figure_that_changed(self):
        from cs import cli, ui

        state = {"offset": 0, "dealt": True, "threaded": False}
        screen = _ClockScreen([-1] * 3)
        moved: list = []

        def tick():
            # Halfway through, a new call lands in the store.
            if screen.now > 1 and not moved:
                self._spend("sess-alpha", datetime.now().astimezone(), 2)
                moved.append(True)
            return screen.now

        keys = [-1] * ((cli.DAY_REFRESH_SECONDS + 2) * 2) + [ord("q")]
        screen.keys = keys
        with mock.patch.object(cli.time, "monotonic", side_effect=tick), \
                mock.patch.object(ui, "PULSE_MS", 10**9):
            cli._day_tui(screen, state)
        self.assertEqual(state["data"]["spend"]["nano_aiu"], 6_000_000_000)
        self.assertTrue(any("▲1" in self._text(f) for f in screen.frames),
                        "the calls card did not show what changed")

    def test_tabs_switch_with_a_slide_and_keep_their_place(self):
        screen, _chosen, state = self._play(
            [9, *[-1] * 30, ord("3"), *[-1] * 30, 353, *[-1] * 30, ord("q")],
            {"offset": 0, "dealt": True})
        texts = [self._text(f) for f in screen.frames]
        self.assertTrue(any("SESSION" in t and "REPOSITORY" in t for t in texts))
        self.assertTrue(any("AIU/MIN" in t for t in texts))
        # Shift-Tab from Breakdown lands on Sessions, and the page remembers it.
        self.assertEqual(state["tab"], 1)
        # The new tab's content slid in: its rows started further right.
        def left_edge(frame):
            return min((x for (y, x), text in frame.items() if 3 <= y < 38 and text.strip()),
                       default=0)
        switch = next(n for n, t in enumerate(texts) if "REPOSITORY" in t)
        self.assertGreater(left_edge(screen.frames[switch]), 0)
        self.assertEqual(left_edge(screen.frames[switch + 25]), 0)

    def test_a_running_session_spins_and_the_ticker_types_its_newest_event(self):
        from cs import cli

        folder = _write_events(self.base, "sess-alpha", [
            _event("user.message", _log_stamp(datetime.now() - timedelta(seconds=40)),
                   {"content": "ship the portal"}),
            _event("tool.execution_start", _log_stamp(datetime.now() - timedelta(seconds=5)),
                   {"toolCallId": "c1", "toolName": "bash",
                    "arguments": {"description": "Run the tests"}}),
        ]).parent
        (folder / f"inuse.{os.getpid()}.lock").write_text(str(os.getpid()))
        data = cli._day_data(0)
        self.assertEqual(data["running"], 1)
        self.assertEqual(self._card(data, "sess-alpha")["status"], "working")
        self.assertEqual(data["feed"][0]["what"], "bash")
        screen, _chosen, _state = self._play([-1] * 30 + [ord("q")],
                                             {"offset": 0, "dealt": True})
        foot = [text for (y, _x), text in screen.frames[-1].items() if y == 38]
        self.assertIn("Run the tests", " ".join(foot))
        # The spinner turns: the mark beside the session changes frame to frame.
        marks = {text for f in screen.frames for (y, x), text in f.items() if x == 2
                 and text in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏|/-\\"}
        self.assertGreater(len(marks), 1)
        self.assertEqual(screen.delay, 110)

    def test_a_reread_runs_in_the_background(self):
        from cs import cli

        state: dict = {"offset": 0}
        cli._day_refresh(state)
        state["worker"].join(10)
        data = cli._day_collect(state)
        self.assertEqual(data["spend"]["nano_aiu"], 4_000_000_000)
        self.assertIsNone(cli._day_collect(state), "a result is handed over once")

    def test_a_reread_that_breaks_says_so_but_a_busy_store_does_not(self):
        from cs import cli

        state: dict = {"offset": 0, "threaded": False}
        with mock.patch.object(cli, "_day_data", side_effect=sqlite3.OperationalError):
            cli._day_refresh(state)
        self.assertIsNone(cli._day_collect(state))
        with mock.patch.object(cli, "_day_data", side_effect=KeyError("bug")):
            cli._day_refresh(state)
        with self.assertRaises(KeyError):
            cli._day_collect(state)

    def test_it_is_the_first_row_on_the_home_menu(self):
        from cs import cli

        icon, label, _what, _action, asks = cli._home_items()[0]
        self.assertEqual((label, asks, cli._home_group(0)), ("Today", "", "Now"))
        self.assertIn("Today", [label for label, _group in cli._HOME_GROUP_STARTS])
        with mock.patch.object(cli, "cmd_day", return_value=True) as day:
            self.assertTrue(cli._home_items()[0][3]())
        day.assert_called_once_with()

    def test_it_goes_to_a_session_and_back(self):
        from cs import cli

        seen = []

        def wrapper(view, state):
            seen.append(view.__name__)
            return "sess-alpha" if len(seen) == 1 else None

        with mock.patch.object(cli, "_curses_wrapper", side_effect=wrapper), \
                mock.patch.object(cli, "cmd_show") as show, \
                mock.patch.object(cli.sys, "stdin", mock.Mock(isatty=lambda: True)), \
                mock.patch.object(cli.sys, "stdout", mock.Mock(isatty=lambda: True)):
            self.assertTrue(cli.cmd_day())
        show.assert_called_once_with("sess-alpha")
        self.assertEqual(seen, ["_day_tui", "_day_tui"])
