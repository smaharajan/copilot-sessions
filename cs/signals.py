"""Three things the store never records, read back out of what it does.

Copilot's session store has no column for approval mode, no parent-session
link, and no notion of a credential. All three matter once you are answering
for the work: which sessions ran unattended, which work was handed from one
session to the next, and which conversations have a password sitting in them.

Each is inferred here, and every verdict carries the evidence it was drawn
from — a judgement you can check is worth more than a number you have to
trust. Nothing in this module writes to the store.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict

from . import db, redact

# ── Autonomy ─────────────────────────────────────────────────────────
# `assistant_usage_events.initiator` says who asked for each model call:
# 'user' for a prompt you typed, 'agent'/'sub-agent' for steps the agent took
# on its own. Steps per prompt is therefore a direct measure of how far the
# session ran between check-ins — which is what YOLO mode actually changes.

UNATTENDED_RATIO = 20.0   # agent steps per prompt; the 98th percentile
UNATTENDED_STEPS = 40     # and enough of them to be a run, not a long answer

# Flags that turn every approval off, and the words that do the same when
# typed. Only your own messages count as evidence: the store is full of the
# agent *explaining* `--allow-all-tools`, which is not the same as using it.
_FLAGS = ("--allow-all-tools", "--allow-all-paths", "--allow-all-permissions")
# "yolo" on its own line is the toggle; "yolo means…" is a question about it.
_TYPED = re.compile(
    r"^\s*/?yolo\s*$|^\s*auto[ -]approve\s*$"
    r"|\b(?:turn on|enable|switch to|run in|use)\b[^.\n]{0,20}\byolo\b",
    re.I | re.M,
)


def _header(conn: sqlite3.Connection) -> str:
    """The six session facts every report here opens with, aliased `s`.

    Written through :func:`db.optional` rather than as a literal column list:
    Copilot has added columns over its releases, and a report that cannot say
    which repository a session belonged to should still say everything else.
    """
    return f"""s.id, substr(MAX(s.created_at, s.updated_at), 1, 16),
                  COALESCE({db.optional(conn, 'sessions', 'summary', 's', "''")}, ''),
                  COALESCE({db.optional(conn, 'sessions', 'repository', 's', "''")}, ''),
                  COALESCE({db.optional(conn, 'sessions', 'cwd', 's', "''")}, ''),
                  (SELECT COUNT(*) FROM turns t WHERE t.session_id = s.id)"""


def _evidence(prompt: str) -> str:
    """What this prompt shows about approvals, or '' if it shows nothing."""
    lowered = prompt.lower()
    flag = next((f for f in _FLAGS if f in lowered), None)
    if flag:
        return f"you passed {flag}"
    if _TYPED.search(prompt):
        return "you turned approvals off in the session"
    return ""


def _explicit(conn: sqlite3.Connection) -> dict[str, str]:
    """Session id → the evidence, for sessions that show approvals were off."""
    rows = conn.execute(
        """SELECT session_id, user_message FROM turns
           WHERE lower(user_message) LIKE '%allow-all-%'
              OR lower(user_message) LIKE '%yolo%'
              OR lower(user_message) LIKE '%auto-approve%'
              OR lower(user_message) LIKE '%auto approve%'"""
    ).fetchall()

    evidence: dict[str, str] = {}
    for session_id, prompt in rows:
        if session_id not in evidence and (why := _evidence(prompt or "")):
            evidence[session_id] = why
    return evidence


def _steps(conn: sqlite3.Connection) -> dict[str, int]:
    """Agent-initiated model calls per session — the steps you did not ask for."""

    if not db.has_delegation(conn):
        return {}
    return {
        session_id: count
        for session_id, count in conn.execute(
            """SELECT session_id, COUNT(*) FROM assistant_usage_events
               WHERE initiator IN ('agent', 'sub-agent') GROUP BY session_id"""
        )
    }


def _verdict(steps: int, turns: int, evidence: str) -> tuple[str, str]:
    """One of yes / high / no, with the reason it was reached."""
    if evidence:
        return "yes", evidence
    ratio = steps / max(turns, 1)
    if ratio >= UNATTENDED_RATIO and steps >= UNATTENDED_STEPS:
        return "high", f"{ratio:.0f} agent steps per prompt, unattended"
    if turns == 0:
        return "no", "no prompts recorded"
    return "no", f"{ratio:.1f} agent steps per prompt"


def autonomy(conn: sqlite3.Connection) -> list[dict]:
    """Every session that has prompts, most autonomous first.

    Sessions with no turns are dropped: a ratio needs something to divide by,
    and an empty session had no chance to run away with anything.
    """
    evidence = _explicit(conn)
    steps = _steps(conn)
    rows = conn.execute(
        f"""SELECT {_header(conn)}
           FROM sessions s"""
    ).fetchall()

    out = []
    for session_id, active, summary, repo, cwd, turns in rows:
        if not turns:
            continue
        agent_steps = steps.get(session_id, 0)
        verdict, why = _verdict(agent_steps, turns, evidence.get(session_id, ""))
        out.append({
            "id": session_id, "active": active, "summary": summary,
            "repo": repo, "cwd": cwd, "turns": turns, "steps": agent_steps,
            "ratio": agent_steps / turns, "verdict": verdict, "why": why,
        })
    order = {"yes": 0, "high": 1, "no": 2}
    out.sort(key=lambda r: (order[r["verdict"]], -r["ratio"]))
    return out


def session_autonomy(conn: sqlite3.Connection, session_id: str) -> dict:
    """The same verdict for one session, without scoring the whole store."""
    turns = conn.execute(
        "SELECT COUNT(*) FROM turns WHERE session_id = ?", (session_id,)
    ).fetchone()[0]
    steps = _steps_for(conn, session_id)
    evidence = _explicit_for(conn, session_id)
    verdict, why = _verdict(steps, turns, evidence)
    return {
        "verdict": verdict, "why": why, "steps": steps, "turns": turns,
        "ratio": steps / max(turns, 1),
    }


def _steps_for(conn: sqlite3.Connection, session_id: str) -> int:

    if not db.has_delegation(conn):
        return 0
    return conn.execute(
        """SELECT COUNT(*) FROM assistant_usage_events
           WHERE session_id = ? AND initiator IN ('agent', 'sub-agent')""",
        (session_id,),
    ).fetchone()[0]


def _explicit_for(conn: sqlite3.Connection, session_id: str) -> str:
    for (prompt,) in conn.execute(
        "SELECT user_message FROM turns WHERE session_id = ? ORDER BY turn_index",
        (session_id,),
    ):
        if why := _evidence(prompt or ""):
            return why
    return ""


# ── Handoffs ─────────────────────────────────────────────────────────
# A handoff is a document one session writes so the next can pick the work
# up. Nothing links the two sessions in the store, but the document does: it
# is opened by both, and `session_files` records every file a session touched.

_DOC = re.compile(r"hand[-_ ]?off[^/]*\.(?:md|markdown|txt)$", re.I)
_ASK_WRITE = re.compile(
    r"\b(?:creat\w*|writ\w*|generat\w*|provid\w*|produc\w*|prepar\w*|updat\w*)\b"
    r"[^.\n]{0,40}\bhand[- ]?off",
    re.I,
)
_ASK_READ = re.compile(
    r"\b(?:read|take|use|open|follow|review|continue)\b[^.\n]{0,40}\bhand[- ]?off"
    r"|\bhand[- ]?off\b[^.\n]{0,40}\b(?:from|of)\s+(?:the\s+)?previous\b"
    r"|\bprevious\s+session\b[^.\n]{0,30}\bhand[- ]?off",
    re.I,
)


def _docs_by_session(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Handoff documents each session opened or wrote."""

    if not db.has_files(conn):
        return {}
    out: dict[str, list[str]] = defaultdict(list)
    for session_id, path in conn.execute(
        "SELECT session_id, file_path FROM session_files WHERE file_path LIKE '%hand%'"
    ):
        if _DOC.search(path or ""):
            out[session_id].append(path)
    return dict(out)


