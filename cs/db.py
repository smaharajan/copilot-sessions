"""Session store access — read-only queries against the Copilot SQLite DB.

The store location can be overridden with the ``COPILOT_HOME`` environment
variable (defaults to ``~/.copilot``). All access is read-only.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
from pathlib import Path


def default_db_path() -> Path:
    """Resolve the session store path, honouring COPILOT_HOME."""
    home = os.environ.get("COPILOT_HOME")
    base = Path(home) if home else Path.home() / ".copilot"
    return base / "session-store.db"


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the session store read-only. Exit with a clear message if absent."""
    path = db_path or default_db_path()
    if not path.exists():
        print(
            f"error: no Copilot session store found at {path}\n"
            f"       set COPILOT_HOME if your Copilot data lives elsewhere.",
            file=sys.stderr,
        )
        sys.exit(1)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    # `connect` itself does not read the file, so a path that is not a
    # database only fails on the first query — which was this one, and it
    # arrived as a traceback rather than as the sentence every other bad
    # COPILOT_HOME gets. A truncated download, a file restored from the
    # wrong backup and a store still being written all land here.
    try:
        shortfall = missing_essentials(conn)
    except sqlite3.DatabaseError as broken:
        conn.close()
        print(
            f"error: {path} is not readable as a database ({broken})\n"
            f"       if Copilot is mid-write, try again; otherwise set "
            f"COPILOT_HOME to the directory Copilot actually writes.",
            file=sys.stderr,
        )
        sys.exit(1)
    if shortfall:
        conn.close()
        print(
            f"error: {path} is not a Copilot session store\n"
            f"       missing {', '.join(shortfall)}\n"
            f"       set COPILOT_HOME to the directory Copilot actually writes.",
            file=sys.stderr,
        )
        sys.exit(1)
    return conn


# ── What this store can answer ───────────────────────────────────────
# Copilot's schema has grown table by table and column by column, and an
# open-source tool meets every version of it. Two rules keep that honest:
# anything in ESSENTIALS is what makes a file a session store at all and is
# checked once on connect, and everything else is optional — asked for before
# it is used, and reported as absent rather than as zero.

ESSENTIALS: dict[str, tuple[str, ...]] = {
    "sessions": ("id", "created_at", "updated_at"),
    "turns": ("session_id", "turn_index", "user_message", "assistant_response"),
}

# Optional pieces, and the plain-English answer each one buys. Anything not
# listed here is either essential or internal.
OPTIONAL: dict[str, str] = {
    "repository": "which repository a session belonged to",
    "cwd": "which directory a session ran in",
    "branch": "which branch a session was on",
    "summary": "Copilot's own one-line summary of a session",
    "timestamps": "when each turn happened",
    "usage": "what a session cost",
    "windowed usage": "cost over a date window rather than all of time",
    "delegation": "who initiated each call — you, the agent, or a sub-agent",
    "files": "which files a session touched",
    "tools": "which tool touched a file",
    "checkpoints": "Copilot's own summary of the work",
    "refs": "the commits and PRs a session produced",
    "search index": "full-text search without scanning every turn",
}


def ignored_prefixes() -> list[str]:
    """Summary prefixes the user wants left out, from $COPILOT_HOME/.cs-ignore.

    One prefix per line, `#` for comments. It lives here rather than in the
    CLI because every view that counts sessions has to agree on which ones
    are yours — a practice review of your scheduled pipelines is a review of
    a cron job.
    """
    path = default_db_path().parent / ".cs-ignore"
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


# ── Names Copilot keeps outside the store ────────────────────────────
# Copilot writes each session's name to session-state/<id>/workspace.yaml,
# and that is the name its own session list shows. The store's `summary` is
# not kept in step with it: rename a session with /rename and the store goes
# on holding whatever the session was first summarised as. Searching for
# the new name must still find it, and listings must show that name.


def session_names() -> dict[str, tuple[str, bool]]:
    """{session id: (name, whether you gave it)} from every workspace.yaml.

    Read from disk on every call — the store is live and so is this.
    A folder that cannot be read, or
    a file with no name in it, is simply a session without one.
    """
    names: dict[str, tuple[str, bool]] = {}
    try:
        folders = list(_session_state().iterdir())
    except OSError:
        return names
    for folder in folders:
        found = session_name(folder.name)
        if found:
            names[folder.name] = found
    return names


def session_name(session_id: str) -> tuple[str, bool] | None:
    """(name, whether you gave it) for one session, or None if it has none."""
    if not session_id or session_id in (".", "..") or "/" in session_id or "\\" in session_id:
        return None
    try:
        root = _session_state().resolve()
        path = (root / session_id / "workspace.yaml").resolve()
        if not path.is_relative_to(root):
            return None
        text = path.read_text(
            encoding="utf-8", errors="replace"
        )
    except (OSError, ValueError, RuntimeError):
        return None
    fields = _yaml_fields(text, ("name", "user_named"))
    name = fields.get("name", "").strip()
    return (name, fields.get("user_named") == "true") if name else None


def _session_state() -> Path:
    return default_db_path().parent / "session-state"


def _yaml_fields(text: str, wanted: tuple[str, ...]) -> dict[str, str]:
    """Top-level scalars from a flat YAML mapping, in the forms Copilot writes.

    Not a YAML parser — the standard library has none, and a workspace file
    is one flat mapping of scalars. A value may run onto indented lines
    below its key; those are read as part of it and never as keys.
    """
    found: dict[str, str] = {}
    lines = text.splitlines()
    for at, line in enumerate(lines):
        key, colon, value = line.partition(":")
        if not colon or key not in wanted or key in found:
            continue
        rest = []
        for more in lines[at + 1:]:
            if more and not more[0].isspace():
                break
            if more.strip():
                rest.append(more.strip())
        found[key] = _yaml_scalar(value.strip(), rest)
    return found


def _yaml_scalar(head: str, rest: list[str]) -> str:
    """One scalar, on one line: a name is a title, however it was stored.

    Plain, 'single-quoted' (a doubled quote is a quote), "double-quoted",
    and the |- block Copilot uses when a name is a pasted multi-line prompt.
    """
    if head[:1] in ("|", ">"):
        return " ".join(rest)
    joined = " ".join([head, *rest]).strip()
    if len(joined) >= 2 and joined[0] == joined[-1] == "'":
        return joined[1:-1].replace("''", "'")
    if len(joined) >= 2 and joined[0] == joined[-1] == '"':
        try:
            return json.loads(joined)
        except ValueError:
            return joined[1:-1]
    return joined


def _given_names(rows: list[tuple], names: dict[str, tuple[str, bool]]) -> list[tuple]:
    """Listing rows titled with the name you gave the session, where you gave one.

    Only a name you chose outranks the store's summary. Copilot's own
    generated names are left where they are: for most sessions the store's
    summary is the later and better of the two, and a generated name is
    often the whole first prompt.
    """
    given = {sid: name for sid, (name, yours) in names.items() if yours}
    if not given:
        return rows
    return [
        (row[0], row[1], given[row[0]], *row[3:]) if row[0] in given else row
        for row in rows
    ]


def missing_essentials(conn: sqlite3.Connection) -> list[str]:
    """What a store would need before any of this is worth running.

    Returned as English ('the turns table', 'sessions.created_at') because it
    is printed straight at whoever pointed COPILOT_HOME somewhere wrong.
    """
    absent: list[str] = []
    for table, columns in ESSENTIALS.items():
        if not _has_table(conn, table):
            absent.append(f"the {table} table")
            continue
        absent.extend(
            f"{table}.{column}"
            for column in columns
            if not _has_columns(conn, table, column)
        )
    return absent


def capabilities(conn: sqlite3.Connection) -> dict[str, bool]:
    """Which optional answers this store can give. Keys are OPTIONAL's."""
    return {
        "repository": _has_columns(conn, "sessions", "repository"),
        "cwd": _has_columns(conn, "sessions", "cwd"),
        "branch": _has_columns(conn, "sessions", "branch"),
        "summary": _has_columns(conn, "sessions", "summary"),
        "timestamps": _has_columns(conn, "turns", "timestamp"),
        "usage": _has_usage(conn),
        "windowed usage": usage_is_windowable(conn),
        "delegation": has_delegation(conn),
        "files": _has_files(conn),
        "tools": _has_columns(conn, "session_files", "tool_name"),
        "checkpoints": _has_table(conn, "checkpoints"),
        "refs": _has_table(conn, "session_refs"),
        "search index": _has_fts(conn),
    }


def optional(conn: sqlite3.Connection, table: str, column: str,
         alias: str = "", default: str = "NULL") -> str:
    """SQL for an optional column: the column, or a literal when it is absent.

    Public because `signals` writes its own SQL over the same tables and has
    to degrade the same way this module does.

    Interpolating a column name is safe here in the one way that matters —
    every name comes from this module, never from user input.
    """
    if not _has_columns(conn, table, column):
        return default
    return f"{alias}.{column}" if alias else column


