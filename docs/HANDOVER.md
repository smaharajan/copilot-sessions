# Handover

**Updated:** 2026-09-25
**Repository:** `smaharajan/copilot-sessions`
**Branch:** `feat/merge-cli-package-and-standup`
**Baseline:** `529c9da` (`main`)
**Current state:** the theme and refresh work below shipped in `8f4d154`.
Since then:

- `ac50173` deliberately changed the refresh interval to **60 seconds**. The
  maintainer confirmed 60 on 2026-09-25, so every "30 seconds" below is
  history, not the current contract.
- This branch splits `cs/cli.py` into the `cs/cli/` package. `cs.cli`
  re-exports every symbol, so `import cs.cli as cli; cli._foo` and patches
  of `cs.cli.*` keep working.
- It adds pins, notes and tags, a daily AIU budget, and `cs standup`.
- It moves `.cs-last-index` out of `COPILOT_HOME` into the config directory.
  The legacy location is still read as a fallback.

Verified on this branch:

- `ruff` and all 612 tests pass.
- `pip install .` ships `cs.cli`.
- In a real terminal, two unattended refreshes landed 60 seconds apart after
  a mouse hover, with quarter-credit changes visible at 40 columns.

## Current task

Improve the `cs` landing-screen theme experience and make its live data refresh
observable and reliable.

The latest required interaction contract is exact:

1. **Theme is a row on the main menu.**
2. Double-clicking the Theme row opens the theme gallery.
3. The gallery offers 20 curated themes.
4. Hover and single-click only move the preview/highlight.
5. `Enter` or double-click applies the highlighted theme and immediately
   returns to the main page.
6. `Esc` returns without applying the previewed theme.
7. The complete home page refreshes from a new read-only SQLite connection
   every 30 seconds.
8. Total AI credits and a visible `updated HH:MM:SS` value must change without
   reopening the app.
9. Commit and push are approved following local verification.

The exact flow above was reproduced in a real tmux terminal using the source
checkout and a synthetic store. No real session data was modified.

## Implemented locally

### Theme system

`cs/ui.py` now defines 20 semantic 256-colour themes from one shared theme
specification rather than duplicating every curses role:

- Dark
- Light
- High Contrast
- Midnight
- Nord
- Dracula
- Solarized Dark
- Solarized Light
- Gruvbox
- Monokai
- Tokyo Night
- Catppuccin
- One Dark
- Material Ocean
- Ayu Dark
- Everforest
- Kanagawa
- Rosé Pine
- Synthwave
- Cyberpunk

The same selected palette feeds the curses UI, ANSI reports, charts and banner
ramp. `CS_THEME=<name>` still selects the startup theme.

### Theme gallery

`cs/cli.py::_theme_picker` provides:

- centered, width-aware layout;
- live preview while navigating;
- keyboard navigation with arrows, Home and End;
- mouse-motion hover;
- single-click preview;
- `Enter` or double-click apply;
- stable scrolling so the item under the pointer does not move between clicks;
- `Esc`/`q` back behavior.

Mouse support enables mode `1003` for menu motion and `1006` for extended
coordinates. Other views do not request motion. `_mouse_event` normalizes
motion to a `move` event. The main menu
also keeps a stable scroll offset, which fixes double-clicking a row after
hover moved the cursor.

### Thirty-second home refresh

`cs/cli.py::_home_snapshot` opens a fresh read-only database connection and
rebuilds:

- session count;
- turn count;
- total AI credits;
- repository count;
- skills and agents used;
- sub-agent runs;
- MCP count;
- 120-day activity series.

The deadline is checked before every complete home redraw, not only after an
idle timeout. Successful refreshes update the visible `updated HH:MM:SS`
status. Failed refreshes preserve the last good snapshot and show a retry
message.

An actual tmux integration check against a synthetic live SQLite store was
performed:

```text
before: 4.00 AIU · updated 19:33:03
after:  5.00 AIU · updated 19:33:33
```

The additional usage row was written externally while `cs` remained open.
The displayed value changed after 30 seconds without reopening the app.

### Follow-up fixes from terminal verification

