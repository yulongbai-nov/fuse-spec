#!/usr/bin/env python3
"""Print the next spec folder name: NNN-<slug>.

Scans <specs-dir> (default .specs) for folders matching NNN-*, takes the
highest NNN, adds 1, and zero-pads to 3 digits. Gap-tolerant; no state file.

Usage:
    next_spec_number.py <slug> [--specs-dir .specs]

Prints e.g. "004-token-rotation" to stdout.
"""

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

NAME_RE = re.compile(r"^(\d{3,})-")


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def next_number(specs_dir: Path) -> int:
    highest = 0
    if specs_dir.is_dir():
        for child in specs_dir.iterdir():
            if not child.is_dir():
                continue
            m = NAME_RE.match(child.name)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest + 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print next NNN-<slug> spec folder name."
    )
    parser.add_argument("slug", help="feature slug (will be slugified)")
    parser.add_argument(
        "--specs-dir", default=".specs", help="specs directory (default: .specs)"
    )
    args = parser.parse_args(argv)

    slug = slugify(args.slug)
    if not slug:
        print("error: slug is empty after slugification", file=sys.stderr)
        return 2

    number = next_number(Path(args.specs_dir))
    print(f"{number:03d}-{slug}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
