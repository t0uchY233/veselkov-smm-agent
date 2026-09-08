# Evidence: Slice 2

Date: 2026-09-04
Spec: 0.2.1
Status: implemented; final risk acceptance remains with Sardor

## Demonstrated behavior

- Plan import opens only the plan gate; only `author` can decide it.
- Editorial import requires 3-5 ordered visual anchors, supported material
  claims, current TOV/Humanize hashes, a clean lint record and a 5-10 minute text.
- The app derives identical Dzen text, protected teleprompter text, Telegram
  template and a DOCX with the same visual bytes.
- Cover and visual dimensions are verified before any state mutation.
- Editorial approval moves the release to `awaiting_recording`.
- A revision invalidates the affected approval and artifacts; reimport creates
  new immutable artifact versions without reviving stale paths.
- An existing Slice 1 database upgrades through migration 2.
- The project `veselkov-smm` skill always reads CLI state and preserves three
  human gates; its scaffold validator passes.

## Reproducible verification

All verification commands ran sequentially through `codex-test-guard`:

```text
uv run ruff check .
All checks passed!

uv run mypy
Success: no issues found in 25 source files

uv run pytest
12 passed in 6.14s

python .../skill-creator/scripts/quick_validate.py .agents/skills/veselkov-smm
Skill is valid!
```

The first targeted Slice 2 run failed with three missing-command failures before
implementation. Later gate failures exposed incorrect CLI exit codes and stale
generated schemas; both were fixed before this evidence was written.

Independent Claude review was unavailable because the installed CLI is not
logged in. The raw result and separate builder findings are preserved under
`reviews/slice-02/iter-01/`; neither substitutes for human risk acceptance.
