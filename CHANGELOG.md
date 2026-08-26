# Changelog

All notable changes to independently released TAF skills are recorded here.

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
