# Optional Context Actions

Native Git recovery is complete without an index or third-party provider. Do not discover, install, build, update, query, or contact a provider automatically.

When the user explicitly asks to use indexed/provider context or supplies provider evidence:

- prefer an already-present project architecture/index manifest over building a new index;
- verify repository identity, HEAD/dirty binding, freshness, capabilities, and locality before using it;
- treat current Git facts as authoritative for worktree state;
- use fresh provider evidence only for relationships it actually covers;
- preserve stale, partial, denied, or unavailable provider status and continue with native evidence;
- request separate, exact consent before any provider execution, network access, installation, index build/update, or persistent write.

## The bundled repository-context index

TAF ships `prepare-repo-context` in the same plugin, so an index it already built is the one already-present index this skill may reuse. Use it only on an explicit request for indexed context, and take at most these two calls, in this order.

1. Readiness, once: the `repo-context` MCP tool `inspect` with the absolute `repo` path, or, when that server is not in the session, `<python> <skill-dir>/../prepare-repo-context/scripts/prepare_repo_context.py inspect --repo <repo>`. Anything other than `next_safe_action: use-index` ends the step: report in one sentence that indexed context is not ready, and never offer to prepare it as part of a recovery.
2. Exactly one query, only after `use-index`: the tool `impact_candidates` with `repo` and `base`, or `<python> <skill-dir>/../prepare-repo-context/scripts/prepare_repo_context.py query --repo <repo> --operation impact-candidates --base <base>`. `<base>` is the `base_sha` of the dossier's current workstream. When that field is null, omit `base` entirely: the query resolves a base by the same rule the collector used, so it will not resolve one either, and its answer then carries `base-unresolved` and covers uncommitted work only. Say so.

Read the answer with `../prepare-repo-context/references/result-contract.md`. Render it as the recovery contract's “Symbols touched and one-hop dependents” section and nothing more: never a second query, never `search-symbols`, `related-symbols`, or `source-snippets` alongside it, and never a snippet or file read to explain a candidate.

Boundaries this step does not move:

- `build`, `activate`, `gc`, and `remove` stay out of a recovery. Nothing here installs a runtime, downloads anything, or contacts the network; a missing engine or a missing index is simply "not ready".
- The readiness check and the query may bring an index that already exists up to the current commit and dirty state, under the standing consent its owner gave at its first build. They never create one.
- Current Git facts stay authoritative. A candidate is a symbol that references something the change set touched, not a defect and not a work-state claim; the dossier's state enum never changes because of it.
- Index answers are bounded results, not a corpus: report the findings the query returned and never widen the budget to pull in more.

Never make recovery wait for a full-project index. Never place a full index or large source/document corpus into model context; request only the smallest relevant slice if separately authorized.
