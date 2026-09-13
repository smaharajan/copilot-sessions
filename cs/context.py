"""Context audit — what this repository gives the agent before you type.

Every other view in cs reads the record of work already done. This one reads
the setup that work will start from: the instruction files, prompts, skills,
agent profiles and hooks that are loaded before your first word.

It is the cheapest thing to get right and the easiest to leave rotting. An
instruction file grows past the point where it is read in full; a skills
directory has one file in it from six months ago; a repository has nothing at
all and every session starts by explaining the same conventions again.

Two scopes are checked: **personal** (`$COPILOT_HOME`, which follows you
between repositories) and **project** (the working directory, which is the
one your colleagues also get). Nothing is written or fixed — this reports.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from . import hooks

# Copilot's code-review path silently truncates an instruction file past this,
# and a model that reads two-thirds of your conventions is worse than one that
# reads a short file completely.
INSTRUCTION_LIMIT = 4_000

# Where each kind of context lives. Copilot's own paths and the AGENTS.md
# convention both get a look because a real repository usually carries more
# than one, and a file the agent reads is a file this should report.
# (kind, path relative to the scope root, what counts as a file, recurse).
# A skill is one directory or one file, never every markdown page inside it —
# counting the pages would report a well-documented skill as forty skills.
_PROJECT = (
    ("instructions", ".github/copilot-instructions.md", None, False),
    ("instructions", "AGENTS.md", None, False),
    ("instructions", ".github/instructions", r"\.instructions\.md$", False),
    ("prompts", ".github/prompts", r"\.prompt\.md$", True),
    ("prompts", "prompts", r"\.prompt\.md$", False),
    # Matched against the path relative to the search directory, so a glob
    # can say "top level only" while still recursing. Every pattern here is
    # suffix-anchored bar one, which is top-level by construction anyway.
    ("skills", ".github/skills", r"^[^/]+\.md$|(?:^|/)SKILL\.md$", True),
    ("skills", ".copilot/skills", r"\.md$", False),
    # The cross-tool root. Copilot loads from it — there are sessions in this
    # store whose load marker names a skill that exists nowhere else — and
    # leaving it out was most of what "not installed" meant on this machine.
    ("skills", ".agents/skills", r"(?:^|/)SKILL\.md$", True),
    ("agents", ".github/agents", r"\.md$", False),
    ("agents", ".copilot/agents", r"\.md$", False),
)

_PERSONAL = (
    ("instructions", "copilot-instructions.md", None, False),
    ("instructions", "AGENTS.md", None, False),
    ("skills", "skills", r"\.md$|^[^.]+$", False),
    ("agents", "agents", r"\.md$", False),
)

# A plugin installs under installed-plugins/<marketplace>/<pack>/, and the
# skills it ships sit in a `skills` directory inside the pack. Only that
# directory is walked: ponytail also keeps a `.openclaw/skills` mirror for a
# different harness beside it, and counting both reports every one of its
# skills twice.
_PLUGINS = "installed-plugins"
_PLUGIN = (("skills", "skills", r"(?:^|/)SKILL\.md$", True),)

# The personal half of the cross-tool root, a sibling of the Copilot home
# rather than a child of it: ~/.agents/skills beside ~/.copilot.
_SHARED_HOME = ".agents"
_SHARED = (("skills", "skills", r"(?:^|/)SKILL\.md$", True),)

# Copilot ships a few skills inside the CLI package itself, under one
# directory per installed version. They are not yours, cannot be removed,
# and still answer to their name in a transcript — so they are counted, and
# counted once: only the newest package is the one that runs.
_BUILTIN = (("skills", "builtin-skills", r"(?:^|/)SKILL\.md$", True),)

_HEADING = re.compile(r"^#{1,6}\s", re.M)


@dataclass
class Item:
    """One context file, and what is true about it."""

    kind: str
    scope: str
    path: Path
    label: str
    chars: int
    lines: int
    headings: int

    @property
    def oversized(self) -> bool:
        return self.kind == "instructions" and self.chars > INSTRUCTION_LIMIT

    @property
    def unsectioned(self) -> bool:
        """A long file with no headings is one the model reads as a wall."""
        return self.kind == "instructions" and self.lines > 60 and not self.headings


def _measure(path: Path, kind: str, scope: str, root: Path) -> Item | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        label = str(path.relative_to(root))
    except ValueError:
        label = path.name
    return Item(kind, scope, path, label, len(text),
                text.count("\n") + 1, len(_HEADING.findall(text)))


def _is_file(path: Path) -> bool:
    """`Path.is_file()`, but False rather than a traceback.

    `is_file()` only swallows "not there" errors; a directory the user cannot
    read raises `PermissionError` instead. One such directory anywhere under
    the scope roots would take the whole audit down, and a context report is
    the last thing that should insist on reading everything to say anything.
    """
    try:
        return path.is_file()
    except OSError:
        return False


def _is_dir(path: Path) -> bool:
    """`Path.is_dir()`, but False rather than a traceback.

    The same hazard :func:`_is_file` was written for, and a wider one now.
    These walks no longer stay inside two scope roots: `locate` and `ships`
    open every directory the store has seen a session in, which on this
    machine is two hundred and forty-one of them — a mounted volume, a
    restored backup, someone else's folder. One that cannot be stat'd would
    take `cs skills` down with a PermissionError.
    """
    try:
        return path.is_dir()
    except OSError:
        return False


def _entries(target: Path, recurse: bool) -> list[Path]:
    """What is in a directory, or nothing if it cannot be listed."""
    try:
        return sorted(target.rglob("*") if recurse else target.iterdir())
    except OSError:
        return []


def _collect(root: Path, scope: str, patterns) -> list[Item]:
    found: list[Item] = []
    for kind, relative, glob, recurse in patterns:
        target = root / relative
        if glob is None:
            if _is_file(target) and (item := _measure(target, kind, scope, root)):
                found.append(item)
            continue
        if not _is_dir(target):
            continue
        matcher = re.compile(glob, re.I)
        entries = _entries(target, recurse)
        for entry in entries:
            # Every segment of the path, not only the last. A dotted
            # *directory* is how a copy meant for another tool sits unnoticed
            # beside the real one, and checking `entry.name` alone counted
            # ponytail's `.openclaw` mirror as six more skills.
            if any(part.startswith(".")
                   for part in entry.relative_to(target).parts):
                continue
            if _is_dir(entry) and not recurse:
                # A skill kept as a directory: measure the file that defines
                # it, so the count is skills rather than pages.
                entry = next(
                    (entry / name for name in ("SKILL.md", "README.md")
                     if _is_file(entry / name)), entry
                )
                if _is_dir(entry):
                    continue
            elif not _is_file(entry) or not matcher.search(
                    entry.relative_to(target).as_posix()):
                continue
            if item := _measure(entry, kind, scope, root):
                found.append(item)
    return found


def audit(project: Path | None = None) -> dict:
    """Everything on disk that will be loaded before your first prompt."""
    root = (project or Path.cwd()).resolve()
    personal_root = hooks.home()
    # Plugins are walked here too, and not as a courtesy: `cs skills` reads
    # this same table, and a skills view that counted an enabled plugin's
    # skills while the context view did not would put the two numbers back
    # into the disagreement that sharing one walk exists to prevent.
    builtin = builtin_root()
    items = (
        _collect(root, "project", _PROJECT)
        + _collect(personal_root, "personal", _PERSONAL)
        + _collect(shared_home(), "personal", _SHARED)
        + [item for pack in plugin_packs()
           for item in _collect(pack, "plugin", _PLUGIN)]
        + (_collect(builtin, "builtin", _BUILTIN) if builtin else [])
    )
    # One file can be reached by two patterns (a repo whose cwd *is* the
    # Copilot home, say). Keyed by resolved path so it is counted once.
    unique = {item.path.resolve(): item for item in items}
    configured, problems = hooks.load()
    return {
        "root": root,
        "personal_root": personal_root,
        "plugin_root": personal_root / _PLUGINS,
        "builtin_root": builtin or personal_root / "pkg",
        "items": sorted(unique.values(), key=lambda i: (i.scope, i.kind, i.label)),
        "hooks": configured,
        "hook_problems": problems,
    }


def settings(home: Path | None = None) -> dict:
    """`$COPILOT_HOME/settings.json`, or `{}` if it cannot be read.

    Copilot keeps two files side by side and only one of them is strict
    JSON: `config.json` opens with a `//` comment. Nothing promises
    `settings.json` will not grow one, so a leading-slash line is tolerated
    here rather than allowed to take the view down.

    An unreadable file reads as *nothing is switched off*. That is the
    permissive answer, and it is the right one: the alternative is a view
    that hides a skill because it could not parse a config file, which is
    the failure the reader is least able to diagnose.
    """
    path = (home or hooks.home()) / "settings.json"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    for attempt in (text, _uncommented(text)):
        try:
            loaded = json.loads(attempt)
        except ValueError:
            continue
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _uncommented(text: str) -> str:
    """`text` without whole-line `//` comments. Not a JSONC parser."""
    return "\n".join(
        "" if line.lstrip().startswith("//") else line
        for line in text.splitlines()
    )


def settings_problem(home: Path | None = None) -> str | None:
    """Why enablement could not be read, or None when it could.

    The view says this out loud. "No skill is disabled" and "I could not
    find out which skills are disabled" produce the same table, and the
    reader cannot tell them apart unless one of them says so.
    """
    path = (home or hooks.home()) / "settings.json"
    if not _is_file(path):
        return None          # no settings at all is a real, ordinary state
    return None if settings(home) else f"{path.name} could not be read"


def disabled_skills(home: Path | None = None) -> set[str]:
    """Skill names switched off in settings, lowercased.

    These stay in the inventory. A skill you turned off is not a skill you
    forgot about, and filing the two together is what made `cs skills`
    report eleven deliberate choices as neglect.
    """
    found = settings(home).get("disabledSkills")
    return {str(name).lower() for name in found} if isinstance(found, list) else set()


def plugin_packs(home: Path | None = None) -> list[Path]:
    """Every *enabled* plugin pack directory.

    Copilot keys enablement as `<pack>@<marketplace>` and installs to
    `installed-plugins/<marketplace>/<pack>/`. A pack that is switched off
    ships skills the CLI will never load, so it is not walked at all — its
    skills are absent from the inventory rather than listed and greyed.
    """
    enabled = settings(home).get("enabledPlugins")
    if not isinstance(enabled, dict):
        return []
    root = (home or hooks.home()) / _PLUGINS
    packs = []
    for key, on in enabled.items():
        pack, _, marketplace = str(key).partition("@")
        if on is True and pack and marketplace:
            candidate = root / marketplace / pack
            if _is_dir(candidate):
                packs.append(candidate)
    return packs


def shared_home() -> Path:
    """`~/.agents`, the root Copilot shares with other tools.

    Anchored to the real home directory and not to `$COPILOT_HOME`. That
    variable is Copilot's own override and may point anywhere; following it
    here meant searching the parent of wherever it pointed, which in the
    test suite is the shared system temp directory. `$CS_AGENTS_HOME`
    overrides this for a machine that keeps the root somewhere else.
    """
    override = os.environ.get("CS_AGENTS_HOME")
    return Path(override) if override else Path.home() / _SHARED_HOME


def builtin_root(home: Path | None = None) -> Path | None:
    """The newest installed CLI package, or None if there is none.

    Copilot keeps every version it has downloaded, so the six directories
    here would report `github-pr-media` six times. Newest wins, by version
    number rather than by mtime: an older package re-downloaded is still an
    older package. Nothing on disk records which one is running, so this is
    a reading of the evidence and not a promise.
    """
    pkg = (home or hooks.home()) / "pkg"
    found = [
        version for platform in _subdirectories(pkg)
        for version in _subdirectories(platform)
    ]
    return max(found, key=lambda path: _version(path.name)) if found else None


def _subdirectories(path: Path) -> list[Path]:
    try:
        return [entry for entry in path.iterdir() if _is_dir(entry)]
    except OSError:
        return []


def _version(name: str) -> tuple:
    """`1.0.9` sorts below `1.0.10`, which a string compare gets backwards."""
    return tuple(int(part) if part.isdigit() else -1
                 for part in name.split("."))


def asset_dirs(kind: str, project: Path | None = None) -> list[Path]:
    """Every place a skill or agent can live, project first then personal.

    Read out of the same tables the audit walks, so an empty report cannot
    name a search path the scan does not actually use.
    """
    root = (project or Path.cwd()).resolve()
    return [
        *(root / relative for k, relative, _glob, _r in _PROJECT if k == kind),
        *(hooks.home() / relative for k, relative, _glob, _r in _PERSONAL
          if k == kind),
        *(shared_home() / relative for k, relative, _glob, _r in _SHARED
          if k == kind),
        *(pack / relative for pack in plugin_packs()
          for k, relative, _glob, _r in _PLUGIN if k == kind),
        *([builtin_root() / relative
           for k, relative, _glob, _r in _BUILTIN if k == kind]
          if builtin_root() else []),
    ]


def _asset_name(path: Path) -> str:
    """What a skill or agent is called, from where it sits on disk.

    A skill is either one file (`review.skill.md`) or a directory holding a
    SKILL.md. In the second case the interesting name is the directory's —
    naming them all "SKILL" would collapse every skill in a repository into
    one row.
    """
    if path.name in ("SKILL.md", "README.md"):
        return path.parent.name
    name = path.name
    for suffix in (".agent.md", ".skill.md", ".md"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


class Asset(NamedTuple):
    """One skill or agent profile Copilot could load, and what it is."""

    name: str
    scope: str      # project | personal | plugin
    state: str      # enabled | disabled
    path: Path

    @property
    def disabled(self) -> bool:
        return self.state == "disabled"


def assets(kind: str, project: Path | None = None) -> list[Asset]:
    """Every skill or agent Copilot could load here, sorted by name.

    The same walk the audit uses, so `cs skills` and `cs context` cannot
    disagree about what is installed — they used to, because this had its own
    shorter list of directories that never mentioned `.github/skills`, which
    is where a repository actually keeps them. Standing in a repo with twenty
    skills, the inventory showed none of them.

    *Here* is the operative word, and it is Copilot's own rule rather than a
    choice made in this file: skills resolve against the working directory,
    so what is loadable is this repository's plus your personal ones plus
    whatever the enabled plugins ship. A list of every skill in every
    repository on the disk would be a larger number describing a set Copilot
    never offers at once. Where a skill it cannot load actually lives is a
    separate question, and :func:`locate` answers that one.

    Precedence runs plugin, then personal, then project, so a name collision
    resolves the way Copilot resolves it: the repository's copy is the one
    your colleagues also get, and your own copy beats a marketplace one.
    """
    root = (project or Path.cwd()).resolve()
    off = disabled_skills() if kind == "skills" else set()
    builtin = builtin_root()
    found: dict[str, Asset] = {}
    # Each root is paired with its own table, never crossed with the others.
    # Walking ~/.copilot/skills with the shared root's recursive rule found a
    # stray `graphify/graphify/skill.md` and filed it as a skill called
    # 'skill' — one row that `cs context`, walking the tables properly, did
    # not have.
    for scope, walks in (
        ("builtin", [(builtin, _BUILTIN)] if builtin else []),
        ("plugin", [(pack, _PLUGIN) for pack in plugin_packs()]),
        ("personal", [(hooks.home(), _PERSONAL), (shared_home(), _SHARED)]),
        ("project", [(root, _PROJECT)]),   # last, so it wins a collision
    ):
        for scope_root, patterns in walks:
            wanted = [p for p in patterns if p[0] == kind]
            for item in _collect(scope_root, scope, wanted):
                name = _asset_name(item.path)
                previous = found.get(name)
                # The same file reached twice keeps the label it had. Stand
                # in your home directory and `.copilot/skills` — a project
                # pattern — *is* the personal directory, which reported all
                # forty-nine personal skills as shipped by this repo.
                if previous and _same_file(previous.path, item.path):
                    continue
                found[name] = Asset(
                    name, scope,
                    "disabled" if name.lower() in off else "enabled",
                    item.path,
                )
    return sorted(found.values())


def _same_file(one: Path, other: Path) -> bool:
    """Whether two paths are the same file, symlinks and all."""
    try:
        return one.resolve() == other.resolve()
    except OSError:
        return one == other


def locate(names: set[str], directories: list[str]) -> dict[str, str]:
    """Which directory ships each of `names`: name -> the directory's name.

    This is what turns "not installed" — true, and useless — into the name
    of the checkout you would have to be standing in for Copilot to load it.

    Named by directory rather than by the repository the store recorded.
    One checkout collects several repository labels over its life as its
    remote is renamed or re-pointed: this machine has one directory filed
    under three, and picking whichever the query returned first put a
    skill in a repository nobody had heard of. The directory is the part
    you can go and look at.

    Only directories that still exist are opened, and only ones a session
    has been run in are considered at all, so this is a short walk rather
    than a sweep of the disk.
    """
    wanted = {name.lower() for name in names}
    found: dict[str, str] = {}
    for directory in directories:
        if not wanted - set(found):
            break                      # everything is accounted for
        root = Path(directory)
        if not _is_dir(root):
            continue                   # a checkout that has since gone
        for item in _collect(root, "project",
                             [p for p in _PROJECT if p[0] == "skills"]):
            name = _asset_name(item.path).lower()
            if name in wanted:
                found.setdefault(name, root.name or directory)
    return found


def ships(directory: str | Path) -> set[str]:
    """The skills a checkout carries itself, lowercased.

    What separates a repository that equips its own work from one leaning on
    whatever the person happened to have installed. The second kind works
    beautifully until a colleague clones it.
    """
    root = Path(directory)
    if not _is_dir(root):
        return set()
    # Your own kit is not shipped by whatever directory you were standing in.
    # Stand in your home and `.copilot/skills` matches a project pattern, so
    # every personal skill counted as one the home directory carried — which
    # would have read as sixty-nine skills a colleague would inherit. Only
    # the personal skill directories are excluded, not the whole Copilot
    # home: a repository that happens to sit inside it is still a repository.
    mine = tuple(
        root / relative for root in (hooks.home(), shared_home())
        for kind, relative, _glob, _r in _PERSONAL + _SHARED
        if kind == "skills"
    )
    found = set()
    for item in _collect(root, "project",
                         [p for p in _PROJECT if p[0] == "skills"]):
        try:
            reached = (item.path, item.path.resolve())
        except OSError:
            reached = (item.path,)
        # Both the path walked and the file it points at. A personal skill is
        # often a symlink into a repository — reaching it through
        # `$COPILOT_HOME/skills` is what makes it yours, whatever it resolves
        # to — and a repository can just as well link into the home.
        if any(root_path in candidate.parents
               for candidate in reached for root_path in mine):
            continue
        found.add(_asset_name(item.path).lower())
    return found


def instruction_paths(project: Path | None = None) -> list[Path]:
    """Every place an instruction file can live, project first then personal.

    Read out of the same tables the audit walks, so an empty report cannot
    name a search path the scan does not actually use.
    """
    root = (project or Path.cwd()).resolve()
    return [
        *(root / relative for kind, relative, _glob, _r in _PROJECT
          if kind == "instructions"),
        *(hooks.home() / relative for kind, relative, _glob, _r in _PERSONAL
          if kind == "instructions"),
    ]


def gaps(found: dict) -> list[tuple[str, str, str]]:
    """(severity, what, what to do) — only things that are actually wrong.

    Deliberately short. A checklist long enough to ignore is a checklist that
    gets ignored, so this reports the gaps that change what the agent sees on
    the very next session and nothing else.
    """
    items: list[Item] = found["items"]
    by = lambda kind, scope: [  # noqa: E731 - a filter, not a function
        i for i in items if i.kind == kind and i.scope == scope
    ]
    out: list[tuple[str, str, str]] = []

    if not by("instructions", "project"):
        out.append((
            "high",
            "This project tells the agent nothing about itself",
            "Add .github/copilot-instructions.md (or AGENTS.md) with the "
            "conventions you would otherwise repeat: how to run the tests, "
            "what not to touch, how commits are worded.",
        ))
    for item in items:
        if item.oversized:
            out.append((
                "medium",
                f"{item.scope} {item.label} is {item.chars:,} characters",
                f"Copilot truncates an instruction file past "
                f"{INSTRUCTION_LIMIT:,} characters, so the end of this one is "
                f"not being read. Move the scoped rules into "
                f".github/instructions/*.instructions.md, which load only "
                f"when they match.",
            ))
        if item.unsectioned:
            out.append((
                "low",
                f"{item.scope} {item.label} is {item.lines} lines with no "
                f"headings",
                "Add ## sections. A model skims structure the same way you "
                "do, and an unsectioned wall gets read as one topic.",
            ))
    # Not the built-in ones. Copilot ships a handful inside its own package,
    # so counting those means this advice can never fire again — on the one
    # machine that needs it, which is the machine with no skills of its own.
    if not any(item.kind == "skills" and item.scope != "builtin"
               for item in items):
        out.append((
            "medium",
            "No skills anywhere",
            "A prompt you have typed three times is a skill. 'cs coach' finds "
            "the repeats; $COPILOT_HOME/skills is where they go.",
        ))
    if not found["hooks"]:
        out.append((
            "low",
            "No hooks configured",
            "Hooks run commands on the session lifecycle — a formatter after "
            "an edit, a guard before a shell command. 'cs hooks' shows where "
            "they are declared.",
        ))
    if found["hook_problems"]:
        out.append((
            "high",
            f"{len(found['hook_problems'])} hook files do not parse",
            "Invalid JSON means those hooks never run, silently. 'cs hooks' "
            "names the files.",
        ))
    return out
