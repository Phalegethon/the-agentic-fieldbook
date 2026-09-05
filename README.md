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
| [`prepare-repo-context`](skills/prepare-repo-context) | 1.7.4 | Inspect the native engine and index state, prepare a reusable native index, run bounded evidence queries, repository overviews, symbol relationships, and change-impact questions, and reclaim unused index state without loading the full repository into model context. |
| [`work-recovery`](skills/work-recovery) | 1.1.0 | Recover interrupted work and the single best next step from bounded, read-only Git evidence, optionally naming the symbols the work touched. |

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
- On an explicit request for indexed context, adds at most a readiness check
  and one read-only `prepare-repo-context` change-impact query against an
  index that is already ready, and reports the symbols the work touched with
  their one-hop dependents. It never builds, installs, or downloads an index,
  and it never delays or replaces the native report.
- Writes no recovery artifact and reports exact evidence omissions within the
  selected context budget.

Runtime requirements are Git and Python 3. The bundled collector uses only the
Python standard library.

## Use prepare-repo-context

From a Git repository, ask:

```text
Use /taf:prepare-repo-context to inspect this repository and prepare reusable bounded context.
```

The first pass is read-only. It reports native engine availability, freshness,
eligible and excluded path counts, user-local state usage, and the next safe
action. It does not load the full repository into model context.

If no reusable index is ready, the skill asks before any persistent or network
action. With approval it downloads the matching native runtime from the TAF
GitHub release, verifies the published SHA-256 checksum, stores it in the
user-local TAF state directory, and builds the index outside the repository.
Later sessions reuse the repository/worktree-bound index and receive only
bounded query results rather than the full index.
After commits or edits the next query refreshes the bound index incrementally;
a full rebuild is only asked for after a runtime upgrade.

