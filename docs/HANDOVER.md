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
| 2 | Evidence views: tool failures, stuck loops, hook health, autonomy evidence, sub-agents, model switches, unclean endings | **done** |
| 3 | Day-to-day workflow: next up, end of day, weekly review, similar work, my asks, saved searches, file history, budget row, clean-up | **done** |
| 4 | Analysis: compare, replay, spend anomalies, repo health, prompt patterns, agent config | **done** |
| 5 | Operations and trust: watch, doctor, schema drift guard, team rollup | **done** |

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

## Phase 2 — evidence views

All in `cs/cli/evidence.py`; each has a `_*_data(days)` reading (masked,
used by `--json` through `export.py`) and a renderer.

| Command | Home row | Evidence shown |
|---|---|---|
| `cs failures [N\|all]` | Tool failures → Govern (period) | by tool, by repo, worst sessions numbered for `cs show N` |
| `cs failures --loops` / `cs loops` | Stuck loops → Govern (period) | tool, run length, turn range, main or sub-agent |
| `cs endings [N\|all]` | Unclean endings → Govern (period) | finish reason and turn of the last billed call (store only) |
| `cs subagents [N\|all]` | Sub-agents → Measure (period) | declared `model:` vs overrides and models actually used |
| `cs switches [N\|all]` | Model switches → Measure (period) | from → to, effort, source, AIU before and after |
| `cs hooks` (extended) | Hooks → Reference | `ran`, `failed`, `last failure` per event, last 30 days |
| `cs yolo` (extended) | Autonomy → Govern | `recorded` (permissions_changed) vs `inferred` |
| `cs show` (extended) | — | failures per turn, stuck loops |

Decisions worth knowing:

- A loop is `LOOP_MIN = 3` consecutive failures of one tool **by one agent**;
  a success of that tool by that agent ends the run. Other tools' calls in
  between do not.
- Listings show a `!` in the cell after `#N` (the pin precedent uses the one
  before it), read from the digest cache only. A cold cache shows no marker
  until an events-backed view has run; the Stuck loops view is the full
  answer.
- Endings flag `error`, `length`, `content_filter` and empty. `tool_calls`
  is treated as clean: on the reference store it is how a session you stop
  between steps ends (47 of 924 sessions).
- Ranked tables number their sessions and save the `#N` index, as a
  listing does, so `cs show 1` after `cs failures` opens the worst session.
  That replaces the previous listing's numbers, which is the documented
  meaning of `#N` ("a row from your last listing").
- The yolo evidence column now gives way last on a narrow window; the
  `source` column goes before it.
- Home type-to-filter now prefers a row whose label matches over one whose
  description does (typing `sub-agents` used to open Delegation). Found in
  the tmux check.

### Measured (reference store, warm cache, read-only)

| View | Time |
|---|---|
| `cs failures` | 0.47 s |
| `cs failures --loops` | 0.23 s |
| `cs subagents` | 0.45 s |
| `cs switches` | 0.50 s |
| `cs endings` | 0.43 s |
| `cs hooks` | 0.28 s |
| `cs yolo` | 0.37 s |
| home snapshot, cold process | 1.65 s |

### Verified

- Lint and the full suite pass on Python 3.12 and 3.10.
  `tests/test_evidence.py` covers every view, its `--json`, the evidence on
  each row, the listing and TUI markers, the home rows, and masking of a
  credential seeded in a tool name, an agent name, a model name and a
  session summary.
- The shared width test (`test_no_report_runs_off_the_window`) now holds
  the five new views to 40, 60, 80, 100 and 140 columns.
- `test_surface` gains a whole-tree hash check: after every command runs,
  with and without `--json`, `COPILOT_HOME` including `session-state` is
  byte-identical.
- tmux, `TERM=xterm-ghostty`, at 100x40 and 40x24: every new row is found by
  typing and opens; Esc returns home; arrows reach Help; the theme gallery
  opens and cancels; `updated HH:MM:SS` advanced 60 s after a mouse hover
  and 60 s after returning from a view.

## Phase 3 — day-to-day workflow

All in `cs/cli/today.py`, readings as `_*_data` shared with `--json`.

