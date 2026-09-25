"""Pins, annotations, and the daily AIU budget."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from support import StoreTest


class PinTest(StoreTest):
    def test_pin_appears_in_pins_and_settings(self):
        code, out = self._run("pin", "sess-alpha")
        self.assertEqual(code, 0)
        self.assertIn("pinned", out)
        settings = json.loads(
            (Path(os.environ["CS_CONFIG_HOME"]) / "cs" / "settings.json").read_text()
        )
        self.assertEqual(settings["pins"], ["sess-alpha"])
        code, out = self._run("pins")
        self.assertEqual(code, 0)
        self.assertIn("Pinned", out)
        self.assertIn("portal", out.lower())

    def test_unpin_removes_from_settings(self):
        self._run("pin", "sess-alpha")
        code, out = self._run("unpin", "sess-alpha")
        self.assertEqual(code, 0)
        self.assertIn("unpinned", out)
        settings = json.loads(
            (Path(os.environ["CS_CONFIG_HOME"]) / "cs" / "settings.json").read_text()
        )
        self.assertEqual(settings.get("pins", []), [])
        code, out = self._run("pins")
        self.assertEqual(code, 0)
        self.assertIn("No pinned sessions", out)

    def test_invalid_ref_errors_like_other_commands(self):
        code, err = self._run_err("pin", "does-not-exist")
        self.assertEqual(code, 1)
        self.assertIn("session not found", err)

    def test_pins_empty_state_is_a_clear_sentence(self):
        code, out = self._run("pins")
        self.assertEqual(code, 0)
        self.assertIn("No pinned sessions", out)
        self.assertIn("cs pin", out)


class PinSortTest(StoreTest):
    def test_float_pins_puts_pinned_first_stably(self):
        from cs import ui

        rows = [
            ("sess-empty", "t1", "Empty", None, "/tmp/b", 0, 0),
            ("sess-alpha", "t2", "Portal", "acme/portal", "/tmp/a", 2, 100),
        ]
        # Unpinned: order unchanged.
        self.assertEqual([r[0] for r in ui.float_pins(rows)],
                         ["sess-empty", "sess-alpha"])
        ui.pin_session("sess-alpha")
        floated = ui.float_pins(rows)
        self.assertEqual(floated[0][0], "sess-alpha")
        self.assertEqual(floated[1][0], "sess-empty")

    def test_listing_shows_pin_marker_and_order(self):
        from cs import ui

        ui.pin_session("sess-alpha")
        # Force non-TTY render so we get the plain listing.
        code, out = self._run("all", "--sort", "summary", "--asc")
        self.assertEqual(code, 0)
        # With pins floated, the portal row (pinned) comes before Empty.
        self.assertIn("*", out)  # one-cell pin marker on the pinned row
        portal_at = out.lower().find("portal")
        empty_at = out.lower().find("empty")
        if portal_at >= 0 and empty_at >= 0:
            self.assertLess(portal_at, empty_at)


class AnnotationTest(StoreTest):
    def test_note_and_tags_round_trip(self):
        self._run("note", "sess-alpha", "keep going on charts")
        code, out = self._run("note", "sess-alpha")
        self.assertEqual(code, 0)
        self.assertIn("keep going on charts", out)
        self._run("tag", "sess-alpha", "wip")
        self._run("tag", "sess-alpha", "charts")
        settings = json.loads(
            (Path(os.environ["CS_CONFIG_HOME"]) / "cs" / "settings.json").read_text()
        )
        entry = settings["annotations"]["sess-alpha"]
        self.assertEqual(entry["note"], "keep going on charts")
        self.assertEqual(entry["tags"], ["wip", "charts"])
        self._run("untag", "sess-alpha", "wip")
        settings = json.loads(
            (Path(os.environ["CS_CONFIG_HOME"]) / "cs" / "settings.json").read_text()
        )
        self.assertEqual(settings["annotations"]["sess-alpha"]["tags"], ["charts"])
        self._run("note", "sess-alpha", "")
        settings = json.loads(
            (Path(os.environ["CS_CONFIG_HOME"]) / "cs" / "settings.json").read_text()
        )
        # Note cleared; tags remain.
        self.assertNotIn("note", settings["annotations"]["sess-alpha"])
        self.assertEqual(settings["annotations"]["sess-alpha"]["tags"], ["charts"])


class BudgetTest(StoreTest):
    def test_budget_set_clear_round_trip(self):
        code, out = self._run("budget")
        self.assertEqual(code, 0)
        self.assertIn("No daily budget", out)
        code, out = self._run("budget", "5")
        self.assertEqual(code, 0)
        self.assertIn("5", out)
        settings = json.loads(
            (Path(os.environ["CS_CONFIG_HOME"]) / "cs" / "settings.json").read_text()
        )
        self.assertEqual(settings["budget"]["daily_aiu"], 5.0)
        code, out = self._run("budget")
        self.assertEqual(code, 0)
        self.assertIn("5", out)
        self.assertRegex(out, r"[\d.]+ / 5")
        # Fixture usage is datetime('now'), so last-24h spend is non-zero.
        self.assertIn("AIU", out)
        code, out = self._run("budget", "clear")
        self.assertEqual(code, 0)
        self.assertIn("cleared", out)
        settings = json.loads(
            (Path(os.environ["CS_CONFIG_HOME"]) / "cs" / "settings.json").read_text()
        )
        self.assertNotIn("budget", settings)

    def test_budget_zero_clears(self):
        self._run("budget", "3")
        code, out = self._run("budget", "0")
        self.assertEqual(code, 0)
        self.assertIn("cleared", out)

    def test_home_snapshot_includes_budget_when_set(self):
        from cs import cli, ui

        ui.set_daily_budget(5.0)
        facts, _series = cli._home_snapshot()
        value, label = facts[0][0], facts[0][1]
        self.assertEqual(label, "AIU today")
        self.assertIn("/", value)
        self.assertEqual(len(facts[0]), 3)
        style = facts[0][2]
        self.assertIn(style, ("credits", "warn", "danger"))

    def test_budget_colour_thresholds(self):
        from cs import ui

        self.assertEqual(ui.budget_style_name(3.0, 10.0), "credits")
        self.assertEqual(ui.budget_style_name(7.0, 10.0), "warn")
        self.assertEqual(ui.budget_style_name(10.0, 10.0), "warn")
        self.assertEqual(ui.budget_style_name(10.1, 10.0), "danger")

    def test_invalid_budget_rejected(self):
        code, err = self._run_err("budget", "nope")
        self.assertEqual(code, 1)
        self.assertIn("budget wants a number", err)


class HomePinnedRowTest(StoreTest):
    def test_pinned_is_on_the_find_menu(self):
        from cs import cli

        labels = [label for _, label, *_ in cli._home_items()]
        self.assertIn("Pinned", labels)
        self.assertLess(labels.index("Pinned"), labels.index("Search"))
        self.assertEqual(labels.index("Pinned"), labels.index("Recent sessions") + 1)


if __name__ == "__main__":
    unittest.main()
