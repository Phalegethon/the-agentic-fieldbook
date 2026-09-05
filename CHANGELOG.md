# Changelog

All notable TAF product releases and their bundled skill versions are recorded
here. Skills keep independent behavior versions inside their `SKILL.md` files.

## [Unreleased]

Native runtime `0.6.0` (unchanged; no new engine download). Skill: `prepare-repo-context` 1.8.0.

### Added

- `changed-symbols` and `impact-candidates` accept `--staged` (`staged: true`
  over MCP), measuring the index against `HEAD` exactly as `git commit`
  would record it and excluding unstaged and untracked edits. Exclusive
  with `--base`; the result's `base.source` reports `staged`.
- `prepare hook install/remove/status --repo <repo>` manages an optional
  `pre-commit` launcher, and the launcher's `hook run` warns about
  dependent files a commit leaves behind: one line per verified, untouched
  dependent file (`TAF: <symbol> changed; <path>:<line> depends on it and is
  not in this commit`) - a file that is both a call site and an import
  reference keeps its call line, and production files print before test
  files - at most five plus a summary line counting the rest. It is
  advisory only - stderr only, exit 0 always, silent whenever the index is
  not ready, `TAF_HOOK=0` is set, or it has waited its 3 seconds for the
  answer - and only `install`/`remove` write, under `--confirm-hook-write`
  and only inside the repository's own hooks directory. Chaining keeps an
  existing foreign hook: one that runs decides the commit with its own exit
  code, one that cannot be run is skipped. macOS and Linux only.

## [2.7.3] - 2026-09-05

Native runtime `0.6.0` (unchanged; no new engine download). ### Changed

- `impact-candidates` now defaults `maximum_results` to 16 on both the CLI
  and the MCP tool, not the shared 8: the candidates are what this operation
  was asked for, not context alongside them, and the output budget already
  bounds what the answer carries. On a small change set the shared 8 bound
  the answer while its 8000-character output budget went unused.

## [2.7.2] - 2026-09-05

Native runtime `0.6.0` (unchanged; no new engine download). Skill: `prepare-repo-context` 1.7.4.

### Fixed

- `impact-candidates` no longer drops a `changed` entry from its output-budget
  trim just because the changed layer alone exceeded its third of the budget:
  once the candidates and the changed layer's own tail trim have settled, any
  budget the whole object still has to spare is given back to `changed`,
  restoring entries in the order they were retained until nothing more fits.
  `changed_trimmed_count` and the `changed-list-trimmed` warning now only ever
  report what genuinely could not fit in the budget.

## [2.7.1] - 2026-09-05

Native runtime `0.6.0` (unchanged; no new engine download). Skill: `prepare-repo-context` 1.7.3.

### Fixed

- `impact-candidates` no longer lets its output-budget trim drop a changed
  entry that a returned candidate names as an anchor: the tail trim of
  `changed` now keeps every such entry, even when the anchors alone exceed
  the changed layer's own third of the budget. `changed` is also reordered
  ahead of any budget step - an entry anchoring a returned candidate first
  (most-anchored first), then the rest, non-test paths before test paths and
  a caller-anchor kind (`definition`/`entry-point`) before `module` - so a
  budget that has to trim spends on the least useful entries first instead of
  on whichever the engine happened to return last.

## [2.7.0] - 2026-09-05

Native runtime `0.6.0` (unchanged; no new engine download). Skill: `prepare-repo-context` 1.7.2.

### Changed

- `repository-overview` defaults `maximum_results` to 24 on both surfaces
  (CLI and MCP), not the 8 every other operation keeps: its ranked file layer
  is a repository-wide sample rather than a search result, so an unbudgeted
  request gets a fuller feel for an unfamiliar repository at the default
  8000-character budget. The default is resolved from the same per-operation
  table `default_output_characters` already used, so the two surfaces cannot
  drift apart on what an unbudgeted request means.
- `repository-overview` findings now carry exactly the twelve base finding
  keys. The four relationship keys (`relation`, `edge_evidence`,
  `reference_line`, `reference_count`) schema 4 reuses from the schema-2 wire
  shape - always null/zero on this operation, since it names no edge - are
  dropped from the broker's summary, matching `repository-map` and
  `search-*`. The wire object and the models are unchanged; only the
  broker's compacted output changes.
