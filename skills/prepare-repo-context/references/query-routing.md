# Query Routing

Turn the user's question into exactly one bounded query. Prefer the smallest operation that can answer it.

| The user asks | Run | Notes |
|---|---|---|
| Where is `X` defined? What is `X`? | `search-symbols --query X` | Add `--symbol-kind definition` when the name is heavily imported. Definitions rank above imports by default. |
| Who imports or uses `X`? | `search-symbols --query X --symbol-kind import` | Import records only; call sites are not indexed in this version. |
| Something about "snapshot" / "state paths" (a concept, not an exact name) | `search-symbols --query <words>` | Every word must match. Words split camelCase and snake_case, so `snapshot` finds `collect_snapshot` and `RepositorySnapshot`. |
| What do the docs say about `Y`? Where is the section on `Y`? | `search-docs --query <words>` | Headings rank above body chunks. Use two or three words from the expected heading. |
| What is in directory `D`? Which modules exist there? | `repository-map --path-prefix D/` | One representative record per file, in path order. |
| Only Go / only Python / only this folder | add `--language <l>` / `--path-prefix <p>` | Language values are case-insensitive. |

## Filter values

- `--language`: `go`, `javascript`, `json`, `markdown`, `python`, `rust`, `toml`, `typescript`.
- `--symbol-kind`: `configuration`, `definition`, `document-chunk`, `entry-point`, `heading`, `import`, `module`.
- `--source-type`: `source`, `document`, `configuration`.
- `--path-prefix`: a repository-relative prefix such as `tools/taf-context/`.

All three filter flags are case-insensitive. `--path-prefix` is case-sensitive and matches the repository-relative path exactly as stored.

An unknown value fails fast and the error lists the valid values.

## Query text

- Exact names win: `collect_snapshot`, `query.Search`, `Install TAF`.
- Multi-word queries intersect: every word must match some part of the record's name or terms.
- Prefixes of a word always match. Substrings and close misspellings (edit distance ≤ 2, words of at least four characters) are tried only when a word has no exact or prefix match, so `service` finds `ServiceWorker` but not `microservice`; search for `microservice` or `micro` instead.
- Type the bare name: trailing `()` or other punctuation is not stripped from the query.

## When `grep` or `rg` is the better tool

- Regular expressions, string literals, comments, log messages, or configuration values (the index stores names and headings, not values).
- Languages outside the supported set, generated code, or vendored trees (they are excluded from the index).
- A single file the user already named.
- The index reports `partial` with `next_safe_action: refine-query` twice for the same question after narrowing with a filter (a `partial` result with `next_safe_action: use-index` is usable bounded coverage, not a reason to switch tools).

## Retry discipline

Run one query. If `next_safe_action` is `refine-query`, narrow once with a filter or a more specific name. Do not loop more than twice; fall back to `grep` and say so.
