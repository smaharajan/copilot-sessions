"""The main surface: listing, search, show, read, resume, export and the
flags that cut across them.
"""

from __future__ import annotations

import io
import os
import re
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path

from support import Screen, StoreTest, _Tty

from cs.ui import cells as ui_cells


def _column_x(frame: dict, label: str) -> int:
    """Where a listing column starts, found by its header rather than assumed."""
    return next(x for (y, x), text in frame.items()
                if y == 3 and text.startswith(label))


class CSTest(StoreTest):
    def test_help(self):
        code, out = self._run("help")
        self.assertEqual(code, 0)
        self.assertIn("cs — Copilot Sessions", out)

    def test_version(self):
        code, out = self._run("version")
        self.assertEqual(code, 0)
        self.assertIn("cs ", out)

    def test_stats(self):
        code, out = self._run("stats")
        self.assertEqual(code, 0)
        self.assertIn("sessions", out)
        self.assertIn("2 (1 interactive)", out)

    def test_stats_reports_what_the_work_produced(self):
        """Commits, PRs, files and handoffs — the output side of the ledger."""
        code, out = self._run("stats")
        self.assertEqual(code, 0)
        self.assertIn("What it produced", out)
        self.assertIn("commits", out)
        self.assertIn("1 recorded (1 distinct)", out)   # one commit, one PR
        self.assertIn("1 created · 2 edited", out)      # files
        self.assertIn("2 checkpoints written", out)
        self.assertIn("What it cost", out)
        self.assertIn("How it was done", out)           # delegation section
        self.assertIn("1 sub-agent calls", out)

    def test_recent_hides_empty(self):
        code, out = self._run("recent", "7")
        self.assertEqual(code, 0)
        self.assertIn("Build Three.js portal", out)
        self.assertNotIn("Empty session", out)  # zero-turn hidden
        self.assertIn("sorted by active ↓", out)
        self.assertIn("Sort: --sort active|turns|credits|skills|agents|summary|repo", out)

    def test_all_shows_empty(self):
        code, out = self._run("all", "7")
        self.assertEqual(code, 0)
        self.assertIn("Empty session", out)

    def test_search(self):
        code, out = self._run("search", "portal")
        self.assertEqual(code, 0)
        self.assertIn("Three.js", out)

    def test_search_shows_matching_text(self):
        """Full-text hits carry the snippet that matched and where it's from."""
        code, out = self._run("search", "globe")
        self.assertEqual(code, 0)
        self.assertIn("best match first", out)
        self.assertIn("Three.js", out)          # the session
        self.assertIn("spinning globe", out)    # the line that matched
        self.assertIn("turn", out)              # where it came from

    def test_search_finds_checkpoints_and_replies(self):
        """Search reaches content the old summary/user-message scan missed."""
        _, out = self._run("search", "live data")
        self.assertIn("Three.js", out)
        self.assertIn("next steps", out)  # matched a checkpoint, not a turn

    def test_search_survives_punctuation(self):
        """'three.js' and 'C++' are FTS5 syntax errors — they must still work."""
        code, out = self._run("search", "three.js")
        self.assertEqual(code, 0)
        self.assertIn("Three.js portal", out)

    def test_search_joins_multiple_words(self):
        """'cs search a b' is one query, not a query plus a stray argument."""
        code, out = self._run("search", "spinning", "globe")
        self.assertEqual(code, 0)
        self.assertIn("'spinning globe'", out)
        self.assertIn("Three.js", out)

    def test_search_supports_operators(self):
        code, out = self._run("search", "globe OR packaging")
        self.assertEqual(code, 0)
        self.assertIn("Three.js", out)

    def test_search_does_not_highlight_fts_operators(self):
        from unittest import mock

        from cs import cli, ui

        with mock.patch.object(ui, "AMBER", "<hit>"), \
                mock.patch.object(ui, "BOLD", ""), \
                mock.patch.object(ui, "RST", "</hit>"), \
                mock.patch.object(ui, "DIM", ""):
            marked = cli._highlight("or deploy near failure", "deploy OR NEAR(failure)")
        self.assertNotIn("<hit>or</hit>", marked.lower())
        self.assertNotIn("<hit>near</hit>", marked.lower())
        self.assertIn("<hit>deploy</hit>", marked)
        self.assertIn("<hit>failure</hit>", marked)

    def test_search_sort_overrides_relevance(self):
        code, out = self._run("search", "portal", "--sort", "credits")
        self.assertEqual(code, 0)
        self.assertIn("sorted by credits ↓", out)
        self.assertNotIn("best match first", out)

    def test_search_without_fts_index(self):
        """Older stores have no search_index — searching still works."""
        import sqlite3 as sq

        conn = sq.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        conn.execute("DROP TABLE search_index")
        conn.commit()
        conn.close()
        code, out = self._run("search", "charts")
        self.assertEqual(code, 0)
        self.assertIn("Three.js", out)  # found via the turns table

    def _add_matching_sessions(self, count: int, *, indexed: bool = True) -> None:
        """`count` sessions that mention 'quokka' once, each in its own turn."""
        conn = sqlite3.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        sessions = [f"sess-q{n:03d}" for n in range(count)]
        conn.executemany(
            "INSERT INTO sessions (id, summary, created_at, updated_at) "
            "VALUES (?, ?, datetime('now'), datetime('now'))",
            [(sid, f"Session {sid}") for sid in sessions],
        )
        conn.executemany(
            "INSERT INTO turns (session_id, turn_index, user_message, "
            "assistant_response, timestamp) VALUES (?, 0, ?, 'ok', 't')",
            [(sid, "where did the quokka go") for sid in sessions],
        )
        if indexed:
            conn.executemany(
                "INSERT INTO search_index (content, session_id, source_type) "
                "VALUES (?, ?, 'turn')",
                [("where did the quokka go", sid) for sid in sessions],
            )
        conn.commit()
        conn.close()

    def test_search_returns_every_matching_session(self):
        """A term found in many sessions finds all of them, not the first 40.

        The cap hid real matches with nothing on screen to say so. 520 also
        crosses the 500-id batch the snippet and row lookups are split into.
        """
        from cs import db

        self._add_matching_sessions(520)
        conn = db.connect()
        try:
            rows, hits = db.search(conn, "quokka")
        finally:
            conn.close()
        self.assertEqual(len(rows), 520)
        self.assertEqual(len({row[0] for row in rows}), 520)
        self.assertEqual(set(hits), {row[0] for row in rows})
        self.assertTrue(all("quokka" in snippet for _, snippet in hits.values()))

        code, out = self._run("search", "quokka")
        self.assertEqual(code, 0)
        self.assertIn("'quokka' · 520 sessions", out)

    def test_search_without_fts_index_is_not_capped(self):
        """The turns scan behind an old store has no limit either."""
        from cs import db

        self._add_matching_sessions(450, indexed=False)
        raw = sqlite3.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        raw.execute("DROP TABLE search_index")
        raw.commit()
        raw.close()
        conn = db.connect()
        try:
            rows, hits = db.search(conn, "quokka")
        finally:
            conn.close()
        self.assertEqual(len(rows), 450)
        self.assertEqual(hits, {})

    def _name_session(self, session_id: str, yaml_body: str) -> None:
        folder = Path(os.environ["COPILOT_HOME"]) / "session-state" / session_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "workspace.yaml").write_text(f"id: {session_id}\n{yaml_body}")

    def test_search_finds_a_session_by_the_name_you_gave_it(self):
        """/rename writes workspace.yaml and never the store's summary.

        The session is found by the name, listed under it, and its page is
        headed with it — the one name you would recognise it by.
        """
        self._name_session(
            "sess-alpha", "name: Checkout QRX review\nuser_named: true\n"
        )
        code, out = self._run("search", "QRX")
        self.assertEqual(code, 0)
        self.assertIn("'QRX' · 1 session", out)
        self.assertIn("Checkout QRX review", out)
        self.assertNotIn("Build Three.js portal", out)  # the stale summary

        _, listed = self._run("recent", "7")
        self.assertIn("Checkout QRX review", listed)
        _, page = self._run("show", "sess-alpha")
        self.assertIn("Checkout QRX review", page)

    def test_search_finds_copilots_generated_name_and_says_so(self):
        """A name Copilot generated is searched, but not promoted to the title."""
        self._name_session(
            "sess-alpha", "name: Explain QRX pilot\nuser_named: false\n"
        )
        from cs import db

        conn = db.connect()
        try:
            rows, hits = db.search(conn, "qrx")
        finally:
            conn.close()
        self.assertEqual([row[0] for row in rows], ["sess-alpha"])
        self.assertEqual(rows[0][2], "Build Three.js portal")
        self.assertEqual(hits["sess-alpha"], ("name", "Explain QRX pilot"))

    def test_a_name_hit_is_masked(self):
        token = "ghp_" + "Z" * 36
        self._name_session(
            "sess-alpha", f"name: 'deploy QRX with {token}'\nuser_named: false\n"
        )
        code, out = self._run("search", "QRX")
        self.assertEqual(code, 0)
        self.assertIn("name", out)
        self.assertNotIn(token, out)

    def test_search_explains_a_stored_summary_hidden_by_a_rename(self):
        self._name_session("sess-alpha", "name: Something else\nuser_named: true\n")
        from cs import db

        conn = db.connect()
        try:
            rows, hits = db.search(conn, "three.js")
        finally:
            conn.close()
        self.assertEqual(rows[0][2], "Something else")
        self.assertEqual(hits["sess-alpha"], ("summary", "Build Three.js portal"))

    def test_workspace_names_in_every_form_copilot_writes(self):
        from cs import db

        cases = {
            "name: Plain name\n": "Plain name",
            "name: 'It''s: quoted'\n": "It's: quoted",
            'name: "Double \\"quoted\\""\n': 'Double "quoted"',
            "name: |-\n  First line\n  second line\nuser_named: true\n":
                "First line second line",
            "summary: |-\n  name: not a key\nname: Real\n": "Real",
        }
        for body, expected in cases.items():
            with self.subTest(body=body):
                self.assertEqual(db._yaml_fields(body, ("name",))["name"], expected)

    def test_no_session_state_folder_is_no_names(self):
        from cs import db

        self.assertEqual(db.session_names(), {})
        self.assertIsNone(db.session_name("sess-alpha"))

    def test_workspace_names_stay_inside_session_state(self):
        from cs import db

        root = Path(os.environ["COPILOT_HOME"])
        (root / "workspace.yaml").write_text("name: Outside\nuser_named: true\n")
        state = root / "session-state"
        state.mkdir()
        (state / "linked").symlink_to(root, target_is_directory=True)
        for sid in ("..", ".", "", str(root), "../", "linked"):
            with self.subTest(sid=sid):
                self.assertIsNone(db.session_name(sid))

    def test_renamed_titles_are_masked_in_machine_output_and_file_results(self):
        token = "ghp_" + "Z" * 36
        self._name_session("sess-alpha", f"name: Review {token}\nuser_named: true\n")
        for args in (("recent", "--json"), ("recent", "--csv"),
                     ("files", "src"), ("show", "sess-alpha")):
            with self.subTest(args=args):
                code, out = self._run(*args)
                self.assertEqual(code, 0)
                self.assertNotIn(token, out)

    def test_cost(self):
        code, out = self._run("cost", "7")
        self.assertEqual(code, 0)
        self.assertIn("AI spend · last 7 days", out)
        self.assertIn("4.00 AIU", out)            # 1.5 + 2.5 nano-AIU
        self.assertIn("claude-opus-4.8", out)
        self.assertIn("2 across 1 sessions", out)  # 2 calls, 1 session
        self.assertIn("portal", out)               # by-repository section
        # No `issues` row. It was thirteen events in thirty-nine thousand on a
        # page about where the credits went, and a content filter is not a
        # spend fact — `cs efficiency` breaks the same calls out by finish
        # reason, which is the question they answer.
        self.assertNotIn("issues", out)
        _, efficiency = self._run("efficiency", "7")
        self.assertIn("Calls that ended badly", efficiency)
        self.assertIn("error: 1", efficiency)      # one 'error' finish_reason

    def test_spend_is_reported_per_vendor_whoever_the_vendor_is(self):
        """Copilot routes across providers, so nothing here may assume one.

        The fixture spends on an OpenAI model and an Anthropic one; both must
        appear, named exactly as the store recorded them.
        """
        code, out = self._run("cost", "7")
        self.assertEqual(code, 0)
        self.assertIn("gpt-5.5", out)
        self.assertIn("claude-opus-4.8", out)

    def test_cost_without_usage_table(self):
        import sqlite3 as sq

        conn = sq.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        conn.execute("DROP TABLE assistant_usage_events")
        conn.commit()
        conn.close()
        code, _ = self._run("cost")
        self.assertEqual(code, 1)

    def test_credentials_are_masked_everywhere(self):
        """A secret in a session must not reach any view."""
        import sqlite3 as sq

        secrets = {
            "aws": "AKIAIOSFODNN7EXAMPLE",
            "github": "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",  # gitleaks:allow
            "assignment": "hunter2trombone",
            "url": "postgres://admin:s3cr3tP4ss@db.internal:5432/app",
        }
        blob = (
            f"deploy with {secrets['aws']} and token {secrets['github']}\n"
            f"password={secrets['assignment']}\n{secrets['url']}\n"
        )
        conn = sq.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        conn.execute(
            "INSERT INTO turns VALUES (7,'sess-alpha',5,?,?,'t')", (blob, blob)
        )
        conn.execute(
            "INSERT INTO search_index (content, session_id, source_type) VALUES (?,?,?)",
            (blob, "sess-alpha", "turn"),
        )
        conn.execute(
            "UPDATE checkpoints SET next_steps = ? WHERE checkpoint_number = 2",
            (f"- rotate {secrets['github']} then redeploy",),
        )
        conn.commit()
        conn.close()

        for args in (
            ("read", "sess-alpha"),
            ("brief", "sess-alpha"),
            ("show", "sess-alpha"),
            ("search", "deploy"),
        ):
            _, out = self._run(*args)
            for label, secret in secrets.items():
                with self.subTest(view=args[0], secret=label):
                    self.assertNotIn(secret, out)
            self.assertIn("[redacted", out.replace("[redacted]", "[redacted"))

    def test_redaction_can_be_turned_off_deliberately(self):
        from cs import redact

        os.environ["CS_REDACT"] = "0"
        self.addCleanup(os.environ.pop, "CS_REDACT", None)
        self.assertFalse(redact.enabled())
        self.assertIn("AKIAIOSFODNN7EXAMPLE", redact.redact("key AKIAIOSFODNN7EXAMPLE"))

    def test_redaction_leaves_ordinary_text_alone(self):
        """AI sessions talk about tokens constantly — those are not secrets."""
        from cs import redact

        for benign in (
            "total tokens: 15234",
            "max_tokens = 4096",
            "the password is stored in the vault",
            "token: the model reported",
            "password=<your-password>",
            "api_key: ${API_KEY}",
            "secret: null",
        ):
            with self.subTest(text=benign):
                self.assertEqual(redact.redact(benign), benign)

    def test_redaction_masks_each_credential_shape(self):
        from cs import redact

        cases = {
            "AKIAIOSFODNN7EXAMPLE": "aws-key-id",
            "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6": "github-token",
            "xoxb-123456789012-abcdefghijkl": "slack-token",
            "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv": "api-key",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.dBjftJeZ4CVP": "jwt",  # gitleaks:allow
        }
        for secret, kind in cases.items():
            masked = redact.redact(f"value: {secret} end")
            with self.subTest(kind=kind):
                self.assertNotIn(secret, masked)
                self.assertIn(kind, masked)
        block = (
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKC\n-----END RSA PRIVATE KEY-----"
        )
        self.assertNotIn("MIIEowIBAAKC", redact.redact(block))

    def test_redaction_masks_credentials_in_json_and_quoted_values(self):
        """Transcripts are full of JSON payloads; a closing quote hid the value."""
        from cs import redact

        for text, secret in (
            ('{"password": "aaaabbbbcccc"}', "aaaabbbbcccc"),
            ('{"token":"aaaabbbbccccdddd"}', "aaaabbbbccccdddd"),
            ('password = "aaaa bbbb cccc"', "aaaa bbbb cccc"),
            ("Server=db;Password=p@ssw0rd;Database=app", "p@ssw0rd"),
        ):
            with self.subTest(text=text):
                masked = redact.redact(text)
                self.assertNotIn(secret, masked)
                self.assertIn("[redacted]", masked)
        # The rest of a connection string stays readable.
        self.assertIn(
            "Database=app", redact.redact("Server=db;Password=p@ssw0rd;Database=app")
        )

    def test_brief_digests_a_session(self):
        """The brief answers 'what happened and what's left' without reading it all."""
        code, out = self._run("brief", "sess-alpha")
        self.assertEqual(code, 0)
        # The opening ask and the final request, in full.
        self.assertIn("First request", out)
        self.assertIn("make a portal", out)
        self.assertIn("Last request", out)
        self.assertIn("add charts", out)
        # Still open leads: it is the section that changes what you do next.
        self.assertLess(out.index("Still open"), out.index("What got done"))
        self.assertLess(out.index("Still open"), out.index("First request"))
        # After that the session tells its own story in order: what was asked
        # for, what came of it, where you left off. The outcome used to print
        # before the request that caused it.
        self.assertLess(out.index("First request"), out.index("What got done"))
        self.assertLess(out.index("What got done"), out.index("Last request"))
        # What got done and — the point of the whole view — what is still open.
        self.assertIn("shipped the globe", out)
        self.assertIn("Still open", out)
        self.assertIn("point the charts at live data", out)
        # Shipped refs and the headline spend.
        self.assertIn("abc1234", out)
        self.assertIn("#42", out)
        self.assertIn("4.00 AIU", out)
        # And a way through to the views this one is not.
        self.assertIn("cs show sess-alpha", out)
        self.assertIn("cs read sess-alpha", out)
        self.assertIn("cs resume sess-alpha", out)

    def test_the_three_session_views_do_not_repeat_each_other(self):
        """show is the whole page, --short is its top, read is the words.

        There used to be three views and a rule that no fact appeared in
        two of them. The rule was sound and the split was not: brief
        answered "what happened" and show answered "what it cost", and
        nobody ever wanted one of those without the other, so every brief
        was followed by a show. They are now one page, and the contract
        that replaces the old one is a nesting rather than a partition —
        `--short` prints a prefix of `show`, never a fact `show` omits,
        and the transcript still belongs to `read` alone.
        """
        _, brief = self._run("brief", "sess-alpha")
        _, show = self._run("show", "sess-alpha")
        _, read = self._run("read", "sess-alpha")

        # The inventory is what --short trades away, and the reason to
        # ever type the long form.
        for section in ("Files touched", "Models", "How the work was done"):
            with self.subTest(section=section):
                self.assertIn(section, show)
                self.assertNotIn(section, brief)
                self.assertNotIn(section, read)
        # The transcript is read's: show reports replies only as a size.
        self.assertIn("sure, here it is", read)
        self.assertNotIn("sure, here it is", show)
        self.assertNotIn("sure, here it is", brief)
        # The judgement leads both, because it is the thing a reader came
        # for — the inventory is evidence for it, not a rival to it.
        for section in ("Still open", "What got done", "Shipped"):
            with self.subTest(section=section):
                self.assertIn(section, brief)
                self.assertIn(section, show)
        # Nesting, stated directly: every heading --short prints, show
        # prints too, in the same order.
        def heads(out):
            return [ln.strip()[1:].strip() for ln in out.split("\n")
                    if ln.strip().startswith("▌")]

        self.assertEqual(heads(brief), heads(show)[:len(heads(brief))])
        # One header, one shape, and each view points at the others.
        for out in (brief, show, read):
            self.assertIn("volume", out)
            self.assertIn("span", out)
            self.assertIn("cs resume sess-alpha", out)
        self.assertNotIn("|resume", brief + show + read)
        # The short form has to say where the rest of it went.
        self.assertIn("cs show sess-alpha", brief)
        self.assertNotIn("cs show sess-alpha", show)

    def test_brief_uses_the_latest_checkpoint(self):
        """Checkpoints accumulate; only the newest describes the current state."""
        _, out = self._run("brief", "sess-alpha")
        self.assertIn("shipped the globe", out)          # checkpoint 2 work
        self.assertIn("point the charts at live data", out)
        self.assertNotIn("Built the first draft", out)   # checkpoint 1 work
        self.assertNotIn("wire up the globe", out)       # its stale next step

    def test_brief_without_a_checkpoint_falls_back(self):
        """Most sessions have no checkpoint — the brief must still be useful."""
        import sqlite3 as sq

        conn = sq.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        conn.execute("DELETE FROM checkpoints")
        conn.commit()
        conn.close()
        code, out = self._run("brief", "sess-alpha")
        self.assertEqual(code, 0)
        self.assertIn("First request", out)
        self.assertLess(out.index("First request"), out.index("Where it ended"))
        self.assertIn("Where it ended", out)   # from the closing reply instead
        self.assertIn("done", out)             # the last assistant response
        # Files are show's, checkpoint or no checkpoint.
        self.assertNotIn("Files touched", out)
        self.assertIn("Files touched", self._run("show", "sess-alpha")[1])

    def test_brief_drops_harness_injected_blocks(self):
        """`<system_reminder>` blocks are not things the user asked."""
        import sqlite3 as sq

        conn = sq.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        conn.execute(
            "INSERT INTO turns VALUES (9,'sess-alpha',2,?,'ok','z')",
            ("<system_reminder>\nCustom instructions from repo/.github/x.md\n"
             "SECRET-INSTRUCTION-TEXT\n</system_reminder>",),
        )
        conn.commit()
        conn.close()
        _, out = self._run("brief", "sess-alpha")
        self.assertNotIn("SECRET-INSTRUCTION-TEXT", out)
        self.assertNotIn("system_reminder", out)
        # The turn is still counted. Filtering it out of the count was a
        # habit from when brief was a separate screen and could hold its
        # own opinion of how big a session was; now that the count and the
        # conversation index share a page, a header saying 2 above an index
        # listing 3 is a bug a reader can see. The block is dropped from
        # the prose, which is where it does harm, and nowhere else.
        self.assertIn("3 turns", out)
        _, listed = self._run("brief", "sess-alpha", "--asks")
        self.assertIn("Every request · 2", listed)  # only two are real asks

    def test_brief_never_truncates_mid_sentence(self):
        """A half-line is not information — long items wrap instead of clipping."""
        import sqlite3 as sq

        sentence = (
            "commit the no-secrets skill to acmeco/webshop-dotfiles and then verify "
            "with git status that only the two expected paths appear in the diff"
        )
        conn = sq.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        conn.execute(
            "UPDATE checkpoints SET next_steps = ? WHERE checkpoint_number = 2",
            ("- " + sentence,),
        )
        conn.commit()
        conn.close()
        _, out = self._run("brief", "sess-alpha")
        self.assertNotIn("…", out.split("Still open")[1].split("What got done")[0])
        # Every word survives, across however many lines it took.
        flattened = " ".join(out.split())
        self.assertIn(sentence, flattened)

    def test_brief_asks_flag_lists_every_request(self):
        _, plain = self._run("brief", "sess-alpha")
        self.assertNotIn("Every request", plain)
        _, full = self._run("brief", "sess-alpha", "--asks")
        self.assertIn("Every request · 2", full)
        self.assertIn("make a portal", full)
        self.assertEqual(self._run("brief", "sess-alpha", "--nope")[0], 1)

    def test_path_extraction_ignores_prose(self):
        """Checkpoint files are prose; only real paths should be listed."""
        from cs.cli import _paths

        text = (
            "`/tmp/a/build.py` handles reads/writes for `acmeco/dotfiles`.\n"
            "Also `~/.config/app/` and `sync.sh`, see docs/guide.md.\n"
            "Written in Node.js, version v1.2 — neither is a file."
        )
        found = _paths(text, 10)
        self.assertIn("/tmp/a/build.py", found)
        self.assertIn("~/.config/app/", found)
        self.assertIn("sync.sh", found)              # bare name, but backticked
        self.assertIn("docs/guide.md", found)
        self.assertNotIn("reads/writes", found)      # prose with a slash
        self.assertNotIn("acmeco/dotfiles", found)   # a repo, not a path
        # A bare word with a dot is only a path when the author marked it as
        # code — otherwise "Node.js" and "v1.2" would litter every brief.
        self.assertNotIn("Node.js", found)
        self.assertNotIn("v1.2", found)

    def test_brief_rejects_unknown_session(self):
        self.assertEqual(self._run("brief")[0], 1)
        self.assertEqual(self._run("brief", "nope")[0], 1)

    def test_read_shows_the_whole_conversation(self):
        """cs read prints both sides in full — cs show only summarises them."""
        code, out = self._run("read", "sess-alpha")
        self.assertEqual(code, 0)
        self.assertIn("make a portal", out)        # the prompt
        self.assertIn("sure, here it is", out)     # the reply, which cs show omits
        self.assertIn("add charts", out)
        self.assertIn("Turn 0", out)
        self.assertIn("Turn 1", out)

        # cs show still reports the reply only as a size, which is why cs read exists.
        _, shown = self._run("show", "sess-alpha")
        self.assertNotIn("sure, here it is", shown)

    def test_read_single_turn(self):
        code, out = self._run("read", "sess-alpha", "--turn", "1")
        self.assertEqual(code, 0)
        self.assertIn("add charts", out)
        self.assertNotIn("make a portal", out)      # turn 0 is left out
        _, missing = self._run("read", "sess-alpha", "--turn", "99")
        self.assertIn("no turn #99", missing)

    def test_ctrl_c_quits_the_listing_without_a_traceback(self):
        """Ctrl-C at the key prompt is a quit, not a crash."""
        from cs.cli import _listing_tui

        class Interrupting(Screen):
            def getch(self):
                if self.no_wait and not self.keys:
                    return -1
                raise KeyboardInterrupt

        rows = [("id-one", "2026-08-01T10:00", "One", "r/a", "/tmp", 1, 10)]
        self.assertIsNone(_listing_tui(Interrupting(), rows, "Sessions"))

    def test_ctrl_c_anywhere_else_exits_quietly(self):
        from unittest import mock

        from cs.cli import main

        with mock.patch("cs.cli._dispatch", side_effect=KeyboardInterrupt):
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(["recent"]), 130)
        self.assertNotIn("Traceback", out.getvalue())

    def test_closed_pipe_is_not_an_error(self):
        """`cs recent | head` closes the pipe early — that is a success."""
        from unittest import mock

        from cs.cli import main

        with mock.patch("cs.cli._dispatch", side_effect=BrokenPipeError):
            self.assertEqual(main(["recent"]), 0)

    def test_paging_reports_whether_it_paged(self):
        """The caller needs to know: a dismissed pager must not also pause."""
        from unittest import mock

        from cs.cli import _page

        long_text = "\n".join(f"line {i}" for i in range(500))
        mock.patch("cs.cli._less_wheel_lines", return_value=False).start()
        self.addCleanup(mock.patch.stopall)
        screen = mock.patch(
            "cs.cli.shutil.get_terminal_size", return_value=os.terminal_size((100, 24))
        )

        # Not a TTY → printed inline, no pager, caller must pause.
        with mock.patch("sys.stdout.isatty", return_value=False):
            with redirect_stdout(io.StringIO()):
                self.assertFalse(_page(long_text))

        # A TTY with more text than fits → pager runs, caller must not pause.
        with mock.patch("sys.stdout.isatty", return_value=True), \
             screen, mock.patch("cs.cli.subprocess.run") as run:
            self.assertTrue(_page(long_text))
            self.assertEqual(run.call_count, 1)

        # Short text on a TTY stays inline — nothing to dismiss, so pause.
        with mock.patch("sys.stdout.isatty", return_value=True), \
             screen, mock.patch("cs.cli.subprocess.run") as run:
            with redirect_stdout(io.StringIO()):
                self.assertFalse(_page("one line"))
            self.assertEqual(run.call_count, 0)

        # No pager on the system → fall back to printing, and pause.
        with mock.patch("sys.stdout.isatty", return_value=True), \
             screen, mock.patch("cs.cli.subprocess.run", side_effect=OSError):
            with redirect_stdout(io.StringIO()):
                self.assertFalse(_page(long_text))

    def test_pager_gets_mouse_support_when_less_is_new_enough(self):
        """Scrolling beats arrow-walking — but only less 551+ can do it."""
        from unittest import mock

        import cs.cli as cli

        def version(text):
            return mock.Mock(stdout=text)

        cli._LESS_MOUSE.clear()
        with mock.patch("cs.cli.subprocess.run", return_value=version("less 668\n")):
            self.assertTrue(cli._less_wheel_lines("less"))
        cli._LESS_MOUSE.clear()
        with mock.patch("cs.cli.subprocess.run", return_value=version("less 487\n")):
            self.assertFalse(cli._less_wheel_lines("less"))
        cli._LESS_MOUSE.clear()
        with mock.patch("cs.cli.subprocess.run", side_effect=OSError):
            self.assertFalse(cli._less_wheel_lines("less"))

        # The answer is cached, so the probe runs once per binary.
        cli._LESS_MOUSE.clear()
        with mock.patch("cs.cli.subprocess.run", return_value=version("less 668")) as run:
            cli._less_wheel_lines("less")
            cli._less_wheel_lines("less")
            self.assertEqual(run.call_count, 1)
        cli._LESS_MOUSE.clear()

    def test_pager_command_carries_the_mouse_flags(self):
        from unittest import mock

        from cs.cli import _page

        long_text = "\n".join(f"line {i}" for i in range(500))
        with mock.patch("sys.stdout.isatty", return_value=True), \
             mock.patch.dict(os.environ, {"PAGER": "less"}), \
             mock.patch("cs.cli.shutil.get_terminal_size",
                        return_value=os.terminal_size((100, 24))), \
             mock.patch("cs.cli._less_wheel_lines", return_value=True), \
             mock.patch("cs.cli.subprocess.run") as run:
            _page(long_text)
        argv = run.call_args[0][0]
        self.assertIn("--mouse", argv)
        self.assertIn("--wheel-lines=3", argv)
        self.assertIn("-R", argv)

    def test_read_reports_paging_to_its_caller(self):
        from unittest import mock

        from cs.cli import cmd_read

        with mock.patch("cs.cli._page", return_value=True) as page:
            self.assertTrue(cmd_read("sess-alpha"))
            self.assertEqual(page.call_count, 1)
        # A session with no turns pages nothing, so the caller still pauses.
        with redirect_stdout(io.StringIO()):
            self.assertFalse(cmd_read("sess-empty"))

    def test_pause_discards_keys_typed_before_the_prompt(self):
        """A 'q' meant for the pager must not answer the next question."""
        from unittest import mock

        from cs.cli import _pause

        with mock.patch("cs.cli._drain_stdin") as drain, \
             mock.patch("cs.cli._read_key", return_value=None), \
             mock.patch("builtins.input", return_value="") as prompt:
            self.assertTrue(_pause("x"))
            drain.assert_called_once()
            # The flush happens before anything is read.
            self.assertEqual(drain.call_count, 1)
            self.assertEqual(prompt.call_count, 1)

    def test_read_rejects_bad_input(self):
        self.assertEqual(self._run("read")[0], 1)                        # no session
        self.assertEqual(self._run("read", "nope")[0], 1)                # unknown id
        self.assertEqual(self._run("read", "sess-alpha", "--turn", "x")[0], 1)
        self.assertEqual(self._run("read", "sess-alpha", "--wat")[0], 1)

    def test_markdown_rendering_rules(self):
        """The shapes assistant replies actually use, rendered not dumped."""
        from cs import ui

        bullet = ui.markdown("- a bullet whose text runs past the edge here", 34)
        self.assertTrue(all(len(line) <= 40 for line in bullet))
        self.assertTrue(bullet[0].lstrip().startswith("•"))
        # Continuation hangs under the text, not under the bullet.
        self.assertTrue(bullet[1].startswith("      "))

        # Fenced code keeps its own alignment and is never reflowed or cut.
        long_line = "x = " + "y" * 120
        code = ui.markdown(f"```python\n    {long_line}\n```", 40)
        self.assertTrue(any(long_line in line for line in code))

        # Headings render as text, not as '###'.
        self.assertNotIn("##", "".join(ui.markdown("## Heading", 40)))

        # Without colour, inline markers are stripped rather than shown raw.
        rendered = " ".join(ui.markdown("use `cs read` for **everything**", 60))
        self.assertNotIn("`", rendered)
        self.assertNotIn("**", rendered)
        self.assertIn("cs read", rendered)

    def test_markdown_styling_survives_wrapping(self):
        """A bold span broken over two lines used to leak its '**' markers."""
        from cs import ui

        lines = ui.markdown(
            "Your last presentation session is **the Q1 Presentation Deck** today.", 44
        )
        self.assertGreater(len(lines), 1)          # it really did wrap
        self.assertNotIn("*", "".join(lines))
        self.assertIn("Q1 Presentation Deck", " ".join(part.strip() for part in lines))

    def test_markdown_tables_render_as_aligned_columns(self):
        """Raw '| a | b |' pipes are the least readable thing on the screen."""
        from cs import ui

        lines = [
            line for line in ui.markdown(
                "| Step | Status |\n|---|---|\n"
                "| Deploy the portal | ✅ done |\n| Verify headless | ⬜ pending |",
                60,
            ) if line.strip()
        ]
        self.assertNotIn("|", "".join(lines))
        self.assertIn("─", lines[1])               # header rule, not '|---|'
        # Emoji take two columns, so 'done' and 'pending' still line up.
        starts = {
            line.index("done") if "done" in line else line.index("pending")
            for line in lines if "done" in line or "pending" in line
        }
        self.assertEqual(len(starts), 1)

    def test_a_long_transcript_offers_a_way_in(self):
        """Turn headers name the ask, so scrolling says where you are."""
        code, out = self._run("read", "sess-alpha")
        self.assertEqual(code, 0)
        self.assertIn("Turn 0 · make a portal", out)

    def test_files_finds_sessions_that_touched_a_path(self):
        code, out = self._run("files", "globe.js")
        self.assertEqual(code, 0)
        self.assertIn("Three.js portal", out)     # sess-alpha edited it
        self.assertIn("edit", out)                # and what it did
        self.assertIn("globe.js", out)

    def test_files_matches_wildcards_and_partials(self):
        _, exact = self._run("files", "portal/index.html")
        self.assertIn("Three.js portal", exact)
        self.assertIn("Empty session", exact)     # both sessions touched it
        _, glob = self._run("files", "*.js")
        self.assertIn("Three.js portal", glob)
        self.assertNotIn("Empty session", glob)   # it never touched a .js file
        # A glob matches the tail of an absolute path without a leading '*'.
        _, tail = self._run("files", "portal/*.js")
        self.assertIn("Three.js portal", tail)
        # A full path works as well, and a wildcard one can be anchored.
        self.assertIn("Three.js portal", self._run("files", "/tmp/a/portal/globe.js")[1])
        self.assertIn("Three.js portal", self._run("files", "/tmp/a/*.js")[1])
        self.assertIn("No sessions found", self._run("files", "/nope/*.js")[1])
        _, none = self._run("files", "nothing-here")
        self.assertIn("No sessions found", none)

    def test_files_needs_a_path_to_look_up(self):
        """The bare form was a leaderboard of the most worked-on files, which
        led nowhere: the busiest path in a real store is a README touched nine
        times. Without a path there is nothing to answer, so it says so."""
        code, out = self._run("files")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")

    def test_show_lists_files_touched(self):
        code, out = self._run("show", "sess-alpha")
        self.assertEqual(code, 0)
        self.assertIn("Files touched", out)
        self.assertIn("1 created", out)
        self.assertIn("index.html", out)

    def test_files_without_the_table(self):
        """Older stores have no session_files — the commands still behave."""
        import sqlite3 as sq

        conn = sq.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        conn.execute("DROP TABLE session_files")
        conn.commit()
        conn.close()
        code, out = self._run("files", "globe.js")
        self.assertEqual(code, 0)
        self.assertIn("No sessions found", out)
        self.assertEqual(self._run("show", "sess-alpha")[0], 0)

    def test_agents_reports_delegation(self):
        """Who initiated the work — the signal only the usage events carry."""
        code, out = self._run("agents", "7")
        self.assertEqual(code, 0)
        self.assertIn("Delegation", out)
        self.assertIn("Who initiated the work", out)
        self.assertIn("sub-agents", out)   # fixture has one sub-agent call
        self.assertIn("you", out)          # and one user-initiated call

    def test_work_split_accounts_for_every_call(self):
        """Spend the store labels oddly — or not at all — still has to appear.

        Older sessions recorded no initiator, and a breakdown naming only the
        kinds we know left that spend out while the total above it counted it.
        """
        import sqlite3 as sq

        path = Path(os.environ["COPILOT_HOME"]) / "session-store.db"
        conn = sq.connect(path)
        session = conn.execute(
            "SELECT session_id FROM assistant_usage_events LIMIT 1"
        ).fetchone()[0]
        conn.executemany(
            """INSERT INTO assistant_usage_events
               (session_id, turn_index, model, total_nano_aiu, initiator, agent_id)
               VALUES (?,0,'gpt-5.6-sol',7000000000,?,NULL)""",
            [(session, None), (session, "some-future-kind")],
        )
        conn.commit()
        conn.close()

        code, out = self._run("show", session)
        self.assertEqual(code, 0)
        split = out.split("How the work was done", 1)[1]
        self.assertIn("other", split.split("Models", 1)[0])

    def test_agents_without_initiator_columns(self):
        """Older stores lack the columns; say so rather than inventing zeros."""
        import sqlite3 as sq

        path = Path(os.environ["COPILOT_HOME"]) / "session-store.db"
        conn = sq.connect(path)
        conn.executescript(
            """
            CREATE TABLE tmp AS SELECT session_id, turn_index, model,
                   total_nano_aiu FROM assistant_usage_events;
            DROP TABLE assistant_usage_events;
            ALTER TABLE tmp RENAME TO assistant_usage_events;
            """
        )
        conn.commit()
        conn.close()
        self.assertEqual(self._run("agents")[0], 1)
        # Everything that does not need those columns still works, including
        # the reports that normally read the token detail.
        for args in (("show", "sess-alpha"), ("stats",), ("cost", "7"),
                     ("brief", "sess-alpha"), ("recent", "7")):
            with self.subTest(command=args[0]):
                self.assertEqual(self._run(*args)[0], 0)

    def test_skills_inventory_counts_qualified_references(self):
        """Configured versus referenced, without counting ordinary English."""
        import sqlite3 as sq

        skills = Path(os.environ["COPILOT_HOME"]) / "skills"
        skills.mkdir()
        # A directory skill carries its SKILL.md, the way every real one on
        # disk does. A bare directory is not loadable, and no longer counts.
        (skills / "deploy-check").mkdir()
        (skills / "deploy-check" / "SKILL.md").write_text("x")
        (skills / "commit.skill.md").write_text("x")
        (skills / "never-used.md").write_text("x")

        conn = sq.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        conn.execute(
            "INSERT INTO turns VALUES (20,'sess-alpha',9,?,?,'t')",
            ("run skills/deploy-check now", "I will commit the change"),
        )
        conn.commit()
        conn.close()

        code, out = self._run("skills")
        self.assertEqual(code, 0)
        self.assertIn("3 on disk", out)
        self.assertIn("deploy-check", out)
        # 'commit' appears in prose only, so it is not counted as a reference.
        self.assertIn("Never referenced", out)
        self.assertIn("never-used", out)

    def test_skills_inventory_finds_the_repository_skills(self):
        """A repo keeps skills in .github/skills, and they used to be invisible."""
        personal = Path(os.environ["COPILOT_HOME"]) / "skills"
        personal.mkdir()
        (personal / "mine.skill.md").write_text("x")

        project = Path(self._tmp.name) / "repo" / ".github" / "skills"
        (project / "apigee").mkdir(parents=True)
        (project / "apigee" / "SKILL.md").write_text("x")
        (project / "review.skill.md").write_text("x")

        here = os.getcwd()
        os.chdir(project.parent.parent)
        try:
            code, out = self._run("skills")
        finally:
            os.chdir(here)
        self.assertEqual(code, 0)
        self.assertIn("3 on disk", out)
        self.assertIn("2 from this repo", out)
        self.assertIn("apigee", out)
        self.assertIn("review", out)

    def test_skills_inventory_prefers_the_repository_copy(self):
        """Both scopes name it; the repo copy is the one Copilot loads."""
        personal = Path(os.environ["COPILOT_HOME"]) / "skills"
        personal.mkdir()
        (personal / "commit.skill.md").write_text("personal")

        project = Path(self._tmp.name) / "repo" / ".github" / "skills"
        project.mkdir(parents=True)
        (project / "commit.skill.md").write_text("project")

        here = os.getcwd()
        os.chdir(project.parent.parent)
        try:
            from cs import context
            found = {a.name: a for a in context.assets("skills")}
        finally:
            os.chdir(here)
        self.assertEqual(found["commit"].scope, "project")
        self.assertEqual(found["commit"].path.read_text(), "project")

    def test_skills_and_context_agree_on_the_project_count(self):
        """The two views walk one table, so they cannot disagree again."""
        (Path(os.environ["COPILOT_HOME"]) / "skills").mkdir()
        project = Path(self._tmp.name) / "repo" / ".github" / "skills"
        (project / "apigee").mkdir(parents=True)
        (project / "apigee" / "SKILL.md").write_text("x")

        here = os.getcwd()
        os.chdir(project.parent.parent)
        try:
            from cs import context
            assets = [a.scope for a in context.assets("skills")]
            audited = [i for i in context.audit()["items"]
                       if i.kind == "skills" and i.scope == "project"]
        finally:
            os.chdir(here)
        self.assertEqual(assets.count("project"), len(audited))

    # ── what Copilot would actually load ──────────────────────────────────
    #
    # The inventory used to be a directory listing. Copilot's own answer is
    # narrower and wider at once: it adds the skills an enabled plugin ships
    # and subtracts everything settings.json switches off. These pin both
    # halves, and the case that has neither a file nor an excuse — a skill
    # the CLI is recorded as having run that is on no shelf here.

    def _plugin(self, marketplace: str, pack: str, *names: str,
                enabled: bool = True) -> Path:
        """Install a plugin pack the way Copilot does, and set its switch."""
        import json

        home = Path(os.environ["COPILOT_HOME"])
        root = home / "installed-plugins" / marketplace / pack
        for name in names:
            (root / "skills" / name).mkdir(parents=True)
            (root / "skills" / name / "SKILL.md").write_text("x")
        settings = home / "settings.json"
        found = json.loads(settings.read_text()) if settings.exists() else {}
        found.setdefault("enabledPlugins", {})[f"{pack}@{marketplace}"] = enabled
        settings.write_text(json.dumps(found))
        return root

    def _settings(self, **keys) -> None:
        import json

        path = Path(os.environ["COPILOT_HOME"]) / "settings.json"
        found = json.loads(path.read_text()) if path.exists() else {}
        found.update(keys)
        path.write_text(json.dumps(found))

    def _loaded(self, name: str, session: str = "sess-plugin") -> None:
        """Record the CLI loading a skill, the way it marks a real turn."""
        conn = sqlite3.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        conn.execute(
            "INSERT INTO turns (session_id, turn_index, user_message, "
            "assistant_response, timestamp) VALUES (?,?,?,?,'t')",
            (session, 1, f'<skill-context name="{name}">body</skill-context>', "ok"),
        )
        conn.commit()
        conn.close()

    def test_an_enabled_plugin_ships_skills_and_a_switched_off_one_ships_none(self):
        """A pack you turned off ships skills Copilot will never load."""
        (Path(os.environ["COPILOT_HOME"]) / "skills").mkdir()
        self._plugin("market", "live", "alpha-tool", "beta-tool")
        self._plugin("market", "dead", "gamma-tool", enabled=False)

        code, out = self._run("skills")
        self.assertEqual(code, 0)
        self.assertIn("alpha-tool", out)
        self.assertIn("beta-tool", out)
        self.assertIn("from plugins", out)
        # Absent, not greyed: a skill from a disabled pack is not a skill
        # this machine has, and listing it invites you to reach for it.
        self.assertNotIn("gamma-tool", out)

    def test_the_context_view_counts_a_plugin_the_skills_view_counts(self):
        """One walk, so the two totals cannot drift apart again."""
        (Path(os.environ["COPILOT_HOME"]) / "skills").mkdir()
        self._plugin("market", "live", "alpha-tool", "beta-tool")

        from cs import context
        # Every scope, not just the plugin one. Checking a single scope let
        # the personal totals drift apart by a row: a recursive rule meant
        # for one root was being applied to the other.
        for scope in ("project", "personal", "plugin", "builtin"):
            audited = [i for i in context.audit()["items"]
                       if i.kind == "skills" and i.scope == scope]
            listed = [a for a in context.assets("skills") if a.scope == scope]
            self.assertEqual(len(audited), len(listed), scope)
        self.assertIn("2 skills", self._run("context")[1])

    def test_a_nested_stray_markdown_file_is_not_a_skill(self):
        """One skill's own pages are not forty more skills."""
        skills = Path(os.environ["COPILOT_HOME"]) / "skills" / "graphify"
        (skills / "graphify").mkdir(parents=True)
        (skills / "SKILL.md").write_text("x")
        (skills / "graphify" / "skill.md").write_text("a page, not a skill")

        from cs import context
        found = [a.name for a in context.assets("skills")]
        self.assertIn("graphify", found)
        self.assertNotIn("skill", found)

    def test_the_shared_agents_root_is_searched_too(self):
        """~/.agents/skills sits beside ~/.copilot and Copilot loads from it."""
        shared = Path(os.environ["CS_AGENTS_HOME"]) / "skills"
        (shared / "dt-obs-hosts").mkdir(parents=True)
        (shared / "dt-obs-hosts" / "SKILL.md").write_text("x")
        (Path(os.environ["COPILOT_HOME"]) / "skills").mkdir()
        self._loaded("dt-obs-hosts")

        code, out = self._run("skills")
        self.assertEqual(code, 0)
        self.assertIn("dt-obs-hosts", out)
        # The point of the root: it stops a skill that ran from being filed
        # as installed nowhere, which is what it was reported as.
        self.assertNotIn("not installed", out)

    def test_a_repository_keeps_skills_in_dot_agents_as_well(self):
        """The same root, the project half of it."""
        (Path(os.environ["COPILOT_HOME"]) / "skills").mkdir()
        project = Path(self._tmp.name) / "repo" / ".agents" / "skills" / "beads"
        project.mkdir(parents=True)
        (project / "SKILL.md").write_text("x")

        here = os.getcwd()
        os.chdir(project.parents[2])
        try:
            from cs import context
            found = {a.name: a for a in context.assets("skills")}
        finally:
            os.chdir(here)
        self.assertEqual(found["beads"].scope, "project")

    def test_only_the_newest_package_of_builtins_is_counted(self):
        """Copilot keeps every version it has downloaded; one of them runs."""
        pkg = Path(os.environ["COPILOT_HOME"]) / "pkg" / "darwin-arm64"
        for version in ("1.0.9", "1.0.10"):
            skill = pkg / version / "builtin-skills" / "github-pr-media"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(version)
        (pkg / "1.0.10" / "builtin-skills" / "newer-one").mkdir()
        (pkg / "1.0.10" / "builtin-skills" / "newer-one" / "SKILL.md").write_text("x")
        (Path(os.environ["COPILOT_HOME"]) / "skills").mkdir()

        from cs import context
        found = {a.name: a for a in context.assets("skills")}
        self.assertEqual(found["github-pr-media"].scope, "builtin")
        # 1.0.10 is newer than 1.0.9, which a string compare gets backwards.
        self.assertEqual(found["github-pr-media"].path.read_text(), "1.0.10")
        self.assertIn("newer-one", found)
        self.assertIn("built in", self._run("skills")[1])

    def test_standing_in_your_home_directory_does_not_rename_your_skills(self):
        """`.copilot/skills` is a project pattern; in $HOME it is the personal one."""
        skills = Path(os.environ["COPILOT_HOME"]) / "skills"
        (skills / "kept-one").mkdir(parents=True)
        (skills / "kept-one" / "SKILL.md").write_text("x")

        here = os.getcwd()
        # The Copilot home's parent is the directory that holds `.copilot`,
        # which is exactly where this goes wrong.
        os.chdir(Path(os.environ["COPILOT_HOME"]).parent)
        try:
            from cs import context
            found = {a.name: a for a in context.assets("skills")}
        finally:
            os.chdir(here)
        self.assertEqual(found["kept-one"].scope, "personal")

    def test_a_mirror_for_another_tool_is_not_counted_twice(self):
        """Plugins carry copies for other harnesses in dotted directories."""
        (Path(os.environ["COPILOT_HOME"]) / "skills").mkdir()
        root = self._plugin("market", "live", "alpha-tool")
        for mirror in (root / ".openclaw" / "skills" / "alpha-tool",
                       root / "skills" / ".archive" / "alpha-tool"):
            mirror.mkdir(parents=True)
            (mirror / "SKILL.md").write_text("x")

        from cs import context
        found = [a for a in context.assets("skills") if a.name == "alpha-tool"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].scope, "plugin")

    def test_your_own_copy_of_a_skill_beats_a_marketplace_one(self):
        """Precedence is plugin, then personal, then the repository."""
        personal = Path(os.environ["COPILOT_HOME"]) / "skills"
        (personal / "alpha-tool").mkdir(parents=True)
        (personal / "alpha-tool" / "SKILL.md").write_text("mine")
        self._plugin("market", "live", "alpha-tool")

        from cs import context
        found = {a.name: a for a in context.assets("skills")}
        self.assertEqual(found["alpha-tool"].scope, "personal")
        self.assertEqual(found["alpha-tool"].path.read_text(), "mine")

    def test_a_switched_off_skill_is_not_filed_as_a_neglected_one(self):
        """disabledSkills is a decision; 'never referenced' read as a charge."""
        skills = Path(os.environ["COPILOT_HOME"]) / "skills"
        for name in ("kept-one", "parked-one"):
            (skills / name).mkdir(parents=True)
            (skills / name / "SKILL.md").write_text("x")
        self._settings(disabledSkills=["parked-one"])

        code, out = self._run("skills")
        self.assertEqual(code, 0)
        self.assertIn("1 switched off in settings.json", out)
        self.assertIn("Switched off", out)
        idle, _, off = out.partition("Switched off")
        self.assertIn("kept-one", idle.partition("Never referenced")[2])
        self.assertIn("parked-one", off)
        # Still installed. Subtracting it would make the view disagree with
        # the directory, which is the one thing the reader can check.
        self.assertIn("2 on disk", out)

    def test_a_skill_that_ran_here_is_listed_even_with_no_file_for_it(self):
        """Proof of a load outranks an empty shelf."""
        (Path(os.environ["COPILOT_HOME"]) / "skills").mkdir()
        self._loaded("vanished-tool")

        code, out = self._run("skills")
        self.assertEqual(code, 0)
        self.assertIn("vanished-tool", out)
        self.assertIn("not installed", out)
        self.assertIn("1 not on this disk", out)
        # And it can be asked about, rather than refused for want of a file.
        code, drill = self._run("skills", "vanished-tool")
        self.assertEqual(code, 0)
        self.assertIn("not installed here", drill)

    def test_the_export_reports_what_the_screen_reports(self):
        """They built separate lists once, and disagreed by eleven rows."""
        import json as js

        skills = Path(os.environ["COPILOT_HOME"]) / "skills"
        (skills / "kept-one").mkdir(parents=True)
        (skills / "kept-one" / "SKILL.md").write_text("x")
        self._loaded("vanished-tool")

        screen = self._run("skills")[1]
        payload = js.loads(self._run("skills", "--json")[1])
        self.assertIn(f"installed  {payload['installed']}", screen)
        self.assertIn(f"{payload['referenced']} appear in at least one", screen)
        self.assertIn(f"{payload['not_installed']} not on this disk", screen)
        self.assertEqual(payload["not_installed"], 1)
        vanished = next(a for a in payload["assets"]
                        if a["name"] == "vanished-tool")
        self.assertEqual(vanished["scope"], "not installed")

    def test_a_skill_from_another_checkout_is_named_not_just_denied(self):
        """'Not installed' is true and useless; the question is where it is."""
        (Path(os.environ["COPILOT_HOME"]) / "skills").mkdir()
        elsewhere = Path(self._tmp.name) / "meeting-notes"
        (elsewhere / ".github" / "skills" / "roundup").mkdir(parents=True)
        (elsewhere / ".github" / "skills" / "roundup" / "SKILL.md").write_text("x")
        conn = sqlite3.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        conn.execute("UPDATE sessions SET cwd = ? WHERE id = (SELECT id FROM "
                     "sessions LIMIT 1)", (str(elsewhere),))
        conn.commit()
        conn.close()
        self._loaded("roundup")

        code, out = self._run("skills")
        self.assertEqual(code, 0)
        self.assertIn("in meeting-notes", out)
        self.assertIn("1 from another checkout", out)
        # The bare form is kept for the ones that really are nowhere.
        self.assertNotIn("· not installed", out)

    def test_a_skill_that_is_nowhere_at_all_says_so_plainly(self):
        """The checkout it ran in is gone, or was never this machine."""
        (Path(os.environ["COPILOT_HOME"]) / "skills").mkdir()
        self._loaded("vanished-tool")

        code, out = self._run("skills")
        self.assertEqual(code, 0)
        self.assertIn("not installed", out)
        self.assertIn("1 not on this disk", out)

    def test_a_checkout_with_many_names_is_called_by_its_directory(self):
        """One directory collects several repository labels over its life."""
        directory = Path(self._tmp.name) / "platform-tools"
        (directory / ".agents" / "skills" / "beads").mkdir(parents=True)
        (directory / ".agents" / "skills" / "beads" / "SKILL.md").write_text("x")
        conn = sqlite3.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        for repository in ("me/platform-tools", "someone-else/renamed"):
            conn.execute(
                "INSERT INTO sessions (id, cwd, repository, summary, created_at,"
                " updated_at) VALUES (?,?,?,'s','2026-01-01','2026-01-01')",
                (f"dir-{repository}", str(directory), repository),
            )
        conn.commit()
        conn.close()
        (Path(os.environ["COPILOT_HOME"]) / "skills").mkdir()
        self._loaded("beads")

        out = self._run("skills")[1]
        self.assertIn("in platform-tools", out)
        self.assertNotIn("renamed", out)

    def _session_at(self, session_id: str, directory: Path,
                    repository: str = "") -> None:
        """A session recorded as having been worked in one place."""
        conn = sqlite3.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        conn.execute(
            "INSERT INTO sessions (id, cwd, repository, summary, created_at,"
            " updated_at) VALUES (?,?,?,'s','2026-01-01','2026-01-01')",
            (session_id, str(directory), repository),
        )
        conn.commit()
        conn.close()

    def test_by_repo_says_where_each_skill_was_reached_for(self):
        """The other half of the inventory: not what is here, but what I used."""
        repo = Path(self._tmp.name) / "meeting-notes"
        (repo / ".github" / "skills" / "roundup").mkdir(parents=True)
        (repo / ".github" / "skills" / "roundup" / "SKILL.md").write_text("x")
        (Path(os.environ["COPILOT_HOME"]) / "skills").mkdir()
        self._session_at("place-one", repo, "me/meeting-notes")
        self._loaded("roundup", session="place-one")

        code, out = self._run("skills", "--by-repo")
        self.assertEqual(code, 0)
        self.assertIn("me/meeting-notes", out)
        self.assertIn("roundup", out)
        # It ships the skill it used, which is the state worth naming.
        self.assertIn("all its own", out)

    def test_by_repo_marks_a_skill_the_checkout_does_not_ship(self):
        """It worked because of what you had installed, not what it carries."""
        skills = Path(os.environ["COPILOT_HOME"]) / "skills" / "commit"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("x")
        repo = Path(self._tmp.name) / "borrower"
        repo.mkdir()
        self._session_at("place-two", repo, "me/borrower")
        self._loaded("commit", session="place-two")

        out = self._run("skills", "--by-repo")[1]
        self.assertIn("none of its own", out)
        self.assertIn("commit 1*", out)

    def test_by_repo_keeps_one_repository_in_one_row(self):
        """Working in a subfolder used to invent a second, skill-less repo."""
        repo = Path(self._tmp.name) / "split"
        (repo / ".github" / "skills" / "roundup").mkdir(parents=True)
        (repo / ".github" / "skills" / "roundup" / "SKILL.md").write_text("x")
        (repo / "docs").mkdir()
        (Path(os.environ["COPILOT_HOME"]) / "skills").mkdir()
        self._session_at("at-root", repo, "me/split")
        self._session_at("at-sub", repo / "docs", "me/split")
        self._loaded("roundup", session="at-root")
        self._loaded("roundup", session="at-sub")

        out = self._run("skills", "--by-repo")[1]
        self.assertEqual(out.count("me/split"), 1)
        self.assertIn("2 ", out.split("me/split")[0][-6:])   # both sessions
        # And the subfolder does not drag the row down to 'none of its own':
        # what a repository ships, it ships wherever inside it you stood.
        self.assertIn("all its own", out)
        self.assertNotIn("none of its own", out)

    def test_your_own_kit_is_not_something_a_directory_ships(self):
        """In $HOME, `.copilot/skills` matches a project pattern."""
        skills = Path(os.environ["COPILOT_HOME"]) / "skills" / "commit"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("x")

        from cs import context
        self.assertEqual(context.ships(Path(os.environ["COPILOT_HOME"]).parent),
                         set())

    def test_by_repo_is_refused_for_agents_which_leave_no_marker(self):
        """Nothing to group, so say so rather than draw an empty page."""
        code, err = self._run_err("profiles", "--by-repo")
        self.assertEqual(code, 1)
        self.assertIn("only skills leave", err)

    def test_by_repo_refuses_what_it_cannot_also_do(self):
        """A flag dropped on the floor answers a question nobody asked."""
        (Path(os.environ["COPILOT_HOME"]) / "skills").mkdir()
        for args, expected in (
            (("skills", "commit", "--by-repo"), "one or the other"),
            (("skills", "--by-repo", "--sort", "name"), "does not apply"),
            (("skills", "--by-repo", "--desc"), "does not apply"),
            (("profiles", "--by-repo"), "only skills leave"),
        ):
            with self.subTest(args=args):
                code, err = self._run_err(*args)
                self.assertEqual(code, 1)
                self.assertIn(expected, err)

    def test_by_repo_can_be_taken_as_data(self):
        """The view's whole point is a number you can put in a spreadsheet."""
        import json as js

        repo = Path(self._tmp.name) / "meeting-notes"
        (repo / ".github" / "skills" / "roundup").mkdir(parents=True)
        (repo / ".github" / "skills" / "roundup" / "SKILL.md").write_text("x")
        (Path(os.environ["COPILOT_HOME"]) / "skills" / "commit").mkdir(parents=True)
        (Path(os.environ["COPILOT_HOME"]) / "skills" / "commit" / "SKILL.md").write_text("x")
        self._session_at("data-one", repo, "me/meeting-notes")
        self._loaded("roundup", session="data-one")
        self._loaded("commit", session="data-one")

        payload = js.loads(self._run("skills", "--by-repo", "--json")[1])
        self.assertEqual(payload["view"], "skills-by-repo")
        self.assertEqual(payload["borrowed"], 1)
        rows = {row["skill"]: row for row in payload["usage"]}
        self.assertTrue(rows["roundup"]["shipped_here"])
        self.assertFalse(rows["commit"]["shipped_here"])
        self.assertEqual(rows["roundup"]["checkout"], "me/meeting-notes")

        # And flat, so a cell never holds a list of objects.
        head, first, *_ = self._run("skills", "--by-repo", "--csv")[1].splitlines()
        self.assertEqual(head, "checkout,checkout_sessions,skill,sessions,shipped_here")
        self.assertNotIn("[", first)

    def test_profiles_by_repo_is_refused_as_data_too(self):
        """The screen refuses it; `--json` must not answer a different way."""
        code, err = self._run_err("profiles", "--by-repo", "--json")
        self.assertEqual(code, 1)
        self.assertIn("only skills leave", err)

    def test_the_no_skills_advice_ignores_the_ones_copilot_ships(self):
        """Built-ins are not kit you chose, and counting them silenced it."""
        pkg = Path(os.environ["COPILOT_HOME"]) / "pkg" / "darwin-arm64" / "1.0.1"
        (pkg / "builtin-skills" / "github-pr-media").mkdir(parents=True)
        (pkg / "builtin-skills" / "github-pr-media" / "SKILL.md").write_text("x")

        from cs import context
        found = context.audit()
        self.assertTrue(any(item.kind == "skills" for item in found["items"]))
        self.assertIn("No skills anywhere",
                      [what for _severity, what, _fix in context.gaps(found)])

        # And it goes quiet as soon as one is actually yours.
        skills = Path(os.environ["COPILOT_HOME"]) / "skills" / "commit"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("x")
        self.assertNotIn("No skills anywhere",
                         [what for _s, what, _f in context.gaps(context.audit())])

    def test_a_directory_it_cannot_read_does_not_take_the_view_down(self):
        """These walks open every directory a session has ever run in."""
        if os.geteuid() == 0:
            self.skipTest("root can stat anything")
        locked = Path(self._tmp.name) / "locked"
        (locked / "inner").mkdir(parents=True)
        (Path(os.environ["COPILOT_HOME"]) / "skills" / "commit").mkdir(parents=True)
        (Path(os.environ["COPILOT_HOME"]) / "skills" / "commit" / "SKILL.md").write_text("x")
        self._session_at("walled", locked / "inner", "me/walled")
        self._loaded("vanished-tool", session="walled")

        # Restored before tearDown rather than after it: the temporary
        # directory cannot be removed while one of its children is unreadable.
        locked.chmod(0o000)
        try:
            for args in (("skills",), ("skills", "--by-repo"), ("context",)):
                with self.subTest(args=args):
                    code, out = self._run(*args)
                    self.assertEqual(code, 0)
                    self.assertNotIn("Traceback", out)
        finally:
            locked.chmod(0o755)

    def test_settings_that_will_not_parse_disable_nothing_and_say_so(self):
        """A config file it cannot read must not silently hide your kit."""
        skills = Path(os.environ["COPILOT_HOME"]) / "skills"
        (skills / "kept-one").mkdir(parents=True)
        (skills / "kept-one" / "SKILL.md").write_text("x")
        (Path(os.environ["COPILOT_HOME"]) / "settings.json").write_text("{ not json")

        code, out = self._run("skills")
        self.assertEqual(code, 0)
        self.assertIn("kept-one", out)
        self.assertIn("could not be read", out)
        self.assertNotIn("switched off in settings.json", out)

    def test_a_comment_in_settings_does_not_lose_the_disabled_list(self):
        """Copilot's sibling config.json opens with a // comment."""
        skills = Path(os.environ["COPILOT_HOME"]) / "skills"
        for name in ("kept-one", "parked-one"):
            (skills / name).mkdir(parents=True)
            (skills / name / "SKILL.md").write_text("x")
        (Path(os.environ["COPILOT_HOME"]) / "settings.json").write_text(
            '// managed automatically\n{"disabledSkills": ["parked-one"]}'
        )

        code, out = self._run("skills")
        self.assertEqual(code, 0)
        self.assertIn("1 switched off in settings.json", out)
        self.assertNotIn("could not be read", out)

    def test_reference_matching_is_precision_first(self):
        """Skills are named `commit` and `status`; only strong evidence counts."""
        import sqlite3 as sq

        from cs import db

        conn = sq.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        conn.executemany(
            "INSERT INTO turns (session_id, turn_index, user_message, "
            "assistant_response, timestamp) VALUES (?,?,?,?,'t')",
            [
                # Strong: the asset's own path, its filename, the word beside it.
                ("sess-alpha", 30, "run skills/deploy-check", "done"),
                ("sess-alpha", 31, "see commit.skill.md", "ok"),
                ("sess-alpha", 32, "use the handover skill", "ok"),
                # Weak: git prose, code spans and repo paths are NOT references.
                ("sess-empty", 33, "please commit this", "`git add`/`commit` then push"),
                ("sess-empty", 34, "check `status()`", "repo acmeco/mule-http"),
                ("sess-empty", 35, "read .github/workflows/ci.yml", "`oracle-osb` differs"),
                # Nor is a longer asset that merely starts with a known name:
                # the alternation stopped at a word boundary and counted these.
                ("sess-empty", 36, "see agents/mule-triage.agent.md", "ok"),
                ("sess-empty", 37, "see agents/handover-deckforge", "ok"),
            ],
        )
        conn.commit()
        conn.close()

        conn = db.connect()
        counts = db.reference_counts(
            conn, ["deploy-check", "commit", "handover", "status", "mule", "oracle",
                  "mule-triage"]
        )
        conn.close()
        self.assertEqual(counts["deploy-check"], 1)   # skills/deploy-check
        self.assertEqual(counts["commit"], 1)         # commit.skill.md, not git prose
        self.assertEqual(counts["handover"], 1)       # "the handover skill"
        self.assertEqual(counts["status"], 0)         # `status()` is code
        self.assertEqual(counts["mule"], 0)           # acmeco/mule-http is a repo
        self.assertEqual(counts["oracle"], 0)         # `oracle-osb` is a product
        # agents/mule-triage is its own asset, and so is handover-deckforge.
        self.assertEqual(counts["mule-triage"], 1)
        self.assertEqual(counts["handover"], 1, "handover-deckforge counted as handover")

    def test_per_session_assets_are_listed(self):
        """The session views name the skills and agents that session used."""
        import sqlite3 as sq

        skills = Path(os.environ["COPILOT_HOME"]) / "skills"
        skills.mkdir(exist_ok=True)
        (skills / "deploy-check.skill.md").write_text("x")
        agents = Path(os.environ["COPILOT_HOME"]) / "agents"
        agents.mkdir(exist_ok=True)
        (agents / "reviewer.agent.md").write_text("x")

        conn = sq.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        conn.execute(
            "INSERT INTO turns (session_id, turn_index, user_message, "
            "assistant_response, timestamp) VALUES ('sess-alpha',40,?,?,'t')",
            ("run skills/deploy-check", "handing to agents/reviewer"),
        )
        conn.commit()
        conn.close()

        # What a session reached for is inventory, so show owns it outright.
        _, out = self._run("show", "sess-alpha")
        self.assertIn("Skills & agents", out)
        self.assertIn("deploy-check", out)
        self.assertIn("reviewer", out)
        # Nothing loaded a skill here, so both are claims about text and
        # both must arrive with the words they were read from.
        self.assertIn("named", out)
        self.assertIn("run skills/deploy-check", out)
        self.assertNotIn("Skills & agents", self._run("brief", "sess-alpha")[1])

    def test_delegation_counts_tasks_not_agent_identities(self):
        """agent_id is a tool-call id, so it counts delegations, not agents."""
        _, out = self._run("agents", "7")
        self.assertIn("handed to sub-agents", out)
        self.assertNotIn("distinct agents", out)
        self.assertIn("records no agent name", out)

    def test_spend_views_keep_their_columns(self):
        """A number gaining a digit used to shove everything after it out of line."""

        import cs.cli as cli

        def plain(text: str) -> str:
            return re.sub(r"\x1b\[[0-9;]*m", "", text)

        header = cli._spend_header("model", f"{'calls':>9}{'avg':>7}{'first':>7}")
        # The bar arrives already the width of its column — a coloured bar is
        # mostly escape characters, so the row cannot pad it and does not try.
        rows = [
            cli._spend_row("116.7k", "claude-opus-5", cli._bar(9, 10, 18),
                           f"{10851:>9,}{'11.5s':>7}{'4.3s':>7}"),
            cli._spend_row("47.0", "gemini-3.6-flash", cli._bar(0, 10, 18),
                           f"{34:>9,}{'3.5s':>7}{'1.6s':>7}"),
        ]
        widths = {len(plain(line).rstrip()) for line in [header, *rows]}
        self.assertEqual(len(widths), 1, f"ragged spend columns: {widths}")

        _, out = self._run("stats", "7")
        fields = [line for line in plain(out).split("\n") if re.match(r"^  [a-z]", line)]
        self.assertTrue(fields)
        for line in fields:
            self.assertEqual(line[10], " ", f"label crowds its value: {line!r}")
            self.assertNotEqual(line[11], " ", f"value starts late: {line!r}")

    def test_hints_shrink_instead_of_being_cut_off(self):
        """A fixed hint string was truncated, hiding 't transcript' and 'q'."""
        import cs.cli as cli

        for width in range(10, 130):
            line = cli._hint_line(width, mouse=True)
            self.assertLessEqual(ui_cells(line), max(width, len("q quit")))
            self.assertIn("q quit", line, f"no way out at {width} columns")
        self.assertIn("t transcript", cli._hint_line(140, mouse=False))
        self.assertIn("q home", cli._hint_line(140, mouse=False, back="home"))

    def test_home_menu_is_wired_up(self):
        """Every landing-screen entry names itself and has something to run."""
        import cs.cli as cli
        from cs import ui

        items = cli._home_items()
        self.assertGreater(len(items), 5)
        for icon, label, description, action, asks in items:
            self.assertTrue(label and description, f"unlabelled entry: {label!r}")
            self.assertTrue(callable(action), f"{label} has nothing to run")
            self.assertIn(
                asks, ("", "term", "period", "theme"),
                f"{label}: odd ask {asks!r}",
            )
            # Two cells, so the label column starts in the same place on
            # every row; anything wider shoves that row's text sideways.
            self.assertEqual(ui.cells(icon), 2, f"{label}: icon is not 2 cells")
        self.assertEqual(len({item[0] for item in items}), len(items),
                         "two entries share an icon")
        self.assertEqual([item[1] for item in items if item[4] == "term"],
                         ["Search"], "only search asks for text")
        self.assertEqual([item[1] for item in items if item[4] == "theme"],
                         ["Theme"], "only Theme changes appearance")
        self.assertTrue([item for item in items if item[4] == "period"],
                        "nothing offers a window to count over")
        code, out = self._run("home")
        self.assertEqual(code, 0)
        self.assertIn("Sessions", out)

    def test_every_menu_entry_can_actually_be_opened(self):
        """Choosing a row ran its action blind — one of them was not callable."""
        from unittest import mock

        import cs.cli as cli

        items = cli._home_items()
        picks: list = [*range(len(items)), (2, "portal"), None]

        def fake_wrapper(view, *_args):
            # The menu hands back a choice; a listing hands back 'user quit'.
            return picks.pop(0) if view is cli._home_tui else None

        out = _Tty()
        with mock.patch.object(cli, "_curses_wrapper", fake_wrapper), \
                mock.patch.object(cli, "_page", lambda _text, _sort=None: True), \
                mock.patch.object(cli, "_pause", lambda _message: True), \
                mock.patch.object(sys, "stdin", mock.Mock(isatty=lambda: True)), \
                redirect_stdout(out):
            cli.cmd_home()
        self.assertEqual(picks, [], "the menu stopped before trying every entry")
        self.assertFalse(cli._HOME_ACTIVE, "the menu flag outlived the menu")
        # Files printed its report straight past the menu instead of showing it
        # in the reader, so it alone dropped out of the UI and could not go back.
        self.assertEqual(out.getvalue(), "", "a menu entry printed outside the UI")

    def test_esc_gets_you_back_and_q_still_quits(self):
        """Esc typed into input() was just a character, so it did nothing."""
        from unittest import mock

        import cs.cli as cli

        for key, expected in [("\x1b", True), ("\r", True), (" ", True),
                              ("q", False), ("Q", False), ("", False)]:
            with redirect_stdout(io.StringIO()), \
                    mock.patch.object(cli, "_read_key", return_value=key):
                self.assertIs(cli._pause("x "), expected, f"key {key!r}")

    def test_the_way_out_of_the_pager_is_always_on_screen(self):
        """less cannot treat Esc as back, so from the menu we read in place."""
        from unittest import mock

        import cs.cli as cli

        long_text = "line\n" * 500
        with mock.patch.object(cli, "_HOME_ACTIVE", True), \
                mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch.object(cli, "_read_in_place", return_value=True) as reader, \
                mock.patch.object(cli.subprocess, "run") as run:
            self.assertTrue(cli._page(long_text))
        self.assertTrue(reader.called, "the menu still shelled out to a pager")
        self.assertFalse(run.called, "less was run even though Esc must mean back")

        # Away from the menu the user's own pager is still the right answer.
        with mock.patch.object(cli, "_HOME_ACTIVE", False), \
                mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch.object(cli, "_less_wheel_lines", return_value=False), \
                mock.patch.object(cli.subprocess, "run") as run:
            self.assertTrue(cli._page(long_text))
        self.assertTrue(run.called, "the pager was skipped outside the menu")

        # A reader that cannot start must fall back, never swallow the report.
        with mock.patch.object(cli, "_HOME_ACTIVE", True), \
                mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch.object(cli, "_read_in_place", return_value=False), \
                mock.patch.object(cli, "_less_wheel_lines", return_value=False), \
                mock.patch.object(cli.subprocess, "run") as run:
            cli._page(long_text)
        self.assertTrue(run.called, "no reader and no pager loses the report")

        # A report that fits still has to stay in the UI: printing it drops the
        # user out of the menu with no way back.
        with mock.patch.object(cli, "_HOME_ACTIVE", True), \
                mock.patch.object(cli, "_read_in_place", return_value=True) as reader, \
                redirect_stdout(_Tty()) as printed:
            self.assertTrue(cli._page("short\n"))
        self.assertTrue(reader.called, "a short report escaped the menu")
        self.assertEqual(printed.getvalue(), "")

    def test_report_colour_survives_the_move_into_curses(self):
        """The reports are ANSI strings; curses needs them as attribute runs."""
        from cs import ui

        palette = {"1": 1 << 8, "2": 1 << 9, "36": 1 << 10}
        esc = "\x1b"
        runs = ui.sgr_runs(f"{esc}[1mBold{esc}[0m plain{esc}[36mcyan", palette)
        self.assertEqual([text for text, _ in runs], ["Bold", " plain", "cyan"])
        self.assertEqual(runs[0][1], 1 << 8)
        self.assertEqual(runs[1][1], 0, "reset did not clear the attribute")
        self.assertEqual(runs[2][1], 1 << 10)
        self.assertEqual(ui.sgr_runs("no codes here", palette), [("no codes here", 0)])
        self.assertEqual(ui.sgr_runs(f"{esc}[38;5;244mgrey", {"38;5;244": 7}), [("grey", 7)])

    def test_the_readers_way_out_survives_a_narrow_window(self):
        import cs.cli as cli
        from cs import ui

        hints = [("↑/↓ scroll", "↑↓", 3), ("space page", "space", 2),
                 ("Esc back", "Esc", 0), ("q quits", "q", 0)]
        for width in range(6, 60):
            line = cli._fit_hints(list(hints), width)
            self.assertIn("Esc", line, f"no way back at width {width}")
            self.assertIn("q", line)
            if width >= ui.cells("Esc · q"):
                self.assertLessEqual(ui.cells(line), width)

    def test_a_listing_with_nothing_in_it_is_not_wiped(self):
        """Returning True meant 'I waited', so an empty result flashed and went."""
        from unittest import mock

        import cs.cli as cli

        with redirect_stdout(io.StringIO()) as buf:
            waited = cli._interactive_listing([], "Search · nothing · 0 sessions",
                                              show_all=True)
        self.assertFalse(waited, "an empty listing never waited for the user")
        self.assertIn("0 sessions", buf.getvalue())

        # The commands must pass that answer up, or the menu still redraws over it.
        with redirect_stdout(io.StringIO()), \
                mock.patch.object(cli, "_interactive_listing", return_value=False), \
                mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("sys.stdout.isatty", return_value=True):
            self.assertFalse(cli.cmd_search("portal"))
            self.assertFalse(cli.cmd_recent(3650))
            self.assertFalse(cli.cmd_files("globe.js"))

    def test_a_session_that_recorded_nothing_is_not_listed_at_all(self):
        """A fifth of `cs all` was sessions the CLI opened and closed without
        a single exchange — no turns, no summary, no credits, and nothing in
        any other table. There is nothing to read, brief or review, so they
        only padded the view out.

        All three conditions are required, not turns alone: a zero-turn
        session that was given a name, or that spent credits, recorded
        something and stays. Dropping a row never hides a session from
        `resume` — ids resolve against the store, not the listing.
        """
        import sqlite3 as sq

        import cs.cli as cli

        conn = sq.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        # Blank on every axis — the one that goes.
        conn.execute(
            "INSERT INTO sessions VALUES ('sess-unused','/tmp/c',NULL,'local',NULL,"
            "NULL, datetime('now'), datetime('now'))"
        )
        # No summary, but it has a turn: genuinely untitled, and it stays.
        conn.execute(
            "INSERT INTO sessions VALUES ('sess-noname','/tmp/d',NULL,'local',NULL,"
            "NULL, datetime('now'), datetime('now'))"
        )
        conn.execute("INSERT INTO turns VALUES (900,'sess-noname',0,'do a thing','ok','z')")
        conn.commit()
        conn.close()

        blank = ("sess-unused", "2026-01-01 00:00", "", "", "/tmp/c", 0, 0)
        self.assertTrue(cli._never_used(blank))
        self.assertFalse(cli._never_used(blank[:5] + (1, 0)), "a turn is content")
        self.assertFalse(cli._never_used(blank[:5] + (0, 42)), "credits are content")
        self.assertFalse(
            cli._never_used(("x", "t", "Named", "", "", 0, 0)), "a name is content"
        )

        for command in ("all", "recent"):
            with self.subTest(command=command):
                code, out = self._run(command, "3650")
                self.assertEqual(code, 0)
                self.assertNotIn("(never used)", out)
                self.assertNotIn("sess-unused", out)

        # The zero-turn session that has a name is still carried by `cs all`.
        _, all_out = self._run("all", "3650")
        self.assertIn("Empty session", all_out)
        self.assertIn("(untitled)", all_out)          # sess-noname, which has a turn
        # …and the header counts what it actually shows.
        import re as _re
        shown = len(_re.findall(r"^\s+\d+\s+\d\d:\d\d\s", all_out, _re.M))
        self.assertIn(f"{shown} total", all_out)

        # Dropping it from the listing does not hide it from resume: the id
        # still resolves, which is what `cs resume <id>` depends on.
        self.assertEqual(cli._resolve_ref("sess-unused"), "sess-unused")
        code, _ = self._run("show", "sess-unused")
        self.assertEqual(code, 0)

    def test_a_session_still_running_is_listed_before_its_first_turn(self):
        """The session you open in the next window is the one cs hid.

        Copilot records usage events as they are billed but only writes a
        `turns` row once an exchange has been persisted, so a live session
        sits at zero turns with real spend against it. The zero-turn rule
        dropped it from every listing but `cs all`, while the landing strip
        — which counts on all three axes in SQL — had already counted it.
        """
        import sqlite3 as sq

        import cs.cli as cli

        # The rule itself: credits are activity, a bare name is not.
        live = ("sess-live", "2026-01-01 00:00", "Live right now", "",
                "/tmp/e", 0, 1654336800)
        self.assertFalse(cli._is_hidden(live[2], live[5], live[6], []))
        self.assertFalse(cli._never_used(live))
        self.assertTrue(cli._is_hidden("", 0, 0, []), "an abandoned launch")
        self.assertTrue(cli._is_hidden("Named", 0, 0, []), "a name is not work")
        # The ignore file still applies to a session that is running, and a
        # summary the store never wrote is not an AttributeError.
        self.assertTrue(cli._is_hidden("Run the pipeline", 0, 99,
                                       ["Run the pipeline"]))
        self.assertFalse(cli._is_hidden(None, 3, 0, ["Run the pipeline"]))

        conn = sq.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        conn.execute(
            "INSERT INTO sessions VALUES ('sess-live','/tmp/e','acme/portal',"
            "'local','main','Live right now', datetime('now'), datetime('now'))"
        )
        conn.execute(
            "INSERT INTO assistant_usage_events (session_id, turn_index, model,"
            " total_nano_aiu) VALUES ('sess-live', 0, 'gpt-5', 1654336800)"
        )
        conn.commit()
        conn.close()

        for command in ("recent", "all"):
            with self.subTest(command=command):
                code, out = self._run(command, "1")
                self.assertEqual(code, 0)
                self.assertIn("Live right now", out)

    def test_the_session_count_agrees_with_the_sessions_you_can_list(self):
        """The landing strip said 963 sessions and `cs all` listed 955.

        Both numbers were right about their own question and neither said
        which it was answering: `db.stats` counted rows, the listing counted
        sessions that recorded something. The count is the first thing on the
        screen and `All sessions` is the row directly under it, so the two
        disagreeing made the header look broken.

        This is the guard on the *pair*, not on either number: the rule now
        exists twice — as SQL in `db.stats` and as `_never_used` over a
        listing row — and the only thing that keeps a change to one from
        quietly desyncing the other is a test that runs both.
        """
        from cs import cli, db

        conn = db.connect()
        try:
            counted = db.stats(conn)["total"]
            listed = cli._visible(db.recent_sessions(conn, 0), show_all=True)
        finally:
            conn.close()
        self.assertEqual(counted, len(listed))
        self.assertTrue(counted, "the fixture store should not be empty")

    def test_all_sessions_means_all_of_time_not_the_last_week(self):
        """`cs all` showed 173 of 858 sessions. "All" referred to the kinds
        of session it included, not the period, and it still carried the
        seven-day default it shares with `cs recent` — so the oldest years
        of the store were simply unreachable from the menu.
        """
        import sqlite3 as sq

        from cs import db

        conn = sq.connect(Path(os.environ["COPILOT_HOME"]) / "session-store.db")
        conn.execute(
            "INSERT INTO sessions VALUES ('sess-ancient','/tmp/old','acme/old','local',NULL,"
            "'Ancient work', datetime('now','-400 days'), datetime('now','-400 days'))"
        )
        conn.execute(
            "INSERT INTO turns VALUES (901,'sess-ancient',0,'long ago','done','w')"
        )
        conn.commit()
        conn.close()

        code, out = self._run("all")
        self.assertEqual(code, 0)
        self.assertIn("Ancient work", out)
        self.assertIn("all time", out)

        # An explicit window still windows, and still refuses nonsense.
        _, windowed = self._run("all", "7")
        self.assertNotIn("Ancient work", windowed)
        self.assertIn("last 7 days", windowed)
        self.assertEqual(self._run("all", "-3")[0], 1)

        # `cs recent` keeps its seven-day default — only `all` changed.
        _, recent = self._run("recent")
        self.assertIn("last 7 days", recent)
        self.assertNotIn("Ancient work", recent)

        # Every row is carried to the renderer; the screen scrolls, the
        # query does not paginate.
        conn = db.connect()
        self.assertEqual(
            len(db.recent_sessions(conn, 0)),
            conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
        )
        conn.close()

    def test_repos(self):
        code, out = self._run("repos")
        self.assertEqual(code, 0)
        self.assertIn("acme/portal", out)


        code, out = self._run("repos")
        self.assertEqual(code, 0)
        self.assertIn("acme/portal", out)

    def test_project_tag_shown(self):
        # sess-alpha has repo acme/portal → tag '#portal' in the listing.
        code, out = self._run("recent", "7")
        self.assertEqual(code, 0)
        self.assertIn("#portal", out)

    def test_timeline(self):
        code, out = self._run("timeline", "7")
        self.assertEqual(code, 0)

    def test_show_by_id(self):
        code, out = self._run("show", "sess-alpha")
        self.assertEqual(code, 0)
        self.assertIn("First request", out)
        self.assertIn("make a portal", out)

    def test_credits_shown(self):
        # sess-alpha has 1.5 + 2.5 = 4.00 AIU
        _, listing = self._run("recent", "7")
        self.assertIn("Credits", listing)
        self.assertIn("4.00", listing)
        _, show = self._run("show", "sess-alpha")
        self.assertIn("AIU", show)
        self.assertIn("claude-opus-4.8", show)
        _, stats = self._run("stats")
        self.assertIn("credits", stats)
        self.assertIn("4.00 AIU", stats)

    def test_recent_sorts_by_credits(self):
        base = Path(self._tmp.name)
        conn = sqlite3.connect(base / "session-store.db")
        conn.execute(
            "INSERT INTO sessions VALUES ('sess-beta','/tmp/b','acme/other','local','main',"
            "'Lower spend', datetime('now'), datetime('now'))"
        )
        conn.execute(
            "INSERT INTO turns VALUES (3,'sess-beta',0,'one turn','done','z')"
        )
        conn.execute(
            "INSERT INTO assistant_usage_events "
            "(session_id, turn_index, model, total_nano_aiu) "
            "VALUES ('sess-beta',0,'gemini-3.6-flash',500000000)"
        )
        conn.commit()
        conn.close()

        _, out = self._run("recent", "7", "--sort", "credits")
        self.assertIn("Credits↓", out)
        self.assertLess(out.index("Build Three.js portal"), out.index("Lower spend"))
        _, ascending = self._run("recent", "7", "--sort", "credits", "--asc")
        self.assertLess(ascending.index("Lower spend"), ascending.index("Build Three.js portal"))

    def test_row_numbers_stay_with_their_session(self):
        """#N identifies a session, so sorting must not repoint it."""
        import curses

        from cs.cli import _index_file, _listing_tui


        # Listed newest-first, so #1 is newest — but #3 holds the most credits.
        rows = [
            ("id-new", "2026-08-01T12:00", "Newest", "r/a", "/tmp", 1, 50_000_000_000),
            ("id-mid", "2026-08-01T11:00", "Middle", "r/b", "/tmp", 2, 5_000_000_000),
            ("id-old", "2026-08-01T10:00", "Oldest", "r/c", "/tmp", 3, 900_000_000_000),
        ]
        # Right alone sorts — no Enter needed, so Enter can act on the row.
        keys = [curses.KEY_RIGHT, curses.KEY_RIGHT, ord("q")]
        screen = Screen(keys)
        _listing_tui(screen, rows, "Sessions")

        final = screen.frames[-1]
        at = _column_x(final, "Summary")
        shown = [(final[(y, 0)].strip(), final[(y, at)].strip()) for y in (5, 6, 7)]
        # Highest credits now sits on the top row but keeps its number.
        self.assertEqual(shown, [("3", "Oldest"), ("1", "Newest"), ("2", "Middle")])

        saved = dict(
            line.split("=", 1) for line in _index_file().read_text().splitlines()
        )
        self.assertEqual(saved, {"1": "id-new", "2": "id-mid", "3": "id-old"})

    def test_an_open_listing_picks_up_a_session_started_beside_it(self):
        """The view read the store once and never again.

        A session started in another window arrived only when the listing
        was next reopened — which, on a screen you sit and watch with a
        refresh running, reads as the listing simply being wrong.
        """
        from unittest.mock import patch

        from cs import cli
        from cs.cli import _index_file, _listing_tui

        rows = [
            ("id-new", "2026-08-01T12:00", "Newest", "r/a", "/tmp", 1, 50_000_000_000),
            ("id-mid", "2026-08-01T11:00", "Middle", "r/b", "/tmp", 2, 5_000_000_000),
        ]
        # What the next read finds: a session that did not exist when the
        # view opened, newest and therefore first in natural order.
        arrived = ("id-live", "2026-08-01T13:00", "Started beside it", "r/c",
                   "/tmp", 0, 1_600_000_000)
        def reload():
            return [arrived, *rows], "Sessions · 3 total"

        # -1 is the heartbeat curses reports when nothing was typed.
        screen = Screen([-1, ord("q")])
        with patch.object(cli, "_REFRESH_SECONDS", 0):
            _listing_tui(screen, rows, "Sessions · 2 total", reload=reload)

        final = screen.frames[-1]
        at = _column_x(final, "Summary")
        summaries = [final[(y, at)].strip() for y in (5, 6, 7)]
        self.assertIn("Started beside it", summaries)
        self.assertIn("Sessions · 3 total", final[(0, 0)])

        # #N identifies a session: the two that were already listed keep the
        # numbers they were read under, and the newcomer takes the next free
        # one rather than becoming #1 and shifting them.
        saved = dict(
            line.split("=", 1) for line in _index_file().read_text().splitlines()
        )
        self.assertEqual(saved, {"1": "id-new", "2": "id-mid", "3": "id-live"})
        numbered = {final[(y, at)].strip(): final[(y, 0)].strip() for y in (5, 6, 7)}
        self.assertEqual(numbered["Started beside it"], "3")
        self.assertEqual(numbered["Newest"], "1")

    def test_a_quiet_heartbeat_changes_nothing_and_a_broken_one_is_survived(self):
        """A refresh that reads the same store, or cannot read it at all,
        must leave the rows already on screen exactly as they are."""
        import sqlite3

        from cs import cli

        rows = [("id-a", "2026-08-01T12:00", "A", "r/a", "/tmp", 1, 5_000_000_000)]
        numbers = {"id-a": 1}
        state: dict = {}

        unchanged, title, kept, follow = cli._reread_listing(
            lambda: (list(rows), "same"), rows, "same", numbers, state, "id-a"
        )
        self.assertEqual(unchanged, rows)
        self.assertEqual(kept, numbers)
        self.assertIsNone(follow, "nothing moved, so nothing to follow")
        self.assertEqual(state, {}, "an unchanged store is not a redraw")

        def broken():
            raise sqlite3.OperationalError("database is locked")

        survived = cli._reread_listing(broken, rows, "same", numbers, state, "id-a")
        self.assertEqual(survived, (rows, "same", numbers, None))

    def test_a_refreshed_listing_keeps_the_cursor_on_its_own_session(self):
        """A new arrival must not pull the highlight onto itself."""
        from cs import cli

        rows = [("id-a", "2026-08-01T12:00", "A", "r/a", "/tmp", 1, 5_000_000_000)]
        arrived = ("id-b", "2026-08-01T13:00", "B", "r/b", "/tmp", 1, 9_000_000_000)
        state: dict = {}
        fresh, title, numbers, follow = cli._reread_listing(
            lambda: ([arrived, *rows], "Sessions · 2 total"),
            rows, "Sessions · 1 total", {"id-a": 1}, state, "id-a",
        )
        self.assertEqual(follow, "id-a")
        self.assertEqual(numbers, {"id-a": 1, "id-b": 2})
        self.assertEqual(state["rows"], fresh)
        self.assertEqual(state["title"], "Sessions · 2 total")

    def test_numbers_are_extended_by_a_refresh_never_reassigned(self):
        from cs import cli

        rows = [("a",), ("b",), ("c",)]
        first = cli._number_rows(rows)
        self.assertEqual(first, {"a": 1, "b": 2, "c": 3})
        # A newcomer at the top of natural order takes the next free number.
        grown = cli._number_rows([("d",), *rows], first)
        self.assertEqual(grown, {"a": 1, "b": 2, "c": 3, "d": 4})
        # A session that has fallen out of the window does not hold a number
        # open, but it does not free one for reuse either.
        shrunk = cli._number_rows([("b",), ("e",)], grown)
        self.assertEqual(shrunk, {"b": 2, "e": 5})

    def test_a_session_without_a_repo_shows_a_dot_not_a_gap(self):
        """The repo column is counted and empty, not failed to draw.

        Skills and agents already mark nothing with a dot; a blank cell in
        the column beside them reads as a rendering fault instead.
        """
        from cs.cli import _listing_tui

        rows = [
            ("id-repo", "2026-08-01T12:00", "Has one", "org/acme",
             "/tmp/acme", 3, 5_000_000_000),
            # Run from the home directory: too generic to name, so there is
            # no tag to show for it.
            ("id-bare", "2026-08-01T11:00", "Has none", "",
             os.path.expanduser("~"), 2, 3_000_000_000),
        ]
        screen = Screen([ord("q")])
        _listing_tui(screen, rows, "Sessions")

        final = screen.frames[-1]
        repo_x = _column_x(final, "Repo")
        self.assertEqual(final[(5, repo_x)].strip(), "acme")
        self.assertEqual(final[(6, repo_x)].strip(), "·")

    def test_a_wide_listing_leaves_no_gap_between_repo_and_title(self):
        """The summary took every spare cell and pushed the repository to the
        far edge — on a wide window, a hundred blanks after a short title.
        It is the last column now, and the repository is only as wide as
        its longest name.
        """
        from cs.cli import _listing_tui

        rows = [
            ("id-a", "2026-08-01T12:00", "Short title", "org/acme",
             "/tmp/acme", 3, 5_000_000_000),
            ("id-b", "2026-08-01T11:00", "Another", "org/widgets",
             "/tmp/widgets", 2, 3_000_000_000),
        ]
        for width in (100, 220):
            with self.subTest(width=width):
                screen = Screen([ord("q")])
                screen.getmaxyx = lambda w=width: (24, w)
                _listing_tui(screen, rows, "Sessions")

                final = screen.frames[-1]
                repo_x = _column_x(final, "Repo")
                title_x = _column_x(final, "Summary")
                self.assertLess(repo_x, title_x)
                self.assertLessEqual(title_x - repo_x, len("widgets") + 2 + 1)
                self.assertEqual(final[(5, title_x)].strip(), "Short title")

    def test_listing_preserves_titles_at_supported_widths(self):
        from cs import cli, ui

        rows = [("id-a", "2026-08-01T12:00", "日本語 title", "org/widgets",
                 "/tmp/widgets", 3, 5_000_000_000)]
        for width in (40, 60, 80, 100, 140):
            with self.subTest(width=width):
                screen = Screen([ord("q")])
                screen.getmaxyx = lambda w=width: (24, w)
                cli._listing_tui(screen, rows, "Sessions")
                frame = screen.frames[-1]
                self.assertEqual(frame[(5, _column_x(frame, "Summary"))], "日本語 title")
                for (_, column), text in frame.items():
                    self.assertLessEqual(column + ui.cells(text), width)

    def test_listing_dates_use_an_unambiguous_month_name(self):
        from cs.cli import _listing_tui

        rows = [
            ("id-date", "2026-09-11T12:31", "September", "r/a",
             "/tmp", 1, 0),
        ]
        screen = Screen([ord("q")])
        _listing_tui(screen, rows, "Sessions")

        self.assertEqual(screen.frames[-1][(5, 5)].strip(), "11 Sep 2026")

    def test_enter_resumes_highlighted_row(self):
        """Enter acts on the row, and sorting keeps the highlight on it."""
        import curses

        from cs.cli import _listing_tui


        rows = [
            ("id-new", "2026-08-01T12:00", "Newest", "r/a", "/tmp", 1, 50_000_000_000),
            ("id-mid", "2026-08-01T11:00", "Middle", "r/b", "/tmp", 2, 5_000_000_000),
            ("id-old", "2026-08-01T10:00", "Oldest", "r/c", "/tmp", 3, 900_000_000_000),
        ]

        # Enter on the second row resumes that session.
        self.assertEqual(
            _listing_tui(Screen([curses.KEY_DOWN, 10]), rows, "Sessions"),
            ("resume", "id-mid"),
        )

        # Highlight 'Middle', then sort by credits — which moves it to the
        # bottom. Enter must still resume 'Middle', not whatever took its place.
        keys = [curses.KEY_DOWN, curses.KEY_RIGHT, curses.KEY_RIGHT, 10]
        self.assertEqual(
            _listing_tui(Screen(keys), rows, "Sessions"), ("resume", "id-mid")
        )

        # 's' reverses the order and likewise keeps the highlight.
        keys = [curses.KEY_DOWN, ord("s"), 10]
        self.assertEqual(
            _listing_tui(Screen(keys), rows, "Sessions"), ("resume", "id-mid")
        )

    def test_the_filter_box_opens_empty_every_time(self):
        """'/' starts a new search, not an edit of the last one.

        The box used to open pre-filled with the filter already applied. The
        reason anyone presses '/' a second time is that the first filter was
        wrong, so the inherited text made the commonest case the expensive
        one — several backspaces before a single new character could be
        typed.
        """
        from cs.cli import _listing_tui

        rows = [
            ("id-new", "2026-08-01T12:00", "Newest", "r/a", "/tmp", 1, 50_000_000_000),
            ("id-mid", "2026-08-01T11:00", "Middle", "r/b", "/tmp", 2, 5_000_000_000),
        ]
        # Filter for 'mid', apply it, then reach for the filter again.
        keys = [ord("/"), ord("m"), ord("i"), ord("d"), 10,
                ord("/"), 10, ord("q")]
        screen = Screen(keys)
        _listing_tui(screen, rows, "Sessions")

        drawn = [frame[(23, 0)] for frame in screen.frames
                 if frame.get((23, 0), "").startswith(" filter:")]
        typed = [line.split("|")[0].removeprefix(" filter:").strip()
                 for line in drawn]
        # First pass builds 'mid' a letter at a time; the second opens blank.
        self.assertEqual(typed[:4], ["", "m", "mi", "mid"])
        self.assertEqual(typed[4], "")

    def test_mouse_selects_and_resumes_row(self):
        """A click moves the highlight; a double-click resumes that session."""
        import curses
        from unittest import mock

        from cs.cli import _listing_tui


        rows = [
            ("id-new", "2026-08-01T12:00", "Newest", "r/a", "/tmp", 1, 50_000_000_000),
            ("id-mid", "2026-08-01T11:00", "Middle", "r/b", "/tmp", 2, 5_000_000_000),
            ("id-old", "2026-08-01T10:00", "Oldest", "r/c", "/tmp", 3, 900_000_000_000),
        ]
        # Rows start at screen line 5, so line 7 is the third session.
        click = (0, 10, 7, 0, curses.BUTTON1_CLICKED)
        double = (0, 10, 7, 0, curses.BUTTON1_DOUBLE_CLICKED)

        # Click selects the row, and Enter then resumes it.
        with mock.patch.object(curses, "getmouse", return_value=click):
            self.assertEqual(
                _listing_tui(Screen([curses.KEY_MOUSE, 10]), rows, "Sessions"),
                ("resume", "id-old"),
            )

        # Double-click resumes without needing Enter at all.
        with mock.patch.object(curses, "getmouse", return_value=double):
            self.assertEqual(
                _listing_tui(Screen([curses.KEY_MOUSE]), rows, "Sessions"),
                ("resume", "id-old"),
            )

        # A click below the last row leaves the highlight where it was.
        empty = (0, 10, 15, 0, curses.BUTTON1_CLICKED)
        with mock.patch.object(curses, "getmouse", return_value=empty):
            self.assertEqual(
                _listing_tui(Screen([curses.KEY_MOUSE, 10]), rows, "Sessions"),
                ("resume", "id-new"),
            )

    def test_hovering_a_listing_header_does_not_sort(self):
        from cs.cli import _listing_tui

        rows = [
            ("id-new", "2026-08-02T12:00", "Newest", "r/a", "/tmp", 1, 10),
            ("id-old", "2026-08-01T12:00", "Oldest", "r/a", "/tmp", 1, 20),
        ]
        report = [27, *map(ord, "[<35;6;4M")]
        screen = Screen([*report, 10])
        self.assertEqual(
            _listing_tui(screen, rows, "Sessions"),
            ("resume", "id-new"),
        )
        self.assertEqual(screen.frames[0][(3, 5)], screen.frames[-1][(3, 5)])

    def test_leaving_a_view_does_not_spill_mouse_reports_onto_the_shell(self):
        """A wheel flick outran the redraws; the leftovers echoed at the prompt."""
        from unittest import mock

        import cs.cli as cli

        order: list[str] = []
        with mock.patch.object(cli, "_SGR_ENABLED", True), \
                mock.patch.object(cli, "_MOUSE_USED", True), \
                mock.patch.object(cli, "_write_terminal",
                                  lambda seq: order.append(seq)), \
                mock.patch.object(cli, "_drain_stdin", lambda: order.append("drain")):
            cli._disable_mouse()
            # Curses restores the terminal after this, so the queue has to be
            # cleared again — that second pass is the one that was missing.
            cli._disable_mouse()
        self.assertEqual(
            order, ["\033[?1003l\033[?1006l", "drain", "drain"]
        )

        # Never used the mouse, never eat what the user typed ahead.
        order.clear()
        with mock.patch.object(cli, "_SGR_ENABLED", False), \
                mock.patch.object(cli, "_MOUSE_USED", False), \
                mock.patch.object(cli, "_drain_stdin", lambda: order.append("drain")):
            cli._disable_mouse()
        self.assertEqual(order, [])

    def test_wheel_scrolls_both_ways(self):
        """Wheel-down only arrives over SGR — the X10 path cannot report it."""
        from cs.cli import _listing_tui

        rows = [
            (f"id-{i}", f"2026-08-01T{10 + i:02d}:00", f"Row{i}", "r/a", "/tmp", 1, 10)
            for i in range(12)
        ]

        def sgr(button, x, y, pressed=True):
            body = f"[<{button};{x};{y}" + ("M" if pressed else "m")
            return [27, *[ord(c) for c in body]]

        def status_of(keys):
            screen = Screen(keys)
            _listing_tui(screen, rows, "Sessions")
            return screen.frames[-1][(23, 0)]

        # Wheel down moves three rows on, wheel up brings them back.
        self.assertIn("4 of 12", status_of([*sgr(65, 20, 10), ord("q")]))
        self.assertIn("7 of 12", status_of([*sgr(65, 20, 10), *sgr(65, 20, 10), ord("q")]))
        self.assertIn(
            "1 of 12",
            status_of([*sgr(65, 20, 10), *sgr(64, 20, 10), ord("q")]),
        )
        # Scrolling stops at the ends rather than wrapping round.
        self.assertIn("1 of 12", status_of([*sgr(64, 20, 10), ord("q")]))

    def test_the_reader_wipes_in_once_and_then_holds_still(self):
        """A report arrives the way the menu that opened it did.

        Two things this has to get right, and both were wrong first time
        round: the wipe must be *over* by the time a keypress is acted on —
        otherwise the last thing on screen is a half-drawn page — and it must
        never run again, because motion while you are reading is motion that
        says nothing and never stops saying it.
        """
        from cs import ui
        from cs.cli import _reader_tui

        lines = [f"line {i} " + "x" * 60 for i in range(40)]

        # -1 is what a timed getch reports when nothing was typed: the wipe's
        # own frames. Three of them, then a key.
        screen = Screen([-1, -1, -1, ord("q")])
        _reader_tui(screen, lines, mouse=False)
        # Part-way through: the top row is clipped, and the rows under it are
        # clipped harder, because each trails the one above.
        during = screen.frames[3]
        self.assertLess(len(during.get((0, 0), "")), len(lines[0]))
        self.assertLess(len(during.get((3, 0), "")), len(during.get((0, 0), "")))
        # The frame the keypress lands on is the whole page.
        settled = screen.frames[-1]
        self.assertEqual(settled[(0, 0)][:6], "line 0")
        self.assertGreater(len(settled[(3, 0)]), 60)
        # It takes fewer frames than the wipe has, because a key ends it.
        self.assertLess(len(screen.frames), ui.REVEAL_FRAMES + 3)

    def test_the_reader_still_draws_where_a_timeout_is_not_available(self):
        """No timed getch means no wipe — and a fully drawn page anyway."""
        from cs.cli import _reader_tui

        class NoTimeout(Screen):
            def timeout(self, milliseconds):
                raise AttributeError("no timeout here")

        screen = NoTimeout([ord("q")])
        _reader_tui(screen, ["hello " + "y" * 40], mouse=False)
        self.assertTrue(screen.frames[-1][(0, 0)].startswith("hello"))

    def test_the_reader_scrolls_on_the_wheel_instead_of_leaving(self):
        """A wheel tick must never be read as the Esc that starts it.

        Reports opened from the menu are read in place, and under SGR a wheel
        tick arrives as `Esc [ < 65 ; x ; y M`. Testing for Esc before
        decoding the report sent every scroll straight back to the menu.
        """
        from cs.cli import _reader_tui

        lines = [f"line {i}" for i in range(80)]

        def sgr(button, x, y):
            return [27, *(ord(c) for c in f"[<{button};{x};{y}M")]

        def top_line_after(keys):
            screen = Screen([*keys, ord("q")])
            _reader_tui(screen, lines, mouse=True)
            return screen.frames[-1][(0, 0)]

        # Three wheel-downs move nine lines on; wheel-up brings them back.
        self.assertEqual(top_line_after([]), "line 0")
        self.assertEqual(top_line_after(sgr(65, 5, 5)), "line 3")
        self.assertEqual(top_line_after([*sgr(65, 5, 5)] * 3), "line 9")
        self.assertEqual(
            top_line_after([*sgr(65, 5, 5), *sgr(65, 5, 5), *sgr(64, 5, 5)]), "line 3"
        )
        # Scrolling up at the top stays put rather than leaving the reader.
        self.assertEqual(top_line_after(sgr(64, 5, 5)), "line 0")

    def test_the_reader_reaches_the_last_line_and_stops_there(self):
        """Both ends are reachable, and neither one falls out of the view."""
        from cs.cli import _reader_tui

        lines = [f"line {i}" for i in range(80)]
        screen = Screen([ord("G"), ord("q")])
        _reader_tui(screen, lines, mouse=False)
        frame = screen.frames[-1]
        # 24 rows: 23 of text, then the status bar.
        self.assertEqual(frame[(0, 0)], "line 57")
        self.assertEqual(frame[(22, 0)], "line 79")
        self.assertIn("end", frame[(23, 0)])

    def test_reader_find_cycles_through_matches_on_the_last_page(self):
        from cs.cli import _reader_tui

        lines = [f"row {i}" for i in range(40)]
        lines[37] = lines[39] = "needle"
        for tail, count in (("", "1/2"), ("n", "2/2"), ("nn", "1/2"), ("N", "2/2")):
            with self.subTest(tail=tail):
                screen = Screen([*map(ord, "/needle"), 10, *map(ord, tail + "q")])
                _reader_tui(screen, lines, False)
                self.assertTrue(any(count in text for text in screen.frames[-1].values()))

    def test_reader_find_recomputes_after_sorting_same_length_report(self):
        from cs.cli import _reader_tui

        lines = ["needle", *[f"row {i}" for i in range(39)]]
        sort = {"column": "name", "descending": False,
                "render": lambda *_: "\n".join(reversed(lines))}
        screen = Screen([*map(ord, "/needle"), 10, ord("s"), ord("n"), ord("q")])
        _reader_tui(screen, lines, False, sort)
        self.assertEqual(screen.frames[-1][(22, 0)], "needle")

    def test_reader_find_empty_clears_and_escape_keeps_previous_search(self):
        from cs.cli import _reader_tui

        for key, remains in ((10, False), (27, True)):
            with self.subTest(key=key):
                ending = [27, -1, -1, -1] if key == 27 else [key]
                screen = Screen([*map(ord, "/missing"), 10, ord("/"), *ending, ord("q")])
                _reader_tui(screen, ["some text"], False)
                status = " ".join(text for (row, _), text in screen.frames[-1].items()
                                  if row == 23)
                self.assertEqual("not found" in status, remains)

    def test_reader_search_wraps_and_highlights_original_unicode_characters(self):
        from cs.cli import _reader_rows

        rows, found = _reader_rows(["prefix Straße target"], {}, 14, "STRASSE")
        self.assertEqual(found, [0])
        self.assertEqual("".join(text for row in rows for text, attr in row if attr == -1),
                         "Straße")
        rows, found = _reader_rows(["x" * 36 + "needle"], {}, 40, "needle")
        self.assertEqual(found, [0, 1])
        self.assertEqual("".join(text for row in rows for text, attr in row if attr == -1),
                         "needle")
        rows, _ = _reader_rows(["Straße target"], {}, 40, "target")
        self.assertEqual([text for row in rows for text, attr in row if attr == -1],
                         ["target"])

    def test_scrolling_mid_filter_neither_cancels_it_nor_types_into_it(self):
        """The filter prompt reads Esc too, and had the same blind spot."""
        from cs.cli import _listing_tui

        rows = [
            (f"id-{i}", f"2026-08-01T{10 + i:02d}:00", f"Row{i}", "r/a", "/tmp", 1, 10)
            for i in range(4)
        ]

        def sgr(button, x, y):
            return [27, *(ord(c) for c in f"[<{button};{x};{y}M")]

        keys = [ord("/"), ord("z"), *sgr(65, 5, 5), ord("x"), 10, ord("q")]
        screen = Screen(keys)
        _listing_tui(screen, rows, "Sessions")
        # Only the prompt line matters: the wheel must leave it open, and the
        # report's own bytes must not land in it.
        typed = [
            str(value)
            for frame in screen.frames
            for value in frame.values()
            if str(value).startswith(" filter: ")
        ]
        self.assertTrue(typed, "the filter prompt never stayed open")
        self.assertTrue(
            any(line.startswith(" filter: zx") for line in typed),
            f"filter never reached 'zx': {typed}",
        )
        self.assertFalse([line for line in typed if "65;5;5" in line])

    def test_the_reader_re_sorts_a_report_without_leaving_it(self):
        """←/→ walk the columns, s reverses, and the report is rebuilt."""
        import curses

        from cs.cli import _REPORT_COLUMNS, _reader_tui

        asked: list[tuple[str, bool]] = []

        def render(column, descending):
            asked.append((column, descending))
            return f"sorted by {column}\n" + "\n".join(f"row {i}" for i in range(40))

        columns = list(_REPORT_COLUMNS["repos"])
        sort = {
            "report": "repos", "columns": columns,
            "defaults": {n: d for n, (_, d) in _REPORT_COLUMNS["repos"].items()},
            "column": "sessions", "descending": True, "render": render,
        }
        keys = [curses.KEY_RIGHT, curses.KEY_RIGHT, ord("s"), curses.KEY_LEFT, ord("q")]
        _reader_tui(Screen(keys), render("sessions", True).split("\n"), False, sort)

        # sessions → repo → turns, reversed, then back to repo.
        self.assertEqual(
            asked,
            [("sessions", True), ("repo", False), ("turns", True),
             ("turns", False), ("repo", False)],
        )

    def test_re_sorting_returns_to_the_top_of_the_report(self):
        """The row you were on is gone once the table reorders."""
        import curses

        from cs.cli import _REPORT_COLUMNS, _reader_tui

        lines = [f"row {i}" for i in range(60)]
        sort = {
            "report": "repos", "columns": list(_REPORT_COLUMNS["repos"]),
            "defaults": {n: d for n, (_, d) in _REPORT_COLUMNS["repos"].items()},
            "column": "sessions", "descending": True,
            "render": lambda column, down: "\n".join(lines),
        }
        screen = Screen([ord("G"), curses.KEY_RIGHT, ord("q")])
        _reader_tui(screen, list(lines), False, sort)
        self.assertEqual(screen.frames[-1][(0, 0)], "row 0")

    def test_a_report_with_no_sort_keeps_the_keys_it_had(self):
        """Without a sort, ← and → must not be swallowed as sort keys."""
        import curses

        from cs.cli import _reader_tui

        screen = Screen([curses.KEY_RIGHT, curses.KEY_DOWN, ord("q")])
        _reader_tui(screen, [f"row {i}" for i in range(40)], False, None)
        # KEY_RIGHT did nothing; KEY_DOWN still scrolled one line.
        self.assertEqual(screen.frames[-1][(0, 0)], "row 1")

    def test_esc_still_leaves_the_reader(self):
        """Esc is back — decoding mouse reports first must not cost that."""
        from cs.cli import _reader_tui

        screen = Screen([27])
        _reader_tui(screen, ["only line"], mouse=True)
        self.assertTrue(screen.frames)  # it drew, then returned on Esc

    def test_sgr_click_is_not_mistaken_for_escape(self):
        """An SGR report opens with Esc; the press half must not quit."""

        from cs.cli import _listing_tui

        rows = [
            ("id-one", "2026-08-01T10:00", "One", "r/a", "/tmp", 1, 10),
            ("id-two", "2026-08-01T11:00", "Two", "r/b", "/tmp", 2, 20),
            ("id-six", "2026-08-01T12:00", "Six", "r/c", "/tmp", 3, 30),
        ]

        def sgr(button, x, y, pressed=True):
            body = f"[<{button};{x};{y}" + ("M" if pressed else "m")
            return [27, *[ord(c) for c in body]]

        def press_release(screen_row):
            # Terminals report 1-based coordinates.
            y = screen_row + 1
            return [*sgr(0, 20, y), *sgr(0, 20, y, pressed=False)]

        def act(keys):
            return _listing_tui(Screen(keys), rows, "Sessions")

        # A click on the third row selects it; Enter then resumes that row —
        # proving the press half neither quit nor was treated as a keypress.
        self.assertEqual(act([*press_release(7), 10]), ("resume", "id-one"))
        # Two clicks in quick succession are a double-click, so no Enter needed.
        self.assertEqual(act([*press_release(6), *press_release(6)]), ("resume", "id-two"))
        # A real Esc still quits, and is not swallowed by the mouse probe.
        self.assertIsNone(act([27]))

    def test_show_returns_to_the_same_view(self):
        """'o' leaves for the detail view, and coming back restores the view."""
        import curses

        from cs.cli import _listing_tui

        rows = [
            ("id-new", "2026-08-01T12:00", "Newest", "r/a", "/tmp", 1, 10),
            ("id-mid", "2026-08-01T11:00", "Middle", "r/b", "/tmp", 2, 20),
            ("id-old", "2026-08-01T10:00", "Oldest", "r/c", "/tmp", 3, 30),
        ]

        # Sort by turns, move down, filter, then show the highlighted session.
        state: dict = {}
        keys = [
            curses.KEY_RIGHT,                       # sort by turns
            curses.KEY_DOWN,                        # move off the first row
            ord("/"), *[ord(c) for c in "old"], 10,  # filter down to 'Oldest'
            ord("o"),
        ]
        self.assertEqual(
            _listing_tui(Screen(keys), rows, "Sessions", state=state),
            ("show", "id-old"),
        )
        self.assertEqual(state["sort_by"], "turns")
        self.assertEqual(state["query"], "old")

        # Re-entering with that state resumes the same row, under the same
        # sort and filter — the trip through the detail view left no trace.
        screen = Screen([10])
        self.assertEqual(
            _listing_tui(screen, rows, "Sessions", state=state),
            ("resume", "id-old"),
        )
        self.assertIn("sorted by turns", screen.frames[0][(0, 0)])
        self.assertIn("filter 'old'", screen.frames[0][(0, 0)])

    def test_pause_lets_you_stop(self):
        from unittest import mock

        from cs.cli import _pause

        with mock.patch("cs.cli._read_key", return_value=None):
            with mock.patch("builtins.input", return_value=""):
                self.assertTrue(_pause("x"))          # Enter → back to the list
            with mock.patch("builtins.input", return_value="q"):
                self.assertFalse(_pause("x"))         # q → done
            with mock.patch("builtins.input", side_effect=EOFError):
                self.assertFalse(_pause("x"))         # Ctrl-D → done, no traceback

    def test_unknown_escape_sequence_does_not_quit(self):
        """A stray ESC-[ sequence must be swallowed, not read as Esc."""

        from cs.cli import _listing_tui

        rows = [
            ("id-one", "2026-08-01T10:00", "One", "r/a", "/tmp", 1, 10),
            ("id-two", "2026-08-01T11:00", "Two", "r/b", "/tmp", 2, 20),
        ]
        # ESC [ C is a cursor-mode arrow ncurses did not translate. It must
        # not reach the quit path — Enter afterwards still resumes.
        keys = [27, ord("["), ord("C"), 10]
        self.assertEqual(
            _listing_tui(Screen(keys), rows, "Sessions"), ("resume", "id-two")
        )
        # A bare Esc still quits.
        self.assertIsNone(_listing_tui(Screen([27]), rows, "Sessions"))

    def test_mouse_header_click_sorts(self):
        """Clicking a column header sorts by it, then reverses on a second click."""
        import curses
        from unittest import mock

        from cs.cli import _listing_tui


        rows = [
            ("one", "2026-08-01T10:00", "One", "repo/one", "/tmp", 1, 10),
            ("two", "2026-08-01T11:00", "Two", "repo/two", "/tmp", 2, 20),
        ]
        # x=17 is the 'Turns' column, on the header line (y=3).
        header = (0, 17, 3, 0, curses.BUTTON1_CLICKED)
        screen = Screen([curses.KEY_MOUSE, curses.KEY_MOUSE, ord("q")])
        with mock.patch.object(curses, "getmouse", return_value=header):
            _listing_tui(screen, rows, "Sessions")
        titles = [frame[(0, 0)] for frame in screen.frames]
        self.assertTrue(any("sorted by turns ↓" in title for title in titles))
        self.assertTrue(any("sorted by turns ↑" in title for title in titles))

    def test_search_tui_starts_on_relevance(self):
        """A search opens best-match-first, shows the hit, and can sort away."""
        import curses

        from cs.cli import _listing_tui


        rows = [
            ("id-best", "2026-08-01T10:00", "Best", "r/a", "/tmp", 1, 10),
            ("id-next", "2026-08-01T11:00", "Next", "r/b", "/tmp", 2, 20),
        ]
        hits = {"id-best": ("checkpoint_next_steps", "wire the charts to live data")}

        screen = Screen([curses.KEY_RIGHT, ord("q")])
        _listing_tui(screen, rows, "Search", "relevance", hits)
        headings = [f[(0, 0)] for f in screen.frames]
        self.assertIn("Search · best match first", headings[0])
        # → leaves relevance for the first real column.
        self.assertTrue(any("sorted by active" in h for h in headings))
        # The hit is shown for the highlighted row, labelled by its source.
        hit_line = screen.frames[0][(22, 0)]
        self.assertIn("next steps", hit_line)
        self.assertIn("wire the charts", hit_line)

        # Enter still resumes the row the search ranked first.
        self.assertEqual(
            _listing_tui(Screen([10]), rows, "Search", "relevance", hits),
            ("resume", "id-best"),
        )

    def test_arrow_key_sorts_immediately(self):
        import curses

        from cs.cli import _listing_tui


        screen = Screen([curses.KEY_RIGHT, ord("q")])
        rows = [
            ("one", "2026-08-01T10:00", "One", "repo/one", "/tmp", 1, 10),
            ("two", "2026-08-01T11:00", "Two", "repo/two", "/tmp", 2, 20),
        ]
        _listing_tui(screen, rows, "Sessions")
        titles = [frame[(0, 0)] for frame in screen.frames]
        self.assertTrue(any("Sessions · sorted by turns ↓" in title for title in titles))

    def test_filter_and_resume_action(self):
        from cs.cli import _listing_tui


        rows = [
            ("id-alpha", "2026-08-01T10:00", "Alpha work", "repo/alpha", "/tmp", 1, 10),
            ("id-beta", "2026-08-01T11:00", "Beta work", "repo/beta", "/tmp", 2, 20),
        ]
        # '/beta<Enter>' filters to one row; 'r' resumes whatever the cursor holds.
        keys = [ord("/"), *[ord(c) for c in "beta"], 10, ord("r")]
        self.assertEqual(_listing_tui(Screen(keys), rows, "Sessions"), ("resume", "id-beta"))

        # Esc clears the filter instead of quitting, so both rows are reachable
        # again — moving down to the second row proves the full list is back.
        import curses

        screen = Screen(
            [ord("/"), *[ord(c) for c in "beta"], 10, 27, curses.KEY_DOWN, ord("o")]
        )
        self.assertEqual(_listing_tui(screen, rows, "Sessions"), ("show", "id-alpha"))

        # A filter matching nothing must not crash or return a phantom session.
        no_match = Screen([ord("/"), *[ord(c) for c in "zzz"], 10, ord("r"), ord("q")])
        self.assertIsNone(_listing_tui(no_match, rows, "Sessions"))

    def test_a_filter_reaches_the_conversation_not_just_the_title(self):
        """Filtering 'All sessions' for a project found only the sessions
        with its name in the title — five of fifty-six. The filter now asks
        the store the way `cs search` does, and says why each row matched.
        """
        import curses

        from cs.cli import _listing_tui

        rows = [
            ("id-title", "2026-08-01T12:00", "QRX dashboard", "r/a", "/tmp", 1, 10),
            ("id-text", "2026-08-01T11:00", "Resume work", "r/b", "/tmp", 2, 20),
            ("id-none", "2026-08-01T10:00", "Unrelated", "r/c", "/tmp", 3, 30),
        ]
        asked: list[str] = []

        def find(query):
            asked.append(query)
            return {"id-text"}, {"id-text": ("turn", "the QRX pilot baseline")}

        state: dict = {}
        screen = Screen([ord("/"), *map(ord, "QRX"), 10, curses.KEY_DOWN, ord("q")])
        _listing_tui(screen, rows, "Sessions", state=state, find=find)

        final = screen.frames[-1]
        at = _column_x(final, "Summary")
        listed = [final.get((y, at), "").strip() for y in (5, 6, 7)]
        self.assertEqual(listed, ["QRX dashboard", "Resume work", ""])
        self.assertIn("turn: the QRX pilot baseline", final[(22, 0)])

        # Back from a session with the filter still up: the answer is kept,
        # not asked for again.
        _listing_tui(Screen([ord("q")]), rows, "Sessions", state=state, find=find)
        self.assertEqual(asked, ["QRX"])

    def test_typing_in_a_listing_starts_the_filter(self):
        """A letter with no job of its own did nothing, so typing a name into
        a listing looked like a search that found nothing."""
        from cs.cli import _listing_tui

        rows = [
            ("id-alpha", "2026-08-01T10:00", "Alpha work", "repo/alpha", "/tmp", 1, 10),
            ("id-beta", "2026-08-01T11:00", "Beta work", "repo/beta", "/tmp", 2, 20),
        ]
        # 'B' opens the box with itself in it; the rest is typed into the box,
        # 't' included, so a shortcut inside a word does not fire.
        screen = Screen([*map(ord, "Beta"), 10, ord("r")])
        self.assertEqual(_listing_tui(screen, rows, "Sessions"), ("resume", "id-beta"))
        self.assertIn("filter 'Beta'", screen.frames[-1][(0, 0)])

    def test_every_listing_can_search_the_store(self):
        from unittest import mock

        from cs import cli

        seen: list[tuple] = []
        with mock.patch.object(cli, "_curses_wrapper",
                               side_effect=lambda view, *args: seen.append(args)):
            cli._interactive_listing(
                [("sess-alpha", "2026-08-01T10:00", "Build Three.js portal",
                  "acme/portal", "/tmp/a", 2, 10)],
                "Sessions", show_all=True,
            )
        self.assertIs(seen[0][-1], cli._find_sessions)

        found, reasons = cli._find_sessions("globe")  # only in a turn's text
        self.assertEqual(found, {"sess-alpha"})
        self.assertEqual(reasons["sess-alpha"][0], "turn")

    def test_sort_rejects_unknown_column(self):
        code, _ = self._run("recent", "--sort", "nonsense")
        self.assertEqual(code, 1)

    def test_rejects_non_positive_days(self):
        code, _ = self._run("recent", "-7")
        self.assertEqual(code, 1)
        timeline_code, _ = self._run("timeline", "0")
        self.assertEqual(timeline_code, 1)

    def test_show_missing(self):
        code, out = self._run("show", "does-not-exist")
        self.assertEqual(code, 1)

    def test_bare_number_shortcut(self):
        # A listing populates the #N index; 'show 1' must resolve like 'show #1'.
        self._run("recent", "7")
        code, out = self._run("show", "1")
        self.assertEqual(code, 0)
        self.assertIn("Build Three.js portal", out)
        # #-prefixed form still works
        code2, out2 = self._run("show", "#1")
        self.assertEqual(code2, 0)
        self.assertIn("Build Three.js portal", out2)

    def test_resume_chdirs_to_session_cwd(self):
        from cs import cli
        base = Path(self._tmp.name)
        conn = sqlite3.connect(base / "session-store.db")
        conn.execute("UPDATE sessions SET cwd = ? WHERE id = 'sess-alpha'", (str(base),))
        conn.commit()
        conn.close()

        seen = {}
        origin = os.getcwd()
        real_chdir, real_exec, real_which = os.chdir, os.execv, cli.shutil.which
        os.chdir = lambda p: seen.setdefault("cwd", p)
        os.execv = lambda f, a: seen.setdefault("exec", (f, a)) or (_ for _ in ()).throw(
            SystemExit(0))
        # $PATH must be walked from where we started, not from the session's
        # own folder — so record whether the lookup happened before the chdir.
        def fake_which(name):
            seen["which_before_cd"] = "cwd" not in seen
            return "/b/c"

        cli.shutil.which = fake_which
        try:
            self._run("resume", "sess-alpha")
        finally:
            os.chdir, os.execv, cli.shutil.which = real_chdir, real_exec, real_which
        self.assertEqual(seen["cwd"], str(base))
        self.assertEqual(seen["exec"], ("/b/c", ["copilot", "--resume", "sess-alpha"]))
        self.assertTrue(seen["which_before_cd"])
        self.assertEqual(os.getcwd(), origin)

    def test_resume_missing_session(self):
        code, _ = self._run("resume", "nope")
        self.assertEqual(code, 1)

    def test_resume_from_the_listing_comes_back_to_it(self):
        """Enter on a row means "go and look at this session", not "close cs".

        `cs resume` from the shell execv's, which is right there — nothing to
        return to. The listing used to call the same function, so quitting
        Copilot left the user at their shell with the app gone. The listing
        needs a child process it can outlive.
        """
        from cs import cli

        seen = {}
        real_run, real_which = cli.subprocess.run, cli.shutil.which
        cli.shutil.which = lambda name: "/b/copilot"
        cli.subprocess.run = lambda cmd, **kw: seen.update(cmd=cmd, cwd=kw.get("cwd"))
        try:
            cli._resume_from_listing("sess-alpha")   # returns; does not exec
        finally:
            cli.subprocess.run, cli.shutil.which = real_run, real_which
        self.assertEqual(seen["cmd"], ["/b/copilot", "--resume", "sess-alpha"])

    def test_ctrl_c_out_of_copilot_returns_to_the_listing(self):
        """The reported bug. Ctrl-C is how people close the Copilot CLI, and
        the child has no process group of its own, so the terminal signals
        `cs` as well. Letting that propagate would tear down the app the
        keystroke was never aimed at."""
        from cs import cli

        def interrupted(cmd, **kw):
            raise KeyboardInterrupt

        real_run, real_which = cli.subprocess.run, cli.shutil.which
        cli.shutil.which = lambda name: "/b/copilot"
        cli.subprocess.run = interrupted
        try:
            cli._resume_from_listing("sess-alpha")   # must not raise
        finally:
            cli.subprocess.run, cli.shutil.which = real_run, real_which

    def test_a_missing_copilot_does_not_close_the_app(self):
        """Same rule one step earlier: from inside the listing a missing
        binary is a message, not a reason to exit. From the shell it is still
        an error, because there is no app to keep alive."""
        from cs import cli

        real_which, real_pause = cli.shutil.which, cli._pause
        cli.shutil.which = lambda name: None
        cli._pause = lambda message: True
        try:
            cli._resume_from_listing("sess-alpha")   # must not raise SystemExit
            self.assertIsNone(cli._resume_target("sess-alpha"))
        finally:
            cli.shutil.which, cli._pause = real_which, real_pause

    def test_the_listing_does_not_chdir_the_app(self):
        """`cwd=` on the child, not `os.chdir` on us: the listing we come
        back to has to resolve paths the way it drew them."""
        from cs import cli

        base = Path(self._tmp.name)
        conn = sqlite3.connect(base / "session-store.db")
        conn.execute("UPDATE sessions SET cwd = ? WHERE id = 'sess-alpha'", (str(base),))
        conn.commit()
        conn.close()

        origin = os.getcwd()
        seen = {}
        real_run, real_which = cli.subprocess.run, cli.shutil.which
        cli.shutil.which = lambda name: "/b/copilot"
        cli.subprocess.run = lambda cmd, **kw: seen.update(cwd=kw.get("cwd"))
        try:
            cli._resume_from_listing("sess-alpha")
        finally:
            cli.subprocess.run, cli.shutil.which = real_run, real_which
        self.assertEqual(seen["cwd"], str(base))    # the child moved
        self.assertEqual(os.getcwd(), origin)       # and we did not

    def test_unknown_command(self):
        code, out = self._run("bogus")
        self.assertEqual(code, 1)