| Command | Home row | Notes |
|---|---|---|
| `cs next [N\|all]` | Next up → Today (first row) | handoff 5 · ended 4 · stuck 3 · wip 2 · pin 1; opens as a listing (`r` resumes) |
| `cs standup` | Standup → Today (moved) | unchanged |
| `cs eod [--md]` | End of day → Today | since local midnight |
| `cs weekly [--md]` | Weekly review → Today | vs the previous 7 days; top 3 `coach` findings |
| `cs budget --check` | Budget → Today | ←/→ on the row steps `ui.BUDGET_STEPS` |
| `cs similar <words>` | Similar work → Find (term) | `db.search`, shipped first, stable |
| `cs asks [--repo .] [N\|all]` | My asks → Find (period) | `c` copies in the listing |
| `cs search --save`, `cs saved [name]` | Saved searches → Find | picker; settings key `saved_searches` |
| `cs files <path> --history` | File history → Find (term) | agent read from the log on demand, never cached |
| `cs cleanup [N]` | Clean-up → Improve | suggests commands; removes nothing |

Decisions worth knowing:

- **Open handoff** = a session asked to write a handoff (role emitted or
  both) where no *later* session touched any handoff document it touched
  (`signals.open_handoffs`). A later session that only quoted its id is not
  a pickup; the document is the contract.
- **Next up ignores an unrecorded finish reason.** `cs endings` still lists
  it as unknown, but there is nothing in it to act on.
- **End of day is since local midnight**; the budget and the header remain
  "last 24 hours", which is what the budget has always meant.
- The home loop now treats `budget` as an ask that takes no argument, and
  ←/→ on that row steps the limit instead of the window. The refresh
  deadline is untouched; the header is re-read so the new limit shows.
- Home grouping now needs `heads * 2 + 4` spare rows (was `heads * 3`), so
  six groups still show from 20 rows up.
- Functions in `today.py` import listing helpers at module level, not
  inside the function, so they resolve through `cs.cli` like everything else
  and tests that patch `cs.cli.*` reach them.
- A ranked listing whose title ends "… first" no longer adds "best match
  first" to its heading.

### Verified

- Lint and the full suite (668 tests) pass on 3.12 and 3.10.
- Width test covers next, eod, weekly, asks, clean-up, file history and
  saved searches at 40–140; every view also checked at 40 on an empty store.
- tmux, `TERM=xterm-ghostty`, 100x40 and 40x24: all nine new rows found by
  typing, opened (term rows with a typed term), and returned; ←/→ on Budget
  changed the limit (at 40 columns the descriptions are hidden and the change
  shows in the header); arrows reach Help; theme gallery; `updated` advanced
  after a hover and after a view.

## Phase 4 — analysis

All in `cs/cli/analysis.py`.

| Command | Home row | Notes |
|---|---|---|
| `cs diff <a> <b>` | Compare sessions → Measure (pair) | stacks below 64 columns; listing `d` … `d` |
| `cs replay <ref>` | Replay → Find (ref) | reuses `_transcript_turn` from `cs read`; listing `e` |
| `cs anomalies [N\|all]` | Spend anomalies → Measure (period) | `ANOMALY_FACTOR = 2.0`, `BASELINE_DAYS = 14`, needs 3 comparable points |
| `cs health [--repo .]` | Repo health → Improve | instructions and skills only for the checkout you are in |
| `cs patterns [N\|all]` | Prompt patterns → Improve (period) | five features, `n` per side, correlation note |
| `cs skills`, `cs profiles` (extended) | Skills, Agents → Reference | invoked, last used, model kept |

Decisions and fixes worth knowing:

- `_transcript_turn` was extracted from `_render_transcript` so `cs read`
  and `cs replay` draw a turn with the same code.
- The reader (`_reader_tui`) gained a step mode: a `sort` dict with `steps`
  makes ←/→ move between pages (clamped at the ends) and hides `s`.
- **Lifting gotcha, fixed.** A function-local `from .x import f` inside a
  lifted `cs.cli` function returns the *un-lifted* `f`, whose `_page`
  raises `KeyError: '_read_in_place'`. The listing's `d`/`e` keys hit it
  and so, since before this work, did `v`/`o`/`t` on the **Pinned** listing
  (`cmd_pins`). Both now resolve through `globals()[...]`; regression tests
  cover each. Top-level imports are fine.
