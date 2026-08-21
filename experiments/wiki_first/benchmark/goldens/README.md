# Frozen synthetic references

These directories are exact generated outputs, not hand-edited ideal answers. They make prompt,
model, and implementation changes visible in ordinary Git diffs.

- `codex/baseline/` is the first Wiki built from the five baseline fixtures.
- `codex/incremental/` is the complete Wiki after adding the sixth follow-up fixture.
- A matching Claude synthetic reference remains pending because Claude Code hit its session quota
  during the 2026-08-21 run. The already-frozen private Claude baseline/incremental reference is not
  affected and remains available through the ignored bundle described in `PRIVATE_REFERENCES.md`.

The unusually high token counts are recorded deliberately. These are quality baselines for a
strong semantic administrator, not cost targets.
