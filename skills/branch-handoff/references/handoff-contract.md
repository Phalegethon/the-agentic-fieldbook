# Handoff Contract

Use this reference after one successful collector run. Read `manifest.json` and
`model-dossier.md` once; use the manifest's `artifact_paths.coverage_ledger`
as the location of the complete ledger. Treat the dossier as bounded evidence,
not as a complete file dump. If the collector reports an error or an invalid
dossier, stop rather than produce a partial handoff.

Produce the complete three-section local report in this order. The first two
sections are copy-ready drafts. Keep them local unless the user separately
authorizes a post through `platform-actions.md`. Include resolved base and head SHAs and base freshness
in scope. Use only supplied context, manifest/dossier evidence, and ledger
metadata. Never invent screen names, ticket intent, defects, product claims,
or pass/fail results; use `Uncertain` when evidence cannot support a behavior
or test surface. Authorized Jira/GitHub content proves what that platform
reported, not that the described behavior is implemented or validated; label
its provenance explicitly.

When authorized platform context is available, compare its stated intent and
acceptance criteria with collector-supported changes in the single synthesis.
Attribute every platform-derived statement to its platform and target. A ticket
or PR states expected intent; it does not prove implementation or validation.
When platform access was declined or unavailable, state that provenance in
`Known assumptions` and still map every collector cluster under this contract.

## QA Handoff

```markdown
## QA Handoff

**Scope:** <base SHA> → <head SHA>
**Change summary:** <short user/operational impact>

### Priority test surfaces

| Priority | Surface | Checkpoint | Why | Confidence |
|---|---|---|---|---|
| P0/P1/P2 | UI/flow/API/job/data/integration/config | ... | Diff evidence | Verified/Inferred/Uncertain |

### Regression focus
- ...

### Known assumptions
- ...
```

Add `base freshness: <freshness> (<base_source>)` to the scope immediately
after the template scope line. Derive the short summary only from supported
evidence. Map every collector cluster to a row in the priority-surface table
or to a changed-area item below; each cluster must map to a test surface, or
explicitly state that no reliable surface can be derived. Use descriptive
workflows when a screen name is unsupported.

Priorities are fixed:

- `P0`: Data integrity, security/authorization, payment, critical primary flow, or difficult-to-reverse behavior.
- `P1`: A directly changed user flow, API, job, data operation, or integration.
- `P2`: An adjacent regression surface or lower-impact configuration/visual behavior.

## Developer / PR Handoff

```markdown
## Developer / PR Handoff

### Problem and intent
<Supplied context or clearly labeled diff inference>

### How it was addressed
- <change cluster → implementation change → effect>

### Changed areas
- <logical clusters, not a file dump>

### Developer validation
- <only user-supplied results>
- Tests/build/lint were not run by this skill.

### QA focus
- <short summary of the highest-priority surfaces>
```

Every cluster not represented in the QA table belongs in `Changed areas`; use
logical cluster names and effects, never a long file list. State supplied
developer results with their supplied source or provenance, and always follow
them with `Tests/build/lint were not run by this skill.` Changed test files
may describe intended behavior but do not prove passing results.

## Local Evidence Appendix

The appendix records:

- Base, head, merge-base, freshness source, and exact SHAs.
- Cluster names, file counts, and ledger location.
- File/hunk support for material claims.
- Working-tree scope and dirty-state warning.
- Provenance of optional context and developer test results.
- Redactions and optional deep-dive recommendations.

Populate these slots from the manifest and dossier, including warnings,
redaction count, and optional-source metadata. Copy each cluster name and its
`file_count` atomically; do not restate per-cluster evidence allocation or
`evidence_chars`, which remain verifiable in the local manifest and dossier.
Keep file/hunk references limited to material claims; the full ledger remains
local at the manifest-provided location.

Each QA-table `Confidence` cell contains exactly one bare label from this list;
put any qualification in `Checkpoint` or `Why`. Confidence labels are fixed:

- `Verified`: Direct diff evidence or an explicitly supplied result.
- `Inferred`: Derived from paths, symbols, routes, or change patterns.
- `Uncertain`: No reliable behavior or test surface can be derived.

Deliver all three sections before offering optional, cluster-scoped deep dives
or any Jira/GitHub post. Never include the Local Evidence Appendix, credentials,
redacted values, or machine-local paths in an external comment.
Do not add a code-review verdict, a generic testing tutorial, a long file
dump, or an instruction to run or rerun target validation.
