# Tasks: [Feature Name]

- **Spec**: NNN-<slug>
- **Status**: draft | approved
- **Requirements**: ./requirements.md (or ./bugfix.md)
- **Design**: ./design.md

## How to read this file

Each task is one checklist line ending in an inline tag the scripts parse:

```
- [ ] T<n> <short title>  [id:T<n> deps:T<a>,T<b> impl:R<x>,D-<Name>]
```

- `id` — stable task id (never renumber).
- `deps` — task ids that must finish first (empty = no blockers). `plan_waves.py` turns these into execution waves.
- `impl` — the requirement ids and design anchors this task implements. `sync_check.py` validates them.

Simple tasks stay one line. A complex task adds an **indented detail block** beneath it with the test-first steps — expand only where the extra detail earns its keep.

## Tasks

- [ ] T1 Scaffold module and config  [id:T1 deps: impl:R1]
- [ ] T2 Add data model + validation  [id:T2 deps:T1 impl:R1,D-Model]

- [ ] T3 Implement auth token rotation  [id:T3 deps:T2 impl:R2,D-Auth]
    <!-- Expanded because this task is non-trivial. Follow the tdd skill. -->
    - **Files:** `src/auth/rotate.ext`, `tests/auth/rotate_test.ext`
    - [ ] Write a failing test: rotating a valid token returns a new token and invalidates the old one.
    - [ ] Run the test → expect FAIL (function not implemented).
    - [ ] Implement the minimal rotation logic to pass.
    - [ ] Run the test → expect PASS.
    - [ ] Commit: "auth: rotate tokens".

- [ ] T4 Wire endpoint + integration test  [id:T4 deps:T3 impl:R2,D-Auth]
