"""What a session's event log recorded — read, reduced to counts, and cached.

Copilot writes a second record beside the store: one JSON object per line in
``session-state/<id>/events.jsonl``. It is where the facts the store does not
keep live — which tool call failed, which hook ran, when allow-all was
switched on, which sub-agent ran on which model. It is also large: thousands
of files and gigabytes in total, with single files past a hundred megabytes,
most of it tool output nobody here needs.

So this module never holds a file in memory and never keeps any text. A log
is streamed a line at a time, only the event types asked for are parsed, and
what comes out is a *digest*: counts, names and timestamps. Tool results are
untrusted and can hold secrets; they are read for their ``success`` flag and
dropped on the spot.

Digests are cached under ``$XDG_CACHE_HOME/cs`` (default ``~/.cache/cs``),
keyed by the log's path, mtime and size, so a warm view re-reads nothing. The
cache holds the digest and nothing else — the same counts-and-ids rule — and
it is a convenience: a cache that cannot be written is simply not written,
and the answer is computed in memory instead. Nothing here writes inside
``COPILOT_HOME``.

Everything is standard library, like the rest of ``cs``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

from . import db

# Bump when the digest's shape changes, so an older cache is ignored rather
# than read as the new shape.
DIGEST_VERSION = 1

# Consecutive failures of one tool, by one agent, before it is called a loop.
# Three is the smallest run that is not a retry: a call failing twice is an
# agent trying again, three times with no success between is an agent stuck.
LOOP_MIN = 3

# Caps on the lists a digest keeps. A session with ten thousand failures
# needs its count, not ten thousand timestamps.
_KEEP_FAILURES = 200
_KEEP_EVENTS = 50
_KEEP_LOOPS = 20

# The event types a digest reads. Everything else is skipped before parsing.
_DIGEST_TYPES = (
    "tool.execution_start", "tool.execution_complete",
    "hook.end", "session.permissions_changed", "session.model_change",
    "subagent.started", "subagent.completed", "subagent.failed",
    "user.message", "skill.invoked",
)


def events_path(session_id: str) -> Path:
    """Where Copilot keeps a session's event log. It may not exist."""
    return db._session_state() / session_id / "events.jsonl"


def cache_path() -> Path:
    """The digest cache: under XDG_CACHE_HOME, never under COPILOT_HOME."""
    home = os.environ.get("XDG_CACHE_HOME")
    base = Path(home) if home else Path.home() / ".cache"
    return base / "cs" / "events-digest.json"


def _prefix(kind: str) -> bytes:
    return b'{"type":"' + kind.encode() + b'"'


def iter_events(session_id: str, types: Iterable[str] | None = None,
                path: Path | None = None) -> Iterator[dict]:
    """Stream a session's events, oldest first, one parsed line at a time.

    `types` narrows what is parsed. Copilot writes `"type"` as the first key
    of every line, so a line of another type is skipped on a byte comparison
    without being decoded at all — which is most of the cost of a log whose
    bulk is tool output. A line in any other shape is still parsed and
    filtered after, so a writer that reorders its keys costs speed rather
    than correctness.

    Lines that are not JSON, or not an object, are skipped: a log is written
    while the session runs and its last line may be half there.
    """
    wanted = frozenset(types) if types is not None else None
    prefixes = tuple(_prefix(kind) for kind in wanted) if wanted else ()
    source = path or events_path(session_id)
    try:
        handle = open(source, "rb")
    except OSError:
        return
    with handle:
        for line in handle:
            if wanted is not None and not line.startswith(prefixes):
                if line.startswith(b'{"type":"'):
                    continue  # a type we were not asked for, unparsed
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if not isinstance(kind, str):
                continue
            if wanted is not None and kind not in wanted:
                continue
            data = event.get("data")
            if not isinstance(data, dict):
                event["data"] = {}
            yield event


def _stamp(event: dict) -> str:
    """The event's time as `YYYY-MM-DDTHH:MM:SS` — the store's own shape."""
    value = event.get("timestamp")
    return value[:19] if isinstance(value, str) else ""


def _name(value) -> str:
    """An identifier from the log, or '' — never a structure, never long text."""
    return value[:120] if isinstance(value, str) else ""


def _empty() -> dict:
    return {
        "version": DIGEST_VERSION,
        "first": "", "last": "",
        "asks": 0,                 # messages you sent to the main agent
        "tools": {},               # name -> [calls, failures]
        "calls": 0, "failures": 0,
        "sub_calls": 0,            # tool calls made by sub-agents
        "failed_at": [],           # [[stamp, tool], ...] capped
        "last_tool": "", "last_failure": None,  # [stamp, tool]
        "hooks": {},               # hookType -> [ran, failed, last failure stamp]
        "permissions": [],         # [{at, allow_all, mode, previous}]
        "model_changes": [],       # [{at, from, to, effort_from, effort_to, source}]
        "subagents": [],           # [{at, name, model, first_model, override, ...}]
        "skills": {},              # name -> [count, last stamp]
        "loops": [],               # [{tool, run, start, end, agent}] run >= LOOP_MIN
        "longest": None,           # the longest failure run, whatever its length
    }


