"""Location purpose tiers.

TARGET   - places Leela would accept an offer: remote, Hyderabad, Bengaluru,
           Chennai and the Gulf.
PRACTICE - places she applies to only to practise interviews (for now:
           Pune, Mumbai, Delhi NCR, Kolkata). She has said she will not accept
           offers there, so these rows are labelled clearly and reported apart.
OTHER    - anything else that passed the work-authorisation gate.

The city lists live in config/discovery.yaml so she can move a city between
tiers without a code change.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .extract import Extracted

TARGET = "TARGET"
PRACTICE = "PRACTICE"
OTHER = "OTHER"
GULF = {"UAE", "Qatar", "Saudi Arabia"}


def _matcher(words: Iterable[str]) -> re.Pattern[str] | None:
    words = [w.strip() for w in words if w and w.strip()]
    if not words:
        return None
    return re.compile(r"(?i)(?<![a-z])(" + "|".join(re.escape(w) for w in words) + r")(?![a-z])")


class LocationTiers:
    def __init__(self, target: Iterable[str], practice: Iterable[str]):
        self._target = _matcher(target)
        self._practice = _matcher(practice)

    def purpose(self, ex: Extracted) -> str:
        if ex.work_mode_label in ("Remote-India", "Remote-Worldwide"):
            return TARGET
        if ex.country in GULF:
            return TARGET
        blob = " | ".join([ex.job.location, *ex.extra_locations])
        # A posting that lists a target city anywhere is a target role, even if
        # a practice city is listed too.
        if self._target and self._target.search(blob):
            return TARGET
        if self._practice and self._practice.search(blob):
            return PRACTICE
        return OTHER