- `impact-candidates` gives its change set a share of the output budget
  instead of spending it on candidates first, cheapest loss first: an answer
  over budget takes the compact `path`, `qualified_name` form for every
  `changed` entry right away, not only once the list itself is over its
  third of the budget - losing entry detail is cheaper than losing a
  candidate, and the identity is still available from `changed-symbols` and
  from every candidate's `anchors`. Only a compact list still over its third
  of the budget drops entries from the tail, and the candidates are only
  then dropped from the tail until the answer fits, so a 46-symbol change
  set at the default 8000 characters keeps every changed entry (compact)
  as well as its candidates instead of reporting `changed: []`. The
  candidates pay for what the changed layer still cannot fit in its share:
  a budget-bound answer can now carry one candidate fewer than 2.6.0
  returned, and its tail is the weakest evidence.

### Added

- `impact-candidates` results carry `changed_trimmed_count`, next to
  `changed_omitted_count`: how many changed symbols the output budget dropped
  from the tail of `changed`. The length of the list plus that count is
  always `changed_count`, so a short change set is a counted one rather than
  a silent one - the 2.6.0 field report saw `changed_count 46` with ten
  entries and nothing to explain the difference.

### Fixed

- Generation retention now converges: the broker prunes superseded index
  generations after a successful `build`, `activate`, or query, not only
  after a refresh, so a repository reaches one referenced generation once
  each superseded generation has aged past the 60-second grace period even
  when nothing ever triggers another refresh - only when the current
  generation is itself older than the grace period, so a generation another
  session just published is never pruned out from under it. The result's
  `refresh` block reports `pruned_generation_count`.

## [2.6.0] - 2026-09-05

Native runtime `0.6.0` (no index format change, so existing indexes built by
runtime 0.3.0 or later are kept; a generation written by an older runtime is
rebuilt on the next `build`. The next `inspect` reports
`install-native-engine` until `activate` downloads the new runtime). Bundled
skills: `branch-handoff` 1.2.1, `prepare-repo-context` 1.7.1,
`work-recovery` 1.1.0.

### Changed

- The `repository-overview` group table is sized by the output budget instead
  of by fixed row counts. The engine no longer stops splitting once the table
  has a certain number of groups and no longer cuts the table to a maximum row
  count: it applies the 40 % share rule, the single-directory descent, and the
  depth cap of four segments, and returns every group it arrives at, ordered by
  definition count, then file count, then prefix, with no `*` row of its own.
  The broker then divides the caller's `maximum_output_characters` between the
  two layers of the answer, at most half of it to each: a table wider than its
  half folds its tail into `*` - counts summed, languages merged,
  `other_group_count` counting every folded directory - until it fits that
  half, down to a single directory row; a table already inside its half is
  handed on whole; and the ranked file layer keeps everything the table did not
  spend, dropping from its tail only for what is left. Only a one-row table
  with no findings at all reports `output-budget-exceeded`. So a wider budget
  buys a wider table *and* more files, and `--path-prefix D/` is what goes
  deeper into one subtree instead of wider over all of them. This replaces the
  fixed twelve-group split stop and sixteen-row table of 2.5.0, whose counts
  were design guesses that left a dominant directory unsplit on a large
  repository. The broker also asks the engine for the widest answer the wire
  allows and does all of the fitting itself, so the engine's own rendered-line
  fit can no longer drop findings a caller's budget could still have carried.
  The wire shape is unchanged: a schema-4 result still carries the same nine
  keys per row, `*` still sums directories and names no representative file,
  and the row cap is now the indexed-path bound rather than seventeen.
  `repository-map` and every other operation are untouched.
- `repository-overview` counts and ranks framework entry points. A file counts
  toward `entry_point_count` when it carries an entry-point record **or** when
  its base name is one an ecosystem starts at, and the well-known names now
  include the Next.js App Router conventional file names (`page`, `layout`,
  `route`, `loading`, `error`, `not-found`, `template`, `default` in `.tsx`,
  `.ts`, `.jsx` and `.js`), matched by base name wherever the file sits, and
  its four single-file conventions (`middleware.ts`, `instrumentation.ts`,
  `sitemap.ts`, `robots.ts`). The
  column therefore counts entry-point *files*, one per file, rather than
  entry-point records. The file layer also offers every group's entry points
  before any group offers a second-tier file, so an `app/` directory of route
  segments leads the answer even when larger directories head the table.
- `impact-candidates` defaults to 8000 output characters on both surfaces
  instead of 4000. It answers in two layers - the change set in `changed` and
  the candidates in `findings` - and 4000 characters were spent on the change
  set before the first candidate, which is what made `changed` shrink to
  identities and then empty on an ordinary branch diff.

