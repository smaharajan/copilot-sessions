"""Kit and configuration inventory: assets, hooks, MCP, instructions, context, agents."""

from __future__ import annotations

import shutil
import sys
import textwrap
from collections import Counter
from pathlib import Path

from .. import (
    context,
    db,
    hooks,
    mcp,
    redact,
    ui,
)
from ._common import (
    _HOOKS_HEADS,
    _LEVELS,
    _MCP_HEADS,
    _capture,
    _cell,
    _chart_spans,
    _extra_gaps,
    _fit_columns,
    _head_rule,
    _hint,
    _item,
    _name_grid,
    _note,
    _page,
    _page_report,
    _row,
    _sort_note,
    _sort_report,
    _when,
    _why,
    _why_hint,
    _window_label,
)


def _asset_dirs(kind: str) -> list[Path]:
    """Where Copilot keeps skills / agents: project first, then personal."""
    return context.asset_dirs(kind)


def _asset_names(kind: str) -> list[tuple[str, Path]]:
    """(name, path) for every skill or agent on disk, deduped by name."""
    return [(asset.name, asset.path) for asset in context.assets(kind)]


def cmd_assets(kind: str, name: str | None = None, limit: int = 25,
               sort_by: str | None = None, descending: bool | None = None,
               by_place: bool = False) -> bool:
    """The inventory, or — given a name — the sessions that reference it."""
    if by_place:
        return _page(_capture(_render_assets_by_place))
    if name:
        return _page(_capture(lambda: _render_asset_sessions(kind, name)))
    return _page_report(
        "assets",
        lambda column, down: _capture(lambda: _render_assets(kind, limit, column, down)),
        sort_by, descending,
    )


def _render_asset_sessions(kind: str, name: str) -> None:
    known = dict(_asset_names(kind))
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    conn = db.connect()
    # A name the CLI is recorded as having loaded is askable about even with
    # nothing on this disk to point at. Refusing to answer for one — which
    # is what looking only at the shelf did — denies the session record on
    # the strength of a missing file.
    elsewhere = sorted({
        loaded for names in db.skills_invoked_by_session(conn).values()
        for loaded in names
    } - {real.lower() for real in known}) if kind == "skills" else []
    match = next(
        (real for real in [*known, *elsewhere] if real.lower() == name.lower()),
        next((real for real in [*known, *elsewhere]
              if name.lower() in real.lower()), None),
    )
    print()
    if not match:
        conn.close()
        print(ui.rule(inner, f"{kind.title()} · {name}"))
        print()
        print(f"  {ui.MUTED}No {kind[:-1]} named '{name}' on disk.{ui.RST}")
        close = [real for real in known if name.lower()[:4] in real.lower()][:6]
        if close:
            print(f"  {ui.MUTED}did you mean: {', '.join(close)}{ui.RST}")
        print()
        return

    rows = db.sessions_for_asset(conn, match)
    conn.close()
    print(ui.rule(inner, f"{match} · {len(rows)} sessions"))
    print()
    if match in known:
        print(ui.field("file", str(known[match])))
    else:
        print(ui.field("file", "not installed here — it ran in sessions whose "
                               "skills this repository does not ship"))
    print()
    if not rows:
        print(f"  {ui.MUTED}No session references it.{ui.RST}")
        print()
        return
    for session_id, summary, last_active in rows:
        print(f"    {ui.MUTED}{last_active[:16]}{ui.RST}  "
              f"{ui.trunc(redact.redact(summary), inner - 26)}")
        print(f"    {ui.MUTED}            cs show {session_id}{ui.RST}")
    print()


def _asset_inventory(kind: str) -> dict:
    """What both the screen and `--json` need to say about one kind of asset.

    Shared for the same reason the disk walk is shared. Each built its own
    list for a while and only one of them knew about the skills that ran
    without being installed, so the screen said forty-six had been
    referenced while the export, from the same store a second later, said
    thirty-five.
    """
    inventory = context.assets(kind)
    on_disk = {asset.name.lower(): asset for asset in inventory}
    conn = db.connect()
    # Sessions that demonstrably *loaded* each one, as opposed to sessions
    # that merely named it. Only skills leave this trace, so agent profiles
    # get an empty map and the view quietly falls back to the signal.
    invoked = db.skills_invoked_by_session(conn) if kind == "skills" else {}
    ran: dict[str, int] = {}
    for loaded in invoked.values():
        for lowered in loaded:
            ran[lowered] = ran.get(lowered, 0) + 1
    # A skill can be proved to have run and be nowhere on this disk: it came
    # from a checkout that is gone, or from a machine that is not this one.
    # Dropping those rows is how a store where a third of the sessions ran
    # four skills could report that it had never seen them. Proof of work
    # outranks an empty directory, so they get a row and an honest label.
    missing = sorted(set(ran) - set(on_disk))
    names = [asset.name for asset in inventory] + missing
    conn_counts = db.reference_counts(conn, names) if names else {}
    # Where the ones this repository cannot load actually live. "Not
    # installed" is true and useless on its own: every one of them ran
    # somewhere, and the question it leaves you with is where.
    elsewhere = (context.locate(set(missing), db.session_directories(conn))
                 if missing else {})
    conn.close()
    return {
        "inventory": inventory, "on_disk": on_disk, "ran": ran,
        "missing": missing, "names": names, "counts": conn_counts,
        "elsewhere": elsewhere,
    }


def _skills_by_place(conn) -> list[dict]:
    """Which skills ran where, one entry per checkout, busiest first.

    The other half of the inventory. `cs skills` answers "what can be loaded
    here"; this answers "what did I actually reach for, and where" — which
    for a store spread over sixty repositories is a different question with a
    different answer.

    Grouped by repository where the store recorded one, and by directory
    where it did not. Grouping by directory alone splits a repository into a
    row per folder you happened to be standing in — this store had one
    appearing four times, three of the rows reporting that it shipped none of
    the skills it plainly ships, because a subdirectory has no `.github`.

    What a group ships is the union over every directory in it, for the same
    reason: the skills are the repository's wherever inside it you were.
    """
    from collections import Counter

    invoked = db.skills_invoked_by_session(conn)
    places = db.session_places(conn)
    found: dict[str, dict] = {}
    for session_id, loaded in invoked.items():
        directory, repository = places.get(session_id, ("", ""))
        if not directory:
            continue          # a session the store cannot place
        place = found.setdefault(repository or directory, {
            "name": repository or Path(directory).name,
            "directories": set(), "sessions": 0, "skills": Counter(),
        })
        place["sessions"] += 1
        place["skills"].update(loaded)
        place["directories"].add(directory)
    for place in found.values():
        place["ships"] = set().union(
            *(context.ships(directory) for directory in place["directories"])
        ) if place["directories"] else set()
    return sorted(found.values(),
                  key=lambda place: (-place["sessions"], place["name"]))


