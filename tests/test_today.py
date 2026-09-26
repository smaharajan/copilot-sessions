"""Day-to-day views: next up, end of day, weekly review, saved searches,
the budget check and clean-up.

Each reason a session is put in front of you must be printed with it, every
stored string is masked on its way to the page and to `--json`, and nothing
here writes anywhere but cs's own settings file.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from support import (
    CALM,
    Screen,
    StoreTest,
    _add_governance_rows,
    _alpha_events,
    _tool,
    _write_events,
)

# A credential-shaped value, built by concatenation so it never sits whole in
# the source. It is planted in a summary, a prompt and a commit value.
SECRET = "ghp_" + "K" * 36  # gitleaks:allow


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")


class TodayTest(StoreTest):
    def setUp(self):
        super().setUp()
        self.base = Path(self._tmp.name)
        _add_governance_rows(self.base)
        _write_events(self.base, "sess-alpha", _alpha_events())
        self.conn = sqlite3.connect(self.base / "session-store.db")

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def _session(self, sid: str, summary: str, when: str, prompts=(),
                 repo: str = "acme/portal") -> None:
        self.conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,'local','main',?,?,?)",
            (sid, "/tmp/x", repo, summary, when, when))
        for index, prompt in enumerate(prompts):
            self.conn.execute(
                "INSERT INTO turns (session_id, turn_index, user_message, "
                "assistant_response, timestamp) VALUES (?,?,?,?,?)",
                (sid, index, prompt, "ok", when))
        self.conn.commit()

    def _json(self, *args: str) -> dict:
        code, out = self._run(*args, "--json")
        self.assertEqual(code, 0, out)
        return json.loads(out)

    # ── Next up ──────────────────────────────────────────────────────

    def test_next_ranks_sessions_and_says_why(self):
        from cs import ui

        ui.pin_session("sess-empty")
        ui.add_tag(CALM, "WIP")        # tags match whatever their case
        self._session("open-handoff", "Leave notes for tomorrow", _ago(1),
                      ["create a handoff file for tomorrow"])
        self.conn.execute("INSERT INTO session_files (session_id, file_path, "
                          "tool_name) VALUES ('open-handoff', "
                          "'/tmp/x/handoff-notes.md', 'create')")
        self.conn.commit()
        data = self._json("next")
        order = [s["id"] for s in data["sessions"]]
        # Ended in an error and stuck in a loop outranks an open handoff;
        # the handoff outranks a wip tag, and that outranks a bare pin.
        self.assertEqual(order[:4], ["sess-alpha", "open-handoff", CALM, "sess-empty"])
        alpha = data["sessions"][0]
        kinds = [r["kind"] for r in alpha["reasons"]]
        self.assertEqual(kinds, ["ended", "stuck"])
        self.assertIn("error at turn 1", alpha["reasons"][0]["why"])
        self.assertIn("bash failed 3× in a row", alpha["reasons"][1]["why"])
        handoff = data["sessions"][1]["reasons"][0]
        self.assertIn("handoff-notes.md", handoff["why"])
        self.assertIn("no later session opened it", handoff["why"])

    def test_a_picked_up_handoff_and_an_unrecorded_ending_are_not_next(self):
        ids = {s["id"] for s in self._json("next")["sessions"]}
        # PARENT's handoff was picked up by CHILD; the governance sessions end
        # with no recorded finish reason, which is nothing to act on.
        self.assertNotIn("44444444-4444-4444-8444-444444444444", ids)
        self.assertNotIn("11111111-1111-4111-8111-111111111111", ids)

    def test_next_prints_resume_commands_that_resolve(self):
        code, out = self._run("next")
        self.assertEqual(code, 0)
        self.assertIn("cs resume 1", out)
        self.assertIn("last call ended: error", out)
        code, out = self._run("show", "1")
        self.assertIn("Build Three.js portal", out)

    def test_next_opens_as_a_listing_in_a_terminal(self):
        from cs import cli

        seen = []
        with mock.patch.object(cli, "_interactive_listing",
                               side_effect=lambda *a, **k: seen.append((a, k)) or True), \
                mock.patch("sys.stdin", mock.Mock(isatty=lambda: True)), \
                mock.patch("sys.stdout", mock.Mock(isatty=lambda: True)):
            self.assertTrue(cli.cmd_next())
        rows, title = seen[0][0][:2]
        self.assertEqual(rows[0][0], "sess-alpha")
        self.assertIn("Next up", title)
        self.assertEqual(seen[0][1]["default_sort"], "relevance")
        self.assertIn("error", seen[0][1]["hits"]["sess-alpha"][1])

    # ── End of day ───────────────────────────────────────────────────

    def test_eod_reports_today_only(self):
        from cs import ui

        ui.set_daily_budget(3)
        data = self._json("eod")
        ids = [s["id"] for s in data["sessions"]]
        self.assertIn("sess-alpha", ids)
        self.assertNotIn("11111111-1111-4111-8111-111111111111", ids)  # 9 days ago
        self.assertEqual([c["value"] for c in data["commits"]], ["abc1234"])
        self.assertEqual([p["value"] for p in data["prs"]], ["42"])
        # sess-alpha's 4 AIU, plus the governance fixture's calls, which are
        # all stamped at the moment the store was built.
        self.assertEqual(data["nano_aiu"], 4_000_115_000)
        self.assertTrue(data["over_budget"])
        self.assertEqual((data["tool_failures"], data["stuck_loops"]), (4, 1))

    def test_eod_markdown_is_paste_ready_and_masked(self):
        self.conn.execute("UPDATE sessions SET summary = ? WHERE id = 'sess-alpha'",
                          (f"rotate {SECRET}",))
        self.conn.execute("UPDATE session_refs SET ref_value = ? WHERE "
                          "ref_type = 'commit'", (f"{SECRET}",))
        self.conn.commit()
        code, out = self._run("eod", "--md")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("## End of day · "))
        self.assertIn("### What moved", out)
        self.assertIn("### Commits and PRs", out)
        self.assertNotIn("\x1b[", out)
        self.assertNotIn(SECRET, out)
        for args in (("eod",), ("eod", "--json")):
            self.assertNotIn(SECRET, self._run(*args)[1])

    # ── Weekly review ────────────────────────────────────────────────

    def test_weekly_compares_with_the_week_before(self):
        self.conn.execute(
            "INSERT INTO assistant_usage_events (session_id, turn_index, model, "
            "total_nano_aiu, created_at) VALUES ('sess-alpha', 0, 'gpt-5.5', "
            "2000000000, datetime('now', '-10 days'))")
        self.conn.commit()
        _write_events(self.base, CALM, [
            *_tool("w1", "bash", _ago(2)[:19], False)])
        data = self._json("weekly")
        self.assertEqual(data["nano_aiu"], 4_000_000_000 + 60_000 + 50_000 + 5_000)
        self.assertEqual(data["previous_nano_aiu"], 2_000_000_000)
        self.assertGreater(data["change"], 0)
        self.assertEqual(data["top_sessions"][0]["id"], "sess-alpha")
        repeated = {r["tool"]: r for r in data["repeated_failures"]}
        self.assertEqual(repeated["bash"]["sessions"], 2)
        self.assertLessEqual(len(data["habits"]), 3)
        code, out = self._run("weekly", "--md")
        self.assertTrue(out.startswith("## Weekly review"))
        self.assertIn("`bash`", out)

    # ── Saved searches ───────────────────────────────────────────────

    def test_a_search_is_saved_listed_and_run_by_name(self):
        from cs import ui

        code, out = self._run("search", "--save", "portal", "three.js")
        self.assertEqual(code, 0)
        self.assertIn("saved 'portal'", out)
        self.assertEqual(ui.saved_searches(), {"portal": "three.js"})
        self.assertTrue(str(ui.settings_path()).startswith(
            str(self.base / ".config")))
        code, out = self._run("saved")
        self.assertIn("three.js", out)
        code, out = self._run("saved", "portal")
        self.assertIn("Build Three.js portal", out)
        self.assertEqual(self._json("saved")["searches"],
                         [{"name": "portal", "term": "three.js"}])
        code, err = self._run_err("saved", "nope")
        self.assertEqual(code, 1)
        self.assertIn("no saved search named 'nope'", err)

    def test_a_pinned_session_opens_from_the_pinned_listing(self):
        """'v' on a pinned row used to raise KeyError: the listing was
        imported inside `cmd_pins`, un-lifted, and its `show` could not find
        the reader."""
        from cs import cli, ui

        ui.pin_session("sess-alpha")
        with mock.patch.object(cli, "_curses_wrapper",
                               side_effect=[("show", "sess-alpha"), None]), \
                mock.patch.object(cli, "_pause", return_value=True), \
                mock.patch("sys.stdin", mock.Mock(isatty=lambda: True)), \
                mock.patch("sys.stdout", mock.Mock(isatty=lambda: True)), \
                mock.patch.object(cli, "_page", return_value=True) as page:
            self.assertTrue(cli.cmd_pins())
        self.assertIn("Build Three.js portal", page.call_args.args[0])

    def test_the_home_row_picks_a_saved_search_and_runs_it(self):
        from cs import cli, ui

        ui.save_search("mine", "portal")
        with mock.patch.object(cli, "_curses_wrapper", return_value=0), \
                mock.patch.object(cli, "cmd_search", return_value=True) as search:
            self.assertTrue(cli.cmd_saved_menu())
        search.assert_called_once_with("portal")

    # ── Budget ───────────────────────────────────────────────────────

    def test_budget_check_exits_one_only_when_over(self):
        from cs import ui

        code, out = self._run("budget", "--check")
        self.assertEqual((code, out.count("\n")), (0, 1))
        self.assertIn("none set", out)
        ui.set_daily_budget(10)
        self.assertEqual(self._run("budget", "--check")[0], 0)
        spent = self._json("budget")["nano_aiu"] / 1e9
        ui.set_daily_budget(spent)      # exactly the day's spend: still within
        self.assertEqual(self._run("budget", "--check")[0], 0)
        ui.set_daily_budget(3)
        code, out = self._run("budget", "--check")
        self.assertEqual(code, 1)
        self.assertIn("over by 1.00", out)
        self.assertTrue(self._json("budget")["over"])

    def test_arrows_step_the_window_and_budget_stays_a_command(self):
        import curses

        from cs import cli, ui

        self.assertNotIn("Budget", [item[1] for item in cli._home_items()])
        screen = Screen([curses.KEY_RIGHT, ord("q")])
        state = {"period": 30, "revealed": True}
        cli._home_tui(screen, state)
        self.assertEqual(state["period"], 90)
        ui.set_daily_budget(42)
        self.assertEqual(ui.step_budget(1), 50)
        self.assertEqual(ui.step_budget(-1), 25)
        ui.set_daily_budget(5)
        self.assertIsNone(ui.step_budget(-1))

    # ── Clean-up ─────────────────────────────────────────────────────

    def test_cleanup_suggests_and_removes_nothing(self):
        from cs import ui

        self._session("old-pin", "Pinned long ago", _ago(20))
        ui.pin_session("old-pin")
        ui.pin_session("gone-from-the-store")
        ui.pin_session("sess-alpha")                # active today: not stale
        # The fixture puts CALM at 09:20 UTC seven days ago, which is under
        # seven whole days before 09:20 — half a day more holds at any hour.
        self.conn.execute(
            "UPDATE sessions SET created_at = ?, updated_at = ? WHERE id = ?",
            (_ago(7.6), _ago(7.5), CALM))
        self.conn.commit()
        ui.add_tag(CALM, "wip")                     # quiet 7+ days
        self._session("stale-handoff", "Notes left behind", _ago(10),
                      ["create a handoff doc for the next person"])
        data = self._json("cleanup")
        self.assertEqual({p["id"] for p in data["pins"]},
                         {"old-pin", "gone-from-the-store"})
        self.assertEqual([w["command"] for w in data["wip"]],
                         [f"cs untag {CALM[:8]} wip"])
        self.assertIn("stale-handoff", {h["id"] for h in data["handoffs"]})
        code, out = self._run("cleanup")
        self.assertIn("cs unpin old-pin", out)
        self.assertIn("never unpins", out)
        self.assertEqual(set(ui.pinned_ids()),
                         {"old-pin", "gone-from-the-store", "sess-alpha"})
        self.assertIn("wip", ui.annotation(CALM)["tags"])


class TodayHomeTest(StoreTest):
    def test_today_opens_the_menu_and_the_groups_are_where_the_brief_puts_them(self):
        from cs import cli

        items = cli._home_items(7)
        labels = [item[1] for item in items]
        by_group: dict[str, list[str]] = {}
        for index, label in enumerate(labels):
            by_group.setdefault(cli._home_group(index), []).append(label)
        self.assertEqual(by_group["Today"], ["Today"])
        self.assertIn("Saved searches", by_group["Find"])
        self.assertNotIn("Similar work", labels)
        self.assertNotIn("My asks", labels)
        self.assertNotIn("File history", labels)
        self.assertEqual(by_group["Improve"][0], "Context")
        self.assertIn("Clean-up", by_group["Improve"])
        self.assertEqual(labels[0], "Today")

    def test_every_new_row_opens(self):
        from cs import cli

        items = {item[1]: item for item in cli._home_items(7)}
        with mock.patch.object(cli, "_page", return_value=True) as page, \
                mock.patch.object(cli, "_interactive_listing", return_value=True), \
                redirect_stdout(io.StringIO()):
            for label, given in (("Today", None), ("Clean-up", None),
                                 ("Saved searches", None)):
                with self.subTest(row=label):
                    action = items[label][3]
                    result = action(given) if given is not None else action()
                    self.assertIsNotNone(result)
        self.assertGreaterEqual(page.call_count, 2)

    def test_now_leads_with_the_session_its_burn_and_the_budget(self):
        from cs import cli

        data = cli._today_data()
        self.assertIn("now", data)
        now = data["now"]
        self.assertEqual(now["summary"], "Build Three.js portal")
        self.assertIn("burn", now)
        text = cli._capture(lambda: cli._render_today(data))
        plain = __import__("re").sub(r"\x1b\[[0-9;]*m", "", text)
        self.assertIn("Build Three.js portal", plain)
        self.assertIn("AIU/min", plain)
        self.assertIn(f"cs resume {now['id'][:8]}", plain)
        self.assertIn("live", plain)
        burn = next(line for line in plain.splitlines() if "AIU/min" in line)
        self.assertIn("·", burn)
        # A session with no failure does not grow a fail row.
        quiet = {**now, "last_failure": None, "last_tool": "view",
                 "last_tool_at": "12:01:00"}
        card = cli._capture(lambda: cli._render_now(quiet, 80, 76))
        card_plain = __import__("re").sub(r"\x1b\[[0-9;]*m", "", card)
        self.assertNotIn("fail", card_plain)
        self.assertIn("view", card_plain)

    def test_the_now_card_keeps_its_column_and_its_command(self):
        from cs import cli

        now = {
            "id": "sess-alpha", "summary": "Build Three.js portal",
            "repo": "acme/portal", "turns": 2, "nano_aiu": 4_000_000_000,
            "burn_per_minute": 0.4,
            "burn": [0, 0, 0, 0, 0, 0, 0, 1, 4, 9],
            "today_nano_aiu": 4_000_000_000, "budget_aiu": 20, "left_aiu": 16,
            "last_tool": "view", "last_tool_at": "12:01:00",
            "last_failure": None,
        }
        over = {**now, "today_nano_aiu": 25_000_000_000, "left_aiu": -5}
        for columns in (40, 60, 80, 100):
            card = cli._capture(lambda c=columns: cli._render_now(now, c, c - 4))
            plain = __import__("re").sub(r"\x1b\[[0-9;]*m", "", card)
            self.assertIn("cs resume sess-alp", plain)
            self.assertIn("left", plain)
            self.assertNotIn("fail", plain)
            burn = next(line for line in plain.splitlines() if "AIU/min" in line)
            budget = next(line for line in plain.splitlines() if "left" in line)
            self.assertEqual(burn.index("0.40"), budget.index("4.00"))
            for line in plain.splitlines():
                self.assertLessEqual(cli.ui.cells(line), columns, line)
            hot = cli._capture(lambda c=columns: cli._render_now(over, c, c - 4))
            hot_plain = __import__("re").sub(r"\x1b\[[0-9;]*m", "", hot)
            self.assertIn("over", hot_plain)
            self.assertIn("cs resume sess-alp", hot_plain)
            for line in hot_plain.splitlines():
                self.assertLessEqual(cli.ui.cells(line), columns, line)

    def test_a_pick_up_reason_too_long_to_keep_stays_under_its_title(self):
        from cs import cli

        def reason(why: str, sid: str) -> dict:
            return {"id": sid, "summary": "Prepare the handoff",
                    "resume": f"cs resume {sid[:8]}",
                    "reasons": [{"kind": "handoff", "why": why}]}

        sessions = [reason("wrote " + ", ".join(f"HANDOFF-{n}.md" for n in range(20)),
                           "c5c88889-long"),
                    reason("asked for a handoff; no one picked it up", "51b00f8a-short")]
        for columns in (40, 80, 120):
            text = cli._capture(lambda c=columns: cli._render_pickup(sessions, c, c - 4))
            plain = __import__("re").sub(r"\x1b\[[0-9;]*m", "", text)
            details = [line for line in plain.splitlines()
                       if line.lstrip().startswith(("wrote", "asked"))]
            self.assertTrue(details, plain)
            for line in details:
                self.assertTrue(line.startswith(" " * 7), line)
            self.assertIn("cs resume c5c88889", plain)
            for line in plain.splitlines():
                self.assertLessEqual(cli.ui.cells(line), columns, line)

    def test_today_page_fits_a_short_window(self):
        import shutil

        from cs import cli

        data = cli._today_data()
        resume = f"cs resume {data['now']['id'][:8]}" if data.get("now") else ""
        for columns in (40, 100):
            size = os.terminal_size((columns, 24))
            with mock.patch.object(shutil, "get_terminal_size", return_value=size):
                text = cli._capture(lambda: cli._render_today(data))
            plain = __import__("re").sub(r"\x1b\[[0-9;]*m", "", text)
            lines = [line for line in plain.splitlines() if line.strip()]
            self.assertLessEqual(len(lines), 22, plain)
            if resume:
                self.assertIn(resume, plain)
            for line in plain.splitlines():
                self.assertLessEqual(cli.ui.cells(line), columns, line)