- Home type-to-filter ranks label matches exact → prefix → substring →
  description (typing `agents` opened Sub-agents; found in the tmux check).
- `db.opening_prompts(..., first_only=True)` fetches only each session's
  first prompt; `cs asks` masks once (`_user_text` already masks), which
  took `asks` from 2.35 s to 1.29 s and `patterns` from 2.06 s to 1.31 s.
- The suite is not `COLUMNS`-agnostic: with `COLUMNS=100` exported, the
  baseline fails 26 tests. CI does not set it; do not export it when running
  the tests locally.

### Measured (reference store, warm cache, read-only)

| View | Time | Note |
|---|---|---|
| `cs diff 1 2` | 0.23 s | |
| `cs replay 1` | 0.23 s | piped: every turn |
| `cs anomalies` | 0.85 s | store only |
| `cs next` / `eod` / `weekly` | 0.56 / 0.52 / 0.66 s | |
| `cs similar <words>` | 0.23 s | |
| `cs asks` | 1.29 s | 241 opening asks, masked |
| `cs patterns` | 1.31 s | |
| `cs health` | 2.11 s | includes the skills inventory scan |
| `cs skills` / `cs profiles` | 1.83 / 1.71 s | baseline `cs skills` was 1.81 s: the reference scan predates this work |

Over the 1-second target: `asks`, `patterns`, `health`, `skills`, `profiles`.
None of them reads the event logs cold, and the extra cost is the store's
turn scans (prompt masking, skill references), not the digest. Deferred:
caching masked opening asks and the skills reference scan across processes
would need a text cache, which the counts-only rule forbids. Worth a
maintainer decision.

### Verified

- Lint and the full suite (686 tests) pass on 3.12 and 3.10.
- Width test covers diff, replay, anomalies, health and patterns at
  40–140 columns; skills and profiles checked at 40, 60 and 100.
- tmux, `TERM=xterm-ghostty`, 100x40 and 40x24: all seven Phase 4 rows
  found by typing, opened (Replay and Compare with typed sessions) and
  returned; replay → moves to the next turn and stops at the last; listing
  `e` opens a replay, `d` marks (status line says so), `d` on another
  opens the comparison; the 60 s refresh advanced after a hover and after a
  view.

## Phase 5 — operations and trust

All in `cs/cli/ops.py`, plus `db.EXPECTED_SCHEMA` / `db.schema_drift`.

| Command | Home row | Notes |
|---|---|---|
| `cs watch` | Watch live → Today (last row) | `WATCH_SECONDS = 5`; tail from the last offset, first look reads the last 256 KB, at most 4 MB a tick |
| `cs doctor [--json]` | Doctor → Reference | nine checks, pass/warn/fail with a fix; writes nothing (`os.access` only) |
| schema drift guard | home status line; Doctor | `KNOWN_SCHEMA_VERSIONS = (8,)`; no `schema_version` = older store, not drift |
| `cs rollup [N\|all] --json` | Team rollup → Measure (period) | counts and rates; repos as `sha256(salt\0name)[:12]`; salt in settings |

Decisions worth knowing:

- Watch picks the most recently active session each tick, so a new session
  started elsewhere takes over the pane. The burn rate is spend billed in
  the last `BURN_MINUTES = 10`.
- The watch loop re-arms `timeout` before every `getch` (at most 1 s) and
  re-reads only on its 5-second deadline, which is kept, not reset, by
  keypresses — the same discipline as home. Tested with a screen that
  records every `timeout` call.
- Doctor's store check does not try a write to prove read-only: the store
  is opened through `db.connect()` with `mode=ro`, and that is reported.
- The drift check runs on home start and on each refresh: a few `PRAGMA
  table_info` calls on a fresh read-only connection, measured at 0.07 s
  including import. It reads no event log.
- The rollup leaves out model names as well as text, ids, paths and user
  names. `tests/test_ops.py` collects every text value in the fixture store
  (ids, titles, repositories, directories, prompts, replies, paths, ref
  values, models) and asserts none appears in `rollup`, `rollup all
  --json` or the paged form. Schema enumerations such as finish reasons are
  vocabulary and are left out of that check.