# ── Query helpers ────────────────────────────────────────────────────
# Each returns plain tuples/rows; the CLI layer handles all formatting.

def _has_usage(conn: sqlite3.Connection) -> bool:
    """Whether the store records AI-unit usage (older stores may not)."""
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='assistant_usage_events'"
        ).fetchone()
        is not None
    )


def _aiu_sub(conn: sqlite3.Connection, alias: str = "s") -> str:
    """SQL scalar giving a session's total spend in nano-AIU (0 if unavailable)."""
    if _has_usage(conn):
        return (
            "COALESCE((SELECT SUM(total_nano_aiu) FROM assistant_usage_events u "
            f"WHERE u.session_id = {alias}.id), 0)"
        )
    return "0"


def _session_cols(conn: sqlite3.Connection) -> str:
    """The shape every listing shares — with the optional parts filled in.

    A store without `repository` reports every session as unattributed rather
    than refusing to list any of them: an absent column is a missing answer,
    not a broken query.
    """
    return f"""
    s.id,
    substr(MAX(s.created_at, s.updated_at), 1, 16) AS last_active,
    COALESCE({optional(conn, 'sessions', 'summary', 's', "''")}, '') AS summary,
    COALESCE({optional(conn, 'sessions', 'repository', 's', "''")}, '') AS repo,
    COALESCE({optional(conn, 'sessions', 'cwd', 's', "''")}, '') AS cwd,
    (SELECT COUNT(*) FROM turns t WHERE t.session_id = s.id) AS turns
"""


def recent_sessions(conn: sqlite3.Connection, days: int) -> list[tuple]:
    """Sessions touched in the last `days`, newest first.

    `days <= 0` means every session ever recorded — what `cs all` needs. A
    very large day count would do the same, but only until it didn't: it
    puts a silent expiry on the oldest sessions, and a store is meant to
    keep them.
    """
    window = "WHERE MAX(s.created_at, s.updated_at) >= datetime('now', ?)" if days > 0 else ""
    rows = conn.execute(
        f"""SELECT {_session_cols(conn)}, {_aiu_sub(conn)} AS nano_aiu
            FROM sessions s
            {window}
            ORDER BY MAX(s.created_at, s.updated_at) DESC""",
        (f"-{days} days",) if days > 0 else (),
    ).fetchall()
    return _given_names(rows, session_names())


def _has_fts(conn: sqlite3.Connection) -> bool:
    """Whether the store carries the full-text index (older stores may not)."""
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='search_index'"
        ).fetchone()
        is not None
    )


def _fts_fallback_query(term: str) -> str:
    """Rewrite a query FTS5 refused into a plain AND of quoted words.

    Punctuation is a syntax error in FTS5 ('three.js', 'C++'), which users
    type constantly — quoting each word makes those queries just work.
    """
    words = ["".join(ch for ch in w if ch.isalnum()) for w in term.split()]
    return " AND ".join(f'"{w}"' for w in words if w)


def _fts_hits(
    conn: sqlite3.Connection, term: str
) -> list[tuple[str, str, str]]:
    """Best-first (session_id, source_type, snippet), one per matching session.

    The query is tried as typed so AND/OR/NEAR and "phrases" keep working,
    then retried sanitised. Returns [] when neither parses.

    Every session that matches is returned. The ranking pass reads no text,
    so it costs little however many rows match; snippet() is the expensive
    part, and it is asked for only on each session's best row. Asking for it
    on every row was what forced a cap in the first place — and the cap, at
    forty sessions, hid real matches: a project name mentioned across fifty
    sessions lost the rest with nothing on screen to say so.
    """
    for query in (term, _fts_fallback_query(term)):
        if not query:
            continue
        try:
            ranked = conn.execute(
                """SELECT rowid, session_id, source_type FROM search_index
                   WHERE search_index MATCH ?
                   ORDER BY bm25(search_index)""",
                (query,),
            ).fetchall()
        except sqlite3.OperationalError:
            continue  # bad FTS syntax — try the sanitised form
        best: dict[str, tuple[int, str]] = {}
        for rowid, sid, source in ranked:
            best.setdefault(sid, (rowid, source))
        # No highlight markers: FTS would insert them at token boundaries,
        # which inside a credential (`ghp` `_` `ZZZZ…` is three tokens)
        # splits it into something no redaction pattern recognises. The
        # match is coloured later, in Python, once the text has been masked.
        # See cli._snippet.
        snippets: dict[int, str] = {}
        rowids = [rowid for rowid, _ in best.values()]
        for chunk in _chunks(rowids):
            snippets.update(conn.execute(
                f"""SELECT rowid, snippet(search_index, 0, '', '', '…', 12)
                    FROM search_index
                    WHERE search_index MATCH ?
                      AND rowid IN ({','.join('?' * len(chunk))})""",
                (query, *chunk),
            ))
        return [
            (sid, source, snippets.get(rowid) or "")
            for sid, (rowid, source) in best.items()
        ]
    return []


def _chunks(values: list, size: int = 500):
    """Slices small enough for an IN (...) list on any SQLite build.

    Older builds refuse more than 999 bound parameters in one statement, and
    an uncapped search can name every session in the store.
    """
    for start in range(0, len(values), size):
        yield values[start:start + size]


def search(
    conn: sqlite3.Connection, term: str
) -> tuple[list[tuple], dict[str, tuple[str, str]]]:
    """Every session that matches, best match first, with the snippet that matched.

    Metadata hits (summary, name, repo, directory) outrank full-text hits: if
    the term names the session, that is a stronger signal than a passing
    mention inside one of its turns. A name is every name the session goes
    by — the one you gave it, and the one Copilot generated — because either
    may be the one you remember it as.
    """
    like = f"%{term}%"
    # A store missing one of these fields simply has one fewer way to match:
    # NULL LIKE ? is NULL, which never satisfies the WHERE.
    named = " OR ".join(
        f"{optional(conn, 'sessions', column, 's')} LIKE ?1"
        for column in ("summary", "repository", "cwd")
    )
    meta = conn.execute(
        f"""SELECT {_session_cols(conn)}, {_aiu_sub(conn)} AS nano_aiu
            FROM sessions s
            WHERE {named}
            ORDER BY MAX(s.created_at, s.updated_at) DESC""",
        (like,),
    ).fetchall()

    # The names live on disk, not in the store, so they are matched here.
    # LIKE folds case for ASCII only; casefold() does it for all of it,
    # which is what anyone typing a name would expect of it.
    names = session_names()
    wanted = term.casefold()
    by_name = {sid for sid, (name, _) in names.items() if wanted in name.casefold()}
    known_ids = {row[0] for row in meta}
    extra = [sid for sid in by_name if sid not in known_ids]
    for chunk in _chunks(extra):
        meta.extend(conn.execute(
            f"""SELECT {_session_cols(conn)}, {_aiu_sub(conn)} AS nano_aiu
                FROM sessions s WHERE s.id IN ({','.join('?' * len(chunk))})""",
            chunk,
        ))
    stored = {row[0]: row[2] or "" for row in meta}
    # Newest first, as the SQL had them; sorted() is stable, so the store's
    # own order survives among sessions active in the same minute.
    meta = sorted(_given_names(meta, names), key=lambda row: row[1] or "", reverse=True)

    # A dict for its order and its membership test: an uncapped search can
    # name a thousand sessions, and `in` on a list of them is quadratic.
    ordered = dict.fromkeys(row[0] for row in meta)
    hits: dict[str, tuple[str, str]] = {}
    # A session found under a title the listing does not show says which
    # one, or it reads as a result with no reason to be there.
    for row in meta:
        if wanted in (row[2] or "").casefold():
            continue
        if row[0] in by_name:
            hits[row[0]] = ("name", " ".join(names[row[0]][0].split()))
        elif wanted in stored[row[0]].casefold():
            hits[row[0]] = ("summary", " ".join(stored[row[0]].split()))
    if _has_fts(conn):
        for sid, source, snippet in _fts_hits(conn, term):
            hits.setdefault(sid, (source, " ".join(snippet.split())))
            ordered.setdefault(sid)
    else:
        # No index: fall back to scanning the turns themselves.
        for (sid,) in conn.execute(
            """SELECT DISTINCT session_id FROM turns
               WHERE user_message LIKE ?1 OR assistant_response LIKE ?1""",
            (like,),
        ):
            ordered.setdefault(sid)

    known = {row[0]: row for row in meta}
    missing = [sid for sid in ordered if sid not in known]
    for chunk in _chunks(missing):
        for row in _given_names(conn.execute(
            f"""SELECT {_session_cols(conn)}, {_aiu_sub(conn)} AS nano_aiu
                FROM sessions s WHERE s.id IN ({','.join('?' * len(chunk))})""",
            chunk,
        ).fetchall(), names):
            known[row[0]] = row

    rows = [known[sid] for sid in ordered if sid in known]
    return rows, {sid: hits[sid] for sid in ordered if sid in hits}



