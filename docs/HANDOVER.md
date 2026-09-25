# Handover

**Updated:** 2026-09-25
**Repository:** `smaharajan/copilot-sessions`
**Branch:** `main`. Each phase is one signed commit, pushed straight to `main`.
**Baseline:** `acf2b50` (`feat: split cs.cli into a package; add pins, budget, standup`)

## Current task

Add a set of day-to-day capabilities, every one of them reachable from the
home screen (`cs` / `cs home`), in five phases:

| Phase | Scope | State |
|---|---|---|
| 1 | `cs/events.py`: streamed, cached digests of `events.jsonl` (no UI) | **done** |
| 2 | Evidence views: tool failures, stuck loops, hook health, autonomy evidence, sub-agents, model switches, unclean endings | next |
| 3 | Day-to-day workflow: next up, end of day, weekly review, similar work, my asks, saved searches, file history, budget row, clean-up | — |
| 4 | Analysis: compare, replay, spend anomalies, repo health, prompt patterns, agent config | — |
| 5 | Operations and trust: watch, doctor, schema drift guard, team rollup | — |

The standing rules are in `AGENTS.md` and `CONTRIBUTING.md`: the store is
opened `mode=ro`, nothing is written under `COPILOT_HOME`, stored text is
masked before it is printed or exported, inferences show their evidence, and
new tables hold their shape from 40 to 140 columns.

## Phase 1 — the event-log foundation

`cs/events.py` is the only reader of `session-state/<id>/events.jsonl`.

- `iter_events(session_id, types=None)` streams a log line by line. Copilot
  writes `"type"` as the first key, so a line of an unwanted type is skipped
  on a byte comparison before it is parsed. Bad JSON, non-objects and lines
  with no type are skipped.
- `session_digest(session_id)` returns counts, names and timestamps: tool
  calls and failures by tool, sub-agent calls, hook runs and failures by
  `hookType`, permission changes, model changes, sub-agent runs, skills
  invoked, stuck-loop runs (`LOOP_MIN = 3` consecutive failures of one tool
  by one agent), and the longest failure run.
- `digests(ids=None, days=None, compute=True)` reads only the sessions
  asked for (or the logs modified within `days`). `compute=False` answers
  from the cache alone, which is what a heartbeat may use.
- The cache is `$XDG_CACHE_HOME/cs/events-digest.json` (default
  `~/.cache/cs`), keyed by `(path, mtime_ns, size)`, versioned, and written
  atomically. If it cannot be written, the digest is computed in memory.

Facts about the log that the code depends on, checked read-only against a
real store:

- `tool.execution_complete` carries **no `toolName`**. The name is on
  `tool.execution_start`, so the digest joins the two on `toolCallId`.
- `turnId` counts steps within one request and resets at each user
  message. It is **not** the store's `turn_index`. Views join an event to a
  store turn on time: `events.turn_of(stamp, [(turn_index, timestamp)])`.
- User messages sent by a sub-agent carry a top-level `agentId`. Only
  messages without it count as your asks.
- `session.permissions_changed` appears in two shapes: `allowAllPermissions`
  with `allowAllPermissionMode` (`on` or `off`), and an older
  `mode`/`previousMode` pair (`allow-all` or `manual`). Both are read.
- `explicitModelOverride` is `null` on about two thirds of sub-agent runs.

### Measured (reference machine, read-only, 2026-09-25)

| Run | Logs | Size on disk | Time |
|---|---|---|---|
| Cold digest of every log | 1,408 | 4.4 GB | 5.7 s |
| Warm digest of every log | 1,408 | — | 0.05 s |
| Cache file | — | 1.7 MB | — |

The warm figure meets the 1-second target. Cold is a one-off per changed
log, reports progress on stderr, and the home heartbeat never calls it.

### Verified

- `ruff check cs tests` and `python -m unittest discover -s tests` pass.
- `tests/test_events.py` (14 tests) covers malformed lines and missing keys,
  a cache hit that does not reopen the log, a miss after a size or mtime
  change, an unwritable and a corrupt cache, windowing by log age,
  cache-only reads, and that no text from the fixture's tool result, hook
  error, skill body or prompt (including a seeded credential) reaches the
  cache file.
- The suite points `XDG_CACHE_HOME` at scratch directories, as it already
  does for `CS_CONFIG_HOME`, so no test reads or writes a real cache.

## What's next

Phase 2. Each events-backed view reads digests for its window only, masks
anything it prints, and shows the evidence (tool, run length and turn range
for a loop; the reason and turn for an unclean ending).

## Earlier history

The theme gallery, the 60-second home refresh (deadline kept across views,
timeout re-armed before every `getch`), `_curses_wrapper` for terminals
whose terminfo declares native SGR mouse input on the legacy ncurses ABI,
two-decimal AIU in the header, and motion reporting for menus only all
shipped before this task. They are covered by `tests/test_home.py` and must
not regress. The detailed record of that work is in the git history of this
file (before `acf2b50`).