def _render_assets_by_place(limit: int = 20) -> None:
    """`cs skills --by-repo` — where each skill was actually reached for."""
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    conn = db.connect()
    places = _skills_by_place(conn)
    conn.close()

    print()
    if not places:
        print(ui.rule(inner, "Skills by checkout"))
        print()
        print(f"  {ui.MUTED}No session in this store recorded loading a "
              f"skill.{ui.RST}")
        print()
        return

    total = sum(len(place["skills"]) for place in places)
    own = sum(1 for place in places for name in place["skills"]
              if name in place["ships"])
    print(ui.rule(inner, f"Skills by checkout · {len(places)}"))
    print()
    print(ui.field("places", f"{len(places)} checkouts ran a skill", 11))
    print(ui.field("borrowed", f"{total - own} of {total} came from outside "
                               f"the checkout that used them", 11))
    print()

    for place in places[:limit]:
        ran = place["skills"].most_common()
        carried = sum(1 for name, _ in ran if name in place["ships"])
        # 'all its own' is worth saying plainly: it is the state a repository
        # should be in, and it is rare enough here to be news.
        shape = (f"{len(ran)} skill{'s' if len(ran) != 1 else ''} · "
                 + ("all its own" if carried == len(ran)
                    else f"{carried} its own · {len(ran) - carried} borrowed"
                    if carried else "none of its own"))
        print(f"    {ui.MINT}{place['sessions']:>4}{ui.RST}  "
              f"{ui.BOLD}{ui._fit(place['name'], inner - 34):<{inner - 34}}"
              f"{ui.RST} {ui.MUTED}{shape}{ui.RST}")
        # The skills themselves, on their own line: a repository with ten of
        # them would otherwise push its own name off the window.
        named = ", ".join(
            f"{name} {count}" + ("" if name in place["ships"] else "*")
            for name, count in ran[:6]
        )
        if len(ran) > 6:
            named += f", +{len(ran) - 6}"
        # A skill called `test-ainb` is one word; breaking it at the hyphen
        # makes it read as two skills that do not exist.
        for line in textwrap.wrap(named, inner - 10, break_on_hyphens=False):
            print(f"          {ui.MUTED}{line}{ui.RST}")
        print()

    if len(places) > limit:
        print(f"    {ui.MUTED}… and {len(places) - limit} more{ui.RST}")
        print()
    _why("Sessions that loaded a skill, grouped by the directory they ran "
         "in — the CLI's own load marker, so this is recorded rather than "
         "inferred. A checkout is named by the repository the store saw most "
         "often for it, because a long-lived one collects several as its "
         "remote is renamed. '*' marks a skill the checkout does not ship: "
         "it worked because of what you had installed, and a colleague "
         "cloning the repository would not get it.", inner)
    print()


def _render_assets(kind: str, limit: int, column: str = "sessions",
                   descending: bool = True) -> None:
    gathered = _asset_inventory(kind)
    inventory, on_disk = gathered["inventory"], gathered["on_disk"]
    ran, missing = gathered["ran"], gathered["missing"]
    names, counts = gathered["names"], gathered["counts"]
    elsewhere = gathered["elsewhere"]
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4

    print()
    if not names:
        print(ui.rule(inner, f"{kind.title()}"))
        print()
        # One path per line, like the other two empty states. Joined with
        # dots they made a single 160-character line on any machine with a
        # long home directory, which is most of them.
        print(f"  {ui.MUTED}None found. cs looks for them in:{ui.RST}")
        for folder in _asset_dirs(kind):
            print(f"    {ui.MUTED}{ui._fit(str(folder), inner - 2)}{ui.RST}")
        print()
        return

    counted = [(name, counts.get(name, 0)) for name in names]
    used = _sort_report([r for r in counted if r[1]], "assets", column, descending)
    switched_off = [asset.name for asset in inventory if asset.disabled]
    idle = [name for name, seen in counted
            if not seen and not on_disk[name.lower()].disabled]

    print(ui.rule(inner, f"{kind.title()} · {len(inventory)} on disk"))
    print()
    # Where they came from, not just how many. A bare total hid the bug this
    # split was added for: standing in a repository with twenty skills of its
    # own, the inventory only ever counted the personal ones.
    scopes = Counter(asset.scope for asset in inventory)
    shelf = " · ".join(
        f"{scopes[scope]} {label}" for scope, label in (
            ("project", "from this repo"),
            ("personal", "personal"),
            ("plugin", "from plugins"),
            ("builtin", "built in"),
        ) if scopes.get(scope)
    )
    print(ui.field("installed",
                   f"{len(inventory)}  ({shelf})" if shelf
                   else str(len(inventory)), 11))
    if problem := context.settings_problem():
        # Said out loud, because "nothing is switched off" and "I could not
        # find out what is switched off" otherwise draw the same table.
        print(ui.field("enablement", f"{problem} — none treated as off", 11))
    if switched_off:
        # Not neglect. A skill in disabledSkills was a decision, and filing
        # it under 'never referenced' read as an accusation: eleven of the
        # sixteen this view used to shame were switched off on purpose.
        print(ui.field("disabled", f"{len(switched_off)} switched off in "
                                   f"settings.json", 11))
    print(ui.field("referenced", f"{len(used)} appear in at least one session", 11))
    if ran:
        # 'here' rather than 'no longer': a skill absent from this shelf may
        # be alive and well in another repository. What is true is that
        # Copilot could not load it standing in this one.
        strangers = len(missing) - len(elsewhere)
        print(ui.field("ran", f"{len(ran)} run by the CLI"
                              + (f" · {len(elsewhere)} from another checkout"
                                 if elsewhere else "")
                              + (f" · {strangers} not on this disk"
                                 if strangers else ""), 11))
    print(ui.field("idle", f"{len(idle)}" + (" enabled," if switched_off else "")
                           + " never referenced", 11))
    print()

    def _note(name: str) -> str:
        """What to say beside a row, plain — the widths are measured on this.

        One fact each, in order of what changes what you would do next: a
        skill that is not installed cannot be run, one that is switched off
        will not be, and after that the recorded load is the strongest thing
        there is to say.
        """
        if name.lower() not in on_disk:
            # Named, when the store knows a checkout that ships it. The bare
            # form is reserved for the ones that really are nowhere.
            found = elsewhere.get(name.lower())
            return f" · in {found}" if found else " · not installed"
        if on_disk[name.lower()].disabled:
            return " · off"
        if not ran:
            return ""          # no marker anywhere: the column does not apply
        return f" · {ran[name.lower()]} ran" if ran.get(name.lower()) else " · none"

    def _paint(name: str) -> str:
        """The same note, coloured."""
        plain = _note(name)
        if not plain:
            return ""
        colour = (ui.AMBER if plain == " · not installed"
                  else ui.MUTED if plain in (" · off", " · none")
                  else ui.VIOLET if plain.startswith(" · in ") else ui.MINT)
        return f" {colour}{plain.lstrip()}{ui.RST}"

    if used:
        title = "Most referenced" if column == "sessions" and descending else "Referenced"
        print(ui.heading(f"{title} {kind} · by {column}", ui.ACCENT, inner))
        peak = max(n for _, n in used)
        # What the note column costs, if it is drawn at all. It used to cost
        # nothing in this sum and be printed anyway, so every row carrying a
        # '· N ran' ran nine columns off the right edge of a 72-column
        # window — the bar was sized as though the note were not there.
        rows = used[:limit]
        load = max((len(_note(name)) for name, _ in rows), default=0)
        # Name and bar split what the window has, instead of a fixed 34 and
        # 16 that ran off anything under 64 columns. The bar gives way first:
        # which skill it is matters more than how long its bar is.
        span, gauge = _chart_spans(inner, 11 + load, name_cap=36)
        for name, sessions in rows:
            print(
                f"    {ui.MINT}{sessions:>4}{ui.RST}  {ui._fit(name, span):<{span}}"
                f" {ui.bar(sessions, peak, gauge, pad=bool(load))}{_paint(name)}".rstrip()
            )
        if len(used) > limit:
            print(f"    {ui.MUTED}… and {len(used) - limit} more{ui.RST}")
        print()

    if idle:
        print(ui.heading(f"Never referenced · {len(idle)}", ui.AMBER, inner))
        for line in _name_grid(idle, inner):
            print(f"{ui.MUTED}{line}{ui.RST}")
        if len(idle) > 24:
            print(f"    {ui.MUTED}… and {len(idle) - 24} more{ui.RST}")
        print()

    if switched_off:
        print(ui.heading(f"Switched off · {len(switched_off)}", ui.DIM, inner))
        for line in _name_grid(switched_off, inner):
            print(f"{ui.MUTED}{line}{ui.RST}")
        if len(switched_off) > 24:
            print(f"    {ui.MUTED}… and {len(switched_off) - 24} more{ui.RST}")
        print()

    print(ui.field("drill down", f"cs {kind if kind == 'skills' else 'profiles'} <name>"))
    print()
    _why(f"Counts are sessions that reference the {kind[:-1]} in a qualified "
          f"way — a path, a /command, backticks, or the word 'skill'/'agent' "
          f"beside it: a usage signal, not a call count. Where the CLI wrote "
          f"its own load marker into the turn, '{'· N ran'}' says so — that "
          f"part is recorded rather than inferred. '· none' means no load was "
          f"recorded, which is weaker than it sounds: the CLI only started "
          f"writing that marker partway through this store's life, so an "
          f"older session that did run one leaves no trace of it. What is "
          f"installed is what Copilot could load standing here — this "
          f"repository's, your own, and the enabled plugins' — because that "
          f"is the set it resolves from. '· off' is in settings.json. '· in "
          f"<name>' is the checkout that ships it — Copilot would load it "
          f"standing there, not here — and '· not installed' is the rest: "
          f"it ran somewhere this disk no longer has.", inner)
    print(_sort_note("assets", column, descending, inner))
    _why_hint(inner)
    print()


