# TAF Unified Plugin and Release Design

**Date:** 2026-08-27  
**Status:** Approved  
**Target release:** The Agentic Fieldbook 2.0.0

## Purpose

Present The Agentic Fieldbook (TAF) as one coherent product across Claude Code,
Codex, and other supported coding agents while keeping each skill independently
discoverable and loaded only when relevant.

Today, Claude's marketplace treats `branch-handoff` and `work-recovery` as
separate plugins because each skill directory is published as its own plugin.
Their plugin detail pages consequently report zero contained skills, and the
GitHub Latest release represents only `branch-handoff`. The resulting product
model is fragmented and does not match the multi-skill experience established
by Superpowers.

## Goals

- Install TAF once and expose all shipped skills beneath it.
- Show the contained skills in Claude and Codex plugin interfaces.
- Keep each skill's instructions, scripts, references, tests, and semantic
  version independent inside the repository.
- Load a skill's full instructions only when the host selects or invokes it.
- Replace per-skill GitHub releases with a primary TAF product release stream.
- Provide a short, deterministic migration from the two legacy Claude plugins.
- Use the same canonical `skills/` sources for every host; do not copy bundles.

## Non-goals

- Changing the behavior of `branch-handoff` or `work-recovery`.
- Loading every skill body into every model context.
- Automatically uninstalling software from a user's machine.
- Deleting historical tags or GitHub releases.
- Publishing, pushing, or creating a GitHub release as part of the local change.
- Building a general cross-agent package manager.

## Product Model

TAF is the distribution unit. A skill remains the execution and context unit.

| Layer | Identity | Version responsibility |
| --- | --- | --- |
| Product | `taf` | Collection, packaging, namespaces, and release contract |
| Skill | `branch-handoff`, `work-recovery` | The individual behavior contract |

The documented user model is: **Install TAF once; use its skills independently.**
Manual extraction of a single skill may remain technically possible, but it is
not the primary supported installation path.

## Repository Architecture

The repository root becomes the plugin root for every plugin-capable host:

```text
the-agentic-fieldbook/
├── .claude-plugin/
│   ├── marketplace.json
│   └── plugin.json
├── .codex-plugin/
│   └── plugin.json
└── skills/
    ├── branch-handoff/
    │   ├── SKILL.md
    │   ├── agents/openai.yaml
    │   ├── references/
    │   └── scripts/
    └── work-recovery/
        ├── SKILL.md
        ├── agents/openai.yaml
        ├── references/
        └── scripts/
```

Both root plugin manifests reference the canonical `skills/` directory. The
Claude marketplace contains exactly one plugin entry named `taf`, sourced from
the repository root. Per-skill `.claude-plugin/plugin.json` files are removed so
they cannot continue advertising separate products. Existing
`agents/openai.yaml` files remain the skill-specific Codex presentation layer.

The product display name is `The Agentic Fieldbook (TAF)`. Its short description
explains the outcome-oriented recovery and handoff collection rather than
describing an implementation detail.

## Discovery, Invocation, and Context Cost

Claude exposes the skills as:

- `/taf:branch-handoff`
- `/taf:work-recovery`

Codex exposes the same logical hierarchy using its plugin-qualified skill names,
such as `taf:branch-handoff` and `taf:work-recovery`, while retaining automatic
skill selection where supported. Other hosts use their native invocation syntax
without changing the product or skill identities.

Installation makes the collection available; it does not place every skill body
in every prompt. Hosts discover compact skill metadata first and read the full
`SKILL.md` plus any routed reference only when the skill applies. The packaging
change must not introduce a generated aggregate prompt or duplicated skill
content.

## Version and Release Model

The initial unified release is TAF `2.0.0` because the Claude installation and
invocation namespaces change incompatibly. The marketplace version and both root
plugin manifest versions are aligned at `2.0.0`.

The bundled skill versions remain:

- `branch-handoff`: `1.2.1`
- `work-recovery`: `1.0.1`

Skill versions change only when that skill's behavior contract changes. TAF uses
semantic versioning as follows:

- major: breaking installation, namespace, or collection-contract change;
- minor: a new backward-compatible skill or product capability;
- patch: compatible packaging, metadata, documentation, or integration fix.

The next GitHub release is tagged `v2.0.0`, titled `The Agentic Fieldbook 2.0.0`,
and becomes Latest when the user publishes it. Its notes list all bundled skills
and their versions. Historical `branch-handoff` releases and tags remain
available as legacy records. Future primary releases follow the TAF product
stream instead of publishing a skill as though it were the whole product.

## Clean Migration

The release documentation provides one copyable migration sequence:

1. uninstall the installed `branch-handoff` and `work-recovery` Claude plugins;
2. update the `the-agentic-fieldbook` marketplace;
3. install `taf` from that marketplace;
4. reload plugins or restart the Claude Code session;
5. verify that both `/taf:*` skills are listed.

Uninstalling precedes the marketplace update so the legacy qualified plugin
identities remain resolvable during the normal path. The final commands must be
validated against the current Claude CLI before publication. No legacy aliases
or duplicate marketplace entries are retained; carrying both models would keep
the Directory clutter and create ambiguous invocation paths.

If migration fails, existing Git tags preserve the old artifacts. Recovery
guidance may point users to a legacy tag, but the primary installation remains
the unified TAF plugin.

## Error Handling and Safety

- A missing skill directory, malformed manifest, version mismatch, or duplicate
  marketplace entry fails release validation.
- Host-specific metadata must never redefine the skill behavior contract.
- Migration instructions must not claim that uninstalling one host affects any
  other host installation.
- Local implementation does not push commits, edit GitHub releases, or modify a
  user's installed plugins.
- Existing skill runtime safety boundaries and consent rules remain unchanged.

## Documentation Changes

The README will lead with the one-product model, provide host-specific TAF
installation instructions, show the qualified skill names, and contain the clean
Claude migration block. Per-skill outcome and usage descriptions remain visible
inside the skill catalog, but separate Claude plugin installation sections are
removed.

The changelog records the breaking packaging and namespace change. Release notes
are prepared locally so the user can publish `v2.0.0` after pushing.

## Verification Strategy

Automated tests must prove:

- the marketplace advertises exactly one root-sourced `taf` plugin;
- Claude and Codex root manifests both identify TAF `2.0.0` and reference the
  canonical `skills/` directory;
- the two implemented skill directories are discoverable from both manifests;
- no per-skill Claude plugin manifest remains;
- TAF and skill versions follow their separate contracts;
- README commands use the unified namespace and no active documentation advises
  installing a legacy plugin;
- the repository layout and vendored runtime invariants still hold.

The implementation will run the full repository test suite, the Claude plugin
validator, any available Codex manifest validation, and focused release/layout
tests. If a validator is unavailable, that limitation is recorded rather than
silently replacing it with an unverified claim.

## Acceptance Criteria

The local release candidate is complete when:

1. Claude and Codex each recognize one TAF plugin containing both skills.
2. The former individual Claude plugin products are absent from the marketplace.
3. Skill behavior and bounded-context guarantees are unchanged.
4. Documentation gives a verified clean migration and unified invocation model.
5. Release metadata consistently reports TAF `2.0.0` and the two bundled skill
   versions.
6. All available automated and platform validators pass.
7. Commits use only the configured Phalegethon personal identity.
8. No push or GitHub release publication occurs without separate authorization.
