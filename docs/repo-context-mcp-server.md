# The repo-context MCP server

TAF bundles an MCP stdio server, `repo-context`, that exposes the same
operations as `prepare-repo-context` as tools. Claude Code starts it when the
plugin is enabled; Codex reads its own manifest. One native engine process
serves the whole session and starts on the first tool call, so repeated
questions do not pay for a process start and index load.

## Tools

| Tool | Answers | Required arguments |
|---|---|---|
| `inspect` | Engine availability, freshness, coverage, state usage, and the next safe action. | `repo` |
| `build` | Builds or rebuilds the bound index. Marked so the host asks before running it. | `repo`, `confirm_state_write: true` |
| `repository_overview` | The directory table, the overview block, and a ranked file layer. | `repo` |
| `repository_map` | A ranked map of files and definitions. | `repo` |
| `search_symbols` | Where a symbol is defined, imported, or referenced. | `repo`, `query` |
| `search_docs` | Headings and passages in documentation. | `repo`, `query` |
| `source_snippets` | The source lines behind selected findings. | `repo`, `result_ids` |
| `related_symbols` | Callers, callees, importers, or imports of earlier findings. | `repo`, `result_ids`, `direction` |
| `changed_symbols` | What a branch or the staged change touches. | `repo` (optional `base` or `staged`) |
| `impact_candidates` | One-hop dependents of the changed symbols, with anchors. | `repo` (optional `base` or `staged`) |

Every tool takes the absolute `repo` path. The server never installs the
native engine, never contacts the network, and never runs `gc` or `remove`;
those and `activate` stay skill commands. A query on a bound index may refresh
it incrementally and prune superseded index generations, under the standing
consent given at the first `build`.

## Tool notes

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
branch's upstream main, then `origin/HEAD`, then a local `main`/`master`, and
uncommitted changes are always included. `staged: true` measures the index
against `HEAD` instead, exactly as `git commit` would record it.
`changed_symbols` accepts the same filters as the other query tools;
`impact_candidates` accepts only `base`, `staged`, `allow_inferred`, and the
two budgets, and composes its answer from one `changed_symbols` call plus one
relationship call per changed symbol and direction over the same engine
process. It answers in two layers, the change set in `changed` and the
candidates in `findings`, so its `maximum_output_characters` defaults to 8000
rather than 4000. Its candidates are attributed in `anchors` to the changed
symbols they depend on; a candidate is a symbol that references changed work,
not a defect.

`repository_overview` answers how a repository is organised in one call: the
directory table in `groups`, the `overview` block, and a ranked file layer in
`findings`. It takes only `path_prefixes`, `languages`, `allow_inferred`, and
the two budgets; `path_prefixes` names whole directory segments and describes
the first of them in sorted order. The group table is sized by the budget
rather than by a fixed row count, and the table and the file layer take at
most half of it each, so this tool's `maximum_output_characters` defaults to
8000 rather than the 4000 the single-layer query tools use.

## Registration

Claude Code starts the server when the plugin is enabled; the tools appear as
`mcp__plugin_taf_repo-context__<tool>` (permission matcher
`mcp__plugin_taf_repo-context__*`), reading the manifest from
`.claude-plugin/mcp.json` (`${CLAUDE_PLUGIN_ROOT}`; the file lives inside the
plugin directory so that a checkout of this repository does not register a
project-level server). Codex reads its own `.codex-plugin/mcp.json`
(`${PLUGIN_ROOT}`), since its Agent Plugins MCP loader does not substitute
`${CLAUDE_PLUGIN_ROOT}`.

If a host does not substitute either variable in the command path, register
the server manually with an absolute path, for example in Codex's
`config.toml`:

```toml
[mcp_servers.repo-context]
command = "python3"
args = ["/absolute/path/to/the-agentic-fieldbook/tools/taf-context/taf_context_mcp.py"]
default_tools_approval_mode = "writes"
```

In that mode Codex prompts only for `build`; the query tools are marked
read-only even though a query may refresh and prune the bound index as
described above.

The manifest names the interpreter `python3`; on Windows installations where
only `python` exists, register the server manually with that name.