def cmd_instructions() -> bool:
    """The instruction files every session in this repo starts with."""
    return _page(_capture(_render_instructions))


def _render_instructions() -> None:
    """Instruction files on disk, and which of them the model won't read whole.

    `cs skills` asks which files a session referenced. Nothing references an
    instruction file — it is loaded before you type — so the only questions
    left are which ones are loaded, from where, and whether each is short
    enough to survive the trip.
    """
    found = context.audit()
    items = [item for item in found["items"] if item.kind == "instructions"]
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4

    print()
    print(ui.rule(inner, f"Instructions · {len(items)} on disk"))
    print()
    if not items:
        print(f"  {ui.MUTED}None found. cs looks for them in:{ui.RST}")
        print()
        for path in context.instruction_paths():
            short = hooks.short(str(path), keep=max(24, inner - 6))
            print(f"    {ui.CODE}{ui._fit(short, inner - 2)}{ui.RST}")
        print()
        _note("A repository with no instruction file starts every session by "
              "explaining itself again. Anything you would say twice belongs "
              "in one.", inner)
        print()
        return

    for scope, root in (("project", found["root"]),
                        ("personal", found["personal_root"])):
        scoped = [item for item in items if item.scope == scope]
        chars = sum(item.chars for item in scoped)
        shape = (f"{len(scoped)} file{'s' if len(scoped) != 1 else ''} · "
                 f"{chars:,} chars" if scoped else "nothing")
        print(ui.field(scope, ui.trunc(shape, inner - 11)))
        # Under its own value, at `field`'s hanging column. Elided from the
        # middle rather than cut short: a checkout deep enough to overflow
        # the window is identified by its last two segments, never its first.
        root_text = hooks.short(str(root), keep=max(24, inner - 13))
        print(f"           {ui.MUTED}{ui._fit(root_text, inner - 11)}{ui.RST}")
    print(ui.field("limit", f"{context.INSTRUCTION_LIMIT:,} chars per file "
                            f"before Copilot truncates"))
    print()

    print(ui.heading(f"Loaded before your first prompt · {len(items)}",
                     ui.ACCENT, inner))
    columns = [("scope", "scope", "<"), ("file", "file", "<"),
               ("chars", "chars", ">"), ("lines", "lines", ">"),
               ("headings", "headings", ">")]
    # Only `scope` and the space after it are fixed; the file name is the
    # flexible column. The old figure reserved another eleven characters for
    # nothing, and the rule under the headings came up eleven short of the
    # section hairline over them.
    # Dropped worst-first, and `chars` is worth the most here: it is the
    # number the limit applies to and the reason the view exists. It used to
    # be the first column to go, so a narrow window kept the heading count
    # and lost the only figure that decides whether a file is read whole.
    spans = _fit_columns(inner - 2, 9,
                         [("headings", 9), ("lines", 6), ("chars", 8)],
                         least=20, flex="file", gaps=_extra_gaps(columns))
    spans.update(scope=8)
    shown = [spec for spec in columns if spans[spec[0]]]
    heads = _row(shown, spans)
    _head_rule(heads)
    for item in items:
        colour = ui.ROSE if item.oversized else ""
        values = {
            "scope": (item.scope, ui.MUTED),
            "file": (item.label, colour),
            "chars": (f"{item.chars:,}", colour),
            "lines": (str(item.lines), ui.MUTED),
            "headings": (str(item.headings) if item.headings else "·", ui.MUTED),
        }
        print("    " + _row(shown, spans, values).rstrip())
    print()

    # The two faults worth naming are the ones that change what the model
    # actually reads: a file past the limit loses its tail, and a long file
    # with no headings is read as one undifferentiated topic. They used to
    # float loose under the table with no heading over them, and each
    # oversized file repeated the same twenty-word remedy — so a checkout
    # with three long files said "move the scoped rules into…" three times.
    oversized = [item for item in items if item.oversized]
    unsectioned = [item for item in items if not item.oversized and item.unsectioned]
    if oversized or unsectioned:
        faults = len(oversized) + len(unsectioned)
        print(ui.heading(f"Not read as written · {faults}",
                         ui.ROSE if oversized else ui.AMBER, inner))
        for item in oversized:
            over = item.chars - context.INSTRUCTION_LIMIT
            print(f"    {ui.ROSE}● {item.scope} {item.label}{ui.RST}")
            _item(f"{item.chars:,} characters — the last {over:,} are past the "
                  f"limit and are not read.",
                  inner - 6, marker="→", colour=ui.ROSE, indent=6)
        for item in unsectioned:
            print(f"    {ui.AMBER}● {item.scope} {item.label}{ui.RST}")
            _item(f"{item.lines} lines with no headings — read as one "
                  f"undifferentiated topic.",
                  inner - 6, marker="→", colour=ui.AMBER, indent=6)
        print()
        if oversized:
            _note("Move the scoped rules into .github/instructions/"
                  "*.instructions.md, which load only when they match.",
                  inner, indent=4)
        if unsectioned:
            _note("Add ## sections — a model skims structure the same way "
                  "you do.", inner, indent=4)
        print()

    _note("Read from disk, not from the store: this is what your next session "
          "starts with, whatever the last one did. Nothing here is written or "
          "changed.", inner)
    print()
    _why("There is no 'referenced' count here as there is on Skills, and that "
         "is the point of the view: an instruction file is loaded before your "
         "first word, so every session got all of it that fit. What varies is "
         "how much fit.", inner)
    _why_hint(inner)
    print()


