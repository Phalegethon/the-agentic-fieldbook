# Query Routing

Turn the user's question into exactly one bounded query. Prefer the smallest operation that can answer it.

| The user asks | Run | Notes |
|---|---|---|
| How is this repository organized? Where is the code? Where do I start? | `repository-overview` | One call, no query text and no identity: a directory table in `groups` with per-prefix counts and languages, plus a ranked file layer in `findings` that leads with entry points and well-known entry file names. Start here in an unfamiliar repository. |
| Where is `X` defined? What is `X`? | `search-symbols --query X` | Add `--symbol-kind definition` when the name is heavily imported. Definitions rank above imports by default. |
| Who imports `X`? (import statements naming `X`) | `search-symbols --query X --symbol-kind import` | Import records only. For call sites, use the `callers` direction below; call sites are not `search-symbols` results in this version. |
| Who calls `X`? | `search-symbols --query X` to get its `result_identity`, then `related-symbols --result-id <identity> --direction callers` | Two-step: `related-symbols` follows the relationships of a result you already have, it does not search by name. Verified edges only unless `--allow-inferred` is set. |
| What does `X` depend on / call? | `search-symbols --query X` then `related-symbols --result-id <identity> --direction callees` | Same two-step shape; findings are the definitions `X` calls. |
| Who uses module `M`? | `search-symbols --query M` (or `repository-map --path-prefix <M's path>`) then `related-symbols --result-id <identity> --direction importers` | Findings are the importing files' `import` records themselves — real, snippet-able results. |
| What does `X` import? | `search-symbols --query X` then `related-symbols --result-id <identity> --direction imports` | Findings are `import` records in `X`'s file, resolved to a definition or module record where one exists in the index; imports of packages outside the index produce no finding. |
| What did I change on this branch? Which symbols did I touch? | `changed-symbols` | One step, no identity: the definitions, entry points, and modules whose lines a changed hunk touches. Add `--base <ref>` only for a base the user named. |
| What could my change break? What depends on what I changed? | `impact-candidates` | One step as well: the callers of the changed definitions and entry points and the importers of the changed modules and definitions, each candidate attributed in `anchors` to the changed symbols it depends on. Verified edges only unless `--allow-inferred` is set. |
| Something about "snapshot" / "state paths" (a concept, not an exact name) | `search-symbols --query <words>` | Every word must match. Words split camelCase and snake_case, so `snapshot` finds `collect_snapshot` and `RepositorySnapshot`. |
| What do the docs say about `Y`? Where is the section on `Y`? | `search-docs --query <words>` | Headings rank above body chunks. Use two or three words from the expected heading. |
| What is in directory `D`? Which modules exist there? | `repository-overview --path-prefix D/`, then `repository-map --path-prefix D/` | The overview answers with that subtree's own directory table and its ranked files; `repository-map` is the flat listing, one representative record per file in path order. |
| Only Go / only Python / only this folder | add `--language <l>` / `--path-prefix <p>` | Language values are case-insensitive. |

## Filter values

- `--language`: `go`, `javascript`, `json`, `markdown`, `python`, `rust`, `toml`, `typescript`.
- `--symbol-kind`: `configuration`, `definition`, `document-chunk`, `entry-point`, `heading`, `import`, `module`.
- `--source-type`: `source`, `document`, `configuration`.
- `--path-prefix`: a repository-relative prefix such as `tools/taf-context/`. `repository-overview` reads it as whole directory segments, so a file path or a partial segment answers with an empty table and the warning `overview-root-not-a-directory` there, and it describes only one subtree: several prefixes are not composed, the first in sorted order becomes the root and the warning `overview-root-first-prefix` says so.
- `--direction`: `callers`, `callees`, `importers`, `imports`. Required by `related-symbols` and rejected by every other operation. `related-symbols` also requires one or more `--result-id` values (anchor identities from an earlier query, at most 16) and accepts no `--query`.
- `repository-overview` accepts only `--path-prefix` and `--language`; it rejects `--query`, `--result-id`, `--direction`, `--base`, `--symbol-kind`, and `--source-type`, each with a message naming the flag to drop.
- `--base`: a Git ref or commit the change set is measured against. Accepted only by `changed-symbols` and `impact-candidates`, and rejected by every other operation; both of them reject `--query`, `--result-id`, and `--direction`.