def session_detail(conn: sqlite3.Connection, session_id: str) -> tuple | None:
    # An absent column reads as '-', the same as a column the store has but
    # never filled in: either way the answer is "not recorded".
    row = conn.execute(
        f"""SELECT COALESCE({optional(conn, 'sessions', 'summary')}, '(no summary)'),
                  COALESCE({optional(conn, 'sessions', 'repository')}, '-'),
                  COALESCE({optional(conn, 'sessions', 'cwd')}, '-'),
                  COALESCE({optional(conn, 'sessions', 'branch')}, '-'),
                  created_at,
                  updated_at
           FROM sessions WHERE id = ?""",
        (session_id,),
    ).fetchone()
    # The page opened from a listing carries the title that listing showed.
    named = session_name(session_id) if row else None
    if named and named[1]:
        return (named[0], *row[1:])
    return row


def session_turn_count(conn: sqlite3.Connection, session_id: str) -> int:
    """How many turns a session holds — every one of them, prompt or not.

    This replaced a query that pulled 2,000 characters of every prompt in the
    session so that the page could print `len()` of the result. That query
    backed the turn index on `cs show`, which has gone: it was a third
    rendering of a list the page already showed the ends of and `--asks`
    already showed in full, and the conversation itself belongs to `cs read`.

    Counted rather than filtered on purpose. A turn with no prompt is still a
    turn — it is compaction, or a tool loop — and the header says "turns",
    not "requests". `session_prompts` is the one that drops the empties, and
    the two disagreeing is the point of having both.
    """
    return conn.execute(
        "SELECT count(*) FROM turns WHERE session_id = ?", (session_id,)
    ).fetchone()[0]


def session_transcript(conn: sqlite3.Connection, session_id: str) -> list[tuple]:
    """Every turn in full: (turn_index, user_message, assistant_response, timestamp).

    Unlike :func:`session_turns`, nothing is truncated — this is what backs
    ``cs read``.
    """
    return conn.execute(
        f"""SELECT turn_index,
                  COALESCE(user_message, ''),
                  COALESCE(assistant_response, ''),
                  COALESCE({optional(conn, 'turns', 'timestamp')}, '')
           FROM turns WHERE session_id = ?
           ORDER BY turn_index""",
        (session_id,),
    ).fetchall()


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def session_checkpoint(conn: sqlite3.Connection, session_id: str) -> dict | None:
    """The session's latest checkpoint — Copilot's own summary of the work.

    Only some sessions have one; returns None when there is nothing recorded.
    """
    if not _has_table(conn, "checkpoints"):
        return None
    row = conn.execute(
        """SELECT COALESCE(overview, ''), COALESCE(work_done, ''),
                  COALESCE(next_steps, ''), COALESCE(important_files, ''),
                  COALESCE(technical_details, ''), checkpoint_number
           FROM checkpoints WHERE session_id = ?
           ORDER BY checkpoint_number DESC, created_at DESC LIMIT 1""",
        (session_id,),
    ).fetchone()
    if not row:
        return None
    keys = ("overview", "work_done", "next_steps", "files", "technical", "number")
    return dict(zip(keys, row, strict=True))


def session_refs(conn: sqlite3.Connection, session_id: str) -> list[tuple]:
    """(ref_type, ref_value) for commits and PRs the session produced."""
    if not _has_table(conn, "session_refs"):
        return []
    return conn.execute(
        """SELECT ref_type, ref_value FROM session_refs
           WHERE session_id = ? ORDER BY ref_type, id""",
        (session_id,),
    ).fetchall()


def session_prompts(conn: sqlite3.Connection, session_id: str) -> list[tuple]:
    """(turn_index, user_message) for every turn — the through-line of a session."""
    return conn.execute(
        """SELECT turn_index, COALESCE(user_message, '')
           FROM turns WHERE session_id = ? AND COALESCE(user_message, '') <> ''
           ORDER BY turn_index""",
        (session_id,),
    ).fetchall()


def session_last_reply(conn: sqlite3.Connection, session_id: str) -> str:
    """The final assistant response — usually where a session says how it ended."""
    row = conn.execute(
        """SELECT assistant_response FROM turns
           WHERE session_id = ? AND COALESCE(assistant_response, '') <> ''
           ORDER BY turn_index DESC LIMIT 1""",
        (session_id,),
    ).fetchone()
    return row[0] if row else ""


# ── Files touched ────────────────────────────────────────────────────
# session_files records every path a session created or edited. Older
# stores may not have it, so each helper degrades to an empty result.

def _has_files(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_files'"
        ).fetchone()
        is not None
    )


def has_files(conn: sqlite3.Connection) -> bool:
    """Whether the store records which files a session touched."""
    return _has_files(conn)


def session_files(conn: sqlite3.Connection, session_id: str) -> list[tuple]:
    """(file_path, tool_name) for one session, created files first."""
    if not _has_files(conn):
        return []
    tool = optional(conn, "session_files", "tool_name")
    return conn.execute(
        f"""SELECT file_path, COALESCE({tool}, '')
           FROM session_files WHERE session_id = ?
           ORDER BY 2 DESC, file_path""",
        (session_id,),
    ).fetchall()


def sessions_for_file(
    conn: sqlite3.Connection, pattern: str, limit: int = 40
) -> tuple[list[tuple], dict[str, tuple[str, str]]]:
    """Sessions that touched a path, newest first, with what they did to it.

    ``pattern`` matches anywhere in the path; ``*`` works as a wildcard, so
    both ``cli.py`` and ``cs/*.py`` find what you would expect.
    """
    if not _has_files(conn):
        return [], {}
    # Stored paths are absolute. A pattern without wildcards matches anywhere
    # in one; a wildcard pattern is floated to the right too unless it starts
    # at the root, so 'cs/*.py' finds /Users/me/proj/cs/cli.py.
    like = pattern.replace("*", "%")
    if "%" not in like:
        like = f"%{like}%"
    elif not like.startswith(("%", "/")):
        like = f"%{like}"
    matches = conn.execute(
        f"""SELECT f.session_id, f.file_path,
                   COALESCE({optional(conn, 'session_files', 'tool_name', 'f')}, '')
            FROM session_files f
            JOIN sessions s ON s.id = f.session_id
            WHERE f.file_path LIKE ?
            ORDER BY MAX(s.created_at, s.updated_at) DESC
            LIMIT {limit * 20}""",
        (like,),
    ).fetchall()

    ordered: list[str] = []
    hits: dict[str, tuple[str, str]] = {}
    for sid, path, tool in matches:
        if sid not in hits:
            hits[sid] = (tool or "touched", path)
            ordered.append(sid)
    ordered = ordered[:limit]
    if not ordered:
        return [], {}

    placeholders = ",".join("?" * len(ordered))
    found = {
        row[0]: row
        for row in conn.execute(
            f"""SELECT {_session_cols(conn)}, {_aiu_sub(conn)} AS nano_aiu
                FROM sessions s WHERE s.id IN ({placeholders})""",
            ordered,
        )
    }
    rows = _given_names([found[sid] for sid in ordered if sid in found],
                        session_names())
    return rows, {sid: hits[sid] for sid in ordered if sid in found}