def cmd_hooks(event: str | None = None, sort_by: str | None = None,
              descending: bool | None = None) -> bool:
    """What Copilot will run around a session, or one event in full."""
    if event:
        return _page(_capture(lambda: _render_hook_event(event)))
    return _page_report(
        "hooks",
        lambda column, down: _capture(lambda: _render_hooks(column, down)),
        sort_by, descending,
    )


def _wrap_path(path: str, width: int) -> list[str]:
    """A path broken after its separators, never through a name in it.

    `textwrap` breaks wherever the column runs out, which turned
    `.copilot/settings.json` into `.copilot/setti` and `ngs.json` — two
    strings, neither of them a path and neither of them a name.
    """
    head, _, tail = path.rpartition("/")
    parts = ([segment + "/" for segment in head.split("/")] + [tail]
             if head or path.startswith("/") else [path])
    lines, line = [], ""
    for part in parts:
        if line and len(line) + len(part) > width:
            lines.append(line)
            line = ""
        while len(part) > width:  # one name longer than the whole row
            lines.append(part[:width])
            part = part[width:]
        line += part
    if line:
        lines.append(line)
    return lines or [""]


def _search_paths(places: list[tuple[object, str]], inner: int) -> None:
    """Where a kind of configuration can be declared, scope first.

    Both empty states printed `f"{path}  ({scope})"` truncated from the
    right, which cut the scope off the end of every workspace line — the one
    word that says whether the file belongs to you or to the repository. The
    scope leads in a fixed column, the home directory contracts to `~`, and
    what is left of a long path is elided from the middle, where the least
    of it is.
    """
    room = max(20, inner - 14)
    for path, scope in places:
        # Wrapped, never elided. This is the one screen whose whole job is to
        # tell you where to put the file, and half a path is not somewhere
        # you can put a file. Only the home directory contracts, to `~`.
        text = hooks.short(str(path), keep=10_000)
        for index, part in enumerate(_wrap_path(text, room)):
            label = scope if index == 0 else ""
            print(f"    {ui.MUTED}{label:<10}{ui.RST}{ui.CODE}{part}{ui.RST}")


def _hook_source(entry: dict) -> str:
    """Where a hook came from, short enough for a column."""
    path = entry["source"]
    if path.name == "settings.json":
        return f"{entry['scope'][:4]}:settings"
    return redact.one_line(path.name)


