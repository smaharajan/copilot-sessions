#!/usr/bin/env python3
"""Regenerate the README screenshots.

    python3 docs/img/make_screens.py

Two rules decide everything about how this works.

**The data must be synthetic.** A screenshot of a real store would publish
repository names, session summaries and spend, permanently, in a public
repository — and no amount of masking makes a published image safe to
un-publish. So this builds a throwaway store under a temporary
``COPILOT_HOME``, on the same schema the test suite uses, and photographs
that.

**The picture must be a real run.** Hand-drawn mock-ups drift from the app
the moment either changes, and a README that flatters a view nobody can
reproduce is worse than one with no pictures at all. Each image here is the
actual bytes ``cs`` wrote to a terminal, run in a pty at a fixed width, with
its ANSI colour translated into SVG text. SVG rather than PNG because it
stays sharp on any display, weighs a few kilobytes, and can be diffed.

Standard library only, like the package it documents.
"""

from __future__ import annotations

import json
import os
import pty
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

# ── the synthetic store ──────────────────────────────────────────────────────

REPOS = (
    ("acme/portal", "Build the customer portal shell"),
    ("acme/webshop", "Add checkout validation"),
    ("acme/payments", "Fix flaky payment tests"),
    ("acme/infra", "Migrate CI to GitHub Actions"),
    ("acme/docs-site", "Write the onboarding guide"),
)

MODELS = ("gpt-5.5", "claude-opus-4.8", "claude-sonnet-4.6", "gpt-5.4-mini")

SKILLS = ("commit", "code-review", "release-notes", "test-authoring", "adr",
          "changelog", "perf-budget", "dependency-audit", "migration-plan")

# How often each skill is reached for in the demo store. A flat distribution
# would draw a flat chart, and the point of the view is that real ones are
# never flat — a handful carry the work and the rest sit there.
SKILL_USE = {"commit": 5, "code-review": 3, "release-notes": 2,
             "test-authoring": 2, "adr": 1}

# A plugin pack, switched on, plus a skill switched off in settings and one
# that ran in a checkout this machine does not have. The view reports all
# three differently, and a screenshot of nine personal skills shows none of
# it.
PLUGIN = ("marketplace", "release-kit", ("changelog-draft", "tag-and-push"))
DISABLED = ("dependency-audit",)
ELSEWHERE = "house-style"

AGENTS = ("triage", "docs-writer", "security-review")

AGENT_USE = {"triage": 4, "docs-writer": 1}