def repos(conn: sqlite3.Connection, limit: int = 40) -> list[tuple]:
    # GROUP BY 1 rather than by name, so a store with no repository column
    # collapses into the single '(none)' row instead of failing to group.
    return conn.execute(
        f"""SELECT COALESCE({optional(conn, 'sessions', 'repository', 's')}, '(none)') AS repo,
                  COUNT(*) AS sessions,
                  SUM((SELECT COUNT(*) FROM turns t WHERE t.session_id = s.id)) AS total_turns,
                  SUM({_aiu_sub(conn)}) AS nano_aiu,
                  MAX(substr(created_at, 1, 10)) AS last_active
           FROM sessions s
           GROUP BY 1
           ORDER BY sessions DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()


def session_directories(conn: sqlite3.Connection) -> list[str]:
    """Every directory a session has been worked in, busiest first.

    The store already knows where you work, so nothing has to guess at a
    workspace root or sweep the disk looking for checkouts. A directory that
    has never held a session is not one this tool has anything to say about.

    Busiest first because the answer it feeds — which checkout ships a
    skill — can have more than one right answer, and the place you work
    most is the one worth naming.
    """
    return [row[0] for row in conn.execute(
        """SELECT COALESCE(cwd, '') AS directory, COUNT(*) AS sessions
           FROM sessions WHERE COALESCE(cwd, '') <> ''
           GROUP BY directory ORDER BY sessions DESC"""
    )]


def session_places(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """session id -> (directory, repository), for attributing work to a place.

    Both halves, because neither is enough on its own. The repository is what
    you would call the place out loud; the directory is what it actually is,
    and it survives the remote being renamed or re-pointed, which on a long
    lived checkout happens more than once.
    """
    repo = optional(conn, "sessions", "repository", default="''")
    return {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            f"""SELECT id, COALESCE(cwd, ''), COALESCE({repo}, '')
                FROM sessions"""
        )
    }


def session_usage(conn: sqlite3.Connection, session_id: str) -> list[tuple]:
    """Per-model spend for one session: (model, events, nano_aiu). Empty if none."""
    if not _has_usage(conn):
        return []
    return conn.execute(
        """SELECT model, COUNT(*) AS events, COALESCE(SUM(total_nano_aiu), 0) AS nano_aiu
           FROM assistant_usage_events
           WHERE session_id = ?
           GROUP BY model ORDER BY nano_aiu DESC""",
        (session_id,),
    ).fetchall()


def stats(conn: sqlite3.Connection) -> dict:
    def one(sql: str, params: tuple = ()):
        return conn.execute(sql, params).fetchone()

    repo = optional(conn, "sessions", "repository")
    # Sessions that recorded *something* — the same rule `cli._never_used`
    # applies to every listing row, in SQL. A plain COUNT(*) counted the
    # blank rows the CLI writes at launch and then drops everywhere, so the
    # landing strip claimed eight more sessions than `cs all` would list and
    # no view on the menu agreed with the number above it.
    total = one(f"""
        SELECT COUNT(*) FROM sessions s
        WHERE EXISTS (SELECT 1 FROM turns t WHERE t.session_id = s.id)
           OR TRIM(COALESCE({optional(conn, 'sessions', 'summary', 's', "''")},
                            '')) <> ''
           OR {_aiu_sub(conn)} > 0
    """)[0]
    total_turns = one("SELECT COUNT(*) FROM turns")[0]
    repo_count = one(
        f"SELECT COUNT(DISTINCT {repo}) FROM sessions WHERE {repo} IS NOT NULL"
    )[0]
    interactive = one(
        "SELECT COUNT(*) FROM sessions WHERE id IN (SELECT DISTINCT session_id FROM turns)"
    )[0]
    oldest = one("SELECT MIN(substr(created_at,1,10)) FROM sessions")[0]
    newest = one("SELECT MAX(substr(created_at,1,10)) FROM sessions")[0]
    avg_turns = one(
        "SELECT ROUND(AVG(c), 1) FROM (SELECT COUNT(*) c FROM turns GROUP BY session_id)"
    )[0]
    top_repo = one(
        f"""SELECT COALESCE({repo}, '(ad-hoc)'), COUNT(*) c
           FROM sessions GROUP BY 1 ORDER BY c DESC LIMIT 1"""
    )
    busiest_day = one(
        """SELECT substr(created_at,1,10) d, COUNT(*) c
           FROM sessions GROUP BY d ORDER BY c DESC LIMIT 1"""
    )
    total_nano_aiu = 0
    top_model = None
    if _has_usage(conn):
        total_nano_aiu = one(
            "SELECT COALESCE(SUM(total_nano_aiu), 0) FROM assistant_usage_events"
        )[0]
        top_model = one(
            """SELECT model, COALESCE(SUM(total_nano_aiu), 0) n
               FROM assistant_usage_events GROUP BY model ORDER BY n DESC LIMIT 1"""
        )
    return {
        "total": total,
        "interactive": interactive,
        "total_turns": total_turns,
        "avg_turns": avg_turns or 0,
        "repos": repo_count,
        "oldest": oldest,
        "newest": newest,
        "top_repo": top_repo,
        "busiest_day": busiest_day,
        "total_nano_aiu": total_nano_aiu,
        "top_model": top_model,
    }


# ── Who did the work ─────────────────────────────────────────────────
# assistant_usage_events tags every call with an `initiator` (user, agent,
# sub-agent, compaction) and an `agent_id`. That makes delegation and context
# churn measurable per session — signals nothing else in the store carries.

def _has_columns(conn: sqlite3.Connection, table: str, *columns: str) -> bool:
    """Whether a table carries these columns — older stores predate some."""
    try:
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.DatabaseError:
        return False
    return all(column in present for column in columns)


def _usage_window(conn: sqlite3.Connection, days: int) -> tuple[str, tuple]:
    """A WHERE clause limiting usage events to a window — empty if it can't.

    Stores that predate `created_at` on usage events cannot be windowed at
    all; reporting everything is the honest fallback, and callers say so.

    `days <= 0` means every event ever recorded, the same convention
    `recent_sessions` uses — one spelling of "all of time" across the module,
    rather than a very large number that quietly expires the oldest rows.
    """
    if days > 0 and _has_columns(conn, "assistant_usage_events", "created_at"):
        return "WHERE created_at >= datetime('now', ?)", (f"-{days} days",)
    return "", ()


def usage_is_windowable(conn: sqlite3.Connection) -> bool:
    return _has_columns(conn, "assistant_usage_events", "created_at")


def has_delegation(conn: sqlite3.Connection) -> bool:
    """Whether this store tags calls with who initiated them."""
    return _has_usage(conn) and _has_columns(
        conn, "assistant_usage_events", "initiator", "agent_id"
    )


def session_work_split(conn: sqlite3.Connection, session_id: str) -> dict:
    """Calls and spend per initiator for one session, plus sub-agent count."""
    if not has_delegation(conn):
        return {}
    rows = conn.execute(
        """SELECT COALESCE(initiator, 'unknown'), COUNT(*),
                  COALESCE(SUM(total_nano_aiu), 0)
           FROM assistant_usage_events WHERE session_id = ?
           GROUP BY 1""",
        (session_id,),
    ).fetchall()
    # agent_id holds the delegating tool-call id (`toolu_…`), unique to one
    # session — so this counts delegated TASKS, not distinct agent identities.
    # The store records no agent name anywhere.
    delegated_tasks = conn.execute(
        """SELECT COUNT(DISTINCT agent_id) FROM assistant_usage_events
           WHERE session_id = ? AND COALESCE(agent_id, '') <> ''
             AND initiator = 'sub-agent'""",
        (session_id,),
    ).fetchone()[0]
    return {
        "by_initiator": {name: (calls, nano) for name, calls, nano in rows},
        "calls": sum(calls for _, calls, _ in rows),
        "delegated_tasks": delegated_tasks,
    }


def work_split(conn: sqlite3.Connection, days: int) -> dict:
    """The same split across a window, with the sessions that delegate most."""
    if not has_delegation(conn):
        return {}
    if not usage_is_windowable(conn):
        return {}
    where, window = _usage_window(conn, days)
    rows = conn.execute(
        f"""SELECT COALESCE(initiator, 'unknown'), COUNT(*),
                   COALESCE(SUM(total_nano_aiu), 0),
                   COUNT(DISTINCT session_id)
            FROM assistant_usage_events
            {where}
            GROUP BY 1 ORDER BY 3 DESC""",
        window,
    ).fetchall()
    top = conn.execute(
        f"""SELECT u.session_id,
                   COALESCE(NULLIF(s.summary, ''), '(untitled)'),
                  SUM(CASE WHEN u.initiator = 'sub-agent' THEN 1 ELSE 0 END) AS delegated,
                  COUNT(DISTINCT CASE WHEN u.initiator = 'sub-agent'
                                      THEN u.agent_id END) AS tasks,
                  COALESCE(SUM(u.total_nano_aiu), 0)
            FROM assistant_usage_events u
            LEFT JOIN sessions s ON s.id = u.session_id
            {where.replace("created_at", "u.created_at")}
            GROUP BY u.session_id HAVING delegated > 0
            ORDER BY delegated DESC LIMIT 8""",
        window,
    ).fetchall()
    totals = conn.execute(
        f"""SELECT COUNT(DISTINCT session_id),
                   COUNT(DISTINCT CASE WHEN initiator = 'sub-agent' THEN agent_id END)
            FROM assistant_usage_events {where}""",
        window,
    ).fetchone()
    return {
        "by_initiator": rows,
        "top_delegating": top,
        "sessions": totals[0],
        "delegated_tasks": totals[1],
    }


_ASSET_REFERENCE = re.compile(
    # Only strong evidence counts: the asset's own path, its filename, or the
    # word skill/agent next to the name. Weaker forms were tried and dropped —
    # skills are called `commit`, `status`, `plan`, so a backticked `commit`
    # is usually git and `/commit` is usually a path. This undercounts by
    # design: a number that is too high is worse than one that is conservative.
    r"(?:skills?/|agents?/)([\w-]+)"
    r"|\b([\w-]+)\.(?:skill|agent)\.md\b"
    r"|\b([\w-]+)[ \t]+(?:skill|agent)\b"
    r"|\b(?:skill|agent)[ \t]+([\w-]+)\b",
    re.IGNORECASE,
)


_MCP_REFERENCE = re.compile(
    # The same rule as _ASSET_REFERENCE, applied to the ways an MCP server
    # gets named. The first form is the one worth having: Copilot writes an
    # MCP tool call as `mcp__atlassian__search`, so the server's own name is
    # in the tool name whenever a transcript quotes one. The rest are how a
    # person writes it — 'the atlassian MCP', 'mcp server snyk'. A bare name
    # never counts: servers are called `github`, `figma`, `notion`, and those
    # words appear in sessions that never called a server in their life.
    r"mcp__([\w-]+?)__"
    r"|\bmcp[/.]([\w-]+)"
    r"|\b([\w-]+)[ \t]+mcp\b"
    r"|\bmcp[ \t]+(?:server[ \t]+)?([\w-]+)\b",
    re.IGNORECASE,
)


def _mcp_hits(text: str):
    """Every name used *as an MCP server* in `text`, lowercased."""
    for match in _MCP_REFERENCE.finditer(text):
        yield next(group for group in match.groups() if group).lower()


def _asset_hits(text: str):
    """Every name used *as* an asset in `text`, lowercased.

    Matching the shape once and looking the name up beats building an
    alternation of every known name: that was four copies of a 125-way
    alternation and took 4.7s over this store, against 0.8s here.

    It is also stricter, because the name it captures is the whole token.
    The alternation ended at a word boundary, so `agents/mule-triage` counted
    as the `mule` asset and `agents/HANDOVER-deckforge` as `handover` — the
    same false positive its own comment warned about for `workflow`.
    """
    for match in _ASSET_REFERENCE.finditer(text):
        yield next(group for group in match.groups() if group).lower()


def sessions_for_asset(
    conn: sqlite3.Connection, name: str, limit: int = 20, hits=_asset_hits
) -> list[tuple]:
    """(session_id, summary, last_active) for sessions referencing one asset.

    `hits` is what counts as a reference — the asset rule by default, and
    `_mcp_hits` for a server. The scan is the same either way, so the two
    inventories cannot drift apart in how they read a transcript.
    """
    wanted = name.lower()
    seen: dict[str, None] = {}
    if hits is _asset_hits:
        for session_id, loaded in skills_invoked_by_session(conn).items():
            if wanted in loaded:
                seen[session_id] = None
    for session_id, text in conn.execute(
        """SELECT session_id,
                  COALESCE(user_message, '') || ' ' || COALESCE(assistant_response, '')
           FROM turns"""
    ):
        if session_id not in seen and any(hit == wanted for hit in hits(text)):
            seen[session_id] = None
    if not seen:
        return []
    ids = list(seen)[: limit * 4]
    placeholders = ",".join("?" * len(ids))
    return conn.execute(
        f"""SELECT id, COALESCE(NULLIF(summary,''), '(untitled)'),
                   substr(MAX(created_at, updated_at), 1, 16) AS last_active
            FROM sessions WHERE id IN ({placeholders})
            ORDER BY last_active DESC LIMIT ?""",
        (*ids, limit),
    ).fetchall()


def reference_counts(
    conn: sqlite3.Connection, names: list[str], hits=_asset_hits
) -> dict[str, int]:
    """Sessions referencing each name, in one pass over the turns.

    The Copilot CLI store records no invocation event, so usage has to be
    inferred from text — but a bare word match is worthless when skills are
    called `commit`, `plan` or `status`. Only qualified forms count: a path
    (`skills/commit`), a slash command, a backticked name, or the word
    'skill'/'agent' beside it. It is a usage signal, never a call count.

    One pass, one alternation. A per-name query needed a full scan per
    pattern (125 skills × 8 patterns ≈ 6.5s here) and was less accurate: its
    LIKE had no word boundary, so `.github/workflows/` counted as the
    `workflow` skill.

    `hits` says what a reference looks like: the asset rule by default, and
    `_mcp_hits` when the names are MCP servers.
    """
    from collections import defaultdict

    if not names:
        return {}
    known = {name.lower() for name in names}
    seen: dict[str, set[str]] = defaultdict(set)
    for session_id, text in conn.execute(
        """SELECT session_id,
                  COALESCE(user_message, '') || ' ' || COALESCE(assistant_response, '')
           FROM turns"""
    ):
        for hit in hits(text):
            if hit in known:
                seen[hit].add(session_id)
    # A load marker is stronger evidence than anything the text scan can
    # find, and it does not always come with a mention the scan would catch:
    # the CLI writes `<skill-context name="x">` and the skill's body, which
    # need never contain the qualified forms above. Folding the two together
    # is what stops a skill that demonstrably ran from being filed under
    # "never referenced" — which it was, and which was the worst kind of
    # wrong: confidently, about the one case there is proof for.
    if hits is _asset_hits:
        for session_id, loaded in skills_invoked_by_session(conn).items():
            for name in loaded & known:
                seen[name].add(session_id)
    return {name: len(seen.get(name.lower(), ())) for name in names}


# Every branch of _ASSET_REFERENCE needs one of these literals present, so a
# row without them cannot match and never has to reach Python. On a large
# store most turns mention neither, and letting SQLite discard them in C is
# the difference between a landing screen that opens and one that pauses.
_ASSET_KEYWORDS = ("skill", "agent")
_MCP_KEYWORDS = ("mcp",)


def assets_by_session(
    conn: sqlite3.Connection, names: list[str], hits=_asset_hits,
    keywords: tuple[str, ...] = _ASSET_KEYWORDS,
) -> dict[str, int]:
    """How many distinct assets from `names` each session references.

    :func:`reference_counts` answers "how many sessions used this skill".
    This is the same scan asked the other way round — "how many skills did
    this session use" — because a listing needs a number per row, not per
    asset, and reading the turns twice to get both would be one scan too
    many on a store of any size.

    Same conservative rule, and the same caveat: it is a reference signal
    read out of the transcript, never a call count. Sessions with no hits
    are simply absent, so the caller reads a missing row as zero.
    """
    from collections import defaultdict

    if not names:
        return {}
    clause = " OR ".join(
        "user_message LIKE ? OR assistant_response LIKE ?" for _ in keywords
    )
    args = [f"%{word}%" for word in keywords for _ in range(2)]
    known = {name.lower() for name in names}
    seen: dict[str, set[str]] = defaultdict(set)
    for session_id, text in conn.execute(
        f"""SELECT session_id,
                   COALESCE(user_message, '') || ' ' || COALESCE(assistant_response, '')
            FROM turns WHERE {clause}""",
        args,
    ):
        for hit in hits(text):
            if hit in known:
                seen[session_id].add(hit)
    if hits is _asset_hits:
        # The recorded half, folded in for the same reason as in
        # :func:`reference_counts`: a load marker is proof, and proof must
        # not lose to a regex that happened not to fire.
        for session_id, loaded in skills_invoked_by_session(conn).items():
            seen[session_id] |= loaded & known
    return {session_id: len(found) for session_id, found in seen.items()}


def assets_used(
    conn: sqlite3.Connection, names: list[str], hits=_asset_hits,
    keywords: tuple[str, ...] = _ASSET_KEYWORDS,
) -> int:
    """How many of `names` were referenced anywhere in the store at all.

    The inventory number's other half: `len(names)` is what is installed,
    and this is what has ever actually been reached for. The gap between
    them is the interesting part — kit nobody uses is kit that is quietly
    going stale, and on most machines the gap is most of the shelf.
    """
    if not names:
        return 0
    clause = " OR ".join(
        "user_message LIKE ? OR assistant_response LIKE ?" for _ in keywords
    )
    args = [f"%{word}%" for word in keywords for _ in range(2)]
    known = {name.lower() for name in names}
    found: set[str] = set()
    if hits is _asset_hits:
        # Cheap and certain, so it goes first — on a store where everything
        # was loaded properly this can satisfy the early exit below without
        # reading a single turn.
        for loaded in skills_invoked_by_session(conn).values():
            found |= loaded & known
    for (text,) in conn.execute(
        f"""SELECT COALESCE(user_message, '') || ' ' || COALESCE(assistant_response, '')
            FROM turns WHERE {clause}""",
        args,
    ):
        found.update(hit for hit in hits(text) if hit in known)
        if len(found) == len(known):
            break  # nothing left to find
    return len(found)


def subagents_by_session(conn: sqlite3.Connection) -> dict[str, int]:
    """Distinct sub-agents each session launched, from the usage records.

    Unlike the skill count above, this one is *exact*. Every model call the
    store bills carries the tool call id of the sub-agent that made it in
    `agent_id` — null when the main agent made it — so counting the distinct
    non-null ids counts the sub-agents that actually ran. Nothing is
    inferred from text, and a session that merely discussed an agent is not
    counted as having run one.

    Stores without usage records get an empty map, which every caller reads
    as "not recorded" rather than "none".
    """
    if not _has_usage(conn):
        return {}
    if not _has_columns(conn, "assistant_usage_events", "agent_id"):
        return {}
    return {
        session_id: count
        for session_id, count in conn.execute(
            """SELECT session_id, COUNT(DISTINCT agent_id)
               FROM assistant_usage_events
               WHERE agent_id IS NOT NULL AND agent_id <> ''
               GROUP BY session_id"""
        )
    }


def subagent_runs(conn: sqlite3.Connection, session_id: str) -> int:
    """How many sub-agents one session launched — 0 when none or unrecorded."""
    return subagents_by_session(conn).get(session_id, 0)


def subagent_detail(conn: sqlite3.Connection, session_id: str) -> list[tuple]:
    """Each sub-agent this session ran, with what it was and what it cost.

    The store does not record what a sub-agent was *called*. `agent_id` is
    the id of the tool call that launched it and nothing else — no name, no
    profile, no description — so a view that promises to name them would be
    promising something the data cannot keep.

    What the data *can* say turns out to be the more useful half anyway: the
    model it ran on, the turn it was launched from, how many calls it made
    and what it spent. That is enough to tell a cheap lookup apart from a
    twenty-minute research run, which is the distinction anyone reviewing a
    session actually needs. The id comes along as a short prefix so two runs
    on the same model in the same turn can still be told apart.

    Returned newest-launched last, so the list reads in the order the work
    happened. Each row is:

        (short_id, model, calls, nano_aiu, duration_ms, first_turn, last_turn)
    """
    if not _has_usage(conn):
        return []
    if not _has_columns(conn, "assistant_usage_events", "agent_id"):
        return []
    nano = optional(conn, "assistant_usage_events", "total_nano_aiu")
    duration = optional(conn, "assistant_usage_events", "duration_ms")
    turn = optional(conn, "assistant_usage_events", "turn_index", default="NULL")
    rows = conn.execute(
        f"""SELECT agent_id, model, COUNT(*), SUM({nano}), SUM({duration}),
                   MIN({turn}), MAX({turn})
            FROM assistant_usage_events
            WHERE session_id = ? AND agent_id IS NOT NULL AND agent_id <> ''
            GROUP BY agent_id, model
            ORDER BY MIN(id)""",
        (session_id,),
    ).fetchall()
    return [
        (_short_agent(agent_id), model, calls, nano_aiu or 0, ms or 0, first, last)
        for agent_id, model, calls, nano_aiu, ms, first, last in rows
    ]


def _short_agent(agent_id: str) -> str:
    """A tool call id, cut to something a person can compare at a glance.

    `toolu_011TnfL3pBRmhQ9g8Pd1B9dH` is not a name and never will be, but
    its last six characters are enough to tell two runs apart in one session
    — which is the only job this identifier has here.
    """
    return agent_id[-6:] if len(agent_id) > 6 else agent_id


_SKILL_INVOCATION = re.compile(r'<skill-context\s+name="([^"]+)"', re.IGNORECASE)


def skills_invoked(conn: sqlite3.Connection, session_id: str) -> dict[str, int]:
    """Skills the CLI actually loaded, and the turn it loaded them in.

    Everything else in this module that reports skill usage is an inference
    off transcript text, for the reason repeated all over this file: the
    store holds no invocation table. It turns out it does not need one. When
    the Copilot CLI loads a skill it injects the skill's own body into the
    turn wrapped in a marker of its own making:

        <skill-context name="okr-planning">

    That is written by the CLI, not by a person, and it is written *because*
    the skill ran. A name found this way is not a guess that survived a
    careful regex — it is a record, and it is reported as one.

    The inference is still worth keeping alongside it: a session that
    discusses a skill without loading it is a real thing to know about, and
    older sessions predate the marker. But the two must never be shown as
    the same claim, which is why they come back separately.
    """
    found: dict[str, int] = {}
    for index, text in conn.execute(
        """SELECT turn_index, COALESCE(user_message, '')
           FROM turns
           WHERE session_id = ? AND user_message LIKE '%<skill-context%'
           ORDER BY turn_index""",
        (session_id,),
    ):
        for name in _SKILL_INVOCATION.findall(text or ""):
            found.setdefault(name.strip().lower(), index or 0)
    return found


def skills_invoked_by_session(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """The same, store-wide — every session that actually loaded a skill.

    The `LIKE` is doing real work here: the marker is a literal string, so
    SQLite can skip all but a handful of rows rather than handing the whole
    transcript to Python for a regex it will almost never match.
    """
    out: dict[str, set[str]] = {}
    for session_id, text in conn.execute(
        """SELECT session_id, COALESCE(user_message, '')
           FROM turns WHERE user_message LIKE '%<skill-context%'"""
    ):
        for name in _SKILL_INVOCATION.findall(text or ""):
            out.setdefault(session_id, set()).add(name.strip().lower())
    return out


def asset_evidence(
    conn: sqlite3.Connection, session_id: str, names: list[str],
    hits=_asset_hits, span: int = 60,
) -> list[tuple[str, int, str, str]]:
    """Where each referenced asset was named, in what words, and how surely.

    Most skill usage in `cs` is *inferred*: for sessions with no invocation
    marker the only evidence there has ever been is that the text named the
    thing. A number derived that way is worth exactly as much as its ability
    to be checked, and until now it could not be — the view said "2 skills"
    and left you to take its word for it.

    So this returns the first place each name appears and the words around
    it, which turns an assertion into something a reader can agree or
    disagree with. Seeing `deploy` matched from "run the deploy skill" is a
    confirmation; seeing it matched from a sentence about deploying on
    Friday is a false positive you can now catch.

    Where the CLI left its own `<skill-context>` marker it is not an
    inference at all, and that row says `ran` rather than `named`. Rows come
    back certainty-first:

        (name, turn_index, quote, "ran" | "named")
    """
    if not names:
        return []
    lookup = {name.lower(): name for name in names}
    found: dict[str, tuple[str, int, str, str]] = {}

    # Certainty first, so a skill that demonstrably ran is never demoted to
    # a mention by an earlier, weaker match on the same name.
    for key, index in skills_invoked(conn, session_id).items():
        name = lookup.get(key)
        if name:
            found[name] = (name, index, "loaded by the CLI into this turn", "ran")

    for index, text in conn.execute(
        """SELECT turn_index,
                  COALESCE(user_message, '') || ' ' || COALESCE(assistant_response, '')
           FROM turns WHERE session_id = ? ORDER BY turn_index""",
        (session_id,),
    ):
        for match in _ASSET_REFERENCE.finditer(text or ""):
            hit = next(group for group in match.groups() if group).lower()
            name = lookup.get(hit)
            if not name or name in found:
                continue
            start = max(0, match.start() - span // 2)
            quote = " ".join((text[start:match.end() + span]).split())
            # Cut back to a word boundary at the front. A quote that opens
            # mid-token ("…t | | **Skill**") reads as damage rather than as
            # context, and the two or three characters it costs to start
            # cleanly are the cheapest legibility there is.
            if start and " " in quote[:20]:
                quote = quote[quote.index(" ") + 1:]
            found[name] = (name, index or 0, quote, "named")
    return sorted(found.values(), key=lambda row: (row[3] != "ran", row[1]))


def mcp_reference_counts(
    conn: sqlite3.Connection, names: list[str]
) -> dict[str, int]:
    """Sessions referencing each MCP server, by the same one-pass scan.

    The store records no MCP invocation event any more than it records a
    skill invocation, so this is a usage signal read out of the transcript,
    never a call count.
    """
    return reference_counts(conn, names, _mcp_hits)


def sessions_for_mcp(
    conn: sqlite3.Connection, name: str, limit: int = 20
) -> list[tuple]:
    """(session_id, summary, last_active) for sessions naming one server."""
    return sessions_for_asset(conn, name, limit, _mcp_hits)


def sessions_referencing(conn: sqlite3.Connection, name: str) -> int:
    """Sessions that reference an asset *as* an asset.

    The Copilot CLI store records no invocation event, so usage has to be
    inferred from text — but a bare word match is worthless when skills are
    called `commit`, `plan` or `status`. Only qualified forms count: a path
    (`skills/commit`), a slash command (`/commit`), a backticked name, or the
    word 'skill'/'agent' adjacent to it. On a real store this cut `commit`
    from 259 sessions to 54, which is the difference between a number and a
    guess. It is still a signal, never a call count.
    """
    patterns = [
        f"%skills/{name}%",
        f"%agents/{name}%",
        f"%/{name}%",
        f"%`{name}`%",
        f"%{name} skill%",
        f"%skill {name}%",
        f"%{name} agent%",
        f"%agent {name}%",
    ]
    clause = " OR ".join(
        ["user_message LIKE ? OR assistant_response LIKE ?"] * len(patterns)
    )
    args = [pattern for pattern in patterns for _ in range(2)]
    return conn.execute(
        f"SELECT COUNT(DISTINCT session_id) FROM turns WHERE {clause}", args
    ).fetchone()[0]


# ── Spend & performance ──────────────────────────────────────────────
# All read from assistant_usage_events, which older stores lack entirely —
# every helper returns an empty result there rather than raising.

def has_usage(conn: sqlite3.Connection) -> bool:
    return _has_usage(conn)


def _percentile(values: list[float], share: float) -> float | None:
    """The value `share` of the way through a sorted sample — None if empty.

    Nearest-rank rather than interpolated: a p95 that is a latency the store
    actually recorded is one you can go and look at, and with a few hundred
    calls in a window the difference between the two methods is noise.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil(share * len(ordered)) - 1))
    return ordered[rank]


