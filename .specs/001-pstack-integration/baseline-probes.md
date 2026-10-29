# Current Fuse validator behavior

These controlled probes were run against Fuse commit `6645e1f6f62ab9ef134acaa0dc824eb101b41adb` during the requirements interview. Inputs were isolated temporary fixtures and were removed afterward. Repository source files were not changed.

## Results

| Probe | Input | Observed result | Interpretation |
|---|---|---|---|
| Missing spec files | An existing directory with no requirements, design, or task files, passed to `sync_check.py` | Exit 0, `OK: traceability is consistent` | Passing the checker alone does not prove required artifacts exist. Future checks need to distinguish a valid draft phase from execution readiness. |
| Duplicate task IDs | Two task lines both use `T1`; the first depends on absent `T99`, the second has no dependencies | Exit 0; the wave contains one `T1` with no dependencies | The latter dictionary entry replaces the earlier task before dependency validation. |
| Checked task without proof | A checked `T1` and an unchecked `T2` depending on it; no evidence records | Exit 0; the next wave contains `T2` | The planner treats a checkbox as sufficient completion information; evidence is outside its current input contract. |
| Overlapping writes | Unchecked `T1` and `T2` with no dependencies; both detail blocks name `src/shared.ts` | Exit 0; both tasks are in the same wave | The planner uses dependency tags and does not inspect write scopes. |
| Cycle control | `T1` depends on `T2` and `T2` depends on `T1` | Exit 1; dependency cycle reported | The existing cycle rejection works for this fixture. |

These cases demonstrate the current boundary of validation. They are not a completed review of all parser edge cases.

## Reproduction inputs

Run `python3 skills/scripts/sync_check.py <empty-directory>` for the first probe.

For each remaining fixture, save the relevant text as a temporary `tasks.md` and run `python3 skills/scripts/plan_waves.py <tasks.md>`.

Duplicate IDs:

```text
- [ ] T1 First [id:T1 deps:T99 impl:R1]
- [ ] T1 Second [id:T1 deps: impl:R1]
```

Checked without proof:

```text
- [x] T1 No recorded proof [id:T1 deps: impl:R1]
- [ ] T2 Dependent [id:T2 deps:T1 impl:R1]
```

Overlapping writes:

```text
- [ ] T1 First [id:T1 deps: impl:R1]
    Files: src/shared.ts
- [ ] T2 Second [id:T2 deps: impl:R1]
    Files: src/shared.ts
```

Cycle control:

```text
- [ ] T1 First [id:T1 deps:T2 impl:R1]
- [ ] T2 Second [id:T2 deps:T1 impl:R1]
```
