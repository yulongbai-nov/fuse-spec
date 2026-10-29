# Tasks: Cleanup Refactor and Deduplication

- **Spec**: 002-cleanup-refactor
- **Status**: approved
- **Requirements**: ./requirements.md
- **Design**: ./design.md

## How to read this file

Each task is one checklist line ending in an inline tag the scripts parse:

```
- [ ] T1 <short title>  [id:T1 deps: impl:R1,D-SpecRecovery]
```

## Tasks

- [x] T1 Restore complete approved spec 001 files  [id:T1 deps: impl:R1,D-SpecRecovery]
- [x] T2 Consolidate canonical JSON digest and UTC timestamp helpers  [id:T2 deps: impl:R2,D-CanonicalHelpers]
    - **Files:** `skills/scripts/fuse_record.py`, `skills/scripts/fuse_checks.py`, `skills/scripts/fuse_reconcile.py`, `skills/scripts/fuse_state.py`, related tests.
    - Add focused regression coverage, then run the affected record, check, reconciliation, and state tests.
- [x] T3 Deduplicate test Git and repository setup helpers  [id:T3 deps: impl:R3,D-TestSupport]
    - **Files:** `tests/_support.py`, candidate/check/state/PR/control/verification tests.
    - Keep repository-specific remotes and race fixtures in their existing test modules.
- [ ] T4 Run full verification and whole-codebase cleanup review  [id:T4 deps:T1,T2,T3 impl:R4,D-CanonicalHelpers,D-TestSupport]