def efficiency(conn: sqlite3.Connection, days: int) -> dict:
    """How well the spend was *spent* — the readings behind ``cs efficiency``.

    Everything here is derived from `assistant_usage_events`, which records a
    row per model call. The cost view answers "what did this cost"; these are
    the four questions that decide whether that number could have been
    smaller, and each is a lever someone can actually pull:

    * **Cache hit rate** — `cache_read / (input + cache_read + cache_write)`,
      the definition Claude Code's telemetry uses, so the number means the
      same thing here as on any dashboard. Cached input is the cheapest
      input there is; a low rate on long sessions is money left on the table.
    * **Multiplier mix** — calls are billed at a rate multiplier, and a
      premium model doing cheap work is the most common way a bill runs away
      without anyone noticing.
    * **First-token latency** — p50 and p95, because the mean hides exactly
      the tail that makes a tool feel slow.
    * **Reasoning share** — reasoning tokens as a fraction of output, next to
      the effort settings that produced them.

    Returns `{}` when the store keeps no usage records at all, and leaves out
    any individual reading whose column this store predates — an absent
    column is a missing answer, not a broken report.
    """
    if not _has_usage(conn):
        return {}
    where, args = _usage_window(conn, days)
    out: dict = {"windowed": bool(where)}
    # Each reading is guarded by the columns it actually reads, and an absent
    # one degrades to a literal rather than removing the whole section: a
    # store that never recorded cache writes can still report a hit rate, it
    # just reports one with nothing in the 'written' half.
    write_col = optional(conn, "assistant_usage_events", "cache_write_tokens",
                         default="0")
    reasoning_col = optional(conn, "assistant_usage_events", "reasoning_tokens",
                             default="0")

    if _has_columns(conn, "assistant_usage_events",
                    "input_tokens", "cache_read_tokens"):
        fresh, read, written, output, reasoning = conn.execute(
            f"""SELECT COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(cache_read_tokens), 0),
                       COALESCE(SUM({write_col}), 0),
                       COALESCE(SUM(output_tokens), 0),
                       COALESCE(SUM({reasoning_col}), 0)
                FROM assistant_usage_events {where}""",
            args,
        ).fetchone()
        offered = fresh + read + written
        out["cache"] = {
            "read": read, "written": written, "fresh": fresh,
            "output": output, "reasoning": reasoning,
            # None rather than 0 for a window with no input at all: "no calls"
            # and "nothing was cached" are different answers.
            "hit_rate": (read / offered) if offered else None,
            "reasoning_share": (reasoning / output) if output else None,
        }

    if _has_columns(conn, "assistant_usage_events", "request_multiplier"):
        out["multipliers"] = conn.execute(
            f"""SELECT COALESCE(request_multiplier, 1),
                       COUNT(*),
                       COALESCE(SUM(total_nano_aiu), 0)
                FROM assistant_usage_events {where}
                GROUP BY 1 ORDER BY 1 DESC""",
            args,
        ).fetchall()

    if _has_columns(conn, "assistant_usage_events", "time_to_first_token_ms"):
        ttft = [
            value
            for (value,) in conn.execute(
                f"""SELECT time_to_first_token_ms FROM assistant_usage_events
                    {where}{' AND' if where else 'WHERE'} time_to_first_token_ms > 0""",
                args,
            )
        ]
        out["latency"] = {
            "calls": len(ttft),
            "p50": _percentile(ttft, 0.50),
            "p95": _percentile(ttft, 0.95),
        }

    if _has_columns(conn, "assistant_usage_events", "reasoning_effort"):
        out["effort"] = conn.execute(
            f"""SELECT COALESCE(NULLIF(reasoning_effort, ''), '(default)'), COUNT(*)
                FROM assistant_usage_events {where}
                GROUP BY 1 ORDER BY 2 DESC""",
            args,
        ).fetchall()

    if _has_columns(conn, "assistant_usage_events", "finish_reason"):
        out["finish"] = conn.execute(
            f"""SELECT COALESCE(NULLIF(finish_reason, ''), '(none)'), COUNT(*)
                FROM assistant_usage_events {where}
                GROUP BY 1 ORDER BY 2 DESC""",
            args,
        ).fetchall()

    if _has_columns(conn, "assistant_usage_events", "model", "cache_read_tokens",
                    "input_tokens", "time_to_first_token_ms"):
        out["by_model"] = conn.execute(
            f"""SELECT model,
                       COUNT(*),
                       COALESCE(SUM(total_nano_aiu), 0) AS nano,
                       COALESCE(SUM(cache_read_tokens), 0),
                       COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(cache_read_tokens), 0)
                           + COALESCE(SUM({write_col}), 0),
                       AVG(NULLIF(time_to_first_token_ms, 0))
                FROM assistant_usage_events {where}
                GROUP BY model ORDER BY nano DESC""",
            args,
        ).fetchall()

    return out


