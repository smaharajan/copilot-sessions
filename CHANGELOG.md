# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Typing on the home screen lands on the row you named.** A row whose
  label matches now beats one that only mentions the word in its
  description, so typing `sub-agents` opens Sub-agents rather than
  Delegation.

- **`cs audit` defaults to the last 30 days.** An unscoped store-wide scan
  re-read every turn on every call; bare `cs audit` now windows on
  `MAX(created_at, updated_at)` like the other reports, with `cs audit all`
  (or a day count) for the whole store. A session id still scans that
  session in full. Home Security follows the ←/→ period.
- **Skill-reference and session-name lookups are cached in-process.**
  `reference_counts` / `sessions_for_asset` reuse one turn scan (and the
  skill-load map) until the store file's mtime or size changes;
  `session_names()` keeps the workspace.yaml map until any watched file's
  mtime/size set changes — so the home heartbeat and repeated listings do
  not re-walk the store.

### Added

- **Evidence views from the event log.** `cs failures [N|all]` (tool calls
  that failed, by tool, by repository and the worst sessions, numbered for
  `cs show N`); `cs failures --loops` / `cs loops` (three or more consecutive
  failures of one tool by one agent, with the tool, the run length and the
  turn range); `cs subagents [N|all]` (runs, models, overrides, tool calls,
  tokens, total and median time, and agents whose declared `model:` was never
  applied); `cs switches [N|all]` (model or effort changes mid-run, their
  source and the spend before and after); and `cs endings [N|all]` (sessions
  whose last call ended in `error`, `length`, `content_filter` or no recorded
  reason, with the turn). All take `--json` and have home rows: Tool failures,
  Stuck loops and Unclean endings under Govern; Sub-agents and Model switches
  under Measure.
- **`cs hooks` shows how hooks actually ran.** The lifecycle table gains
  `ran`, `failed` and `last failure` per event, from the last 30 days of event
  logs, including events a plugin declared.
- **`cs yolo` reads recorded evidence.** A `session.permissions_changed`
  event switching allow-all on is now evidence in its own right, and every
  row says whether its verdict is `recorded` or `inferred`.
- **`cs show` places tool failures on turns**, and names any stuck loop.
- **Listings mark stuck sessions** with a `!` in the spare cell after `#N`,
  from the digest cache only, so a listing never waits on a log.
- **`cs` reads Copilot's per-session event logs.** A new `events` module
  streams `session-state/<id>/events.jsonl` — never loading a file whole —
  and reduces each log to a digest of counts: tool calls and failures by
  tool, hook runs and failures by type, permission and model changes,
  sub-agent runs, and the longest run of consecutive failures of one tool.
  Digests are cached in `~/.cache/cs/events-digest.json` (honours
  `XDG_CACHE_HOME`), keyed by each log's size and mtime, and hold no text
  from a tool result, prompt or reply. This is the foundation for the
  evidence views that follow; on its own it changes no view.
- **Pins, notes/tags, and a daily AIU budget.** Sticky daily-workflow
  prefs live in `~/.config/cs/settings.json` beside the theme (never in
  COPILOT_HOME): `cs pin` / `unpin` / `pins`, `cs note`, `cs tag` /
  `untag`, and `cs budget [N|clear]`. Listings float pinned sessions to
  the top and mark them; `p` toggles a pin in the TUI. Home gains a
  Pinned row under Find, and when a budget is set the header shows
  `spent / budget AIU` for the last 24 hours (same window as standup),
  amber from 70% and rose when over.
- **Improve is back on the home screen, with `cs standup`.** Practice,
  Rhythm and Context were restored to the landing menu and `cs help` (they
  never stopped running when typed). `cs standup` / `cs daily` is a
  deterministic, offline daily brief — activity, notable sessions, handoffs
  in the window, and a light unattended count when the window is small —
  defaulting to the last day. `--json` emits the same readings. Working days
  (`cs timeline`) stays off the menu on purpose.

### Fixed

- **Search finds every session that matches, not the first forty.** Results
  were cut at 40 sessions with nothing on screen to say so, and the title
  counted what survived the cut — so a term mentioned across more sessions
  than that lost the rest, including recent ones, while the header read as
  if that were all there was. A project acronym found in 55 sessions showed
  40. Every match is returned now, from the full-text index and from the
  turns scan an older store falls back to. It is also faster on common
  words: the index is ranked without reading any text, and a snippet is
  taken only from each session's best row rather than from every row, so a
  search matching over a thousand sessions went from 0.8s to 0.2s.
- **A session you renamed is found, and listed, by the name you gave it.**
  `/rename` writes the new name to `session-state/<id>/workspace.yaml`,
  which is what Copilot's own session list shows, and never updates the
  store's `summary`. Searching for a renamed title could therefore miss the
  session, and listings still showed its original summary. A name
  you gave a session is now its title in `recent`, `all`, `search` and
  `files`, and on its page. Search also matches the name Copilot
  generated, which is not promoted to the title, since the store's summary
  is usually the better of the two. A session that matched on a title the
  listing is not showing says which one, masked like any other session
  text. The files are read, never written.
- **No more gap between a session's title and its repository.** The
  full-screen listing put the summary before the repository and gave it
  every cell the other columns left, so on a wide window the repository sat
  at the far edge — a hundred blank cells after a short title, with nothing
  to carry the eye from one to the other. The repository now comes first,
  only as wide as its longest name, and the summary is the last column, so
  spare width falls off the end of the row. `←`/`→` step through the
  columns in the order they are drawn.
