"""Terminal UI helpers — colours, tables, boxes, and the splash screen.

Colour is auto-disabled when stdout is not a TTY (pipes, CI, ``TERM=dumb``),
so output stays clean when redirected.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from datetime import date
from pathlib import Path

# ── Colour support ───────────────────────────────────────────────────
_COLOR = sys.stdout.isatty() and os.environ.get("TERM", "") != "dumb"


def _c(code: str) -> str:
    return code if _COLOR else ""


CYAN = _c("\033[36m")
GREEN = _c("\033[32m")
YELLOW = _c("\033[33m")
RED = _c("\033[31m")
MAGENTA = _c("\033[35m")
BOLD = _c("\033[1m")
DIM = _c("\033[2m")
RST = _c("\033[0m")


def c256(number: int) -> str:
    """A 256-colour foreground escape, or nothing when colour is off.

    Every colour `cs` invents goes through here rather than through a
    truecolour escape, for one reason: reports are built as ANSI strings and
    then *replayed inside curses* when opened from the menu, and curses can
    hold a 256-colour pair but cannot hold an arbitrary RGB. A palette the
    reader cannot render is a report that looks like two different tools
    depending on how you opened it.
    """
    return _c(f"\033[38;5;{number}m")


# ── Theme ────────────────────────────────────────────────────────────
def _theme(label: str, description: str, *, bg: int, fg: int, accent: int,
           secondary: int, muted: int, panel: int, cursor: int, good: int,
           warn: int, danger: int, code: int, ramp: tuple[int, ...],
           cursor_fg: int | None = None, slate: int | None = None) -> dict:
    return {
        "label": label, "description": description, "bg": bg, "fg": fg,
        "accent": accent, "secondary": secondary, "muted": muted,
        "panel": panel, "cursor": cursor, "cursor_fg": cursor_fg or fg,
        "good": good, "warn": warn, "danger": danger, "code": code,
        "slate": panel if slate is None else slate, "ramp": ramp,
    }


_THEMES = {
    "dark": _theme(
        "Dark", "Copilot blue on a soft charcoal ground",
        bg=234, fg=253, accent=39, secondary=177, muted=245, panel=240,
        cursor=25, cursor_fg=231, good=148, warn=214, danger=204, code=180,
        slate=239, ramp=(99, 105, 69, 33, 39, 45, 44, 49),
    ),
    "light": _theme(
        "Light", "Crisp ink and blue accents on white",
        bg=255, fg=235, accent=27, secondary=91, muted=240, panel=250,
        cursor=25, cursor_fg=255, good=22, warn=130, danger=160, code=130,
        ramp=(90, 91, 57, 27, 25, 31, 30, 29),
    ),
    "contrast": _theme(
        "High Contrast", "Maximum separation for accessibility",
        bg=16, fg=231, accent=51, secondary=201, muted=250, panel=244,
        cursor=226, cursor_fg=16, good=46, warn=226, danger=196, code=226,
        ramp=(201, 207, 213, 219, 51, 87, 123, 159),
    ),
    "midnight": _theme(
        "Midnight", "Deep navy with electric blue highlights",
        bg=17, fg=189, accent=75, secondary=141, muted=103, panel=24,
        cursor=24, cursor_fg=231, good=84, warn=215, danger=204, code=180,
        ramp=(54, 55, 61, 67, 74, 80, 86, 87),
    ),
    "nord": _theme(
        "Nord", "Arctic blue-grey with calm cool accents",
        bg=236, fg=254, accent=110, secondary=146, muted=109, panel=239,
        cursor=67, cursor_fg=16, good=108, warn=179, danger=167, code=180,
        ramp=(60, 67, 74, 110, 109, 116, 150, 151),
    ),
    "dracula": _theme(
        "Dracula", "Vivid cyan, pink and green on charcoal",
        bg=234, fg=255, accent=117, secondary=212, muted=246, panel=238,
        cursor=61, cursor_fg=231, good=84, warn=228, danger=203, code=180,
        ramp=(141, 177, 212, 117, 81, 84, 120, 121),
    ),
    "solarized-dark": _theme(
        "Solarized Dark", "Balanced low-contrast colours for long sessions",
        bg=234, fg=254, accent=33, secondary=175, muted=244, panel=237,
        cursor=37, cursor_fg=234, good=64, warn=136, danger=160, code=166,
        ramp=(125, 61, 33, 37, 36, 64, 70, 100),
    ),
    "solarized-light": _theme(
        "Solarized Light", "Warm paper with measured blue and cyan",
        bg=230, fg=238, accent=32, secondary=125, muted=244, panel=187,
        cursor=37, cursor_fg=234, good=64, warn=136, danger=160, code=166,
        ramp=(125, 61, 33, 37, 36, 64, 70, 100),
    ),
    "gruvbox": _theme(
        "Gruvbox", "Retro warm contrast with earthy accents",
        bg=235, fg=223, accent=214, secondary=175, muted=246, panel=239,
        cursor=172, cursor_fg=235, good=142, warn=214, danger=167, code=208,
        ramp=(175, 174, 208, 214, 142, 108, 109, 110),
    ),
    "monokai": _theme(
        "Monokai", "Punchy pink, cyan and lime on graphite",
        bg=234, fg=231, accent=81, secondary=204, muted=245, panel=238,
        cursor=197, cursor_fg=16, good=148, warn=221, danger=197, code=186,
        ramp=(135, 141, 197, 203, 81, 80, 86, 148),
    ),
    "tokyo-night": _theme(
        "Tokyo Night", "Muted indigo with luminous blue and green",
        bg=17, fg=189, accent=75, secondary=141, muted=103, panel=24,
        cursor=61, cursor_fg=231, good=114, warn=179, danger=203, code=117,
        ramp=(60, 61, 68, 75, 81, 87, 114, 120),
    ),
    "catppuccin": _theme(
        "Catppuccin", "Soft lavender and blue with pastel warmth",
        bg=235, fg=189, accent=111, secondary=183, muted=146, panel=239,
        cursor=60, cursor_fg=255, good=151, warn=223, danger=210, code=180,
        ramp=(139, 147, 183, 111, 117, 123, 151, 158),
    ),
    "one-dark": _theme(
        "One Dark", "Balanced editor greys with clear blue focus",
        bg=235, fg=188, accent=75, secondary=176, muted=102, panel=238,
        cursor=60, cursor_fg=255, good=114, warn=180, danger=168, code=180,
        ramp=(97, 104, 176, 75, 74, 73, 114, 120),
    ),
    "material-ocean": _theme(
        "Material Ocean", "Deep ocean panels with cyan and violet",
        bg=17, fg=195, accent=81, secondary=141, muted=103, panel=24,
        cursor=31, cursor_fg=16, good=84, warn=221, danger=204, code=180,
        ramp=(55, 61, 98, 75, 81, 87, 84, 121),
    ),
    "ayu-dark": _theme(
        "Ayu Dark", "Warm amber detail on restrained charcoal",
        bg=234, fg=252, accent=215, secondary=180, muted=244, panel=237,
        cursor=94, cursor_fg=231, good=114, warn=215, danger=203, code=180,
        ramp=(95, 131, 167, 173, 179, 143, 108, 114),
    ),
    "everforest": _theme(
        "Everforest", "Low-glare forest greens and warm neutrals",
        bg=235, fg=223, accent=108, secondary=175, muted=246, panel=239,
        cursor=65, cursor_fg=16, good=108, warn=179, danger=167, code=180,
        ramp=(95, 101, 107, 108, 109, 115, 151, 187),
    ),
    "kanagawa": _theme(
        "Kanagawa", "Ink-dark Japanese tones with wave blue",
        bg=234, fg=223, accent=109, secondary=175, muted=245, panel=238,
        cursor=60, cursor_fg=231, good=108, warn=179, danger=167, code=180,
        ramp=(96, 97, 103, 109, 110, 116, 152, 188),
    ),
    "rose-pine": _theme(
        "Rosé Pine", "Muted rose, iris and foam on deep navy",
        bg=17, fg=189, accent=110, secondary=182, muted=103, panel=24,
        cursor=60, cursor_fg=231, good=108, warn=180, danger=174, code=181,
        ramp=(96, 132, 168, 174, 181, 110, 116, 152),
    ),
    "synthwave": _theme(
        "Synthwave", "Neon magenta and cyan with arcade energy",
        bg=17, fg=231, accent=51, secondary=201, muted=146, panel=53,
        cursor=201, cursor_fg=17, good=119, warn=227, danger=198, code=213,
        ramp=(129, 165, 201, 207, 51, 87, 123, 159),
    ),
    "cyberpunk": _theme(
        "Cyberpunk", "Electric yellow and cyan on absolute black",
        bg=16, fg=231, accent=226, secondary=201, muted=250, panel=238,
        cursor=51, cursor_fg=16, good=46, warn=226, danger=196, code=201,
        ramp=(201, 207, 213, 219, 226, 190, 51, 87),
    ),
}
THEMES = tuple(_THEMES)
_THEME_ALIASES = {
    "high-contrast": "contrast", "high_contrast": "contrast",
    "solarized": "solarized-dark", "tokyo": "tokyo-night",
}
_RAMPS = {name: theme["ramp"] for name, theme in _THEMES.items()}


def _tui_palette(name: str) -> dict[str, tuple[int, int]]:
    theme = _THEMES[name]
    bg, fg = theme["bg"], theme["fg"]
    return {
        "background": (fg, bg),
        "title": (theme["accent"], bg),
        "help": (theme["muted"], bg),
        "selected": (theme["accent"], bg),
        "cursor": (theme["cursor_fg"], theme["cursor"]),
        "header": (fg, bg),
        "separator": (theme["panel"], bg),
        "number": (theme["muted"], bg),
        "active": (theme["good"], bg),
        "turns": (theme["ramp"][-1], bg),
        "credits": (theme["secondary"], bg),
        "summary": (fg, bg),
        "repo": (theme["ramp"][2], bg),
        "status": ((theme["cursor_fg"], theme["cursor"])
                   if name == "contrast" else (fg, theme["panel"])),
        "warn": (theme["warn"], bg),
        "danger": (theme["danger"], bg),
        "label": (fg, bg),
    }


_TUI_PALETTES = {name: _tui_palette(name) for name in THEMES}


def _normalise_theme(name: str | None) -> str:
    chosen = (name or "dark").strip().lower()
    if chosen in THEMES or chosen in _THEME_ALIASES:
        return _THEME_ALIASES.get(chosen, chosen)
    return "dark"


# ── Remembered choices ───────────────────────────────────────────────
# Choices that have to outlive the process live here: the theme, pinned
# sessions, annotations, and a daily AIU budget. Everything else on the
# landing screen is a view you open and close. Never write into
# COPILOT_HOME — that store is opened read-only.


def settings_path() -> Path:
    """Where remembered choices (theme, pins, budget) are kept.

    Never inside COPILOT_HOME: that is Copilot's own store, opened
    read-only, and cs has no business writing next to it.
    """
    home = os.environ.get("CS_CONFIG_HOME") or os.environ.get("XDG_CONFIG_HOME")
    base = Path(home) if home else Path.home() / ".config"
    return base / "cs" / "settings.json"


def _load_settings() -> dict:
    """The stored choices. An unreadable or malformed file simply has none."""
    try:
        with open(settings_path(), encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return {}
    return stored if isinstance(stored, dict) else {}


def saved_theme() -> str | None:
    """The theme last applied from the picker, or None if there isn't one.

    None rather than "dark" for a name that no longer exists, so a theme
    retired between releases falls back to the default instead of pinning
    the file's stale answer over it.
    """
    name = _load_settings().get("theme")
    if not isinstance(name, str):
        return None
    chosen = name.strip().lower()
    chosen = _THEME_ALIASES.get(chosen, chosen)
    return chosen if chosen in THEMES else None


def _save_settings(settings: dict) -> bool:
    """Write the settings dict atomically. False when it could not be written.

    Written to a neighbouring file and renamed over the target, so an
    interrupted write leaves the previous choices intact rather than a
    half-written file that reads as no choice at all. Never touches
    COPILOT_HOME — only the config home beside this file.
    """
    path = settings_path()
    partial = path.with_name(path.name + ".partial")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(partial, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2)
            handle.write("\n")
        os.replace(partial, path)
    except OSError:
        try:
            partial.unlink()
        except OSError:
            pass
        return False
    return True


def save_theme(name: str) -> bool:
    """Remember `name` for the next run. False when it could not be written."""
    settings = _load_settings()
    settings["theme"] = _normalise_theme(name)
    return _save_settings(settings)


def pinned_ids() -> list[str]:
    """Session ids the user has pinned, in pin order. Missing key → []."""
    pins = _load_settings().get("pins")
    if not isinstance(pins, list):
        return []
    return [sid for sid in pins if isinstance(sid, str) and sid]


def is_pinned(session_id: str) -> bool:
    return session_id in pinned_ids()


def pin_session(session_id: str) -> bool:
    """Remember a session as pinned. Idempotent; False on write failure."""
    settings = _load_settings()
    pins = settings.get("pins")
    if not isinstance(pins, list):
        pins = []
    pins = [sid for sid in pins if isinstance(sid, str) and sid]
    if session_id not in pins:
        pins.append(session_id)
    settings["pins"] = pins
    return _save_settings(settings)


def unpin_session(session_id: str) -> bool:
    """Drop a pin. Idempotent when it was not pinned."""
    settings = _load_settings()
    pins = settings.get("pins")
    if not isinstance(pins, list):
        pins = []
    settings["pins"] = [sid for sid in pins if isinstance(sid, str) and sid != session_id]
    return _save_settings(settings)


def toggle_pin(session_id: str) -> bool:
    """Pin if unpinned, unpin if pinned. Returns the new pinned state."""
    if is_pinned(session_id):
        unpin_session(session_id)
        return False
    pin_session(session_id)
    return True


def _annotations_map(settings: dict | None = None) -> dict:
    raw = (settings if settings is not None else _load_settings()).get("annotations")
    return raw if isinstance(raw, dict) else {}


def annotation(session_id: str) -> dict:
    """Tags and note for a session. Tolerant of missing or malformed keys."""
    entry = _annotations_map().get(session_id)
    if not isinstance(entry, dict):
        return {"tags": [], "note": ""}
    tags = entry.get("tags")
    note = entry.get("note")
    return {
        "tags": [t for t in tags if isinstance(t, str) and t] if isinstance(tags, list) else [],
        "note": note if isinstance(note, str) else "",
    }


def set_note(session_id: str, note: str) -> bool:
    """Set or clear the note on a session (empty string clears the note)."""
    settings = _load_settings()
    anns = dict(_annotations_map(settings))
    entry = dict(anns.get(session_id) if isinstance(anns.get(session_id), dict) else {})
    tags = entry.get("tags") if isinstance(entry.get("tags"), list) else []
    tags = [t for t in tags if isinstance(t, str) and t]
    note = note.strip()
    if not note and not tags:
        anns.pop(session_id, None)
    else:
        entry = {"tags": tags}
        if note:
            entry["note"] = note
        anns[session_id] = entry
    if anns:
        settings["annotations"] = anns
    else:
        settings.pop("annotations", None)
    return _save_settings(settings)


def add_tag(session_id: str, tag: str) -> bool:
    """Attach a tag to a session. Empty tag is ignored."""
    tag = tag.strip()
    if not tag:
        return True
    settings = _load_settings()
    anns = dict(_annotations_map(settings))
    entry = dict(anns.get(session_id) if isinstance(anns.get(session_id), dict) else {})
    tags = entry.get("tags") if isinstance(entry.get("tags"), list) else []
    tags = [t for t in tags if isinstance(t, str) and t]
    if tag not in tags:
        tags.append(tag)
    entry["tags"] = tags
    note = entry.get("note")
    if isinstance(note, str) and note.strip():
        entry["note"] = note.strip()
    else:
        entry.pop("note", None)
    anns[session_id] = entry
    settings["annotations"] = anns
    return _save_settings(settings)


def remove_tag(session_id: str, tag: str) -> bool:
    """Drop a tag from a session."""
    settings = _load_settings()
    anns = dict(_annotations_map(settings))
    entry = anns.get(session_id)
    if not isinstance(entry, dict):
        return True
    tags = entry.get("tags") if isinstance(entry.get("tags"), list) else []
    tags = [t for t in tags if isinstance(t, str) and t and t != tag]
    note = entry.get("note") if isinstance(entry.get("note"), str) else ""
    note = note.strip()
    if not tags and not note:
        anns.pop(session_id, None)
    else:
        kept = {"tags": tags}
        if note:
            kept["note"] = note
        anns[session_id] = kept
    if anns:
        settings["annotations"] = anns
    else:
        settings.pop("annotations", None)
    return _save_settings(settings)


def saved_searches() -> dict[str, str]:
    """Named searches, in the order they were saved. Malformed entries dropped."""
    saved = _load_settings().get("saved_searches")
    if not isinstance(saved, dict):
        return {}
    return {name: term for name, term in saved.items()
            if isinstance(name, str) and name.strip()
            and isinstance(term, str) and term.strip()}


def save_search(name: str, term: str) -> bool:
    """Remember `term` under `name`, replacing one of the same name."""
    settings = _load_settings()
    saved = saved_searches()
    saved[name.strip()] = term.strip()
    settings["saved_searches"] = saved
    return _save_settings(settings)


def rollup_salt() -> str:
    """The salt `cs rollup` hashes repository names with, made once and kept.

    Random and local: the same repository hashes the same way every time you
    run a rollup, so two weeks can be compared, but nobody holding only the
    rollup can recover the name. If the settings cannot be written, a fresh
    salt is used for this run only.
    """
    import secrets

    settings = _load_settings()
    salt = settings.get("rollup_salt")
    if isinstance(salt, str) and len(salt) >= 16:
        return salt
    salt = secrets.token_hex(16)
    settings["rollup_salt"] = salt
    _save_settings(settings)
    return salt


# The steps ←/→ walk on the home screen's Budget row. 0 is "no limit".
BUDGET_STEPS = (0, 5, 10, 20, 25, 50, 75, 100, 150, 200, 300, 500, 1000)


def step_budget(delta: int) -> float | None:
    """Move the daily budget one step along BUDGET_STEPS and save it.

    A budget between two steps moves to the next one in that direction, so
    a hand-set 42 goes to 50 on → and 25 on ←. Returns the new limit, or
    None when it is now off.
    """
    current = daily_budget_aiu() or 0
    if delta > 0:
        chosen = next((step for step in BUDGET_STEPS if step > current),
                      BUDGET_STEPS[-1])
    else:
        chosen = next((step for step in reversed(BUDGET_STEPS) if step < current),
                      0)
    set_daily_budget(chosen or None)
    return daily_budget_aiu()


def daily_budget_aiu() -> float | None:
    """User-facing daily AIU budget, or None when unset / cleared."""
    budget = _load_settings().get("budget")
    if not isinstance(budget, dict):
        return None
    value = budget.get("daily_aiu")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def set_daily_budget(aiu: float | None) -> bool:
    """Set the daily AIU budget. None or <= 0 clears it."""
    settings = _load_settings()
    if aiu is None or aiu <= 0:
        settings.pop("budget", None)
    else:
        settings["budget"] = {"daily_aiu": float(aiu)}
    return _save_settings(settings)


def budget_colour(spent_aiu: float, limit_aiu: float) -> str:
    """ANSI colour for spend-vs-budget: normal, amber (70–100%), rose (over)."""
    if limit_aiu <= 0:
        return ""
    ratio = spent_aiu / limit_aiu
    if ratio > 1.0:
        return ROSE
    if ratio >= 0.7:
        return AMBER
    return ""


def budget_style_name(spent_aiu: float, limit_aiu: float) -> str:
    """Curses theme key for the same thresholds as `budget_colour`."""
    if limit_aiu <= 0:
        return "credits"
    ratio = spent_aiu / limit_aiu
    if ratio > 1.0:
        return "danger"
    if ratio >= 0.7:
        return "warn"
    return "credits"


# CS_THEME stays an explicit override for the run it is set on. Without it,
# the theme you last applied from the picker is the one you come back to.
_THEME = _normalise_theme(os.environ.get("CS_THEME") or saved_theme())
_RAMP = _RAMPS[_THEME]


def theme_name() -> str:
    """The active theme name."""
    return _THEME


def theme_label(name: str | None = None) -> str:
    """The display name for a theme."""
    return _THEMES[_normalise_theme(name or _THEME)]["label"]


def theme_description(name: str) -> str:
    """The short description shown in the theme picker."""
    return _THEMES[_normalise_theme(name)]["description"]


def next_theme(name: str | None = None) -> str:
    """The theme after `name`, wrapping back to dark."""
    current = _normalise_theme(name or _THEME)
    return THEMES[(THEMES.index(current) + 1) % len(THEMES)]


def tui_theme(curses, name: str | None = None) -> dict[str, int]:
    """Create the curses theme, falling back to attributes on limited terminals."""
    fallback = {
        "background": 0,
        "title": curses.A_BOLD,
        "help": curses.A_DIM,
        "selected": curses.A_REVERSE | curses.A_BOLD,
        "cursor": curses.A_REVERSE,
        "header": curses.A_BOLD,
        "separator": curses.A_DIM,
        "number": curses.A_DIM,
        "active": 0,
        "turns": 0,
        "credits": curses.A_BOLD,
        "summary": 0,
        "repo": curses.A_DIM,
        "status": curses.A_DIM,
        "warn": curses.A_BOLD,
        "danger": curses.A_BOLD,
        "label": curses.A_BOLD,
    }
    try:
        curses.start_color()
        try:
            curses.use_default_colors()
            default_bg = -1
        except curses.error:
            default_bg = curses.COLOR_BLACK

        if curses.COLORS >= 256:
            palette = _TUI_PALETTES[_normalise_theme(name or _THEME)]
        else:
            palette = {
                "background": (curses.COLOR_WHITE, default_bg),
                "title": (curses.COLOR_BLUE, default_bg),
                "help": (curses.COLOR_WHITE, default_bg),
                "selected": (curses.COLOR_GREEN, curses.COLOR_BLUE),
                "cursor": (curses.COLOR_WHITE, curses.COLOR_BLUE),
                "header": (curses.COLOR_CYAN, default_bg),
                "separator": (curses.COLOR_BLUE, default_bg),
                "number": (curses.COLOR_WHITE, default_bg),
                "active": (curses.COLOR_CYAN, default_bg),
                "turns": (curses.COLOR_GREEN, default_bg),
                "credits": (curses.COLOR_MAGENTA, default_bg),
                "summary": (curses.COLOR_WHITE, default_bg),
                "repo": (curses.COLOR_BLUE, default_bg),
                "status": (curses.COLOR_YELLOW, default_bg),
                "warn": (curses.COLOR_YELLOW, default_bg),
                "danger": (curses.COLOR_RED, default_bg),
                "label": (curses.COLOR_WHITE, default_bg),
            }

        styles = {}
        for pair, (name, colors) in enumerate(palette.items(), 1):
            curses.init_pair(pair, *colors)
            styles[name] = curses.color_pair(pair)
        styles["title"] |= curses.A_BOLD
        styles["selected"] |= curses.A_BOLD
        styles["cursor"] |= curses.A_BOLD
        styles["header"] |= curses.A_BOLD
        styles["credits"] |= curses.A_BOLD
        styles["status"] |= curses.A_BOLD
        styles["warn"] |= curses.A_BOLD
        styles["danger"] |= curses.A_BOLD
        styles["label"] |= curses.A_BOLD
        return styles
    except curses.error:
        return fallback


# ── ANSI text inside curses ──────────────────────────────────────────
_SGR = re.compile(r"\x1b\[([0-9;]*)m")

# The reports are built as ANSI-coloured strings for the plain terminal.
# Showing them in a curses window means translating those codes, and this is
# the whole set they emit — measured, not guessed.
_SGR_COLORS = {"31": 1, "32": 2, "33": 3, "35": 5, "36": 6}


def sgr_palette(curses) -> dict[str, int]:
    """ANSI codes the reports emit, mapped to curses attributes.

    Every index in `PALETTE_256` gets a pair, so a report looks the same
    opened from the menu as it does piped to the shell. It used to carry two
    of them by hand, and every colour added after that quietly came out as
    plain text in the full-screen reader — the one place a long report is
    actually read.

    Pairs start at 20 to stay clear of ``tui_theme``'s 1-17.
    """
    palette = {"1": curses.A_BOLD, "2": curses.A_DIM, "7": curses.A_REVERSE}
    try:
        curses.start_color()
        try:
            curses.use_default_colors()
            background = -1
        except curses.error:
            background = curses.COLOR_BLACK
        if curses.COLORS >= 256:
            background = _THEMES[_THEME]["bg"]
        pair = 20
        for code, colour in _SGR_COLORS.items():
            curses.init_pair(pair, colour, background)
            palette[code] = curses.color_pair(pair)
            pair += 1
        if curses.COLORS >= 256:
            for colour in PALETTE_256:
                if pair >= min(getattr(curses, "COLOR_PAIRS", 64), 200):
                    break
                curses.init_pair(pair, colour, background)
                palette[f"38;5;{colour}"] = curses.color_pair(pair)
                pair += 1
    except curses.error:
        pass  # monochrome terminal: bold and dim still work
    return palette


# ── The wordmark's gradient ──────────────────────────────────────────
# The splash prints the wordmark in the Copilot purple→cyan with truecolour
# escapes. The curses landing screen could not, so it drew the same art in one
# flat blue and the two looked like different products. This is that gradient
# in 256-colour terms, which curses can hold as colour pairs.

_BANNER_RAMP = list(_RAMP)

# The reveal is a wipe, not a loop: the wordmark is drawn a few columns at a
# time when the screen first opens and is then still for as long as you are
# on it. Motion on arrival says the tool is awake; motion while you are
# reading says nothing and never stops saying it.
REVEAL_MS = 45
REVEAL_FRAMES = 14
# Columns each banner row trails the one above it, so the wipe's edge is a
# slant rather than a wall — light crossing the letters instead of a shutter.
REVEAL_LAG = 2


def reveal_columns(frame: int, span: int) -> int:
    """How much of the wipe is drawn at `frame`, of `span` columns.

    Eased so the wipe arrives rather than stops. A constant number of columns
    per frame reads as a machine drawing; decelerating into place reads as
    something settling. The exponent is 1.5 rather than the usual 2 because
    squaring leaves the last three frames moving one column between them,
    which looks less like an ending than like a stall.
    """
    if span <= 0:
        return 0
    if frame >= REVEAL_FRAMES:
        return span
    return round(span * (1 - (1 - frame / REVEAL_FRAMES) ** 1.5))


# The menu follows the wordmark down rather than appearing under a finished
# one. It starts a third of the way into the wipe: cascading from frame zero
# races the banner and reads as two animations, while waiting for the wipe to
# finish reads as a pause. Overlapping them makes it one gesture.
REVEAL_MENU_START = REVEAL_FRAMES // 3


def reveal_rows(frame: int, count: int) -> int:
    """How many menu rows are drawn at `frame`, of `count`.

    Shares the wipe's easing so the two halves of the landing screen settle
    on the same curve. Returns `count` unchanged once the frames are spent,
    which is what makes every caller safe to run past the end of the reveal
    and on a terminal that never animated at all.
    """
    if count <= 0:
        return 0
    if frame >= REVEAL_FRAMES:
        return count
    run = REVEAL_FRAMES - REVEAL_MENU_START
    progress = max(0.0, (frame - REVEAL_MENU_START) / run)
    return min(count, round(count * (1 - (1 - progress) ** 1.5)))


# The agent paces the rule under the header while the screen is idle, then
# stops. The wipe's comment above still holds — motion you are not meant to
# watch should not run forever — so this is a walk with an end to it rather
# than a spinner: it is company on arrival, not a thing blinking at you while
# you read, and a terminal left open on the menu goes quiet and stays quiet.
PACE_MS = 90
PACE_FRAMES = 220


def pace_column(frame: int, span: int) -> int:
    """Where the agent stands at `frame`, bouncing across `span` columns.

    A bounce rather than a wrap: something that walks off one edge and
    reappears at the other reads as a glitch, while something that turns
    round at the wall reads as pacing.
    """
    if span <= 1:
        return 0
    leg = span - 1
    at = frame % (2 * leg)
    return at if at <= leg else 2 * leg - at


def banner_palette(curses) -> list[int]:
    """The gradient as curses attributes, purple first.

    Empty on a terminal without 256 colours: there is no honest way to show
    this ramp in eight, and the wordmark is better flat than wrong.
    """
    try:
        curses.start_color()
        if curses.COLORS < 256:
            return []
        background = _THEMES[_THEME]["bg"]
        attributes = []
        for offset, colour in enumerate(_BANNER_RAMP):
            # The banner and report reader never draw together; each rebuilds
            # its pairs on entry. tui_theme owns 1-17 in both views.
            curses.init_pair(60 + offset, colour, background)
            attributes.append(curses.color_pair(60 + offset) | curses.A_BOLD)
        return attributes
    except curses.error:
        return []


# Eight heights, and a blank for nothing at all. A day with no sessions gets
# no bar rather than the shortest one: "quiet" and "barely busy" are
# different answers, and a sparkline that cannot tell them apart is decoration.
_SPARKS = " ▁▂▃▄▅▆▇█"


def sparkline(values: list[int]) -> str:
    """A row of block characters, scaled to the busiest value in the series.

    Scaled to the maximum rather than to a fixed ceiling: the question this
    answers is "what shape has my work been", and that shape is relative.
    """
    if not values:
        return ""
    peak = max(values)
    if peak <= 0:
        return " " * len(values)
    return "".join(
        _SPARKS[0] if value <= 0
        else _SPARKS[max(1, round(value / peak * (len(_SPARKS) - 1)))]
        for value in values
    )


def gradient_runs(text: str, colours: int) -> list[tuple[int, str, int]]:
    """Split a line into (column, run, colour) bands, purple left to cyan right.

    One write per band rather than per character — the same picture for a
    tenth of the calls, which matters while the wipe is running.
    """
    if not text or colours < 1:
        return []
    runs: list[tuple[int, str, int]] = []
    start = 0
    for column in range(1, len(text) + 1):
        here = start * colours // len(text)
        if column == len(text) or column * colours // len(text) != here:
            runs.append((start, text[start:column], here))
            start = column
    return runs


def sgr_runs(line: str, palette: dict[str, int]) -> list[tuple[str, int]]:
    """Split an ANSI-coloured line into (text, curses attribute) runs."""
    runs: list[tuple[str, int]] = []
    attr, pos = 0, 0
    for match in _SGR.finditer(line):
        if match.start() > pos:
            runs.append((line[pos:match.start()], attr))
        code = match.group(1)
        if code in palette:
            attr |= palette[code]
        else:
            for part in code.split(";"):
                if part in ("", "0"):
                    attr = 0
                else:
                    attr |= palette.get(part, 0)
        pos = match.end()
    if pos < len(line):
        runs.append((line[pos:], attr))
    return runs


# What a wrapped line repeats at the start of each continuation row: its
# indent, and the bars that mark a quote or a block of tool output.
_GUTTER = " ▎│┃|>"


def wrap_runs(
    runs: list[tuple[str, int]], width: int, *, starts: list[int] | None = None
) -> list[list[tuple[str, int]]]:
    """One line's (text, attribute) runs as the rows a `width`-wide window holds.

    Cut at the cell, the way less wraps, not at a word: what runs past the
    edge is nearly always a table or a block of tool output, where a word
    boundary means nothing and every column does. Each continuation row
    repeats the line's gutter — its indent and quote bars — so a wrapped
    block still reads as one block. A gutter wider than half the window is
    not repeated; there would be no room left for what it holds. `starts`,
    when supplied, receives each row's character offset in the source line.
    """
    limit = max(1, width - 1)
    if sum(cells(text) for text, _ in runs) <= limit:
        if starts is not None:
            starts.append(0)
        return [runs]
    plain = "".join(text for text, _ in runs)
    depth = len(plain) - len(plain.lstrip(_GUTTER))
    span = cells(plain[:depth])
    gutter = _take_cells(runs, span)[0] if depth and span * 2 <= limit else []
    indent = sum(cells(text) for text, _ in gutter)
    rows, rest, room = [], runs, limit
    consumed = 0
    while rest:
        if starts is not None:
            starts.append(consumed)
        head, rest = _take_cells(rest, room)
        consumed += sum(len(text) for text, _ in head)
        rows.append(head if not rows else [*gutter, *head])
        room = limit - indent
    return rows


def _take_cells(
    runs: list[tuple[str, int]], room: int
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Split runs after `room` cells: (what fits, the rest).

    Always takes at least one character, so a wrap can never stall on a
    wide character that is broader than the room left for it.
    """
    head: list[tuple[str, int]] = []
    used = 0
    for at, (text, attr) in enumerate(runs):
        cut = 0
        for ch in text:
            size = cells(ch)
            if used + size > room and (head or cut):
                break
            used += size
            cut += 1
        if cut < len(text):
            if cut:
                head.append((text[:cut], attr))
            rest = [(text[cut:], attr), *runs[at + 1:]]
            return head, [run for run in rest if run[0]]
        head.append((text, attr))
    return head, []


