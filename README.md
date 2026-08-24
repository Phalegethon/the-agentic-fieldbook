# The Agentic Fieldbook

**TAF** is a collection of small, independently installable Agent Skills for
real software-delivery work. Skills follow the open
[Agent Skills specification](https://agentskills.io/specification) and keep
deterministic work in scripts so agents spend their context on decisions and
communication.

Created and maintained by
[Gürkan Süerdem (@Phalegethon)](https://github.com/Phalegethon).

## Available skills

| Skill | Status | Purpose |
|---|---|---|
| [`branch-handoff`](skills/branch-handoff) | Stable | Compare a branch with its base and prepare evidence-backed DEV and QA handoffs without code review or rerunning project tests. |

Planned names such as `pr-summary`, `release-risk`, `incident-brief`, and
`dependency-audit` are roadmap items, not installable packages yet.

## Install `branch-handoff`

### Quick install by agent

Run one command from the project that should use the skill. These commands use
the current `skills` CLI and avoid package, agent, scope, confirmation, and
optional `find-skills` prompts. The flow is verified with CLI version `1.5.23`
and later.

#### Claude Code

```bash
npx --yes skills@latest add Phalegethon/the-agentic-fieldbook \
  --skill branch-handoff \
  --agent claude-code \
  --yes
```

Expected project path: `.claude/skills/branch-handoff`.

#### Codex

```bash
npx --yes skills@latest add Phalegethon/the-agentic-fieldbook \
  --skill branch-handoff \
  --agent codex \
  --yes
```

Expected project path: `.agents/skills/branch-handoff`.

#### Antigravity

```bash
# Antigravity
npx --yes skills@latest add Phalegethon/the-agentic-fieldbook \
  --skill branch-handoff \
  --agent antigravity \
  --yes

# Antigravity CLI
npx --yes skills@latest add Phalegethon/the-agentic-fieldbook \
  --skill branch-handoff \
  --agent antigravity-cli \
  --yes
```

Expected project path: `.agents/skills/branch-handoff`.

To install for more than one agent, repeat `--agent`. The installer keeps one
canonical copy when possible and links agent-specific paths to it:

```bash
npx --yes skills@latest add Phalegethon/the-agentic-fieldbook \
  --skill branch-handoff \
  --agent claude-code \
  --agent codex \
  --yes
```

The first `--yes` belongs to `npx`; the final `--yes` belongs to the `skills`
installer. These examples install into the current project. Add `--global`
only when you intentionally want the skill available across projects.

Project installation creates or updates `skills-lock.json`. Verify the result
and selected agent paths with:

```bash
npx --yes skills@latest list --json
```

### Interactive installation

Use the interactive form when you deliberately want to choose agents and
scope from menus:

```bash
npx --yes skills@latest add Phalegethon/the-agentic-fieldbook \
  --skill branch-handoff
```

Before confirming, check that the selected-agent summary contains only the
agents you intended. The optional `find-skills` offer is provided by the
installer and is not part of TAF or required by `branch-handoff`.

### Alternative installation paths

Codex can install the skill directly from its GitHub path:

```text
Install branch-handoff from
https://github.com/Phalegethon/the-agentic-fieldbook/tree/main/skills/branch-handoff
```

The Codex user-level destination is `$CODEX_HOME/skills/branch-handoff`,
normally `~/.codex/skills/branch-handoff`.

Claude Code users may alternatively use the TAF marketplace:

```text
/plugin marketplace add Phalegethon/the-agentic-fieldbook
/plugin install branch-handoff@the-agentic-fieldbook
/reload-plugins
```

The marketplace command is `/branch-handoff:branch-handoff`. For a manual
project installation, copy `skills/branch-handoff` into the agent's supported
project skill directory.

## Update `branch-handoff`

Copied and project-installed skills cannot receive a universal push
notification from this repository. Re-run the matching agent-specific install command
from the installation section so the selected runtime copy is overwritten.
For example, Claude Code project and global updates are:

```bash
# Claude Code project installation
npx --yes skills@latest add Phalegethon/the-agentic-fieldbook \
  --skill branch-handoff \
  --agent claude-code \
  --yes

# Claude Code global installation
npx --yes skills@latest add Phalegethon/the-agentic-fieldbook \
  --skill branch-handoff \
  --agent claude-code \
  --global \
  --yes
```

Codex and Antigravity users should rerun their matching command above with the
same `--agent` and scope. For agent-targeted installations, do not substitute
the generic `skills update` command: a TAF release smoke test found that it can
refresh the canonical `.agents/skills` copy while leaving a runtime-specific
copy stale. Verify the active path and source afterward with
`npx --yes skills@latest list --json`.

Subscribe to this repository's GitHub Releases to be notified when a new TAF
skill version is published.

Claude marketplace users can update manually with
`/plugin update branch-handoff@the-agentic-fieldbook`, then run
`/reload-plugins` when prompted. To opt into auto-update, open `/plugin`, choose
`Marketplaces`, select `the-agentic-fieldbook`, and enable auto-update. TAF
never enables silent updates or performs a network update check when the skill
runs.

## Use branch-handoff

From a Git repository, ask the agent:

```text
Compare this branch with main and prepare the DEV and QA handoff.
```

When the request supplies neither a platform target nor `local only`, the skill
asks one combined structured question for optional Jira and GitHub context
before repository or diff analysis. Choose Jira, GitHub, both, or local-only;
the skill then requests only the selected exact targets and permissions.

To include Jira intent and acceptance criteria, supply the exact issue. The
skill asks for bounded Jira consent before repository or diff analysis:

```text
Use branch-handoff to compare the current branch with origin/main and prepare the DEV and QA handoff. jira FE-2669
```

Defaults and boundaries:

- Uses a merge-base comparison and prefers a fresh remote base when available.
- Reads committed changes only unless working-tree inclusion is requested.
- Does not run project tests, builds, lint, code review, blame, or broad history.
- Produces local QA, Developer/PR, and evidence sections.
- Keeps Jira and GitHub local by default. Every platform read or comment needs
  the session-scoped consent described by the skill.

Runtime requirements are Git and Python 3. There are no Python package
dependencies.

## Repository structure

```text
the-agentic-fieldbook/
├── .claude-plugin/marketplace.json
├── skills/
│   └── branch-handoff/
│       ├── SKILL.md
│       ├── .claude-plugin/plugin.json
│       ├── agents/openai.yaml
│       ├── references/
│       └── scripts/collect_diff.py
└── tests/
```

## Verification

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 tests/preparing_branch_handoff/benchmark_collector.py
```

Claude Code users can additionally validate the local marketplace:

```bash
claude plugin validate .
```

## Contributing

Issues, skill proposals, forks, and pull requests are welcome. Contributors use
their own GitHub identities; `@Phalegethon` is the maintainer and review owner,
not a required commit identity. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

MIT. See [`LICENSE`](LICENSE).