class InterpreterFloorTest(StoreTest):
    """The Python floor is declared in three places and enforced in one.

    `pip` honours requires-python, but `install.sh` and `bin/cs` never go
    through pip, and every module carries `from __future__ import annotations`
    so an old interpreter sails past import and only fails later, deep inside a
    view. The guard in `cs/__init__.py` is the one thing standing in the way,
    and CI can never execute it -- the interpreter running these tests is
    always new enough -- so it is exercised here against a stubbed `sys`.
    """

    def _run_guard(self, version: tuple[int, ...]):
        source = Path("cs/__init__.py").read_text(encoding="utf-8")
        # Split on the declaration rather than a version literal: pinning the
        # number here made every release break a test about interpreters.
        _, guard = re.split(r'^__version__ = ".*"$', source, maxsplit=1, flags=re.M)
        stub = type(
            "StubSys",
            (),
            {"version_info": version, "executable": "/usr/bin/python3"},
        )()
        namespace: dict = {"sys": stub}
        exec(compile(guard, "cs/__init__.py", "exec"), namespace)

    def test_an_old_interpreter_is_turned_away_with_a_way_out(self):
        with self.assertRaises(SystemExit) as caught:
            self._run_guard((3, 9, 18))
        message = str(caught.exception)
        self.assertIn("3.9.18", message)
        self.assertIn("Python 3.10 or newer", message)
        self.assertIn("brew install smaharajan/tap/copilot-sessions", message)

    def test_a_supported_interpreter_passes_through(self):
        self._run_guard((3, 10, 0))
        self._run_guard((3, 13, 2))


class DeclaredVersionTest(StoreTest):
    """One number, quoted in several files that no bump goes near.

    `cs/__init__.py` is the version; the README sample and the bug report's
    placeholder only echo it. The 1.1.0 release left `cs 1.0.0` behind in the
    issue template, inviting every reporter to file against a build nobody is
    running, and nothing failed. The changelog is history and keeps its own
    numbers, so it is not read here.
    """

    ECHOES = ("README.md", ".github/ISSUE_TEMPLATE/bug_report.yml")

    def test_every_echoed_version_is_the_declared_one(self):
        from cs import __version__

        root = Path(__file__).resolve().parent.parent
        for name in self.ECHOES:
            text = (root / name).read_text(encoding="utf-8")
            for quoted in re.findall(r"\bcs (\d+\.\d+\.\d+)", text):
                self.assertEqual(
                    quoted, __version__, f"{name} still says cs {quoted}"
                )
