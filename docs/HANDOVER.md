# Handover

Written to be picked up on a different machine. Setup lives in
[CONTRIBUTING.md](../CONTRIBUTING.md) and design lives in
[ARCHITECTURE.md](../ARCHITECTURE.md); this file is only the things you would
otherwise have to rediscover — what changed, what was decided and why, and
what is deliberately left undone.

**State at handover:** `main`, working tree clean, pushed, CI green. 478 tests,
all passing. Python 3.10+, zero runtime dependencies. **Public, released as
v1.0.0, and installable with `brew install smaharajan/tap/copilot-sessions`.**

No SHA is quoted here on purpose: a file cannot name the commit that contains
it, and the previous handover spent its whole life one commit out of date.
`git log -1` is never wrong.

```bash
git clone https://github.com/smaharajan/copilot-sessions.git
cd copilot-sessions
python3 -m unittest discover -s tests -q     # expect: Ran 478 tests ... OK
pipx install 'ruff==0.15.22' && ruff check cs tests   # expect: All checks passed!
```

---

## 1 · Pick up here

Everything asked for is shipped and **the repository is public**: released as
`v1.0.0`, served by a public Homebrew tap, CI green. Nothing is half-finished:
no branch to resume, no failing test, no `TODO` left in the code.

There is no queued feature. Multi-device was the one outstanding body of work
and it has since been **withdrawn** — see
[§5](#5--the-work-that-was-withdrawn) for what was learned before it was, which
is worth keeping even though the code was never written.

**Releasing is one action: publish a GitHub Release.** The tap follows
`releases/latest` and rewrites its own formula, so a tag without a Release
moves nothing and brew users stay where they are. See
[§1a](#1a--why-there-is-only-one-commit).

Open, none of it code:

- **Two account settings only the owner can make**, and they are the safety
  net that would have caught the leak in [§1a](#1a--why-there-is-only-one-commit)
  before it was published rather than after. Set the account email to private,
  and tick *Block command line pushes that expose my email* — that switch
  rejects the push instead of discovering it later. Separately, the signing
  key's UID still resolves to an employer address on a keyserver; nothing is
  embedded in any git object, but a personal key would close it.
- **Beads no longer syncs to this remote.** Its `refs/dolt/data` chunks
  carried an employer address in raw bytes, so the refs were deleted rather
  than published — see [§1a](#1a--why-there-is-only-one-commit) before turning
  sync back on.
- **"Agents" now means two things.** The Reference row and `cs profiles` mean
  *agents you have defined on disk*; the Measure row and `cs agents` mean
  *sub-agents that ran*. Renaming the second one was offered and not answered.
- **`cs help` is still the one plain view.** It is a single 125-line f-string
  in `cmd_help` styled with `BOLD` and `DIM`, hard-wrapped at 78 columns and
  not routed through the pager, so it matches nothing else. A redesign was
  asked for and never started. Approach: `ui.rule`/`heading`/`field`/
  `menu_icon`, width-aware, paged — preserving every line currently in it.
- **The Improve group and Working days are commented out** of the menu while
  `cs coach`, `cs rhythm`, `cs context` and `cs timeline` all still run. The
  README marks them *Unlisted*, which is accurate. Decide whether they come
  back or the commented rows eventually go.
- **"Agents" now means two things.** The Reference row and `cs profiles` mean
  *agents you have defined on disk*; the Measure row and `cs agents` mean
  *sub-agents that ran*. Renaming the second one was offered and not answered.
- **`cs help` is still the one plain view.** It is a single 125-line f-string
  in `cmd_help` styled with `BOLD` and `DIM`, hard-wrapped at 78 columns and
  not routed through the pager, so it matches nothing else. A redesign was
  asked for and never started. Approach: `ui.rule`/`heading`/`field`/
  `menu_icon`, width-aware, paged — preserving every line currently in it.
- **The Improve group and Working days are commented out** of the menu while
  `cs coach`, `cs rhythm`, `cs context` and `cs timeline` all still run. The
  README marks them *Unlisted*, which is accurate. Decide whether they come
  back or the commented rows eventually go.

---

## 1a · Why there is only one commit

**The public history starts at a single "Initial commit."** Everything before
it — 114 commits from first prototype to first release — was collapsed
deliberately, and the repository was deleted and recreated on GitHub to drop
the pull-request refs that survive a branch. If you have a clone from before
that, it is worthless: delete it and clone again.

The full pre-collapse history is **not lost**. A mirror, a verified bundle,
the working-tree tarball and the repository's settings as JSON sit outside the
repository with their own `RESTORE.md`. Anything below that reads like it is
describing a commit you cannot find is describing one of those 114; the
decisions are kept here precisely because the commits are not.

The collapse was the last step of a cleanup, and the cleanup is the part worth
reading. Four things were found, each of which would have published an
employer address into a public repository:

1. **`filter-repo --mailmap` fixes the author, and the config fixes the
   committer.** The rewrite left `committer` pointing at whatever
   `git config user.name`/`user.email` said, so both had to be pinned
   repo-locally and the whole history replayed a second time. Check both
   fields, never just `%ae`. The pin is repo-local on purpose: the global
   config still carries the work identity, and other repositories still want
   it.
2. **A rewrite strips every signature.** All 111 commits were re-signed with
   `git rebase --root --exec 'git commit --amend --no-edit -S -q'`. Move the
   untracked local tooling (`.beads/`, `.agents/`, `.claude/`, `.codex/`,
   `CLAUDE.md`) aside first, or the replay of the commit that once added those
   paths aborts on "untracked working tree files would be overwritten".
3. **`refs/pull/*` outlives the branch.** Two closed dependabot PRs still held
   pre-rewrite commits, GitHub keeps those refs permanently, and a PR cannot be
   deleted through the API. Recreating the repository is the only way to drop
   them, and it is the reason the repository was recreated at all.
4. **Grep is not a clearance.** `.beads/issues.jsonl` was tracked for thirteen
   commits and every copy carried `"owner": "<employer address>"`; the path was
   purged with `--invert-paths`, which took five beads-only commits with it and
   no code. Worse, the beads `refs/dolt/data` chunk files hold the same address
   in **raw bytes that `git grep -a` does not report** — a byte-level scan of
   `cat-file --batch-all-objects` found it. Those refs were deleted from the
   remote rather than published.

The clearance that was actually accepted is that byte scan over every object
the remote holds — loose and packed, all refs — returning zero for the
employer domain, the real name and the local username. Collapsing to one
commit makes findings 1 and 2 moot for the past, not for the future: the sole
commit is signed and authored
`sommaharajan <28564186+ssmule@users.noreply.github.com>`, and the identity is
pinned repo-locally so the next commit matches it.

The account has since been renamed (`ssmule` → `smaharajan`), so
the repo-local pin is now `28564186+smaharajan@users.noreply.github.com`.
Existing commits keep the old address and stay attributed regardless: GitHub
resolves a noreply address on the `28564186+` account id, not on the username
after it. The ID-less form (`ssmule@users.noreply.github.com`) is the one that
silently loses attribution on a rename — never use it. Run the scan again
before going public:

```bash
git clone --mirror <remote> /tmp/check.git
git --git-dir=/tmp/check.git cat-file --batch-all-objects --batch \
  | grep -aic '<pattern>'      # expect 0
```

**Before turning beads sync back on**, scrub the `owner` field in the local
beads database. Its issue records still carry the old address, and the next
`bd` push would put it straight back on the remote.

**It happened a second time, and the reason is worth more than the fix.** Days
after that clearance, three commits reached the two public repositories
authored with the employer address. The repo-local pin was still correct and
still working; it simply was not consulted. Two routes went around it:

- **A fresh clone inherits the global identity.** The pin lives in
  `.git/config` of one directory. Anything that clones the repository
  elsewhere — a tool, an agent, a second checkout — starts with the global
  config, which still carried the work identity. The pin protects one
  directory, not an account. The fix was to invert the default: global is now
  the personal identity, and the work identity is pinned locally across the
  work repositories instead. A leak into a work repo is harmless; the reverse
  is not.
- **A squash-merge is authored by GitHub, from your profile.** Merging a pull
  request through the web UI ignores git config entirely and uses the account's
  public profile name and email. No local setting can prevent that. Set the
  email to private on the account, and tick *Block command line pushes that
  expose my email* — that switch is the only one of these controls that fails
  the push rather than discovering it afterwards.

Recreating a repository is cheap; the debris around it is not. Budget for all
of it, because each piece fails silently:

- **Every checksum downstream is now wrong.** A rewrite changes every tree, so
  the release tarball changes, so the Homebrew formula's `sha256` describes a
  file that no longer exists and *every install fails*. Recompute it from the
  new tag and push the formula before anything else.
- **A recreated repository has never run CI.** Restore a ruleset that requires
  status checks and the first pull request waits forever for a check that has
  no history. Land one commit on `main` first and watch it go green.
- **Push protection blocks the whole push, not the offending commit**, so a
  fix-forward commit is useless — history has to be rewritten again. It also
  reports one secret per attempt, so expect several rounds. The redaction test
  fixtures are fake credentials by design; splitting each literal just after
  its prefix (`"sk_live_" "51H8…"`) defeats the scanner and, because adjacent
  string literals concatenate, changes nothing about the test.
- **Existing clones keep the orphaned history.** Point them at the new remote
  and hard-reset, or the next push restores exactly what was deleted.


**The Homebrew tap had the same problem, and was rebuilt the same way.**
`smaharajan/homebrew-tap` (public) serves `brew install smaharajan/tap/copilot-sessions`.
Its old history tracked a beads database carrying the work address in twelve
objects, so it was backed up, rebuilt as a single signed commit and the
repository deleted and recreated — a force-push alone would not have done it,
because an object stays fetchable at its old SHA until the repository itself is
gone. Two consequences worth knowing:

- **The formula is not maintained by hand.** `.github/workflows/bump.yml` in the
  tap checks daily for a newer release, rewrites the `url` and `sha256`, and
  commits. It computes the checksum from the tarball it has just downloaded, so
  it cannot publish a formula whose checksum disagrees with its url. Publishing
  a GitHub Release is therefore the whole release process; if you tag without
  releasing, `releases/latest` does not move and the formula stays put.
- **Formulae for private repositories were dropped**, because a public tap that
  serves one hands every stranger a 404. The old tap is recoverable from the
  backup bundle under `~/Work/backups/` if one is ever needed again.
- **Never `git push origin main` from the Homebrew tap directory.** Homebrew
  keeps its tap clones on a detached HEAD, and the local `main` ref there can be
  stale — pushing it sends an old commit, not your work, and the rejection reads
  as "behind its remote counterpart" even though your commit is cleanly ahead.
  Either push `HEAD:main` or, better, work in an ordinary clone.
- **Homebrew 6 will not install from an untrusted third-party tap.**
  `brew tap smaharajan/tap && brew install copilot-sessions` is refused outright;
  the fully-qualified `brew install smaharajan/tap/copilot-sessions` works cold,
  because naming the tap is itself the trust signal. That is why the READMEs
  lead with the long form. The escape hatch is `brew trust smaharajan/tap`. Test
  install instructions from a genuinely untapped state or this stays invisible:
  once a machine has installed the formula the two-step appears to work.
- **A rewrite of this repository breaks the formula.** Every tree changes, so
  the release tarball changes, so the pinned `sha256` describes a file that no
  longer exists and *every install fails* with a checksum mismatch. The bump
  workflow only reacts to a new release, not to a rewritten tag, so this one is
  fixed by hand:
  ```bash
  curl -sL https://github.com/smaharajan/copilot-sessions/archive/refs/tags/vX.Y.Z.tar.gz | shasum -a 256
  ```
- **A noisy install is usually the machine, not the formula.** Two warnings look
  like defects in `cs` and are neither. Homebrew 6 lists *every* untrusted tap on
  the machine on *any* install, so unrelated taps appear in the middle of yours —
  `brew trust` the ones in use, untap the ones with nothing installed. And a
  developer symlink on `PATH` (`~/.local/bin/cs` → the checkout) shadows the
  Homebrew build, which Homebrew reports at the end of every install. Run
  `python3 -m cs` from the checkout instead of symlinking, and the two cannot
  diverge silently.
- **`caveats` is the only post-install surface a formula controls.** Homebrew
  owns its own output: a formula cannot animate an install or pace it, and
  padding a four-second install with delay only makes it feel slower. Spend the
  block on the first commands to type instead.

## 2 · What shipped, and the decision behind each

None of this is in the log any more — that is the point of keeping it here.
The table is in the order the work happened: the first entries took the app to
its current shape, the rest prepared it to be published and then went back
over what publishing exposed — the docs, the landing menu and the deck. A
later pass added the Instructions view and the screenshot pipeline; see
[§2a](#2a--the-instructions-view-and-the-screenshots).

| What changed | The decision worth keeping |
|--------------|----------------------------|
| Improve group (Practice / Rhythm / Context) taken off the landing menu and out of `cs help` | **Commented, not deleted.** All three still run when typed and every test of them still runs. A working view is a bad thing to make hard to get back. |
| `--why` — reports print findings, hold back lessons | A paragraph explaining what a p95 is belongs on run 1, not run 50. `cs efficiency` went 41 → 30 lines. |
| Landing menu cascades in on the wordmark's clock | The banner already animated; the menu under it appeared complete on frame 1, so the screen animated its decoration and not its content. |
| Every menu icon replaced with one terminals can actually draw | See [§3](#3--the-icon-rule-read-this-before-adding-a-menu-row) — this is the one that bit hardest. |
| Deck's fourth card points at Reference, not the hidden Improve | The deck was contradicting both the product and its own terminal mock. |
| CI made green after ten red builds; four unreferenced functions deleted | The lint step ran before the test step, so a lint error **skipped** the tests — ten commits shipped with no test signal at all. Test now runs `if: always()`. |
| Deck slide 1 holds the question before offering answers | Verified in a headless browser rather than by reading the diff, which caught a reversed reel and an invisible caret. See [§3](#3--the-icon-rule-read-this-before-adding-a-menu-row). |
| Beads untracked; Code of Conduct, issue forms, PR template and changelog added | The repo told agents to use `bd` for all tracking while `bd`'s database was gitignored — every clone got a tracker with nothing behind it. Public contract is now GitHub Issues. |
| Repository made safe to publish and true about itself; `HANDOVER.md` moved to `docs/` | The docs described a tool that had moved on. A README that overstates is worse than one that is terse. |
| Test suite split by subject into ten files | One 6,000-line `test_cli.py` was unreadable and unnavigable. **The harnesses moved to `tests/support.py`** — see [§4](#4--traps-this-codebase-has-set-for-you). |
| Deck slide 1: poster drawn as SVG, local poster override, caption placement, type scale | The poster is **drawn, not borrowed** — a committed binary would have been the only asset in the repo nobody could diff or regenerate. |
| Repo column shows a dimmed `·` when a session has no project tag | A blank cell next to two columns that write `·` for nothing reads as a **failure to draw**, not as an empty value. The fallback belongs at the column, not in `_project_tag` — see below. |
| Listing counts match what is listed; the agent walks the rule while idle; per-assistant config ignored | A count computed by a different rule than the list under it is a bug that looks like a rounding error. |
| Days sorted before they are cut; a split mouse report no longer types itself into the filter | A terminal can deliver one SGR mouse report across two reads. The parser now holds a partial rather than treating its tail as typing. |
| README cut 1,453 → ~310 lines, manual moved to `docs/GUIDE.md`, then rewritten to lead with the app | The first cut still opened on install and a 28-row command table, which says what `cs` accepts and never what it **is**. It opens on the home screen now. |
| Skills chart says `· none` where it used to say nothing, and budgets the column it prints | Two bugs, one row — see [§4](#4--traps-this-codebase-has-set-for-you). Also renamed "Agent profiles" → "Agents" on the menu, in help and in the export. |
| Deck slide 1: caption above the artwork and teasing rather than naming the film | The caption sat under the poster and named it, so the artwork had nothing left to reveal. Caption and poster are two beats now. |
| Deck slide 2's terminal demo repainted, and given `cs resume`, `cs agents` and `cs skills` | Every screen was painted with `c1`–`c7`, which **no CSS has defined** since the palette was renamed — six of seven views rendered flat grey. |
| Working days commented off the landing menu | Third counting view in Measure: Stats already had the totals and AI spend already had the per-day bars. Commented, not deleted; `cs timeline` still runs. |

## 2a · The Instructions view, and the screenshots

`cs instructions` (`cmd_instructions` / `_render_instructions` in `cli.py`,
backed by `context.instruction_paths()`) reads the instruction files on disk
in both scopes and reports how much of each one Copilot actually gets.

**It deliberately breaks the house pattern.** Every other inventory —
`cs skills`, `cs profiles`, `cs hooks`, `cs mcp` — reports *installed versus
used*. This one cannot: nothing references an instruction file, because it is
loaded before the first prompt. Every session got all of it that fit. So the
only reading worth printing is how much fit, which is why the view names two
faults and no others: over the 4,000-character truncation limit, and long
without headings. Do not add a "referenced" column to it later; there is no
evidence that could fill one.

The row was put **after Agents**, not beside Skills alphabetically, because
`_HOME_GROUP_STARTS` anchors the Reference heading on the row labelled
`Skills`. Inserting above it moves the heading onto the new row and the
startup assertion does not catch it, because the anchor still exists.

**The screenshots are generated, not taken.** `docs/img/make_screens.py` seeds
a synthetic store with `tests/support._build_store` — the same builder the
tests use, so the demo store cannot drift from the real schema — runs the real
commands under a pty, and converts the ANSI to SVG. Four traps are already
paid for and will come back if the script is rewritten:

- **Colour needs a pty.** `ui._COLOR` is `sys.stdout.isatty()`; a pipe
  produces a grey screenshot that looks like the app has no colour at all.
- **The escape-stripping regex must not eat SGR.** `\033\[[0-9;?]*[A-Za-z]`
  matches colour too, because `m` is a letter. The class is `[A-Za-ln-z]`.
- **Every text run carries `textLength`.** Without it a browser's monospace
  metrics walk a 90-column table out of true by the right-hand edge. Widths
  come from `ui.cells`, so emoji count as two.
- **The demo store lives at a hard-coded `/tmp/cs-demo`**, not
  `tempfile.gettempdir()`, which on macOS is a per-boot `/var/folders/…` path
  that would rewrite every committed SVG on every run.

Check a regenerated SVG in a real browser, not by reading the diff — the same
rule as the deck.

### Why the repo dot is not in `_project_tag`

`_project_tag(repo, cwd)` has two callers that want opposite things from an
empty tag. The printed listing appends it as a `#name` suffix and uses
emptiness as the signal to **omit it**; the TUI draws it as a fixed column
that must never be blank. Putting the `·` inside the shared function would
print a meaningless `#·` on every repo-less row of the printed listing. The
fallback therefore lives at the TUI cell.

Sorting is unaffected: the Repo column sorts on the stored `repository`
value, not on the rendered tag. About one session in five has no tag, which
is by design — `_GENERIC_DIRS` suppresses leaf directories like `projects` or
a home directory that would name nothing useful.

### `--why` in one paragraph

Every report used to carry teaching prose under each section, on every run.
It is now split by **what a sentence is about**: a line about the numbers on
screen is a *finding* (`_note`, always prints); a line about how the view
works is a *lesson* (`_why`, waits for the flag). `--why` is stripped globally
in `_dispatch` rather than parsed per command, because it changes how every
report prints and what none of them computes.

Two non-obvious properties, both covered by tests:

- **It never changes a number, a filter or an order.** The two forms of a
  report are the same report — otherwise the flag becomes a second code path.
- **The footer hint tracks what was actually withheld** (`_WHY_WITHHELD`), so
  it can never advertise an explanation a given run had none of. This matters
  on empty reports: `cs hooks` with no hooks configured teaches *inline* and
  says nothing about `--why`, because a screen with nothing on it is the one
  moment the explanation **is** the report.

`CS_WHY=1` turns lessons on permanently.

---

## 3 · The icon rule (read this before adding a menu row)

The Hooks row used **U+1FA9D**, added to Unicode in **2020**. Any terminal
whose emoji font predates it drew a blank, so that row read as the one option
on the menu that forgot to bring an icon. A missing glyph looks like a bug in
the tool rather than a gap in a font.

**Rule:** menu icons come from the Unicode 6.0 emoji set — the one every emoji
font has shipped since 2010. The single exception is 🤖 (Unicode 8.0), which
is already the Copilot mark in transcripts; one glyph for the agent in both
places beats the five-year gap.

Icons live in **`cs/ui.py` → `_MENU_GLYPHS`**, not beside the menu rows, and
are read through `ui.menu_icon(name)`. That table also carries the plain-ASCII
marker for each row. Three things follow:

- `MenuIconTest.TOO_NEW` **fails the suite** on a post-Unicode-9 codepoint, so
  the next person to add a row cannot repeat this by accident.
- The table includes the rows that are commented off the menu, so restoring
  one cannot walk the bug back in with it.
- `menu_icon()` **raises** on an unknown name rather than returning `""` —
  a blank icon is the exact bug it exists to prevent.

Two icons (`⚡`, `❓`) come from a symbol block a terminal may draw from a
monochrome *text* font, which renders them thin and a column narrower than
their neighbours. Both carry `U+FE0F` to request the emoji form.

### Two bugs that fell out of fixing that

- **`CS_GLYPHS=ascii` was a lie on the landing screen.** The setting you reach
  for *precisely because* your terminal cannot draw emoji was honoured by the
  two transcript speaker marks and ignored by all 17 menu icons. It now covers
  both.
- **`ui.cells()` charged a column to zero-width characters.** Variation
  selectors, joiners and combining marks (`Mn`/`Me`/`Cf`) were counted as one
  cell each, so text measured **wider than it draws** — the direction that
  truncates lines that would have fit and drops columns that had room. `café`
  spelled with a combining accent measured 5. Fixed in `cs/ui.py → cells()`;
  it underpins every table in the app, so treat changes to it carefully.

---

## 4 · Traps this codebase has set for you

Discovered the hard way. None are documented anywhere else.

**Which sessions a listing shows is decided in two places, not one.**
`_visible()` is the choke point, and it applies two different rules:
`_is_hidden()` drops **every zero-turn session** from the default views, while
`_never_used()` drops a session from `cs all` as well — but only when it has
no turns, *no credits and no name*. So a zero-turn session that was given a
name deliberately survives `cs all`; that is the documented behaviour and
`tests/support.py` ships a fixture named "Empty session" to hold it.

Before changing either rule, check the other. A change making `_never_used()`
treat the CLI's own "Empty Session" placeholder as no-name looks obviously
right, and breaks three tests: it is a magic string, and the rows it targets
are already hidden everywhere except the one view whose entire purpose is to
show everything.

**The test harnesses live in `tests/support.py`.** The suite was one file
before it was split by subject; `tests/test_cli.py` no longer exists. `Screen`,
`StoreTest`
and `_Tty` are imported as `from support import …`, which is why the suite
must be run as `python -m unittest discover -s tests` — running a module
directly (`python -m unittest tests.test_core`) fails on `No module named
'support'` and reports "Ran 1 test", which looks like a collection bug and is
not. Stale `test_cli` `.pyc` files may linger in `tests/__pycache__/` and
pollute a `grep -rn`.

**Menu groups are label-anchored, and hiding a group is two edits.**
`_HOME_GROUP_STARTS` maps a *row label* to a heading. It replaced a dict keyed
by list position, which silently misfiled every row after any row you added.
`_home_groups()` **raises `KeyError`** if an anchor names a row that is not on
the menu — that guard is what makes "comment it out for now" safe rather than
a slow leak. To hide a group you must comment out both the rows in
`_home_items()` **and** its anchor; do one without the other and `cs` refuses
to start.

**`_home_groups()` cannot be evaluated at import time.** `_home_items()` calls
`_window_label()`, which is defined later in the module. Hence the lazily
populated module-level `_HOME_GROUPS` cache.

**Test fixture timestamps are not dates.** `_build_store` stamps turns with
`'x'`, `'y'`, `'z'`, `'t'`. Any test touching time behaviour must call
`self._stamp()`. Related live bug, already fixed: `db.timeline` grouped on
`substr(stamp, 1, 10)`, so a fixture value of `'x'` became a day named `x` —
and `"x"` string-compares *greater than any real date*, so junk landed inside
every window. It groups on `date(stamp)` now.

**The curses harness records frames as `{(row, col): text}`,** not lines.
Count menu rows with `len({row for (row, col) in frame if col == 6})`.
`Screen(keys)` takes keys positionally; subclass and override `getmaxyx()` to
change the window size.

**The landing menu is type-to-filter.** In a test, `ord("j")` opens the filter
box and types `j` — it does not move the cursor. Use `curses.KEY_DOWN`. And
`q` only quits when the query is empty.

**`db.timeline` runs three queries and stitches by day** rather than joining.
A sessions→turns→usage join fans out and inflates each day by a different
factor. A day earns a row if anything happened on it.

**Reports are `_capture()`d and replayed inside curses.** Do not animate
anything inside a report renderer; animation belongs in the TUI layer only.
Anything you do add must degrade on `TERM=dumb` and on non-TTY output.

**A chart's budget must include everything printed after the bar.**
`_chart_spans(inner, fixed, …)` only reserves the columns named in `fixed`.
The skills view appended a `· N ran` note *after* `ui.bar(…)` without counting
it, so rows ran off any window under about 80 columns — 50 cells written into
a 46-cell terminal. Pass `ui.bar(…, pad=True)` so the bar returns exactly
`width` cells, and add the note's own width to `fixed`. Sweep
`COLUMNS=46/72/96` on anything that draws a bar.

**An absent skill marker does not mean the skill never ran.** Sessions carry a
`<skill-context>` marker only from **2026-05-30**, while the store starts
**2026-03-09**. `reference_counts` and `skills_invoked_by_session` are both
store-wide, so a blank really is a zero — but a zero *invocation* count over a
window the store cannot see is not the same claim as "never ran". That is why
the chart says `· none` and never `· 0 ran`: writing the number would assert
something the data does not support. Roughly thirteen invoked names have no
file on disk at all (renamed, deleted, or plugin-provided), which is expected.

**The deck carries its own dead code.** Slide 2's terminal screens were
written with colour classes `c1`–`c7`; the CSS palette was later renamed to
two-letter classes (`ac`, `mu`, `cr`, `go`, …) and the markup was never
updated, so six of seven views rendered in flat grey for months and read as a
dull animation rather than as a bug. **Grep the deck for classes that no rule
defines** after touching its palette. QA it headless by copying the file and
injecting `<style>.slide .rv{opacity:1!important;transform:none!important}
</style>` before `</head>` — `.rv` only changes opacity and transform, so
layout is measurable at step 0.

**`gh run list` can 404 while CI is perfectly fine.** During a GitHub
incident the aggregate Actions endpoint returns 404 even with `workflow`
scope. `gh api repos/<owner>/<repo>/actions/workflows/<id>/runs` keeps
working, and is the fallback worth reaching for before assuming a broken
token. Relatedly, `cs resume` ends in `os.execv`, which inherits the
environment unchanged — it cannot be the cause of an auth failure, so an
auth error there is always upstream.

**Three tests are structural, and they are the ones that keep the rest
honest.** Each was added because a whole body of code had never been executed
by any test, which is the state in which code stops working silently:

- **`tests/test_surface.py` reads the command list out of `cli._dispatch`
  with `ast`** and runs every command — against the fixture store, against an
  emptied one, and against a store carrying only `sessions` and `turns`. That
  last one is the point: the schema is Copilot's, and a release that has not
  shipped `assistant_usage_events` is the likeliest way this breaks on
  somebody else's machine. The same file hashes the store before and after
  every command, which is how the README's read-only guarantee is now a test
  rather than a sentence. A new command needing an argument goes in that
  file's `ARGUMENTS` table or the smoke test runs it bare.
- **`tests/test_practice_rules.py` fails until a new `coach` rule has a
  fixture** that fires it and one that does not. Seventeen of the twenty-two
  rules had never run. A rule that goes quiet does not look broken — the
  report just gets shorter, which reads as good news.
- **The credential corpus in `test_governance.py` must name every rule in
  `redact._RULES`.** Nine had no sample. Same failure mode, worse
  consequence.

**`cs show` no longer ends in a turn index, and that changes a documented
rule.** `_render_transcript` carries a comment saying `read` prints no
contents list *because* `show` has the index. That reasoning outlived the
index: the block was a third rendering of one list — the page prints the first
and last request above it, `--asks` prints every one numbered, and `read` is
the conversation. What it uniquely carried, the `cs read <id> --turn N`
command, moved to the `--asks` block, which is the only numbered list left.
`db.session_turns` went with it (nothing else called it) and
`db.session_turn_count` replaced it: the page was pulling 2,000 characters of
every prompt in the session so it could take `len()` of the result, and
`--short` paid for that too. The count is deliberately *not* filtered — a turn
with no prompt is still a turn, and the header says turns while `--asks` says
requests.

**The transcript is set with a rail, not with rules.** `_render_transcript`
drew three full-width rules per turn — its own and one per speaker — plus a
lone grey line underneath carrying the size. All the same weight, so nothing
ranked. It is now one rule (`ui.rule(..., note=…)` puts the time and size on
its right-hand end) and `ui.spine()` runs a coloured rail down each speaker's
block. Two things follow that are not obvious:

- **A heading inside a reply cannot use the accent *bar*.** The bar wants the
  two columns to the left of the text and the rail is already there, so
  headings are set in the accent colour instead. Bold alone does not work: a
  reply is already full of `**bold**`.
- **`spine()` strips the indent `markdown` applied rather than adding to the
  margin**, so a transcript and a report still line up when read one after
  the other. Hand it lines with a different indent and it says so.

**Tooling:** tests are **unittest, not pytest**. **Run `ruff` before every
commit** — `pipx install 'ruff==0.15.22' && ruff check cs tests`, the exact
version CI pins. This used to say ruff was CI's problem, and the result was
ten consecutive red builds over a handful of import-order nits; because the
lint step runs first, the test step was skipped every time and the suite's
real state was invisible. BSD userland — no `grep -P`, no `cat -A`; use
`awk '{printf "%3d|%s\n", length($0), $0}'` to check line widths. Preview a
report with `CS_PAGER=cat COLUMNS=92 python3 -m cs <cmd>`, and sweep
`COLUMNS=46/72/96` before calling a table done.

**House style:** lines ≤ ~88 columns; comments explain *why*, never *what*;
every behavioural change gets a test whose name is a sentence.

---

## 5 · The work that was withdrawn

Multi-device. Designed, researched, then **dropped at the user's request**
before any code was written. Recorded here because the research is the
expensive part and re-running it would waste a day.

**The problem it addressed.** `cs` reads exactly one store:
`$COPILOT_HOME/session-store.db`, on the machine it runs on. Across three
machines you get three partial views and no consolidated answer to "what did I
spend this month".

**Why it is not simply a sync feature.** There is no official remote source to
sync from. The cloud store holds only Coding Agent and Code Review runs — no
CLI terminal sessions — `copilot` has no session-listing subcommand, and the
remote export path is write-only. Any consolidation therefore has to merge
local store *files*, which makes it a deployment problem as much as a code one.

**What the store already gives you.** `sessions.host_type` distinguishes
cloud-run work from local, so the split between "already visible anywhere" and
"local-only" is readable without new schema.

If it is ever revived, the mechanism was prototyped and does work:

| Piece | Shape |
|-------|-------|
| **`multi-store-core`** | `db.connect()` does `ATTACH DATABASE 'file:…?mode=ro'` then `CREATE TEMP VIEW sessions AS SELECT … FROM main.sessions UNION ALL …`. SQLite resolves temp before main, so the view **shadows** the table and every existing query spans devices **unrewritten**. The view body must qualify `main.sessions` or it recurses. |
| **`device-config`** | A registry mapping device label → store path, plus `cs devices` to list, add, remove and show freshness. A registry, not a transport. |
| **`origin-awareness`** | Device and cloud-sync state in listings and headers. **`cs resume` must refuse another device's session** — its `cwd` does not exist here, and that is the sharp edge. |

Two traps found while prototyping, both of which cost time: `db._has_table`
filters `type='table'` and returns False for a shadowing view; and `_has_fts()`
must return False whenever devices are attached, because FTS5 virtual tables
cannot be unioned through a view — search has to fall back to LIKE.

**Constraints that still hold.** `db.connect()` opens the store **read-only**
(`mode=ro`) and `cs` must never write to Copilot's own data — that is a stated
privacy guarantee in the README, not an implementation detail. Keep zero
runtime dependencies. And the user has explicitly rejected any web or HTML
surface for this tool more than once: *"I need only the terminal based
representation."* Consolidation would mean a merged terminal view, not a
dashboard.

---

## 6 · Where things are

| Path | What it holds |
|------|----------------|
| `cs/cli.py` | Every view, the curses TUI and dispatch. Grep `def _home_items` (menu rows; Improve commented out just below it), `_HOME_GROUP_STARTS`, `def _why`/`def _why_hint`, `def _dispatch` |
| `cs/ui.py` | All drawing primitives: `cells`, `sparkline`, `bar`, `meter`, `field`, `_MENU_GLYPHS`/`menu_icon`, `reveal_columns`/`reveal_rows` |
| `cs/db.py` | Every SQL query; `connect()` is read-only |
| `cs/practice.py` | Powers the three unlisted Improve views |
| `cs/export.py` | `--json` / `--csv` — the readings without the drawing |
| `tests/support.py` | The synthetic store and the fake curses screen; every test file imports it |
| `tests/test_*.py` | Split by subject — see the table in `CONTRIBUTING.md` |
| `tests/test_surface.py` | Every command run against three shapes of store, plus the read-only guarantee |
| `tests/test_practice_rules.py` | One fixture per `coach` rule, and the guard that a new rule needs one |
| `tests/test_transcript.py` | How a conversation is set on the page |
| `README.md` | The landing page: what `cs` is, the home screen, six capability sections, then install and the command table |
| `docs/GUIDE.md` | The manual the README used to be — every view, every key, what each report is for. "Every view earns its place" explains what is unlisted and why |
| `ARCHITECTURE.md` | Why the code is shaped the way it is |
| `CONTRIBUTING.md` | The contributor contract — module table, what needs a test |
| `AGENTS.md` | The agent brief: the traps that have actually cost time here |
| `.github/ISSUE_TEMPLATE/` | Bug and feature issue forms; blank issues are disabled |
| `docs/deck/index.html` | The pitch deck. Slide 1 is the problem, slide 2 the demo; both build on keypresses via `keyed()`. Each `pre.view` carries `data-row` naming the menu row it opened from |
| `docs/deck/poster.local.*` | **Gitignored, and must stay that way** — a third-party poster used locally in place of the drawn `poster.svg`. A blob pushed once stays fetchable at its old SHA |

Symbol names, not line numbers, on purpose: line numbers in this table were
wrong within three commits of being written, and a stale line number is
worse than none — it sends a reader confidently to the wrong function.
`grep -n` finds any of these in a second and is never out of date.

---

## 7 · Verify the handover on the new machine

```bash
python3 -m unittest discover -s tests -q          # Ran 478 tests ... OK
ruff check cs tests                               # All checks passed!
CS_PAGER=cat COLUMNS=92 python3 -m cs efficiency  # findings only, ends with --why hint
CS_PAGER=cat COLUMNS=92 python3 -m cs efficiency --why   # ~11 lines longer
CS_GLYPHS=ascii python3 -m cs                     # landing screen, no emoji anywhere
python3 -m cs                                     # menu cascades in once per run
CS_PAGER=cat COLUMNS=46 python3 -m cs skills      # load column stays inside the window
CS_PAGER=cat COLUMNS=92 python3 -m cs timeline    # off the menu, still runs when typed
```

If `cs` exits with *"no Copilot session store found"*, set `COPILOT_HOME` to
wherever Copilot writes on that machine. That is expected on a fresh box and
is not a regression.
