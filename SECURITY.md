# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report it privately through
[GitHub Security Advisories](https://github.com/smaharajan/copilot-sessions/security/advisories/new).

You can expect an acknowledgement within **3 working days** and an assessment
within **10 working days**. If a fix is needed we will agree a disclosure date
with you before publishing.

## Supported versions

The latest release on `main` receives security fixes.

## Threat model

`cs` reads a local SQLite database and prints it to a terminal. That shape
determines what is and is not a security concern here.

**What `cs` does**

| | |
|---|---|
| Opens `~/.copilot/session-store.db` | `mode=ro` via SQLite URI — the store is never written; cs sidecars live under `~/.config/cs/` |
| Network access | none — the package makes no outbound connections |
| Runtime dependencies | none — Python standard library only |
| Processes it starts | your `$PAGER`, and `copilot --resume` on `cs resume` |

**What the data is.** Session transcripts are untrusted input. They contain
whatever was typed or echoed during a Copilot session, including text a
third-party repository, web page or prompt injection may have caused the model
to emit. `cs` therefore treats every stored value as hostile before printing it.

Structured one-line fields (for example repository names, model names and
hook metadata) are also prevented from introducing new rows. Transcript prose
keeps its line breaks; table cells and headings do not.

**In scope**

- A credential reaching the terminal unmasked (see below).
- Terminal control or escape sequences from stored text reaching the terminal.
- Command or SQL injection through a session id, path, pattern or environment
  variable.
- Anything that writes to the store, or writes session content outside it.
- Privilege or path issues in `install.sh`.

**Out of scope**

- Read access to your own store by a process already running as you. The store
  is your file; `cs` does not add a boundary that the filesystem does not
  already have.
- Secrets that are in the store in the first place. `cs audit` exists to *find*
  those; it cannot remove them, and Copilot — not `cs` — wrote them.
- `CS_REDACT=0`. It is a documented, explicit opt-out.

## Credential masking

`cs` masks credential-shaped text **at the render edge**, in `cs/redact.py`, so
nothing secret-shaped reaches your screen, scrollback, a screen-share or
anything you paste out of a terminal.

Masking is display-only and deliberately conservative in both directions:

- It errs **towards masking** on the render path — a false positive costs you
  one obscured word.
- It errs **towards silence** in `cs audit` — a false finding costs you a
  pointless credential rotation, so findings are validated before reporting.

A credential format that `cs` fails to mask is a **valid security report**.
Please include the shape of the value, not a real one — a synthetic example is
enough to write the rule and the test.

## Testing note

`tests/test_governance.py` contains a corpus of **synthetic, non-functional**
credential strings used to test the masking rules — it sits there because
`cs audit` is the view that scans for secrets. They are marked
`# gitleaks:allow`. If you believe any of them resembles a real credential,
report it privately using the process above.