def _render_hooks(column: str = "when", descending: bool = False) -> None:
    entries, problems = hooks.load()
    switched_off = hooks.parked()
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4

    print()
    print(ui.rule(inner, f"Hooks · {len(entries)} commands"))
    print()
    if not entries:
        print(f"  {ui.MUTED}No hook is configured.{ui.RST}")
        print()
        _note("A hook is a command Copilot runs on the lifecycle — when a "
              "session starts, before a tool call, when the agent stops. "
              "cs looks for them in:", inner)
        print()
        _search_paths(hooks.search_paths(), inner)
        print()
        _print_hook_problems(problems, switched_off, inner)
        return

    by_event: Counter[str] = Counter(entry["event"] for entry in entries)
    files = {entry["source"] for entry in entries}
    absent = [entry for entry in entries if entry["missing"]]
    personal = sum(1 for entry in entries if entry["scope"] == "personal")

    print(ui.field("files", f"{len(files)} declaring {len(entries)} commands"))
    unknown = [name for name in by_event if name not in hooks.EVENTS]
    known = f"{len(by_event) - len(unknown)} of the {len(hooks.EVENTS)} cs knows"
    print(ui.field("events", known + (f" · {len(unknown)} it does not"
                                      if unknown else "")))
    print(ui.field("scope", f"{personal} personal · {len(entries) - personal} "
                            f"from this workspace"))
    if absent:
        print(ui.field("missing", f"{len(absent)} point at a script that is "
                                  f"not on disk"))
    print()

    # Lifecycle order, not commonest first: the question a hook list answers
    # is "what happens to my session, in what order", and sorting by count
    # shuffles the answer.
    print(ui.heading(f"When they run · {len(by_event)}", ui.ACCENT, inner))
    peak = max(by_event.values())
    for name in sorted(by_event, key=hooks.order):
        count = by_event[name]
        known = "" if name in hooks.EVENTS else f" {ui.AMBER}?{ui.RST}"
        print(f"    {ui.MINT}{count:>5}{ui.RST}  {ui.trunc(name, 22):<22}"
              f" {ui.bar(count, peak, 12)}{known}")
    print()

    if absent:
        print(ui.heading(f"Scripts that are gone · {len(absent)}", ui.ROSE, inner))
        print(f"    {ui.MUTED}"
              f"{ui.trunc('Copilot will still run these, and the shell will fail.', inner - 2)}"
              f"{ui.RST}")
        for entry in absent:
            gone = ui.trunc(redact.plain(str(entry["target"])), inner - 2)
            print(f"    {ui.ROSE}{gone}{ui.RST}")
            print(f"      {ui.MUTED}{entry['event']} · "
                  f"{ui.trunc(_hook_source(entry), inner - 20)}{ui.RST}")
        print()

    print(ui.heading(f"Every hook · {len(entries)}", ui.ACCENT, inner))
    entries = _sort_report(entries, "hooks", column, descending)
    # Fixed: indent 4, when 19+1. The matcher goes first when the window
    # narrows — most hooks have none — then where it was declared.
    columns = [("when", "when", "<"), ("tool", "tool", "<"),
               ("command", "runs", "<"), ("source", "from", "<")]
    spans = _fit_columns(inner - 2, 24, [("tool", 8), ("source", 14)],
                         least=24, flex="command", gaps=_extra_gaps(columns))
    spans.update(when=19)
    shown = [spec for spec in columns if spans[spec[0]]]
    heads = _row(shown, spans)
    _head_rule(heads, 4, column, _HOOKS_HEADS, descending)
    for entry in entries:
        values = {
            "when": (entry["event"], ui.SKY),
            "tool": (entry["matcher"] or "·",
                     "" if entry["matcher"] else ui.MUTED),
            # Masked like every other view: a hook command is a shell line,
            # and shell lines are where an exported token ends up.
            "command": (hooks.short(redact.redact(entry["command"])),
                        ui.ROSE if entry["missing"] else ""),
            "source": (_hook_source(entry), ui.MUTED),
        }
        print("    " + _row(shown, spans, values).rstrip())
    print()
    _print_hook_problems(problems, switched_off, inner)
    _why("Hooks are configuration, not history: the session store records "
          "no hook event, so this is what Copilot will run — never a count "
          "of what it did run.", inner)
    drill = ui.trunc("cs hooks <event> — one event, commands in full", inner - 2)
    print(f"  {ui.MUTED}{drill}{ui.RST}")
    print(_sort_note("hooks", column, descending, inner))
    _why_hint(inner)
    print()


def _print_hook_problems(problems: list, switched_off: list, inner: int) -> None:
    """Files that would have declared hooks, and don't — for two reasons."""
    if problems:
        print(ui.heading(f"Not loaded · {len(problems)}", ui.ROSE, inner))
        for path, why in problems:
            print(f"    {ui.ROSE}{ui.trunc(redact.plain(path.name), inner - 2)}{ui.RST}")
            print(f"      {ui.MUTED}{ui.trunc(why, inner - 6)}{ui.RST}")
        print()
    if switched_off:
        print(ui.heading(f"Switched off · {len(switched_off)}", ui.AMBER, inner))
        for path in switched_off:
            # Abbreviated from the middle: `.off` and `.bak` are the whole
            # story of a parked file, and truncating loses exactly that.
            short = hooks.short(redact.plain(str(path)), keep=max(24, inner - 6))
            print(f"    {ui.MUTED}{ui._fit(short, inner - 2)}{ui.RST}")
        print()


def _render_hook_event(event: str) -> None:
    """One event, with every command in full — the drill-down `cs hooks` offers."""
    entries, _ = hooks.load()
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    wanted = event.lower().replace("-", "").replace("_", "")
    matched = [
        entry for entry in entries
        if entry["event"].lower().startswith(wanted)
        or wanted in entry["event"].lower()
    ]
    print()
    if not matched:
        print(ui.rule(inner, f"Hooks · {event}"))
        print()
        print(f"  {ui.MUTED}No hook runs on '{event}'.{ui.RST}")
        names = sorted({entry["event"] for entry in entries}, key=hooks.order)
        if names:
            listed = ui.trunc(", ".join(names), inner - 22)
            print(f"  {ui.MUTED}configured events: {listed}{ui.RST}")
        print()
        return

    name = matched[0]["event"]
    label = "command" if len(matched) == 1 else "commands"
    print(ui.rule(inner, f"{name} · {len(matched)} {label}"))
    print()
    for position, entry in enumerate(matched, 1):
        colour = ui.ROSE if entry["missing"] else ui.SKY
        head = f"{position}. {_hook_source(entry)}"
        if entry["matcher"]:
            head += f" · on {entry['matcher']}"
        if entry["timeout"]:
            head += f" · {entry['timeout']}s limit"
        print(f"  {colour}{ui.trunc(head, inner)}{ui.RST}")
        _item(redact.redact(entry["command"]), inner - 2)
        if entry["missing"]:
            gone = f"{entry['target']} is not on disk"
            print(f"      {ui.ROSE}{ui.trunc(gone, inner - 6)}{ui.RST}")
        print()


def cmd_mcp(name: str | None = None, sort_by: str | None = None,
            descending: bool | None = None) -> bool:
    """MCP servers wired up for this session, or one of them in full."""
    if name:
        return _page(_capture(lambda: _render_mcp_server(name)))
    return _page_report(
        "mcp",
        lambda column, down: _capture(lambda: _render_mcp(column, down)),
        sort_by, descending,
    )


def _mcp_source(server: dict) -> str:
    """Where a server was declared, short enough for a column.

    Without the extension: every file this can name is JSON, so five of the
    twenty characters said nothing and cost the rest of the name.
    """
    name = redact.one_line(server["source"].name)
    return f"{server['scope'][:4]}:{name.removesuffix('.json')}"


def _mcp_endpoint(server: dict) -> str:
    """What the server actually is, with the part that never varies removed.

    Every remote endpoint began `https://`, which is eight columns of the
    same eight characters on every row and was pushing the host — the only
    thing that identifies the server — off the end of the column.
    """
    endpoint = redact.redact(server["endpoint"])
    for scheme in ("https://", "http://"):
        if endpoint.startswith(scheme):
            return endpoint[len(scheme):]
    return hooks.short(endpoint, keep=34)


def _mcp_tools(server: dict) -> str:
    """What this server is allowed to expose, in a column's worth of words."""
    if server["off"]:
        return "none"
    if server["all_tools"]:
        return "all" if not server["tools"] else f"all +{len(server['tools'])}"
    return str(len(server["tools"]))


