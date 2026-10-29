# Design: Cleanup Refactor and Deduplication

- **Spec**: 002-cleanup-refactor
- **Status**: approved
- **Requirements**: ./requirements.md

## Overview

Restore the complete approved spec 001 design and requirements, then consolidate a small set of exact duplicate helpers. Keep domain-specific Git wrappers and distinct fixture logic separate where their error handling or inputs differ.

### Key Design Decisions

- Keep the cleanup scoped to exact common behavior; do not split the large workflow modules.
- Put the shared canonical digest and UTC timestamp helpers in fuse_record.py, which already owns run-record identity and timestamp creation and is already imported by the dependent modules.
- Put common Git and minimal repository construction helpers in a test-only support module. Production code must not depend on test support.
- Restore spec 001 directly from the complete approved local source, preserving its existing IDs and wording.

## Components

### D-SpecRecovery Restore approved spec text

satisfies: R1

**Purpose:** Ensure the main branch contains the full approved spec 001 design and requirements.

**Interface:**
- Input: The complete approved design.md and requirements.md from the integration branch.
- Output: The same complete files on the cleanup PR branch.
- Dependencies: Spec 001's traceability IDs and the current main revision.

**Notes:** Do not regenerate, summarize, or edit the spec prose. Verify the restored files against the full source by Git blob identity before publishing.

### D-CanonicalHelpers One source for digest and UTC format

satisfies: R2, R4

**Purpose:** Let record creation, check evidence, reconciliation snapshots, and remote checkpoint code reuse shared digest and timestamp helpers without changing their contracts.

**Interface:**
- Input: JSON-serializable values with optional serializer settings for a legacy snapshot digest, and requests for the current UTC timestamp.
- Output: SHA-256 over the established canonical JSON representation, and UTC ISO-8601 strings ending in Z.
- Dependencies: Python standard library; fuse_checks.py and fuse_state.py may import the helpers from fuse_record.py through the existing package/fallback import pattern.

**Notes:** Preserve json.dumps options exactly at each call site. The default digest settings are sort_keys, compact separators, ensure_ascii=False, and allow_nan=False; reconciliation explicitly retains its existing ensure_ascii=True and allow_nan=True behavior. Preserve datetime.now(UTC).isoformat().replace("+00:00", "Z"). Keep file-content hashing distinct from JSON canonicalization.

### D-TestSupport Shared test setup

satisfies: R3, R4

**Purpose:** Remove repeated Git wrapper and minimal candidate/check repository setup from the tests.

**Interface:**
- Input: An existing temporary directory, a fixture option for required sample input, and a commit message.
- Output: A rooted Git repository and spec directory with the standard minimal files.
- Dependencies: pytest temp paths and Python standard library only.

**Notes:** Existing test-specific remote, race, symlink, configuration, and source-tree fixtures remain local. The shared git helper uses cwd consistently and preserves checked text output.

## Testing Strategy

- Add or adjust focused assertions for canonical digest equivalence and unchanged UTC formatting where they fit existing tests.
- Run the full pytest suite, Ruff check and format, mypy, config validation, spec sync, and dependency planner.
- Compare the restored spec files' Git blob IDs with the complete approved source before publishing.
- Review the full diff for accidental behavior changes and new duplication.
