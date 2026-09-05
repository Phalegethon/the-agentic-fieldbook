# prepare-repo-context

Inspect the native engine and index state, prepare a reusable native index,
run bounded evidence queries, repository overviews, symbol relationships, and
change-impact questions, warn at commit time about dependents left behind, and
reclaim unused index state, all without loading the full repository into model
context.

Skill version: see the `version` field in
[`skills/prepare-repo-context/SKILL.md`](../skills/prepare-repo-context/SKILL.md).
Related pages: [Commit-time impact warning](commit-time-impact-hook.md) and
[The repo-context MCP server](repo-context-mcp-server.md).

## Ask it

From a Git repository:

```text
Use /taf:prepare-repo-context to inspect this repository and prepare reusable bounded context.
```

Once the index is ready, ask repository questions directly:

```text
How is this repository organized, and where should I start reading?
Who calls apply_discount, and what does checkout depend on?
What did I change on this branch, and what could it affect?
What is my staged change about to affect?
```

## The first pass is read-only

The first pass reports native engine availability, freshness, eligible and
excluded path counts, user-local state usage, and the next safe action. It
does not load the full repository into model context.

If no reusable index is ready, the skill asks before any persistent or network
action. With approval it downloads the matching native runtime from the TAF
GitHub release, verifies the published SHA-256 checksum, stores it in the
user-local TAF state directory, and builds the index outside the repository.
Later sessions reuse the repository/worktree-bound index and receive only
bounded query results rather than the full index. After commits or edits the
next query refreshes the bound index incrementally; a full rebuild is only
asked for after a runtime upgrade.

## Queries

Once ready, the same skill answers repository-overview, repository-map,
symbol, documentation, and relationship questions with bounded results.
Findings carry paths, line ranges, evidence class, and a one-line preview;
multi-word queries intersect their words, and `--language`, `--symbol-kind`,
and `--path-prefix` filters narrow them. The skill's
`references/query-routing.md` maps questions to queries and
`references/result-contract.md` explains `status`, `truncated`,
`omitted_count`, and the relationship fields. Source snippets are retrieved
only for selected result identities.

| Operation | Answers | Needs |
|---|---|---|
| `repository-overview` | How the repository is organised: a directory table, an overview block, and a ranked file layer leading with entry points. | nothing |
| `repository-map` | A ranked map of files and definitions. | optional query text |
| `search-symbols` | Where a symbol is defined, imported, or referenced. | query text |
| `search-docs` | Headings and passages in documentation. | query text |
| `source-snippets` | The source lines behind selected findings. | result identities |
| `related-symbols` | Who calls X, what X calls, who imports M, what M imports. | one result identity and a `--direction` |
| `changed-symbols` | The definitions, entry points, and modules a branch or the staged change touches. | optional `--base` or `--staged` |
| `impact-candidates` | Their one-hop callers and importers, each attributed to the changed symbols it depends on. | optional `--base` or `--staged` |

### Repository overview

An unfamiliar repository starts with one `query --operation repository-overview`
call, which needs neither a query nor an identity: it returns a directory table
in `groups` with, per prefix, the file, definition, entry-point, document, and
configuration counts and the languages; an `overview` block naming the
described root and the counted files; and a ranked file layer that leads with
entry points and well-known entry file names such as `main.go` under `cmd/`,
`index.ts`, or a Next.js App Router `page.tsx`. `--path-prefix D/` describes
one subtree, and `--language` counts one language's files. The table has no
fixed width: the output budget sizes it, and the table and the file layer take
at most half of it each. A table wider than its half folds its tail into a
`*` row until it fits, a table inside its half is kept whole, and the file
layer keeps everything the table did not spend. This operation therefore
defaults to 8000 characters rather than the 4000 the single-layer queries use;
`--maximum-output-characters 12000` buys a wider table and more files, while
`--path-prefix D/` is the way to go deeper into one subtree.

### Relationship questions

A relationship question ("who calls X", "what does X depend on", "who uses
module M") is answered in two steps: a query that finds the symbol, then
`related-symbols --result-id <identity> --direction callers|callees|importers|imports`,
whose findings additionally carry `relation`, `edge_evidence`,
`reference_line`, and `reference_count`. `edge_evidence: inferred` is a name
match, never proof, and stays hidden unless `--allow-inferred` is set.

### Change questions

A change question is answered in one step and needs no identity.
`query --operation changed-symbols` reports the definitions, entry points,
and modules whose lines a changed hunk touches, and
`query --operation impact-candidates` reports their one-hop callers and
importers, each candidate attributed in `anchors` to the changed symbols it
depends on. Both compare the working tree, including staged, unstaged, and
untracked changes, with a base resolved as the branch's upstream main, then
`origin/HEAD`, then a local `main`/`master`, and `--base <ref>` selects
another one. "What am I about to commit?" is `--staged` instead: it measures
the index against `HEAD` exactly as `git commit` would record it, excludes
unstaged and untracked edits, and is exclusive with `--base`. Neither reopens
a source file: the change set only selects records the index already carries.
`impact-candidates` asks the engine one relationship question per changed
symbol and direction, so its cost grows with the change set; narrow a large
one with `--path-prefix` before raising `--maximum-results`. `--path-prefix`
narrows the changed side, not the affected side: name the directory you
changed (the library) and the candidates show where it is used (the app).

A candidate is a symbol that references changed work, not a defect. The
report names it with its anchors and leaves the judgement of whether it
breaks to a review of that call.

### Budgets and filters

- `--maximum-output-characters` accepts 2000, 4000, 8000, or 12000. Single-layer
  queries default to 4000; `repository-overview` and `impact-candidates`
  default to 8000 because they answer in two layers.
- `--maximum-results` accepts 1 to 64.
- `--language`, `--symbol-kind`, `--path-prefix`, and `--source-type
  source|document|configuration` narrow a query.

## State, removal, and garbage collection

Index state lives in the user-local TAF state directory, never in the
repository:

| Platform | Default state directory |
|---|---|
| macOS | `~/Library/Application Support/TAF/context` |
| Linux | `$XDG_STATE_HOME/taf/context` or `~/.local/state/taf/context` |
| Windows | `%LOCALAPPDATA%\TAF\context` |

`TAF_STATE_HOME` overrides the location for one process, which is how the test
suite and the demo recordings keep a scratch state.

`prepare_repo_context.py remove --repo <repo>` deletes the entry for one
repository worktree and `prepare_repo_context.py gc` reclaims orphaned
entries, entries not used for 30 days (`--unused-for`), runtime versions other
than the current one, generations no longer referenced by an index, leftover
control files, interrupted deletions, and the empty parent directories they
leave behind. Both commands only report what they would delete until
`--confirm-state-write` is supplied.

## Runtime requirements

Git and Python 3. Go is not required for normal use. The published native
runtime supports macOS and Linux on amd64/arm64 and Windows on amd64, and
extracts Python, TypeScript, JavaScript, Go, Rust, and Markdown.