def _compute(path: Path) -> dict:
    """Read one log into a digest. Counts, names and stamps — no text."""
    out = _empty()
    names: dict[str, str] = {}           # toolCallId -> tool name, while open
    runs: dict[tuple[str, str], list] = {}  # (agent, tool) -> [length, start, end]

    def close_run(key: tuple[str, str]) -> None:
        run = runs.pop(key, None)
        if not run:
            return
        length, start, end = run
        entry = {"tool": key[1], "run": length, "start": start, "end": end,
                 "agent": "sub-agent" if key[0] else "main"}
        longest = out["longest"]
        if longest is None or length > longest["run"]:
            out["longest"] = entry
        if length >= LOOP_MIN and len(out["loops"]) < _KEEP_LOOPS:
            out["loops"].append(entry)

    for event in iter_events("", _DIGEST_TYPES, path=path):
        kind, data, at = event["type"], event["data"], _stamp(event)
        if at:
            out["first"] = out["first"] or at
            out["last"] = at
        agent = _name(event.get("agentId"))
        if kind == "tool.execution_start":
            call = _name(data.get("toolCallId"))
            if call:
                names[call] = _name(data.get("toolName")) or "unknown"
        elif kind == "tool.execution_complete":
            call = _name(data.get("toolCallId"))
            tool = names.pop(call, "") or _name(data.get("toolName")) or "unknown"
            failed = data.get("success") is False
            counts = out["tools"].setdefault(tool, [0, 0])
            counts[0] += 1
            out["calls"] += 1
            if agent:
                out["sub_calls"] += 1
            out["last_tool"] = tool
            key = (agent, tool)
            if failed:
                counts[1] += 1
                out["failures"] += 1
                out["last_failure"] = [at, tool]
                if len(out["failed_at"]) < _KEEP_FAILURES:
                    out["failed_at"].append([at, tool])
                run = runs.setdefault(key, [0, at, at])
                run[0] += 1
                run[2] = at
            else:
                close_run(key)
        elif kind == "hook.end":
            hook = _name(data.get("hookType")) or "unknown"
            entry = out["hooks"].setdefault(hook, [0, 0, ""])
            entry[0] += 1
            if data.get("success") is False:
                entry[1] += 1
                entry[2] = at
        elif kind == "session.permissions_changed":
            if len(out["permissions"]) < _KEEP_EVENTS:
                allow = data.get("allowAllPermissions")
                mode = data.get("allowAllPermissionMode") or data.get("mode")
                previous = data.get("previousAllowAllPermissionMode") or data.get(
                    "previousMode")
                if allow is None and isinstance(mode, str):
                    allow = mode in ("on", "allow-all")
                out["permissions"].append({
                    "at": at,
                    "allow_all": bool(allow),
                    "mode": _name(mode),
                    "previous": _name(previous),
                })
        elif kind == "session.model_change":
            if len(out["model_changes"]) < _KEEP_EVENTS:
                out["model_changes"].append({
                    "at": at,
                    "from": _name(data.get("previousModel")),
                    "to": _name(data.get("newModel")),
                    "effort_from": _name(data.get("previousReasoningEffort")),
                    "effort_to": _name(data.get("reasoningEffort")),
                    "source": _name(data.get("source")),
                })
        elif kind in ("subagent.completed", "subagent.failed"):
            if len(out["subagents"]) < _KEEP_FAILURES:
                override = data.get("explicitModelOverride")
                out["subagents"].append({
                    "at": at,
                    "name": _name(data.get("agentName")) or "unknown",
                    "model": _name(data.get("model")),
                    "first_model": _name(data.get("firstDispatchedModel")),
                    "override": _name(override),
                    "source": _name(data.get("modelSelectionSource")),
                    "tool_calls": _int(data.get("totalToolCalls")),
                    "tokens": _int(data.get("totalTokens")),
                    "duration_ms": _int(data.get("durationMs")),
                    "failed": kind == "subagent.failed",
                })
        elif kind == "user.message":
            if not agent:
                out["asks"] += 1
        elif kind == "skill.invoked":
            skill = _name(data.get("name"))
            if skill:
                entry = out["skills"].setdefault(skill, [0, ""])
                entry[0] += 1
                entry[1] = at
    for key in list(runs):
        close_run(key)
    out["loops"].sort(key=lambda loop: -loop["run"])
    return out


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


