# branch-handoff

Compare a branch with its base and prepare evidence-backed DEV and QA handoffs
without code review or rerunning project tests.

Skill version: see the `version` field in
[`skills/branch-handoff/SKILL.md`](../skills/branch-handoff/SKILL.md).

## Ask it

From a Git repository:

```text
Use /taf:branch-handoff to compare this branch with main and prepare the DEV and QA handoff.
```

Natural-language invocation also works when the host supports automatic skill
selection:

```text
Compare this branch with main and prepare the DEV and QA handoff.
```

When the request supplies neither a platform target nor `local only`, the
skill asks one combined structured question for optional Jira and GitHub
context before repository or diff analysis. Choose Jira, GitHub, both, or
local-only; the skill then requests only the selected exact targets and
permissions.

To include Jira intent and acceptance criteria, supply the exact issue:

```text
Use branch-handoff to compare the current branch with origin/main and prepare the DEV and QA handoff. jira FE-2669
```

To include the uncommitted working tree as well as the committed branch
difference, say so explicitly:

```text
Use /taf:branch-handoff against main, include the working tree, local only.
```

## What the report contains

The skill runs its collector exactly once, reads the emitted evidence dossier
and `references/handoff-contract.md`, and produces three sections in one
synthesis pass:

1. **QA Handoff**: scope (base and head SHAs), change summary, a priority table
   of test surfaces with checkpoints and a confidence level per row
   (`Verified`, `Inferred`, or `Uncertain`), regression focus, and known
   assumptions.
2. **Developer / PR Handoff**: a copy-ready description of the change, the
   files and clusters it touched, warnings, and provenance of any platform
   context.
3. **Local Evidence Appendix**: resolved SHAs, base freshness, cluster counts,
   the coverage ledger location, and omissions.

Every SHA, count, freshness, and provenance value is copied verbatim from the
collector's manifest. Unsupported intent, defects, screen names, and test
outcomes are routed to `Uncertain`; nothing is invented. Authorised Jira or
GitHub content proves what that platform reported, not that the behaviour is
implemented or validated, and is labelled with its provenance.

After the complete report, the skill offers optional cluster detail. On
acceptance, any further inspection is restricted to the paths of that cluster.

## Boundaries

- Uses a merge-base comparison and prefers a fresh remote base when available
  (`--offline` skips the fetch when requested).
- Reads committed changes only unless working-tree inclusion is requested.
- Does not run project tests, builds, lint, code review, blame, or broad
  history.
- Produces local QA, Developer/PR, and evidence sections.
- Keeps Jira and GitHub local by default. Every platform read or comment needs
  the session-scoped consent described in
  `references/platform-actions.md`; explicit local-only performs no adapter
  lookup or platform network request.
- Does not mutate the repository.

## Runtime requirements

Git and Python 3. There are no Python package dependencies.