def cost_totals(conn: sqlite3.Connection, days: int) -> dict:
    if not _has_usage(conn):
        return {}
    where, args = _usage_window(conn, days)
    if not _has_columns(
        conn, "assistant_usage_events", "input_tokens", "duration_ms", "finish_reason"
    ):
        # Spend is still knowable without the token detail.
        row = conn.execute(
            f"""SELECT COALESCE(SUM(total_nano_aiu), 0), COUNT(*),
                       COUNT(DISTINCT session_id)
                FROM assistant_usage_events {where}""",
            args,
        ).fetchone()
        return {
            "nano_aiu": row[0], "calls": row[1], "sessions": row[2],
            "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "reasoning_tokens": 0, "duration_ms": 0,
        }
    row = conn.execute(
        f"""SELECT COALESCE(SUM(total_nano_aiu), 0),
                   COUNT(*),
                   COUNT(DISTINCT session_id),
                  COALESCE(SUM(input_tokens), 0),
                  COALESCE(SUM(output_tokens), 0),
                  COALESCE(SUM(cache_read_tokens), 0),
                  COALESCE(SUM(reasoning_tokens), 0),
                  COALESCE(SUM(duration_ms), 0)
            FROM assistant_usage_events {where}""",
        args,
    ).fetchone()
    keys = (
        "nano_aiu", "calls", "sessions", "input_tokens", "output_tokens",
        "cache_read_tokens", "reasoning_tokens", "duration_ms",
    )
    return dict(zip(keys, row, strict=True))


