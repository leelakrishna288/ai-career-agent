"""ATS estimate with a truthful ceiling and a confidence flag.

Two defects found on 2026-09-18 in the live tracker, both fixed here.

1. Thin-JD artefacts. Four rows carried scores that cannot be measurements:
   PradeepIT Senior Java ATS 100 against match 71.2, gravity9 88.9/62.3,
   micro1 85.7/62.6, and one row at ATS 0. Two Greenhouse rows sat at exactly
   50.0 and two at exactly 66.7. Those are small-denominator artefacts - a JD
   from which only two or three skills were extracted can score 100 because
   both matched. Left alone, a thin JD can manufacture a 100 and, once the
   auto-apply rule fires, drive a real submission.
   Fix: `confidence`. Fewer than MIN_SKILLS extracted -> LOW, and LOW rows are
   excluded from the auto-apply rule whatever the score.

2. Unreachable gate. The READY gate is ESTIMATED ATS >= 90, but on most good
   JDs the missing points sit in technologies Leela does not have (C#, Node,
   Bedrock, NoSQL). The only way to 90 is fabrication, which SYSTEM_SPEC 3 and
   12 forbid. Result: 12 resumes prepared, 1 ever reached READY.
   Fix: `ceiling` - the best score reachable without claiming anything
   unsupported. A score at its ceiling is fully optimised; a score below its
   ceiling is genuinely under-tailored and should be re-run.

This module only measures. Whether a below-90 resume gets routed to Leela is a
policy decision in SYSTEM_SPEC 5 step 7 and is not decided here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

KEYWORD_POINTS = 35
PREFERRED_POINTS = 10
MIN_SKILLS = 8  # below this, the denominator is too small to measure


@dataclass(frozen=True)
class AtsResult:
    score: float
    ceiling: float
    confidence: str  # HIGH | MEDIUM | LOW
    matched: tuple[str, ...]
    missing_claimable: tuple[str, ...]
    missing_unsupported: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def at_ceiling(self) -> bool:
        return self.ceiling - self.score < 0.5

    @property
    def auto_apply_eligible(self) -> bool:
        """Gate 11a: >= 90 AND the score must be measurable."""
        return self.score >= 90 and self.confidence != "LOW"


def _present(skill: str, resume_text: str) -> bool:
    """Word-boundary match, so 'Go' does not match 'Google' and 'C#' works."""
    token = skill.strip().lower()
    if not token:
        return False
    escaped = re.escape(token)
    if token[0].isalnum():
        escaped = r"\b" + escaped
    if token[-1].isalnum():
        escaped = escaped + r"\b"
    return re.search(escaped, resume_text.lower()) is not None


def score_ats(
    *,
    required: list[str],
    preferred: list[str],
    resume_text: str,
    unsupported: list[str],
    title_points: float,
    experience_points: float,
    responsibility_points: float,
    education_points: float,
    parse_points: float,
) -> AtsResult:
    """Score one resume against one JD.

    `unsupported` names the skills Leela cannot truthfully claim, so they are
    excluded from the ceiling instead of being counted as closeable gaps.
    """
    notes: list[str] = []
    unsup = {u.strip().lower() for u in unsupported}

    def classify(skills: list[str]) -> tuple[list[str], list[str], list[str]]:
        hit, claimable, blocked = [], [], []
        for s in skills:
            if _present(s, resume_text):
                hit.append(s)
            elif s.strip().lower() in unsup:
                blocked.append(s)
            else:
                claimable.append(s)
        return hit, claimable, blocked

    req_hit, req_claimable, req_blocked = classify(required)
    pref_hit, pref_claimable, pref_blocked = classify(preferred)

    kw = KEYWORD_POINTS * len(req_hit) / len(required) if required else 0.0
    kw_ceiling = (
        KEYWORD_POINTS * (len(req_hit) + len(req_claimable)) / len(required)
        if required
        else 0.0
    )
    pf = PREFERRED_POINTS * len(pref_hit) / len(preferred) if preferred else PREFERRED_POINTS * 0.8
    pf_ceiling = (
        PREFERRED_POINTS * (len(pref_hit) + len(pref_claimable)) / len(preferred)
        if preferred
        else pf
    )
    if not preferred:
        notes.append("JD lists no preferred skills; that component scored at 0.8 of maximum")

    fixed = title_points + experience_points + responsibility_points + education_points + parse_points
    score = round(kw + pf + fixed, 1)
    ceiling = round(kw_ceiling + pf_ceiling + fixed, 1)

    total_skills = len(required) + len(preferred)
    if total_skills < MIN_SKILLS:
        confidence = "LOW"
        notes.append(
            f"only {total_skills} skills extracted from the JD (minimum {MIN_SKILLS}); "
            "the denominator is too small to measure - excluded from the auto-apply rule"
        )
    elif total_skills < MIN_SKILLS * 2:
        confidence = "MEDIUM"
    else:
        confidence = "HIGH"

    if req_blocked or pref_blocked:
        notes.append(
            "ceiling excludes unsupported skills: " + ", ".join(sorted(req_blocked + pref_blocked))
        )
    if ceiling < 90:
        notes.append(
            f"90 is unreachable truthfully for this JD (ceiling {ceiling}); "
            "closing the gap would require an unsupported claim"
        )

    return AtsResult(
        score=score,
        ceiling=ceiling,
        confidence=confidence,
        matched=tuple(req_hit + pref_hit),
        missing_claimable=tuple(req_claimable + pref_claimable),
        missing_unsupported=tuple(req_blocked + pref_blocked),
        notes=tuple(notes),
    )
