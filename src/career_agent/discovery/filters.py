"""Cheap gates that run BEFORE scoring.

Title relevance and work-authorisation reachability are checked first so
analysis effort is never spent on a job that could not be applied to - the
weekly analytics found 10% of scored rows were unreachable on work
authorisation alone.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .extract import Extracted


def compile_any(words: Iterable[str]) -> re.Pattern[str] | None:
    words = [w for w in words if w.strip()]
    if not words:
        return None
    return re.compile(r"(?i)\b(" + "|".join(re.escape(w.strip()) for w in words) + r")\b")


def title_relevant(
    title: str, include: re.Pattern[str] | None, exclude: re.Pattern[str] | None
) -> tuple[bool, str]:
    if exclude and (m := exclude.search(title)):
        return False, f"title excluded by '{m.group(0)}'"
    if include and not include.search(title):
        return False, "title not in target role families"
    return True, ""


def reachable(ex: Extracted, allowed_countries: set[str]) -> tuple[bool, str]:
    """Work authorisation / location gate. Unknown country is allowed through
    to scoring (and flagged) rather than silently dropped."""
    country = ex.country
    if not country:
        return True, "country unknown - verify location before applying"
    if country in allowed_countries:
        return True, ""
    if country == "Remote-Worldwide":
        return True, ""
    return False, f"work authorisation: {country} role ({ex.job.location or 'no location'})"
