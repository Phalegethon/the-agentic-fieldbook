# The Agentic Fieldbook

**The Agentic Fieldbook (TAF)** is one plugin containing focused Agent Skills
for real software-delivery work. Install TAF once; use its skills independently.
Each skill follows the open
[Agent Skills specification](https://agentskills.io/specification), keeps
deterministic work in scripts, and loads its full instructions only when it is
relevant.

Created and maintained by
[Gürkan Süerdem (@Phalegethon)](https://github.com/Phalegethon).

## Available skills

| Skill | Version | Purpose |
|---|---:|---|
| [`branch-handoff`](skills/branch-handoff) | 1.2.1 | Compare a branch with its base and prepare evidence-backed DEV and QA handoffs without code review or rerunning project tests. |
| [`prepare-repo-context`](skills/prepare-repo-context) | 1.0.0 | Inspect providers, prepare a reusable native index, and run bounded evidence queries without loading the full repository into model context. |
| [`work-recovery`](skills/work-recovery) | 1.0.1 | Recover interrupted work and the single best next step from bounded, read-only Git evidence. |

Claude Code exposes these as `/taf:branch-handoff`,
`/taf:prepare-repo-context`, and `/taf:work-recovery`. Codex uses the
corresponding plugin-qualified TAF skills and can select them automatically
for relevant requests.

Planned names such as `pr-summary`, `release-risk`, `incident-brief`, and
`dependency-audit` are roadmap items, not shipped skills.

## Install TAF

### Claude Code

Add the TAF marketplace, install the single plugin, and reload:

```text
/plugin marketplace add Phalegethon/the-agentic-fieldbook
/plugin install taf@the-agentic-fieldbook
/reload-plugins
```

Open the plugin details to see all contained skills. Installing TAF makes the
collection discoverable; Claude reads a full `SKILL.md` only when that skill
is invoked or selected.

### Codex

Add this Git repository as a marketplace and install the single TAF plugin:

```bash
codex plugin marketplace add Phalegethon/the-agentic-fieldbook
codex plugin add taf@the-agentic-fieldbook
```

In the Codex app, the same plugin can then present
`taf:branch-handoff`, `taf:prepare-repo-context`, and `taf:work-recovery`
beneath TAF. The repository
contains a native `.codex-plugin/plugin.json`; no copied aggregate skill
bundle is created.

### Other Agent Skills hosts

For a host supported by the `skills` CLI, install the complete TAF collection
for that agent:

```bash
npx --yes skills@latest add Phalegethon/the-agentic-fieldbook \
  --skill '*' \
  --agent antigravity \
  --yes
```

Replace `antigravity` with the intended supported agent. Add `--global`
only for an intentional user-wide installation. This compatibility path may
materialize separate skill directories because the host has no plugin
container, but it installs the same TAF collection from the same canonical
`skills/` sources.

## Migrate to TAF 2.0

TAF 2.0 replaces the former `branch-handoff` and `work-recovery` Claude
plugins with one `taf` plugin. Run this clean migration in order:

```text
/plugin uninstall branch-handoff@the-agentic-fieldbook
/plugin uninstall work-recovery@the-agentic-fieldbook
/plugin marketplace update the-agentic-fieldbook
/plugin install taf@the-agentic-fieldbook
/reload-plugins
```

The old namespaces are intentionally not retained. After reloading, verify that
`/taf:branch-handoff`, `/taf:prepare-repo-context`, and `/taf:work-recovery`
appear under the TAF plugin.
Historical Git tags remain available if an installation must be recovered, but
TAF 2.x is the supported product line.

## Update TAF

Claude Code users can refresh the marketplace and update the unified plugin:

```text
/plugin marketplace update the-agentic-fieldbook
/plugin update taf@the-agentic-fieldbook
/reload-plugins
```

To opt into Claude marketplace auto-update, open `/plugin`, choose
`Marketplaces`, select `the-agentic-fieldbook`, and enable auto-update. TAF
never enables silent updates or performs a network update check while a skill
runs.

Codex users can refresh the Git marketplace and reinstall the selected product:

```bash
codex plugin marketplace upgrade the-agentic-fieldbook
codex plugin add taf@the-agentic-fieldbook
```

Agent Skills compatibility installations should rerun the matching
`skills@latest add` command with the same agent and scope, then verify active
paths with:

```bash
npx --yes skills@latest list --json
```

Subscribe to this repository's GitHub Releases for TAF product updates. Product
releases list every bundled skill version.

## Use branch-handoff

From a Git repository, ask:

```text
Use /taf:branch-handoff to compare this branch with main and prepare the DEV and QA handoff.
```

Natural-language invocation also works when the host supports automatic skill
selection:

```text
Compare this branch with main and prepare the DEV and QA handoff.
```

When the request supplies neither a platform target nor `local only`, the
skill asks one combined structured question for optional Jira and GitHub
context before repository or diff analysis. Choose Jira, GitHub, both, or
local-only; the skill then requests only the selected exact targets and
permissions.

To include Jira intent and acceptance criteria, supply the exact issue:

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

## Use work-recovery

From the interrupted Git worktree, ask:

```text
Use /taf:work-recovery to tell me where this work stopped and the single best next step. Do not run tests or change repository state.
```

The normal report ends with a reminder rather than a generated handoff. To move
the same evidence into another session, ask separately:

```text
Now turn that recovery dossier into a compact continuation prompt for another session.
```

Default boundaries:

- Reads bounded current-worktree Git metadata and tracked staged/unstaged diff
  evidence; other worktrees contribute metadata only.
- Untracked content requires exact path authorization and remains protected by
  no-follow, file-type, size, binary, generated, and credential gates.
- Does not run tests, builds, lint, project validation, or repository mutation.
- Does not build or update an index, query a provider, or access the network.
- Uses an existing provider/index only after separate explicit authorization;
  native recovery remains complete when none is available.
- Writes no recovery artifact and reports exact evidence omissions within the
  selected context budget.

Runtime requirements are Git and Python 3. The bundled collector uses only the
Python standard library.

## Use prepare-repo-context

From a Git repository, ask:

```text
Use /taf:prepare-repo-context to inspect this repository and prepare reusable bounded context.
```

The first pass is read-only. It reports registered providers, freshness,
eligible and excluded path counts, the native runtime state, and the next safe
action. It does not load the full repository into model context.

If no reusable index is ready, the skill asks before any persistent or network
action. With approval it downloads the matching native runtime from the TAF
GitHub release, verifies the published SHA-256 checksum, stores it in the
user-local TAF state directory, and builds the index outside the repository.
Later sessions reuse the repository/worktree-bound index and receive only
bounded query results rather than the full index.

Once ready, the same skill can answer repository-map, symbol, and documentation
questions with bounded results. It retrieves source snippets only for selected
result identities, keeping paths, line ranges, and evidence class attached.

Runtime requirements are Git and Python 3. Go is not required for normal use.
The published native runtime supports macOS and Linux on amd64/arm64 and
Windows on amd64.

## Versioning and releases

TAF and its skills have separate versions:

- TAF `2.1.1` versions the collection, manifests, namespaces, and release.
- `branch-handoff` `1.2.1` versions its behavior contract.
- `prepare-repo-context` `1.0.0` versions its behavior contract.
- `work-recovery` `1.0.1` versions its behavior contract.

New primary GitHub releases use the TAF product version, beginning with
`v2.0.0`. Historical per-skill releases remain as legacy records.

## Repository structure

```text
the-agentic-fieldbook/
├── .agents/plugins/marketplace.json
├── .claude-plugin/
│   ├── marketplace.json
│   └── plugin.json
├── .codex-plugin/plugin.json
├── skills/
│   ├── branch-handoff/
│   │   ├── SKILL.md
│   │   ├── agents/openai.yaml
│   │   ├── references/
│   │   └── scripts/collect_diff.py
│   ├── prepare-repo-context/
│   │   ├── SKILL.md
│   │   ├── agents/openai.yaml
│   │   └── scripts/prepare_repo_context.py
│   └── work-recovery/
│       ├── SKILL.md
│       ├── agents/openai.yaml
│       ├── references/
│       └── scripts/
└── tests/
```

Both plugin manifests use the same canonical `skills/` directory. Full skill
instructions are not combined into one prompt.

## Verification

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
scripts/vendor-work-recovery-runtime --check
claude plugin validate .
```

Codex packaging can be tested without changing a normal Codex installation by
using a temporary `CODEX_HOME`, adding the repository marketplace, and
installing `taf@the-agentic-fieldbook`.

## Contributing

Issues, skill proposals, forks, and pull requests are welcome. Contributors use
their own GitHub identities; `@Phalegethon` is the maintainer and review owner,
not a required commit identity. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

MIT. See [`LICENSE`](LICENSE).
