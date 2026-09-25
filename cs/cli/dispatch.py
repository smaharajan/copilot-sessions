"""Argument parsing, ``run`` / ``main``, and command dispatch."""

from __future__ import annotations

import os
import sys

from .. import (
    __version__,
    db,
    export,
    ui,
)
from ._common import (
    _REPORT_COLUMNS,
    _SORT_COLUMNS,
    _SORT_NAMES,
    _visible,
    _with_assets,
)
from .analysis import (
    _config_usage,
    cmd_anomalies,
    cmd_diff,
    cmd_health,
    cmd_patterns,
    cmd_replay,
)
from .evidence import (
    cmd_endings,
    cmd_failures,
    cmd_subagents,
    cmd_switches,
)
from .governance import cmd_audit, cmd_handoff, cmd_yolo
from .home import cmd_help, cmd_home
from .inventory import (
    _asset_inventory,
    _skills_by_place,
    cmd_agents,
    cmd_assets,
    cmd_context,
    cmd_hooks,
    cmd_instructions,
    cmd_mcp,
)
from .listing import cmd_files, cmd_recent, cmd_search
from .practice_cmds import cmd_coach, cmd_rhythm, cmd_standup
from .reports import (
    cmd_cost,
    cmd_efficiency,
    cmd_repos,
    cmd_stats,
    cmd_timeline,
)
from .resume import cmd_resume
from .session import (
    cmd_brief,
    cmd_export,
    cmd_read,
    cmd_show,
)
from .today import (
    cmd_asks,
    cmd_cleanup,
    cmd_eod,
    cmd_file_history,
    cmd_next,
    cmd_saved,
    cmd_similar,
    cmd_weekly,
)
from .workflow import (
    budget_check,
    cmd_budget,
    cmd_note,
    cmd_pin,
    cmd_pins,
    cmd_tag,
    cmd_unpin,
    cmd_untag,
)

_COMPLETION_COMMANDS = (
    "home", "recent", "all", "search", "repos", "stats", "cost", "efficiency",
    "agents", "timeline", "yolo", "handoff", "audit", "skills", "profiles",
    "instructions", "hooks", "mcp", "standup", "daily", "coach", "rhythm",
    "context", "pin", "unpin", "pins", "note", "tag", "untag", "budget",
    "failures", "loops", "subagents", "switches", "endings",
    "next", "eod", "weekly", "similar", "asks", "saved", "cleanup",
    "diff", "replay", "anomalies", "health", "patterns",
    "show", "brief", "read",
    "export", "files", "resume", "help", "version",
)


_COMPLETION_FLAGS = (
    "--json", "--csv", "--sort", "--asc", "--desc",
    "--all", "--turn", "--asks", "--short", "--by-repo", "--loops",
    "--md", "--save", "--repo", "--history", "--check",
)