The earlier idle-only check missed a mouse interaction failure: parsing an SGR
report reset curses input to blocking mode **after** the home loop armed its
timer. A hover followed by no keyboard input froze the credits and timestamp.
The home loop now re-arms the timeout **after** decoding mouse input, covering
hover, click and wheel reports.

Enabling motion globally also made hovering a session-list column header
toggle its sort direction. The listing now ignores motion reports; header
clicks retain their existing sorting behavior.

The home status now retains `updated HH:MM:SS` at 40 columns rather than
dropping the refresh time with the longer hints.

### Review after the follow-up report

The earlier terminal checks used `TERM=xterm-256color`. That missed a real
compatibility problem with `TERM=xterm-ghostty`: its terminfo declares native
SGR mouse input (`kmous=ESC[<`), which legacy ncurses consumes before the
application parser can see it. On the old mouse ABI, hover was dropped and
wheel-down was reported as motion.

`_curses_wrapper` now detects that capability/ABI combination once and uses
`xterm-256color` only inside each curses session. All three wrapper call sites
use it. The existing raw-SGR parser handles the mouse, curses still handles
keyboard keys, and `TERM` is restored even after an exception. This is not a
replacement installation or a change to shell configuration.

#### Standards

Fixed two rendering/timing findings: low contrast in essential theme roles,
and the refresh deadline leaving an obsolete short timeout armed. Selection,
status and credit text now meet 4.5:1 across the 20 palettes. Banner and report
colour pairs also use the selected background instead of terminal-default
backgrounds.

#### Spec

Fixed the native-SGR interaction failure, preserved the refresh deadline when
returning from another view, and kept Enter/Esc hints visible in narrow
galleries. Mouse motion is requested only by the menus, not by report readers
or session listings. Returning from the search prompt also re-arms the home
timer after the prompt's mouse parser has switched input back to blocking.

Review coverage: **9 total files, 9 reviewed, 0 skipped, 100%**. This includes
all five Python files identified by the review tool and the four Markdown
files that its extension filter excluded. Low-priority style changes and
unconfirmed findings were not applied.

### Refresh follow-up: realistic totals and repeated cycles

A read-only live-store comparison confirmed new usage was being recorded while
the displayed credit total stayed unchanged: `ui.fmt_aiu` rounds large totals
to 100-AIU steps (`k` with one decimal). The earlier 4-to-5-AIU fixture never
exercised that formatting path.

The home screen now shows credits to two decimal places, with thousands
separators, and puts them first so they survive at 40 columns. Other reports
retain their existing compact formatting.

The timer also advances its existing deadline rather than adding 30 seconds
after a query completes. A simulated 1.5-second query previously produced
polls at 30, 61.5 and 93 seconds; it now produces 30, 60 and 90. Overdue
re-entry or a query that overruns an interval skips missed polls rather than
running a catch-up loop.

Three consecutive real PTY cycles passed under `TERM=xterm-ghostty`, at 40
columns, using a synthetic million-credit balance and quarter-credit writes:

```text
initial: 1,000,000.00 AIU · updated 20:34:31
cycle 1: 1,000,000.25 AIU · updated 20:35:01 · interval 30s
cycle 2: 1,000,000.50 AIU · updated 20:35:31 · interval 30s
cycle 3: 1,000,000.75 AIU · updated 20:36:01 · interval 30s
```

The process remained open throughout. Each write was made externally to the
throwaway store; no real usage data was modified.

### Refresh follow-up: clock frozen until a keypress

The next report was specifically the home screen's update clock freezing,
not merely an unchanged credit total. Pressing Down immediately released the
overdue refresh. The running launcher was confirmed to use this checkout;
process samples showed it waiting in curses input.

The loop no longer relies on a timeout armed earlier by an animation, mouse
handler or modal return. It now re-arms immediately before **every** home
input read, with a maximum one-second idle wait. The database is still read
on its 30-second deadline, not every second. The scattered timeout changes
were removed; theme selection and palette definitions were left alone.

Validation went beyond tmux:

- A native Ghostty test deliberately reset curses input to blocking mode
  after every timed read. Refresh still completed at 30 and 60 seconds, with
  every home input wait bounded to at most 1,000 ms.