All three filter flags (`--language`, `--symbol-kind`, `--source-type`) are case-insensitive. `--path-prefix` is case-sensitive and matches the repository-relative path exactly as stored.

An unknown value fails fast and the error lists the valid values.

## Query text

- Exact names win: `collect_snapshot`, `query.Search`, `Install TAF`.
- Multi-word queries intersect: every word must match some part of the record's name or terms.
- Prefixes of a word always match. Substrings and close misspellings (edit distance ≤ 2, words of at least four characters) are tried only when a word has no exact or prefix match, so `service` finds `ServiceWorker` but not `microservice`; search for `microservice` or `micro` instead.
- Type the bare name: trailing `()` or other punctuation is not stripped from the query.

## Change questions

- The change set is the working tree against a resolved base: committed, staged, unstaged, and untracked changes together. The base is the ref the user named with `--base`, else the branch's upstream main, else `origin/HEAD`, else a local `main`/`master`. When none of those exists the result carries `base-unresolved` and covers uncommitted changes only; say so.
- A changed symbol is a definition, entry point, or module record whose line span meets a changed hunk of the same path. A deleted file leaves no record, so it appears only as the warning `changed-path-not-indexed`; the same warning covers any changed path the index carries no record for.
- **Narrow a large change set with `--path-prefix D/` before anything else** (or with `--language`): the filters apply to the changed set of both operations, and cutting a forty-symbol diff down to the directory the question is about is what keeps the answer readable and cheap. Raise `--maximum-results` only after that. The `impact_candidates` MCP tool accepts no filters, so use the script when a change set needs narrowing.
- `impact-candidates` follows at most 64 changed symbols and asks one relationship question per changed symbol and direction (`callers` for a changed definition or entry point, `importers` for a changed module or definition), so its cost grows with the change set. It answers in two layers - the change set in `changed` and the candidates in `findings` - so it defaults to 8000 output characters on both surfaces, not the 4000 the single-layer queries use; under budget pressure `changed` still shrinks to identities and then drops from the tail with `changed-list-trimmed`, so a trimmed `changed` list means asking `changed-symbols` separately or narrowing the change set.
- Neither operation reopens a file: the change set only selects records the index already carries, and the incremental refresh the query performs first is what brings edited, new, and untracked files into it. A path the index excludes stays outside the answer and is reported as `changed-path-not-indexed`.

## Overview questions

- The group table has no fixed width; the output budget sizes it, and the table and the file layer take at most half of it each. A table over its half folds its tail into `*` until it fits, down to one directory row; a table inside its half is kept whole and the file layer gets the rest. So 4000 characters answer with a short table and a few files, and 8000 - the default on both surfaces - carries this repository's whole table and about eight files. Ask for `--maximum-output-characters 12000` when the answer should be wider still, and use `--path-prefix D/` to go deeper into one subtree rather than wider over all of them. Only a budget that not even one row with no files fits in is reported as `output-budget-exceeded`.
- Read the table first and query second: a group's `representative_identity` and every file-layer finding is an ordinary record identity, never a `reference`, so `source-snippets` can fetch one without another search; `related-symbols` still needs an identity that names a `definition`, `module`, or `entry-point` record.
- Narrow with `--path-prefix D/` to describe one subtree, or with `--language` to count only that language's files. Neither changes what a row means: prefixes stay relative to the repository root.

## When `grep` or `rg` is the better tool

- Regular expressions, string literals, comments, log messages, or configuration values (the index stores names and headings, not values).
- Languages outside the supported set, generated code, or vendored trees (they are excluded from the index).
- A single file the user already named.
- The index reports `partial` with `next_safe_action: refine-query` twice for the same question after narrowing with a filter (a `partial` result with `next_safe_action: use-index` is usable bounded coverage, not a reason to switch tools).

## Retry discipline

Run one query. If `next_safe_action` is `refine-query`, narrow once with a filter or a more specific name. Do not loop more than twice; fall back to `grep` and say so.