# ── Copilot gradient (purple → cyan) ─────────────────────────────────
GRADIENT = [
    (139, 92, 246),
    (124, 108, 252),
    (99, 132, 255),
    (59, 160, 255),
    (0, 188, 255),
    (0, 210, 240),
    (0, 228, 220),
    (0, 245, 200),
]


def trunc(s: str, n: int) -> str:
    """Truncate a string to n columns with an ellipsis.

    Columns, not characters: a title with an emoji or CJK in it is wider on
    screen than it is long, and cutting it by character count let it run
    into the column beside it.
    """
    return _fit(s, n)


def fmt_aiu(nano: int | None) -> str:
    """Format nano-AIU spend as compact AI credits ('-' when none)."""
    aiu = (nano or 0) / 1e9
    if aiu <= 0:
        return "-"
    if aiu >= 1000:
        return f"{aiu / 1000:.1f}k"
    if aiu >= 100:
        return f"{aiu:.0f}"
    if aiu >= 10:
        return f"{aiu:.1f}"
    return f"{aiu:.2f}"


def friendly_day(day: str) -> str:
    """Turn an ISO date into 'Today', 'Yesterday', a weekday, or a full date."""
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return day
    diff = (date.today() - d).days
    weekday = d.strftime("%A")
    if diff == 0:
        return f"Today · {weekday} {day}"
    if diff == 1:
        return f"Yesterday · {weekday} {day}"
    if 0 < diff < 7:
        return f"{weekday} {day}"
    return d.strftime("%a %d %b %Y")


