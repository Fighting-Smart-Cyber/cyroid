"""Semantic version helpers for the release update channel.

Kept apart from the update endpoint so the ordering rules can be tested
without Docker, a git remote, or an API client.

Only X.Y.Z is recognised. scripts/tag-release.sh already refuses anything
else -- the CI release rule matches ^v[0-9]+\\.[0-9]+\\.[0-9]+$ exactly, so a
tag like v1.0.0-rc1 runs no release jobs and must not be offered as an update
either.
"""

import re
from typing import Iterable, Optional, Tuple

# Anchored, and the leading "v" is optional so the same parser reads a git tag
# ("v0.43.0") and the VERSION file ("0.43.0").
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_version(text: Optional[str]) -> Optional[Tuple[int, int, int]]:
    """(major, minor, patch), or None if this is not an X.Y.Z version.

    None rather than a zero tuple: an unparseable version must not sort as
    0.0.0 and make every real release look newer than it.
    """
    if not text:
        return None
    m = _SEMVER.match(text.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def latest_release(tags: Iterable[str]) -> Optional[str]:
    """The highest X.Y.Z tag, as originally spelled, or None.

    Sorted numerically, not lexically -- "v0.9.0" > "v0.10.0" as strings, which
    would pin a host to an older release forever.
    """
    best: Optional[Tuple[Tuple[int, int, int], str]] = None
    for tag in tags:
        parsed = parse_version(tag)
        if parsed is None:
            continue
        if best is None or parsed > best[0]:
            best = (parsed, tag.strip())
    return None if best is None else best[1]


def is_newer(candidate: Optional[str], current: Optional[str]) -> bool:
    """Is `candidate` a strictly newer release than `current`?

    False when either side is unparseable. A host whose VERSION is "dev" (a
    working checkout, not a release) is not offered every tag ever cut -- it
    already has code no tag describes, and "update" would move it backwards.
    """
    c = parse_version(candidate)
    n = parse_version(current)
    if c is None or n is None:
        return False
    return c > n
