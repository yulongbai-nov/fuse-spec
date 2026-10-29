"""Small Markdown helpers shared by Fuse's spec processing tools."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from re import Match

FENCE_OPEN_RE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})(?P<info>.*)$")
FENCE_CLOSE_RE = re.compile(r"^ {0,3}(?P<marker>`+|~+)[ \t]*$")
TASK_CHECKBOX_ROW_RE = re.compile(
    r"^(?P<bullet>[-*+][ \t]+)"
    r"\[(?P<status>[ xX])\][ \t]+(?P<rest>[^\r\n]*)(?P<ending>\r?\n)?$"
)
TASK_ID_TAG_RE = re.compile(r"\[[^\]]*\bid:[^\s\]]+[^\]]*\]")


def task_checkbox_row_match(line: str) -> Match[str] | None:
    """Match a column-zero task row with a checkbox and a nonempty ID tag."""
    match = TASK_CHECKBOX_ROW_RE.match(line)
    if match is None or TASK_ID_TAG_RE.search(match.group("rest")) is None:
        return None
    return match


def markdown_fence_line_flags(lines: Iterable[str]) -> Iterator[bool]:
    """Yield true for top-level CommonMark fenced-code delimiters and contents.

    Container-prefixed fences are outside this helper's scope. Fuse task rows
    use the separate strict checkbox-row predicate so nested and quoted task
    examples cannot become declarations or checkbox projections.
    """
    fence_character: str | None = None
    fence_length = 0
    for line in lines:
        source_line = line.rstrip("\r\n")
        if fence_character is None:
            opening = FENCE_OPEN_RE.match(source_line)
            if opening is None:
                yield False
                continue
            marker = opening.group("marker")
            info = opening.group("info")
            if marker[0] == "`" and "`" in info:
                yield False
                continue
            fence_character = marker[0]
            fence_length = len(marker)
            yield True
            continue

        closing = FENCE_CLOSE_RE.match(source_line)
        yield True
        if (
            closing is not None
            and closing.group("marker")[0] == fence_character
            and len(closing.group("marker")) >= fence_length
        ):
            fence_character = None
            fence_length = 0