def gradient_text(text: str) -> str:
    if not _COLOR:
        return text
    out = []
    n = len(GRADIENT)
    for i, ch in enumerate(text):
        if ch == " ":
            out.append(ch)
        else:
            idx = int(i / max(len(text) - 1, 1) * (n - 1))
            r, g, b = GRADIENT[min(idx, n - 1)]
            out.append(f"\033[38;2;{r};{g};{b}m{ch}")
    out.append(RST)
    return "".join(out)


# ── Presentation primitives ──────────────────────────────────────────
# Shared by every long-form view so brief, show and read read as one product
# rather than three scripts: same rules, same gutters, same accents.

# One palette, and every view draws from it. Before this there were five
# colours and a grey, so a bar chart, a risk verdict and a file path were all
# the same shade of nothing — the screen had no way of saying which of the
# things on it mattered. These are 256-colour indices on the Copilot
# purple→cyan axis, with warm tones reserved for the two things that are
# genuinely warnings.
#
# The rule the whole palette obeys: hue carries *meaning*, brightness carries
# *emphasis*. Anything cool is information, anything warm is a finding, and
# grey is furniture. A view that needs a sixth colour needs a rethink instead.
#
# Tuned for a dark terminal, which is what a terminal overwhelmingly is. The
# first version was picked on a light background and every colour in it was a
# pastel — #87afff for the accent, #af87ff for spend — because on white a
# pastel is the only thing that stays legible. Replayed on black those same
# values have almost no chroma left: the whole product came out a washed pale
# blue, and a palette where nothing is saturated is a palette where nothing
# can be emphasised.
#
_REPORT_THEMES = {
    name: {
        "ACCENT": theme["accent"],
        "MUTED": theme["muted"],
        "CODE": theme["code"],
        "PAPER": theme["fg"],
        "VIOLET": theme["secondary"],
        "INDIGO": theme["ramp"][0],
        "SKY": theme["ramp"][2],
        "AZURE": theme["ramp"][4],
        "TEAL": theme["ramp"][6],
        "MINT": theme["ramp"][-1],
        "LIME": theme["good"],
        "AMBER": theme["warn"],
        "ORANGE": theme["warn"],
        "ROSE": theme["danger"],
        "SLATE": theme["slate"],
    }
    for name, theme in _THEMES.items()
}