def cmd_completion(shell: str) -> None:
    """Print a completion script for bash, zsh or fish.

    Small, static, and generated from the same command list the dispatcher
    uses, so a subcommand cannot exist without being completable. Nothing
    here shells back into `cs` to compute candidates: a completion that runs
    a program on every Tab is a completion that makes the shell feel broken
    the first time the store is large.
    """
    names = " ".join(_COMPLETION_COMMANDS)
    flags = " ".join(_COMPLETION_FLAGS)
    if shell == "bash":
        script = f"""# cs completions for bash — add to ~/.bashrc:
#   source <(cs completion bash)
_cs_complete() {{
    local cur=${{COMP_WORDS[COMP_CWORD]}}
    if [ "$COMP_CWORD" -eq 1 ]; then
        COMPREPLY=($(compgen -W "{names}" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "{flags}" -- "$cur"))
    fi
}}
complete -F _cs_complete cs
"""
    elif shell == "zsh":
        script = f"""# cs completions for zsh — add to ~/.zshrc:
#   source <(cs completion zsh)
_cs() {{
    local -a commands
    commands=({names})
    if (( CURRENT == 2 )); then
        compadd -- $commands
    else
        compadd -- {flags}
    fi
}}
compdef _cs cs
"""
    elif shell == "fish":
        lines = [
            "# cs completions for fish — save to "
            "~/.config/fish/completions/cs.fish:",
            "#   cs completion fish > ~/.config/fish/completions/cs.fish",
            "complete -c cs -f",
        ]
        lines += [
            f"complete -c cs -n __fish_use_subcommand -a {name}"
            for name in _COMPLETION_COMMANDS
        ]
        lines += [
            "complete -c cs -l json -d 'machine-readable output'",
            "complete -c cs -l csv -d 'comma-separated output'",
            "complete -c cs -l sort -d 'sort column'",
            "complete -c cs -l asc -d 'ascending'",
            "complete -c cs -l desc -d 'descending'",
            "complete -c cs -l short -d 'the story, without the inventory'",
            "complete -c cs -l asks -d 'list every request in order'",
            "complete -c cs -l turn -d 'one turn of a transcript'",
            "complete -c cs -l all -d 'every record, not just the window'",
            "complete -c cs -l by-repo -d 'skills grouped by where they ran'",
            "complete -c cs -l loops -d 'stuck loops rather than failures'",
            "complete -c cs -l md -d 'paste-ready Markdown'",
            "complete -c cs -l save -d 'save this search under a name'",
            "complete -c cs -l repo -d 'only sessions in this repository'",
            "complete -c cs -l history -d 'every touch of the file'",
            "complete -c cs -l check -d 'exit 1 when over the daily budget'",
        ]
        script = "\n".join(lines) + "\n"
    else:
        print(
            f"error: unknown shell '{shell}' — choose from: bash, zsh, fish",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.stdout.write(script)


def _turn_option(rest: list[str]) -> tuple[int | None, str | None]:
    """Read '--turn N' (or '--turn=N') from what follows a session reference."""
    i = 0
    turn = None
    while i < len(rest):
        arg = rest[i]
        if arg == "--turn":
            i += 1
            if i >= len(rest):
                return None, "missing value for --turn"
            value = rest[i]
        elif arg.startswith("--turn="):
            value = arg.split("=", 1)[1]
        else:
            return None, f"unknown option '{arg}'"
        if not value.lstrip("#").isdigit():
            return None, f"--turn wants a number, not '{value}'"
        turn = int(value.lstrip("#"))
        i += 1
    return turn, None


def _listing_options(
    rest: list[str], *, term: bool = False, default_days: int = 7
) -> tuple[int, str | None, bool | None, str | None, str | None]:
    days = default_days
    sort_by = None
    descending = None
    words: list[str] = []
    value = None
    i = 0
    while i < len(rest):
        arg = rest[i]
        if term and not arg.startswith("-"):
            # Every bare word joins the query: 'cs search entra group removal'
            # is one search, not a search plus two stray arguments.
            words.append(arg)
        elif not term and i == 0 and arg.lower() == "all":
            days = 0          # every session ever recorded
        elif not term and i == 0 and arg.removeprefix("-").isdigit():
            days = int(arg)
            if days < 1:
                return days, sort_by, descending, None, "days must be a positive integer"
        elif arg == "--sort":
            i += 1
            if i >= len(rest):
                return days, sort_by, descending, None, "missing value for --sort"
            sort_by = rest[i]
        elif arg.startswith("--sort="):
            sort_by = arg.split("=", 1)[1]
        elif arg == "--asc":
            if descending is True:
                return days, sort_by, descending, None, "use only one of --asc or --desc"
            descending = False
        elif arg == "--desc":
            if descending is False:
                return days, sort_by, descending, None, "use only one of --asc or --desc"
            descending = True
        else:
            return days, sort_by, descending, None, f"unknown option '{arg}'"
        i += 1
    value = " ".join(words) if words else None
    if term and value is None:
        return days, sort_by, descending, None, "missing search term"
    if sort_by and sort_by.lower() not in _SORT_COLUMNS:
        error = f"unknown sort column '{sort_by}' — choose from: {_SORT_NAMES}"
        return days, sort_by, descending, value, error
    return days, sort_by, descending, value, None


def _report_options(
    rest: list[str], report: str | None, *, days: bool = False,
    word: bool = False, flags: tuple[str, ...] = (),
) -> tuple[int | None, str | None, bool | None, str | None, set[str], str | None]:
    """Parse a report's arguments: [days|word] [--sort X] [--asc|--desc] [flags].

    Returns days, sort column, direction, the bare word, the flags that were
    given, and an error — reported the same way listings report theirs, so
    every command fails in one recognisable shape.

    `report` names the column set to check `--sort` against. It is None for
    `cs files`, which is a listing rather than a report and so is checked
    against the listing's columns by the caller.
    """
    day_count: int | None = None
    sort_by: str | None = None
    descending: bool | None = None
    value: str | None = None
    seen: set[str] = set()
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in flags:
            seen.add(arg)
        elif arg == "--sort":
            i += 1
            if i >= len(rest):
                return (day_count, sort_by, descending, value, seen,
                        "missing value for --sort")
            sort_by = rest[i]
        elif arg.startswith("--sort="):
            sort_by = arg.split("=", 1)[1]
        elif arg == "--asc":
            if descending is True:
                return (day_count, sort_by, descending, value, seen,
                        "use only one of --asc or --desc")
            descending = False
        elif arg == "--desc":
            if descending is False:
                return (day_count, sort_by, descending, value, seen,
                        "use only one of --asc or --desc")
            descending = True
        elif arg.startswith("-"):
            return (day_count, sort_by, descending, value, seen,
                    f"unknown option '{arg}'")
        elif days and arg.lower() == "all":
            day_count = 0     # every record, however old
        elif days and arg.isdigit() and (not word or len(arg) <= 4):
            # With a bare word also accepted (cs audit), short digit strings
            # are day counts and longer ones are session prefixes — an
            # eight-digit hex id is not "last 66666666 days".
            day_count = int(arg)
            if day_count < 1:
                return (day_count, sort_by, descending, value, seen,
                        "days must be a positive integer")
        elif word and value is None:
            value = arg
        else:
            return (day_count, sort_by, descending, value, seen,
                    f"unexpected argument '{arg}'")
        i += 1
    if report and sort_by and sort_by.lower() not in _REPORT_COLUMNS[report]:
        choices = ", ".join(_REPORT_COLUMNS[report])
        return (day_count, sort_by, descending, value, seen,
                f"unknown sort column '{sort_by}' — choose from: {choices}")
    return day_count, sort_by, descending, value, seen, None


def main(argv: list[str] | None = None) -> int:
    """Run a command. Ctrl-C anywhere exits quietly with the shell's 130."""
    try:
        return _dispatch(argv)
    except KeyboardInterrupt:
        print()
        return 130
    except BrokenPipeError:
        # `cs recent | head` closes the pipe early; that is not an error.
        # Nothing is done to sys.stdout here on purpose — main() is callable
        # from tests and from bin/cs, and redirecting the real fd would reach
        # far outside this function.
        return 0


def run() -> int:
    """Process entry point. main() plus the care a real process needs.

    Redirecting the fd on a broken pipe belongs here, not in main(): this
    only ever runs when cs *is* the process, so there is no caller's stream
    to disturb. Without it the interpreter prints a BrokenPipeError to
    stderr while flushing at shutdown — `cs recent | head` looked like it
    had failed when it had simply been cut off.
    """
    code = main()
    try:
        sys.stdout.flush()
    except BrokenPipeError:
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return 0
    return code


def _data_format(rest: list[str]) -> tuple[str | None, list[str], str | None]:
    """Pull `--json` / `--csv` out of a command's arguments.

    Returns the format asked for (or None), whatever is left for the command
    to parse, and an error when both were asked for at once — a request for
    two formats on one stream has no sensible answer, and picking one for the
    user would be a guess written to a file.
    """
    wanted = [flag for flag in ("--json", "--csv") if flag in rest]
    if len(wanted) > 1:
        return None, rest, "use only one of --json or --csv"
    if not wanted:
        return None, rest, None
    return wanted[0][2:], [a for a in rest if a not in wanted], None


def _emit_data(cmd: str, rest: list[str], fmt: str) -> int:
    """Answer a `--json` / `--csv` request, or explain that this view has none.

    Only the views whose answer is *figures* are here. `resume` runs a
    program, `read` is a conversation and `help` is prose — none of them have
    a data form, and inventing one for the sake of a uniform flag would mean
    maintaining an interface nobody asked for. Anything not listed says so,
    and names what can be asked for instead.
    """
    if cmd == "export":
        if fmt == "csv":
            print("error: a transcript has no CSV form — use 'cs export <#N|id>' "
                  "for Markdown or '--json' for structured turns", file=sys.stderr)
            return 1
        if not rest:
            print("error: export <#N|id> [--json]", file=sys.stderr)
            return 1
        cmd_export(rest[0], "json")
        return 0

    if cmd in ("recent", "list", "ls", "all"):
        show_all = cmd == "all"
        days, _sort, _desc, _term, error = _listing_options(
            rest, default_days=0 if show_all else 7
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        conn = db.connect()
        rows = db.recent_sessions(conn, days)
        conn.close()
        rows = _with_assets(_visible(rows, show_all))
        export.emit(export.sessions(days, show_all, rows), fmt)
        return 0

    if cmd in ("search", "find", "grep"):
        _days, _sort, _desc, term, error = _listing_options(rest, term=True)
        if error or not term:
            print(f"error: {error or 'search <words> — nothing to search for'}",
                  file=sys.stderr)
            return 1
        conn = db.connect()
        rows, hits = db.search(conn, term)
        conn.close()
        export.emit(export.search(term, _with_assets(rows), hits), fmt)
        return 0

    if cmd in ("skills", "profiles", "agent-profiles"):
        kind = "skills" if cmd == "skills" else "agents"
        if "--by-repo" in rest:
            if kind != "skills":
                print("error: --by-repo needs load markers, which only skills "
                      "leave — try 'cs skills --by-repo'", file=sys.stderr)
                return 1
            conn = db.connect()
            places = _skills_by_place(conn)
            conn.close()
            export.emit(export.assets_by_place(places), fmt)
            return 0
        gathered = _asset_inventory(kind)
        export.emit(export.assets(
            kind, gathered["names"], gathered["counts"],
            scopes={a.name: a.scope for a in gathered["inventory"]},
            states={a.name: a.state for a in gathered["inventory"]},
            ran=gathered["ran"],
            usage=_config_usage(kind),
        ), fmt)
        return 0

    if cmd == "repos":
        export.emit(export.repos(), fmt)
        return 0

    windowed = {
        "stats": (export.stats, None),
        "timeline": (export.timeline, 30),
        "cost": (export.cost, 30),
        "spend": (export.cost, 30),
        "efficiency": (export.efficiency, 30),
        "eff": (export.efficiency, 30),
        "agents": (export.delegation, 30),
        "delegation": (export.delegation, 30),
    }
    if cmd in windowed:
        build, fallback = windowed[cmd]
        days, _sort, _desc, _word, _flags, error = _report_options(
            rest, None, days=True
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        export.emit(build(fallback if days is None else days), fmt)
        return 0

    evidence_views = {
        "failures": export.failures, "fails": export.failures,
        "loops": export.loops, "subagents": export.subagents,
        "sub-agents": export.subagents, "switches": export.switches,
        "endings": export.endings,
    }
    if cmd in evidence_views:
        days, _sort, _desc, _word, flags, error = _report_options(
            rest, None, days=True, flags=("--loops",)
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        build = export.loops if "--loops" in flags else evidence_views[cmd]
        export.emit(build(30 if days is None else days), fmt)
        return 0

    if cmd in ("next", "eod", "weekly", "cleanup"):
        days, _sort, _desc, _word, _flags, error = _report_options(
            rest, None, days=cmd in ("next", "cleanup"), flags=("--md",)
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        builders = {"next": lambda: export.next_up(14 if days is None else days),
                    "eod": export.eod, "weekly": export.weekly,
                    "cleanup": lambda: export.cleanup(14 if days is None else days)}
        export.emit(builders[cmd](), fmt)
        return 0

    if cmd == "similar":
        _days, _sort, _desc, term, error = _listing_options(rest, term=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        export.emit(export.similar(term), fmt)
        return 0

    if cmd == "asks":
        repo, rest, error = _repo_option(rest)
        if not error:
            days, _sort, _desc, _word, _flags, error = _report_options(
                rest, None, days=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        export.emit(export.asks(30 if days is None else days, repo), fmt)
        return 0

    if cmd == "saved":
        export.emit(export.saved(), fmt)
        return 0

    if cmd in ("diff", "compare"):
        if len(rest) != 2:
            print("error: diff <#N|id> <#N|id> — two sessions", file=sys.stderr)
            return 1
        export.emit(export.diff(rest[0], rest[1]), fmt)
        return 0

    if cmd in ("anomalies", "patterns"):
        days, _sort, _desc, _word, _flags, error = _report_options(
            rest, None, days=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        build = export.anomalies if cmd == "anomalies" else export.patterns
        export.emit(build(30 if days is None else days), fmt)
        return 0

    if cmd == "health":
        repo, rest, error = _repo_option(rest)
        if error or rest:
            print(f"error: {error or 'unexpected argument ' + repr(rest[0])}",
                  file=sys.stderr)
            return 1
        export.emit(export.health(repo or "."), fmt)
        return 0

    if cmd == "files" and "--history" in rest:
        words = [arg for arg in rest if arg != "--history"]
        if not words:
            print("error: files <path> --history — which file", file=sys.stderr)
            return 1
        export.emit(export.file_history(" ".join(words)), fmt)
        return 0

    if cmd == "budget":
        export.emit(export.budget(), fmt)
        return 0

    if cmd in ("standup", "daily"):
        if fmt == "csv":
            print("error: standup has no CSV form — use '--json'",
                  file=sys.stderr)
            return 1
        days, _sort, _desc, _word, _flags, error = _report_options(
            rest, None, days=True
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        export.emit(export.standup(1 if days is None else days), fmt)
        return 0

    print(
        f"error: '{cmd}' has no data form — "
        f"--json and --csv work on: {', '.join(export.DATA_COMMANDS)}",
        file=sys.stderr,
    )
    return 1


def _dispatch(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]

    if not args:
        cmd_home()
        return 0

    cmd, rest = args[0], args[1:]

    # `--why` is global and stripped here rather than parsed per command,
    # because it changes how *every* report prints and not what any of them
    # computes. Threading it through twenty option parsers would be twenty
    # chances to forget it.
    global _WHY
    _WHY = "--why" in rest or os.environ.get("CS_WHY", "") not in ("", "0")
    rest = [a for a in rest if a != "--why"]

    # `--json` / `--csv` are answered before anything is drawn. A data request
    # is a different question from a view request — it has no window to page,
    # no cursor and no colour — so it takes its own path out rather than
    # threading a format flag through every renderer.
    fmt, rest, error = _data_format(rest)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if fmt:
        return _emit_data(cmd, rest, fmt)
    args = [cmd, *rest]

    if cmd in ("home", "menu"):
        cmd_home()
        return 0

    if cmd in ("recent", "list", "ls"):
        days, sort_by, descending, _, error = _listing_options(rest)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_recent(days, sort_by=sort_by, descending=descending)
    elif cmd == "all":
        # No window by default: "all" that meant "the last week" was the
        # single most confusing thing in the listing.
        days, sort_by, descending, _, error = _listing_options(rest, default_days=0)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_recent(days, show_all=True, sort_by=sort_by, descending=descending)
    elif cmd in ("search", "find", "grep"):
        name, rest, error = _save_option(rest)
        if not error:
            _, sort_by, descending, term, error = _listing_options(rest, term=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        if name:
            if not ui.save_search(name, term):
                print("error: could not write settings", file=sys.stderr)
                return 1
            print(f"  saved '{name}' · run it again with 'cs saved {name}'")
        cmd_search(term, sort_by=sort_by, descending=descending)
    elif cmd in ("show", "view", "info"):
        _require(rest, "show <#N|id> [--short] [--asks]")
        known = {"--short", "--brief", "--asks"}
        flags = [a for a in rest[1:] if a not in known]
        if flags:
            print(f"error: unknown option '{flags[0]}'", file=sys.stderr)
            return 1
        cmd_show(
            rest[0],
            short=bool({"--short", "--brief"} & set(rest[1:])),
            show_asks="--asks" in rest[1:],
        )
    elif cmd in ("brief", "digest", "summary"):
        _require(rest, "brief <#N|id> [--asks]")
        flags = [a for a in rest[1:] if a != "--asks"]
        if flags:
            print(f"error: unknown option '{flags[0]}'", file=sys.stderr)
            return 1
        cmd_brief(rest[0], show_asks="--asks" in rest[1:])
    elif cmd in ("read", "transcript"):
        _require(rest, "read <#N|id> [--turn N]")
        turn, error = _turn_option(rest[1:])
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_read(rest[0], turn)
    elif cmd == "files" and "--history" in rest:
        words = [arg for arg in rest if arg != "--history"]
        if not words:
            print("error: files <path> --history — which file", file=sys.stderr)
            return 1
        cmd_file_history(" ".join(words))
    elif cmd == "files":
        _, sort_by, descending, pattern, _, error = _report_options(
            rest, None, word=True
        )
        # A path is what this command is for; without one there is nothing to
        # look up, and the answer is the usage line rather than a leaderboard.
        if not error and not pattern:
            error = "files <path> — which sessions touched a file"
        if not error and sort_by and sort_by.lower() not in _SORT_COLUMNS:
            error = (f"unknown sort column '{sort_by}' — "
                     f"choose from: {', '.join(_SORT_COLUMNS)}")
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_files(pattern, sort_by=sort_by, descending=descending)
    elif cmd == "completion":
        _require(rest, "completion <bash|zsh|fish>")
        cmd_completion(rest[0])
    elif cmd == "export":
        _require(rest, "export <#N|id> [--json]")
        cmd_export(rest[0])
    elif cmd in ("resume", "r"):
        _require(rest, "resume <#N|id>")
        cmd_resume(rest[0])
    elif cmd == "repos":
        _, sort_by, descending, _, _, error = _report_options(rest, "repos")
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_repos(sort_by=sort_by, descending=descending)
    elif cmd == "stats":
        days, _, _, _, _, error = _report_options(rest, None, days=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_stats(days)
    elif cmd == "timeline":
        days, sort_by, descending, _, _, error = _report_options(
            rest, "timeline", days=True
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_timeline(30 if days is None else days,
                     sort_by=sort_by, descending=descending)
    elif cmd in ("cost", "spend"):
        days, sort_by, descending, _, _, error = _report_options(
            rest, "cost", days=True
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_cost(30 if days is None else days,
                 sort_by=sort_by, descending=descending)
    elif cmd in ("efficiency", "eff"):
        days, _, _, _, _, error = _report_options(rest, None, days=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_efficiency(30 if days is None else days)
    elif cmd in ("agents", "delegation"):
        days, _, _, _, _, error = _report_options(rest, None, days=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_agents(30 if days is None else days)
    elif cmd in ("yolo", "autonomy"):
        _, sort_by, descending, _, seen, error = _report_options(
            rest, "yolo", flags=("--all", "-a")
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_yolo(show_all=bool(seen), sort_by=sort_by, descending=descending)
    elif cmd in ("handoff", "handoffs"):
        _, sort_by, descending, ref, _, error = _report_options(
            rest, "handoff", word=True
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_handoff(ref, sort_by=sort_by, descending=descending)
    elif cmd in ("audit", "secrets", "security"):
        days, sort_by, descending, ref, _, error = _report_options(
            rest, "audit", days=True, word=True
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        # Session-scoped: full scan. Unscoped: default 30 days (0 = all time).
        if ref:
            cmd_audit(ref, sort_by=sort_by, descending=descending)
        else:
            cmd_audit(
                None, sort_by=sort_by, descending=descending,
                days=30 if days is None else days,
            )
    elif cmd in ("standup", "daily"):
        days, _, _, _, _, error = _report_options(rest, None, days=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_standup(1 if days is None else days)
    elif cmd in ("failures", "fails", "loops"):
        days, _, _, _, flags, error = _report_options(
            rest, None, days=True, flags=("--loops",)
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_failures(30 if days is None else days,
                     loops=cmd == "loops" or "--loops" in flags)
    elif cmd in ("subagents", "sub-agents"):
        days, _, _, _, _, error = _report_options(rest, None, days=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_subagents(30 if days is None else days)
    elif cmd in ("switches", "model-switches"):
        days, _, _, _, _, error = _report_options(rest, None, days=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_switches(30 if days is None else days)
    elif cmd in ("endings", "unclean"):
        days, _, _, _, _, error = _report_options(rest, None, days=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_endings(30 if days is None else days)
    elif cmd in ("coach", "practice", "review"):
        days, sort_by, descending, _, _, error = _report_options(
            rest, "coach", days=True
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_coach(30 if days is None else days,
                  sort_by=sort_by, descending=descending)
    elif cmd in ("rhythm", "when"):
        days, _, _, _, _, error = _report_options(rest, None, days=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_rhythm(30 if days is None else days)
    elif cmd in ("context", "setup"):
        if rest:
            print(f"error: unexpected argument '{rest[0]}'", file=sys.stderr)
            return 1
        cmd_context()
    elif cmd in ("instructions", "rules"):
        if rest:
            print(f"error: unexpected argument '{rest[0]}'", file=sys.stderr)
            return 1
        cmd_instructions()
    elif cmd == "hooks":
        _, sort_by, descending, event, _, error = _report_options(
            rest, "hooks", word=True
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_hooks(event, sort_by=sort_by, descending=descending)
    elif cmd in ("mcp", "servers"):
        _, sort_by, descending, name, _, error = _report_options(
            rest, "mcp", word=True
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_mcp(name, sort_by=sort_by, descending=descending)
    elif cmd in ("skills", "profiles", "agent-files"):
        kind = "skills" if cmd == "skills" else "agents"
        _, sort_by, descending, name, flags, error = _report_options(
            rest, "assets", word=True, flags=("--by-repo",)
        )
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        if "--by-repo" in flags:
            # Agent profiles leave no load marker, so there is nothing to
            # group. Saying so beats drawing an empty page.
            if kind != "skills":
                print("error: --by-repo needs load markers, which only skills "
                      "leave — try 'cs skills --by-repo'", file=sys.stderr)
                return 1
            # Refused rather than quietly ignored. A flag that is dropped on
            # the floor is worse than one that is rejected: the reader gets
            # an answer to a question they did not ask and no sign of it.
            if name:
                print(f"error: --by-repo lists every checkout, so it cannot "
                      f"also be narrowed to '{name}' — use one or the other",
                      file=sys.stderr)
                return 1
            if sort_by or descending is not None:
                print("error: --by-repo is ordered by how much work each "
                      "checkout did; --sort does not apply to it",
                      file=sys.stderr)
                return 1
        cmd_assets(kind, name, sort_by=sort_by, descending=descending,
                   by_place="--by-repo" in flags)
    elif cmd == "pin":
        _require(rest, "pin <#N|id>")
        cmd_pin(rest[0])
    elif cmd == "unpin":
        _require(rest, "unpin <#N|id>")
        cmd_unpin(rest[0])
    elif cmd == "pins":
        if rest:
            print(f"error: unexpected argument '{rest[0]}'", file=sys.stderr)
            return 1
        cmd_pins()
    elif cmd == "note":
        _require(rest, "note <#N|id> [text]")
        text = " ".join(rest[1:]) if len(rest) > 1 else None
        cmd_note(rest[0], text)
    elif cmd == "tag":
        _require(rest, "tag <#N|id> <tag>")
        if len(rest) < 2:
            print("error: missing tag — usage: cs tag <#N|id> <tag>",
                  file=sys.stderr)
            return 1
        cmd_tag(rest[0], rest[1])
    elif cmd == "untag":
        _require(rest, "untag <#N|id> <tag>")
        if len(rest) < 2:
            print("error: missing tag — usage: cs untag <#N|id> <tag>",
                  file=sys.stderr)
            return 1
        cmd_untag(rest[0], rest[1])
    elif cmd == "budget" and "--check" in rest:
        if len(rest) > 1:
            print("error: --check takes nothing else", file=sys.stderr)
            return 1
        return budget_check()
    elif cmd == "budget":
        if len(rest) > 1:
            print(f"error: unexpected argument '{rest[1]}'", file=sys.stderr)
            return 1
        cmd_budget(rest[0] if rest else None)
    elif cmd in ("next", "up"):
        days, _, _, _, _, error = _report_options(rest, None, days=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_next(14 if days is None else days)
    elif cmd in ("eod", "weekly"):
        _, _, _, _, flags, error = _report_options(rest, None, flags=("--md",))
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        (cmd_eod if cmd == "eod" else cmd_weekly)(markdown="--md" in flags)
    elif cmd in ("similar", "like"):
        _, _, _, term, error = _listing_options(rest, term=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_similar(term)
    elif cmd == "asks":
        repo, rest, error = _repo_option(rest)
        if not error:
            days, _, _, _, _, error = _report_options(rest, None, days=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_asks(30 if days is None else days, repo)
    elif cmd == "saved":
        if len(rest) > 1:
            print(f"error: unexpected argument '{rest[1]}'", file=sys.stderr)
            return 1
        cmd_saved(rest[0] if rest else None)
    elif cmd in ("diff", "compare"):
        if len(rest) != 2:
            print("error: missing argument — usage: cs diff <#N|id> <#N|id>",
                  file=sys.stderr)
            return 1
        cmd_diff(rest[0], rest[1])
    elif cmd == "replay":
        _require(rest, "replay <#N|id>")
        if len(rest) > 1:
            print(f"error: unexpected argument '{rest[1]}'", file=sys.stderr)
            return 1
        cmd_replay(rest[0])
    elif cmd in ("anomalies", "spikes"):
        days, _, _, _, _, error = _report_options(rest, None, days=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_anomalies(30 if days is None else days)
    elif cmd == "health":
        repo, rest, error = _repo_option(rest)
        if not error and rest:
            error = f"unexpected argument '{rest[0]}'"
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_health(repo or ".")
    elif cmd == "patterns":
        days, _, _, _, _, error = _report_options(rest, None, days=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_patterns(30 if days is None else days)
    elif cmd in ("cleanup", "clean-up", "tidy"):
        days, _, _, _, _, error = _report_options(rest, None, days=True)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        cmd_cleanup(14 if days is None else days)
    elif cmd in ("help", "-h", "--help"):
        cmd_help()
    elif cmd in ("version", "-v", "--version"):
        print(f"cs {__version__}")
    else:
        print(f"error: unknown command '{cmd}' — run 'cs help'", file=sys.stderr)
        return 1
    return 0


def _save_option(rest: list[str]) -> tuple[str | None, list[str], str | None]:
    """Pull `--save NAME` out of a search's arguments."""
    if "--save" not in rest:
        return None, rest, None
    at = rest.index("--save")
    if at + 1 >= len(rest) or rest[at + 1].startswith("-"):
        return None, rest, "--save wants a name: cs search --save <name> <words>"
    return rest[at + 1], rest[:at] + rest[at + 2:], None


def _repo_option(rest: list[str]) -> tuple[str | None, list[str], str | None]:
    """Pull `--repo X` (or `--repo=X`) out of the arguments. '.' is here."""
    for at, arg in enumerate(rest):
        if arg.startswith("--repo="):
            return arg.split("=", 1)[1] or ".", rest[:at] + rest[at + 1:], None
        if arg == "--repo":
            if at + 1 < len(rest) and not rest[at + 1].startswith("-"):
                return rest[at + 1], rest[:at] + rest[at + 2:], None
            return ".", rest[:at] + rest[at + 1:], None
    return None, rest, None


def _require(rest: list[str], usage: str) -> None:
    if not rest:
        print(f"error: missing argument — usage: cs {usage}", file=sys.stderr)
        sys.exit(1)
