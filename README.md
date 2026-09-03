# media-controller
media controller for reaction video making.

## Documents

- [`Ahmed_Reaction_Studio_MASTER_BLUEPRINT.md`](./Ahmed_Reaction_Studio_MASTER_BLUEPRINT.md) — frozen architecture & requirements (source of truth).
- [`PLAN.md`](./PLAN.md) — the trackable execution plan: 450 checkable tasks across 10 phases plus sign-off checklists.

## Tracking progress

```bash
python3 scripts/update_plan_progress.py                       # recompute the progress dashboard
python3 scripts/update_plan_progress.py status                # print per-phase counts
python3 scripts/update_plan_progress.py done P1-01 -n "note"  # mark a task complete + stamp the ledger
python3 scripts/update_plan_progress.py undone P1-01          # revert a task
```

Every completed task is flipped to `- [x]` in `PLAN.md` and stamped with a date in the
Task Status Register (Appendix F). The Progress Dashboard near the top of `PLAN.md` is
auto-generated — never hand-edit between the `PROGRESS:START` / `PROGRESS:END` markers.
