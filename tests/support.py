"""Shared fixtures: a synthetic Copilot store, and a fake curses screen.

Every test in this directory runs against a store built here under a
temporary `COPILOT_HOME`, so the suite never reads real session data and
behaves identically on any machine and in CI.

`Screen` is the other half of that: it stands in for curses so keys, mouse
reports and redraws can be asserted frame by frame without a terminal.
"""

from __future__ import annotations

import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


class _Tty(io.StringIO):
    """Stdout that claims to be a terminal, so the TTY-only paths are tested."""

    def isatty(self) -> bool:
        return True


def _build_store(base: Path) -> None:
    """Create a minimal session-store.db with a couple of sessions."""
    db = base / "session-store.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, cwd TEXT, repository TEXT, host_type TEXT,
            branch TEXT, summary TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, turn_index INTEGER,
            user_message TEXT, assistant_response TEXT, timestamp TEXT
        );
        CREATE TABLE assistant_usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            turn_index INTEGER, model TEXT NOT NULL, total_nano_aiu INTEGER,
            input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
            reasoning_tokens INTEGER, duration_ms INTEGER,
            time_to_first_token_ms INTEGER, finish_reason TEXT,
            content_filter_triggered INTEGER, initiator TEXT, agent_id TEXT,
            parent_tool_call_id TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE VIRTUAL TABLE search_index USING fts5(
            content, session_id UNINDEXED, source_type UNINDEXED,
            source_id UNINDEXED
        );
        CREATE TABLE checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            checkpoint_number INTEGER NOT NULL, title TEXT, overview TEXT,
            history TEXT, work_done TEXT, technical_details TEXT,
            important_files TEXT, next_steps TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE session_refs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            ref_type TEXT NOT NULL, ref_value TEXT NOT NULL, turn_index INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE session_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            file_path TEXT NOT NULL, tool_name TEXT, turn_index INTEGER,
            first_seen_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('sess-alpha','/tmp/a','acme/portal','local','main',"
        "'Build Three.js portal', datetime('now'), datetime('now'))"
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('sess-empty','/tmp/b',NULL,'local',NULL,"
        "'Empty session', datetime('now'), datetime('now'))"
    )
    conn.execute(
        "INSERT INTO turns VALUES (1,'sess-alpha',0,'make a portal','sure, here it is','x')"
    )
    conn.execute(
        "INSERT INTO turns VALUES (2,'sess-alpha',1,'add charts','done','y')"
    )
    conn.executemany(
        """INSERT INTO assistant_usage_events
           (session_id, turn_index, model, total_nano_aiu, input_tokens,
            output_tokens, cache_read_tokens, reasoning_tokens, duration_ms,
            time_to_first_token_ms, finish_reason, content_filter_triggered,
            initiator, agent_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
        [
            ("sess-alpha", 0, "gpt-5.5", 1_500_000_000, 1000, 200, 3000, 50,
             4000, 900, "stop", 0, "user", None),
            ("sess-alpha", 1, "claude-opus-4.8", 2_500_000_000, 2000, 300, 5000, 60,
             6000, 800, "error", 0, "sub-agent", "toolu_agent_1"),
        ],
    )
    conn.executemany(
        "INSERT INTO search_index (content, session_id, source_type) VALUES (?,?,?)",
        [
            ("make a portal with three.js and a spinning globe", "sess-alpha", "turn"),
            ("Remaining work: wire the charts to live data", "sess-alpha",
             "checkpoint_next_steps"),
            ("a quiet session about packaging", "sess-empty", "turn"),
        ],
    )
    conn.executemany(
        "INSERT INTO session_files (session_id, file_path, tool_name) VALUES (?,?,?)",
        [
            ("sess-alpha", "/tmp/a/portal/index.html", "create"),
            ("sess-alpha", "/tmp/a/portal/globe.js", "edit"),
            # The same file, worked on by a second session.
            ("sess-empty", "/tmp/a/portal/index.html", "edit"),
        ],
    )
    conn.executemany(
        """INSERT INTO checkpoints
           (session_id, checkpoint_number, overview, work_done,
            important_files, next_steps, technical_details)
           VALUES (?,?,?,?,?,?,?)""",
        [
            ("sess-alpha", 1, "Early overview", "Built the first draft",
             "`/tmp/a/portal/old.html`", "- wire up the globe", "n/a"),
            # The later checkpoint is the one a brief must use.
            ("sess-alpha", 2, "Portal work for acme",
             "- shipped the globe\n- added charts",
             "`/tmp/a/portal/index.html` the entry point\n`globe.js` the renderer",
             "- point the charts at live data\n- write tests", "Uses three.js"),
        ],
    )
    conn.executemany(
        "INSERT INTO session_refs (session_id, ref_type, ref_value) VALUES (?,?,?)",
        [("sess-alpha", "commit", "abc1234"), ("sess-alpha", "pr", "42")],
    )
    conn.commit()
    conn.close()


class Screen:
    """A curses window stand-in that records what each frame drew.

    Keys are served from a queue. Under nodelay an empty queue reports
    'nothing buffered' instead of blocking, which is what a terminal does once
    a sequence has ended — that is what stops a probe for a mouse report from
    hanging on the key that follows an Esc.
    """

    def __init__(self, keys=()):
        self.keys = list(keys)
        self.frame = {}
        self.frames = []
        self.no_wait = False

    def keypad(self, enabled):
        pass

    def bkgd(self, char, style):
        pass

    def nodelay(self, enabled):
        self.no_wait = enabled

    def timeout(self, milliseconds):
        # Real curses reads a timeout as 'wait this long, then report -1',
        # which is nodelay with a delay; -1 restores blocking.
        self.no_wait = milliseconds >= 0

    def getmaxyx(self):
        return 24, 100

    def erase(self):
        self.frame = {}

    def addnstr(self, y, x, text, width, style=0):
        self.frame[(y, x)] = text[:width]

    def refresh(self):
        self.frames.append(dict(self.frame))

    def getch(self):
        if self.no_wait and not self.keys:
            return -1
        return self.keys.pop(0)


class StoreTest(unittest.TestCase):
    """A throwaway session store and a way to run `cs` against it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        _build_store(base)
        os.environ["COPILOT_HOME"] = str(base)
        # The cross-tool root lives beside the real home, so without this
        # the suite would read whatever the developer happens to keep in
        # ~/.agents and count it — a test that passes on one machine.
        os.environ["CS_AGENTS_HOME"] = str(base / ".agents")
        os.environ["TERM"] = "dumb"  # disable colour

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("COPILOT_HOME", None)
        os.environ.pop("CS_AGENTS_HOME", None)

    def _run(self, *args: str) -> tuple[int, str]:
        from cs.cli import main

        buf = io.StringIO()
        code = 0
        with redirect_stdout(buf):
            try:
                code = main(list(args))
            except SystemExit as e:  # error paths exit rather than return
                code = e.code if isinstance(e.code, int) else 1
        return code, buf.getvalue()

    def _run_err(self, *args: str) -> tuple[int, str]:
        """The same, capturing stderr — for the paths that refuse to run."""

        from cs.cli import main

        buf = io.StringIO()
        code = 0
        with redirect_stderr(buf), redirect_stdout(io.StringIO()):
            try:
                code = main(list(args))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
        return code, buf.getvalue()


YOLO = "11111111-1111-4111-8111-111111111111"
YOLO_TWIN = "11111199-1111-4111-8111-111111111111"
UNATTENDED = "22222222-2222-4222-8222-222222222222"
CALM = "33333333-3333-4333-8333-333333333333"
PARENT = "44444444-4444-4444-8444-444444444444"
CHILD = "55555555-5555-4555-8555-555555555555"
LEAK = "66666666-6666-4666-8666-666666666666"
# One session that reports having removed things, and one that only offers to.
WIPED = "88888888-8888-4888-8888-888888888888"
OFFERED = "99999999-9999-4999-8999-999999999999"
# A credential written as source, in a session that wrote a file — both halves
# of "hardcoded" — beside LEAK, which pastes one into a sentence and writes
# nothing.
HARDCODED = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _add_governance_rows(base: Path) -> None:
    """Sessions that exercise autonomy, handoffs and credential exposure.

    Kept out of the shared fixture so the counts every other test asserts on
    stay where they were.
    """
    conn = sqlite3.connect(base / "session-store.db")
    conn.executemany(
        "INSERT INTO sessions VALUES (?,?,?,'local','main',?,?,?)",
        [
            (YOLO, "/tmp/y", "acme/portal", "Try it unsupervised",
             "2026-07-01T09:00", "2026-07-01T09:30"),
            (YOLO_TWIN, "/tmp/y", "acme/portal", "A near-identical id",
             "2026-07-01T10:00", "2026-07-01T10:30"),
            (UNATTENDED, "/tmp/u", "acme/portal", "One prompt, long run",
             "2026-07-02T09:00", "2026-07-02T11:00"),
            (CALM, "/tmp/c", "acme/portal", "A supervised session",
             "2026-07-03T09:00", "2026-07-03T09:20"),
            (PARENT, "/tmp/h", "acme/portal", "First half of the work",
             "2026-07-04T09:00", "2026-07-04T12:00"),
            (CHILD, "/tmp/h", "acme/portal", "Second half of the work",
             "2026-07-05T09:00", "2026-07-05T12:00"),
            (LEAK, "/tmp/l", "acme/portal", "Wire up the database",
             "2026-07-06T09:00", "2026-07-06T09:40"),
            (WIPED, "/tmp/w", "acme/portal", "Clear out the stale tree",
             "2026-07-07T09:00", "2026-07-07T09:40"),
            (OFFERED, "/tmp/o", "acme/portal", "Ask about clearing the tree",
             "2026-07-08T09:00", "2026-07-08T09:40"),
            (HARDCODED, "/tmp/hc", "acme/portal", "Write the settings module",
             "2026-07-09T09:00", "2026-07-09T09:40"),
        ],
    )
    conn.executemany(
        "INSERT INTO turns (session_id, turn_index, user_message, assistant_response)"
        " VALUES (?,?,?,?)",
        [
            (YOLO, 0, "yolo", "off we go"),
            (YOLO, 1, "carry on", "done"),
            (YOLO_TWIN, 0, "what does yolo mean?", "it means approvals are off"),
            (UNATTENDED, 0, "build the whole thing", "built"),
            *[(CALM, i, f"step {i}", "ok") for i in range(5)],
            (PARENT, 0, "create a handoff file for this work", "written"),
            (CHILD, 0, "read HANDOFF.md and continue from there", "continuing"),
            (LEAK, 0, "connect with DB_PASSWORD=hunter2xyz please", "connected"),
            # Past tense, a tick, and no fence: the session says it did it.
            (WIPED, 0, "clean up the build tree",
             "Deleted the stale tree with `rm -rf build/` \u2713 and "
             "`git push --force` to drop the bad commit \u2713"),
            # The same commands, offered inside a fenced block: an instruction,
            # not a report, and the store cannot say whether it was followed.
            (OFFERED, 0, "how would I clear it?",
             "You could remove it first:\n```bash\nrm -rf build/\n```"),
            (HARDCODED, 0, "write the settings module",
             'Written to settings.py:\n\nAPI_PASSWORD = "hunter2xyz"\n'),
        ],
    )
    conn.executemany(
        """INSERT INTO assistant_usage_events
           (session_id, turn_index, model, total_nano_aiu, initiator, agent_id)
           VALUES (?,?,'gpt-5.6-sol',1000,?,NULL)""",
        [(YOLO, 0, "agent")] * 60
        + [(UNATTENDED, 0, "agent")] * 50
        + [(CALM, 0, "agent")] * 5,
    )
    conn.executemany(
        "INSERT INTO session_files (session_id, file_path, tool_name) VALUES (?,?,?)",
        [
            (PARENT, "/tmp/h/HANDOFF.md", "create"),
            (CHILD, "/tmp/h/HANDOFF.md", "read"),
            (HARDCODED, "/tmp/hc/settings.py", "create"),
        ],
    )
    conn.commit()
    conn.close()


def _add_practice_rows(base: Path) -> None:
    """Sessions built to trip specific rules, and one built to trip none.

    Every rule has a minimum sample, so a fixture that only shows the shape
    of a habit proves nothing — these are sized to clear the thresholds the
    rules actually use.
    """
    conn = sqlite3.connect(base / "session-store.db")
    turn_id = 1000

    def session(sid: str, summary: str, prompts, *, hour=14, weekday_offset=0):
        nonlocal turn_id
        # A Monday, so weekday_offset lands where the test expects it.
        day = 3 + weekday_offset
        stamp = f"2026-08-{day:02d}T{hour:02d}:%02d:00.000Z"
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
            (sid, "/tmp/p", "acme/app", "local", "main", summary,
             stamp % 0, stamp % 0),
        )
        for index, prompt in enumerate(prompts):
            conn.execute(
                "INSERT INTO turns VALUES (?,?,?,?,?,?)",
                (turn_id, sid, index, prompt, "ok", stamp % min(index, 59)),
            )
            turn_id += 1

    # Thin prompts, in bulk — enough to clear the 20-prompt minimum and the
    # 30% share the rule needs.
    for n in range(12):
        session(f"thin-{n}", f"Thin session {n}", ["fix it", "no", "again"])
    # Sessions long enough to be mega, and to count as vague openers.
    session("mega-1", "The session that never ended",
            [f"keep going {i}" for i in range(60)])
    # The same ask, three times over, in three different sessions.
    for n in range(4):
        session(f"repeat-{n}", f"Repeat {n}",
                ["run the release checklist end to end please"])
    # Frustration, in the plain.
    for n in range(3):
        session(f"cross-{n}", f"Cross {n}",
                ["this is broken!!! why won't it work"])
    # A well-run session: a structured brief and steady follow-ups.
    session("calm-1", "A calm session", [
        "## Goal\n1. add the parser\n2. do not touch the CLI\n3. tests must pass",
        "now wire it to the reader, but only in the new module",
    ])
    conn.commit()
    conn.close()
