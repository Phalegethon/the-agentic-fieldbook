# Recovery Report Contract

Render a concise report in the user's language from the single retained dossier.

1. **Scope:** repository/worktree identity, branch, HEAD, resolved base, evidence budget, and collection boundary.
2. **Where work stopped:** the classified state and a short evidence-supported account. Label material as `observed`, `reported`, `inferred`, `conflicted`, or `unknown`; never invent a confidence percentage.
3. **Changed work:** summarize included tracked staged and unstaged evidence separately. Untracked items are metadata-only unless their claim says `content-authorized`.
4. **Conflicts and unknowns:** make stale notes, stale validation, unresolved intent/base, and omissions visible. A completion note never overrides dirty Git evidence.
5. **Validation state:** report only supplied results marked `validation-current`; otherwise say validation was not run or is stale.
6. **Next action:** give exactly one primary action grounded in the evidence. Add no more than two conditional follow-ups, each tied to an explicit uncertainty.
7. **Coverage:** report changed/examined counts and exact omissions.
8. **Reminder:** in the user's language, offer to turn this same dossier into a compact continuation prompt on request.

Do not imply that tests, source-wide inspection, network access, provider execution, or repository mutation occurred. Do not expose machine-local paths, credentials, redacted content, or unsupported intent.