### Fixed

- `build` and `activate` no longer fail silently over an index an older
  runtime wrote. Such a generation is removed before the build, under the
  state-write authorization the command already required, and the summary
  carries the warning `incompatible-generation` with the runtime that wrote it
  in `engine.replaced_generation_version`. When the engine refuses a build
  without a warning of its own and the state explains the refusal, the answer
  is `next_safe_action: rebuild-index` with that warning instead of a bare
  "native context build did not become ready"; a refusal nothing explains now
  names the engine's own status.
- `inspect` reports `state.incompatible_generation_count` next to
  `stale_runtime_count`, and `gc` proposes those records under a new category
  `incompatible-generation`, removed only with `--confirm-state-write` like
  every other category and named by the repository record they belong to.
  Before this, a state root holding 0.1.x generations reported them through
  nothing: they were bound and their repositories still existed, so neither
  `orphan_count` nor the stale runtime covered them. Only a manifest that
  positively names a format version other than the current one counts, so a
  corrupt or half-written generation is still left to the engine's own
  refusal rather than deleted on a guess.

## [2.5.0] - 2026-09-05

Native runtime `0.5.0` (no index format change, so existing indexes are kept;
the next `inspect` reports `install-native-engine` until `activate` downloads
the new runtime). Bundled skills: `branch-handoff` 1.2.1,
`prepare-repo-context` 1.7.0, `work-recovery` 1.1.0.

### Added

- The native engine answers a read-only `repository-overview` operation over
  the additive wire schema `4`: how a repository is organized, in one call.
  `groups` is a bounded directory table, one row per prefix with its file,
  definition, entry-point, document, and configuration counts, its languages,
  and the identity of a representative file; `overview` names the described
  root, the counted files, and how many directories the row `*` folds
  together. Prefixes stay relative to the repository root. A group holding
  more than 40 % of the counted files is replaced by its children while it
  stays within four segments of depth and the table has fewer than twelve
  groups, and a group whose only child is one directory is replaced by that
  directory so the descent reaches the branch point below it; the rows are
  ordered by definition count, then file count, then prefix, and the surplus
  beyond sixteen is folded into `*`. Nothing is resolved and no file is
  reopened.
- The same answer carries a ranked file layer in `findings`: each group's
  files are ordered by entry points and well-known entry file names
  (`main.go` under a `cmd/` directory, `main.py`, `__main__.py`, `app.py`,
  `manage.py`, `cli.py`, `index.js`, `index.ts`, `index.tsx`, `page.tsx`,
  `layout.tsx`, `server.ts`, `main.rs`, `lib.rs`), then the files that define
  something, then documents with a README first, then configuration, then
  whatever is left, and one file is taken from each group per round so a
  dominant directory cannot fill the answer alone. The name list is a ranking
  hint only: a file it names keeps the record kinds it really has.
  `omitted_count` is the counted files the answer does not list.
- `prepare-repo-context` 1.7.0 exposes it as
  `query --operation repository-overview`, which needs neither a query nor a
  result identity and accepts only `--path-prefix` and `--language`;
  `--query`, `--result-id`, `--direction`, `--base`, `--symbol-kind`, and
  `--source-type` are rejected. For this operation `--path-prefix` names whole
  directory segments, and naming several prefixes describes the first in
  sorted order with the warning `overview-root-first-prefix`; a prefix no
  indexed path lies under answers with an empty table and the warning
  `overview-root-not-a-directory`. The group table is never trimmed, so the
  output budget takes the file layer from the tail and
  `output-budget-exceeded` is reported only when even an empty file layer does
  not fit; `output_characters` is the canonical length of the answer, as it
  already is for `impact-candidates`. Without `--maximum-output-characters`
  this operation answers at 8000 characters rather than the 4000 the other
  operations use, because the group table alone leaves little of the smaller
  budget for the file layer.
- The `repo-context` MCP server, now 1.3.0, gains a tenth tool,
  `repository_overview` (`path_prefixes`, `languages`, `allow_inferred`, and
  the two budgets), read-only like the other query tools. Its
  `maximum_output_characters` defaults to 8000 rather than 4000, the same
  per-operation default the script resolves.

### Changed

- The callers dogfood now measures a fixed-range clone at the pinned commit
  instead of the working tree, so its precision and recall figures no longer
  move with uncommitted work; the truth set is the one that matches that
  commit again.
- `changed-symbols` and `impact-candidates` reuse the repository root the
  snapshot already resolved, so one change query runs
  `git rev-parse --show-toplevel` once instead of three times.

