# Changelog

All notable TAF product releases and their bundled skill versions are recorded
here. Skills keep independent behavior versions inside their `SKILL.md` files.

## [Unreleased]

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

### Changed

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
