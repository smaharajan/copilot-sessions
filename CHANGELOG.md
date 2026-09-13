# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`cs skills` counts every root Copilot loads from**, not the two it used to
  walk. A machine with forty-nine personal skills was reporting forty-nine
  while the CLI was resolving eighty-two: the enabled plugins'
  (`installed-plugins/<marketplace>/<pack>/skills`), the cross-tool
  `~/.agents/skills` and a repository's `.agents/skills`, and the handful the
  CLI package ships itself. A plugin's mirror for another harness — ponytail
  keeps one under `.openclaw` — is rejected rather than counted twice, and
  only the newest installed package of built-ins is read.
- **Enablement is read from `settings.json`.** A pack switched off in
  `enabledPlugins` ships skills Copilot will never load, so its skills are
  absent rather than listed. A skill in `disabledSkills` stays in the
  inventory under its own `Switched off` heading: eleven of the sixteen this
  view used to file as `never referenced` were deliberate choices, and
  calling a decision neglect is the one thing an inventory must not do.
- **A skill that ran gets a row even with no file to point at.** Rows come
  from the load markers in the store as well as from the disk, so the
  thirty-four per cent of this store's skill usage that was installed
  nowhere local can no longer be invisible. Where the store knows a checkout
  that ships it, the row names it — `· in meeting-notes` — and `· not
  installed` is kept for the ones that really are nowhere. `cs skills <name>`
  answers for those too instead of refusing for want of a file.
- **`cs skills --by-repo`** — which checkout each skill was actually reached
  for in, from the load markers alone. `*` marks one the checkout does not
  ship: it worked because of what you had installed, and a colleague cloning
  the repository would not get it. Ninety of ninety-seven are borrowed on this
  machine. Grouped by repository where the store recorded one, so a session
  run in a subfolder does not become a second, skill-less row. Skills only —
  agent profiles leave no marker to group, and the flag says so rather than
  drawing an empty page.
- `CS_AGENTS_HOME` moves the `~/.agents/skills` root. It sits beside the
  Copilot home rather than inside it, so `COPILOT_HOME` does not move it, and
  anything photographing or testing `cs` needs a way to keep the real one out
  of the picture — the screenshot script now sets it.
- `--json` and `--csv` carry `scope`, `state` and `sessions_loaded` per row
  and `disabled`, `ran` and `not_installed` totals. The existing `name` and
  `sessions_referencing` keys are unchanged.

- **Security says which credentials are hardcoded**, not just which text is
  credential-shaped. A finding is called `hardcoded` when the line reads as
  source (`API_PASSWORD = "…"`, `export TOKEN=…`, a quoted JSON value — never
  `the token: …` in a sentence) *and* `session_files` shows the session
  created or edited a file. Neither half alone counts. Hardcoded rows sort
  half a step above their own severity, so they are not buried under the
  mentions of the same certainty, and never above a `critical`.
- **Security reports destructive actions** — files removed, history rewritten,
  a database dropped, infrastructure torn down, permissions widened, code run
  from the network. Two tiers, because the store cannot prove any of it:
  `ran` when the session reports having done it (the command is outside a
  code fence, with a completion word near it and no negation between), and
  `proposed` for everything else, including every destructive command you
  typed yourself. `cs show` carries the same reading for one session.
  - The honest limit is printed above the table: `session_files.tool_name`
    records `create` and `edit` and **no deletion**, and no command exit code
    is stored anywhere, so this is read out of the conversation.
  - `Reported as done` is the **first** block on the page and is named in the
    lead; `Offered, outcome unknown` goes to the foot, after the credentials.
    The two tiers are printed at two ends of the report because the uncertain
    one is also the longest, and putting seventy-four rows of it between the
    lead and the credentials buries both.

### Removed

- The `issues` row on **AI spend** (`cs cost`), which read
  `0 errors · 7 filtered`. It was thirteen events out of thirty-nine
  thousand on a page about where three hundred thousand credits went, a
  content-filter trigger is not a spend fact at all, and it was the only
  reader of two `SUM(CASE WHEN …)` columns in the cost query, which go with
  it. `cs efficiency` already breaks the same calls out by finish reason
  under **Calls that ended badly** — the page whose question they answer.