Once ready, the same skill can answer repository-overview, repository-map,
symbol, documentation, and relationship questions with bounded results.
Findings carry paths, line ranges, evidence class, and a one-line preview;
multi-word queries intersect their words, and `--language`, `--symbol-kind`,
and `--path-prefix` filters narrow them. A relationship question ("who calls
X", "what does X depend on", "who uses module M") is answered in two steps: a
query that finds the symbol, then `related-symbols --result-id <identity>
--direction callers|callees|importers|imports`, whose findings additionally
carry `relation`, `edge_evidence`, `reference_line`, and `reference_count`. The
skill's `references/query-routing.md` maps questions to queries and
`references/result-contract.md` explains `status`, `truncated`,
`omitted_count`, and the relationship fields. It retrieves source snippets
only for selected result identities.

An unfamiliar repository starts with one `query --operation repository-overview`
call, which needs neither a query nor an identity: it returns a directory table
in `groups` with, per prefix, the file, definition, entry-point, document, and
configuration counts and the languages; an `overview` block naming the
described root and the counted files; and a ranked file layer that leads with
entry points and well-known entry file names such as `main.go` under `cmd/`,
`index.ts`, or a Next.js App Router `page.tsx`. `--path-prefix D/` describes
one subtree, and `--language` counts one language's files. The table has no
fixed width: the output budget sizes it, and the table and the file layer take
at most half of it each — a table wider than its half folds its tail into a
`*` row until it fits, a table inside its half is kept whole, and the file
layer keeps everything the table did not spend. This operation therefore
defaults to 8000 characters rather than the 4000 the single-layer queries use;
`--maximum-output-characters 12000` buys a wider table and more files, while
`--path-prefix D/` is the way to go deeper into one subtree.

A change question is answered in one step and needs no identity.
`query --operation changed-symbols` reports the definitions, entry points,
and modules whose lines a changed hunk touches, and
`query --operation impact-candidates` reports their one-hop callers and
importers, each candidate attributed in `anchors` to the changed symbols it
depends on. Both compare the working tree, including staged, unstaged, and
untracked changes, with a base resolved as the branch's upstream main, then
`origin/HEAD`, then a local `main`/`master`, and `--base <ref>` selects
another one. Neither reopens a source file: the change set only selects
records the index already carries. `impact-candidates` asks the engine one
relationship question per changed symbol and direction, so its cost grows
with the change set; narrow a large one with `--path-prefix` before raising
`--maximum-results`.

Index state lives in the user-local TAF state directory, never in the
repository. `prepare_repo_context.py remove --repo <repo>` deletes the entry
for one repository worktree and `prepare_repo_context.py gc` reclaims orphaned
entries, entries not used for 30 days (`--unused-for`), runtime versions other
than the current one, generations no longer referenced by an index, leftover
control files, interrupted deletions, and the empty parent directories they
leave behind. Both commands only report what they would delete until
`--confirm-state-write` is supplied.

Runtime requirements are Git and Python 3. Go is not required for normal use.
The published native runtime supports macOS and Linux on amd64/arm64 and
Windows on amd64.

### The repo-context MCP server

TAF bundles an MCP stdio server, `repo-context`, that exposes the same
operations as `prepare-repo-context` as tools: `inspect`, `build`,
`repository_map`, `search_symbols`, `search_docs`, `source_snippets`,
`related_symbols`, `changed_symbols`, `impact_candidates`, and
`repository_overview`. Every tool takes the absolute `repo` path; `build`
requires `confirm_state_write: true` and is marked so the host asks before
running it.
The server never installs the native engine, never contacts the network, and
never runs `gc` or `remove`; those and `activate` stay skill commands. A query
on a bound index may refresh it incrementally and prune superseded index
generations, under the standing consent given at the first `build`. One
native engine process serves the whole session and starts on the first tool
call, so repeated questions do not pay for a process start and index load.

`related_symbols` follows the relationships of one or more `result_ids` from
an earlier query in one required `direction`: `callers` (who calls this),
`callees` (what this calls), `importers` (who imports this module), or
`imports` (what this imports). Every finding carries `relation`,
`edge_evidence`, `reference_line`, and `reference_count`; `edge_evidence:
inferred` is a name match, never proof, and stays hidden unless
`allow_inferred` is set. Reference records that back these edges are never
returned by the other query tools and cannot be fetched with
`source_snippets`.

`changed_symbols` and `impact_candidates` answer from a Git difference
instead of a query string or a result identity, so neither takes `query` or
`result_ids`. Both accept an optional `base`; without it the base is the
branch's upstream main, then `origin/HEAD`, then a local `main`/`master`,
and uncommitted changes are always included. `changed_symbols` accepts the
same filters as the other query tools; `impact_candidates` accepts only
`base`, `allow_inferred`, and the two budgets, and composes its answer from
one `changed_symbols` call plus one relationship call per changed symbol and
direction over the same engine process. It answers in two layers — the change
set in `changed` and the candidates in `findings` — so its
`maximum_output_characters` defaults to 8000 rather than 4000. Its candidates are attributed in
`anchors` to the changed symbols they depend on; a candidate is a symbol
that references changed work, not a defect.

`repository_overview` answers how a repository is organized in one call: the
directory table in `groups`, the `overview` block, and a ranked file layer in
`findings`. It takes only `path_prefixes`, `languages`, `allow_inferred`, and
the two budgets; `path_prefixes` names whole directory segments and describes
the first of them in sorted order. The group table is sized by the budget
rather than by a fixed row count, and the table and the file layer take at
most half of it each, so this tool's `maximum_output_characters` defaults to
8000 rather than the 4000 the single-layer query tools use; a larger value
answers with a wider table and more files.

Claude Code starts the server when the plugin is enabled; the tools appear as
`mcp__plugin_taf_repo-context__<tool>` (permission matcher
`mcp__plugin_taf_repo-context__*`), reading its manifest from
`.claude-plugin/mcp.json` (`${CLAUDE_PLUGIN_ROOT}`; the file lives inside the
plugin directory so that a checkout of this repository does not register a
project-level server). Codex reads its own `.codex-plugin/mcp.json`
(`${PLUGIN_ROOT}`), since its Agent Plugins MCP loader does not substitute
`${CLAUDE_PLUGIN_ROOT}`. If a host does not substitute either variable in the
command path, register the server manually with an absolute path, for
example in Codex's `config.toml`:

    [mcp_servers.repo-context]
    command = "python3"
    args = ["/absolute/path/to/the-agentic-fieldbook/tools/taf-context/taf_context_mcp.py"]
    default_tools_approval_mode = "writes"

In that mode Codex prompts only for `build`; the query tools are marked
read-only even though a query may refresh and prune the bound index as
described above.

The manifest names the interpreter `python3`; on Windows installations where
only `python` exists, register the server manually with that name.

## Versioning and releases

TAF and its skills have separate versions:

- TAF `2.7.2` versions the collection, manifests, namespaces, and release.
- `branch-handoff` `1.2.1` versions its behavior contract.
- `prepare-repo-context` `1.7.4` versions its behavior contract.
- `work-recovery` `1.1.0` versions its behavior contract.

New primary GitHub releases use the TAF product version, beginning with
`v2.0.0`. Historical per-skill releases remain as legacy records.

## Repository structure

```text
the-agentic-fieldbook/
├── .agents/plugins/marketplace.json
├── .claude-plugin/
│   ├── marketplace.json
│   ├── mcp.json
│   └── plugin.json
├── .codex-plugin/
│   ├── mcp.json
│   └── plugin.json
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
