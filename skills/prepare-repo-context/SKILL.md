---
name: prepare-repo-context
description: Use when a developer wants to inspect, prepare, refresh, query, or understand bounded repository context without loading or indexing the full codebase into the model context.
license: MIT
metadata:
  author: The Agentic Fieldbook
  version: "1.1.0"
---

# Prepare Repo Context

Prepare reusable local repository context with deterministic tooling while keeping the model context small.

Let `<skill-dir>` be this skill's directory. Resolve the repository, then resolve `<python>` to `python3` or a Python 3 `python`. If Git or Python 3 is unavailable, report the missing prerequisite; do not install anything.

1. Run `<python> <skill-dir>/scripts/prepare_repo_context.py inspect --repo <repo>` exactly once. This read-only inspection is authorized by invoking the skill. Do not scan source files yourself.
2. Summarize only the returned engine availability, freshness, eligible/excluded path counts, state usage, required authorizations, and `next_safe_action`. Do not put an index, repository-wide file list, or source content into the model context.
3. If `next_safe_action` is `build-index`, explain the estimated scope and ask for state-write authorization. After approval, run `<python> <skill-dir>/scripts/prepare_repo_context.py build --repo <repo> --confirm-state-write` exactly once and report its compact result.
4. If it is `install-native-engine`, explain the estimated scope and ask once for network plus state-write authorization. After approval, run `<python> <skill-dir>/scripts/prepare_repo_context.py activate --repo <repo> --confirm-network --confirm-state-write` exactly once. This downloads the matching release runtime, verifies its SHA-256 checksum, installs it in user-local TAF state, and prepares the index. Report only the compact result.
5. Once `next_safe_action` is `use-index`, stop if the user asked only to prepare context. If they asked a repository question, run exactly the smallest matching read-only query: `query --operation repository-map`, `query --operation search-symbols --query <term>`, or `query --operation search-docs --query <term>`. Fetch source only when needed with `query --operation source-snippets --result-id <identity>` using identities returned by an earlier query. Keep the default output budget unless the user explicitly needs more evidence.

Current repository identity, worktree identity, commit, dirty fingerprint, and native freshness must agree before context is described as ready. When `state.orphan_count` is nonzero or `state.root_bytes` is large, you may mention that `<python> <skill-dir>/scripts/prepare_repo_context.py gc` (or `remove --repo <repo>` for this repository) reports reclaimable state; both delete only with `--confirm-state-write`, which requires the user's explicit state-write authorization. An exact-binding `partial` context with `next_safe_action: use-index` is usable bounded coverage; report its warnings without rebuilding it. Report query findings with their paths and line ranges; do not infer beyond their evidence class. A failed preparation never blocks ordinary Git-based skills.
