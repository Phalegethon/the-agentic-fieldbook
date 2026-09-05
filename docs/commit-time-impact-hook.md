# Commit-time impact warning

An optional `pre-commit` launcher warns about dependent files a commit leaves
behind. It asks one bounded `impact-candidates --staged` query and writes one
short report to stderr: a header, at most five aligned detail lines for the
untouched production files, and a trailer naming what was left out.

This is the report a real staged edit to `apply_discount` printed in the
sample project used for the README recordings:

```text
TAF impact: 1 file depends on this change and is not in this commit
  src/orbit/checkout.py:12  <- pricing.apply_discount
  ... plus 1 test file (ask your agent to list TAF impact for this commit)
```

And this is the report a real staged edit to `normalize_change_base` prints in
this repository, captured from the skill's own dogfood test:

```text
TAF impact: 2 files depend on this change and are not in this commit
  tools/taf-context/taf_context/mcp_server.py:481   <- context_operations.normalize_change_base
  tools/taf-context/taf_context/prepare_cli.py:324  <- context_operations.normalize_change_base
  ... plus 1 test file (ask your agent to list TAF impact for this commit)
```

## Ask for it

```text
Warn me at commit time when I leave dependents behind.
```

The skill runs `hook status` first, explains what installing writes, and asks
for hook-write authorisation before installing anything.

## What it prints

The header counts the untouched production files (or the test files instead,
when no production file depends but a test does). Production files print as
detail lines first, and a test file only takes one of the five slots when no
production file depends. The trailer, present whenever something was left
out, names the remaining production files and folds the untouched test files
into a count, and points at the agent rather than a command: `query
impact-candidates --staged` is not runnable in a plugin installation and the
CLI's own budget would show only part of a wide result anyway. A trailing `+`
marks a count as a lower bound when the engine itself omitted candidates in
some direction, and the trailer falls back to "possibly more" when nothing
exact remains to name.

The header is bold on a real TTY stderr with `NO_COLOR` unset and `TERM` not
`dumb`; every other line, and every line on a non-TTY stderr (GUI clients, CI,
pipes), is plain ASCII.

## What it never does

- It is advisory only: stderr only, never stdout, exit code 0 always.
- It waits at most 3 seconds for the answer before giving up silently, so a
  slow or cold engine can never hold up a commit.
- It stays completely silent whenever the bound index is not ready, the
  staged change has no untouched dependents, or that wait runs out.
- It is deliberately not interactive: a prompt inside `pre-commit` would hang
  GUI clients, CI, and an agent's own commits, so questions about the full
  list go to the agent afterwards.
- Its own query performs the same standing-consent incremental refresh and
  superseded-generation prune as every query; it never builds, activates,
  downloads, or removes state, and `build` stays its own separate consent.

The hook is available on macOS and Linux, the platforms whose `pre-commit` is
a POSIX `sh` script; `install` and `remove` refuse elsewhere, and `--staged`
and the queries themselves are unaffected.

## Commands

```bash
<python> skills/prepare-repo-context/scripts/prepare_repo_context.py hook status --repo <repo>
<python> skills/prepare-repo-context/scripts/prepare_repo_context.py hook install --repo <repo> --confirm-hook-write
<python> skills/prepare-repo-context/scripts/prepare_repo_context.py hook remove --repo <repo> --confirm-hook-write
```

`install` and `remove` write only under `--confirm-hook-write` and only inside
the repository's own hooks directory, never a tracked file. The flag records
the user's approval of that write: an agent asks first and adds it only after
the user agreed in the conversation; a request to set up the warning starts
the procedure, it is not that approval. `status` writes no launcher, and like
`inspect` it performs the standing-consent incremental refresh of the bound
index.

- `--chain` keeps an existing foreign `pre-commit` hook: it is moved aside and
  still runs, after TAF's line. A chained hook that runs decides the commit
  with its own exit code, so a failing one still blocks it; a chained hook
  that cannot be run at all (deleted, or no longer executable) is skipped and
  the commit proceeds. `install --chain` refuses a foreign hook that is not
  executable, because git was not running it either.
- `status` reports `redirected` when `core.hooksPath` points elsewhere; TAF
  never installs there.
- `TAF_HOOK=0 git commit` silences one commit without touching the launcher.

## The launcher follows the current broker

The launcher follows the broker that last ran on this machine instead of
trusting only its own embedded, install-time paths. Every `prepare` command
but `hook run`, `hook install` itself, and the bundled MCP server at startup
refresh a small pointer file under TAF's own user-local state (two lines, the
resolved interpreter and the plugin's entry-point script) atomically, mode
0600 in a 0700 directory, and only when the state root already exists;
writing it is a convenience, and every failure to do so is swallowed rather
than failing the command. `hook run` never writes it, so a stale broker
recorded once can never re-assert itself through a later commit.

At commit time the launcher reads the pointer first and uses it when it is
readable and names an existing script; otherwise, or when the pointer is
missing, it falls back to its own embedded paths, then to `command -v python3`
when the chosen interpreter is not executable, and stays silent if neither an
interpreter nor a script can be found. A plugin update is therefore picked up
automatically by the next TAF session. `status` reports three fields about the
launcher: `launcher_current` means it runs this plugin's broker, so `false`
means it does not and re-installing fixes it; `launcher_text_current` is the
older byte-for-byte comparison with what install would write now, and `false`
there alone needs nothing, since a correct pointer keeps the launcher current
even when its embedded fallback text is older; `launcher_generation` names the
installed template (`pointer` for the self-healing one, `embedded` for an older
TAF launcher). A hook manager that appends its own block after TAF's launcher
trips `launcher_text_current: false` but leaves `launcher_current: true`; a
re-install would rewrite the whole file and drop that appended block, so nothing
requires it. The pointer file lives in the user's own state directory, the same
trust level as the launcher itself in `.git/hooks`.
