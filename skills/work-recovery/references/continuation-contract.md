# Continuation Prompt Contract

Generate this only after the user asks, using the retained recovery dossier. Do not invoke the collector, Git, filesystem, provider, or network again.

The prompt is portable and compact. Include:

- objective and repository/worktree identities;
- branch, exact HEAD, base, and classified work state;
- completed or observed work, with staged and unstaged evidence distinguished;
- unfinished work and the single next action;
- at most two conditional actions tied to named unknowns;
- current validation state and stale-result warning where applicable;
- scope restrictions: no inferred permission to inspect untracked/other-worktree content, run validation, use network/providers, or mutate;
- exact coverage and omission counts.

Preserve evidence classes and conflicts. Omit machine-local paths and verbose diff payloads. Tell the receiving session to verify that HEAD and dirty state still match before acting; if they do not, it must recover again rather than trust this prompt.