def _render_mcp(column: str = "name", descending: bool = False) -> None:
    servers, problems = mcp.load()
    switched_off = mcp.parked()
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4

    print()
    print(ui.rule(inner, f"MCP servers · {len(servers)}"))
    print()
    if not servers:
        print(f"  {ui.MUTED}No MCP server is configured.{ui.RST}")
        print()
        _note("An MCP server is a tool source Copilot can call that is not "
              "its own — a local process, or an HTTP endpoint. cs looks for "
              "them in:", inner)
        print()
        _search_paths(mcp.search_paths(), inner)
        print()
        _print_mcp_problems(problems, switched_off, inner)
        return

    conn = db.connect()
    counts = db.mcp_reference_counts(conn, [s["name"] for s in servers])
    conn.close()
    for server in servers:
        server["sessions"] = counts.get(server["name"], 0)

    remote = [s for s in servers if s["transport"] != "local"]
    personal = sum(1 for s in servers if s["scope"] == "personal")
    absent = [s for s in servers if s["missing"]]
    open_ended = [s for s in servers if s["all_tools"] and not s["off"]]
    leaking = [s for s in servers if s["secrets"]]
    files = {s["source"] for s in servers}

    print(ui.field("files", f"{len(files)} declaring {len(servers)} servers"))
    # Local versus remote first, because it is the only line here that says
    # whether the conversation leaves this machine.
    print(ui.field("type", f"{len(servers) - len(remote)} local · "
                           f"{len(remote)} remote"))
    print(ui.field("scope", f"{personal} personal · {len(servers) - personal} "
                            f"from this workspace"))
    print(ui.field("tools", f"{len(open_ended)} of {len(servers)} expose "
                            f"everything the server offers"))
    if absent:
        print(ui.field("missing", f"{len(absent)} run a command that is not "
                                  f"on this machine"))
    print()

    print(ui.heading(f"Every server · {len(servers)}", ui.ACCENT, inner))
    ordered = _sort_report(servers, "mcp", column, descending)
    # Indent 4, then name, transport 9, tools 6 and their gaps. The endpoint
    # flexes, and `from` is the first column a narrow window drops — a handful
    # of files declare all of these, and the name identifies the server.
    # `least` is low because a truncated command still reads (the program is
    # at the front of it), and losing the session count to keep four more
    # characters of `npx -y @some/server` would be the wrong trade. The name
    # gives ground too rather than staying at 20 and pushing the row off a
    # small window.
    name_span = max(10, min(20, inner - 33,
                            max(ui.cells(s["name"]) for s in servers)))
    columns = [("name", "server", "<"), ("transport", "transport", "<"),
               ("endpoint", "runs", "<"), ("tools", "tools", ">"),
               ("sessions", "sessions", ">"), ("source", "from", "<")]
    # name + transport(9) + tools(6), each with the space after it: the old
    # figure was four columns too generous, so the rule under the headings
    # stopped short of the section hairline above them.
    spans = _fit_columns(inner - 2, name_span + 1 + 10 + 7,
                         [("source", 18), ("sessions", 9)],
                         least=14, flex="endpoint", gaps=_extra_gaps(columns))
    spans.update(name=name_span, transport=9, tools=6)
    shown = [spec for spec in columns if spans[spec[0]]]
    heads = _row(shown, spans)
    _head_rule(heads, 4, column, _MCP_HEADS, descending)
    for server in ordered:
        # A remote server is coloured because it is the one that matters: a
        # local command is code you already have, an https endpoint is your
        # session going somewhere else.
        transport = (ui.AMBER if server["transport"] != "local"
                     else ui.MUTED)
        values = {
            "name": (server["name"], ui.ROSE if server["missing"] else ui.SKY),
            "transport": (server["transport"], transport),
            # Masked like every other view: a config line is where a pasted
            # token ends up, and this one is read out of the repository.
            "endpoint": (_mcp_endpoint(server),
                         ui.ROSE if server["missing"] else ""),
            "tools": (_mcp_tools(server),
                      ui.AMBER if server["all_tools"] else ui.MUTED),
            "sessions": (str(server["sessions"]) if server["sessions"] else "·",
                         "" if server["sessions"] else ui.MUTED),
            "source": (_mcp_source(server), ui.MUTED),
        }
        print("    " + _row(shown, spans, values).rstrip())
    print()

    if leaking:
        print(ui.heading(
            ui._fit(f"Credentials written into the config · {len(leaking)}", inner),
            ui.ROSE, inner))
        warning = ("A literal value, not ${VAR} — so it is in the file, and "
                   "the file gets committed.")
        print(f"    {ui.MUTED}{ui.trunc(warning, inner - 2)}{ui.RST}")
        for server in leaking:
            keys = ", ".join(server["secrets"])
            print(f"    {ui.ROSE}{ui.trunc(server['name'], 20):<20}{ui.RST}"
                  f" {ui.MUTED}{ui.trunc(keys, inner - 26)}{ui.RST}")
        print()

    if absent:
        print(ui.heading(f"Commands that are gone · {len(absent)}", ui.ROSE, inner))
        warning = "Copilot will still try to start these, and the spawn will fail."
        print(f"    {ui.MUTED}{ui.trunc(warning, inner - 2)}{ui.RST}")
        for server in absent:
            print(f"    {ui.ROSE}{ui.trunc(server['name'], inner - 2)}{ui.RST}")
            print(f"      {ui.MUTED}"
                  f"{ui.trunc(redact.redact(server['endpoint']), inner - 6)}{ui.RST}")
        print()

    idle = [s["name"] for s in servers if not s["sessions"]]
    if idle:
        print(ui.heading(f"Never referenced · {len(idle)}", ui.AMBER, inner))
        for line in _name_grid(idle, inner):
            print(f"{ui.MUTED}{line}{ui.RST}")
        if len(idle) > 24:
            print(f"    {ui.MUTED}… and {len(idle) - 24} more{ui.RST}")
        print()

    _print_mcp_problems(problems, switched_off, inner)
    _why("MCP servers are configuration, not history: the store records no "
          "MCP invocation event. The session counts are a text signal — a "
          "tool named mcp__server__tool, or the server named as one — so "
          "they are evidence a server was reached for, never a call count.",
          inner)
    drill = ui.trunc("cs mcp <name> — one server, and the sessions that named it",
                     inner - 2)
    print(f"  {ui.MUTED}{drill}{ui.RST}")
    print(_sort_note("mcp", column, descending, inner))
    _why_hint(inner)
    print()


