"""The evidence views: tool failures, stuck loops, sub-agents, switches, endings.

Each verdict here must arrive with what triggered it, and every string that
came from the store or the event log must be masked on the way out — to the
page and to `--json` alike. The fixture log (`support._alpha_events`) holds
one stuck loop, a failing hook, allow-all switched on, a model switch and a
sub-agent run, with a credential in a tool result.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from support import (
    EVENT_SECRET,
    StoreTest,
    _add_governance_rows,
    _alpha_events,
    _event,
    _tool,
    _write_events,
)


def _today(second: int) -> str:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{day}T00:00:{second:02d}"


class EvidenceTest(StoreTest):
    def setUp(self):
        super().setUp()
        self.base = Path(self._tmp.name)
        _write_events(self.base, "sess-alpha", _alpha_events())
        # Give sess-alpha's turns real times, so events can be placed on them:
        # turn 0 opens at second 0 and turn 1 at second 10, like the log.
        conn = sqlite3.connect(self.base / "session-store.db")
        conn.execute("UPDATE turns SET timestamp = ? WHERE session_id = "
                     "'sess-alpha' AND turn_index = 0", (_today(0) + ".000Z",))
        conn.execute("UPDATE turns SET timestamp = ? WHERE session_id = "
                     "'sess-alpha' AND turn_index = 1", (_today(10) + ".000Z",))
        conn.commit()
        conn.close()

    def _json(self, *args: str) -> dict:
        code, out = self._run(*args, "--json")
        self.assertEqual(code, 0, out)
        return json.loads(out)

    # ── Tool failures ────────────────────────────────────────────────

    def test_failures_by_tool_repo_and_session(self):
        code, out = self._run("failures")
        self.assertEqual(code, 0)
        self.assertIn("4 of 6 tool calls failed", out)
        self.assertIn("bash", out)
        self.assertIn("acme/portal", out)
        self.assertIn("Build Three.js portal", out)
        data = self._json("failures")
        bash = next(r for r in data["by_tool"] if r["tool"] == "bash")
        self.assertEqual((bash["calls"], bash["failures"]), (4, 3))
        self.assertEqual(data["worst"][0]["id"], "sess-alpha")
        self.assertEqual(data["worst"][0]["top_tool"], "bash")

    def test_the_worst_sessions_are_numbered_for_cs_show(self):
        self._run("failures")
        code, out = self._run("show", "1")
        self.assertEqual(code, 0)
        self.assertIn("Build Three.js portal", out)

    def test_show_places_failures_on_their_turns(self):
        code, out = self._run("show", "sess-alpha")
        self.assertEqual(code, 0)
        self.assertIn("Tool calls · 6 · 4 failed", out)
        # The bash loop ran in turn 0; the sub-agent's edit failed in turn 1.
        self.assertRegex(out, r"3 failed\s+turn 0\s+bash ×3")
        self.assertRegex(out, r"1 failed\s+turn 1\s+edit")
        self.assertIn("stuck loop", out)
        self.assertIn("turns 0", out)

    def test_a_session_without_a_log_says_nothing_about_tools(self):
        code, out = self._run("show", "sess-empty")
        self.assertEqual(code, 0)
        self.assertNotIn("Tool calls", out)

    def test_no_logs_in_the_window_is_said_plainly(self):
        (self.base / "session-state" / "sess-alpha" / "events.jsonl").unlink()
        code, out = self._run("failures")
        self.assertEqual(code, 0)
        self.assertIn("No session in this window left an event log", out)

    # ── Stuck loops ──────────────────────────────────────────────────

    def test_a_loop_carries_its_tool_run_and_turns(self):
        code, out = self._run("failures", "--loops")
        self.assertEqual(code, 0)
        self.assertIn("×3", out)
        self.assertIn("bash", out)
        loop = self._json("loops")["loops"][0]
        self.assertEqual((loop["tool"], loop["run"], loop["agent"]),
                         ("bash", 3, "main"))
        self.assertEqual((loop["first_turn"], loop["last_turn"]), (0, 0))
        self.assertEqual(self._run("loops")[1], out)

    def test_the_listing_marks_a_stuck_session_once_the_cache_knows(self):
        _code, before = self._run("recent")
        self.assertNotRegex(before, r"\d!")      # cache cold: no log was read
        self._run("failures", "--loops")          # warms the cache
        _code, after = self._run("recent")
        row = next(line for line in after.splitlines() if "Three.js" in line)
        self.assertRegex(row, r"\d!")
        self.assertIn("! = a tool failed 3+ times in a row", after)

    def test_the_tui_marks_the_stuck_row_in_the_spare_cell(self):
        from support import Screen

        from cs import cli

        self._run("loops")
        rows = [("sess-alpha", "2026-09-01T12:00", "Stuck", "r/a", "/tmp", 1, 0),
                ("sess-empty", "2026-09-01T11:00", "Fine", "r/b", "/tmp", 1, 0)]
        screen = Screen([ord("q")])
        cli._listing_tui(screen, rows, "Sessions")
        frame = screen.frames[-1]
        self.assertEqual(frame.get((5, 4)), "!")
        self.assertIsNone(frame.get((6, 4)))

    # ── Sub-agents ───────────────────────────────────────────────────

    def test_subagents_are_summarised_by_name(self):
        code, out = self._run("subagents")
        self.assertEqual(code, 0)
        self.assertIn("explore", out)
        agent = self._json("subagents")["agents"][0]
        self.assertEqual((agent["name"], agent["runs"], agent["tool_calls"],
                          agent["tokens"], agent["duration_ms"]),
                         ("explore", 1, 4, 12000, 9000))
        self.assertEqual(agent["models"], ["gpt-5.5"])
        self.assertFalse(agent["override_ignored"])

    def test_a_declared_model_that_was_never_applied_is_flagged(self):
        agents = self.base / "agents"
        agents.mkdir()
        (agents / "explore.agent.md").write_text(
            "---\nname: explore\nmodel: claude-opus-4.8\n---\n\nLook around.\n")
        code, out = self._run("subagents")
        self.assertEqual(code, 0)
        self.assertIn("Declared model not applied", out)
        self.assertIn("declares model: claude-opus-4.8", out)
        self.assertIn("0 of 1 runs had an override", out)
        agent = self._json("subagents")["agents"][0]
        self.assertTrue(agent["override_ignored"])
        self.assertEqual(agent["declared_model"], "claude-opus-4.8")

    # ── Model switches ───────────────────────────────────────────────

    def test_a_switch_shows_from_to_source_and_spend_either_side(self):
        code, out = self._run("switches")
        self.assertEqual(code, 0)
        self.assertIn("gpt-5.5 → claude-opus-4.8", out)
        self.assertIn("effort medium → high", out)
        self.assertIn("picker", out)
        session = self._json("switches")["sessions"][0]
        switch = session["switches"][0]
        self.assertEqual((switch["from"], switch["to"], switch["source"]),
                         ("gpt-5.5", "claude-opus-4.8", "model_picker"))
        self.assertEqual((switch["effort_from"], switch["effort_to"]),
                         ("medium", "high"))
        self.assertEqual(switch["turn"], 0)
        # The fixture's two calls are stamped when the store was built, which
        # is after the switch at midnight: all of the spend is "after".
        self.assertEqual(switch["nano_aiu_before"] + switch["nano_aiu_after"],
                         4_000_000_000)

    def test_the_first_model_of_a_session_is_not_a_switch(self):
        _write_events(self.base, "sess-alpha", [
            _event("session.model_change", _today(1),
                   {"previousModel": None, "newModel": "gpt-5.5",
                    "source": "startup"}),
        ])
        self.assertEqual(self._json("switches")["sessions"], [])

    # ── Unclean endings ──────────────────────────────────────────────

    def test_an_ending_carries_its_reason_and_turn(self):
        code, out = self._run("endings")
        self.assertEqual(code, 0)
        self.assertIn("error", out)
        ending = next(e for e in self._json("endings")["endings"]
                      if e["id"] == "sess-alpha")
        self.assertEqual((ending["reason"], ending["turn"]), ("error", 1))

    def test_a_missing_reason_is_labelled_unknown(self):
        _add_governance_rows(self.base)
        data = self._json("endings")
        reasons = {e["id"][:8]: e["reason"] for e in data["endings"]}
        self.assertEqual(reasons["11111111"], "unknown")
        self.assertIn("unknown", data["by_reason"])

    def test_a_clean_stop_is_not_an_unclean_ending(self):
        conn = sqlite3.connect(self.base / "session-store.db")
        conn.execute("UPDATE assistant_usage_events SET finish_reason = 'stop'")
        conn.commit()
        conn.close()
        code, out = self._run("endings")
        self.assertEqual(code, 0)
        self.assertIn("ended on a clean call", out)

    # ── Hooks and autonomy, extended ─────────────────────────────────

    def test_hooks_report_runs_and_failures_from_the_log(self):
        code, out = self._run("hooks")
        self.assertEqual(code, 0)
        row = next(line for line in out.splitlines() if "preToolUse" in line)
        self.assertRegex(row, r"preToolUse\s+2\s+1")

    def test_yolo_labels_recorded_evidence_apart_from_inferred(self):
        _add_governance_rows(self.base)
        code, out = self._run("yolo")
        self.assertEqual(code, 0)
        alpha = next(line for line in out.splitlines() if "sess-alp" in line)
        self.assertIn("recorded", alpha)
        self.assertIn("allow-all", alpha)
        typed = next(line for line in out.splitlines() if "11111111" in line)
        self.assertIn("inferred", typed)

    # ── Masking ──────────────────────────────────────────────────────

    def test_no_view_or_export_prints_a_credential_from_the_log_or_store(self):
        """A hostile tool name, agent name and summary each carry a secret.
        The page and `--json` must mask all three; the tool result's own
        secret must never be read at all."""
        leak = "ghp_" + "Q" * 36  # gitleaks:allow
        conn = sqlite3.connect(self.base / "session-store.db")
        conn.execute("UPDATE sessions SET summary = ? WHERE id = 'sess-alpha'",
                     (f"deploy with {leak}",))
        conn.commit()
        conn.close()
        _write_events(self.base, "sess-alpha", [
            *_alpha_events(),
            *_tool("h1", f"tool-{leak}", _today(20), False),
            *_tool("h2", f"tool-{leak}", _today(21), False),
            *_tool("h3", f"tool-{leak}", _today(22), False),
            _event("subagent.completed", _today(23),
                   {"agentName": f"agent-{leak}", "model": f"m-{leak}",
                    "totalToolCalls": 1}),
        ])
        for view in ("failures", "loops", "subagents", "switches", "endings",
                     "hooks", "yolo"):
            with self.subTest(view=view):
                code, out = self._run(view)
                self.assertEqual(code, 0)
                self.assertNotIn(leak, out)
                self.assertNotIn(EVENT_SECRET, out)
                if view in ("hooks", "yolo"):
                    continue
                code, out = self._run(view, "--json")
                self.assertEqual(code, 0)
                self.assertNotIn(leak, out)
                self.assertNotIn(EVENT_SECRET, out)
        code, out = self._run("show", "sess-alpha")
        self.assertNotIn(leak, out)
        self.assertNotIn(EVENT_SECRET, out)


class EvidenceHomeTest(StoreTest):
    def test_the_rows_sit_in_their_groups_and_open_with_the_window(self):
        from unittest import mock

        from cs import cli

        items = cli._home_items(7)
        labels = [label for _, label, *_ in items]
        groups = {labels[i]: cli._home_group(i) for i in range(len(labels))}
        for label, group in (("Tool failures", "Govern"), ("Stuck loops", "Govern"),
                             ("Sub-agents", "Measure"),
                             ("Model switches", "Measure")):
            with self.subTest(row=label):
                self.assertEqual(groups[label], group)
                row = items[labels.index(label)]
                self.assertEqual(row[4], "period")
                self.assertIn("last 7 days", row[2])
        with mock.patch.object(cli, "_page", return_value=True) as page:
            for label in ("Tool failures", "Stuck loops",
                          "Sub-agents", "Model switches"):
                self.assertTrue(items[labels.index(label)][3](7))
        self.assertEqual(page.call_count, 4)
        self.assertNotIn("Unclean endings", labels)
        loops = page.call_args_list[1].args[0]
        self.assertIn("Stuck loops · last 7 days", loops)
