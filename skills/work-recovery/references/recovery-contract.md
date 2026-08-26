# Recovery Report Contract

Render a concise report in the user's language from the single retained dossier.

1. **Scope:** repository/worktree identity, branch, HEAD, resolved base, evidence budget, and collection boundary.
2. **Where work stopped:** copy exactly one dossier state: `active-dirty`, `active-committed`, `integrated`, `superseded-stale`, `diverged`, `clean-unresolved`, or `unknown`. Never replace it with a synonym such as “complete” or “synchronized.” Copy each claim's `observed`, `reported`, `inferred`, `conflicted`, or `unknown` class; never downgrade observed Git facts to reported. Never invent a confidence percentage.
3. **Changed work:** summarize included tracked staged and unstaged evidence separately. Untracked items are metadata-only unless their claim says `content-authorized`.
4. **Conflicts and unknowns:** make stale notes, stale validation, unresolved intent/base, and omissions visible. A completion note never overrides dirty Git evidence.
5. **Validation state:** report only supplied results marked `validation-current`; otherwise say validation was not run or is stale.
6. **Next action:** use the matrix below for exactly one primary action. Add no more than two conditional follow-ups, each tied to an explicit uncertainty and its required authorization.
7. **Coverage:** copy changed/examined counts and exact omission counts from the dossier. Scope exclusions such as source, tests, providers, and other worktrees are boundaries, not omissions; do not convert them into new omitted items.
8. **Reminder:** the final line always says, in the user's language: “A compact continuation prompt is available on request from this same dossier; it was not generated now.”

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

Do not imply that tests, source-wide inspection, network access, provider execution, or repository mutation occurred. Do not expose machine-local paths, credentials, redacted content, or unsupported intent.
