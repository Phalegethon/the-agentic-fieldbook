# Contributing to TAF

The Agentic Fieldbook accepts focused, reusable Agent Skills that solve a
specific software-delivery problem across languages and projects.

Fork `Phalegethon/the-agentic-fieldbook`, create a focused branch in your fork,
and open a pull request against `main`. Use your own GitHub identity and follow
the pull request template. Project ownership by `@Phalegethon` does not restrict
who can contribute.

## Add a skill

1. Create `skills/<skill-name>/SKILL.md` using a lowercase kebab-case name that
   matches its directory.
2. Keep the default workflow local and require explicit authorization before
   external reads or writes.
3. Put deterministic repeated work in `scripts/` and conditional detail in
   `references/`; avoid third-party runtime dependencies unless essential.
4. Add focused tests under `tests/` and demonstrate the failure they prevent.
5. Add `.claude-plugin/plugin.json` to the skill and one matching entry to
   `.claude-plugin/marketplace.json` so the skill remains separately installable.
6. Add the implemented skill to the README table. Do not publish empty roadmap
   directories.

## Validate

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 tests/preparing_branch_handoff/benchmark_collector.py
claude plugin validate .
```

Run only the checks relevant to the skill. A documentation-only skill does not
need a synthetic application test suite, while bundled scripts must have
behavioral tests.

## Pull requests

Explain the user problem, the intended trigger, runtime requirements, external
side effects, and verification evidence. Keep unrelated refactors out of the
change.
