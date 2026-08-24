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

## Install one skill

### Cross-runtime installer

The `skills` CLI can install only `branch-handoff` from the repository:

```bash
npx skills add Phalegethon/the-agentic-fieldbook --skill branch-handoff
```

Select the target agent and project/global scope when prompted. For a
non-interactive Antigravity CLI installation:

```bash
npx skills add Phalegethon/the-agentic-fieldbook --skill branch-handoff --agent antigravity-cli --yes
```

### Codex

Ask Codex to install the GitHub path:

```text
Install branch-handoff from
https://github.com/Phalegethon/the-agentic-fieldbook/tree/main/skills/branch-handoff
```

The equivalent local destination is
`$CODEX_HOME/skills/branch-handoff`, normally
`~/.codex/skills/branch-handoff`.

### Claude Code marketplace

Add TAF once, then install only the plugin you need:

```text
/plugin marketplace add Phalegethon/the-agentic-fieldbook
/plugin install branch-handoff@the-agentic-fieldbook
/reload-plugins
```

The installed plugin command is `/branch-handoff:branch-handoff`. Claude may
also select it automatically when the request matches its description.

### Manual project installation

Copy `skills/branch-handoff` into the target project's supported skill folder,
for example `.agents/skills/branch-handoff`. Claude Code also supports
`.claude/skills/branch-handoff`; use the Codex user destination documented
above when installing directly for Codex.

## Use branch-handoff

From a Git repository, ask the agent:

```text
Compare this branch with main and prepare the DEV and QA handoff.
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