def _seed(base: Path) -> None:
    """A store with enough shape for every view to have something to say."""
    # The schema is the test suite's, imported rather than copied so the
    # screenshots can never be taken of a store shape `cs` no longer reads.
    from support import _build_store

    _build_store(base)
    conn = sqlite3.connect(base / "session-store.db")
    for table in ("sessions", "turns", "assistant_usage_events", "search_index",
                  "session_files", "session_refs", "checkpoints"):
        conn.execute(f"DELETE FROM {table}")

    for index in range(42):
        repo, summary = REPOS[index % len(REPOS)]
        sid = f"{index:08x}-1111-4111-8111-{index:012x}"
        stamp = f"datetime('now','-{index % 28} days','-{index % 7} hours')"
        # What this session reached for. Written the way `cs` insists on
        # reading it — a qualified mention, never a bare word — because the
        # screenshot has to be produced by the real counting rule.
        mentions = [name for name, weight in SKILL_USE.items()
                    if index % max(1, 6 - weight) == 0]
        mentions += [f"{name} agent" for name, weight in AGENT_USE.items()
                     if index % max(1, 7 - weight) == 0]
        conn.execute(
            f"INSERT INTO sessions VALUES (?,?,?,'github','main',?,{stamp},{stamp})",
            (sid, f"/work/{repo.split('/')[1]}", repo,
             f"{summary} · part {index // len(REPOS) + 1}"),
        )
        for turn in range((index % 9) + 2):
            asked = (f"{summary.lower()} — review how it works today first"
                     if not turn else
                     "use the " + " skill, then the ".join(mentions) + " skill"
                     if mentions else "carry on")
            # The CLI writes this marker into the turn when it actually loads
            # a skill, and `cs skills` reports those separately from mentions.
            if turn == 1 and index % 4 == 0 and mentions:
                asked = f'<skill-context name="{mentions[0]}">\n{asked}'
            conn.execute(
                f"INSERT INTO turns (session_id, turn_index, user_message,"
                f" assistant_response, timestamp) VALUES (?,?,?,?,{stamp})",
                (sid, turn, asked, "Done. Tests pass; opened the pull request."),
            )
            model = MODELS[(index + turn) % len(MODELS)]
            initiator = ("user" if turn % 5 else
                         "sub-agent" if turn % 3 else "compaction")
            conn.execute(
                f"""INSERT INTO assistant_usage_events
                    (session_id, turn_index, model, total_nano_aiu, input_tokens,
                     output_tokens, cache_read_tokens, reasoning_tokens,
                     duration_ms, time_to_first_token_ms, finish_reason,
                     content_filter_triggered, initiator, agent_id, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,'stop',0,?,?,{stamp})""",
                (sid, turn, model, 90_000_000 * (turn + 2) * (index % 5 + 1),
                 1200 * (turn + 1), 240 * (turn + 1), 9000 * (turn + 1),
                 60 * turn, 3200 + 400 * turn, 620 + 40 * turn, initiator,
                 f"toolu_agent_{index}" if initiator == "sub-agent" else None),
            )
        conn.execute(
            "INSERT INTO search_index (content, session_id, source_type)"
            " VALUES (?,?,'turn')", (f"{summary} with cache and retries", sid))
        conn.executemany(
            "INSERT INTO session_files (session_id, file_path, tool_name)"
            " VALUES (?,?,?)",
            [(sid, f"/work/{repo.split('/')[1]}/src/main.py", "edit"),
             (sid, f"/work/{repo.split('/')[1]}/README.md", "create")])
        if index % 3 == 0:
            conn.executemany(
                "INSERT INTO session_refs (session_id, ref_type, ref_value)"
                " VALUES (?,?,?)",
                [(sid, "commit", f"{index:07x}a"), (sid, "pr", str(100 + index))])
        if index % 4 == 0:
            conn.execute(
                """INSERT INTO checkpoints (session_id, checkpoint_number,
                   overview, work_done, important_files, next_steps,
                   technical_details) VALUES (?,1,?,?,?,?,?)""",
                (sid, f"{summary} for {repo}",
                 "- shipped the first cut\n- wired the tests in",
                 "`src/main.py` the entry point",
                 "- point the charts at live data\n- confirm the tag convention",
                 "Standard library only."))

    # One session that pasted something credential-shaped, so `cs audit` has a
    # finding. Synthetic and non-functional, in the shape the masker matches.
    sid = "a1b2c3d4-1111-4111-8111-000000000099"
    secret = "AKIA" + "IOSFODNN7EXAMPLE"          # gitleaks:allow
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,'github','main',?,"
        "datetime('now','-2 days'),datetime('now','-2 days'))",
        (sid, "/work/infra", "acme/infra", "Provision the staging stack"))
    conn.execute(
        "INSERT INTO turns (session_id, turn_index, user_message,"
        " assistant_response, timestamp) VALUES (?,3,?,?,datetime('now'))",
        (sid, f"here is the key: {secret}",
         "Understood — rotate that and use the secret store instead."))
    conn.execute(
        "INSERT INTO search_index (content, session_id, source_type)"
        " VALUES (?,?,'turn')", (f"here is the key: {secret}", sid))

    # One session that wrote a credential into a file it created, so `cs audit`
    # has a `hardcoded` row and not only a `review` one — the two halves of
    # that verdict are a code-shaped line and a session_files record, and the
    # picture is worth nothing if it only ever shows one of them.
    sid = "e5f6a7b8-1111-4111-8111-000000000098"
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,'github','main',?,"
        "datetime('now','-3 days'),datetime('now','-3 days'))",
        (sid, "/work/webshop", "acme/webshop", "Wire the reporting database"))
    conn.execute(
        "INSERT INTO turns (session_id, turn_index, user_message,"
        " assistant_response, timestamp) VALUES (?,24,?,?,datetime('now'))",
        (sid, "add the reporting connection",
         'Written to `settings.py`:\n\nDB_PASSWORD = "s3cr3t-staging-pw"\n'))
    conn.execute(
        "INSERT INTO session_files (session_id, file_path, tool_name)"
        " VALUES (?,?,'create')", (sid, "/work/webshop/settings.py"))

    # …and one that reports having removed a tree and forced a push, so the
    # destructive block shows both of its tiers rather than only the quiet one.
    sid = "9c0d1e2f-1111-4111-8111-000000000097"
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,'github','main',?,"
        "datetime('now','-1 days'),datetime('now','-1 days'))",
        (sid, "/work/infra", "acme/infra", "Retire the legacy runner"))
    conn.executemany(
        "INSERT INTO turns (session_id, turn_index, user_message,"
        " assistant_response, timestamp) VALUES (?,?,?,?,datetime('now'))",
        [(sid, 2, "drop the old runner",
          "Deleted the tree with `rm -rf legacy-runner/` \u2713 and "
          "`git push --force` to drop the bad commit \u2713"),
         (sid, 5, "and the cache?",
          "You could clear it yourself:\n```bash\nrm -rf ~/.cache/runner\n```")],
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('gone-0001','/work/house-style',"
        "'acme/house-style','github','main','Apply the house style',"
        "datetime('now','-3 days'),datetime('now','-3 days'))"
    )
    conn.execute(
        "INSERT INTO turns (session_id, turn_index, user_message, "
        "assistant_response, timestamp) VALUES ('gone-0001',1,?,?,'t')",
        (f'<skill-context name="{ELSEWHERE}">\nbody\n', "Applied."),
    )
    conn.commit()
    conn.close()


def _seed_disk(home: Path, project: Path) -> None:
    """Skills, agents and instruction files, so the setup views have content."""
    for kind, entries in (("skills", SKILLS), ("agents", AGENTS)):
        folder = home / kind
        folder.mkdir(parents=True, exist_ok=True)
        for name in entries:
            (folder / f"{name}.md").write_text(f"# {name}\n\nA {kind[:-1]}.\n")
    marketplace, pack, shipped = PLUGIN
    for name in shipped:
        skill = home / "installed-plugins" / marketplace / pack / "skills" / name
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(f"# {name}\n\nFrom a plugin.\n")
    (home / "settings.json").write_text(json.dumps({
        "enabledPlugins": {f"{pack}@{marketplace}": True},
        "disabledSkills": list(DISABLED),
    }))
    (home / "AGENTS.md").write_text(
        "# Personal brief\n\n" + "Conventions that follow me between repos.\n" * 130)
    (home / "copilot-instructions.md").write_text(
        "# Personal instructions\n\n## Style\nBe brief.\n")
    (project / ".github").mkdir(parents=True, exist_ok=True)
    (project / ".github" / "copilot-instructions.md").write_text(
        "# acme/portal\n\n## Tests\n`make test`\n\n## Style\nNo new dependencies.\n")
    (project / "AGENTS.md").write_text(
        "# Agent brief\n\n## Build\n`make build`\n\n## Review\nOne test per fix.\n")


# ── terminal capture ─────────────────────────────────────────────────────────

def capture(argv: list[str], home: Path, cwd: Path, columns: int = 92) -> str:
    """What `cs` writes to a *terminal*, colour and all.

    A pty, not a pipe: `cs` turns colour off when stdout is not a tty, which
    is right for a redirect and useless for a screenshot.
    """
    env = {
        **os.environ,
        "COPILOT_HOME": str(home),
        # The cross-tool skills root sits beside the *real* home and does not
        # follow COPILOT_HOME, so without this the photograph includes the
        # names of whatever is in ~/.agents/skills — real ones, published,
        # permanently, which is the one thing this script exists to prevent.
        "CS_AGENTS_HOME": str(home / ".agents"),
        "COLUMNS": str(columns), "LINES": "60",
        "TERM": "xterm-256color",
        # `PAGER`, which is the variable `_page` actually reads. This said
        # `CS_PAGER`, which nothing in the package has ever honoured — so a
        # report longer than LINES spawned `less`, which found the pty on
        # /dev/tty and sat there waiting for a keypress that never came.
        # Every screenshot of a long report depended on that not happening.
        "PAGER": "cat",
        "PYTHONPATH": str(ROOT),
    }
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "cs", *argv],
            stdin=subprocess.DEVNULL, stdout=slave, stderr=slave,
            cwd=str(cwd), env=env, close_fds=True,
        )
        os.close(slave)
        chunks = []
        while True:
            try:
                data = os.read(master, 65536)
            except OSError:      # the child closed its end
                break
            if not data:
                break
            chunks.append(data)
        process.wait(timeout=60)
    finally:
        os.close(master)
    return b"".join(chunks).decode("utf-8", "replace")