## [2.4.0] - 2026-09-04

Native runtime `0.4.0` (no index format change, so existing indexes are kept;
the next `inspect` reports `install-native-engine` until `activate` downloads
the new runtime). Bundled skills: `branch-handoff` 1.2.1,
`prepare-repo-context` 1.6.0, `work-recovery` 1.1.0.

### Added

- The native engine answers a read-only `changed-symbols` operation: given the
  changed line ranges of a Git difference, it returns the `definition`,
  `entry-point`, and `module` records whose line span meets a changed hunk of
  the same path. Nothing is resolved and no file is reopened; the change set
  only selects records the index already carries. A changed path the index has
  no record for is reported once as the warning `changed-path-not-indexed`.
- The broker derives those ranges from one `git diff -U0` between a resolved
  base and the working tree, so committed, staged, and unstaged changes are
  covered together and untracked files count as changed as a whole. The base is
  the one work recovery resolves: an explicit request, then the branch's
  upstream main, then `origin/HEAD`, then a local `main`/`master`; the result
  carries it as `base`, and an unresolved base is reported as `base-unresolved`
  with uncommitted changes only. The change set is bounded to 200 paths and 64
  hunks per path, and the warnings `changed-paths-limit`,
  `changed-ranges-collapsed`, `changed-path-unsafe`, `changed-diff-unavailable`,
  `changed-selector-collapsed`, and `changed-selector-limit` each say what a
  bound cost the answer.
- The broker composes a second operation, `impact-candidates`, from one
  `changed-symbols` answer plus one `related-symbols` call per changed symbol
  and direction (`callers` for a changed definition or entry point, `importers`
  for a changed module or definition). Each candidate is one related record
  that is not itself changed, and carries in `anchors` every changed symbol it
  depends on with the edge that reached it, strongest evidence first; the
  candidate's own `relation`, `edge_evidence`, `reference_line`, and
  `reference_count` come from that first anchor. Candidates rank verified
  before inferred, then by number of anchors, then by path and start line, so a
  truncated list is the strongest prefix. `inferred` edges are returned only
  with `--allow-inferred`/`allow_inferred` and are never upgraded. Over the
  output budget the result sheds the `changed` list before candidates
  (`changed-list-trimmed`, then `truncated`, then `output-budget-exceeded`).
- `prepare-repo-context` 1.6.0 exposes both as
  `query --operation changed-symbols|impact-candidates [--base <ref>]`, which
  need no result identity and reject `--query`, `--result-id`, and
  `--direction`; `--base` is rejected by every other operation. The
  `repo-context` MCP server, now 1.2.0, gains the eighth and ninth tools,
  `changed_symbols` (with the usual filters) and `impact_candidates` (`base`,
  `allow_inferred`, and the two budgets), both read-only. One
  `impact_candidates` call follows at most 64 changed symbols of the change
  set, fewer under a tight output budget, reports the rest as omissions with
  `truncated`, and returns the ranked one-hop candidates of the symbols it
  followed; the cost grows with the size of the change set.
- `work-recovery` 1.1.0 may, only on an explicit request for indexed context,
  add one readiness check and exactly one `impact-candidates` query against an
  index that is already ready, and append a "Symbols touched and one-hop
  dependents" section to the recovery report. It never builds, installs, or
  downloads an index, never repeats the query, and never delays or replaces the
  native report; the recovery collector itself is unchanged.

### Changed

- Wire schema gains an additive version `3`, which carries the request key
  `changed_ranges` and the `changed-symbols` operation and reuses the schema-2
  finding field set. Schema `1` and schema `2` requests and results are
  unchanged, and a schema-3 `changed-symbols` finding leaves `relation` and
  `edge_evidence` null with both reference counters `0`.
- The native engine version becomes `0.4.0`. The index format, the record
  tuple, and the manifest format are untouched, so no index rebuilds.

## [2.3.0] - 2026-09-04

Native runtime `0.3.0` (index format 4; existing indexes report `rebuild-index`
once and the next `inspect` reports `install-native-engine` until `activate`
downloads the new runtime). Bundled skills: `branch-handoff` 1.2.1,
`prepare-repo-context` 1.5.0, `work-recovery` 1.0.1.

### Added

