# Project working agreement

This repository follows an artifact-driven, AI-native SDLC. The human owner
supplies domain knowledge, resolves judgment calls, and accepts gates. The
agent elicits missing information, exposes contradictions and risks, maintains
the artifacts, implements accepted plans, and provides verification evidence.

## Source-of-truth artifacts

- `CONTEXT.md` is the canonical glossary for project-specific terms. Create and
  extend it only when a real term has been resolved.
- `docs/adr/NNNN-slug.md` records only hard-to-reverse, surprising decisions
  that involve a genuine trade-off. Do not create ceremonial ADRs.
- Each body of work lives in `docs/work/<slug>/` and progresses through:
  `intent.md` -> 7w3 design files -> `spec.md` -> `plan.md` -> implementation
  and verification evidence.
- Keep all ten facets of one 7w3 subject together in `<subject>.7w3.md`.
  Decompose by subject, never by facet.
- The repository artifacts are authoritative. If implementation reality
  changes an accepted artifact, update the artifact in the same change and
  explain why.

## Gates

1. Do not design from an unaccepted `intent.md`.
2. Do not write a build specification until the 7w3 design has no hidden gaps.
3. Do not implement without an accepted `spec.md` and a file-specific,
   testable `plan.md`.
4. Acceptance is a human judgment. Never infer it merely because a document
   exists or was edited.
5. Every implementation must carry reproducible verification evidence.
6. Agent-written code cannot approve itself. Use a separate review pass and
   reserve final risk acceptance for the human owner.

## Elicitation standard

- Ask focused questions in manageable rounds.
- Challenge vague outcomes, unnamed users, missing boundaries, unmeasurable
  success criteria, contradictions, and solution-first assumptions.
- Mark unknowns explicitly; never silently invent domain facts.
- Prefer observable behavior and concrete examples over adjectives such as
  "fast", "simple", "smart", or "user-friendly".
- Preserve lessons from defects and review findings in the appropriate source:
  tests/evals for behavior, `AGENTS.md` for recurring working mistakes,
  `CONTEXT.md` for vocabulary, and ADRs for durable decisions.

## Test execution safety

- Never run more than one test command at a time on this host.
- Run every test through
  `/root/.local/bin/codex-test-guard --timeout <duration> -- <command...>`.
- Use a repository-documented timeout when available; otherwise use `10m` for
  a targeted test and `20m` for a full suite.
- Before starting a test, confirm no earlier test process or yielded test
  session is still active. After a timeout or failure, confirm the process and
  descendants exited before diagnosis or retry.
- Never automatically retry a failed full suite.