# ── ANSI → SVG ───────────────────────────────────────────────────────────────

_SGR = re.compile(r"\033\[([0-9;]*)m")
# Everything a terminal acts on *except* colour. 'm' is deliberately absent
# from the final class: stripping escapes with a blanket `[A-Za-z]` also
# stripped every SGR sequence, which produced a set of perfectly aligned,
# perfectly grey screenshots of a colour application.
_OTHER_ESCAPE = re.compile(r"\033\[[0-9;?]*[A-Za-ln-z]|\033\][^\007]*\007|\r")

# The xterm 256-colour palette, worked out rather than pasted: sixteen system
# colours, a 6×6×6 cube, then a 24-step grey ramp.
_BASE = ("000000", "cd3131", "0dbc79", "e5e510", "2472c8", "bc3fbc", "11a8cd",
         "e5e5e5", "666666", "f14c4c", "23d18b", "f5f543", "3b8eea", "d670d6",
         "29b8db", "ffffff")
_STEPS = (0, 95, 135, 175, 215, 255)


def _palette() -> list[str]:
    colours = list(_BASE)
    for red in _STEPS:
        for green in _STEPS:
            for blue in _STEPS:
                colours.append(f"{red:02x}{green:02x}{blue:02x}")
    colours += [f"{value:02x}{value:02x}{value:02x}" for value in range(8, 239, 10)]
    return colours