def _asked(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """What each session's prompts asked for: writing a handoff, reading one."""
    out: dict[str, set[str]] = defaultdict(set)
    for session_id, prompt in conn.execute(
        "SELECT session_id, user_message FROM turns "
        "WHERE lower(user_message) LIKE '%hand%off%'"
    ):
        text = prompt or ""
        if _ASK_WRITE.search(text):
            out[session_id].add("wrote")
        if _ASK_READ.search(text):
            out[session_id].add("read")
    return dict(out)


def _role(docs: list[str], asked: set[str]) -> str:
    """emitted / received / both / none, from the document and what was asked."""
    if "wrote" in asked and "read" in asked:
        return "both"
    if "wrote" in asked:
        return "emitted"
    if "read" in asked:
        return "received"
    return "touched" if docs else "none"


def handoffs(conn: sqlite3.Connection) -> list[dict]:
    """Every session involved in a handoff, newest first."""
    docs = _docs_by_session(conn)
    asked = _asked(conn)
    ids = set(docs) | set(asked)
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT {_header(conn)}
            FROM sessions s WHERE s.id IN ({placeholders})
            ORDER BY MAX(s.created_at, s.updated_at) DESC""",
        tuple(ids),
    ).fetchall()
    return [
        {
            "id": session_id, "active": active, "summary": summary,
            "repo": repo, "cwd": cwd, "turns": turns,
            "docs": sorted(docs.get(session_id, [])),
            "role": _role(docs.get(session_id, []), asked.get(session_id, set())),
        }
        for session_id, active, summary, repo, cwd, turns in rows
    ]


def session_handoff(conn: sqlite3.Connection, session_id: str) -> dict:
    """Whether one session handed work on, took it up, or neither."""
    docs = sorted(_docs_by_session(conn).get(session_id, []))
    asked = _asked(conn).get(session_id, set())
    return {"role": _role(docs, asked), "docs": docs}


_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I
)


def edges(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """(parent, child, why) links between sessions, always older → newer.

    Two kinds of link, both evidence rather than inference: sessions that
    opened the same handoff document, and sessions that name another session's
    id — which is what `cs resume` prints and people paste into the next one.

    Building this reads every turn, so callers drawing more than one chain
    should build it once and hand it to `chain`.
    """
    started = {
        session_id: created
        for session_id, created in conn.execute("SELECT id, created_at FROM sessions")
    }
    edges: list[tuple[str, str, str]] = []

    shared: dict[str, list[str]] = defaultdict(list)
    for session_id, paths in _docs_by_session(conn).items():
        for path in paths:
            shared[path].append(session_id)
    for path, sessions in shared.items():
        if len(sessions) < 2:
            continue
        chain = sorted(sessions, key=lambda s: started.get(s) or "")
        name = path.rsplit("/", 1)[-1]
        for parent, child in zip(chain, chain[1:], strict=False):
            edges.append((parent, child, f"via {name}"))

    for session_id, prompt, reply in conn.execute(
        "SELECT session_id, user_message, assistant_response FROM turns"
    ):
        for match in _UUID.findall(f"{prompt or ''}\n{reply or ''}"):
            other = match.lower()
            if other == session_id or other not in started:
                continue
            older, newer = (
                (other, session_id)
                if (started.get(other) or "") <= (started.get(session_id) or "")
                else (session_id, other)
            )
            edges.append((older, newer, "session id referenced"))

    seen: set[tuple[str, str]] = set()
    unique = []
    for parent, child, why in edges:
        if (parent, child) in seen:
            continue
        seen.add((parent, child))
        unique.append((parent, child, why))
    return unique


def chain(
    conn: sqlite3.Connection, session_id: str,
    links: list[tuple[str, str, str]] | None = None,
) -> dict:
    """The handoff tree `session_id` belongs to: its whole connected group.

    Returns roots and a parent → children map, so the caller can draw it.
    Sessions with no links come back as a single node — a chain of one is
    still the honest answer.
    """
    links = edges(conn) if links is None else links
    neighbours: dict[str, set[str]] = defaultdict(set)
    for parent, child, _ in links:
        neighbours[parent].add(child)
        neighbours[child].add(parent)

    group, queue = {session_id}, [session_id]
    while queue:
        for neighbour in neighbours[queue.pop()]:
            if neighbour not in group:
                group.add(neighbour)
                queue.append(neighbour)

    children: dict[str, list[tuple[str, str]]] = defaultdict(list)
    has_parent: set[str] = set()
    for parent, child, why in links:
        if parent in group and child in group:
            children[parent].append((child, why))
            has_parent.add(child)

    detail = {}
    placeholders = ",".join("?" * len(group))
    for row in conn.execute(
        f"""SELECT s.id, substr(s.created_at, 1, 16),
                   COALESCE({db.optional(conn, 'sessions', 'summary', 's', "''")}, ''),
                   COALESCE({db.optional(conn, 'sessions', 'repository', 's', "''")}, ''),
                   COALESCE({db.optional(conn, 'sessions', 'cwd', 's', "''")}, ''),
                   (SELECT COUNT(*) FROM turns t WHERE t.session_id = s.id)
            FROM sessions s WHERE s.id IN ({placeholders})""",
        tuple(group),
    ):
        detail[row[0]] = {
            "id": row[0], "started": row[1], "summary": row[2],
            "repo": row[3], "cwd": row[4], "turns": row[5],
        }
    roots = sorted(
        (s for s in group if s not in has_parent),
        key=lambda s: detail.get(s, {}).get("started") or "",
    )
    return {"roots": roots, "children": dict(children), "detail": detail,
            "size": len(group)}


# ── Credential exposure ──────────────────────────────────────────────
# `cs` masks secrets on the way to the screen. Masking alone leaves you
# unaware, so the same rules are run over the store to answer the question
# masking cannot: which conversations have a credential in them at all.

# Files whose whole purpose is to hold a credential. `session_files` proves
# that a path was touched, not that its contents were read, so this is a path
# warning rather than a credential-exposure claim.
_SENSITIVE_FILES: list[tuple[str, re.Pattern[str]]] = [
    ("env file", re.compile(r"(?:^|/)\.env(?:\.[^/]*)?$")),
    ("ssh private key", re.compile(r"(?:^|/)id_(?:rsa|dsa|ecdsa|ed25519)$")),
    ("aws credentials", re.compile(r"/\.aws/(?:credentials|config)$")),
    ("kubeconfig", re.compile(r"(?:/\.kube/config$|kubeconfig[^/]*$)")),
    ("registry login", re.compile(r"(?:^|/)\.(?:npmrc|pypirc|netrc|docker/config\.json)$")),
    ("certificate or keystore", re.compile(r"(?i)\.(?:pem|key|p12|pfx|jks|keystore)$")),
    ("terraform state", re.compile(r"\.tfstate(?:\.backup)?$")),
    ("secrets file", re.compile(r"(?i)(?:^|/)[^/]*secrets?[^/]*\.(?:ya?ml|json|toml|txt)$")),
]



def _session_activity_window(days: int | None) -> tuple[str, tuple]:
    """SQL fragment limiting rows to sessions active in the last `days`.

    Matches `db.recent_sessions`: filter on MAX(created_at, updated_at).
    `days` None or <= 0 means every session — no fragment.
    The alias `s` must be in scope (JOIN sessions s …).
    """
    if days is not None and days > 0:
        return (
            "MAX(s.created_at, s.updated_at) >= datetime('now', ?)",
            (f"-{days} days",),
        )
    return "", ()


def sensitive_files(
    conn: sqlite3.Connection, session_id: str | None = None,
    days: int | None = None,
) -> list[dict]:
    """Sessions that touched a file whose job is to hold a credential.

    The text scan can only find a secret that was written down in the
    conversation. `session_files` records paths created or edited; it does not
    prove that the file contents were read, so the report says only "touched".

    When `session_id` is None and `days` > 0, only sessions active in that
    window are scanned — the same window `exposures` uses for unscoped audit.
    """
    if not db.has_files(conn):
        return []
    tool = db.optional(conn, "session_files", "tool_name", alias="f")
    sql = f"SELECT f.session_id, f.file_path, {tool} FROM session_files f"
    params: list = []
    where: list[str] = []
    if session_id:
        where.append("f.session_id = ?")
        params.append(session_id)
    else:
        clause, window = _session_activity_window(days)
        if clause:
            sql += " JOIN sessions s ON s.id = f.session_id"
            where.append(clause)
            params.extend(window)
    if where:
        sql += " WHERE " + " AND ".join(where)

    per: dict[str, dict] = {}
    for sid, path, tool in conn.execute(sql, params):
        kind = next((name for name, pattern in _SENSITIVE_FILES
                     if pattern.search(path or "")), None)
        if not kind:
            continue
        entry = per.setdefault(
            sid, {"id": sid, "count": 0, "kinds": Counter(), "paths": [],
                  "wrote": False}
        )
        entry["count"] += 1
        entry["kinds"][kind] += 1
        entry["wrote"] = entry["wrote"] or tool == "create"
        if path not in entry["paths"]:
            entry["paths"].append(path)
    if not per:
        return []

    placeholders = ",".join("?" * len(per))
    meta = {
        row[0]: row[1:]
        for row in conn.execute(
            f"""SELECT id, substr(MAX(created_at, updated_at), 1, 16),
                       COALESCE({db.optional(conn, 'sessions', 'summary', '', "''")}, ''),
                       COALESCE({db.optional(conn, 'sessions', 'cwd', '', "''")}, '')
                FROM sessions WHERE id IN ({placeholders})""",
            tuple(per),
        )
    }
    for entry in per.values():
        active, summary, cwd = meta.get(entry["id"], ("", "", ""))
        entry.update(active=active, summary=summary, cwd=cwd)
    return sorted(per.values(), key=lambda e: (-e["count"], e["active"]), reverse=False)


def _masked_line(text: str) -> str:
    """The masked line a finding sits on, or "" when there isn't a safe one.

    The line is taken from `redact.redact`'s output, never the raw text, so
    the only way a value could reach the caller is if the masker had already
    let it through — and a line with no mask on it is dropped, which is also
    what happens when masking is switched off entirely.
    """
    for line in redact.redact(text).splitlines():
        if "[redacted" in line:
            return " ".join(line.split())
    return ""


# A credential *mentioned* and a credential *hardcoded* are different
# problems: one is a value that went past the model, the other is a value
# that is now in a file. Neither the store nor the masker can tell them
# apart on their own, so this is the shape half of the test — a line that
# reads as source rather than as prose — and `_wrote_files` is the other.
#
# Deliberately narrow. `password: hunter2` in a sentence is prose with a
# colon in it; what counts is a quoted value, an `export`, or the
# no-spaces-around-the-equals form an env file and a shell line both use.
_NAME = r"[A-Za-z_][A-Za-z0-9_.\-]*"
_CODE_SHAPE = re.compile(
    rf"""(?ix)
    (?:
        # An equals with a quoted value is an assignment wherever it
        # appears — a shell line, a config literal, a keyword argument.
        (?: ^ | [\s({{\[,;"'`] ) (?: export \s+ )?
        {_NAME} \s* = \s* ["']
      |
        # Unquoted, the spacing is what separates code from prose: an env
        # file and a shell line write `NAME=value` closed up, and a sentence
        # writes `the token = …` with room around it.
        (?: ^ | [\s({{\[,;"'`] ) (?: export \s+ )?
        {_NAME} = \[redacted
      |
        # A colon only counts when the value is quoted: that is JSON, YAML or
        # a config literal. `token: [redacted]` unquoted is an English
        # sentence with a colon in it, and most of them are.
        (?: ^ | [\s({{\[,;"'] ) ["']? {_NAME} ["']? \s* : \s* ["']
      |
        # …unless it is plainly inside an object literal, where an unquoted
        # value is still code: `{{secret: …}}`.
        [{{,] \s* ["']? {_NAME} ["']? \s* : \s* \[redacted
    )
    """
)


def _hardcoded(line: str) -> bool:
    """Whether a finding's evidence line reads as source, not as prose."""
    return bool(line) and bool(_CODE_SHAPE.search(line))


def _wrote_files(conn: sqlite3.Connection, session_ids: list[str]) -> set[str]:
    """Which of these sessions created or edited a file.

    The corroborating half of "hardcoded". `session_files` records the paths
    a session wrote; it does not record which line went into which file, so
    this says the session put something on disk, never that it put *this*
    value there. That is why the report says "in a session that wrote files"
    rather than "in this file".
    """
    if not session_ids or not db.has_files(conn):
        return set()
    tool = db.optional(conn, "session_files", "tool_name")
    placeholders = ",".join("?" * len(session_ids))
    return {
        row[0] for row in conn.execute(
            f"""SELECT DISTINCT session_id FROM session_files
                WHERE session_id IN ({placeholders})
                  AND {tool} IN ('create', 'edit')""",
            tuple(session_ids),
        )
    }


def _checkpoint_text(
    conn: sqlite3.Connection, session_id: str | None,
    days: int | None = None,
) -> list[tuple[str, str]]:
    """(session id, prose) for the latest checkpoint of each session.

    Checkpoints are written by the agent, out of the same conversation, and
    say things like "set DB_PASSWORD in the config". They are a different
    table from `turns`, so scanning turns alone left them unread. `cs show`
    opens only the latest checkpoint, so the audit scans exactly that record.

    Unscoped calls honour the same activity window as `exposures`.
    """
    if not db._has_table(conn, "checkpoints"):
        return []
    columns = [row[1] for row in conn.execute("PRAGMA table_info(checkpoints)")]
    visible = [name for name in ("next_steps", "work_done") if name in columns]
    if not visible or "session_id" not in columns:
        return []
    sql = f"SELECT c.session_id, {', '.join('c.' + n for n in visible)} FROM checkpoints c"
    where: list[str] = []
    params: list = []
    if "checkpoint_number" in columns:
        where.append("""c.checkpoint_number = (
                    SELECT MAX(newer.checkpoint_number)
                    FROM checkpoints newer
                    WHERE newer.session_id = c.session_id
                  )""")
    if session_id:
        where.append("c.session_id = ?")
        params.append(session_id)
    else:
        clause, window = _session_activity_window(days)
        if clause:
            sql += " JOIN sessions s ON s.id = c.session_id"
            where.append(clause)
            params.extend(window)
    if where:
        sql += " WHERE " + " AND ".join(where)
    def bullets(text: str) -> list[str]:
        picked = []
        in_code = False
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("```"):
                in_code = not in_code
                continue
            if in_code or not line:
                continue
            line = re.sub(r"\*\*|__|^#{1,6}\s*", "", line)
            line = re.sub(r"^([-*•]|\d+[.)])\s+", "", line)
            picked.append(re.sub(r"\s+", " ", line))
            if len(picked) >= 5:
                break
        return picked

    return [
        (row[0], "\n".join(
            line for value in row[1:] if value for line in bullets(str(value))
        ))
        for row in conn.execute(sql, params)
    ]


def exposures(
    conn: sqlite3.Connection, session_id: str | None = None,
    days: int | None = None,
) -> list[dict]:
    """Sessions holding credential-shaped text, worst first.

    A finding names the kind and where it is. It never carries the value:
    an audit that prints the secret has simply leaked it somewhere new.

    When `session_id` is set, that session is scanned in full. When it is
    None and `days` > 0, only sessions whose MAX(created_at, updated_at)
    falls in the window are scanned — bare `cs audit` defaults to 30 days.
    `days` None or <= 0 means the whole store.
    """
    sql = """SELECT t.session_id, t.turn_index, t.user_message, t.assistant_response
             FROM turns t"""
    params: list = []
    if session_id:
        sql += " WHERE t.session_id = ?"
        params.append(session_id)
    else:
        clause, window = _session_activity_window(days)
        if clause:
            sql += " JOIN sessions s ON s.id = t.session_id WHERE " + clause
            params.extend(window)

    # One row per session and side. A prompt finding is proven to have been
    # pasted by the user; a reply finding is only proven to be assistant
    # output. Combining them made every finding in a mixed session look pasted.
    per: dict[tuple[str, str], dict] = {}

    def record(sid: str, side: str, source: str, where: int, text: str) -> None:
        for kind, hint in redact.findings(text or ""):
            entry = per.setdefault(
                (sid, side), {"id": sid, "count": 0, "kinds": Counter(),
                              "hints": [], "pasted": side == "you",
                              "turn": where, "line": "", "side": side,
                              "source": source}
            )
            entry["count"] += 1
            entry["kinds"][kind] += 1
            if hint and hint not in entry["hints"]:
                entry["hints"].append(hint)
            if not entry["line"]:
                line = _masked_line(text or "")
                if line:
                    entry.update(line=line, turn=where)

    for sid, turn_index, prompt, reply in conn.execute(
        sql + " ORDER BY t.turn_index", params
    ):
        for side, text in (("you", prompt), ("agent", reply)):
            record(sid, side, "turn", turn_index, text)

    # Its own side, so a checkpoint finding is never reported as a turn you
    # could open — there is no turn number to open it at.
    for sid, prose in _checkpoint_text(conn, session_id, days=days):
        record(sid, "checkpoint", "checkpoint", 0, prose)

    if not per:
        return []
    session_ids = sorted({entry["id"] for entry in per.values()})
    placeholders = ",".join("?" * len(session_ids))
    meta = {
        row[0]: row[1:]
        for row in conn.execute(
            f"""SELECT id, substr(MAX(created_at, updated_at), 1, 16),
                       COALESCE({db.optional(conn, 'sessions', 'summary', '', "''")}, ''),
                       COALESCE({db.optional(conn, 'sessions', 'repository', '', "''")}, ''),
                       COALESCE({db.optional(conn, 'sessions', 'cwd', '', "''")}, '')
                FROM sessions WHERE id IN ({placeholders})""",
            tuple(session_ids),
        )
    }
    wrote = _wrote_files(conn, session_ids)
    for entry in per.values():
        sid = entry["id"]
        active, summary, repo, cwd = meta.get(sid, ("", "", "", ""))
        entry.update(active=active, summary=summary, repo=repo, cwd=cwd)
        # Both halves, or neither: a code-shaped line in a session that wrote
        # nothing to disk is a snippet discussed, and a session that wrote
        # files but whose finding is a sentence is a password talked about.
        entry["hardcoded"] = _hardcoded(entry["line"]) and sid in wrote
        # A session is as serious as the most certain thing in it, and is
        # ranked on that rather than on how many findings it has: one leaked
        # private key outranks thirty password-shaped assignments.
        entry["severity"] = next(
            (name for name in redact.RANK
             if any(redact.severity(kind) == name for kind in entry["kinds"])),
            "medium",
        )
        # Half a step up when it is hardcoded. A password-shaped assignment
        # sitting in a file the session wrote is a worse finding than the
        # same shape quoted in a sentence, and burying twenty-two of them
        # under ninety mentions of the same certainty is not highlighting
        # them. Half, not a whole step: it is still less certain than a
        # documented key format, and should not outrank one.
        entry["rank"] = redact.RANK.index(entry["severity"]) - (
            0.5 if entry["hardcoded"] else 0
        )
    return sorted(per.values(),
                  key=lambda e: (e["rank"], -e["count"], e["id"], e["side"]))


# ── Destructive activity ─────────────────────────────────────────────
# The third thing you have to be able to answer about work an agent did:
# what did it take away. Nothing here is a column in the store either.
#
# `session_files.tool_name` records `create` and `edit` and nothing else —
# there is no delete event, anywhere, in any Copilot release this reads. So a
# removal can only be found where it was written down: in the conversation.
# That is a weaker basis than `cs audit`'s and this module says so rather
# than dressing it up, exactly as the autonomy verdict does.

# Kind → (severity, what it is, the pattern). Ordered worst first; the first
# rule to claim a line wins it, so `git push --force` is history rather than
# the `push` half of anything else.
_DESTRUCTIVE: list[tuple[str, str, str, re.Pattern[str]]] = [
    ("history", "critical", "rewrote or discarded committed work", re.compile(
        r"""(?ix) \b (?:
            git \s+ push [^\n|;&]*? (?: --force(?!-with-lease) | \s -f \b )
          | git \s+ reset \s+ --hard
          | git \s+ (?: clean \s+ -[a-z]* [dfx] | branch \s+ -D )
          | git \s+ filter-branch | git \s+ filter-repo
        ) """)),
    ("data", "critical", "dropped or emptied a database object", re.compile(
        r"""(?ix) \b (?:
            drop \s+ (?: table | database | schema | index ) \b
          | truncate \s+ table \b
          | delete \s+ from \s+ [A-Za-z_][\w.]* \s* (?: ; | $ )
          | db \.dropDatabase \s* \(
        ) """)),
    ("infra", "critical", "destroyed something that was deployed", re.compile(
        r"""(?ix) \b (?:
            terraform \s+ destroy
          | kubectl \s+ delete \b
          | helm \s+ (?: delete | uninstall ) \b
          | aws \s+ s3 \s+ (?: rb | rm ) [^\n]* --recursive
          | docker \s+ (?: system \s+ prune | volume \s+ rm | rm \s+ -[a-z]*f )
        ) """)),
    ("delete", "high", "removed files from disk", re.compile(
        r"""(?ix) (?:
            \b rm \s+ (?: -[a-z]+ \s+ )* -[a-z]* [rf] [a-z]* \b
          | \b git \s+ rm \b
          | \b find \b [^\n]* \s -delete \b
          | \b shutil\.rmtree \s* \(
          | \b os\. (?: remove | unlink | rmdir ) \s* \(
          | \b Remove-Item \b [^\n]* -Recurse
          | \b rimraf \b
        ) """)),
    ("remote-exec", "high", "ran code fetched from the network", re.compile(
        r"""(?ix) \b (?: curl | wget ) \b [^\n|]* \| \s* (?: sudo \s+ )?
            (?: ba | z | fi )? sh \b """)),
    ("privilege", "medium", "widened permissions or ran as root", re.compile(
        r"""(?ix) (?:
            \b chmod \s+ (?: -[a-z]+ \s+ )* 777 \b
          | \b chmod \s+ (?: -[a-z]+ \s+ )* a?\+ [rwx]* w
          | \b chown \s+ -R \b
          | \b sudo \s+ (?! -h | --help ) [a-z]
        ) """)),
]

# What turns a command into a report of one. The agent narrates its own work
# in the past tense and ticks it off; a command it is *offering* is written
# as an instruction, in a fenced block, with no outcome attached.
_DONE = re.compile(
    r"""(?ix) (?: ✓ | ✅ | \b (?:
        deleted | removed | dropped | destroyed | truncated | wiped | purged
      | staged | committed | pushed | cleaned | uninstalled
      | ran \s+ (?:it|this|that) | already \s+ (?:gone|removed|deleted)
    ) \b ) """
)

# …and what takes it back. A line can carry a completion word that belongs to
# something else entirely — "their blobs still sit in the repo history (they
# were committed in earlier commits)" sits on the same line as the
# `git filter-repo` the agent goes on to say it did **not** run. Anything in
# this list inside the window cancels the claim.
_NOT_DONE = re.compile(
    r"""(?ix) (?: n't | \b (?:
        not | never | without | skip (?:ped)? | instead | interrupted
      | failed | would | should | could | want \s+ me | if \s+ you
    ) \b ) """
)

# How close a completion word has to sit to the command for it to be about
# that command. Generous after (the agent writes "`git rm -r x` staged"),
# tight before ("deleted it with `rm -rf x`"), and nothing beyond that: a
# marker at the far end of a paragraph is about the paragraph.
_DONE_AFTER, _DONE_BEFORE = 48, 24

DESTRUCTIVE_BASIS = ("ran", "proposed")


def _fenced(text: str) -> list[tuple[int, int]]:
    """The spans of ``` blocks in `text`.

    A command inside one is a command being *offered*: the whole convention
    of a fenced block is "here is something for you to run". The same command
    in running prose, in the past tense, is the agent telling you what it did.
    """
    marks = [m.start() for m in re.finditer(r"^\s*```", text, re.M)]
    return list(zip(marks[::2], marks[1::2] + [len(text)] * (len(marks) % 2),
                    strict=False))


def _basis(text: str, at: int, fences: list[tuple[int, int]] | None = None) -> str:
    """`ran` or `proposed` for a match at offset `at`.

    Two questions, in this order: is it inside a fenced block, and does the
    line it sits on report an outcome. Nothing here is proof — the store
    holds no exit code — and `ran` means "the session says so", which is the
    strongest thing that can honestly be said about it.
    """
    if any(start <= at < end
           for start, end in (_fenced(text) if fences is None else fences)):
        return "proposed"
    line_start = text.rfind("\n", 0, at) + 1
    line_end = text.find("\n", at)
    line_end = line_end if line_end != -1 else len(text)
    start = max(line_start, at - _DONE_BEFORE)
    # Snap back to a word boundary. Cutting mid-word is wrong in both
    # directions: it sliced "Deleted the folder with `rm -rf x`" down to
    # "eleted …" and lost a real report, and it would equally cut
    # "undeleted" down to a "deleted" that was never written.
    while start > line_start and not text[start - 1].isspace():
        start -= 1
    window = text[start:min(line_end, at + _DONE_AFTER)]
    if not _DONE.search(window) or _NOT_DONE.search(window):
        return "proposed"
    return "ran"


def _command(text: str, at: int, span: int = 160) -> str:
    """The command, taken from where it starts rather than where its line does.

    Reading from the start of the line put the sentence that introduces a
    command in the column where the command should be — thirty characters of
    "⚠️ **One caveat:** these files are…" and then the window ran out. The
    match is what the row is about, so the match leads, and the page trims
    whatever is left to its own width.
    """
    line_end = text.find("\n", at)
    rest = text[at:line_end if line_end != -1 else len(text)]
    cut = " ".join(rest.split()).rstrip("`*|> ")
    return cut[:span] + "…" if len(cut) > span else cut


def destructive(
    conn: sqlite3.Connection, session_id: str | None = None,
    days: int | None = None,
) -> list[dict]:
    """Destructive commands the sessions show, most certain first.

    One row per session, kind and basis — the same shape `exposures` uses,
    and for the same reason: `rm -rf` offered eight times in one explanation
    is one thing to look at, not eight.

    Unscoped calls honour the same activity window as `exposures`.
    """
    sql = """SELECT t.session_id, t.turn_index, t.user_message, t.assistant_response
             FROM turns t"""
    params: list = []
    if session_id:
        sql += " WHERE t.session_id = ?"
        params.append(session_id)
    else:
        clause, window = _session_activity_window(days)
        if clause:
            sql += " JOIN sessions s ON s.id = t.session_id WHERE " + clause
            params.extend(window)

    per: dict[tuple[str, str, str], dict] = {}
    for sid, turn_index, prompt, reply in conn.execute(
        sql + " ORDER BY t.turn_index", params
    ):
        for side, text in (("you", prompt or ""), ("agent", reply or "")):
            if not text:
                continue
            # Once per reply, not once per match: a long reply with twenty
            # `rm` in it was re-scanned for fences twenty times.
            fences = _fenced(text)
            claimed: list[tuple[int, int]] = []
            for kind, severity, meaning, pattern in _DESTRUCTIVE:
                for match in pattern.finditer(text):
                    span = match.span()
                    if any(span[0] < end and start < span[1]
                           for start, end in claimed):
                        continue
                    claimed.append(span)
                    # Only the agent can report having done something. A
                    # command you typed is an instruction however it is
                    # phrased, and the store cannot say it was carried out.
                    basis = (_basis(text, span[0], fences)
                             if side == "agent" else "proposed")
                    entry = per.setdefault(
                        (sid, kind, basis),
                        {"id": sid, "kind": kind, "basis": basis,
                         "severity": severity, "meaning": meaning, "count": 0,
                         "turn": turn_index, "side": side, "line": ""},
                    )
                    entry["count"] += 1
                    if not entry["line"]:
                        entry.update(
                            line=redact.redact(_command(text, span[0])),
                            turn=turn_index, side=side,
                        )
    if not per:
        return []

    session_ids = sorted({entry["id"] for entry in per.values()})
    placeholders = ",".join("?" * len(session_ids))
    meta = {
        row[0]: row[1:]
        for row in conn.execute(
            f"""SELECT id, substr(MAX(created_at, updated_at), 1, 16),
                       COALESCE({db.optional(conn, 'sessions', 'summary', '', "''")}, '')
                FROM sessions WHERE id IN ({placeholders})""",
            tuple(session_ids),
        )
    }
    for entry in per.values():
        active, summary = meta.get(entry["id"], ("", ""))
        entry.update(active=active, summary=summary)
        entry["rank"] = redact.RANK.index(entry["severity"])
    return sorted(
        per.values(),
        key=lambda e: (DESTRUCTIVE_BASIS.index(e["basis"]), e["rank"],
                       -e["count"], e["id"], e["kind"]),
    )