def _print_mcp_problems(problems: list, switched_off: list, inner: int) -> None:
    """Files that would have declared a server, and don't — for two reasons."""
    if problems:
        print(ui.heading(f"Not loaded · {len(problems)}", ui.ROSE, inner))
        for path, why in problems:
            print(f"    {ui.ROSE}{ui.trunc(redact.plain(path.name), inner - 2)}{ui.RST}")
            print(f"      {ui.MUTED}{ui.trunc(why, inner - 6)}{ui.RST}")
        print()
    if switched_off:
        print(ui.heading(f"Switched off · {len(switched_off)}", ui.AMBER, inner))
        for path in switched_off:
            # Abbreviated from the middle, not truncated from the right: the
            # suffix is the whole point of the line — `.bak` and `.sample` are
            # different stories — and truncating loses exactly that.
            short = hooks.short(redact.plain(str(path)), keep=inner - 8)
            print(f"    {ui.MUTED}{ui.trunc(short, inner - 2)}{ui.RST}")
        print()


def _render_mcp_server(name: str) -> None:
    """One server in full — what it is, what it may call, who reached for it."""
    servers, _ = mcp.load()
    known = {server["name"]: server for server in servers}
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    match = next(
        (real for real in known if real.lower() == name.lower()),
        next((real for real in known if name.lower() in real.lower()), None),
    )
    print()
    if not match:
        print(ui.rule(inner, f"MCP servers · {redact.one_line(name)}"))
        print()
        print(f"  {ui.MUTED}No MCP server named '{redact.one_line(name)}' is "
              f"configured.{ui.RST}")
        if known:
            print(f"  {ui.MUTED}configured: "
                  f"{ui.trunc(', '.join(known), inner - 14)}{ui.RST}")
        print()
        return

    server = known[match]
    conn = db.connect()
    rows = db.sessions_for_mcp(conn, match)
    conn.close()

    print(ui.rule(inner, f"MCP servers · {match}",
                  note=f"{len(rows)} sessions"))
    print()
    print(ui.field("type", server["transport"]))
    print(ui.field("runs", ui.trunc(redact.redact(server["endpoint"]), inner - 11)))
    print(ui.field("scope", server["scope"]))
    declared = hooks.short(redact.plain(str(server["source"])),
                           keep=max(24, inner - 13))
    print(ui.field("declared", ui._fit(declared, inner - 11)))
    if server["off"]:
        print(ui.field("tools", "none — every tool is switched off"))
    elif server["all_tools"]:
        print(ui.field("tools", "every tool the server offers, including ones "
                                "it adds later"))
    else:
        print(ui.field("tools", ui.trunc(", ".join(server["tools"]), inner - 11)))
    print()

    if server["missing"]:
        print(f"  {ui.ROSE}The command is not on this machine — this server "
              f"will fail to start.{ui.RST}")
        print()
    if server["secrets"]:
        print(f"  {ui.ROSE}A literal credential is written into the config: "
              f"{ui.trunc(', '.join(server['secrets']), inner - 44)}{ui.RST}")
        print()

    if not rows:
        print(f"  {ui.MUTED}No session names it.{ui.RST}")
        print()
        return

    # The same table shape as every other listing, rather than two lines per
    # session with a full uuid and `cs show` repeated down the page: the id
    # is a column, and the command that takes one is named once underneath.
    columns = [("active", "last active", "<"), ("session", "session", "<"),
               ("summary", "summary", "<")]
    spans = _fit_columns(inner - 2, 10, [("active", 12)],
                         gaps=_extra_gaps(columns))
    spans.update(session=9)
    shown = [spec for spec in columns if spans[spec[0]]]
    heads = _row(shown, spans)
    print(ui.heading(f"Sessions that named it · {len(rows)}", ui.ACCENT, inner))
    _head_rule(heads)
    for session_id, summary, last_active in rows:
        values = {
            "active": (_when(last_active), ui.MUTED),
            "session": (session_id[:8], ui.SKY),
            "summary": (redact.redact(summary) or "(untitled)", ""),
        }
        print("    " + _row(shown, spans, values).rstrip())
    print()
    _hint("cs show <session> — one session's ledger", inner)
    print()


def cmd_context() -> bool:
    """What this repository hands the agent before you type anything.

    Reads the working directory rather than the session store, so it is the
    one Improve view with no time window: instruction files, prompts, skills,
    agent profiles and hooks are either on disk now or they are not.
    """
    return _page(_capture(_render_context))


def _render_context() -> None:
    found = context.audit()
    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    items = found["items"]

    print()
    print(ui.rule(inner, f"Context · {found['root'].name}"))
    print()
    for scope, root in (("project", found["root"]),
                        ("personal", found["personal_root"]),
                        ("plugin", found["plugin_root"]),
                        ("builtin", found["builtin_root"])):
        kinds = Counter(item.kind for item in items if item.scope == scope)
        # 'nothing' is worth saying for the two scopes you always have. A
        # plugin row on a machine with no plugins is furniture.
        if not kinds and scope in ("plugin", "builtin"):
            continue
        shape = " · ".join(f"{count} {kind}" for kind, count in
                           sorted(kinds.items())) or "nothing"
        print(ui.field(scope, ui.trunc(shape, inner - 11)))
        print(f"           {ui.MUTED}{ui.trunc(str(root), inner - 11)}{ui.RST}")
    print(ui.field("hooks", f"{len(found['hooks'])} commands on the lifecycle"))
    servers, _ = mcp.load()
    remote = sum(1 for server in servers if server["transport"] != "local")
    print(ui.field("mcp", f"{len(servers)} servers · {remote} remote"))
    print()

    problems = context.gaps(found)
    if problems:
        print(ui.heading(f"Gaps · {len(problems)}", ui.ACCENT))
        print()
        for severity, what, fix in problems:
            colour, _ = _LEVELS[severity]
            print(f"    {colour}● {severity:<7}{ui.RST}"
                  f"{ui.BOLD}{ui.trunc(what, inner - 13)}{ui.RST}")
            _item(fix, inner - 6, marker="→", colour=colour, indent=6)
            print()
    else:
        print(f"  {ui.MINT}Nothing missing that cs knows to look for.{ui.RST}")
        print()

    # Only instruction and prompt files are worth a row each. Skills and
    # agent profiles are an inventory, and cs already has two commands that
    # do inventories properly.
    listed = [item for item in items if item.kind in ("instructions", "prompts")]
    if items:
        print(ui.heading(f"Instruction files · {len(listed)}", ui.ACCENT))
        spans = _fit_columns(inner - 2, 20, [("chars", 8), ("lines", 6)],
                             least=20, flex="file")
        spans.update(scope=8, kind=12)
        columns = [("scope", "scope", "<"), ("kind", "kind", "<"),
                   ("file", "file", "<"), ("chars", "chars", ">"),
                   ("lines", "lines", ">")]
        shown = [spec for spec in columns if spans[spec[0]]]
        heads = " ".join(_cell(head, spans[key], align)
                         for key, head, align in shown)
        print(f"    {ui.MUTED}{heads}{ui.RST}")
        print(f"    {ui.MUTED}{'─' * len(heads)}{ui.RST}")
        for item in listed:
            colour = ui.ROSE if item.oversized else ""
            values = {
                "scope": (item.scope, ui.MUTED),
                "kind": (item.kind, ui.MUTED),
                "file": (item.label, colour),
                "chars": (f"{item.chars:,}", colour),
                "lines": (str(item.lines), ui.MUTED),
            }
            print("    " + " ".join(
                _cell(values[key][0], spans[key], align, values[key][1])
                for key, _head, align in shown
            ).rstrip())
        rest = len(items) - len(listed)
        if rest:
            _note(f"… and {rest} skills and agent profiles — "
                  f"cs skills · cs profiles", inner, indent=4)
        print()
    _note("Read from disk, not from the store: this is the setup your next "
          "session will start from, whatever the last one did. Nothing here "
          "is written or changed.", inner)
    print()


