"""Every command, run — against a full store, an empty one, and an old one.

The rest of the suite tests what each view *says*. This tests that each view
still runs at all, which is a different and blunter question, and the one a
refactor actually breaks. It is deliberately structural: the command list is
read out of `cli._dispatch` itself rather than typed here, so a command added
next year is smoke-tested the day it is added and an alias that quietly stops
dispatching fails the suite rather than waiting for a bug report.

Three stores, because the same code meets three shapes of them:

* the fixture store, which has every table;
* an **empty** store — a fresh machine, or a window with nothing in it, where
  every count is zero and every list is blank;
* an **old** store, carrying only `sessions` and `turns`. The schema belongs
  to Copilot, not to `cs`, so a release that has not shipped
  `assistant_usage_events` yet — or renames it — is the single most likely
  way this tool breaks on somebody else's machine. `db.optional` and the
  `has_*` guards exist for exactly that, and nothing was running them across
  the whole command surface.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from support import StoreTest, _build_store


def _dispatch_commands() -> set[str]:
    """Every literal `_dispatch` compares the command word against.

    Read from the source rather than listed here on purpose. A hand-kept
    copy of a dispatch table is a list that is correct on the day it is
    written; this one cannot fall behind, and a new command arrives already
    covered by everything below.
    """
    source = Path(__file__).resolve().parent.parent / "cs" / "cli" / "dispatch.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    dispatch = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_dispatch"
    )
    found: set[str] = set()
    for node in ast.walk(dispatch):
        if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name):
            continue
        if node.left.id != "cmd":
            continue
        for operand in node.comparators:
            if isinstance(operand, ast.Constant) and isinstance(operand.value, str):
                found.add(operand.value)
            elif isinstance(operand, (ast.Tuple, ast.Set, ast.List)):
                found.update(
                    element.value for element in operand.elts
                    if isinstance(element, ast.Constant)
                    and isinstance(element.value, str)
                )
    return found


# What each command needs before it will do anything. Anything not named here
# is run bare.
ARGUMENTS = {
    "show": ["sess-alpha"], "view": ["sess-alpha"], "info": ["sess-alpha"],
    "brief": ["sess-alpha"], "digest": ["sess-alpha"], "summary": ["sess-alpha"],
    "read": ["sess-alpha"], "transcript": ["sess-alpha"],
    "export": ["sess-alpha"],
    "resume": ["sess-alpha"], "r": ["sess-alpha"],
    "search": ["portal"], "find": ["portal"], "grep": ["portal"],
    "similar": ["sess-alpha"], "like": ["sess-alpha"],
    "files": ["globe.js"],
    "completion": ["bash"],
    "pin": ["sess-alpha"], "unpin": ["sess-alpha"],
    "note": ["sess-alpha", "fixture note"],
    "tag": ["sess-alpha", "wip"], "untag": ["sess-alpha", "wip"],
}

# `-h`/`-v` are the same code as their words; running both proves nothing and
# doubles the slowest tests here.
SKIP = {"-h", "--help", "-v", "--version"}

# Commands that take a session and therefore cannot be asked anything about a
# store with no sessions in it.
NEEDS_A_SESSION = {
    "show", "view", "info", "brief", "digest", "summary", "read",
    "transcript", "export", "resume", "r",
    "pin", "unpin", "note", "tag", "untag", "similar", "like",
}


class SurfaceTest(StoreTest):
    """The commands, run.  Nothing here reads the output — only the exit."""

    def setUp(self):
        super().setUp()
        # `resume` hands the process to the Copilot CLI. On a machine that has
        # one, a smoke test would replace the test runner with it.
        self._which = mock.patch("shutil.which", return_value=None)
        self._which.start()

    def tearDown(self):
        self._which.stop()
        super().tearDown()

    def _try(self, *args: str) -> tuple[int, str]:
        """Run, and let anything that is not a clean exit reach the test."""
        from cs.cli import main

        out, err = io.StringIO(), io.StringIO()
        code = 0
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(list(args))
            except SystemExit as stop:
                code = stop.code if isinstance(stop.code, int) else 1
        return code, out.getvalue() + err.getvalue()

    def _commands(self) -> list[tuple[str, list[str]]]:
        return [
            (name, ARGUMENTS.get(name, []))
            for name in sorted(_dispatch_commands() - SKIP)
        ]

    # ── The surface exists ───────────────────────────────────────────

    def test_the_dispatch_table_was_actually_found(self):
        """The rest of this file is worthless if the parse returned nothing,
        and an empty set passes every loop below in silence."""
        commands = _dispatch_commands()
        self.assertGreater(len(commands), 25, commands)
        for expected in ("recent", "show", "read", "cost", "audit", "skills"):
            self.assertIn(expected, commands)

    def test_every_command_the_completion_script_offers_dispatches(self):
        """A completion that offers a command the dispatcher dropped teaches
        people to type something that errors."""
        from cs.cli import _COMPLETION_COMMANDS

        self.assertEqual(set(_COMPLETION_COMMANDS) - _dispatch_commands(), set())

    def test_every_command_runs_against_a_full_store(self):
        for name, args in self._commands():
            with self.subTest(command=name):
                code, out = self._try(name, *args)
                self.assertNotIn("Traceback", out)
                self.assertNotIn("unknown command", out)
                # resume has nowhere to go with no `copilot` on PATH; that is
                # a refusal, and a clear one, not a failure to run.
                if name not in ("resume", "r"):
                    self.assertEqual(code, 0, out[:400])

    def test_every_command_runs_against_a_store_with_nothing_in_it(self):
        """A fresh machine, or a window with no work in it. Every count is
        zero and every list is blank, which is the shape that divides by
        zero and indexes an empty list."""
        conn = sqlite3.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        for table in ("turns", "assistant_usage_events", "session_files",
                      "checkpoints", "session_refs", "search_index",
                      "sessions"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()
        for name, args in self._commands():
            if name in NEEDS_A_SESSION:
                continue
            with self.subTest(command=name):
                code, out = self._try(name, *args)
                self.assertNotIn("Traceback", out)
                self.assertEqual(code, 0, out[:400])

    def test_every_command_runs_against_a_store_from_an_older_copilot(self):
        """The schema is Copilot's. `db.optional` and the `has_*` guards
        exist so a store without the usage, files or search tables still
        reports everything it can — and nothing ran them across the whole
        surface, so a query that stopped being guarded would only show up
        on somebody else's machine."""
        base = Path(self._tmp.name) / "old"
        base.mkdir()
        conn = sqlite3.connect(base / "session-store.db")
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, cwd TEXT, summary TEXT,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
                turn_index INTEGER, user_message TEXT, assistant_response TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO sessions VALUES ('old-1','/tmp/o','An older store',"
            "'2026-08-01T09:00:00Z','2026-08-01T10:00:00Z')"
        )
        conn.execute(
            "INSERT INTO turns (session_id, turn_index, user_message,"
            " assistant_response) VALUES ('old-1',0,'do the thing','done')"
        )
        conn.commit()
        conn.close()

        previous = os.environ["COPILOT_HOME"]
        os.environ["COPILOT_HOME"] = str(base)
        try:
            for name, args in self._commands():
                args = ["old-1"] if name in NEEDS_A_SESSION else args
                if name in ("files",):
                    continue          # needs the table it is named after
                with self.subTest(command=name):
                    code, out = self._try(name, *args)
                    self.assertNotIn("Traceback", out)
                    if name not in ("resume", "r"):
                        self.assertIn(code, (0, 1), out[:400])
                    # A missing table must be explained, never crashed on.
                    self.assertNotIn("no such table", out)
                    self.assertNotIn("no such column", out)
        finally:
            os.environ["COPILOT_HOME"] = previous

    # ── The promise the README makes ─────────────────────────────────

    def test_no_command_writes_to_the_session_store(self):
        """`db.connect()` opens the store read-only, and the README states it
        as a privacy guarantee rather than an implementation note. One
        command opening its own connection would break it silently — the
        store is Copilot's data and this tool has no business in it."""
        store = Path(os.environ["COPILOT_HOME"]) / "session-store.db"
        before = hashlib.sha256(store.read_bytes()).hexdigest()
        for name, args in self._commands():
            self._try(name, *args)
        self.assertEqual(hashlib.sha256(store.read_bytes()).hexdigest(), before)

    def test_no_command_writes_anywhere_under_copilot_home(self):
        """The event logs are read too, now, so the promise covers the whole
        tree: after every command has run, `COPILOT_HOME` is byte-identical.
        The settings and cache directories the suite points inside it are the
        only things allowed to change, and they are cs's own."""
        from support import _alpha_events, _write_events

        home = Path(os.environ["COPILOT_HOME"])
        _write_events(home, "sess-alpha", _alpha_events())
        ours = {home / ".config", home / ".cache"}

        def listing() -> dict[str, str]:
            return {
                str(path.relative_to(home)):
                    hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(home.rglob("*"))
                if path.is_file() and not any(p in ours for p in path.parents)
            }

        before = listing()
        for name, args in self._commands():
            self._try(name, *args)
            self._try(name, *args, "--json")
        self.assertEqual(listing(), before)

    def test_no_command_leaves_a_journal_beside_the_store(self):
        """A write attempt shows up as a -wal or -journal file even when the
        write itself fails, so their absence is the stronger claim."""
        home = Path(os.environ["COPILOT_HOME"])
        for name, args in self._commands():
            self._try(name, *args)
        strays = [p.name for p in home.iterdir()
                  if p.name.startswith("session-store.db-")]
        self.assertEqual(strays, [])

    # ── The machine-readable surface ─────────────────────────────────

    def test_every_data_format_is_valid_or_refused_in_a_sentence(self):
        """`--json` reaches every command, including the ones that have no
        data form. Those must say so; none of them may emit half a
        document, because something downstream is parsing it."""
        for name, args in self._commands():
            for flag in ("--json", "--csv"):
                with self.subTest(command=name, flag=flag):
                    code, out = self._try(name, *args, flag)
                    self.assertNotIn("Traceback", out)
                    if code != 0:
                        self.assertIn("error:", out)
                        continue
                    if flag == "--json" and out.strip():
                        json.loads(out)

    def test_an_unknown_command_is_refused_rather_than_guessed_at(self):
        code, out = self._try("recennt")
        self.assertEqual(code, 1)
        self.assertIn("unknown command", out)

    def test_the_why_flag_never_changes_what_a_report_computes(self):
        """The two forms of a report are the same report; the moment one of
        them filters differently the flag has become a second code path."""
        numbers = __import__("re").compile(r"\d[\d,.]*")
        for name in ("stats", "cost", "efficiency", "repos", "timeline",
                     "audit", "yolo", "handoff", "skills", "hooks", "mcp"):
            with self.subTest(command=name):
                _, plain = self._try(name)
                _, why = self._try(name, "--why")
                self.assertTrue(
                    set(numbers.findall(plain)) <= set(numbers.findall(why)),
                    f"{name} computed something different with --why",
                )