- Three unattended quarter-credit updates at a million-credit balance
  remained visible at 40 columns, exactly 30 seconds apart.
- A native live-store diagnostic recorded eight successful refreshes from
  21:16:14 through 21:19:44, each 30 seconds apart.
- User confirmation: Light applies on Enter, and the update clock advances
  without a keypress. The temporary diagnostic window was then closed.

Already-running processes retain the old Python code; restart them to load
this input-loop change. No real session data or terminal configuration was
modified.

## UI review performed

The real TUI was run at 140 columns by 40 rows against the synthetic test
store. The first review found the theme gallery pinned to the top with excessive
blank space; it was changed to a centered panel. A second review verified:

- all 20 themes are visible at standard terminal height;
- theme names and descriptions align;
- the selected row is visually distinct;
- applying Light leaves the chosen theme visible on the main Theme row;
- the main status shows the latest refresh time.

The picker also has an automated shape check at 40, 60, 80, 100 and 140
columns.

The final real-terminal check covered those same five widths, a scrolled
10-row gallery, and the exact hover/single-click/Enter/double-click/Esc flow.
All 20 themes were present. After the last input was a hover, external writes
to the synthetic store were reflected without any further input:

```text
before: 2 sessions · 2 turns · 4.00 AIU · 1 repos · updated 19:59:05
after:  3 sessions · 3 turns · 5.00 AIU · 2 repos · updated 19:59:35
```

The write occurred during the refresh interval and appeared 22.2 seconds
later. Temporary terminal sessions and fixture stores were cleaned up.

After the compatibility fix, the same checks passed with both
`TERM=xterm-ghostty` and `TERM=xterm-256color`. The native-SGR run also verified
wheel-up/down, arrow keys, and a second curses view followed by home:

```text
initial:  4.00 AIU · updated 20:22:49
refresh:  5.00 AIU · updated 20:23:19
return from Stats after another write and 31 seconds: 6.00 AIU immediately
```

These are real PTY/curses protocol checks using synthetic stores, not a visual
inspection of a user's existing terminal window. Restart any already-running
`cs` process to load the changed code. The final live-refresh run included
mouse input inside an empty search prompt, then returning home and waiting
without another keypress.

## Validation

Latest completed checks:

```bash
ruff check cs tests
python -m unittest discover -s tests
```

Result:

```text
All checks passed
Ran 550 tests ... OK
```

There are focused tests for:

- all 20 theme palettes and declared ANSI colours;
- Theme row wiring;
- keyboard selection;
- hover and single-click preview without application;
- double-click application;
- double-click opening the Theme row from the main menu;
- stable hover behavior in a short/scrolled picker;
- picker widths from 40 through 140 columns;
- SGR mouse-motion decoding;
- a real new usage event changing the next home AIU snapshot;
- a complete redraw after the 30-second deadline;
- retaining the last good snapshot after a refresh failure;
- keeping the home refresh timer armed after real SGR hover, click and wheel
  decoding;
- keeping the update timestamp visible at all five supported widths;
- preventing hover from sorting a session-list header;
- old-ABI native-SGR compatibility, repeated view entry and `TERM` restoration
  after success or failure;
- requesting motion only for menus;
- preserving the refresh deadline across views and re-arming idle input;
- applying theme backgrounds to banner/report pairs and checking actual
  contrast ratios for selection, status and credits;
- small credit changes at large totals, still visible across all supported
  widths at both 24- and 40-row heights;
- three consecutive polling deadlines with nonzero query time.

## Delivery scope

Implementation, regression tests and related documentation:

```text
CHANGELOG.md
README.md
cs/cli.py
cs/ui.py
docs/GUIDE.md
docs/HANDOVER.md
tests/test_core.py
tests/test_home.py
tests/test_render.py
```

The work builds on the baseline above. Use the branch history for the current
published commit; no pull request has been requested.

## Completion

The reproduced issues are fixed, the user confirmed theme application and
unattended refresh, and commit/push is approved. Already-running instances
must be restarted to load updated Python code:

```bash
./bin/cs
```

For review, use `git diff d4347c8`.