ACCENT = MUTED = CODE = PAPER = ""
VIOLET = INDIGO = SKY = AZURE = TEAL = MINT = ""
LIME = AMBER = ORANGE = ROSE = SLATE = ""


def _apply_report_theme(name: str) -> None:
    globals().update({
        key: c256(colour) for key, colour in _REPORT_THEMES[name].items()
    })


_apply_report_theme(_THEME)
GUTTER = "  "

# The gradient bars sweep along this — the same eight steps as the wordmark,
# so a bar and the logo above it are visibly the same object. Short bars stay
# at the purple end rather than compressing the whole ramp into four cells:
# colour here means "how far along", and a two-cell bar has not gone far.
_BAR_RAMP = _RAMP


def set_theme(name: str) -> str:
    """Switch the active theme for this process and return its canonical name."""
    global _THEME, _RAMP, _BANNER_RAMP, _BAR_RAMP
    _THEME = _normalise_theme(name)
    _RAMP = _RAMPS[_THEME]
    _BANNER_RAMP = list(_RAMP)
    _BAR_RAMP = _RAMP
    _apply_report_theme(_THEME)
    if "_MARKS" in globals():
        _MARKS["\x02"] = CODE
    return _THEME


# Eighths of a cell. A bar that rounds down to nothing tells you a row scored
# zero when it scored one, which is the one thing a chart must never do — so
# the last cell is drawn as a partial block instead of dropped.
_EIGHTHS = "▏▎▍▌▋▊▉█"

