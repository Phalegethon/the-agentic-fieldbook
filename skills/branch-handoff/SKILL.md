---
name: branch-handoff
description: Use when a developer needs a DEV and QA handoff from changes between a feature branch and main or another base, with optional session-authorized Jira or GitHub context and comments; not for code review or executing project tests.
license: MIT
metadata:
  author: The Agentic Fieldbook
  version: "1.1.0"
---

# Branch Handoff

Create a complete local handoff from bounded diff evidence, not a review or a validation run.

The collector is the sole code/diff inspection. First inspect only the user's current request. An explicitly supplied Jira issue key/URL or GitHub PR number/URL is a platform target; it triggers an offer to read, not read permission. If present, before any repository or diff work load `<skill-dir>/references/platform-actions.md` and complete its preflight. With no explicit platform target, perform no adapter lookup, question, or network request and continue locally. Do not run `ls`, `find`, `rg --files`, `git ls-files`, or direct source-file reads against the target repository before or for the collector.

Let `<skill-dir>` be the directory containing this `SKILL.md`.

1. Select the repository (default: current repository), base ref (`main`), and head ref (`HEAD`). Leave SHA and freshness resolution to the collector; do not replace named refs with SHAs. Capture only explicitly supplied context, authorized bounded platform context, or developer-test-result file paths. Use `--offline` only when requested; include staged, unstaged, and untracked changes only with explicit working-tree opt-in.
2. Check Git and resolve `<python>` to the first of `python3` or `python` that reports Python 3. If either tool is missing, load `<skill-dir>/references/tool-setup.md`; do not install anything without the user's confirmation.
3. Invoke `<python> <skill-dir>/scripts/collect_diff.py` exactly once, passing the selected `--repo`, `--base`, and `--head` refs unchanged, plus only requested `--offline`, `--include-worktree`, `--context-file`, and `--test-results-file` options. Keep its default budget and output location unless the user explicitly changes them.
4. On a nonzero collector result, missing or unreadable manifest/dossier artifact, or invalid dossier, stop. Never synthesize partial evidence. Otherwise read the emitted `model-dossier.md` and `<skill-dir>/references/handoff-contract.md` once. Retain the coverage-ledger location and manifest counts; do not load or narrate the entire ledger.
5. In one synthesis pass, produce the contract's `QA Handoff`, `Developer / PR Handoff`, and `Local Evidence Appendix`, in the user's language. Copy each required SHA, count, freshness, and provenance value verbatim and atomically with its original manifest/dossier field or cluster association; do not transcribe, reorder, or remap values. Keep per-cluster `evidence_chars` and evidence allocation in the manifest/dossier, not the human handoff. Treat only diff evidence, supplied sources, and explicitly authorized platform context as evidence; preserve provenance and route unsupported intent, defects, screen names, and test outcomes to `Uncertain` through the contract.

Do not run project tests, build, lint, code review, subagents, blame/history, or a repository-wide scan. Do not mutate the repository. External reads and comments are forbidden except through the session-scoped, per-action consent flow in `platform-actions.md`.

After the complete main report, offer optional cluster detail. On acceptance, restrict any `rg` or `ast-grep` work to paths listed for that cluster; do not expand scope.