### Changed

- **The landing screen has colour.** It was the one place in `cs` where a
  section heading had no accent: four grey captions over eighteen rows of grey
  label and dull blue description, with a hairline (238) four shades off its
  own background (234) and therefore invisible. Each group now draws the same
  `▌` bar `ui.heading` draws in every report, in its own hue — blue for Find,
  violet for Measure (what spend is drawn in), amber for Govern, mint for
  Reference — the menu labels are bright and bold rather than the same weight
  as the sentence explaining them, and the hairlines are visible at 240.
- The five Govern and Reference pages — Autonomy, Security, Instructions,
  Hooks and MCP servers — were laid out five different ways and now share one
  set of components. Section headings all draw their hairline to the same
  right edge as the rule above them, a table's headings and its rows are
  built by one function so they cannot be spaced differently, and a
  right-aligned number no longer sits flush against the word beside it
  (`steps  per turn summary` read as a sentence, not as three headings).
- **Autonomy** (`cs yolo`) opens on the verdict rather than on a table, and
  files each session under its verdict — so the `mark` column and the
  repeated sentence under every YOLO row are both gone, replaced by a section
  heading and an `evidence` column. Its inference note moved behind `--why`.
- **Security** (`cs audit`) no longer prints `inspect cs read <id> --turn <n>`
  on every row: the session and the turn it needs are already columns of the
  row above, so the command is named once per section and the masked evidence
  gets the width it was sharing. The severity counts are drawn as the same
  tier block Autonomy opens with. The page is titled `Security`, the name of
  the menu row that opens it, rather than `Security posture` on a wide window
  and `Security` on a narrow one.
- **MCP servers** is titled `MCP servers`, matching its menu row. Its name
  column takes what the longest name needs instead of a flat twenty, `https://`
  is dropped from the endpoint column, and `from` no longer truncates.
- **Instructions** keeps `chars` when the window narrows — it is the figure
  the limit applies to — rather than dropping it first. The two faults get a
  section of their own, and the remedy is printed once for the group instead
  of once per file.
- With nothing configured, **Hooks** and **MCP servers** print where they
  looked with the scope in its own column and the path wrapped after a `/`
  rather than truncated mid-name.
- The transcript (`cs read`, and `t` from any listing) is set differently.
  Each turn drew three full-width rules — its own, and one per speaker — all
  the same weight, plus a lone line of grey underneath carrying the size. A
  turn now opens on one rule that carries the ask at one end and the time and
  size at the other, and each speaker's words run beside a coloured rail so
  who-said-what survives scrolling. Section headings inside a reply are set in
  the accent rather than in bold, which a reply full of `**bold**` already
  uses, and a blockquote is marked rather than left with its `>`.
- `ui.rule()` takes a `note` that rides its right-hand end, and `ui.spine()`
  is new — both are shared primitives, so any view can be set this way.
- The turn index has gone from `cs show`. It was a third rendering of the same
  list: the page already prints the first and last request, `--asks` prints
  every one of them numbered, and `cs read` is the conversation itself. On a
  hundred-turn session the summary closed with a hundred lines of truncated
  prompt. `--asks` now carries the `--turn N` command the index used to.
- `cs show` and `cs brief` no longer read 2,000 characters of every prompt in
  the session in order to count them.

### Fixed

- Standing in your home directory no longer renames your own kit.
  `.copilot/skills` is a *project* pattern, and in `$HOME` it **is** the
  personal directory, so the inventory reported all forty-nine personal skills — and
  all twenty-five agent profiles — as shipped by "this repo".
- `cs context` and `cs skills` count the same skills again. They had drifted
  by one: a recursive rule belonging to one root was being applied to
  another, which found a stray page inside a documented skill and filed it as
  a skill named `skill`.
- The screen and `--json` report the same figures. Each built its own list,
  and only one of them knew about the skills that ran without being
  installed, so the screen said forty-six referenced where the export said
  thirty-five.

