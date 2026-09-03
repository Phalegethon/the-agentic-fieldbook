# Query Result Contract

The same object is returned as `structuredContent` (and as compact JSON text) by the `repo-context` MCP tools; a tool call with `isError: true` carries the error message the script would have printed.

Every `query` invocation returns one JSON object. Read these fields before summarizing.

| Field | Meaning | What to do |
|---|---|---|
| `status` | `ready`: the search examined everything it needed (this also covers results with more matches than `--maximum-results`, where `truncated` is `true` with a counted `omitted_count`). `partial`: two unrelated causes, told apart by `next_safe_action`. With `next_safe_action: use-index`, the *index* has incomplete coverage (parse failures or extraction limits at build time); findings are complete for what was indexed. With `next_safe_action: refine-query` and warning `query-frontier-exhausted`, a work budget stopped the *search*; results may be missing. `stale`/`error`: the index does not match the worktree. `stale` from `query` now means only a refusal the broker could not repair; follow the error message (`run prepare inspect` or `run prepare build --confirm-state-write`). | `partial` + `use-index`: answer normally; mention once that coverage is partial (some files were not indexed). `partial` + `refine-query`: say results may be incomplete and narrow with a filter or fall back to `grep`. On `stale`, run `inspect` and follow `next_safe_action`. |
| `freshness` | `exact` binds the index to the current commit and dirty state. Anything else is not current. | Only `exact` results may be described as current. |
| `truncated` | `true` when the finding list is known to be incomplete for any reason: more matches than `--maximum-results`, the output budget trimmed findings, or the search was exhausted. | Tell the user the list is a prefix. |
| `omitted_count` | Omissions the engine actually counted (ranking overflow and output trimming). It is `0` when the engine could not count what it missed; `truncated` is still `true` then. | Never present `omitted_count: 0` as proof of completeness when `truncated` is `true`. |
| `warnings` | Codes such as `python-dynamic-lookup` (a file uses `getattr`/`eval`/dynamic imports; its literal definitions are still verified) or `query-frontier-exhausted` (a budget stopped the search). | Mention warnings that affect the answer; do not list them all. |
| `next_safe_action` | `use-index`, `refine-query`, `rebuild-index` (a `query` result never returns `build-index` or `install-native-engine`; the broker refuses to query without a ready context, so those two come from `inspect`, `build`, and `activate`). | Follow it; never invent another action. |
| `refresh` | `{performed, changed_path_count, duration_ms}`. `performed: true` means the index was brought to the current commit and dirty state before the search. | Do not report it unless it affects the answer's timing; `performed: false` needs no mention. |

## Findings

Each finding has `rank`, `result_identity`, `path`, `start_line`, `end_line`, `language`, `record_kind`, `qualified_name`, `evidence_class`, and `preview`.

- `evidence_class` is `verified` when the extractor proved the record from a literal token in the current source, `inferred` otherwise (hidden unless `--allow-inferred`). Never upgrade it in prose.
- `preview` is one sanitized line: the first line of the record's range for functions, classes, and methods (for a decorated Python definition that is the decorator line); the first body line under a heading. It is a display hint for choosing the right result. It is not evidence; quote source only from `source-snippets`. A heading directly followed by another heading has an empty preview.
- `record_kind` tells you what matched: `definition`, `import`, `module`, `entry-point`, `configuration` (a key name), `heading`, `document-chunk`. Within a match tier, findings are ordered: definitions, modules, entry points and headings first; configuration keys and document chunks next; imports last; then by path.

## Presenting results

Report `path:start_line-end_line`, the qualified name, the evidence class, and the preview for each finding you use. State `truncated` plainly, and state `partial` according to its cause (see the `status` row: `use-index` is partial coverage, `refine-query` is an exhausted search) in one sentence. Do not paste raw JSON. Do not describe what a symbol does beyond its preview unless you fetched the snippet.
