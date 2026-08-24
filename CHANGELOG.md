# Changelog

All notable changes to independently released TAF skills are recorded here.

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
