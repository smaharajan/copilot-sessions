# Contributing

Thanks for taking a look. This is a small, deliberately boring codebase and the
bar for changes is "does it earn its complexity".

## Getting set up

Python 3.10+, no dependencies.

```bash
git clone https://github.com/smaharajan/copilot-sessions.git
cd copilot-sessions
python -m unittest discover -s tests     # tests use a synthetic store
pipx install ruff && ruff check cs tests # lint
```

Tests build a throwaway session store via `COPILOT_HOME`, so **they never touch
your real Copilot data** and run identically in CI. The curses UI is tested
through a fake screen that records every frame, so keys, mouse reports and
redraws are asserted without a terminal.

CI runs exactly two commands on Python 3.10 and 3.12:

```bash
ruff check cs tests
python -m unittest discover -s tests
```

Run both before opening a PR.

## Where code goes

Eleven modules, one job each. Keeping the seams is most of the design:

| Module | Owns | Must not know about |
|---|---|---|
| `db.py` | SQL, one function per question | formatting |
| `signals.py` | inferences about one session (autonomy, handoffs, exposure) | drawing |
| `practice.py` | inferences about habits across a window | drawing |
| `events.py` | `session-state/*/events.jsonl`, streamed into cached counts — never its text | drawing |
| `redact.py` | every credential pattern | sessions |
| `ui.py` | colour, boxes, tables, text fitting | sessions |
| `export.py` | `--json` / `--csv` — the readings without the drawing | layout |
| `hooks.py` | hooks configured for the next session | the store |
| `mcp.py` | MCP servers configured for the next session | the store |
| `context.py` | instruction files on disk | the store |
| `cli/` | commands, dispatch, the interactive UI (package) | — |

`db.py` returns plain tuples, never formatted strings. The split between
`db.py` and `signals.py` is the split between **recorded** and **inferred** —
if a function's answer is a judgement rather than a fact, it belongs in
`signals.py` and must carry its evidence.

Three of them — `hooks.py`, `mcp.py`, `context.py` — never open the store at
all. They describe what *will* happen on the next session, not what a past one
did, and their views have to say so; a configuration inventory read as a usage
count is the easiest wrong answer to give here.

`ui.py` and `export.py` are the two output edges: one draws for a person, the
other hands back plain data for a pipe. Masking applies to both — a file is
more exposed than a screen, not less.

The full reasoning lives in [ARCHITECTURE.md](ARCHITECTURE.md).

## House rules

- **The store is read-only.** It is opened `mode=ro` and there is no write
  path. Any PR that adds one will be declined.
- **No runtime dependencies.** Standard library only. This is a feature, not an
  oversight — it is what makes `cs` installable on a locked-down machine.
- **Never print stored text directly.** Session content is untrusted. It goes
  through the masking and sanitising path in `redact.py` / `ui.py` first.
- **Inferences state their evidence.** A report that says a session ran
  unattended must also say what made it think so.
- **Simplest thing that works.** No abstraction for one caller, no config for a
  value that never changes.

## Changes that need a test

Anything with a branch, a loop, a parser, or a credential path. In particular:

- A new credential rule in `redact.py` needs a case in the corpus in
  `tests/test_governance.py` — one string that must mask, and one near-miss
  that must **not**. Use a **synthetic** value and mark the line
  `# gitleaks:allow`. The corpus lives there because `cs audit` is the view
  that scans for secrets, so the rule and its caller are tested together.
- A new report or table needs to hold its shape at 40, 60, 80, 100 and 140
  columns. There is a shared width test that will do this for you.

## Where tests go

`tests/` is split by subject, and each file says at the top what it is
responsible for. Put a test next to the behaviour it describes:

| File | Covers | Tests |
|------|--------|-------|
| `support.py` | Not a test file. The synthetic store and the fake curses screen every other file imports | — |
| `test_core.py` | Listing, search, show, read, resume, export, and the flags that cut across them | 110 |
| `test_config.py` | Hooks, MCP servers, instruction files, capability detection, and config that does not parse | 74 |
| `test_home.py` | The landing screen — menu rows, icons, banner, animation | 51 |
| `test_views.py` | What a view may claim: clarity, restored state, quiet defaults, skill attribution | 50 |
| `test_governance.py` | Unattended runs, handoff chains, the credential audit and its masking corpus | 46 |
| `test_reports.py` | Sorting, spend windows, efficiency and the machine-readable output | 42 |
| `test_render.py` | Charts, themes, glyph fallback, cell widths, markdown shapes, search snippets | 31 |
| `test_transcript.py` | How a conversation is set on the page: furniture, attribution, width | 27 |
| `test_practice_rules.py` | Every `cs coach` rule, fired and silenced — one fixture each | 18 |
| `test_practice.py` | Inferences drawn across a window of sessions (the unlisted Improve views) | 15 |
| `test_surface.py` | Every command, run — against a full store, an empty one, and one from an older Copilot | 14 |
| `test_analysis.py` | Compare, replay and its stepping, spend anomalies and their evidence, repo health, prompt patterns, agent-config columns, and the listing keys that open them | 17 |
| `test_today.py` | Next up, end of day, weekly review, similar work, my asks and copying one, saved searches, file history, the budget check and row, clean-up | 22 |
| `test_evidence.py` | Tool failures, stuck loops, sub-agents, model switches, unclean endings, hook runs and recorded autonomy — each with its evidence and masked | 19 |
| `test_events.py` | The event log: malformed lines, the digest cache and its misses, and that no result text reaches the cache | 14 |
| `test_security.py` | Hostile stored text — terminal control sequences that must never reach the screen | 2 |

Three of those are structural rather than behavioural, and are the ones to
keep working when you add something:

* **`test_surface.py` reads the command list out of `cli._dispatch`.** A new
  command is smoke-tested the day it is added, against all three stores — and
  if it needs an argument, add it to that file's `ARGUMENTS` table or the
  smoke test will run it bare and fail on the usage line.
* **`test_practice_rules.py` fails until a new `coach` rule has a fixture**
  that fires it and one that does not. Half the rules had never been executed
  by any test before it existed.
* **The credential corpus in `test_governance.py` must name every rule in
  `redact._RULES`.** A pattern that stops matching is silent by construction:
  the audit simply reports less, which reads as good news.

Fixtures are imported as `from support import _build_store, Screen` — plain,
not package-relative, because `unittest discover -s tests` puts `tests/` on
`sys.path`. There is deliberately no `__init__.py`.

## Commits and PRs

- One logical change per commit; a message that says *why*, not *what*.
- Explain the user-visible behaviour in the PR description, and paste the
  before/after terminal output for anything that changes a view.
- Screenshots are welcome for UI changes, but paste the text too — it is
  searchable and diffable.

## Reporting bugs

Include your terminal, its width, `python --version`, and the exact command.
For a rendering bug, paste the output rather than describing it.

**Security issues do not go in the issue tracker** — see
[SECURITY.md](SECURITY.md).