- **Filtering a listing reaches the conversations, not just the titles.**
  `/` in *Recent* or *All sessions* matched only the summary, repository
  and folder of the rows on screen, so a project named in dozens of
  conversations but in few titles was nearly unreachable from a listing:
  filtering *All sessions* for one found 5 sessions where `cs search`
  found 56. The filter now asks the store the same question `cs search`
  does and keeps the rows it answers, alongside the titles that contain
  the letters typed, and the line under the list says what matched in the
  highlighted session. Typing into a listing also starts the filter: a
  letter with no job of its own used to do nothing, which read as a search
  that found nothing. The listing's own keys (`v` `o` `t` `r` `s` `g` `q`)
  keep their jobs.
- **Long transcripts wrap and can be searched from the menu.** `/` finds
  text, highlights matches across wrapped lines, and `n` / `N` move through
  matching rows. Searching handles Unicode case folding and refreshes after
  sorting or resizing. Quote and tool-output gutters repeat on wrapped rows.
- **Small windows retain the useful parts of the interface.** Listings give
  titles space before optional columns, wide characters fit their cells,
  and menu arrows show when more entries are available by scrolling.

## [1.1.0] — 2026-09-23

### Added

- **An open session listing re-reads the store every 60 seconds.** The
  landing page had the heartbeat; the listing took its rows as a parameter,
  queried once before the view opened, and never read again — so a session
  started in another window arrived only when the view was next reopened,
  which on a screen you sit and watch reads as the listing being wrong.
  `#N` survives a refresh: a session already listed keeps the number you
  read it under and a new arrival takes the next free one, because the
  status line offers `cs resume N` as something to type after you quit.
  A refresh that cannot read the store leaves the rows on screen alone, and
  search results are not re-read.
- **A session that is still running is listed while it runs.** Copilot
  records usage events as they are billed but only writes a `turns` row once
  an exchange has been persisted, so a session opened in another window sits
  at zero turns with real spend against it — and the zero-turn rule dropped
  it from every listing but `cs all`, while the landing strip above it had
  already counted it. Credits now count as activity in the listing the same
  way they already did in `db.stats`. Launches that were opened and closed
  without an exchange are still hidden: of 251 zero-turn sessions in a
  1,465-session store, exactly one had credits — the live one.
- **The theme you pick stays picked.** It lived only in the running process,
  so applying a palette and quitting put you back on the default landing
  screen with the gallery to visit again. What you apply is now written to
  `~/.config/cs/settings.json` and is what the next run starts in.
  `CS_THEME=<name>` still overrides it for the run it is set on,
  `CS_CONFIG_HOME`/`XDG_CONFIG_HOME` moves the file, and a theme that could
  not be written says `not saved` on the status line rather than being
  quietly forgotten. Nothing is ever written inside `COPILOT_HOME`, which
  `cs` continues to open read-only.
- The home screen now offers a mouse-and-keyboard live-preview Theme picker
  with 20 curated palettes (`t` is the shortcut; `CS_THEME` selects one at
  launch), and rebuilds the complete landing page — including total AI credits
  and a visible update time — from its read-only SQLite store on a timer.
- The live-refresh interval is 60 seconds, and every line that quotes it
  reads it from one constant rather than spelling it out.
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

- **`#N` shortcuts leave Copilot's home alone.** The last-listing map that
  powers `cs show #3` moved from `$COPILOT_HOME/.cs-last-index` to
  `~/.config/cs/.cs-last-index`, beside the remembered theme. An existing
  legacy file is still read when the new one is absent, so shortcuts keep
  working until the next listing rewrites the map; cs never deletes the old
  file. Docs that said there was "no write path" now say the accurate thing:
  the store is opened `mode=ro`, and cs may write its own config-home
  sidecars (theme settings and this index). `$COPILOT_HOME/.cs-ignore`
  remains user-authored and read-only to cs.
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

- Hover and wheel input on native-SGR terminals with legacy ncurses now use
  the existing SGR parser through an xterm-compatible terminal entry. Native
  keyboard handling is preserved, and the original `TERM` is restored outside
  each full-screen view.
- Returning home after its refresh deadline updates the data immediately,
  rather than starting another 30-second wait. Refresh also re-arms the
  correct idle or animation timeout, and normal query time no longer extends
  each 30-second interval.
- The home loop now sets its timeout immediately before every input read,
  with idle waits capped at one second. Mouse or modal input-mode changes can
  no longer leave the next refresh waiting for a keypress.
- Home credits show two decimal places and take priority in narrow windows.
  Rounded `k` totals previously hid changes smaller than 100 AIU, making
  successful live refreshes appear not to update.
- Theme backgrounds now extend to the banner and report text. Selection,
  status and credit colours meet 4.5:1 text contrast across all 20 palettes;
  narrow theme galleries retain the Enter and Esc hints.
- Standing in your home directory no longer renames your own kit.
  `.copilot/skills` is a *project* pattern, and in `$HOME` it **is** the
  personal directory, so the inventory reported all forty-nine personal skills — and
  all twenty-five agent profiles — as shipped by "this repo".
- `cs context` and `cs skills` count the same skills again. They had drifted
  by one: a recursive rule belonging to one root was being applied to
  another, which found a stray page inside a documented skill and filed it as
  a skill named `skill`.
- A directory `cs` cannot read no longer takes the whole view down with it.
  `Path.is_dir()` raises on one it cannot stat rather than returning False,
  and these walks no longer stay inside two known roots: naming the checkout
  a skill came from opens every directory the store has seen a session in,
  which here is two hundred and forty-one of them.
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

[Unreleased]: https://github.com/smaharajan/copilot-sessions/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/smaharajan/copilot-sessions/releases/tag/v1.1.0
[1.0.0]: https://github.com/smaharajan/copilot-sessions/releases/tag/v1.0.0
