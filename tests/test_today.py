"""Day-to-day views: next up, end of day, weekly review, similar work, my asks,
saved searches, file history, the budget check and clean-up.

Each reason a session is put in front of you must be printed with it, every
stored string is masked on its way to the page and to `--json`, and nothing
here writes anywhere but cs's own settings file.
"""

from __future__ import annotations

import io
import json
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
    _event,
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

    # ── Similar work ─────────────────────────────────────────────────

    def test_similar_puts_the_sessions_that_shipped_first(self):
        later = datetime.now(timezone.utc) + timedelta(minutes=5)
        self._session("sess-newer", "Another portal attempt",
                      later.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
        code, out = self._run("search", "portal")
        self.assertLess(out.index("Another portal attempt"),
                        out.index("Build Three.js portal"))
        data = self._json("similar", "portal")
        self.assertEqual(data["sessions"][0]["id"], "sess-alpha")
        self.assertEqual(data["sessions"][0]["outcome"], "1 commit · 1 PR")
        code, out = self._run("similar", "portal")
        self.assertLess(out.index("Build Three.js portal"),
                        out.index("Another portal attempt"))
        self.assertIn("1 commit · 1 PR", out)

    # ── My asks ──────────────────────────────────────────────────────

    def test_asks_lists_openers_and_the_turn_after_a_handoff(self):
        self._session("sess-pickup", "Carry on", _ago(0.5),
                      ["good morning", "read the handoff from the previous session",
                       f"and use token {SECRET}"])
        data = self._json("asks")
        mine = [a for a in data["asks"] if a["id"] == "sess-pickup"]
        self.assertEqual([(a["turn"], a["kind"]) for a in mine],
                         [(0, "opening"), (1, "after handoff")])
        alpha = next(a for a in data["asks"] if a["id"] == "sess-alpha")
        self.assertEqual((alpha["ask"], alpha["outcome"]),
                         ("make a portal", "1 commit · 1 PR"))

    def test_asks_are_masked_and_filterable_by_repo(self):
        self._session("sess-secret", "Paste", _ago(0.5), [f"use {SECRET} now"],
                      repo="acme/other")
        for args in (("asks",), ("asks", "--json")):
            code, out = self._run(*args)
            self.assertEqual(code, 0)
            self.assertNotIn(SECRET, out)
        only = self._json("asks", "--repo", "other")
        self.assertEqual({a["id"] for a in only["asks"]}, {"sess-secret"})

    def test_c_copies_the_ask_in_the_listing(self):
        from cs import cli

        rows = [("sess-alpha", "2026-09-01T12:00", "make a portal", "r/a", "/tmp", 1, 0)]
        screen = Screen([ord("c")])
        self.assertEqual(cli._listing_tui(screen, rows, "My asks",
                                          copy={"sess-alpha": "make a portal"}),
                         ("copy", "sess-alpha"))
        # Without anything to copy, 'c' is a letter and opens the filter.
        screen = Screen([ord("c"), 10, ord("q")])
        self.assertIsNone(cli._listing_tui(screen, rows, "Sessions"))
        self.assertIn("filter 'c'", screen.frames[-1][(0, 0)])

    def test_copy_uses_a_clipboard_tool_only_if_there_is_one(self):
        from cs import cli

        with mock.patch("shutil.which", return_value=None), \
                redirect_stdout(io.StringIO()) as out:
            cli._copy_ask("the whole ask")
        self.assertIn("the whole ask", out.getvalue())
        with mock.patch("shutil.which",
                        side_effect=lambda tool: tool if tool == "xclip" else None), \
                mock.patch("subprocess.run") as run, \
                redirect_stdout(io.StringIO()) as out:
            cli._copy_ask("the whole ask")
        self.assertEqual(run.call_args.args[0], ["xclip", "-selection", "clipboard"])
        self.assertEqual(run.call_args.kwargs["input"], "the whole ask")
        self.assertNotIn("the whole ask", out.getvalue())

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

    def test_the_home_row_picks_a_saved_search_and_runs_it(self):
        from cs import cli, ui

        ui.save_search("mine", "portal")
        with mock.patch.object(cli, "_curses_wrapper", return_value=0), \
                mock.patch.object(cli, "cmd_search", return_value=True) as search:
            self.assertTrue(cli.cmd_saved_menu())
        search.assert_called_once_with("portal")

    # ── File history ─────────────────────────────────────────────────

    def test_file_history_names_session_agent_turn_and_ask(self):
        self.conn.execute("UPDATE session_files SET turn_index = 1 WHERE "
                          "file_path LIKE '%globe.js'")
        self.conn.execute("UPDATE turns SET timestamp = ? WHERE session_id = "
                          "'sess-alpha' AND turn_index = 1", (_ago(0.01),))
        self.conn.execute("UPDATE turns SET timestamp = ? WHERE session_id = "
                          "'sess-alpha' AND turn_index = 0", (_ago(0.02),))
        self.conn.commit()
        _write_events(self.base, "sess-alpha", [
            _event("tool.execution_start", _ago(0.005)[:19],
                   {"toolCallId": "e1", "toolName": "edit",
                    "arguments": {"path": "/tmp/a/portal/globe.js"}},
                   agent="agent-1"),
        ])
        data = self._json("files", "globe.js", "--history")
        touch = data["files"][0]["touches"][0]
        self.assertEqual((touch["id"], touch["turn"], touch["tool"], touch["agent"],
                          touch["turn_summary"]),
                         ("sess-alpha", 1, "edit", "sub-agent", "add charts"))
        code, out = self._run("files", "globe.js", "--history")
        self.assertIn("sub-agent", out)
        self.assertIn("add charts", out)

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

    def test_arrows_on_the_budget_row_step_the_limit(self):
        import curses

        from cs import cli, ui

        labels = [item[1] for item in cli._home_items()]
        at = labels.index("Budget")
        screen = Screen([curses.KEY_DOWN] * at + [curses.KEY_RIGHT, curses.KEY_RIGHT,
                                                  curses.KEY_LEFT, ord("q")])
        state = {"period": 30, "revealed": True}
        cli._home_tui(screen, state)
        self.assertEqual(ui.daily_budget_aiu(), 5)
        self.assertEqual(state["period"], 30, "the window must not move")
        self.assertIn("daily limit 5 AIU", cli._home_items()[at][2])
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
        ui.add_tag(CALM, "wip")                     # quiet 7 days
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
        self.assertEqual(by_group["Today"][:5], ["Next up", "Standup", "End of day",
                                                "Weekly review", "Budget"])
        for label in ("Similar work", "My asks", "Saved searches", "File history"):
            self.assertIn(label, by_group["Find"])
        self.assertEqual(by_group["Improve"][0], "Practice")
        self.assertIn("Clean-up", by_group["Improve"])
        self.assertEqual(labels[0], "Next up")

    def test_every_new_row_opens(self):
        from cs import cli

        items = {item[1]: item for item in cli._home_items(7)}
        with mock.patch.object(cli, "_page", return_value=True) as page, \
                mock.patch.object(cli, "_interactive_listing", return_value=True), \
                redirect_stdout(io.StringIO()):
            for label, given in (("Next up", None), ("End of day", None),
                                 ("Weekly review", None), ("Budget", None),
                                 ("Similar work", "portal"), ("My asks", 7),
                                 ("File history", "globe"), ("Clean-up", None),
                                 ("Saved searches", None)):
                with self.subTest(row=label):
                    action = items[label][3]
                    result = action(given) if given is not None else action()
                    self.assertIsNotNone(result)
        self.assertGreater(page.call_count, 5)

    def test_a_term_row_says_what_it_asks_for(self):
        from cs import cli

        self.assertEqual(cli._TERM_PROMPTS["File history"], " file: ")
        self.assertEqual(cli._TERM_PROMPTS["Similar work"], " similar to: ")