def cmd_agents(days: int = 30) -> bool:
    """Delegation and context churn: who actually did the work."""
    return _page(_capture(lambda: _render_agents(days)))


def _render_agents(days: int) -> None:
    conn = db.connect()
    if not db.has_delegation(conn):
        conn.close()
        print(
            "error: this session store does not record who initiated each call "
            "(no initiator/agent_id columns), so delegation cannot be measured",
            file=sys.stderr,
        )
        sys.exit(1)
    split = db.work_split(conn, days)
    conn.close()

    width = min(shutil.get_terminal_size().columns, 96)
    inner = width - 4
    rows = split.get("by_initiator") or []
    print()
    print(ui.rule(inner, f"Delegation · {_window_label(days)}"))
    print()
    if not rows:
        print(f"  {ui.MUTED}No AI usage recorded in this range.{ui.RST}")
        print()
        return

    total_calls = sum(calls for _, calls, _, _ in rows)
    total_nano = sum(nano for _, _, nano, _ in rows)
    print(ui.field("sessions", str(split["sessions"])))
    print(ui.field("calls", f"{total_calls:,}"))
    print(ui.field("tasks", f"{split['delegated_tasks']} handed to sub-agents"))
    print(ui.field("spend", f"{ui.fmt_aiu(total_nano)} AIU"))
    print()

    print(ui.heading("Who initiated the work", ui.ACCENT, inner))
    labels = {
        "user": ("you", ui.MINT),
        "agent": ("main agent", ui.ACCENT),
        "sub-agent": ("sub-agents", ui.VIOLET),
        "compaction": ("compaction", ui.AMBER),
        "unknown": ("unattributed", ui.MUTED),
    }
    # A named, ruled table like every other report, instead of a fixed-width
    # row that ran off any window under 86 columns. The bar is last and takes
    # whatever is left, so it is the only thing that changes size.
    spans = _fit_columns(inner - 2, 13,
                         [("sessions", 8), ("share", 5),
                          ("spend", 9), ("calls", 7)], least=6)
    spans["who"] = 12
    columns = [
        ("who", "who", "<"), ("calls", "calls", ">"), ("spend", "spend", ">"),
        ("share", "share", ">"), ("sessions", "sessions", ">"),
        ("summary", "", "<"),
    ]
    layout = [spec for spec in columns if spans[spec[0]]]
    heads = " ".join(_cell(head, spans[key], align) for key, head, align in layout)
    print(f"    {ui.MUTED}{heads.rstrip()}{ui.RST}")
    print(f"    {ui.MUTED}{'─' * len(heads)}{ui.RST}")
    peak = max((calls for _, calls, _, _ in rows), default=0)
    for name, calls, nano, sessions in rows:
        label, colour = labels.get(name, (name, ui.MUTED))
        # At least one block for any non-zero count: a blank cell beside 2,115
        # calls reads as nothing happening, which is the opposite of the truth.
        filled = int(calls / peak * spans["summary"]) if peak else 0
        bar = "█" * max(1, filled) if calls else ""
        values = {
            "who": (label, ui.MUTED),
            "calls": (f"{calls:,}", ""),
            "spend": (ui.fmt_aiu(nano), ui.VIOLET),
            "share": (f"{nano / total_nano * 100:.0f}%" if total_nano else "-",
                      ui.MUTED),
            "sessions": (str(sessions), ui.MUTED),
            "summary": (bar, colour),
        }
        print("    " + " ".join(
            _cell(values[key][0], spans[key], align, values[key][1])
            for key, _head, align in layout
        ).rstrip())
    print()

    note = ("A 'task' is one delegation. The store records no agent name — "
            "agent_id is the delegating tool-call id.")
    for line in textwrap.wrap(note, max(30, inner)):
        print(f"  {ui.MUTED}{line}{ui.RST}")
    print()

    if split["top_delegating"]:
        print(ui.heading("Sessions that delegate most", ui.VIOLET, inner))
        # One row per session, not two: the id used to appear only in a hint
        # line under each row, which doubled the height of the block and left
        # nothing to scan down.
        top = _fit_columns(inner - 2, 10, [("calls", 6), ("tasks", 6)])
        top["session"] = 9
        top_columns = [
            ("session", "session", "<"), ("calls", "calls", ">"),
            ("tasks", "tasks", ">"), ("summary", "summary", "<"),
        ]
        top_layout = [spec for spec in top_columns if top[spec[0]]]
        top_heads = " ".join(
            _cell(head, top[key], align) for key, head, align in top_layout)
        print(f"    {ui.MUTED}{top_heads.rstrip()}{ui.RST}")
        print(f"    {ui.MUTED}{'─' * len(top_heads)}{ui.RST}")
        for sid, summary, delegated, tasks, _nano in split["top_delegating"]:
            values = {
                "session": (sid[:8], ui.SKY),
                "calls": (f"{delegated:,}", ui.VIOLET),
                "tasks": (str(tasks), ui.ACCENT),
                "summary": (
                    redact.one_line(redact.redact(summary)) or "(untitled)",
                    "",
                ),
            }
            print("    " + " ".join(
                _cell(values[key][0], top[key], align, values[key][1])
                for key, _head, align in top_layout
            ).rstrip())
        print()
        print(f"    {ui.MUTED}cs show <id> — one session's ledger{ui.RST}")
        print()

    compaction = next((r for r in rows if r[0] == "compaction"), None)
    if compaction and total_nano:
        churn = (f"Compaction is context being re-summarised: {compaction[1]} "
                 f"events cost {ui.fmt_aiu(compaction[2])} AIU "
                 f"({compaction[2] / total_nano * 100:.0f}% of spend).")
        for line in textwrap.wrap(churn, max(30, inner)):
            print(f"  {ui.MUTED}{line}{ui.RST}")
        print()