def cost_by_model(conn: sqlite3.Connection, days: int) -> list[tuple]:
    """(model, calls, nano_aiu, avg_duration_ms, avg_ttft_ms) — dearest first."""
    if not _has_usage(conn):
        return []
    where, args = _usage_window(conn, days)
    if not _has_columns(
        conn, "assistant_usage_events", "duration_ms", "time_to_first_token_ms"
    ):
        return [
            (model, calls, nano, None, None)
            for model, calls, nano in conn.execute(
                f"""SELECT model, COUNT(*), COALESCE(SUM(total_nano_aiu), 0) AS nano
                    FROM assistant_usage_events {where}
                    GROUP BY model ORDER BY nano DESC""",
                args,
            )
        ]
    return conn.execute(
        f"""SELECT model,
                   COUNT(*) AS calls,
                  COALESCE(SUM(total_nano_aiu), 0) AS nano_aiu,
                  AVG(duration_ms),
                  AVG(time_to_first_token_ms)
            FROM assistant_usage_events {where}
            GROUP BY model ORDER BY nano_aiu DESC""",
        args,
    ).fetchall()


def cost_by_repo(conn: sqlite3.Connection, days: int, limit: int = 10) -> list[tuple]:
    """(repo_or_dir, sessions, nano_aiu) — dearest first."""
    if not _has_usage(conn):
        return []
    where, args = _usage_window(conn, days)
    return conn.execute(
        f"""SELECT COALESCE(NULLIF({optional(conn, 'sessions', 'repository', 's', "''")}, ''),
                            NULLIF({optional(conn, 'sessions', 'cwd', 's', "''")}, ''),
                            '(none)') AS place,
                   COUNT(DISTINCT u.session_id) AS sessions,
                   COALESCE(SUM(u.total_nano_aiu), 0) AS nano_aiu
            FROM assistant_usage_events u
            JOIN sessions s ON s.id = u.session_id
            {where.replace('created_at', 'u.created_at')}
            GROUP BY place ORDER BY nano_aiu DESC LIMIT ?""",
        (*args, limit),
    ).fetchall()


def cost_by_day(conn: sqlite3.Connection, days: int) -> list[tuple]:
    """(day, nano_aiu, calls) in date order."""
    if not _has_usage(conn) or not usage_is_windowable(conn):
        return []  # no timestamps, no per-day series
    where, args = _usage_window(conn, days)
    return conn.execute(
        f"""SELECT substr(created_at, 1, 10) AS day,
                   COALESCE(SUM(total_nano_aiu), 0) AS nano_aiu,
                   COUNT(*) AS calls
            FROM assistant_usage_events {where}
            GROUP BY day ORDER BY day""",
        args,
    ).fetchall()


