"""IANA time zone names, checked against the tz database shipped with Python (`tzdata`).

A name is only ever compared with the database's own list, never turned into a path, so a
crafted value cannot make `zoneinfo` read an arbitrary file.
"""

import re
import zoneinfo
from functools import lru_cache

# Syntax check first: cheap, and bounds what reaches the set lookup.
TIMEZONE_PATTERN = r"^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){0,2}$"
_TIMEZONE_RE = re.compile(TIMEZONE_PATTERN)


@lru_cache(maxsize=1)
def _known() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones())


def is_known_timezone(name: str) -> bool:
    return len(name) <= 64 and bool(_TIMEZONE_RE.match(name)) and name in _known()
