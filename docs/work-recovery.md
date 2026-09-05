# work-recovery

Recover interrupted work and the single best next step from bounded, read-only
Git evidence, optionally naming the symbols the work touched.

Skill version: see the `version` field in
[`skills/work-recovery/SKILL.md`](../skills/work-recovery/SKILL.md).

## Ask it

From the interrupted Git worktree:

```text
Use /taf:work-recovery to tell me where this work stopped and the single best next step. Do not run tests or change repository state.
```

The normal report ends with a reminder rather than a generated handoff. To move
the same evidence into another session, ask separately:

```text
Now turn that recovery dossier into a compact continuation prompt for another session.
```

To add the symbols the work touched and their one-hop dependents (only when a
`prepare-repo-context` index is already ready for the repository):

```text
Use /taf:work-recovery with indexed context: where did this stop, and which symbols did the work touch?
```

## What the report contains

The skill runs its collector exactly once and renders one report from the
returned dossier, following `references/recovery-contract.md`:

1. **Scope**: repository and worktree identity, branch, HEAD, resolved base,
   evidence budget, and collection boundary.
2. **Where work stopped**: exactly one dossier state, copied verbatim:
   `active-dirty`, `active-committed`, `integrated`, `superseded-stale`,
   `diverged`, `clean-unresolved`, or `unknown`. Every claim carries its
   evidence class (`observed`, `reported`, `inferred`, `conflicted`,
   `unknown`). Observed Git facts are never downgraded, and no confidence
   percentage is invented.
3. **Changed work**: tracked staged and unstaged evidence, summarised
   separately. Untracked items are metadata only unless their content was
   explicitly authorised.
4. **Conflicts and unknowns**: stale notes, stale validation, unresolved intent
   or base, and omissions. A completion note never overrides dirty Git
   evidence.
5. **Validation state**: only supplied results marked current; otherwise the
   report says validation was not run or is stale.
6. **Next action**: exactly one primary action from the state matrix below,
   plus at most two conditional follow-ups tied to an explicit uncertainty.
7. **Coverage**: changed and examined counts and exact omission counts.
8. **Symbols touched and one-hop dependents**: only when the optional indexed
   context step ran.
9. **Reminder**: "A compact continuation prompt is available on request from
   this same dossier; it was not generated now."

| State | Primary action |
|---|---|
| `active-dirty` | Review and reconcile only the included tracked staged/unstaged evidence. |
| `active-committed` | Compare retained committed evidence with the stated objective; ask to confirm the unfinished objective when either is absent. |
| `integrated` | Confirm whether any unfinished intent remains; cleanup is not the primary action. |
| `superseded-stale` | Confirm retention intent; cleanup only after explicit authorisation. |
| `diverged` | Choose among metadata-only candidates, or resolve the branch relationship before implementation. |
| `clean-unresolved` or `unknown` | Establish the intended base and objective before changing code. |

## Boundaries

- Reads bounded current-worktree Git metadata and tracked staged/unstaged diff
  evidence; other worktrees contribute metadata only.
- Untracked content requires exact path authorisation and remains protected by
  no-follow, file-type, size, binary, generated, and credential gates.
- Does not run tests, builds, lint, project validation, or repository mutation.
- Does not build or update an index, query a provider, or access the network.
- Uses an existing index only after separate explicit authorisation; native
  recovery remains complete when none is available.
- On an explicit request for indexed context, adds at most a readiness check
  and one read-only `prepare-repo-context` change-impact query against an
  index that is already ready, and reports the symbols the work touched with
  their one-hop dependents. It never builds, installs, or downloads an index,
  and it never delays or replaces the native report.
- Writes no recovery artifact and reports exact evidence omissions within the
  selected context budget (2000 characters by default; `--max-output-chars`
  accepts 2000, 4000, 8000, or 12000).

## Runtime requirements

Git and Python 3. The bundled collector uses only the Python standard
library.