def cost_top_sessions(conn: sqlite3.Connection, days: int, limit: int = 5) -> list[tuple]:
    """(session_id, summary, nano_aiu) for the dearest sessions in range."""
    if not _has_usage(conn):
        return []
    where, args = _usage_window(conn, days)
    return conn.execute(
        f"""SELECT u.session_id,
                   COALESCE(NULLIF(s.summary, ''), '(untitled)'),
                   COALESCE(SUM(u.total_nano_aiu), 0) AS nano_aiu
            FROM assistant_usage_events u
            LEFT JOIN sessions s ON s.id = u.session_id
            {where.replace('created_at', 'u.created_at')}
            GROUP BY u.session_id ORDER BY nano_aiu DESC LIMIT ?""",
        (*args, limit),
    ).fetchall()


def impact(conn: sqlite3.Connection, days: int | None = None) -> dict:
    """Everything the store can say about what the work produced.

    `days` narrows the window; None covers the whole store. Sections absent
    from an older store come back as zeros rather than failing the report.
    """
    # Coerced because it is interpolated into SQL rather than bound: callers
    # are trusted today, but the function should not depend on that.
    days = int(days) if days else None
    scope = f"WHERE created_at >= datetime('now', '-{days} days')" if days else ""
    repo = optional(conn, "sessions", "repository")

    def one(sql: str, default=0):
        try:
            row = conn.execute(sql).fetchone()
        except sqlite3.DatabaseError:
            return default
        return (row[0] if row and row[0] is not None else default)

    out: dict = {
        "sessions": one(f"SELECT COUNT(*) FROM sessions {scope}"),
        "repos": one(
            f"SELECT COUNT(DISTINCT {repo}) FROM sessions {scope}"
            f"{' AND' if scope else ' WHERE'} COALESCE({repo},'') <> ''"
        ),
        "days_active": one(f"SELECT COUNT(DISTINCT substr(created_at,1,10)) FROM sessions {scope}"),
        "turns": one(
            "SELECT COUNT(*) FROM turns"
            + (
                f" WHERE session_id IN (SELECT id FROM sessions {scope})"
                if scope
                else ""
            )
        ),
        "commits": 0, "unique_commits": 0, "prs": 0, "unique_prs": 0,
        "files_created": 0, "files_edited": 0, "checkpoints": 0,
        "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
        "reasoning_tokens": 0, "model_hours": 0.0, "nano_aiu": 0,
        "delegated_calls": 0, "delegated_tasks": 0, "compactions": 0,
    }

    session_scope = (
        f"WHERE session_id IN (SELECT id FROM sessions {scope})" if scope else ""
    )
    if _has_table(conn, "session_refs"):
        for kind, total, unique in conn.execute(
            f"""SELECT ref_type, COUNT(*), COUNT(DISTINCT ref_value)
                FROM session_refs {session_scope} GROUP BY ref_type"""
        ):
            if kind == "commit":
                out["commits"], out["unique_commits"] = total, unique
            elif kind == "pr":
                out["prs"], out["unique_prs"] = total, unique
    if _has_files(conn):
        for tool, total in conn.execute(
            f"SELECT COALESCE({optional(conn, 'session_files', 'tool_name')},''), "
            f"COUNT(*) FROM session_files "
            f"{session_scope} GROUP BY 1"
        ):
            if tool == "create":
                out["files_created"] = total
            else:
                out["files_edited"] += total
    if _has_table(conn, "checkpoints"):
        out["checkpoints"] = one(f"SELECT COUNT(*) FROM checkpoints {session_scope}")
    if _has_usage(conn):
        usage_scope = (
            f"WHERE created_at >= datetime('now', '-{days} days')"
            if days and usage_is_windowable(conn)
            else ""
        )
        # Older stores record spend but none of the token detail. Select only
        # what exists, so the report loses a line instead of failing.
        detailed = _has_columns(
            conn, "assistant_usage_events",
            "input_tokens", "output_tokens", "cache_read_tokens",
            "reasoning_tokens", "duration_ms",
        )
        columns = (
            """COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),
               COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(reasoning_tokens),0),
               COALESCE(SUM(duration_ms),0), COALESCE(SUM(total_nano_aiu),0)"""
            if detailed
            else "0, 0, 0, 0, 0, COALESCE(SUM(total_nano_aiu),0)"
        )
        row = conn.execute(
            f"SELECT {columns} FROM assistant_usage_events {usage_scope}"
        ).fetchone()
        (
            out["input_tokens"], out["output_tokens"], out["cache_read_tokens"],
            out["reasoning_tokens"], duration_ms, out["nano_aiu"],
        ) = row
        out["model_hours"] = round(duration_ms / 3_600_000, 1)
        if has_delegation(conn):
            extra = conn.execute(
                f"""SELECT
                      SUM(CASE WHEN initiator='sub-agent' THEN 1 ELSE 0 END),
                      COUNT(DISTINCT CASE WHEN initiator='sub-agent' THEN agent_id END),
                      SUM(CASE WHEN initiator='compaction' THEN 1 ELSE 0 END)
                    FROM assistant_usage_events {usage_scope}"""
            ).fetchone()
            out["delegated_calls"] = extra[0] or 0
            out["delegated_tasks"] = extra[1] or 0
            out["compactions"] = extra[2] or 0
    return out


def top_repos_by_output(conn: sqlite3.Connection, limit: int = 5) -> list[tuple]:
    """(repo, commits+PRs, sessions) — where the work actually landed."""
    if not _has_table(conn, "session_refs"):
        return []
    return conn.execute(
        f"""SELECT COALESCE(NULLIF({optional(conn, 'sessions', 'repository', 's', "''")}, ''),
                           '(ad-hoc)') AS repo,
                  COUNT(*) AS refs, COUNT(DISTINCT r.session_id) AS sessions
           FROM session_refs r JOIN sessions s ON s.id = r.session_id
           GROUP BY repo ORDER BY refs DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def activity(conn: sqlite3.Connection, days: int) -> list[int]:
    """Sessions started on each of the last `days` days, oldest first.

    Dense, unlike `timeline`: a day with nothing still gets its zero. A
    sparkline built from the sparse series would close the gaps up and draw a
    busy fortnight and a quiet quarter as the same shape.
    """
    from datetime import date, timedelta

    if days < 1:
        return []
    counted = dict(
        conn.execute(
            """SELECT substr(created_at, 1, 10) AS day, COUNT(*)
               FROM sessions
               WHERE created_at >= datetime('now', ?)
               GROUP BY day""",
            (f"-{days} days",),
        )
    )
    today = date.today()
    return [
        counted.get((today - timedelta(days=offset)).isoformat(), 0)
        for offset in range(days - 1, -1, -1)
    ]


def timeline(conn: sqlite3.Connection, days: int) -> list[tuple]:
    """(day, sessions, turns, nano_aiu) in date order.

    `days <= 0` covers the whole store.

    Sessions alone used to be the whole row, and sessions alone is the
    textbook vanity metric: opening twelve sessions is not twelve times the
    work of opening one, and a day spent in a single long session reads as
    the quietest day of the month. Turns say how much was actually asked and
    spend says what it cost, so a row now carries the three numbers you need
    to tell a busy day from an expensive one from a productive one.

    The three series are counted separately and stitched together by day
    rather than joined in SQL, because a session-to-turns-to-usage join fans
    out: every turn would be multiplied by that session's usage rows and the
    counts would come back inflated by a different factor on every day.

    Every series groups on `date(stamp)` rather than on the first ten
    characters of it. `substr` will happily make a day called "x" out of a
    row whose timestamp was never a date, and a string window compares "x"
    as greater than any real date, so the junk lands inside every window.
    `date()` returns NULL for anything it cannot read, which turns the same
    rows into something a WHERE clause can drop.
    """
    def series(sql: str, table: str, stamp: str) -> dict[str, int]:
        where = f"WHERE date({stamp}) IS NOT NULL"
        args: tuple = ()
        if days > 0:
            where += f" AND {stamp} >= datetime('now', ?)"
            args = (f"-{days} days",)
        return {
            day: value
            for day, value in conn.execute(
                sql.format(where=where, table=table, stamp=stamp), args
            )
        }

    counts = series(
        "SELECT date({stamp}) AS day, COUNT(*) "
        "FROM {table} {where} GROUP BY day",
        "sessions", "created_at",
    )
    turns_by_day: dict[str, int] = {}
    if _has_columns(conn, "turns", "timestamp"):
        turns_by_day = series(
            "SELECT date({stamp}) AS day, COUNT(*) "
            "FROM {table} {where} GROUP BY day",
            "turns", "timestamp",
        )
    spend_by_day: dict[str, int] = {}
    if _has_usage(conn) and usage_is_windowable(conn):
        spend_by_day = series(
            "SELECT date({stamp}) AS day, "
            "COALESCE(SUM(total_nano_aiu), 0) FROM {table} {where} GROUP BY day",
            "assistant_usage_events", "created_at",
        )
    # A day earns a row if *anything* happened on it. A session that opened
    # yesterday and was worked in today has turns today and no session start,
    # and dropping that day would hide the work rather than the gap.
    return [
        (day, counts.get(day, 0), turns_by_day.get(day, 0), spend_by_day.get(day, 0))
        for day in sorted(set(counts) | set(turns_by_day) | set(spend_by_day))
    ]