- A `session-store.db` that is not a database — a truncated download, a file
  restored from the wrong backup, a store mid-write — produced a raw
  `sqlite3.DatabaseError` traceback instead of the sentence every other bad
  `COPILOT_HOME` gets.
- The `credentials masked · CS_REDACT=0 …` line in every session footer was
  the one fixed-width string on an otherwise width-aware page, and ran off any
  window under about fifty columns. It shortens now.
- The speaker label put the name in a different column depending on
  `CS_GLYPHS`, because the emoji marks are two cells wide and their plain
  forms are one.
- Installing on Python 3.9 no longer gets as far as running. `pip` honours
  `requires-python`, but `install.sh` and `bin/cs` bypass pip, and every module
  imports `annotations` from `__future__` — so an old interpreter used to start
  cleanly and fail later inside a view. `cs/__init__.py`, which every entry
  point imports, now refuses up front and names Homebrew as the way out.
- `install.sh` checks the Python version before creating the symlink rather
  than leaving a link that cannot work.
- `install.sh` warns when another `cs` earlier on `PATH` will keep answering,
  which previously made a successful install look like it had done nothing.

### Changed

- Package metadata gained the supported Python versions, operating systems and
  the issue, source and changelog links.

## [1.0.0] — 2026-08-17

First public release. `cs` reads the local GitHub Copilot CLI session store,
read-only, and reports on it in the terminal. Python 3.10+, standard library
only.

### Added

**Listing and history**

- `cs` / `cs home` — landing screen, every view one keypress away.
- `cs recent`, `cs all`, `cs repos` — sessions by recency, including quiet and
  automated ones, or grouped by repository.
- `cs timeline` — working days, with sessions, turns and spend per day.

**Cost and efficiency**

- `cs cost` — AI spend broken down by model, repository and day.
- `cs efficiency` — whether it had to cost that: cache hit rate, rate
  multiplier, first-token latency and reasoning share.
- `cs stats` — the output ledger: commits, PRs, files, cost and delegation.

**What ran, and on whose behalf**

- `cs agents` — delegation split across you, the main agent and sub-agents.
- `cs skills`, `cs profiles` — what is configured on disk versus what sessions
  actually referenced and loaded.
- `cs instructions` — the instruction files every session in a repository
  starts with, in both scopes, naming the ones past Copilot's
  4,000-character truncation limit and counting the shortfall.
- `cs mcp` — MCP servers wired up, local and remote, and what they may call.
- `cs hooks` — commands Copilot runs on the session lifecycle, flagging any
  that point at a script that no longer exists.

**Governance**

- `cs yolo` — which sessions ran unattended, and the evidence for saying so.
- `cs handoff` — sessions that wrote or picked up a handoff, and the chain a
  session belongs to.
- `cs audit` — credential-shaped text found in sessions, reported by name and
  prefix only, never by value.

**Finding and resuming**

- `cs search` — full-text across summaries, repositories, both sides of every
  turn and session checkpoints, with `AND` / `OR` / `NEAR` and phrases.
- `cs show` — one session whole: what is open, what was asked and done, then
  spend, files, skills, agents, risk and turns. `--short` (aliased as
  `cs brief`) stops after the story; `--asks` lists every request in order.
- `cs read` — the conversation itself, both sides, in full. `--turn N` prints
  one turn. `cs transcript` is an alias.
- `cs files` — sessions that touched a path, with globs and partials.
- `cs resume` — changes to the session directory and runs `copilot --resume`.

**Getting it out**

- `--json` and `--csv` on the main views, `cs export` for one session as
  Markdown, and `cs completion` for bash, zsh and fish. Credentials are masked
  on the way out exactly as they are on screen — a file is more exposed than a
  screen, not less.
- `cs <view> --why` — how to read any view, section by section.

### Security

- The store is opened `mode=ro` through a SQLite URI. There is no write path.
- No network access and no runtime dependencies.
- Session content is treated as untrusted input: credential-shaped text is
  masked at the render edge in `cs/redact.py`, and terminal control sequences
  and row-breaking characters are stripped before anything is drawn.

[1.0.0]: https://github.com/smaharajan/copilot-sessions/releases/tag/v1.0.0
