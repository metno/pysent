# Plans — index

**Every plan this project has, open or closed, has a row here**, so that this file answers "what has
already been planned?" without reading every file in `PLANNING/`. The checker only *requires* a row
for an open (`TODO_`-prefixed) plan, so keeping the closed ones listed — and backfilling the plans
that predate the session system — is a habit, not something CI will catch for you.

`TODO_`-prefixed files are OPEN; a closed plan loses the prefix and gains a ✅ (or ◐) banner
(`WORKFLOW.md` §5.3). How we work: [`WORKFLOW.md`](WORKFLOW.md). What runs next:
[`TODO_session_sequence.md`](TODO_session_sequence.md). Decisions that bind more than one plan:
[`DECISIONS.md`](DECISIONS.md). `python3 PLANNING/check_planning.py` checks that every open plan has a
row here and that every link resolves.

## Open

| Added | Plan | What | State |
|---|---|---|---|
| 2026-10-31 | [`TODO_PLANNING_composites.md`](TODO_PLANNING_composites.md) | composite several granules into one map | Phase 1 shipped (#35); later phases not started |

## Completed

Plans that predate the session system, backfilled when it was installed. Their files are in
[`legacy/`](legacy/); each carries a superseded banner. Their evidence scripts stayed where they
are — `*_audit/` in this directory — because commit messages cite them by path and they are how any
number in these plans is re-checked.

| Closed | Plan | PRs |
|---|---|---|
| 2026-11-05 | [`legacy/PLANNING_atmospheric_correction.md`](legacy/PLANNING_atmospheric_correction.md) | #32, #33 |
| 2026-10-31 | [`legacy/PLANNING_s3_review_fixes.md`](legacy/PLANNING_s3_review_fixes.md) | #31 |
| 2026-10-30 | [`legacy/PLANNING_s5p_s3_pipelines.md`](legacy/PLANNING_s5p_s3_pipelines.md) | #27 |
| 2026-10-22 | [`legacy/PLANNING_s1_radiometry.md`](legacy/PLANNING_s1_radiometry.md) | #11 |
| 2026-10-20 | [`legacy/PLANNING_bulk_processing.md`](legacy/PLANNING_bulk_processing.md) | #4–#10, #12 |
