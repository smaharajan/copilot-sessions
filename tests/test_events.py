"""The event log: streamed, reduced to counts, and cached without its text.

`cs/events.py` reads `session-state/<id>/events.jsonl`, which Copilot writes
beside the store and which holds tool output verbatim. These tests pin the
three promises that make reading it safe: a bad line never stops the read,
the cache is only trusted while the log is unchanged, and no text from a
tool result — secrets included — ever reaches the cache.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

from support import EVENT_SECRET, StoreTest, _alpha_events, _event, _tool, _write_events


class EventStreamTest(StoreTest):
    def setUp(self):
        super().setUp()
        self.base = Path(self._tmp.name)

    def test_malformed_lines_and_missing_keys_are_skipped(self):
        from cs import events

        _write_events(self.base, "sess-alpha", [
            "not json at all",
            '{"type":"tool.execution_complete","data":',   # cut off mid-write
            "[1, 2, 3]",                                   # JSON, not an object
            '{"data":{"toolCallId":"x"}}',                 # no type
            '{"type":"tool.execution_complete","data":"not a dict"}',
            *_tool("c1", "bash", "2026-09-01T00:00:01", False),
        ])
        seen = list(events.iter_events("sess-alpha"))
        # The two good lines, plus the one whose data was not a dict — kept,
        # with its data made empty rather than trusted.
        self.assertEqual([e["type"] for e in seen],
                         ["tool.execution_complete", "tool.execution_start",
                          "tool.execution_complete"])
        digest = events.session_digest("sess-alpha")
        self.assertEqual(digest["tools"]["bash"], [1, 1])
        # A completion with no start and no name is still a call.
        self.assertEqual(digest["tools"]["unknown"], [1, 0])

    def test_types_narrow_what_is_parsed(self):
        from cs import events

        _write_events(self.base, "sess-alpha", _alpha_events())
        hooks = list(events.iter_events("sess-alpha", ["hook.end"]))
        self.assertEqual(len(hooks), 2)
        self.assertTrue(all(e["type"] == "hook.end" for e in hooks))

    def test_a_session_without_a_log_has_no_digest(self):
        from cs import events

        self.assertIsNone(events.session_digest("sess-alpha"))
        self.assertEqual(list(events.iter_events("sess-alpha")), [])

    def test_the_digest_counts_what_the_log_recorded(self):
        from cs import events

        _write_events(self.base, "sess-alpha", _alpha_events())
        digest = events.session_digest("sess-alpha")
        self.assertEqual(digest["calls"], 6)
        self.assertEqual(digest["failures"], 4)
        self.assertEqual(digest["tools"]["bash"], [4, 3])
        self.assertEqual(digest["sub_calls"], 1)
        self.assertEqual(digest["asks"], 2)
        self.assertEqual(digest["hooks"]["preToolUse"][:2], [2, 1])
        self.assertTrue(digest["permissions"][0]["allow_all"])
        self.assertEqual(digest["permissions"][0]["mode"], "on")
        change = digest["model_changes"][0]
        self.assertEqual((change["from"], change["to"]), ("gpt-5.5", "claude-opus-4.8"))
        self.assertEqual(change["source"], "model_picker")
        agent = digest["subagents"][0]
        self.assertEqual((agent["name"], agent["tool_calls"], agent["override"]),
                         ("explore", 4, ""))
        self.assertEqual(digest["skills"]["docs"][0], 1)
        # The loop: three bash failures in a row by the main agent.
        self.assertEqual(len(digest["loops"]), 1)
        loop = digest["loops"][0]
        self.assertEqual((loop["tool"], loop["run"], loop["agent"]), ("bash", 3, "main"))
        self.assertEqual(digest["longest"]["run"], 3)
        self.assertEqual(digest["last_failure"][1], "edit")

    def test_a_success_between_failures_breaks_the_run(self):
        from cs import events

        _write_events(self.base, "sess-alpha", [
            *_tool("a", "bash", "2026-09-01T00:00:01", False),
            *_tool("b", "bash", "2026-09-01T00:00:02", False),
            *_tool("c", "bash", "2026-09-01T00:00:03", True),
            *_tool("d", "bash", "2026-09-01T00:00:04", False),
            *_tool("e", "bash", "2026-09-01T00:00:05", False),
        ])
        digest = events.session_digest("sess-alpha")
        self.assertEqual(digest["loops"], [])
        self.assertEqual(digest["longest"]["run"], 2)

    def test_turn_of_joins_an_event_to_the_store_turn_before_it(self):
        from cs import events

        turns = [(0, "2026-09-01T10:00:00"), (1, "2026-09-01T11:00:00"),
                 (2, "")]
        self.assertIsNone(events.turn_of("2026-09-01T09:59:59", turns))
        self.assertEqual(events.turn_of("2026-09-01T10:30:00", turns), 0)
        self.assertEqual(events.turn_of("2026-09-01T11:00:00", turns), 1)
        self.assertIsNone(events.turn_of("", turns))


class EventCacheTest(StoreTest):
    def setUp(self):
        super().setUp()
        self.base = Path(self._tmp.name)
        self.log = _write_events(self.base, "sess-alpha", _alpha_events())

    def test_a_warm_read_does_not_open_the_log(self):
        from unittest import mock

        from cs import events

        first = events.session_digest("sess-alpha")
        self.assertTrue(events.cache_path().exists())
        events.reset_cache()  # a new process: only the file remains
        with mock.patch.object(events, "_compute",
                               side_effect=AssertionError("log was re-read")):
            self.assertEqual(events.session_digest("sess-alpha"), first)

    def test_a_changed_size_or_mtime_is_a_miss(self):
        from cs import events

        events.session_digest("sess-alpha")
        with open(self.log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(_event(
                "hook.end", "2026-09-01T00:00:59",
                {"hookType": "agentStop", "success": False})) + "\n")
        self.assertIn("agentStop", events.session_digest("sess-alpha")["hooks"])

        # Same size, new mtime: still a miss.
        events.reset_cache()
        before = events.session_digest("sess-alpha")
        text = self.log.read_text().replace('"agentStop"', '"agentStart"')
        self.log.write_text(text)
        later = time.time() + 5
        os.utime(self.log, (later, later))
        after = events.session_digest("sess-alpha")
        self.assertIn("agentStop", before["hooks"])
        self.assertIn("agentStart", after["hooks"])

    def test_an_unwritable_cache_falls_back_to_memory(self):
        from cs import events

        blocked = self.base / "blocked"
        blocked.mkdir()
        os.chmod(blocked, stat.S_IRUSR | stat.S_IXUSR)
        try:
            os.environ["XDG_CACHE_HOME"] = str(blocked)
            events.reset_cache()
            digest = events.session_digest("sess-alpha")
            self.assertEqual(digest["failures"], 4)
            self.assertFalse(events.cache_path().exists())
        finally:
            # Before tearDown removes the tree, which it cannot do read-only.
            os.chmod(blocked, stat.S_IRWXU)

    def test_a_corrupt_cache_is_ignored(self):
        from cs import events

        path = events.cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        events.reset_cache()
        self.assertEqual(events.session_digest("sess-alpha")["failures"], 4)

    def test_the_cache_holds_no_text_from_a_result(self):
        """Counts and ids only. The fixture's tool result, hook error and
        skill body carry text — including a credential — and none of it may
        reach the file on disk."""
        from cs import events

        events.session_digest("sess-alpha")
        cached = events.cache_path().read_text()
        for text in (EVENT_SECRET, "exit 1", "SKILL BODY", "make a portal"):
            self.assertNotIn(text, cached)

    def test_the_cache_is_never_written_inside_copilot_home(self):
        from cs import events

        events.session_digest("sess-alpha")
        self.assertFalse(str(events.cache_path()).startswith(self._tmp.name + "/session"))
        self.assertNotIn("events-digest", " ".join(
            str(p) for p in (self.base / "session-state").rglob("*")))

    def test_digests_windows_by_log_age(self):
        from cs import events

        _write_events(self.base, "sess-empty", [
            *_tool("x", "view", "2026-01-01T00:00:00", True)])
        old = time.time() - 40 * 86400
        os.utime(self.base / "session-state" / "sess-empty" / "events.jsonl",
                 (old, old))
        self.assertEqual(set(events.digests(days=7)), {"sess-alpha"})
        self.assertEqual(set(events.digests(days=None)), {"sess-alpha", "sess-empty"})
        self.assertEqual(set(events.digests(["sess-empty"])), {"sess-empty"})

    def test_cache_only_reads_never_open_a_log(self):
        from cs import events

        self.assertEqual(events.digests(["sess-alpha"], compute=False), {})
        events.digests(["sess-alpha"])
        self.assertIn("sess-alpha", events.digests(["sess-alpha"], compute=False))