class StoreDiscoveryTest(unittest.TestCase):
    """What happens before there is a store to read."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self._previous = os.environ.get("COPILOT_HOME")
        os.environ["COPILOT_HOME"] = self._tmp.name
        os.environ["TERM"] = "dumb"

    def tearDown(self):
        if self._previous is None:
            os.environ.pop("COPILOT_HOME", None)
        else:
            os.environ["COPILOT_HOME"] = self._previous
        self._tmp.cleanup()

    def _try(self, *args: str) -> tuple[int, str]:
        from cs.cli import main

        out, err = io.StringIO(), io.StringIO()
        code = 0
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(list(args))
            except SystemExit as stop:
                code = stop.code if isinstance(stop.code, int) else 1
        return code, out.getvalue() + err.getvalue()

    def test_no_store_is_explained_and_not_a_traceback(self):
        """Expected on a fresh box, and the message has to say what to do —
        the handover leans on this being a sentence rather than a stack."""
        code, out = self._try("recent")
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", out)
        self.assertIn("session store", out.lower())

    def test_a_store_that_is_not_a_database_is_refused_cleanly(self):
        """Half a download, or a file restored from the wrong backup."""
        store = Path(self._tmp.name) / "session-store.db"
        store.write_bytes(b"this is not a database, it is a note\n")
        code, out = self._try("recent")
        self.assertNotIn("Traceback", out)
        self.assertEqual(code, 1)

    def test_a_store_with_no_sessions_table_is_refused_cleanly(self):
        """A valid SQLite file that is not a Copilot store — the shape a
        wrong COPILOT_HOME actually produces."""
        store = Path(self._tmp.name) / "session-store.db"
        conn = sqlite3.connect(store)
        conn.execute("CREATE TABLE notes (body TEXT)")
        conn.commit()
        conn.close()
        code, out = self._try("recent")
        self.assertNotIn("Traceback", out)
        self.assertEqual(code, 1)

    def test_the_store_is_found_again_once_it_is_there(self):
        """The negative tests above are only meaningful if the positive one
        runs in the same place."""
        _build_store(Path(self._tmp.name))
        code, out = self._try("recent", "3650")
        self.assertEqual(code, 0, out[:400])
