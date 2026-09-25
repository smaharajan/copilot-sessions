"""The landing screen: menu rows, icons, the banner and how it animates in."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from support import Screen, StoreTest


class HomeMenuTest(StoreTest):
    def test_the_menu_offers_every_view_it_means_to(self):
        from cs import cli

        labels = [label for _, label, _, _, _ in cli._home_items()]
        for wanted in ("Today", "Autonomy", "Handoffs", "Security",
                       "Efficiency", "Hooks", "Theme", "Context"):
            self.assertIn(wanted, labels)
        # Practice, Rhythm, Standup and Working days are commands, not rows.
        # Asserting both halves keeps a restored view from quietly rotting
        # and a retired command from disappearing.
        for wanted in ("Practice", "Rhythm", "Standup", "Working days",
                       "Watch live", "Doctor"):
            self.assertNotIn(wanted, labels)
        for command in ("standup", "coach", "rhythm", "context", "timeline",
                        "hooks", "doctor", "next"):
            self.assertEqual(self._run(command)[0], 0, command)

    def test_group_headings_follow_their_rows(self):
        """Groups are anchored to labels, not to positions in the list.

        The bug this replaces was silent: `_HOME_GROUPS` held indices, so
        adding or retiring a row filed the ones after it under the previous
        heading and nothing about the output looked wrong.
        """
        from cs import cli

        items = cli._home_items()
        groups = cli._home_groups(items)
        labels = [label for _, label, _, _, _ in items]
        for label, group in cli._HOME_GROUP_STARTS:
            self.assertEqual(groups[labels.index(label)], group)
        # Every group opens exactly one row, and they open in menu order.
        self.assertEqual(len(groups), len(cli._HOME_GROUP_STARTS))
        self.assertEqual(list(groups.values()),
                         [group for _, group in cli._HOME_GROUP_STARTS])

    def test_a_group_anchored_to_a_missing_row_is_an_error(self):
        """Better a stack trace on the first frame than three rows quietly
        filed under the wrong heading.

        This is what makes hiding a group safe: comment the rows out and
        leave the anchor behind, and cs says so at startup instead of
        drawing an empty heading.
        """
        from cs import cli

        items = [item for item in cli._home_items() if item[1] != "Skills"]
        with self.assertRaises(KeyError) as caught:
            cli._home_groups(items)
        self.assertIn("Skills", str(caught.exception))

    def test_every_menu_entry_is_reachable_on_a_short_window(self):
        """The menu grows; a terminal does not. Scrolling has to keep up."""
        import curses

        from cs import cli

        items = cli._home_items()
        for height in (18, 24, 40):
            with self.subTest(height=height):
                class Short(Screen):
                    rows = height

                    def getmaxyx(self):
                        return self.rows, 100

                screen = Short([curses.KEY_DOWN] * (len(items) - 1) + [ord("q")])
                cli._home_tui(screen, {})
                drawn = {
                    text for frame in screen.frames for text in frame.values()
                }
                for _icon, label, _what, _action, _asks in items:
                    self.assertTrue(
                        any(label[:14] in line for line in drawn),
                        f"{label} is unreachable at {height} rows",
                    )

    def test_short_menu_marks_more_rows_above_and_below(self):
        import curses

        from cs import cli

        screen = Screen([curses.KEY_DOWN] * 30 + [ord("q")])
        cli._home_tui(screen, {"revealed": True})
        self.assertIn("↓", screen.frames[0].values())
        self.assertIn("↑", screen.frames[-1].values())

    def test_every_menu_action_runs_with_no_arguments(self):
        """The menu calls actions blind, so none may need a term it won't get."""
        from cs import cli

        for _, label, _, action, asks in cli._home_items():
            if asks == "term":
                continue
            if asks in ("ref", "pair"):
                # The menu asks for a session (or two) first, then passes it.
                given = "sess-alpha" if asks == "ref" else ("sess-alpha", "sess-empty")
                with self.subTest(label=label), redirect_stdout(io.StringIO()):
                    action(given)
                continue
            if asks == "theme":
                self.assertEqual(action("dark"), "light")
                continue
            if asks == "period":
                # The menu picks the window; check it runs at both ends.
                for days in (30, 0):
                    with self.subTest(label=label, days=days):
                        buf = io.StringIO()
                        with redirect_stdout(buf):
                            action(days)
                continue
            with self.subTest(label=label):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    action()


    def test_the_wordmark_is_coloured_along_its_length(self):
        """Purple on the left through to cyan on the right, like the splash.

        The bands have to tile the line exactly: a gap drops characters off
        the wordmark and an overlap draws them twice in two colours, which
        reads as a tear rather than a gradient.
        """
        from cs import ui

        line = "██████╗ ██████╗ ██████╗ ██╗██╗"
        runs = ui.gradient_runs(line, 8)
        self.assertEqual("".join(text for _, text, _ in runs), line)
        at = 0
        for column, text, _colour in runs:
            self.assertEqual(column, at)
            at += len(text)
        colours = [colour for _, _, colour in runs]
        self.assertEqual(colours, sorted(colours), "the ramp doubles back")
        self.assertEqual(colours[0], 0)
        self.assertEqual(colours[-1], 7)
        self.assertEqual(ui.gradient_runs("", 8), [])
        self.assertEqual(ui.gradient_runs(line, 0), [])

    def test_the_layout_holds_every_option_however_it_is_grouped(self):
        """Grouped or not, the menu is all of the menu and none of it twice."""
        import cs.cli as cli

        count = len(cli._home_items())
        for grouped in (True, False):
            layout = cli._home_layout(range(count), grouped)
            with self.subTest(grouped=grouped):
                self.assertEqual(
                    [value for kind, value in layout if kind == "item"],
                    list(range(count)))
                heads = [value for kind, value in layout if kind == "head"]
                # Today is a single row and draws no heading of its own.
                expected = (len(cli._HOME_GROUPS) - 1) if grouped else 0
                self.assertEqual(len(heads), expected)
        self.assertEqual(cli._home_layout(range(count), True)[0][0], "item")

    def test_the_menu_is_grouped_at_every_size_worth_grouping(self):
        """Nineteen options in one column is the thing being fixed.

        This asserted the opposite rule for a while — headings only if they
        cost the wordmark nothing — and on every window under about forty
        rows they cost it something, so nobody ever saw a group. The wordmark
        knows how to be smaller; a flat list of nineteen does not know how to
        be readable.
        """
        import cs.cli as cli

        shown = list(range(len(cli._home_items())))
        groups = len(cli._HOME_GROUPS)
        # From 20 rows up. The classic terminal is 24, and below about 19 the
        # captions would be a third of what is on screen — that case is the
        # test underneath.
        for height in range(20, 60):
            with self.subTest(height=height):
                layout, _art = cli._home_plan(100, height, shown, True)
                heads = [row for row in layout if row[0] == "head"]
                self.assertEqual(len(heads), groups - 1,
                                 "the menu lost its sections")
                # And every option is still in the layout, so scrolling
                # reaches it — a heading may cost a scroll, never a row.
                self.assertEqual([value for kind, value in layout
                                  if kind == "item"], shown)

    def test_a_window_too_short_for_captions_goes_flat(self):
        """On a window with barely more rows than groups, captions would be
        most of what is on screen — so they go, and the options stay."""
        import cs.cli as cli

        shown = list(range(len(cli._home_items())))
        layout, _art = cli._home_plan(100, 10, shown, True)
        self.assertFalse(any(kind == "head" for kind, _ in layout))
        self.assertEqual(len(layout), len(shown))

    def test_the_wordmark_never_shrinks_as_the_window_grows(self):
        """A logo that got smaller on a bigger terminal reads as a bug."""
        import cs.cli as cli

        shown = list(range(len(cli._home_items())))
        tallest = 0
        for height in range(18, 60):
            _layout, art = cli._home_plan(100, height, shown, True)
            with self.subTest(height=height):
                self.assertGreaterEqual(
                    len(art), tallest,
                    f"the wordmark shrank from {tallest} to {len(art)} rows "
                    f"when the window grew to {height}",
                )
            tallest = len(art)

    def test_the_header_and_the_menu_fit_the_window_together(self):
        """Whatever it decides, the two halves have to add up."""
        import cs.cli as cli

        shown = list(range(len(cli._home_items())))
        for height in range(24, 60):
            layout, _art = cli._home_plan(100, height, shown, True)
            rows = cli._home_header_rows(100, height, len(layout), True)
            with self.subTest(height=height):
                # The header and the status bar never overrun the window —
                # whatever does not fit is menu, and menu scrolls.
                self.assertLessEqual(rows + 1, height)
                self.assertGreaterEqual(height - rows - 1, 1,
                                        "no rows left to draw a menu in")

    def test_a_window_with_room_for_both_gets_both(self):
        """A tall window owes nothing to either: full wordmark, full groups."""
        import cs.cli as cli

        shown = list(range(len(cli._home_items())))
        # Tall enough for every row, every heading and the full wordmark —
        # the menu has grown past forty rows, so "tall" is measured off it.
        height = len(shown) + len(cli._HOME_GROUP_STARTS) + 14
        layout, art = cli._home_plan(100, height, shown, True)
        self.assertTrue(any(kind == "head" for kind, _ in layout),
                        "a tall window should show its groups")
        self.assertGreater(len(art), 4, "and still have the full wordmark")

    def test_no_row_is_numbered(self):
        """A column of 1-18 only ever reached the first nine, so it went."""
        import cs.cli as cli

        screen = Screen([ord("q")])
        cli._home_tui(screen, {"revealed": True})
        for (_y, x), text in screen.frames[-1].items():
            if x <= 3:
                self.assertNotRegex(
                    text.strip()[:2], r"^\d",
                    f"a numbered column is back: {text!r}",
                )

    def test_typing_narrows_the_menu(self):
        import cs.cli as cli

        items = cli._home_items()
        screen = Screen([ord(c) for c in "credentials"] + [27, ord("q")])
        cli._home_tui(screen, {"revealed": True})
        drawn = {text for text in screen.frames[-2].values()}
        self.assertTrue(any("Security" in line for line in drawn))
        self.assertFalse(any("Repositories" in line for line in drawn),
                         "the filter did not narrow anything")
        # And clearing it brings the whole menu back.
        final = {text for text in screen.frames[-1].values()}
        self.assertTrue(any("Repositories" in line for line in final))
        self.assertEqual(len(cli._home_matches(items, "")), len(items))

    def test_a_filter_that_matches_nothing_says_so_and_opens_nothing(self):
        import cs.cli as cli

        screen = Screen([ord(c) for c in "zzz"] + [10, 27, ord("q")])
        self.assertIsNone(cli._home_tui(screen, {"revealed": True}))
        self.assertTrue(
            any("nothing matches" in text
                for frame in screen.frames for text in frame.values()),
            "an empty result has to say it is empty",
        )

    def test_q_types_while_a_filter_is_up_and_quits_when_it_is_not(self):
        """Every letter belongs to the filter, so Esc is what clears it."""
        import cs.cli as cli

        # 'q' inside a word must not quit halfway through typing it.
        screen = Screen([ord(c) for c in "requests"] + [27, ord("q")])
        self.assertIsNone(cli._home_tui(screen, {"revealed": True}))
        self.assertGreater(len(screen.frames), 9, "q quit while typing")

    def test_t_opens_the_gallery_and_enter_applies_without_filtering(self):
        import curses

        from cs import cli, ui

        original = ui.theme_name()
        state = {"revealed": True, "theme": "dark"}
        try:
            cli._home_tui(
                Screen([
                    ord("t"), curses.KEY_DOWN, 10, ord("q"), ord("q")
                ]),
                state,
            )
            self.assertEqual(state["theme"], "light")
        finally:
            ui.set_theme(original)

    def test_an_applied_theme_is_remembered_for_the_next_run(self):
        """The whole point of a gallery: you visit it once.

        The theme used to live only in the running process, so applying one
        and quitting put you back on the default landing screen with the
        gallery to visit again.
        """
        import curses
        import importlib

        from cs import cli, ui

        original = ui.theme_name()
        try:
            cli._home_tui(
                Screen([ord("t"), curses.KEY_DOWN, 10, ord("q"), ord("q")]),
                {"revealed": True, "theme": "dark"},
            )
            self.assertEqual(ui.saved_theme(), "light")
            # What the next `cs` starts in, with nothing carried in memory.
            self.assertEqual(importlib.reload(ui).theme_name(), "light")
        finally:
            importlib.reload(ui)
            ui.set_theme(original)

    def test_leaving_the_gallery_without_applying_remembers_nothing(self):
        """Esc is 'back', and back must not write a preview to disk."""
        from cs import cli, ui

        original = ui.theme_name()
        try:
            with patch.object(cli, "_theme_picker", return_value="dark"):
                cli._home_tui(
                    Screen([ord("t"), ord("q"), ord("q")]),
                    {"revealed": True, "theme": "dark"},
                )
            self.assertIsNone(ui.saved_theme())
            self.assertFalse(ui.settings_path().exists())
        finally:
            ui.set_theme(original)

    def test_a_theme_that_could_not_be_saved_says_so_on_the_status_line(self):
        """Silently forgetting it is how the same bug gets reported twice."""
        import curses

        from cs import cli, ui

        original = ui.theme_name()
        screen = Screen([ord("t"), curses.KEY_DOWN, 10, ord("q"), ord("q")])
        try:
            with patch.object(ui, "save_theme", return_value=False):
                state = {"revealed": True, "theme": "dark"}
                cli._home_tui(screen, state)
            self.assertTrue(state["theme_error"])
            self.assertIn("not saved", screen.frames[-1][(23, 0)])
        finally:
            ui.set_theme(original)

    def test_the_theme_row_opens_the_gallery_and_returns_home_after_applying(self):
        import curses

        from cs import cli, ui

        original = ui.theme_name()
        items = cli._home_items(theme="dark")
        theme = next(i for i, item in enumerate(items) if item[1] == "Theme")
        state = {"revealed": True, "theme": "dark"}
        screen = Screen(
            [curses.KEY_DOWN] * theme
            + [10, curses.KEY_DOWN, 10, ord("q"), ord("q")]
        )
        try:
            self.assertIsNone(cli._home_tui(screen, state))
            self.assertEqual(state["theme"], "light")
            self.assertTrue(any(
                text.startswith("Light · choose from 20")
                for text in screen.frames[-1].values()
            ))
        finally:
            ui.set_theme(original)

    def test_double_click_opens_the_theme_gallery(self):
        import curses

        from cs import cli

        probe = Screen([curses.KEY_END, ord("q")])
        cli._home_tui(probe, {"revealed": True, "theme": "dark"})
        theme_row = next(
            row for frame in probe.frames for (row, column), text in frame.items()
            if column == 6 and text.strip() == "Theme"
        )
        events = iter([
            ("move", 6, theme_row),
            ("double", 6, theme_row),
        ])

        def mouse_event(_screen, _curses, key, _last_click, _pending):
            return next(events) if key == curses.KEY_MOUSE else None

        screen = Screen([
            curses.KEY_END, curses.KEY_MOUSE, curses.KEY_MOUSE, 27
        ])
        with (
            patch.object(cli, "_mouse_event", side_effect=mouse_event),
            patch.object(cli, "_theme_picker", return_value="dark") as picker,
        ):
            cli._home_tui(screen, {"revealed": True, "theme": "dark"})
        picker.assert_called_once_with(screen, "dark")

    def test_hover_and_single_click_preview_without_applying(self):
        import curses

        from cs import cli

        screen = Screen([curses.KEY_MOUSE, curses.KEY_MOUSE, ord("q")])
        with patch.object(
            cli,
            "_mouse_event",
            side_effect=[("move", 5, 3), ("click", 5, 3), None],
        ):
            self.assertEqual(cli._theme_picker(screen, "dark"), "dark")
        self.assertGreaterEqual(len(screen.frames), 2)

    def test_double_click_applies_a_theme_and_returns_to_home(self):
        import curses

        from cs import cli

        screen = Screen([curses.KEY_MOUSE, curses.KEY_MOUSE, ord("q")])
        with patch.object(
            cli,
            "_mouse_event",
            side_effect=[("move", 5, 3), ("double", 5, 3)],
        ):
            self.assertEqual(cli._theme_picker(screen, "dark"), "light")
        self.assertEqual(len(screen.frames), 2)

    def test_hover_keeps_the_same_theme_under_the_pointer_when_scrolled(self):
        import curses

        from cs import cli

        class Short(Screen):
            def getmaxyx(self):
                return 10, 100

        screen = Short([curses.KEY_MOUSE, curses.KEY_MOUSE, ord("q")])
        with patch.object(
            cli,
            "_mouse_event",
            side_effect=[("move", 5, 3), ("double", 5, 3)],
        ):
            chosen = cli._theme_picker(screen, "cyberpunk")
        self.assertEqual(chosen, "everforest")

    def test_the_theme_picker_holds_its_shape_at_supported_widths(self):
        from cs import cli, ui

        for width in (40, 60, 80, 100, 140):
            with self.subTest(width=width):
                class Sized(Screen):
                    def __init__(self, keys, columns):
                        super().__init__(keys)
                        self.columns = columns

                    def getmaxyx(self):
                        return 24, self.columns

                screen = Sized([ord("q")], width)
                self.assertEqual(cli._theme_picker(screen, "dark"), "dark")
                for (row, column), text in screen.frames[-1].items():
                    self.assertLess(row, 24)
                    self.assertLessEqual(column + ui.cells(text), width)
                footer = screen.frames[-1][(23, max((width - 100) // 2, 0))]
                self.assertIn("Enter", footer)
                self.assertIn("Esc", footer)

    def test_the_cursor_steps_through_what_is_on_screen(self):
        """With a filter up, the rows between two matches are not there."""
        import cs.cli as cli

        items = cli._home_items()
        shown = cli._home_matches(items, "days")
        self.assertGreater(len(shown), 2)
        first, second = shown[0], shown[1]
        self.assertEqual(cli._home_step(shown, first, 1), second)
        self.assertEqual(cli._home_step(shown, first, -1), first)
        self.assertEqual(cli._home_step(shown, shown[-1], 1), shown[-1])
        self.assertEqual(cli._home_step([], 3, 1), 3)

    def test_typing_lands_on_the_row_whose_name_matches(self):
        """Delegation's description mentions sub-agents and sits above the
        Sub-agents row; typing the row's name must not open Delegation."""
        import curses

        import cs.cli as cli

        items = cli._home_items()
        wanted = next(i for i, item in enumerate(items) if item[1] == "Sub-agents")
        screen = Screen([*map(ord, "sub-agents"), curses.KEY_ENTER])
        choice = cli._home_tui(screen, {"period": 30, "revealed": True})
        self.assertEqual(choice, (wanted, 30))
        # And a label that is a word inside another one: "agents" is the
        # Agents row, not the Sub-agents row that happens to come first.
        agents = next(i for i, item in enumerate(items) if item[1] == "Agents")
        screen = Screen([*map(ord, "agents"), curses.KEY_ENTER])
        self.assertEqual(cli._home_tui(screen, {"period": 30, "revealed": True}),
                         agents)

    def test_a_heading_only_appears_when_its_group_does(self):
        """Filtering to two rows should show two headings, not all five."""
        import cs.cli as cli

        items = cli._home_items()
        shown = cli._home_matches(items, "credentials")
        layout = cli._home_layout(shown, True)
        heads = [value for kind, value in layout if kind == "head"]
        self.assertEqual(len(heads), 1)
        self.assertEqual(len(shown), len([1 for kind, _ in layout
                                          if kind == "item"]))

    def test_the_wordmark_wipes_in_once_and_then_stops(self):
        """Motion on arrival, stillness while you read — and never again.

        Replaying it every time a view handed you back would turn a greeting
        into a stutter, so the menu records that it has run.
        """
        import cs.cli as cli

        # -1 is what a timed getch reports when nothing was typed; the wipe
        # advances on each one and has to finish on its own.
        screen = Screen([-1] * 40 + [ord("q")])
        state: dict = {}
        self.assertIsNone(cli._home_tui(screen, state))
        self.assertTrue(state["revealed"], "the wipe never finished")
        self.assertFalse(screen.keys, "it should have read every frame")

        # Second visit: no wipe at all, so the first frame is the whole thing.
        again = Screen([ord("q")])
        cli._home_tui(again, state)
        self.assertEqual(len(again.frames), 1)

    def test_the_wipe_eases_and_never_stalls(self):
        """A constant number of columns per frame reads as a machine drawing.

        Decelerating into place reads as something settling — but only if it
        keeps moving: squaring the curve left the last three frames one
        column apart, which looks less like an ending than like a hang.
        """
        from cs import ui

        span = 68
        columns = [ui.reveal_columns(frame, span)
                   for frame in range(ui.REVEAL_FRAMES + 1)]
        self.assertEqual(columns[0], 0)
        self.assertEqual(columns[-1], span, "the wipe never finished")
        self.assertEqual(columns, sorted(columns), "the wipe went backwards")
        steps = [b - a for a, b in zip(columns, columns[1:], strict=False)]
        self.assertTrue(all(step > 0 for step in steps), f"a frame stalled: {steps}")
        # Eased, not linear: it starts faster than it ends.
        self.assertGreater(steps[0], steps[-1] * 2)
        # And it stays finished past the last frame.
        self.assertEqual(ui.reveal_columns(ui.REVEAL_FRAMES * 3, span), span)
        self.assertEqual(ui.reveal_columns(4, 0), 0)

    def test_the_wipe_crosses_the_wordmark_on_a_slant(self):
        """A vertical edge is a shutter; a slanted one is light crossing the
        letters. Each row trails the one above, and the strip below trails
        them all, so the whole header is lit by one pass."""
        import cs.cli as cli

        theme = {k: 0 for k in ("title", "help", "repo", "separator", "number",
                                "summary", "cursor", "header", "credits", "turns")}

        def edges(frame):
            screen = Screen()
            art = cli._home_art(110, 40, 14, True)
            cli._draw_home_header(screen, theme, 110, art, [("9", "s")],
                                  None, frame, 0, [3] * 60)
            drawn = {}
            for (y, _x), text in screen.frame.items():
                if any(ch in "█╗╔╚╝║═" for ch in text):
                    drawn[y] = max(drawn.get(y, 0), len(text))
            return [drawn[y] for y in sorted(drawn)]

        part = edges(4)
        self.assertGreater(len(part), 2, "the wordmark was not part-drawn")
        # Each row is at most as far along as the one above it.
        self.assertEqual(part, sorted(part, reverse=True),
                         f"the edge is not a slant: {part}")
        self.assertGreater(part[0], part[-1], "the edge is vertical")
        # By the end every row has caught up.
        done = edges(None)
        self.assertEqual(len(set(len(str(e)) for e in done)), 1)

    def test_the_activity_strip_is_the_store_s_own_shape(self):
        """A dense series, so a quiet fortnight and a busy one differ.

        Built from `timeline`'s sparse rows the gaps would close up and every
        stretch would draw as equally busy — which is the one thing the line
        exists to show.
        """
        from cs import db, ui

        conn = db.connect()
        series = db.activity(conn, 30)
        self.assertEqual(db.activity(conn, 0), [], "no window, no series")
        conn.close()
        self.assertEqual(len(series), 30, "a day with nothing still gets a slot")
        self.assertTrue(all(isinstance(count, int) for count in series))

        # Zero is blank, the busiest day is full height, and nothing in
        # between rounds down to nothing.
        self.assertEqual(ui.sparkline([0, 1, 8]), " ▁█")
        self.assertEqual(ui.sparkline([]), "")
        self.assertEqual(ui.sparkline([0, 0]), "  ")
        drawn = ui.sparkline([0, 1, 2, 3, 40])
        self.assertEqual(len(drawn), 5)
        self.assertEqual(drawn[0], " ")
        self.assertNotIn(" ", drawn[1:], "a day with sessions drew as empty")

    def test_the_activity_strip_fills_in_with_the_wordmark(self):
        """Both are driven by the one wipe, so they finish together rather
        than one waiting on the other."""
        import cs.cli as cli

        theme = {k: 0 for k in ("title", "help", "repo", "separator", "number",
                                "summary", "cursor", "header", "credits", "turns")}
        activity = [1, 5, 2, 8, 3, 9, 4, 7, 2, 6] * 6

        def strip(reveal):
            screen = Screen()
            art = cli._home_art(110, 40, 14, True)
            cli._draw_home_header(screen, theme, 110, art, [("9", "sessions")],
                                  None, reveal, 0, activity)
            return "".join(
                text for (_y, _x), text in sorted(screen.frame.items())
                if any(ch in "▁▂▃▄▅▆▇█" for ch in text)
            )

        part, whole = strip(12), strip(None)
        self.assertTrue(part, "nothing drawn part way through the wipe")
        self.assertLess(len(part), len(whole), "the strip ignored the wipe")
        self.assertTrue(whole.endswith(part[-1]) or part in whole
                        or whole.startswith(part[:4]),
                        "the strip fills from the oldest day forward")
        # And a store with nothing in it gets no row at all rather than a
        # blank one pretending to be a chart.
        screen = Screen()
        rows = cli._draw_home_header(screen, theme, 110,
                                     cli._home_art(110, 40, 14, True), [],
                                     None, None, 0, [0] * 60)
        empty = cli._draw_home_header(Screen(), theme, 110,
                                      cli._home_art(110, 40, 14, False), [],
                                      None, None, 0, None)
        self.assertEqual(rows, empty, "an empty series still took a row")

    def test_the_window_steps_with_the_arrows(self):
        """Clamped at both ends: ← at seven days stays at seven days."""
        import cs.cli as cli

        self.assertEqual(cli._step_period(30, 1), 90)
        self.assertEqual(cli._step_period(30, -1), 7)
        self.assertEqual(cli._step_period(7, -1), 7, "wrapped round to all time")
        self.assertEqual(cli._step_period(0, 1), 0, "wrapped round to a week")
        # 0 is 'all time', a window like any other, and stepping off it works.
        self.assertEqual(cli._step_period(0, -1), 365)

    def test_every_row_opens_on_one_enter(self):
        """Six of nineteen rows used to want a second Enter.

        The counting views asked which window to count over *after* you had
        chosen them, so half the menu opened on one keystroke and the rest on
        two — which reads as the menu missing the first press.
        """
        import curses

        import cs.cli as cli

        items = cli._home_items()
        state: dict = {"revealed": True, "period": 30, "theme": "dark"}
        for index, item in enumerate(items):
            if item[4] in ("term", "ref", "pair"):
                continue  # a search with no term, or a replay of nothing, is not a view
            with self.subTest(row=item[1]):
                # Down to the row, then a single Enter.
                stays = item[4] == "theme"
                keys = [curses.KEY_DOWN] * index + [10]
                if stays:
                    keys += [curses.KEY_DOWN, 10, ord("q"), ord("q")]
                current = dict(state)
                chosen = cli._home_tui(Screen(keys), current)
                if stays:
                    self.assertIsNone(chosen)
                    self.assertEqual(current["theme"], "light")
                    continue
                got = chosen[0] if isinstance(chosen, tuple) else chosen
                self.assertEqual(got, index, f"{item[1]} did not open")
                if item[4] == "period":
                    self.assertEqual(chosen, (index, 30),
                                     "the remembered window was not passed in")

    def test_the_menu_remembers_the_window_you_chose(self):
        """Asking again from scratch for every view is a form, not a menu."""
        import curses

        import cs.cli as cli

        items = cli._home_items()
        spend = next(i for i, item in enumerate(items) if item[1] == "AI spend")
        self.assertEqual(items[spend][4], "period")

        state: dict = {"revealed": True}
        # Type the name, widen the window to all time, then open it.
        opening = [ord(c) for c in "ai spend"]
        chosen = cli._home_tui(Screen(opening + [curses.KEY_RIGHT] * 4 + [10]), state)
        self.assertEqual(chosen, (spend, 0))
        self.assertEqual(state["period"], 0, "the window was not remembered")

        # Next time Enter alone takes it — 0 is a window, not 'nothing chosen'.
        again = cli._home_tui(Screen(opening + [10]), state)
        self.assertEqual(again, (spend, 0))

    def test_the_counting_rows_say_which_window_they_will_use(self):
        """The window is a setting now, so the rows have to show it."""
        import cs.cli as cli

        for period, said in ((7, "last 7 days"), (0, "all time")):
            with self.subTest(period=period):
                described = {label: description for _icon, label, description,
                             _action, _asks in cli._home_items(period)}
                self.assertIn(said, described["Efficiency"])
                self.assertIn(said, described["AI spend"])
                self.assertIn(said, described["Security"])
                # And a row that counts nothing says nothing about a window.
                self.assertNotIn(said, described["MCP servers"])

    def test_every_counting_view_takes_all_from_the_shell_too(self):
        """The menu is not the only way in, so `cs cost all` has to work."""
        for command in ("timeline", "cost", "agents", "stats", "audit"):
            with self.subTest(command=command):
                code, out = self._run(command, "all")
                self.assertEqual(code, 0)
                self.assertIn("all time", out)
                self.assertNotIn("last 0 days", out)
        code, out = self._run("timeline", "7")
        self.assertEqual(code, 0)
        self.assertIn("last 7 days", out)
        # A bad window is still an error, not a silent 'everything'.
        self.assertEqual(self._run("timeline", "-3")[0], 1)


class BannerTest(unittest.TestCase):
    def test_the_banner_shrinks_before_it_disappears(self):
        from cs import ui

        wide = ui.banner(100, 20)
        self.assertEqual(len(wide), len(ui.BANNER))
        narrow = ui.banner(40, 20)
        self.assertLess(len(narrow), len(wide))
        self.assertGreater(len(narrow), 1)
        self.assertEqual(ui.banner(30, 2), ["cs · copilot sessions"])
        self.assertEqual(ui.banner(10, 2), ["cs"])

    def test_every_banner_line_is_the_same_width(self):
        from cs import ui

        for width, height in ((100, 20), (40, 20)):
            art = ui.banner(width, height)
            self.assertEqual(len({len(line) for line in art}), 1)


class LandingAnimationTest(StoreTest):
    """The menu arrives with the wordmark instead of under a finished one.

    The wipe across the banner already said the tool was awake; the menu
    below it appeared complete on frame one, so the landing screen animated
    its decoration and not its content. The cascade is the same easing on the
    same clock, which makes the two halves one gesture rather than two.
    """

    def test_the_cascade_shares_the_wipes_clock(self):
        """Both halves have to finish together. A menu that settles before
        the wordmark makes the banner look stuck; after it, dropped."""
        from cs import ui

        self.assertEqual(ui.reveal_rows(0, 20), 0)
        self.assertEqual(ui.reveal_rows(ui.REVEAL_FRAMES, 20), 20)
        self.assertEqual(ui.reveal_columns(ui.REVEAL_FRAMES, 60), 60)

    def test_the_menu_starts_after_the_wordmark_and_never_overruns(self):
        """It trails the wipe rather than racing it, and it is monotonic —
        a row that appeared must not disappear on the next frame."""
        from cs import ui

        counts = [ui.reveal_rows(f, 20) for f in range(ui.REVEAL_FRAMES + 4)]
        self.assertEqual(counts[0], 0)
        self.assertTrue(any(c == 0 for c in counts[1:ui.REVEAL_MENU_START]))
        self.assertEqual(counts, sorted(counts))
        self.assertTrue(all(0 <= c <= 20 for c in counts))

    def test_an_empty_menu_animates_to_nothing_rather_than_dividing(self):
        """A filter that matches no row leaves the cascade with zero rows to
        deal out, which must be a quiet no-op and not an arithmetic error."""
        from cs import ui

        for frame in range(ui.REVEAL_FRAMES + 2):
            self.assertEqual(ui.reveal_rows(frame, 0), 0)

    @staticmethod
    def _menu_rows(frame: dict) -> int:
        """How many menu rows a captured frame actually drew.

        Labels are placed at a fixed column, which is what makes them
        countable without knowing anything about the wording.
        """
        return len({row for (row, col) in frame if col == 6})

    @staticmethod
    def _on_rule(frame: dict) -> int | None:
        """Which column the agent stands in on the header rule — None if absent.

        The rule is the only full-width run of '─' drawn at column 0; the
        group headings draw their own hairlines, but indented past a caption.
        Finding the row first is what keeps this from matching the identical
        glyph on the Agent profiles menu row.
        """
        from cs import ui

        rule = next((row for (row, col), text in frame.items()
                     if col == 0 and text and set(text) == {"─"}), None)
        if rule is None:
            return None
        icon = ui.menu_icon("copilot")
        return next((col for (row, col), text in frame.items()
                     if row == rule and text == icon), None)

    def test_a_terminal_that_cannot_animate_draws_the_whole_menu(self):
        """`reveal` is None wherever the wipe never armed — no timeout
        support, or a screen already visited. Every row draws immediately in
        that case, which is what the frame cap is written to allow."""
        from cs import cli

        screen = Screen([ord("q")])
        cli._home_tui(screen, {"revealed": True})
        self.assertGreaterEqual(self._menu_rows(screen.frames[-1]), 10)

    def test_the_cascade_actually_deals_the_rows_out(self):
        """The first frame of a fresh screen shows fewer rows than the last.

        Without this the easing could be perfect arithmetic wired to nothing,
        which is the failure mode of every animation written against a
        helper rather than against the screen.
        """
        from cs import cli, ui

        screen = Screen([-1] * (ui.REVEAL_FRAMES + 1) + [ord("q")])
        cli._home_tui(screen, {})
        counts = [self._menu_rows(f) for f in screen.frames]
        self.assertEqual(counts[0], 0)
        self.assertLess(counts[ui.REVEAL_MENU_START], counts[-1])
        self.assertEqual(counts, sorted(counts))

    def test_the_cascade_settles_when_a_key_arrives(self):
        """Nobody should have to wait out an animation. A keypress lands you
        on the finished screen with every row on it, not on a half-dealt one."""
        import curses

        from cs import cli

        # An arrow, not a letter: the menu is type-to-filter, so a letter
        # would open the filter box rather than move the cursor.
        screen = Screen([-1, -1, curses.KEY_DOWN, ord("q")])
        state: dict = {}
        cli._home_tui(screen, state)
        self.assertTrue(state.get("revealed"))
        self.assertGreaterEqual(self._menu_rows(screen.frames[-1]), 10)

    def test_the_wipe_plays_once_a_run_and_not_once_a_visit(self):
        """Coming back from a view is a return, not an arrival. Replaying the
        cascade every time a report hands you back would turn a greeting into
        a stutter, so the finished state is remembered on the session."""
        from cs import cli

        state: dict = {}
        cli._home_tui(Screen([-1, ord("q")]), state)
        self.assertTrue(state.get("revealed"))
        second = Screen([ord("q")])
        cli._home_tui(second, state)
        self.assertGreaterEqual(self._menu_rows(second.frames[0]), 10)

    def test_the_agent_paces_the_rule_and_then_stops(self):
        """The one moving thing on a settled screen, and it has an end.

        Two halves, and the second is the one worth a test. It has to move —
        an animation wired to nothing draws the same frame forever and looks
        exactly like a working one that happens to be still. And it has to
        stop, because a terminal left open on this menu would otherwise ask
        for a frame every 90ms for as long as it is open, which is the whole
        reason the wipe was written as a wipe and not as a loop.

        Read off the rule row rather than off the whole frame: the Agent
        profiles row carries the same 🤖, deliberately, so a search of the
        screen for the glyph finds the menu and passes whatever the walker
        does.
        """
        from cs import cli, ui

        screen = Screen([-1] * (ui.PACE_FRAMES + 40) + [ord("q")])
        cli._home_tui(screen, {"revealed": True})

        walked = [self._on_rule(frame) for frame in screen.frames]
        walked = [col for col in walked if col is not None]
        self.assertTrue(walked, "the agent never appeared on the rule")
        self.assertGreater(len(set(walked)), 1, "it appeared but never moved")
        # Having rested it stands still — still drawn, no longer pacing.
        self.assertEqual(len(set(walked[-20:])), 1, "it never settled")

    def test_the_agent_stays_off_the_screen_that_cannot_time_a_keypress(self):
        """No timeout means no frames, and a walk drawn once is a smudge on
        the rule that never moves. The wipe already declines that window."""
        from cs import cli

        class Untimed(Screen):
            def timeout(self, milliseconds):
                raise AttributeError("no timed read here")

        screen = Untimed([ord("q")])
        cli._home_tui(screen, {"revealed": True})
        self.assertEqual(
            [self._on_rule(frame) for frame in screen.frames],
            [None] * len(screen.frames),
        )

    def test_the_whole_home_refreshes_on_its_heartbeat(self):
        from cs import cli, ui

        class ClockScreen(Screen):
            def __init__(self):
                # One idle second per tick, so the interval itself is the
                # number of ticks it takes to reach a refresh.
                super().__init__([-1] * cli._REFRESH_SECONDS + [ord("q")])
                self.now = 0.0
                self.delay = 0
                self.delays = []
                self.read_delays = []

            def timeout(self, milliseconds):
                super().timeout(milliseconds)
                self.delay = milliseconds
                self.delays.append(milliseconds)

            def getch(self):
                self.read_delays.append(self.delay)
                key = super().getch()
                if key == -1:
                    self.now += self.delay / 1000
                return key

        screen = ClockScreen()
        state = {"revealed": True, "facts": [("1", "sessions")]}
        snapshot = ([("2", "sessions")], [0, 1])
        with (
            patch.object(ui, "PACE_FRAMES", 0),
            patch.object(cli.time, "monotonic", side_effect=lambda: screen.now),
            patch.object(cli, "_home_snapshot", return_value=snapshot) as fresh,
        ):
            cli._home_tui(screen, state)

        fresh.assert_called_once_with()
        self.assertEqual(state["facts"], snapshot[0])
        self.assertEqual(state["activity"], snapshot[1])
        self.assertTrue(any("2" in text
                            for text in screen.frames[-1].values()))
        self.assertTrue(any(ui.PACE_MS < delay <= 1000
                            for delay in screen.delays))
        self.assertEqual(screen.read_delays[-1], 1000,
                         "idle input must keep the one-second refresh heartbeat")

    def test_a_new_usage_event_changes_the_next_home_snapshot(self):
        import sqlite3

        from cs import cli

        before, _activity = cli._home_snapshot()
        conn = sqlite3.connect(cli.db.default_db_path())
        conn.execute(
            """INSERT INTO assistant_usage_events
               (session_id, turn_index, model, total_nano_aiu)
               VALUES ('sess-alpha', 2, 'gpt-test', 1000000000)"""
        )
        conn.commit()
        conn.close()
        after, _activity = cli._home_snapshot()

        def aiu(facts):
            return next(value for value, label in facts if label == "AIU")

        self.assertEqual(aiu(before), "4.00")
        self.assertEqual(aiu(after), "5.00")

    def test_small_credit_updates_remain_visible_with_large_totals(self):
        import sqlite3

        from cs import cli, ui

        with sqlite3.connect(cli.db.default_db_path()) as conn:
            conn.execute(
                """INSERT INTO assistant_usage_events
                   (session_id, model, total_nano_aiu)
                   VALUES ('sess-alpha', 'test-model', 999996000000000)"""
            )
        before, _ = cli._home_snapshot()
        with sqlite3.connect(cli.db.default_db_path()) as conn:
            conn.execute(
                """INSERT INTO assistant_usage_events
                   (session_id, model, total_nano_aiu)
                   VALUES ('sess-alpha', 'test-model', 250000000)"""
            )
        after, _ = cli._home_snapshot()
        self.assertIn(("1,000,000.00", "AIU"), before)
        self.assertIn(("1,000,000.25", "AIU"), after)
        facts = [
            ({"sessions": "1,234", "turns": "7,890"}.get(label, value), label)
            for value, label in after
        ]
        for height in (24, 40):
            for width in (40, 60, 80, 100, 140):
                with self.subTest(height=height, width=width):
                    screen = Screen([ord("q")])
                    screen.getmaxyx = lambda h=height, w=width: (h, w)
                    cli._home_tui(screen, {"revealed": True, "facts": facts})
                    self.assertIn("1,000,000.25", screen.frames[0].values())
                    for (_row, column), text in screen.frames[0].items():
                        self.assertLessEqual(column + ui.cells(text), width)

    def test_repeated_refreshes_rearm_input_without_query_time_drift(self):
        from cs import cli, ui

        starts = []

        class ClockScreen(Screen):
            now = 0.0
            delay = -1

            def timeout(self, milliseconds):
                super().timeout(milliseconds)
                self.delay = milliseconds

            def getch(self):
                delay = self.delay
                self.delay = -1  # a read/handler must not leave the next read blocking
                if not 1 <= delay <= 1000:
                    raise AssertionError("every home read needs a bounded timeout")
                if len(starts) == 3:
                    return ord("q")
                self.now += delay / 1000
                return -1

        screen = ClockScreen()

        def snapshot():
            starts.append(screen.now)
            screen.now += 1.5
            return [(f"{len(starts):.2f}", "AIU")], []

        with (
            patch.object(ui, "PACE_FRAMES", 0),
            patch.object(cli.time, "monotonic", side_effect=lambda: screen.now),
            patch.object(cli, "_home_snapshot", side_effect=snapshot),
        ):
            cli._home_tui(screen, {"revealed": True})
        self.assertEqual(
            starts,
            [cli._REFRESH_SECONDS * n for n in (1, 2, 3)],
            "each refresh is timed from the deadline, not from when the last"
            " query happened to finish",
        )
        self.assertIn("3.00", screen.frames[-1].values())

    def test_mouse_reports_do_not_stop_idle_refresh(self):
        from cs import cli, ui

        class ClockScreen(Screen):
            now = 0.0
            delay = -1

            def timeout(self, milliseconds):
                super().timeout(milliseconds)
                self.delay = milliseconds

            def nodelay(self, enabled):
                self.timeout(0 if enabled else -1)

            def getch(self):
                key = super().getch()
                if key == -1:
                    if self.delay < 0:
                        raise AssertionError("mouse report left home input blocking")
                    self.now += self.delay / 1000
                return key

        events = [
            [27, *map(ord, report)]
            for report in ("[<35;10;5M", "[<0;10;5m", "[<65;10;5M")
        ]
        events.append([ord("/"), *events[0], 10])
        for keys in events:
            with self.subTest(keys=keys):
                screen = ClockScreen(
                    [*keys, *([-1] * cli._REFRESH_SECONDS), ord("q")]
                )
                state = {"revealed": True, "facts": [("4.00", "AIU")]}
                snapshot = ([("5.00", "AIU")], [0, 1])
                with (
                    patch.object(ui, "PACE_FRAMES", 0),
                    patch.object(
                        cli.time, "monotonic", side_effect=lambda screen=screen: screen.now
                    ),
                    patch.object(cli, "_home_snapshot", return_value=snapshot) as fresh,
                ):
                    cli._home_tui(screen, state)
                fresh.assert_called_once_with()
                self.assertEqual(state["facts"], snapshot[0])
                self.assertTrue(any(
                    "5.00" in text for text in screen.frames[-1].values()
                ))

    def test_refresh_time_stays_visible_at_supported_widths(self):
        from cs import cli, ui

        for width in (40, 60, 80, 100, 140):
            with self.subTest(width=width):
                status = cli._home_status("", 20, 20, width, refreshed="12:34:56")
                self.assertIn("updated 12:34:56", status)
                self.assertLessEqual(ui.cells(status), width)

    def test_a_failed_refresh_keeps_the_last_good_readings(self):
        import sqlite3

        from cs import cli

        state = {"facts": [("1", "sessions")], "activity": [1]}
        with patch.object(
            cli, "_home_snapshot", side_effect=sqlite3.OperationalError
        ):
            self.assertFalse(cli._refresh_home(state))
        self.assertEqual(state["facts"], [("1", "sessions")])
        self.assertEqual(state["activity"], [1])
        self.assertTrue(state["refresh_error"])

    def test_returning_home_does_not_restart_an_overdue_refresh_deadline(self):
        from cs import cli

        state = {"revealed": True, "facts": [("4.00", "AIU")]}
        with patch.object(cli.time, "monotonic", return_value=0):
            cli._home_tui(Screen([ord("q")]), state)
        screen = Screen([ord("q")])
        with (
            patch.object(cli.time, "monotonic", return_value=60),
            patch.object(
                cli, "_home_snapshot", return_value=([("5.00", "AIU")], [])
            ) as fresh,
        ):
            cli._home_tui(screen, state)
        fresh.assert_called_once_with()
        self.assertTrue(any("5.00" in text for text in screen.frames[0].values()))


class CursesMouseCompatibilityTest(unittest.TestCase):
    def test_only_old_native_sgr_needs_compat_and_term_is_restored(self):
        import os
        from types import SimpleNamespace
        from unittest.mock import Mock

        from cs import cli

        for modern, mouse, expected in (
            (False, b"\x1b[<", "xterm-256color"),
            (False, b"\x1b[M", "test-terminal"),
            (True, b"\x1b[<", "test-terminal"),
        ):
            with self.subTest(modern=modern, mouse=mouse):
                seen = []

                def wrapper(callback, *args, seen=seen):
                    seen.append(os.environ["TERM"])
                    return callback(*args)

                curses = SimpleNamespace(
                    setupterm=Mock(), tigetstr=Mock(return_value=mouse),
                    wrapper=wrapper,
                )
                if modern:
                    curses.BUTTON5_PRESSED = 1
                with (
                    patch.dict("sys.modules", {"curses": curses}),
                    patch.dict(os.environ, {"TERM": "test-terminal"}),
                    patch.object(cli, "_CURSES_MOUSE_COMPAT", None),
                ):
                    self.assertEqual(cli._curses_wrapper(lambda n: n, 42), 42)
                    # ncurses caches the fallback's terminfo after the first view.
                    curses.tigetstr.return_value = b"\x1b[M"
                    self.assertEqual(cli._curses_wrapper(lambda n: n, 43), 43)
                    with self.assertRaisesRegex(RuntimeError, "test failure"):
                        cli._curses_wrapper(
                            Mock(side_effect=RuntimeError("test failure"))
                        )
                    self.assertEqual(os.environ["TERM"], "test-terminal")
                self.assertEqual(seen, [expected] * 3)

    def test_only_menus_request_mouse_motion(self):
        import curses

        from cs import cli

        for motion in (False, True):
            with (
                self.subTest(motion=motion),
                patch.object(curses, "mousemask", return_value=(1, 0)) as mask,
                patch.object(curses, "mouseinterval"),
                patch.object(curses, "set_escdelay", create=True),
                patch.object(cli, "_write_terminal") as write,
                patch.object(cli, "_SGR_ENABLED", False),
                patch.object(cli, "_MOUSE_USED", False),
            ):
                self.assertTrue(cli._enable_mouse(curses, motion=motion))
                self.assertEqual(
                    bool(mask.call_args.args[0] & curses.REPORT_MOUSE_POSITION), motion
                )
                self.assertEqual("\033[?1003h" in write.call_args.args[0], motion)


class MenuIconTest(StoreTest):
    """Every row brings an icon, and it renders where the row is read.

    The hook row used U+1FA9D, added to Unicode in 2020, and a terminal whose
    emoji font predates it draws a blank — so the row looked like the one
    option on the menu that forgot its icon. A missing glyph reads as a bug
    in the tool rather than as a gap in a font, which is why the rule these
    cover is about the *age* of a codepoint and not about taste.
    """

    # Emoji whose fonts a terminal cannot be assumed to have: anything added
    # after Unicode 9.0 (2016). Not exhaustive — it does not need to be. It
    # names the ones that were actually reached for here, so reaching for
    # them again fails in the suite instead of on someone's screen.
    TOO_NEW = {0x1F9E9: "jigsaw, Unicode 11", 0x1FA9D: "hook, Unicode 13",
               0x1F9ED: "compass, Unicode 11", 0x1F9E0: "brain, Unicode 10",
               0x1F9F0: "toolbox, Unicode 11", 0x1FA99: "coin, Unicode 13"}

    def test_every_row_has_an_icon(self):
        """The bug as reported: some rows appeared to have none."""
        from cs import cli

        for icon, label, _, _, _ in cli._home_items():
            with self.subTest(row=label):
                self.assertTrue(icon.strip(), f"{label} has no icon")

    def test_no_icon_is_too_new_for_a_terminal_font(self):
        """Every glyph in the table, including ones not currently on a row —
        so adding a menu entry cannot walk a missing-glyph bug back in."""
        from cs import ui

        for name, (emoji, _) in ui._MENU_GLYPHS.items():
            for ch in emoji:
                with self.subTest(row=name):
                    self.assertNotIn(ord(ch), self.TOO_NEW,
                                     f"{name}: {self.TOO_NEW.get(ord(ch))}")

    def test_every_icon_occupies_exactly_two_columns(self):
        """Labels are placed at an absolute column, so an icon that measures
        one cell or three leaves the row looking unset rather than wrong —
        which is the other way a row reads as having no icon."""
        from cs import ui

        for name, (emoji, plain) in ui._MENU_GLYPHS.items():
            with self.subTest(row=name):
                self.assertEqual(ui.cells(emoji), 2)
                self.assertLessEqual(ui.cells(plain), 2)

    def test_no_icon_comes_from_the_older_symbol_blocks(self):
        """The defect behind the two rows that looked iconless.

        U+2100–U+2BFF holds emoji that predate emoji: ⚡ and ❓ have text
        glyphs going back decades, so a terminal may draw them from the
        monospace font rather than the emoji font — thin and flat where it
        has a glyph, blank where it does not. The supplemental pictographs
        at U+1F300 and up have no text form, so there is nothing to get
        wrong. This is a rule about the *block*, not about taste.
        """
        from cs import ui

        for name, (emoji, _) in ui._MENU_GLYPHS.items():
            for ch in emoji:
                with self.subTest(row=name, ch=hex(ord(ch))):
                    self.assertFalse(
                        0x2100 <= ord(ch) <= 0x2BFF,
                        f"{name}: {ch!r} is a text-presentation symbol",
                    )

    def test_no_icon_leans_on_a_variation_selector(self):
        """U+FE0F was the first attempt at the bug above, and made it worse.

        It only *requests* the emoji form. A terminal may decline, and
        declining can swallow the whole sequence — so the rows that had been
        rendering thin started rendering as nothing at all.
        """
        from cs import ui

        for name, (emoji, _) in ui._MENU_GLYPHS.items():
            with self.subTest(row=name):
                self.assertNotIn("\ufe0f", emoji)
                self.assertNotIn("\ufe0e", emoji)

    def test_an_unknown_row_is_an_error_rather_than_a_blank(self):
        """Returning "" for a typo would reintroduce the missing icon by the
        one route a font can't be blamed for."""
        from cs import ui

        with self.assertRaises(KeyError):
            ui.menu_icon("no-such-row")

    def test_the_plain_markers_stay_distinct(self):
        """They are shapes to learn, not pictures to read, so two rows
        sharing one is the whole of their job undone. This caught 'practice'
        and 'stats' both using %."""
        from cs import ui

        plain = [marker for _, marker in ui._MENU_GLYPHS.values()]
        self.assertEqual(len(set(plain)), len(plain))


class SplitMouseReportTest(unittest.TestCase):
    """A mouse report the terminal delivered in two pieces.

    An SGR report is `ESC [ < b ; x ; y M`. `_sgr_report` waits a few
    milliseconds after the Esc for the '[' and, if it has not arrived, rules
    the Esc a keypress — correct for a bare Esc, wrong for a report split
    across two reads, which is common on the frame after a full-screen
    redraw. The Esc was then already spent, and the remaining ten bytes
    reached the key handler as ordinary printable keys. Every loop in `cs`
    types printable keys into its filter box, so the report was landing
    there as text: `nothing matches '[<'`, on a menu nobody had typed into.
    """

    REPORT = "<0;30;12M"

    def setUp(self):
        from cs import cli

        self._was = cli._SGR_ENABLED
        cli._SGR_ENABLED = True   # the guard only applies with reporting on
        cli._ESC_AT = 0.0
        cli._CSI_OPEN = False

    def tearDown(self):
        from cs import cli

        cli._SGR_ENABLED = self._was
        cli._ESC_AT = 0.0
        cli._CSI_OPEN = False

    def _event(self, screen, key):
        import curses

        from cs import cli

        return cli._mouse_event(screen, curses, key, [0.0, -1], [])

    def test_the_tail_of_a_split_report_is_not_typing(self):
        """The bug in one assertion: after an Esc has been let through as a
        keypress, the '[' behind it must not reach the caller."""
        screen = Screen(keys=[ord(c) for c in self.REPORT])
        self.assertIsNone(self._event(screen, 27))         # ruled a real Esc
        self.assertEqual(self._event(screen, ord("[")), ("ignored", 0, 0))

    def test_it_swallows_the_report_and_stops(self):
        """Consuming too much would eat the keypress behind it — a fix that
        loses a keystroke is not better than the bug it replaces."""
        screen = Screen(keys=[ord(c) for c in self.REPORT] + [ord("z")])
        self._event(screen, 27)
        self._event(screen, ord("["))
        self.assertEqual(screen.getch(), ord("z"))

    def test_a_bracket_typed_later_is_still_a_bracket(self):
        """The guard is armed by an Esc and expires. Nothing a person types
        can fall inside it, and '[' has to stay a character you can search
        for."""
        import time

        from cs import cli

        self._event(Screen(keys=[]), 27)
        cli._ESC_AT = time.monotonic() - (cli._ESC_ORPHAN_SECONDS + 0.1)
        self.assertIsNone(self._event(Screen(keys=[]), ord("[")))

    def test_one_esc_arms_one_tail(self):
        """A second '[' is the user's, not the terminal's."""
        screen = Screen(keys=[ord(c) for c in self.REPORT])
        self._event(screen, 27)
        self._event(screen, ord("["))
        self.assertIsNone(self._event(Screen(keys=[]), ord("[")))

    def test_a_report_split_after_the_bracket_is_not_typing(self):
        """The seam is not always behind the Esc. Here the terminal delivered
        `ESC [` and then paused, so both bytes were spent before the parser
        gave up and the tail arrived as ten printable keys — reported from a
        live terminal as `nothing matches '<64;44;22M'`, a wheel scroll."""
        self._event(Screen(keys=[ord("[")]), 27)
        typed = [ch for ch in "<64;44;22M"
                 if self._event(Screen(keys=[]), ord(ch)) is None]
        self.assertEqual(typed, [])

    def test_an_sgr_motion_report_becomes_a_hover_event(self):
        screen = Screen(keys=[ord(c) for c in "[<35;10;5M"])
        self.assertEqual(self._event(screen, 27), ("move", 9, 4))

    def test_a_report_split_mid_digits_is_not_typing(self):
        """A sequence can break at any byte, so the fix cannot key off which
        byte arrives first — only off the parser saying it stopped mid-CSI."""
        self._event(Screen(keys=[ord(c) for c in "[<6"]), 27)
        typed = [ch for ch in "4;44;22M"
                 if self._event(Screen(keys=[]), ord(ch)) is None]
        self.assertEqual(typed, [])

    def test_a_keystroke_behind_a_split_report_still_arrives(self):
        """Swallowing past the report's final byte would cost a keypress,
        which is a worse bug than the one being fixed."""
        self._event(Screen(keys=[ord("[")]), 27)
        screen = Screen(keys=[ord(c) for c in "64;44;22M"] + [ord("z")])
        self._event(screen, ord("<"))
        self.assertEqual(screen.getch(), ord("z"))

    def test_a_less_than_typed_later_is_still_a_character(self):
        """Mid-CSI widens what counts as a tail to any byte at all, so the
        window is the only thing keeping real typing out of it."""
        import time

        from cs import cli

        self._event(Screen(keys=[ord("[")]), 27)
        cli._ESC_AT = time.monotonic() - (cli._ESC_ORPHAN_SECONDS + 0.1)
        self.assertIsNone(self._event(Screen(keys=[]), ord("<")))

    def test_nothing_is_swallowed_when_the_mouse_is_off(self):
        """No reporting, no reports — so a '[' after Esc is only ever typing."""
        from cs import cli

        self._event(Screen(keys=[]), 27)
        cli._SGR_ENABLED = False
        self.assertIsNone(self._event(Screen(keys=[]), ord("[")))
