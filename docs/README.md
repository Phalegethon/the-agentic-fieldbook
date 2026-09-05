# TAF documentation

The [README](../README.md) is the short tour. These pages hold the full
behaviour of each skill and the bundled tooling.

| Page | What it covers |
|---|---|
| [work-recovery](work-recovery.md) | Where interrupted work stopped, the single best next step, the continuation prompt, and the optional indexed-context step. |
| [branch-handoff](branch-handoff.md) | Merge-base diff evidence, the QA and Developer/PR handoff sections, and the session-scoped Jira and GitHub consent flow. |
| [prepare-repo-context](prepare-repo-context.md) | Inspecting and preparing the native index, every query operation, filters, budgets, and state reclamation. |
| [Commit-time impact warning](commit-time-impact-hook.md) | The advisory `pre-commit` launcher: what it prints, when it stays silent, how to install, chain, and remove it. |
| [The repo-context MCP server](repo-context-mcp-server.md) | The bundled stdio server, its tools, consent rules, and manual registration. |

## About the demo recordings

The animations in the README were rendered from real runs recorded on a small
sample project (`orbit-store`: a Python checkout service with a TypeScript
front end) using a scratch `TAF_STATE_HOME`. The commit-time warning is the
hook's own stderr from a real `git commit`. The chat recordings condense real
Claude Code sessions that invoked the installed TAF skills: the tool calls,
collector counts, SHAs, paths, line numbers, and the agent's sentences are
copied from those sessions, shortened to fit the frame.