### Measured (reference store, warm, read-only)

| Run | Time |
|---|---|
| `cs doctor` | 0.24 s |
| `cs rollup --json` | 0.23 s |
| `cs watch`, piped snapshot | 0.42 s |
| watch tick, first / next | 0.20 / 0.03 s |
| home start data, cold process | 1.64 s |

## Final state

All 27 items are shipped and selectable from the home screen:

| Group | Rows |
|---|---|
| Today | Next up · Standup · End of day · Weekly review · Budget · Watch live |
| Find | Recent sessions · Pinned · All sessions · Search · Similar work · My asks · Saved searches · File history · Replay |
| Measure | Repositories · Stats · AI spend · Efficiency · Delegation · Sub-agents · Model switches · Spend anomalies · Compare sessions · Team rollup |
| Govern | Autonomy · Handoffs · Security · Tool failures · Stuck loops · Unclean endings |
| Improve | Practice · Rhythm · Context · Repo health · Prompt patterns · Clean-up |
| Reference | Skills · Agents · Instructions · Hooks · MCP servers · Doctor · Theme · Help |

Working days stays commented out. CLI-only mechanics are reachable through
their rows: the budget check through Budget, schema drift through Doctor and
the status line, extensions (`hooks`, `yolo`, `show`, `files --history`,
`skills`, `profiles`) through their existing rows.

Final verification:

- `ruff check cs tests` clean; `python -m unittest discover -s tests` — 703
  tests, OK on Python 3.12 and 3.10 locally; CI green on 3.10 and 3.13 plus
  the secrets scan for every pushed phase.
- tmux, `TERM=xterm-ghostty`, at 100x40 and 40x24: all 44 non-theme rows
  found by typing and opened (term, ref and pair rows with typed input), and
  Esc returned home from each; arrows reach Help; the theme gallery opens
  and cancels; ←/→ on Budget changes the limit; Watch live advanced its own
  timestamp every 5 s and `q` returned; home `updated HH:MM:SS` advanced 60
  s after a mouse hover and 60 s after returning from a view.
- The drift notice showed at 40 and 100 columns with the refresh time kept,
  on a scratch copy of the store with `schema_version` 99.
- Read-only proof: a hash listing of the synthetic `COPILOT_HOME` (store and
  `session-state` log) was byte-identical after every phase's checks, and
  `test_surface` asserts the same for the whole tree after every command,
  with and without `--json`.

## Deferred, with reasons

- **Five views over the 1-second warm target** on the reference store:
  `asks` 1.29 s, `patterns` 1.31 s, `health` 2.11 s, `skills` 1.83 s,
  `profiles` 1.71 s. `skills` was 1.81 s before this work (its reference
  scan), and `health` includes that scan. The rest is masking opening
  prompts. Going faster needs a cross-process cache of masked text or of
  reference scans, which the "counts and ids only" cache rule forbids.
  Needs a maintainer decision.
- **The listing's stuck-loop marker reads the digest cache only**, so it
  appears once any events-backed view has read those logs. Reading logs from
  a listing would put a cold scan on its heartbeat.
- **The rollup page is not in the width test.** It is a JSON document; the
  reader and `less` wrap it. Its readings are covered by `--json` tests.
- **Release.** No version bump, tag or GitHub release was made: the
  maintainer will review and release. `CHANGELOG.md` keeps everything under
  Unreleased.

## What's next

Maintainer review, then a release: bump the version in `pyproject.toml` and
`cs/__init__.py`, move Unreleased in `CHANGELOG.md` under the new version, and
tag.

## Earlier history

The theme gallery, the 60-second home refresh (deadline kept across views,
timeout re-armed before every `getch`), `_curses_wrapper` for terminals
whose terminfo declares native SGR mouse input on the legacy ncurses ABI,
two-decimal AIU in the header, and motion reporting for menus only all
shipped before this task. They are covered by `tests/test_home.py` and must
not regress. The detailed record of that work is in the git history of this
file (before `acf2b50`).