- The native engine indexes call and import references as file-local
  `reference` records (one per enclosing definition, or per module for
  module-level calls) and adds a read-only `related-symbols` operation with
  four directions: `callers`, `callees`, `importers`, `imports`. Anchored on
  one or more identities from an earlier query, it resolves edges at query
  time and returns each related definition, module, or import record with
  four extra fields: `relation` (`call` or `import`), `edge_evidence`
  (`verified` or `inferred`), `reference_line`, and `reference_count`.
  `edge_evidence: inferred` is a name match, never proof, and stays hidden
  unless `allow_inferred`/`--allow-inferred` is set. `reference` records are
  never returned by `search-symbols`, `search-docs`, `repository-map`, or
  `source-snippets`; they are reachable only through `related-symbols`.
- `prepare-repo-context` 1.5.0 exposes `related-symbols` through
  `query --operation related-symbols --result-id <id> --direction
  callers|callees|importers|imports`, and the `repo-context` MCP server gains
  a seventh tool, `related_symbols`, with the same contract.

### Changed

- Wire schema gains an additive version `2`, used only for `related-symbols`
  requests and results; schema `1` requests and results are unchanged.
- The record tuple, store format (index format 4, manifest format `"3"`), and
  native engine version (`0.3.0`) all change to carry the two new reference
  fields. Existing indexes rebuild once on the next `inspect`.

## [2.2.0] - 2026-09-03

Native runtime `0.2.0` (every engine change below; `--serve` is required by the
MCP server). Existing installations report `install-native-engine` on the
next `inspect` and `activate` downloads the new runtime; existing indexes
rebuild once. Bundled skills: `branch-handoff` 1.2.1, `prepare-repo-context`
1.4.0, `work-recovery` 1.0.1.

### Added

- `prepare-repo-context` 1.1.0 reports user-local state usage in `inspect`
  and adds `remove` and `gc` commands that only delete with explicit
  state-write confirmation.
- Bound index state records its last use so `gc` can reclaim entries unused
  for a configurable number of days.
- `prepare-repo-context` 1.2.0 ships `references/query-routing.md` and
  `references/result-contract.md`, one-line previews on definition and
  heading findings, a 4,000-character default output budget, and
  case-insensitive filter values with validation.
- A dogfood recall benchmark (`tools/taf-context-native/testdata/dogfood`)
  gates symbol and heading recall on this repository.