# Every 256-colour index this module can emit. Declared rather than
# discovered because the curses reader has to allocate a colour pair for each
# one up front, and a colour that was not declared silently renders as plain
# text in the one view that shows reports full-screen.
#
# Every theme is declared, not just the active one: the set is cheap, and a
# reader that only knew about the running theme would render a saved report
# wrong the moment someone changed CS_THEME between writing and reading it.
PALETTE_256 = sorted({
    *(colour for palette in _REPORT_THEMES.values() for colour in palette.values()),
    *(colour for ramp in _RAMPS.values() for colour in ramp),
})


def rule(width: int, title: str = "", colour: str = "", note: str = "") -> str:
    """A full-width rule, optionally titled: ── Title ──────── note ──.

    The dashes are furniture and are drawn as furniture. Every one of them
    used to be in the accent colour, which on a page with six sections meant
    six full-width coloured lines competing with the words between them —
    the loudest thing on screen was the thing carrying the least. The accent
    survives as the two leading dashes, which is enough to mark the edge.

    `note` rides the right-hand end instead of taking a line of its own.
    Metadata about the section a rule opens — a timestamp, a size — was
    printed underneath it, which put a lone fragment of grey between the
    label and the thing it labels. A rule whose two ends are "what this is"
    and "how big it is" is one line you can skim; two lines are two.
    """
    colour = colour or ACCENT
    # Reserve the note's own columns before anything else is measured, so a
    # long title shortens rather than pushing the note off the right edge.
    tail = f" {MUTED}{note}{RST} {SLATE}──{RST}" if note else ""
    room = max(width - (cells(_strip(note)) + 4 if note else 0), 1)
    if not title:
        return f"{GUTTER}{SLATE}{'─' * room}{RST}{tail}"
    title = _fit(title, max(room - 5, 1))
    dashes = max(room - cells(_strip(title)) - 4, 0)
    return (f"{GUTTER}{colour}──{RST} {BOLD}{title}{RST} "
            f"{SLATE}{'─' * dashes}{RST}{tail}")


# ── Who is speaking ──────────────────────────────────────────────────
# A transcript is a conversation, and the one thing it has to make obvious at
# a glance is who is talking. "You" and "Copilot" were set in the same bold
# type and differed only by the word, so finding your own last question in a
# fifty-turn session meant reading rather than glancing.
#
# Emoji rather than a font glyph: GitHub's Copilot mark lives in Nerd Fonts,
# and a terminal without one draws a hollow box exactly where the speaker
# should be — worse than no mark at all. These render anywhere that renders
# the box-drawing this module is already built from.
#
# `CS_GLYPHS=ascii` is for the terminals that do neither, and for anyone
# piping a transcript somewhere that would rather not receive astral-plane
# characters.
_ASCII_GLYPHS = os.environ.get("CS_GLYPHS", "").lower() == "ascii"

