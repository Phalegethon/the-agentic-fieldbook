---
name: work-recovery
description: Use when a developer asks where interrupted work stopped, what remains, what to do next, or needs a reliable recovery after changing sessions, branches, worktrees, or long contexts.
license: MIT
metadata:
  author: The Agentic Fieldbook
  version: "1.1.0"
---

# Work Recovery

Recover one current Git workstream from bounded evidence. Be an evidence-backed, fast assistant—not a predictor.

Let `<skill-dir>` be this skill's directory. Resolve the repository (default: current repository) and only inputs the user explicitly supplied. Invocation authorizes bounded current-worktree Git metadata and tracked staged/unstaged diff hunks. It does not authorize untracked content, other-worktree content, unchanged source, broad history, providers, network access, indexing, tests, builds, lint, or mutation. An explicit request for indexed context adds exactly one read-only context query against an index that is already ready (step 4); it never authorizes building, installing, or downloading one.

1. Check Git and resolve `<python>` to `python3` or a Python 3 `python`. If missing, read `references/tool-setup.md`; never install without approval.
2. Run `<python> <skill-dir>/scripts/collect_recovery.py --repo <repo>` exactly once. Pass `--base`, `--max-output-chars`, `--include-untracked`, `--note-file`, or `--test-results-file` only for exact values the user supplied. Do not inspect repository source before or after the collector.
3. Stop on nonzero exit or invalid JSON. Retain the returned dossier for this conversation. Read `references/recovery-contract.md` and synthesize one report in the user's language. Copy the dossier's exact work-state enum and evidence classes; do not rename or downgrade them. Current Git facts outrank reported notes; preserve conflicts and unknowns. Use the contract's state-based next-action matrix.
4. Only when the user explicitly asked for indexed context, or supplied such evidence, read `references/context-actions.md` and take the single bounded step it describes: check readiness once, and only if it reports `next_safe_action: use-index`, run exactly one read-only `impact-candidates` query anchored on the dossier's resolved base, then append the contract's “Symbols touched and one-hop dependents” section before the reminder line. On any other `next_safe_action`, say in one sentence that indexed context is not ready and stop. Never build, activate, install, or download an index, never run `gc` or `remove`, never repeat the query, and never let this step delay or replace the native report.
5. End every report with the contract's localized continuation-reminder line. “Do not generate a continuation prompt” still requires this one-line reminder: the reminder is not the prompt.

Do not run project validation or change repository state. Do not generate the continuation prompt in the recovery report. If the user later asks for it, read `references/continuation-contract.md` and render from the retained dossier without collecting again. Read `references/context-actions.md` only when the user explicitly asks about indexed/provider context or supplies such evidence.
