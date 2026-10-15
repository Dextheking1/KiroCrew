"""Bounded, end-only list-form options, mirrored by the frontend protocol parser."""

import re

from kiro_crew.constants import OPTIONS_RE_LINE

_HEADER = re.compile(r"\[OPTIONS\]:?", re.IGNORECASE)
# At most six items, so a number never needs more than one digit; the bound
# keeps the match linear on a long run of digits.
_ITEM = re.compile(r"(?:[-*]|[0-9]{1,2}\.)[ \t]+(\S.*)")


def split_bulleted_options_trailer(text: str) -> tuple[str, list[str]] | None:
    """Accept only a header followed by one to six nonempty single-line items."""
    lines = text.rstrip().split("\n")
    choices: list[str] = []
    for i in range(len(lines) - 1, max(-1, len(lines) - 8), -1):
        line = lines[i].rstrip()
        if _HEADER.fullmatch(line):
            if not choices:
                return None
            # The trailer supersedes any earlier ``[OPTIONS: ...]`` marker, so
            # that marker is stripped the way the frontend parser strips it.
            body = OPTIONS_RE_LINE.sub("", "\n".join(lines[:i])).rstrip()
            return body, list(reversed(choices))
        item = _ITEM.fullmatch(line)
        if not item:
            return None
        choices.append(item[1])
    return None