YOU_MARK = ">" if _ASCII_GLYPHS else "👤"
COPILOT_MARK = "*" if _ASCII_GLYPHS else "🤖"
# Listing row marker for a pinned session — one glyph, ascii falls back to *.
PIN_MARK = "*" if _ASCII_GLYPHS else "📌"


def float_pins(rows: list[tuple]) -> list[tuple]:
    """Stable-sort so pinned sessions float first; relative order is kept."""
    pinned = set(pinned_ids())
    if not pinned:
        return rows
    return sorted(rows, key=lambda row: 0 if row[0] in pinned else 1)


def pin_cell(session_id: str) -> str:
    """A fixed-width pin marker (or blanks) for a listing row."""
    width = cells(PIN_MARK)
    if is_pinned(session_id):
        return f"{AMBER}{PIN_MARK}{RST}"
    return " " * width

# The landing screen's icons, and what each one is when emoji are off. They
# live here rather than beside the menu rows for two reasons: `CS_GLYPHS=ascii`
# was being honoured by the two transcript marks above and ignored by the
# seventeen icons on the first screen anyone sees, which is the wrong way
# round; and an icon is a rendering decision, which is what this module is.
#
# Every glyph is from the original Unicode 6.0 emoji set — the one that every
# emoji font has shipped since 2010 — bar the robot, which is Unicode 8.0 and
# is already the Copilot mark in transcripts: one glyph for the agent in both
# places is worth more than the five-year gap, and 2015 is old enough.
#
# That rule is not fussiness. The hook row used U+1FA9D, added in Unicode 13 in
# 2020, and a terminal whose font predates it draws a blank where the icon
# goes; the row then reads as the one option on the menu that forgot to bring
# an icon. A missing glyph is worse than a plainer one, because a blank looks
# like a bug in the tool rather than a gap in a font.
#
# The two symbol-block characters carry U+FE0F, which asks for the emoji form
# rather than the monochrome text form a terminal would otherwise be free to
# pick out of a symbol font. Without it they render thin, flat and a cell
# narrower than every icon beside them, which is the same "no icon here"
# impression by a different route.
_MENU_GLYPHS: dict[str, tuple[str, str]] = {
    "recent": ("🕒", "~"),
    "all": ("📚", "="),
    "search": ("🔍", "/"),
    "repos": ("📦", "#"),
    "stats": ("📊", "%"),
    "days": ("📅", "|"),
    "spend": ("💰", "$"),
    "efficiency": ("🔋", ">"),
    "delegation": ("👥", "&"),
    "autonomy": ("🚀", "^"),
    "handoff": ("🔗", "-"),
    "security": ("🔐", "!"),
    "instructions": ("📋", "]"),
    "skills": ("🎓", "+"),
    "profiles": ("🤖", "*"),
    "hooks": ("🔔", "}"),
    "mcp": ("🔌", ":"),
    "theme": ("🎨", "T"),
    "help": ("💡", "?"),
    # Not a menu row: the agent that paces the rule under the header. It
    # lives here so it is held to the same rule as every other glyph — the
    # age, width and block tests iterate this table, so the one piece of
    # decoration on the screen cannot be the thing that draws a blank.
    "copilot": ("🤖", "@"),
    # Improve group icons. Kept on the same age / block rules as every other
    # glyph so restoring or extending a row cannot reintroduce a blank icon.
    "context": ("📍", "."),
    "pin": ("📌", ","),
    # Evidence read from the per-session event log and the last billed call.
    "failures": ("💥", "x"),
    "loops": ("🌀", "o"),
    "endings": ("🏁", "e"),
    "subagents": ("🐝", "a"),
    "switches": ("🔀", "w"),
    # The one Today row, and the Find row that starts from a session.
    "today": ("🌅", "n"),
    "similar": ("🔭", "s"),
    "saved": ("🔖", "v"),
    "cleanup": ("🚮", "c"),
    # Analysis that stayed on the menu.
    "anomalies": ("📈", "y"),
    "health": ("🏥", "i"),
    "patterns": ("🔣", "p"),
    "rollup": ("📤", "m"),
}
# Every icon is drawn from the supplemental pictograph planes (U+1F300 and
# up) rather than from the older symbol blocks at U+2100–U+2BFF. Both are
# emoji by the standard, but only the first is emoji to a *terminal*: the
# symbol blocks predate emoji and have long-standing text glyphs, so a
# terminal is free — and quite often configured — to draw them from the
# monospace text font instead of the emoji font. That renders them thin and
# flat where it has a glyph and blank where it does not, which is how
# 'efficiency' and 'help' came to be the two rows on the menu that looked
# like they had forgotten their icons.
#
# U+FE0F was tried first and is not a fix. It is a *request* for the emoji
# form that a terminal may decline, and declining it can drop the whole
# sequence, so the row that was thin became empty. Choosing a codepoint with
# no text form is the fix, because it leaves the terminal nothing to get
# wrong.
# The plain markers are chosen to be *distinct* rather than descriptive: the
# label is right beside them and already says the word, so an icon's remaining
# job on a menu you open daily is to be a shape you learn. Where a symbol can
# also mean what it points at — / for search because / is the search key, $ for
# spend, # for a repository, * for the agent because that is already the
# Copilot mark in transcripts — it does.


def menu_icon(name: str) -> str:
    """The landing screen's icon for a row, honouring `CS_GLYPHS=ascii`.

    Raises on an unknown name rather than returning a blank, because a menu
    row with no icon is exactly the bug this table was added to fix and it
    should not be possible to reintroduce it by misspelling a key.
    """
    emoji, plain = _MENU_GLYPHS[name]
    return plain if _ASCII_GLYPHS else emoji


def speaker(mark: str, name: str, colour: str = "") -> str:
    """A transcript speaker label: the mark, then the name in its own colour.

    A compact label, not a rule. It used to draw a full-width hairline like
    `heading` does, which meant a two-line exchange arrived under three
    full-width rules — the turn's and one per speaker — all the same weight,
    so the eye had nothing to rank and the furniture outnumbered the words.
    Attribution is `spine`'s job now, down the side of the block, and this
    only has to name who is about to speak.

    The name carries the colour and the mark does not: a terminal is free to
    render an emoji in its own palette, and a mark that ignores the escape
    while the word beside it obeys reads as a rendering fault.
    """
    colour = colour or ACCENT
    # Padded to a fixed column rather than by a fixed gap: the emoji marks are
    # two cells wide and their ASCII forms are one, so a literal two spaces
    # put the name in a different column depending on `CS_GLYPHS` — visible
    # the moment the rail runs underneath it.
    return f"{GUTTER}{mark}{' ' * max(4 - cells(mark), 1)}{colour}{BOLD}{name}{RST}"


# The rail that attributes a block of text to whoever said it, and the one
# that marks a quotation inside it. Both are single-cell verticals from the
# box-drawing block this module already draws every table from, so a terminal
# that can render a rule can render these — and `CS_GLYPHS=ascii` gets the
# pipe, for the same reason the speaker marks have a plain form.
SPINE = "|" if _ASCII_GLYPHS else "▎"


def spine(lines: list[str], colour: str = "", indent: str = "    ") -> list[str]:
    """Attribute an already-rendered block to a speaker with a left rail.

    A transcript's one job is to make who-said-what answerable at a glance.
    The label above a block says it in words, once; this says it down the
    block's whole height, which is what lets a fifty-turn session be scanned
    instead of read. It replaced a full-width hairline per speaker — three
    rules a turn, all of them the same weight, so the eye had nothing to
    rank and the furniture outnumbered the conversation two to one.

    The rail takes the first columns of the indent `markdown` already
    applied rather than adding to the left margin: body text stays in the
    column every other view puts it in, so a transcript and a report still
    line up when read one after the other.

    A blank line inside the block keeps the rail and drops the trailing
    space — an unbroken edge is the whole point, and trailing whitespace on
    a blank line is invisible until something diffs it.
    """
    colour = colour or ACCENT
    rail = f"{GUTTER}{colour}{SPINE}{RST} "
    out = []
    for line in lines:
        if not line.strip():
            out.append(rail.rstrip())
        elif line.startswith(indent):
            out.append(rail + line[len(indent):])
        else:
            out.append(rail + line)
    return out


