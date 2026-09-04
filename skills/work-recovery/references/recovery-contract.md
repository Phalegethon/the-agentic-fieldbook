# Recovery Report Contract

Render a concise report in the user's language from the single retained dossier.

1. **Scope:** repository/worktree identity, branch, HEAD, resolved base, evidence budget, and collection boundary.
2. **Where work stopped:** copy exactly one dossier state: `active-dirty`, `active-committed`, `integrated`, `superseded-stale`, `diverged`, `clean-unresolved`, or `unknown`. Never replace it with a synonym such as “complete” or “synchronized.” Copy each claim's `observed`, `reported`, `inferred`, `conflicted`, or `unknown` class; never downgrade observed Git facts to reported. Never invent a confidence percentage.
3. **Changed work:** summarize included tracked staged and unstaged evidence separately. Untracked items are metadata-only unless their claim says `content-authorized`.
4. **Conflicts and unknowns:** make stale notes, stale validation, unresolved intent/base, and omissions visible. A completion note never overrides dirty Git evidence.
5. **Validation state:** report only supplied results marked `validation-current`; otherwise say validation was not run or is stale.
6. **Next action:** use the matrix below for exactly one primary action. Add no more than two conditional follow-ups, each tied to an explicit uncertainty and its required authorization.
7. **Coverage:** copy changed/examined counts and exact omission counts from the dossier. Scope exclusions such as source, tests, providers, and other worktrees are boundaries, not omissions; do not convert them into new omitted items.
8. **Symbols touched and one-hop dependents:** only when the optional context step ran (`references/context-actions.md`). Name the base the query compared against, then the changed symbols as `path:start_line-end_line` with their qualified names, then the candidates that depend on them, each with the changed symbols listed in its `anchors` and the edge's evidence. Verified edges only unless the user asked for `inferred` ones, which are name matches and must be called that. A candidate is a symbol that references changed work, never a defect and never a break; the section adds context and never revises the work state, the evidence classes, or the primary next action above it. Copy the answer's own `truncated`, `omitted_count`, and change-set warnings instead of restating the dossier's coverage counts. Omit the section entirely when the step did not run or the index was not ready, in which case one sentence in section 4 says so.
9. **Reminder:** the final line always says, in the user's language: “A compact continuation prompt is available on request from this same dossier; it was not generated now.”

## Primary next-action matrix

| State | Primary action |
|---|---|
| `active-dirty` | Review and reconcile only the included tracked staged/unstaged evidence. |
| `active-committed` | Compare retained committed evidence with the stated objective; when either is absent, ask the user to confirm the unfinished objective. |
| `integrated` | Confirm whether any unfinished intent remains; do not make cleanup the primary action. |
| `superseded-stale` | Confirm retention intent; cleanup is only a conditional option after explicit authorization. |
| `diverged` with indistinguishable candidates | Ask the user to choose from the metadata-only candidates; do not open candidate content. |
| `diverged` current workstream | Resolve the branch relationship using retained evidence before implementation. |
| `clean-unresolved` or `unknown` | Establish the intended base and objective before changing code. |

Validation, untracked-content inspection, other-worktree content inspection, cleanup, and repository mutation are never direct next actions under existing recovery authorization. A conditional follow-up may say what becomes useful **if the user separately authorizes it**; it must not instruct the user or agent to perform it now.

Do not imply that tests, source-wide inspection, network access, provider execution, or repository mutation occurred. The one exception is the optional context step of section 8: when it ran, say plainly that the report also used one bounded index query, and never imply more than the two calls it is allowed. Do not expose machine-local paths, credentials, redacted content, or unsupported intent.
