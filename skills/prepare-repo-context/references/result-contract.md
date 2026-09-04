# Query Result Contract

The same object is returned as `structuredContent` (and as compact JSON text) by the `repo-context` MCP tools; a tool call with `isError: true` carries the error message the script would have printed.

Every `query` invocation returns one JSON object. Read these fields before summarizing.

| Field | Meaning | What to do |
|---|---|---|
| `status` | `ready`: the search examined everything it needed (this also covers results with more matches than `--maximum-results`, where `truncated` is `true` with a counted `omitted_count`). `partial`: two unrelated causes, told apart by `next_safe_action`. With `next_safe_action: use-index`, the *index* has incomplete coverage (parse failures or extraction limits at build time); findings are complete for what was indexed. With `next_safe_action: refine-query` and warning `query-frontier-exhausted`, a work budget stopped the *search*; results may be missing. `stale`/`error`: the index does not match the worktree. `stale` from `query` now means only a refusal the broker could not repair; follow the error message (`run prepare inspect` or `run prepare build --confirm-state-write`). `stale` with `next_safe_action: update-index` and no findings means the request named an identity the operation cannot use for that purpose — for example a `related-symbols --result-id` that is not a `definition`, `module`, or `entry-point` record, or a `source-snippets`/`related-symbols` identity naming a `reference` record. | `partial` + `use-index`: answer normally; mention once that coverage is partial (some files were not indexed). `partial` + `refine-query`: say results may be incomplete and narrow with a filter or fall back to `grep`. `stale` + `update-index`: re-run the query that produced the identity and use a fresh one; do not retry the same identity. On any other `stale`, run `inspect` and follow `next_safe_action`. |
| `freshness` | `exact` binds the index to the current commit and dirty state. Anything else is not current. | Only `exact` results may be described as current. |
| `truncated` | `true` when the finding list is known to be incomplete for any reason: more matches than `--maximum-results`, the output budget trimmed findings, or the search was exhausted. | Tell the user the list is a prefix. |
| `omitted_count` | Omissions the engine actually counted (ranking overflow and output trimming). It is `0` when the engine could not count what it missed; `truncated` is still `true` then. | Never present `omitted_count: 0` as proof of completeness when `truncated` is `true`. |
| `warnings` | Codes such as `python-dynamic-lookup` (a file uses `getattr`/`eval`/dynamic imports; its literal definitions are still verified) or `query-frontier-exhausted` (a budget stopped the search). | Mention warnings that affect the answer; do not list them all. |
| `next_safe_action` | `use-index`, `refine-query`, `rebuild-index`, `update-index` (a `query` result never returns `build-index` or `install-native-engine`; the broker refuses to query without a ready context, so those two come from `inspect`, `build`, and `activate`). | Follow it; never invent another action. |
| `refresh` | `{performed, changed_path_count, duration_ms}`. `performed: true` means the index was brought to the current commit and dirty state before the search. | Do not report it unless it affects the answer's timing; `performed: false` needs no mention. |

## Findings

Each finding has `rank`, `result_identity`, `path`, `start_line`, `end_line`, `language`, `record_kind`, `qualified_name`, `evidence_class`, and `preview`.

- `evidence_class` is `verified` when the extractor proved the record from a literal token in the current source, `inferred` otherwise (hidden unless `--allow-inferred`). Never upgrade it in prose.
- `preview` is one sanitized line: the first line of the record's range for functions, classes, and methods (for a decorated Python definition that is the decorator line); the first body line under a heading. It is a display hint for choosing the right result. It is not evidence; quote source only from `source-snippets`. A heading directly followed by another heading has an empty preview.
- A `source-snippets` result that is `partial` with `returned_count` 0 and `omitted_count` 1 means the snippet did not fit the output budget: ask again with a larger budget (`--maximum-output-characters 8000` or `12000` for the script, `maximum_output_characters` for the tool).
- `record_kind` tells you what matched: `definition`, `import`, `module`, `entry-point`, `configuration` (a key name), `heading`, `document-chunk`. Within a match tier, findings are ordered: definitions, modules, entry points and headings first; configuration keys and document chunks next; imports last; then by path.

## Relationship findings (`related-symbols`)

A `related-symbols` finding carries four extra fields beyond the ones above: `relation` (`call` or `import`), `edge_evidence` (`verified` or `inferred`), `reference_line`, and `reference_count`. These four fields are present only on `related-symbols` findings; every other operation's findings do not carry these keys at all, so do not look for them there.

- `edge_evidence: verified` means the resolution from the reference to this specific record was unambiguous (same module scope, or through a single matching import). `edge_evidence: inferred` is a **name match, never proof** — the engine found a definition with the right name somewhere in the index but could not prove it is the one actually called. `related-symbols` hides `inferred` edges by default; only `--allow-inferred` (`allow_inferred` over MCP) returns them, and you must still present them as unconfirmed, not as fact.
- `reference_line` is the source line of the call or import that produced this edge. `reference_count` is how many times that same target name is referenced from the same enclosing symbol (merged occurrences, not a count of distinct call sites elsewhere).
- A `callers` finding for a bare module-level call (outside any function, class, or method) is synthesized with `record_kind: module`; its `result_identity` belongs to the underlying `reference` record, and `source-snippets` refuses it by design (a `reference` identity is never snippet-able). Report the finding's `path` and `start_line` directly instead of fetching a snippet for it.
- The summary object's own `schema_version` field stays `"1"` for every operation, including `related-symbols` — it names the broker's envelope shape, not the four relationship fields, which are carried on the findings regardless of that value.

## Presenting results

Report `path:start_line-end_line`, the qualified name, the evidence class, and the preview for each finding you use. State `truncated` plainly, and state `partial` according to its cause (see the `status` row: `use-index` is partial coverage, `refine-query` is an exhausted search) in one sentence. Do not paste raw JSON. Do not describe what a symbol does beyond its preview unless you fetched the snippet. For a `related-symbols` finding, also state the `relation` and, when `edge_evidence` is `inferred`, say plainly that the edge is a name match rather than a confirmed call or import.