def heading(text: str, colour: str = "", width: int = 0) -> str:
    """A section label, set apart by an accent bar rather than shouting.

    `width` draws a hairline from the end of the label to that column. A page
    of six sections used to be six bold words floating in a column of
    numbers; the line is what turns them into edges you can find without
    reading. It is dropped rather than crowded when the label nearly fills
    the row.
    """
    colour = colour or ACCENT
    head = f"{GUTTER}{colour}▌{RST}{BOLD}{text}{RST}"
    # Ends where `rule` ends. The hairline used to stop two columns short of
    # every full-width rule above it, which on a page of five sections is
    # five ragged right edges and nothing to tell you they were deliberate.
    span = width - cells(_strip(text)) - 2
    if width and span > 3:
        head += f" {SLATE}{'─' * span}{RST}"
    return head


def bar(value: float, peak: float, width: int, colour: str = "",
        track: bool = False, pad: bool = False) -> str:
    """A horizontal bar, drawn to an eighth of a cell and coloured by length.

    Three things this does that `"█" * n` did not:

    * **It never rounds a real value away.** The old bars used `int()`, so
      anything under one cell drew nothing at all and the row read as zero.
    * **It says how long it is twice** — in length and in hue, sweeping the
      wordmark's purple→cyan. On a chart of twenty rows the shape is
      readable before any number is.
    * **It can show what it is a share of.** `track=True` draws the unfilled
      remainder as a dim rail, which turns "nine" into "nine out of eleven"
      without spending a column on the total.

    `colour` overrides the ramp for a bar whose meaning is not its size — a
    severity, or a spend that is already coloured by risk.

    Returns exactly `width` visible cells when `pad` or `track` is set, and
    only the drawn part otherwise, so callers can align a column either way.
    """
    width = max(int(width), 0)
    if width <= 0:
        return ""
    share = 0.0 if peak <= 0 else max(0.0, min(float(value) / float(peak), 1.0))
    eighths = round(share * width * 8)
    if value > 0 and eighths == 0:
        eighths = 1  # something is never nothing
    full, part = divmod(eighths, 8)
    drawn = "█" * full + (_EIGHTHS[part - 1] if part else "")
    rest = width - cells(drawn)

    if not _COLOR:
        tail = ("·" * rest if track else " " * rest) if (track or pad) else ""
        return drawn + tail
    if colour:
        body = f"{colour}{drawn}{RST}"
    else:
        # Banded by position along the *track*, not along the bar: colouring
        # by the bar's own length would run every row through the whole ramp
        # and make a two-cell bar and a forty-cell bar end on the same cyan.
        #
        # One escape per band, not per cell. Per cell was eleven bytes of
        # escape for every block drawn — a 75-column chart row came to nine
        # hundred characters of mostly punctuation, which the curses reader
        # then had to parse back out again on every frame.
        steps = len(_BAR_RAMP)
        parts, run, band = [], "", -1
        for index, char in enumerate(drawn):
            here = min(index * steps // width, steps - 1)
            if here != band:
                if run:
                    parts.append(f"{c256(_BAR_RAMP[band])}{run}")
                run, band = "", here
            run += char
        if run:
            parts.append(f"{c256(_BAR_RAMP[band])}{run}")
        body = "".join(parts) + RST
    if track:
        return body + f"{SLATE}{'·' * rest}{RST}"
    return body + " " * rest if pad else body


def meter(share: float, width: int, colour: str = "") -> str:
    """A bar that is always a share of one — the same drawing, fixed scale."""
    return bar(share, 1.0, width, colour=colour, track=True)


def field(label: str, value: str, label_width: int = 9) -> str:
    """An aligned metadata row: label in muted type, value in normal.

    The column always leaves a gap, even for a label as wide as it — an
    over-long label pushes its value right rather than running into it.

    A value too long for the window wraps under itself, hanging at the value
    column so the block still reads as one row. Every report caps its own
    layout at 96 columns and so does this, which is why it can ask the
    terminal directly instead of being handed a width at eighty call sites.

    A value carrying its own colour is left alone: wrapping counts escape
    codes as characters and can split one down the middle, and a field that
    printed half an escape sequence would be a worse bug than a long line.
    """
    width = max(label_width, len(label) + 1)
    head = f"{GUTTER}{MUTED}{label:<{width}}{RST}"
    room = min(shutil.get_terminal_size().columns, 96) - len(GUTTER) - width
    if room < 12 or _strip(value) != value:
        return head + value

    import textwrap

    lines = textwrap.wrap(value, room, break_long_words=False,
                          break_on_hyphens=False) or [""]
    hang = " " * (len(GUTTER) + width)
    return "\n".join([head + _fit(lines[0], room)]
                     + [hang + _fit(line, room) for line in lines[1:]])


def markdown(text: str, width: int, indent: str = "    ") -> list[str]:
    """Render light markdown to styled terminal lines.

    Handles headings, bullets, numbered items, tables, fenced code and inline
    code — the shapes assistant replies actually use. Anything else passes
    through wrapped. Code is never reflowed: its alignment is the information.
    """
    import textwrap

    out: list[str] = []
    rows: list[str] = []
    in_code = False
    body = max(width - len(indent), 24)

    def wrap(text: str, room: int) -> list[str]:
        """Wrap prose with its styling intact across the line break.

        Styling is applied *after* wrapping, so it has to survive it: a
        `**span**` broken over two lines no longer matches its own regex,
        which is how raw `**` used to reach the screen. Markers become
        one-character sentinels first, and each line closes its own styling.
        """
        parts = textwrap.wrap(
            _mark(text), width=room, break_long_words=False, break_on_hyphens=False
        ) or [""]
        return [_paint(part + _RESET_MARK) for part in parts]

    def flush_table() -> None:
        if rows:
            out.extend(_table(rows, body, indent))
            rows.clear()

    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            flush_table()
            in_code = not in_code
            language = stripped[3:].strip()
            if in_code and language:
                out.append(f"{indent}{MUTED}│ {language}{RST}")
            continue

        if in_code:
            # Never truncate code: a cut line loses the very thing that makes
            # it useful (and once cut a masked value reads as a leak). Long
            # lines overflow and the pager wraps them.
            out.append(f"{indent}{MUTED}│{RST} {CODE}{line}{RST}")
            continue

        # A table is gathered whole: column widths are only knowable at the
        # end of the block.
        if stripped.startswith("|"):
            rows.append(stripped)
            continue
        flush_table()

        if not stripped:
            out.append("")
            continue

        level = len(stripped) - len(stripped.lstrip("#"))
        if 0 < level <= 6 and stripped[level : level + 1] == " ":
            label = _inline(stripped[level + 1 :])
            # Bold alone does not out-rank a reply that is already full of
            # bold spans, and the section titles in a long answer are the one
            # structure it has — they read as another sentence. The accent
            # carries the rank instead of a bar, because a bar would want the
            # two columns to the left of the text and those belong to
            # whichever rail the block is nested in.
            weight = f"{ACCENT}{BOLD}" if level <= 2 else BOLD
            out.append(f"{indent}{weight}{label}{RST}")
            continue

        # A quotation is somebody else's words inside these ones — usually an
        # error message or a line of the prompt being answered. Passed
        # through as prose it arrived with a literal '>' in front of it and
        # read as a typo; the rail says the same thing the source meant.
        if stripped.startswith(">"):
            quoted = stripped.lstrip("> ").rstrip()
            rail = f"{indent}{SLATE}{SPINE}{RST} "
            out.extend(rail + part for part in wrap(quoted, max(body - 2, 12)))
            continue

        marker, rest = _list_parts(stripped)
        if marker:
            hang = indent + " " * (len(marker) + 1)
            wrapped = wrap(rest, body - len(marker) - 1)
            bullet = "•" if marker in "-*•" else marker
            out.append(f"{indent}{ACCENT}{bullet}{RST} {wrapped[0]}")
            out.extend(f"{hang}{part}" for part in wrapped[1:])
            continue

        if raw.startswith(("    ", "\t")):
            out.append(f"{indent}{CODE}{line}{RST}")
            continue

        out.extend(f"{indent}{part}" for part in wrap(stripped, body))

    flush_table()
    return out


def _table(rows: list[str], width: int, indent: str) -> list[str]:
    """Render a markdown table as aligned columns.

    Replies are full of tables, and raw `| a | b |` pipes are the least
    readable thing on the screen — the columns never line up, because the
    source was written for a renderer. Here the widths are computed and the
    `|---|` divider becomes a rule under the header.
    """
    grid = [[cell.strip() for cell in row.strip().strip("|").split("|")] for row in rows]
    header = grid[0] if len(grid) > 1 and _is_divider(grid[1]) else None
    body = [row for row in grid if not _is_divider(row)]
    if not body:
        return []

    columns = max(len(row) for row in body)
    body = [[_strip_markers(cell) for cell in row] + [""] * (columns - len(row))
            for row in body]
    widths = [max(cells(row[i]) for row in body) for i in range(columns)]

    # Shrink the widest column until the table fits; a narrow column carries
    # a key or a status, and losing that is worse than folding the prose one.
    gap = 2
    while sum(widths) + gap * (columns - 1) > width and max(widths) > 6:
        widths[widths.index(max(widths))] -= 1

    def render(row: list[str], style: str = "") -> str:
        parts = [_fit(cell, w) + " " * (w - cells(_fit(cell, w)))
                 for cell, w in zip(row, widths, strict=True)]
        text = (" " * gap).join(parts).rstrip()
        if not text:
            return ""
        return f"{indent}{style}{text}{RST if style else ''}"

    out = []
    if header:
        out.append(render(body[0], BOLD))
        out.append(f"{indent}{MUTED}{(' ' * gap).join('─' * w for w in widths)}{RST}")
        body = body[1:]
    out.extend(render(row) for row in body)
    return out


def _is_divider(row: list[str]) -> bool:
    """Whether a table row is the `|---|:--:|` rule rather than data."""
    import re

    return bool(row) and all(re.fullmatch(r":?-{2,}:?", cell) for cell in row)


def cells(text: str) -> int:
    """Columns a string occupies. Emoji and CJK take two, and replies are
    full of ✅/⬜ — counting them as one shears every column to their right.

    Zero-width characters take none. A combining accent, a variation
    selector and a zero-joiner all draw *into* the character before them
    rather than beside it, so counting them as a column each shears the same
    row this function exists to keep straight — and in the other direction,
    which is worse, because the text then looks like it fits when it does
    not. `Mn`/`Me` are the marks, `Cf` the formatting codepoints that carry
    no glyph at all.
    """
    import unicodedata

    total = 0
    for ch in text:
        if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in "WF" else 1
    return total


def _fit(text: str, width: int) -> str:
    """Truncate to a column count, not a character count."""
    if width <= 0:
        return ""
    if cells(text) <= width:
        return text
    return clip(text, width - 1) + "…"


def clip(text: str, width: int) -> str:
    """The prefix that fits in `width` cells, including attached zero-width marks."""
    if width <= 0:
        return ""
    # Measured a character at a time rather than re-measuring the whole
    # prefix on every step, which made a long title quadratic to fit.
    used, cut = 0, 0
    for at, ch in enumerate(text):
        used += cells(ch)
        if used > width:
            break
        cut = at + 1
    return text[:cut]


def _list_parts(text: str) -> tuple[str, str]:
    """Split a list marker from its text, or ('', text) when not a list."""
    import re

    match = re.match(r"^([-*•]|\d{1,3}[.)])\s+(.*)$", text)
    return (match.group(1), match.group(2)) if match else ("", text)


def _inline(text: str) -> str:
    """Style `code`, **bold** and *italic* on a line that is not wrapped."""
    return _paint(_mark(text) + _RESET_MARK)


# One-character stand-ins for styling, applied before wrapping and swapped for
# escape codes after it. They are never printed: _paint always runs last, and
# with colour off every code is the empty string, which is how piped output
# comes out as clean prose rather than raw markdown.
_RESET_MARK = "\x00"
_MARKS = {"\x01": BOLD, "\x02": CODE, "\x03": DIM, _RESET_MARK: RST}


def _mark(text: str) -> str:
    """Replace inline markdown with wrap-safe style sentinels."""
    import re

    text = re.sub(r"`([^`]+)`", "\x02\\1\x00", text)
    text = re.sub(r"\*\*([^*]+)\*\*", "\x01\\1\x00", text)
    text = re.sub(r"__([^_]+)__", "\x01\\1\x00", text)
    return re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", "\x03\\1\x00", text)


def _paint(text: str) -> str:
    """Swap style sentinels for escape codes — or for nothing, without colour."""
    for mark, code in _MARKS.items():
        text = text.replace(mark, code)
    return text


def _strip_markers(text: str) -> str:
    """Plain text: markdown markers removed, nothing styled."""
    marked = _mark(text)
    for mark in _MARKS:
        marked = marked.replace(mark, "")
    return marked



def _strip(s: str) -> str:
    """Length of a string ignoring ANSI escape codes."""
    import re

    return re.sub(r"\033\[[0-9;]*m", "", s)


# ── Banner ───────────────────────────────────────────────────────────
# Three sizes, because a banner that does not fit is not a banner. The
# caller asks for the biggest one the window can hold; each is a plain list
# of equal-length lines, so centring and colouring stay someone else's job.

BANNER = [
    r" ██████╗ ██████╗ ██████╗ ██╗██╗      ██████╗ ████████╗",
    r"██╔════╝██╔═══██╗██╔══██╗██║██║     ██╔═══██╗╚══██╔══╝",
    r"██║     ██║   ██║██████╔╝██║██║     ██║   ██║   ██║   ",
    r"██║     ██║   ██║██╔═══╝ ██║██║     ██║   ██║   ██║   ",
    r"╚██████╗╚██████╔╝██║     ██║███████╗╚██████╔╝   ██║   ",
    r" ╚═════╝ ╚═════╝ ╚═╝     ╚═╝╚══════╝ ╚═════╝    ╚═╝   ",
    r"        S E S S I O N S   B R O W S E R              ",
]

_BANNER_SMALL = [
    r"  ___ ___ ",
    r" / __/ __|",
    r"| (__\__ \  copilot sessions",
    r" \___|___/",
]


def _block(art: list[str]) -> list[str]:
    """Pad art to a rectangle so centring it cannot make it ragged."""
    span = max(len(line) for line in art)
    return [line.ljust(span) for line in art]


def banner(width: int, height: int) -> list[str]:
    """The largest wordmark that fits, or a single line when nothing does.

    `height` is how many rows the caller can spare, not the whole window: a
    banner that pushes the menu off the screen has cost more than it gave.
    """
    for art in (BANNER, _BANNER_SMALL):
        block = _block(art)
        if width >= len(block[0]) + 4 and height >= len(block):
            return block
    return ["cs · copilot sessions"] if width >= 23 else ["cs"]


# ── Splash ───────────────────────────────────────────────────────────

def render_splash(stats: dict | None) -> None:
    if not _COLOR:
        return
    cols = shutil.get_terminal_size().columns
    print("\033[2J\033[H", end="")

    print()
    for line in BANNER:
        print(gradient_text(line.center(cols)))
    print()
    print(f"  {gradient_text('━' * min(cols - 4, 56))}")
    print()

    if stats:
        w = min(cols - 6, 56)
        bc = "\033[38;2;59;160;255m" if _COLOR else ""
        print(f"  {bc}┌{'─' * w}┐{RST}")
        for label, val in [
            ("Sessions", stats["total"]),
            ("Interactive", stats["interactive"]),
            ("Turns", stats["total_turns"]),
            ("Repositories", stats["repos"]),
        ]:
            line = f"{label}: {BOLD}{val}{RST}"
            pad = w - len(f"{label}: {val}") - 3
            print(f"  {bc}│{RST}  {line}{' ' * max(pad, 0)}{bc}│{RST}")
        print(f"  {bc}└{'─' * w}┘{RST}")
        print()

    print(f"  {BOLD}Commands:{RST}")
    for cmd, desc in [
        ("cs recent [days]", "recent interactive sessions (default 7)"),
        ("cs all [days]", "include automated/scheduled sessions"),
        ("cs search <words>", "full-text search, best match first"),
        ("cs show <#N|id>", "overview: spend, files, turn list"),
        ("cs read <#N|id>", "the full conversation, paged"),
        ("cs files [path]", "sessions that touched a file"),
        ("cs resume <#N|id>", "resume a session"),
        ("cs repos", "sessions by repository"),
        ("cs stats", "overall statistics"),
        ("cs timeline", "sessions-per-day chart"),
        ("cs cost [days]", "AI spend by model, repo and day"),
    ]:
        print(f"  {DIM}  {cmd:<20} → {desc}{RST}")
    print()