PALETTE = _palette()
FOREGROUND = "#e6e6e6"
BACKGROUND = "#14161c"


def _spans(line: str) -> list[tuple[str, str, bool, bool]]:
    """(text, colour, bold, dim) runs, with every other escape dropped."""
    out: list[tuple[str, str, bool, bool]] = []
    colour, bold, dim = FOREGROUND, False, False
    position = 0
    for match in _SGR.finditer(line):
        if text := line[position:match.start()]:
            out.append((text, colour, bold, dim))
        position = match.end()
        codes = [int(code or 0) for code in (match.group(1) or "0").split(";")]
        index = 0
        while index < len(codes):
            code = codes[index]
            if code == 0:
                colour, bold, dim = FOREGROUND, False, False
            elif code == 1:
                bold = True
            elif code == 2:
                dim = True
            elif code == 22:
                bold = dim = False
            elif code == 38 and codes[index + 1:index + 2] == [5]:
                colour = "#" + PALETTE[codes[index + 2] % 256]
                index += 2
            elif code == 39:
                colour = FOREGROUND
            elif 30 <= code <= 37:
                colour = "#" + PALETTE[code - 30]
            elif 90 <= code <= 97:
                colour = "#" + PALETTE[code - 90 + 8]
            index += 1
    if text := line[position:]:
        out.append((text, colour, bold, dim))
    return out


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def to_svg(raw: str, title: str) -> str:
    """One terminal capture as a self-contained SVG."""
    # The app's own idea of how wide text is, so an emoji is measured as the
    # two cells a terminal gave it rather than the one character it is.
    from cs.ui import cells

    lines = [_OTHER_ESCAPE.sub("", line).rstrip()
             for line in raw.replace("\r\n", "\n").split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    char_w, line_h, pad, chrome = 8.1, 19.0, 18.0, 34.0
    columns = max((cells(_SGR.sub("", line)) for line in lines), default=80)
    width = round(columns * char_w + pad * 2)
    height = round(len(lines) * line_h + pad * 2 + chrome)

    body = []
    for row, line in enumerate(lines):
        y = pad + chrome + (row + 1) * line_h - 5
        x = pad
        for text, colour, bold, dim in _spans(line):
            span = cells(text) * char_w
            if text.strip():
                weight = ' font-weight="600"' if bold else ""
                fade = ' opacity="0.55"' if dim else ""
                # textLength pins each run to the exact number of terminal
                # cells it occupied. Without it the SVG is at the mercy of
                # whichever monospace font the reader's browser picks, and a
                # 1% difference in advance width walks a 90-column table out
                # of alignment by most of a character by the right-hand edge.
                body.append(
                    f'<text x="{x:.1f}" y="{y:.1f}" fill="{colour}"'
                    f'{weight}{fade} textLength="{span:.1f}"'
                    f' lengthAdjust="spacingAndGlyphs"'
                    f' xml:space="preserve">{_escape(text)}</text>'
                )
            x += span

    dots = "".join(
        f'<circle cx="{22 + n * 18}" cy="17" r="6" fill="{shade}"/>'
        for n, shade in enumerate(("#ff5f56", "#ffbd2e", "#27c93f"))
    )
    drawn = chr(10).join("    " + item for item in body)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" \
height="{height}" viewBox="0 0 {width} {height}" role="img" \
aria-label="{_escape(title)}">
  <rect width="{width}" height="{height}" rx="10" fill="{BACKGROUND}"/>
  <rect width="{width}" height="{chrome}" rx="10" fill="#1d2029"/>
  <rect y="{chrome - 10}" width="{width}" height="10" fill="#1d2029"/>
  {dots}
  <text x="{width / 2:.0f}" y="22" fill="#8b93a7" font-size="12" \
text-anchor="middle" font-family="ui-monospace,SFMono-Regular,Menlo,\
Consolas,monospace">{_escape(title)}</text>
  <g font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,'DejaVu Sans Mono',\
monospace" font-size="13.5">
{drawn}
  </g>
</svg>
"""


# ── what gets photographed ───────────────────────────────────────────────────

SHOTS: tuple[tuple[str, list[str], str], ...] = (
    ("skills", ["skills"], "cs skills"),
    ("instructions", ["instructions"], "cs instructions"),
    ("cost", ["cost", "30"], "cs cost 30"),
    ("delegation", ["agents", "30"], "cs agents 30"),
    ("efficiency", ["efficiency", "30"], "cs efficiency 30"),
    ("stats", ["stats", "30"], "cs stats 30"),
    ("audit", ["audit"], "cs audit"),
    ("repos", ["repos"], "cs repos"),
)


def main() -> int:
    # A fixed, anonymous path rather than a random temporary one: `cs` prints
    # the directory it audited, so a random name would put a different string
    # in the committed image on every regeneration and make the diff useless.
    # `/tmp` explicitly rather than `tempfile.gettempdir()`, which on macOS is
    # a per-user directory whose name is a machine-specific token.
    root = Path("/tmp/cs-demo")
    shutil.rmtree(root, ignore_errors=True)
    try:
        home = root / "copilot"
        project = root / "acme-portal"
        home.mkdir(parents=True)
        project.mkdir(parents=True)
        _seed(home)
        _seed_disk(home, project)
        for name, argv, title in SHOTS:
            raw = capture(argv, home, project)
            (OUT / f"{name}.svg").write_text(to_svg(raw, title), encoding="utf-8")
            print(f"  wrote docs/img/{name}.svg")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