# ── The cache ────────────────────────────────────────────────────────

_CACHE: dict | None = None
_DIRTY = False


def _key(path: Path) -> list | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return [str(path), stat.st_mtime_ns, stat.st_size]


def _load_cache() -> dict:
    global _CACHE
    if _CACHE is None:
        try:
            with open(cache_path(), encoding="utf-8") as handle:
                stored = json.load(handle)
        except (OSError, ValueError):
            stored = {}
        ok = isinstance(stored, dict) and stored.get("version") == DIGEST_VERSION
        entries = stored.get("digests") if ok else None
        _CACHE = entries if isinstance(entries, dict) else {}
    return _CACHE


def _save_cache() -> bool:
    """Write the cache atomically. False — and no harm done — when it can't."""
    global _DIRTY
    if not _DIRTY or _CACHE is None:
        return True
    path = cache_path()
    partial = path.with_name(f"{path.name}.{os.getpid()}.partial")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(partial, "w", encoding="utf-8") as handle:
            json.dump({"version": DIGEST_VERSION, "digests": _CACHE}, handle,
                      separators=(",", ":"))
        os.replace(partial, path)
    except OSError:
        try:
            partial.unlink()
        except OSError:
            pass
        return False
    _DIRTY = False
    return True


def reset_cache() -> None:
    """Forget what this process read from the cache file (tests use this)."""
    global _CACHE, _DIRTY
    _CACHE, _DIRTY = None, False


def _digest(session_id: str, compute: bool = True) -> dict | None:
    global _DIRTY
    path = events_path(session_id)
    key = _key(path)
    if key is None:
        return None
    cache = _load_cache()
    held = cache.get(session_id)
    if isinstance(held, dict) and held.get("key") == key:
        return held.get("digest")
    if not compute:
        return None
    digest = _compute(path)
    cache[session_id] = {"key": key, "digest": digest}
    _DIRTY = True
    return digest


def session_digest(session_id: str) -> dict | None:
    """One session's digest, from the cache when the log is unchanged.

    None when the session has no event log — an older Copilot, or a session
    whose state directory was cleaned up. That is an absent answer, not a
    session with no failures.
    """
    digest = _digest(session_id)
    _save_cache()
    return digest


def session_ids(days: int | None = None) -> list[str]:
    """Sessions that have a log, newest first — within `days` if given.

    The window is read off each log's modification time, which is when the
    session last wrote anything: the same "last active" the listings use,
    without opening the store.
    """
    root = db._session_state()
    cutoff = time.time() - days * 86400 if days else None
    found: list[tuple[float, str]] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        try:
            mtime = os.stat(os.path.join(entry.path, "events.jsonl")).st_mtime
        except OSError:
            continue
        if cutoff is None or mtime >= cutoff:
            found.append((mtime, entry.name))
    found.sort(reverse=True)
    return [name for _, name in found]


def digests(ids: Iterable[str] | None = None, days: int | None = None,
            *, compute: bool = True, progress: bool = True) -> dict[str, dict]:
    """Digests for many sessions: `ids`, or every log within `days`.

    Only the sessions asked for are read, so a seven-day view never touches
    the rest of the store's history. A cold read of many logs reports its
    progress on stderr — never on stdout, which may be a pipe or a page —
    and only when stderr is a terminal.

    `compute=False` answers from the cache alone and reads no log at all.
    That is what a screen on a heartbeat uses: it may say less, but it can
    never stall.
    """
    wanted = list(ids) if ids is not None else session_ids(days)
    out: dict[str, dict] = {}
    cold = 0
    started = time.monotonic()
    tell = progress and compute and _stderr_is_tty()
    for index, session_id in enumerate(wanted, 1):
        before = _DIRTY
        digest = _digest(session_id, compute)
        if digest is not None:
            out[session_id] = digest
        if _DIRTY and not before:
            cold += 1
        if tell and cold and time.monotonic() - started > 0.5:
            sys.stderr.write(f"\r  reading session logs {index}/{len(wanted)} ")
            sys.stderr.flush()
    if tell and cold and time.monotonic() - started > 0.5:
        sys.stderr.write("\r" + " " * 40 + "\r")
        sys.stderr.flush()
    _save_cache()
    return out


def _stderr_is_tty() -> bool:
    try:
        return sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def turn_of(stamp: str, turn_times: list[tuple[int, str]]) -> int | None:
    """The store turn an event belongs to: the last one begun at or before it.

    The log numbers steps within each request, not turns within the session,
    so the two are joined on time. `turn_times` is (turn_index, timestamp)
    from the store, in order. None when the event predates every turn the
    store kept, or when the store has no turn times to join on.
    """
    if not stamp:
        return None
    before = [index for index, when in turn_times if when and when[:19] <= stamp]
    return max(before) if before else None
