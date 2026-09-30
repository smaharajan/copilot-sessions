"""Live sessions: every Copilot CLI running now, on one page.

Running is decided by the lock a CLI holds, not by how recently the store
moved, and each session's status is an inference from the event log that has
to name its evidence. The page must hold its shape at every width, mask
what it prints, and leave the home strip counting every live CLI.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from support import Screen, StoreTest, _event, _write_events


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)
            ).strftime("%Y-%m-%dT%H:%M:%S")


def _dead_pid() -> int:
    pid = 999_999
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except OSError:
            pass
        pid += 1


def _start(call: str, name: str, at: str, agent: str | None = None, **args) -> dict:
    return _event("tool.execution_start", at,
                  {"toolCallId": call, "toolName": name, "arguments": args}, agent)


def _done(call: str, at: str, ok: bool = True, agent: str | None = None) -> dict:
    return _event("tool.execution_complete", at,
                  {"toolCallId": call, "success": ok, "model": "gpt-5.5"}, agent)


class _ClockScreen(Screen):
    """A Screen whose clock moves by the wait the page asked for."""

    now = 0.0
    delay = 0

    def timeout(self, milliseconds):
        super().timeout(milliseconds)
        self.delay = milliseconds

    def getch(self):
        key = super().getch()
        if key == -1:
            self.now += self.delay / 1000
        return key


class LiveTest(StoreTest):
    def setUp(self):
        super().setUp()
        self.base = Path(self._tmp.name)

    def _lock(self, sid: str, pid: int | None = None, events: list | None = None):
        _write_events(self.base, sid, events if events is not None else [
            _event("assistant.turn_end", _ago(60), {"turnId": "0"})])
        folder = self.base / "session-state" / sid
        pid = os.getpid() if pid is None else pid
        (folder / f"inuse.{pid}.lock").write_text(str(pid))
        return folder

    def _card(self, sid: str) -> dict:
        from cs import cli

        state: dict = {}
        cli._live_read(state)
        return {item["id"]: item for item in state["snapshot"]["sessions"]}[sid]

    # ── Which CLIs are running ────────────────────────────────────────

    def test_running_means_a_lock_held_by_a_live_process(self):
        from cs import db

        self._lock("sess-alpha")
        self._lock("sess-beta", _dead_pid())
        stale = self._lock("sess-gamma")
        old = time.time() - 3 * 86400
        os.utime(stale / "events.jsonl", (old, old))
        (self.base / "session-state" / "sess-alpha" / "inuse.x.lock").write_text("")
        self.assertEqual(db.running_sessions(), [("sess-alpha", os.getpid())])

    def test_no_session_state_is_nothing_running(self):
        from cs import db

        self.assertEqual(db.running_sessions(), [])

    # ── What each one is doing ────────────────────────────────────────

    def test_an_open_question_is_asking_you(self):
        self._lock("sess-alpha", events=[
            _event("user.message", _ago(90), {"content": "ship it"}),
            _start("c1", "ask_user", _ago(30), message="Which theme?"),
        ])
        card = self._card("sess-alpha")
        self.assertEqual(card["status"], "asking")
        self.assertEqual(card["asked"], "ship it")
        self.assertGreaterEqual(card["for"], 29)

    def test_an_open_tool_is_working_and_says_what_on(self):
        self._lock("sess-alpha", events=[
            _start("c1", "report_intent", _ago(20), intent="Running tests"),
            _done("c1", _ago(20)),
            _start("c2", "bash", _ago(10), command="make", description="Run tests"),
        ])
        card = self._card("sess-alpha")
        self.assertEqual(card["status"], "working")
        self.assertEqual(card["doing"], "bash · Run tests")
        self.assertEqual(card["intent"], "Running tests")

    def test_a_finished_turn_is_your_turn_and_a_long_one_is_idle(self):
        self._lock("sess-alpha", events=[
            _event("assistant.message", _ago(70), {"content": "All done."}),
            _event("assistant.turn_end", _ago(60), {"turnId": "0"})])
        self._lock("sess-beta", events=[
            _event("assistant.turn_end", _ago(2 * 3600), {"turnId": "0"})])
        self.assertEqual(self._card("sess-alpha")["status"], "waiting")
        self.assertEqual(self._card("sess-alpha")["said"], "All done.")
        self.assertEqual(self._card("sess-beta")["status"], "idle")

    def test_a_turn_in_flight_is_thinking(self):
        self._lock("sess-alpha", events=[
            _event("assistant.turn_start", _ago(1), {"turnId": "1"})])
        self.assertEqual(self._card("sess-alpha")["status"], "thinking")

    def test_three_failures_in_a_row_is_failing_until_one_succeeds(self):
        failing = [e for n in range(3) for e in (
            _start(f"c{n}", "edit", _ago(9 - n)), _done(f"c{n}", _ago(9 - n), False))]
        self._lock("sess-alpha", events=failing)
        card = self._card("sess-alpha")
        self.assertEqual(card["status"], "failing")
        self.assertIn("3 failed calls in a row", card["doing"])
        self._lock("sess-alpha", events=[
            *failing, _start("c9", "edit", _ago(5)), _done("c9", _ago(5))])
        self.assertNotEqual(self._card("sess-alpha")["status"], "failing")

    def test_running_sub_agents_are_working_and_their_calls_are_not_the_main_agents(self):
        self._lock("sess-alpha", events=[
            _event("subagent.started", _ago(40),
                   {"agentDisplayName": "Explore", "toolCallId": "t"}, "a1"),
            _start("s1", "grep", _ago(30), "a1"),
            _done("s1", _ago(30), False, "a1"),
            _start("s2", "grep", _ago(20), "a1"),
        ])
        card = self._card("sess-alpha")
        self.assertEqual(card["status"], "working")
        self.assertEqual(card["doing"], "1 sub-agent: Explore")
        self.assertEqual(card["agents"], 1)
        self.assertEqual(card["failures"], 1)

    def test_the_tail_reads_only_what_was_added(self):
        from cs import cli

        folder = self._lock("sess-alpha", events=[
            _start("c1", "bash", _ago(5), description="first")])
        state: dict = {}
        cli._live_read(state)
        tail = state["tails"]["sess-alpha"]
        offset = tail.offset
        with open(folder / "events.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(_done("c1", _ago(1)), separators=(",", ":")) + "\n")
        cli._live_read(state)
        self.assertGreater(tail.offset, offset)
        self.assertEqual(tail.calls, 1)
        self.assertIs(state["tails"]["sess-alpha"], tail)

    def test_a_status_change_is_marked_for_the_flash(self):
        from cs import cli

        folder = self._lock("sess-alpha", events=[
            _start("c1", "bash", _ago(5), description="build")])
        state: dict = {}
        cli._live_read(state)
        self.assertEqual(state["marks"], {})
        with open(folder / "events.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(_start("c2", "ask_user", _ago(1)),
                                    separators=(",", ":")) + "\n")
        cli._live_read(state)
        self.assertIn("sess-alpha", state["marks"])

    def test_todos_come_from_the_sessions_own_database(self):
        folder = self._lock("sess-alpha")
        conn = sqlite3.connect(folder / "session.db")
        conn.execute("CREATE TABLE todos (id TEXT, title TEXT, status TEXT)")
        conn.executemany("INSERT INTO todos VALUES (?,?,?)", [
            ("a", "Write it", "done"), ("b", "Test it", "in_progress"),
            ("c", "Ship it", "pending")])
        conn.commit()
        conn.close()
        self.assertEqual(self._card("sess-alpha")["todos"], (1, 3, "Test it"))

    def test_what_it_prints_is_masked(self):
        from cs import cli

        secret = "ghp_" + "c" * 36
        self._lock("sess-alpha", events=[
            _event("user.message", _ago(9), {"content": f"use {secret}"}),
            _event("assistant.turn_end", _ago(8), {"turnId": "0"})])
        text = cli._live_text(120)
        self.assertIn("use ", text)
        self.assertNotIn(secret, text)

    # ── The page ─────────────────────────────────────────────────────

    def test_the_page_holds_its_shape_at_every_width(self):
        from cs import cli, ui

        self._lock("sess-alpha", events=[
            _start("c1", "bash", _ago(5), description="a long description " * 9)])
        self._lock("sess-beta")
        state: dict = {}
        cli._live_read(state)
        for width in (40, 60, 80, 100, 140):
            for height in (None, 24, 48):
                with self.subTest(width=width, height=height):
                    rows, hits = cli._live_screen(state["snapshot"], width, height)
                    for line in rows:
                        for x, text, _role in line:
                            self.assertLessEqual(x + ui.cells(text), width - 1, text)
                    if height:
                        self.assertLessEqual(len(rows), height - 1)
                    self.assertIn("2 live", " ".join(t for _x, t, _r in rows[0]))
                    self.assertEqual({hit[3] for hit in hits}, {0, 1})
            self.assertLessEqual(ui.cells(cli._live_hint(width)), width - 1)

    def test_nothing_running_says_so(self):
        from cs import cli

        text = cli._live_text(80)
        self.assertIn("0 live", text)
        self.assertIn("No Copilot CLI is running right now.", text)

    def test_enter_opens_the_chosen_session_and_q_goes_back(self):
        from cs import cli

        self._lock("sess-alpha", events=[
            _start("c1", "ask_user", _ago(5))])
        self._lock("sess-beta")
        state: dict = {"dealt": True}
        chosen = cli._live_tui(Screen([ord("j"), 10]), state)
        self.assertEqual(chosen, "sess-beta")
        self.assertIsNone(cli._live_tui(Screen([ord("q")]), state))

    # ── One session's page ───────────────────────────────────────────

    def _focus(self, sid: str = "sess-alpha") -> tuple[dict, dict]:
        from cs import cli

        state: dict = {"dealt": True, "focus": sid}
        cli._live_read(state)
        item = {s["id"]: s for s in state["snapshot"]["sessions"]}[sid]
        return state, item

    def test_a_sessions_page_holds_its_shape_and_masks_what_it_prints(self):
        from cs import cli, ui

        secret = "ghp_" + "d" * 36
        self._lock("sess-alpha", events=[
            _event("user.message", _ago(90), {"content": f"deploy with {secret}"}),
            _start("c1", "bash", _ago(80), description="a long description " * 9),
            _done("c1", _ago(79), False),
            _event("assistant.message", _ago(70), {"content": "Done. " * 80}),
            _event("assistant.turn_end", _ago(60), {"turnId": "0"})])
        state, item = self._focus()
        tail = state["tails"]["sess-alpha"]
        for width in (40, 60, 80, 100, 140):
            for height in (12, 24, 48):
                with self.subTest(width=width, height=height):
                    rows, back, total = cli._focus_screen(item, tail, width, height)
                    self.assertLessEqual(len(rows), height - 1)
                    for line in rows:
                        for x, text, _role in line:
                            self.assertLessEqual(x + ui.cells(text), width - 1, text)
                    text = "\n".join(" ".join(t for _x, t, _r in line) for line in rows)
                    self.assertIn("Live ›", text)
                    self.assertNotIn(secret, text)
                    self.assertEqual(back, 0)
                    self.assertGreater(total, 3, "a long reply wraps")
            self.assertLessEqual(ui.cells(cli._focus_hint(width)), width - 1)
        rows, _back, _total = cli._focus_screen(item, tail, 100, 48)
        text = ""
        for line in rows:
            for x, part, _role in sorted(line):
                text += " " * max(x - ui.cells(text.rsplit("\n", 1)[-1]), 0) + part
            text += "\n"
        for expected in ("your turn", "deploy with", "Done. Done.", "✗", "FAILED 1"):
            self.assertIn(expected, text)

    def test_enter_opens_the_sessions_page_where_keys_move_and_q_goes_back(self):
        from cs import cli

        self._lock("sess-alpha", events=[_start("c1", "ask_user", _ago(5))])
        self._lock("sess-beta")
        state, _item = self._focus()
        self.assertIsNone(cli._live_focus_tui(Screen([ord("q")]), state))
        self.assertEqual(cli._live_focus_tui(Screen([10]), state), "show")
        self.assertEqual(cli._live_focus_tui(Screen([ord("n"), ord("q")]), state), None)
        self.assertEqual((state["focus"], state["cursor"]), ("sess-beta", 1))
        cli._live_focus_tui(Screen([ord("p"), ord("q")]), state)
        self.assertEqual(state["focus"], "sess-alpha")

    def test_scrolled_back_the_view_holds_while_new_lines_land_below(self):
        from cs import cli

        folder = self._lock("sess-alpha", events=[
            _start(f"c{n}", "bash", _ago(90 - n), description=f"step {n}")
            for n in range(40)])
        state, item = self._focus()
        tail = state["tails"]["sess-alpha"]

        def top(back: int) -> str:
            rows, _back, _total = cli._focus_screen(item, tail, 80, 24, {"back": back})
            return " ".join(t for _x, t, _r in rows[-15])

        before = top(5)
        with open(folder / "events.jsonl", "a", encoding="utf-8") as handle:
            for n in range(40, 43):
                handle.write(json.dumps(_start(f"c{n}", "bash", _ago(1),
                                               description=f"step {n}"),
                                        separators=(",", ":")) + "\n")
        tail.read()
        self.assertEqual(top(8), before)
        self.assertIn("step 42", " ".join(
            t for line in cli._focus_screen(item, tail, 80, 24)[0] for _x, t, _r in line))

    def test_a_sessions_page_opens_with_motion_and_then_holds_still(self):
        from cs import cli, ui

        folder = self._lock("sess-alpha", events=[
            _event("assistant.turn_end", _ago(60), {"turnId": "0"})])
        old = time.time() - 600
        for lock in folder.glob("inuse.*.lock"):
            os.utime(lock, (old, old))
        state, _item = self._focus()
        screen = _ClockScreen([-1] * 60 + [ord("q")])
        wall = time.time()
        with (
            mock.patch.object(ui, "PULSE_MS", 10**9),
            mock.patch.object(cli.time, "monotonic", side_effect=lambda: screen.now),
            mock.patch.object(cli.time, "time", side_effect=lambda: wall + screen.now),
            mock.patch.object(cli.time, "strftime", return_value="12:00:00"),
        ):
            cli._live_focus_tui(screen, state)
        self.assertNotEqual(screen.frames[0], screen.frames[-1], "no entrance")
        self.assertEqual(screen.delay, 1000)
        self.assertEqual(len({tuple(sorted(f.items())) for f in screen.frames[-3:]}), 1)

    def test_live_goes_from_the_list_to_a_session_and_back(self):
        from cs import cli

        views = iter(["sess-alpha", "show", "show", None, None])
        seen = []

        def wrapper(view, state):
            seen.append((view.__name__, state.get("focus"), state.pop("note", "")))
            return next(views)

        with (
            mock.patch.object(cli.sys.stdin, "isatty", return_value=True),
            mock.patch.object(cli.sys.stdout, "isatty", return_value=True),
            mock.patch.object(cli, "_curses_wrapper", side_effect=wrapper),
            mock.patch.object(cli, "cmd_show",
                              side_effect=[None, SystemExit(1)]) as show,
        ):
            self.assertTrue(cli.cmd_live())
        self.assertEqual([call.args for call in show.call_args_list],
                         [("sess-alpha",), ("sess-alpha",)])
        self.assertEqual([name for name, _sid, _note in seen], [
            "_live_tui", "_live_focus_tui", "_live_focus_tui",
            "_live_focus_tui", "_live_tui"])
        self.assertIn("Not in the store yet", seen[3][2])

    def test_the_sessions_deal_in_and_then_the_page_stops_moving(self):
        from cs import cli, ui

        old = time.time() - 600
        for sid in ("sess-alpha", "sess-beta"):
            for lock in self._lock(sid).glob("inuse.*.lock"):
                os.utime(lock, (old, old))
        screen = _ClockScreen([-1] * 60 + [ord("q")])
        wall = time.time()
        with (
            mock.patch.object(ui, "PULSE_MS", 10**9),
            mock.patch.object(cli.time, "monotonic", side_effect=lambda: screen.now),
            mock.patch.object(cli.time, "time", side_effect=lambda: wall + screen.now),
            mock.patch.object(cli.time, "strftime", return_value="12:00:00"),
        ):
            cli._live_tui(screen, {})
        # Each session is two rows in the list, each starting with its chip.
        dealt = [len({y for (y, x), text in frame.items()
                      if x == 2 and text.startswith("▌")}) for frame in screen.frames]
        self.assertLess(dealt[0], 4)
        self.assertEqual(dealt[-1], 4)
        # Settled: redrawn once a second for the clocks, and nothing else moves.
        self.assertEqual(screen.delay, 1000)
        self.assertEqual(len({tuple(sorted(f.items())) for f in screen.frames[-3:]}), 1)

    def test_new_activity_lands_in_the_feed_and_lights_up(self):
        from cs import cli

        folder = self._lock("sess-alpha", events=[
            _start("c1", "bash", _ago(5), description="build it")])
        state: dict = {}
        cli._live_read(state)
        self.assertEqual(state["arrivals"], {})
        self.assertEqual(state["snapshot"]["feed"][0]["detail"], "build it")
        with open(folder / "events.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(_start("c2", "view", _ago(1), path="/a/b/notes.md"),
                                    separators=(",", ":")) + "\n")
        cli._live_read(state)
        newest = state["snapshot"]["feed"][0]
        self.assertEqual((newest["what"], newest["detail"]), ("view", "notes.md"))
        self.assertEqual(list(state["arrivals"]), [newest["key"]])

    def test_compacting_is_thinking_and_says_so(self):
        start = _event("session.compaction_start", _ago(30), {})
        self._lock("sess-alpha", events=[start])
        card = self._card("sess-alpha")
        self.assertEqual((card["status"], card["doing"]),
                         ("thinking", "compacting the conversation"))
        self._lock("sess-alpha", events=[
            start, _event("session.compaction_complete", _ago(20), {}),
            _event("assistant.turn_end", _ago(10), {"turnId": "0"})])
        card = self._card("sess-alpha")
        self.assertEqual((card["status"], card["compactions"]), ("waiting", 1))

    def test_live_runs_as_a_command_and_as_data(self):
        self._lock("sess-alpha")
        code, out = self._run("live")
        self.assertEqual(code, 0)
        self.assertIn("1 live", out)
        code, out = self._run("live", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["view"], "live")
        self.assertEqual([s["id"] for s in payload["sessions"]], ["sess-alpha"])

    def test_the_home_menu_opens_it_first(self):
        from cs import cli

        _icon, label, _what, action, asks = cli._home_items()[0]
        self.assertEqual((label, asks), ("Live sessions", ""))
        self.assertEqual(cli._home_group(0), "Find")
        with mock.patch.object(cli, "cmd_live", return_value=True) as live:
            self.assertTrue(cli._home_items()[0][3]())
        live.assert_called_once()

    # ── The home strip ───────────────────────────────────────────────

    def test_the_home_strip_counts_every_live_cli(self):
        from cs import cli

        self._lock("sess-alpha")
        self._lock("sess-beta")
        os.utime(self.base / "session-state" / "sess-beta" / "events.jsonl")
        state: dict = {}
        cli._watch_read(state)
        self.assertEqual(state["session"]["live"], 2)
        self.assertEqual(state["session"]["id"], "sess-beta")
        for width in (40, 60, 80, 100, 140):
            with self.subTest(width=width):
                text = " ".join(t for t, _ in cli._watch_lines(state, width))
                self.assertIn("2 live", text)