- `prepare-repo-context` 1.3.0 refreshes a bound index incrementally inside
  `query` and `inspect` (binding schema 2 remembers the head and dirty paths;
  the engine's `update` operation is driven with the Level 0 change document),
  prunes superseded generations older than 60 seconds after a refresh,
  tolerates parse failures in updated files like `build`, and reports a
  `refresh` block.
- The plugin bundles the `repo-context` MCP stdio server (`.claude-plugin/mcp.json`
  for Claude Code, `.codex-plugin/mcp.json` for Codex): `inspect`, `build` (with
  an explicit `confirm_state_write` argument), and the four read-only query
  operations as tools, served by one session-scoped native engine process
  that starts on the first call, restarts after a crash or time-out, and
  closes with the session. `prepare-repo-context` 1.4.0 prefers those tools
  when they are available; `activate`, `gc`, and `remove` remain script
  commands.

### Changed

- The native engine's incremental `update` now peeks at the current
  generation and publishes through a compare-and-swap on the previous
  generation, making a one-file refresh cheaper than a full build and safe
  against concurrent refreshes; loading a stored index precomputes the
  canonical sort keys and reuses its key buffers, cutting index
  materialization on very large indexes by about a quarter. Results are
  unchanged (a frozen synthetic oracle guards them).
- The context broker keeps one provider, the native engine. Third-party
  provider discovery, registry, consent ledger, and adapter execution are
  removed; `prepare` output no longer contains a `providers` field.
- Tests and benchmarks pin `TAF_STATE_HOME` to a temporary directory.
- The native engine keeps literal definitions `verified` in files that use
  dynamic lookups, searches through postings and the token dictionary with a
  budget that scales with the index, treats filters as record predicates,
  ranks definitions above imports, and sets `truncated` whenever a result is
  known to be incomplete. Existing indexes rebuild once on the next `inspect`.
  Substring and fuzzy matching apply only to words with no exact or prefix
  match; configuration keys rank below code definitions.
- JavaScript and TypeScript module-scope `const`, `let`, and `var`
  declarations are indexed as definitions, and repository maps represent a
  file by its definitions before its imports.
- `prepare query` makes one native call per query, the engine verifies the
  payload digest and generation identity instead of running the raw
  structural validator on `status` and query paths (the validator still runs
  at build time and in `metrics`), the broker collects the Git snapshot with
  concurrent read-only commands, and installer modules load only for
  `activate`. End-to-end query latency on this repository drops from about
  1.0 s to about 0.3 s. A `source-snippets` request whose result identities
  cannot be verified is refused with a message that says so instead of a
  stale-context error.

## [2.1.2] - 2026-08-31

### Fixed

- Bounded parser limits are reported as coverage warnings instead of parse
  failures.
- Exact-binding partial indexes are persisted, activated, inspected, and
  queried without an endless rebuild recommendation.
- Preparation summaries now expose native warnings and compact coverage
  counters for partial indexes.

## [2.1.1] - 2026-08-31

### Fixed

- Native context activation now loads reliably from the standalone skill on
  Windows and preserves durable, owner-protected state across platforms.
- Release smoke artifacts remain outside the repository snapshot so a clean
  checkout stays exact during the user-facing build and query flow.
- Published checksum sidecars use portable LF endings, including when packaged
  on Windows.

## [2.1.0] - 2026-08-30

### Added

- `prepare-repo-context` 1.0.0 with read-only provider/index inspection,
  bounded native estimates, explicit write/network gates, and reusable
  repository/worktree-bound state and read-only evidence queries.
- Checksum-verified native runtime acquisition for supported macOS, Linux, and
  Windows targets, plus tagged-release asset automation.

### Changed

- The unified TAF plugin now exposes three independently loaded skills.
- Repository context remains outside the model context; only compact lifecycle
  summaries and bounded query results cross the agent boundary.

## [2.0.0] - 2026-08-27

### Added

- One `taf` plugin for Claude Code and Codex containing `branch-handoff` 1.2.1
  and `work-recovery` 1.0.1.
- A Codex marketplace manifest that installs the repository root without
  copying or aggregating the canonical `skills/` sources.
- Clean migration guidance from the two legacy Claude plugin installations.

### Changed

- Claude invocations are now `/taf:branch-handoff` and
  `/taf:work-recovery` under one product entry.
- GitHub releases now version The Agentic Fieldbook product and list bundled
  skill versions instead of presenting one skill as the complete release.
- Installation makes the collection discoverable while full skill instructions
  continue to load only when relevant.

### Compatibility

- This is a breaking Claude packaging and namespace change. The legacy
  per-skill plugin entries are removed rather than retained as aliases.
- Skill behavior, runtime requirements, bounded-context guarantees, consent
  rules, and output contracts are unchanged.

## [1.3.1] - 2026-08-27

### Changed

- Claude marketplace cards now explain each skill's outcome and show its
  namespaced invocation command.
- Plugin display names and documentation links now lead users from compact UI
  metadata to the matching English install and usage guidance.
- `branch-handoff` 1.2.1 and `work-recovery` 1.0.1 contain metadata-only
  discoverability improvements; their runtime behavior is unchanged.

## [1.3.0] - 2026-08-26

### Added

- `work-recovery` 1.0.0 with bounded current-worktree evidence, strict work
  states, exact omission accounting, and an optional same-dossier continuation
  prompt.
- A standalone vendored Python standard-library runtime with deterministic hash
  drift checks and a zero-artifact collector.

### Changed

- The marketplace now publishes `branch-handoff` and `work-recovery` as
  independently installable skills.

### Security

- Recovery is read-only by default: it performs no validation, network/provider
  action, index lifecycle, repository mutation, or unapproved untracked and
  other-worktree content read.

## [1.2.0] - 2026-08-24

### Added

- One structured context-discovery choice when a request omits Jira and GitHub targets.
- Exact Jira target entry and bounded current-branch PR discovery before target-specific read consent.

### Changed

- Explicit platform targets and explicit local-only requests skip context discovery.
- Jira and GitHub may be selected together while local-only remains mutually exclusive.

### Security

- Context discovery performs no adapter or network access, and platform reads still require session-scoped consent.

## [1.1.0] - 2026-08-24

### Added

- Preflight for explicitly supplied Jira issue and GitHub PR targets before diff collection.
- Structured consent choices when supported, with equivalent numbered fallback.
- One session-scoped Jira permission for connection verification and bounded issue fields.
- Deterministic agent-specific re-install/update commands and Claude marketplace auto-update guidance.

### Changed

- Authorized platform intent and acceptance criteria now participate in the first and only handoff synthesis.
- Platform decline or read failure now continues to a complete diff-only report.

### Security

- Platform content remains untrusted evidence, and every external comment still requires exact target and exact draft confirmation.
