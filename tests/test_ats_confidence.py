"""Tests for ats_confidence - pytest-native, pure functions, no network."""

from __future__ import annotations

import pytest
from ats_confidence import MIN_SKILLS, score_ats

FIXED = {
    "title_points": 13,
    "experience_points": 15,
    "responsibility_points": 8,
    "education_points": 5,
    "parse_points": 10,
}
PERFECT_FIXED = {
    "title_points": 15,
    "experience_points": 15,
    "responsibility_points": 10,
    "education_points": 5,
    "parse_points": 10,
}


def test_word_boundary_matching() -> None:
    result = score_ats(
        required=["Go", "C#", "Java", "R"],
        preferred=["Node.js", "AI"],
        resume_text="Google Cloud, Java 21, Rust experience, AI-900 certification",
        unsupported=["C#", "Node.js"],
        **FIXED,
    )
    assert "Java" in result.matched
    assert "Go" not in result.matched, "'Go' must not match inside 'Google'"
    assert "R" not in result.matched, "'R' must not match inside 'Rust'"
    assert "C#" in result.missing_unsupported
    assert "Node.js" in result.missing_unsupported


def test_ceiling_excludes_unsupported_skills() -> None:
    result = score_ats(
        required=["Java", "REST", "C#", "Node.js", "Bedrock", "NoSQL", "SQL", "Python"],
        preferred=["Docker", "Kafka"],
        resume_text="Java, REST APIs, SQL, Python, Docker",
        unsupported=["C#", "Node.js", "Bedrock", "NoSQL", "Kafka"],
        **FIXED,
    )
    assert result.at_ceiling, "only unsupported skills are missing, so this is fully optimised"
    assert result.score == pytest.approx(result.ceiling)
    assert "Kafka" in result.missing_unsupported
    assert any("unreachable truthfully" in note for note in result.notes)


def test_under_tailored_scores_below_its_ceiling() -> None:
    result = score_ats(
        required=["Java", "REST", "SQL", "Docker", "Kafka", "Redis", "Git", "Maven"],
        preferred=["Linux", "JIRA"],
        resume_text="Java only",
        unsupported=[],
        **FIXED,
    )
    assert not result.at_ceiling
    assert result.ceiling > result.score + 5
    assert len(result.missing_claimable) == 9


def test_thin_jd_scoring_near_100_is_low_confidence() -> None:
    """Replays the real PradeepIT row: ATS 100 against match 71 from a 2-skill JD."""
    result = score_ats(
        required=["Java", "SQL"],
        preferred=[],
        resume_text="Java, SQL",
        unsupported=[],
        **PERFECT_FIXED,
    )
    assert result.score >= 95
    assert result.confidence == "LOW"
    assert result.auto_apply_eligible is False, "a LOW-confidence 100 must never auto-apply"
    assert any("too small to measure" in note for note in result.notes)


@pytest.mark.parametrize(
    ("required", "matched"),
    [(["Java", "Python"], "Java"), (["Java", "Python", "Go"], "Java Python")],
)
def test_greenhouse_exact_fraction_artefacts_are_flagged(required: list[str], matched: str) -> None:
    """The rows sitting at exactly 50.0 and 66.7 are small-denominator artefacts."""
    result = score_ats(
        required=required, preferred=[], resume_text=matched, unsupported=[], **FIXED
    )
    assert result.confidence == "LOW"
    assert result.auto_apply_eligible is False


@pytest.mark.parametrize(
    ("n_required", "n_preferred", "expected"),
    [
        (3, 2, "LOW"),
        (MIN_SKILLS - 1, 0, "LOW"),
        (MIN_SKILLS, 0, "MEDIUM"),
        (10, 5, "MEDIUM"),
        (20, 5, "HIGH"),
    ],
)
def test_confidence_bands(n_required: int, n_preferred: int, expected: str) -> None:
    result = score_ats(
        required=[f"s{i}" for i in range(n_required)],
        preferred=[f"p{i}" for i in range(n_preferred)],
        resume_text="",
        unsupported=[],
        **FIXED,
    )
    assert result.confidence == expected


def test_auto_apply_needs_score_and_confidence() -> None:
    skills = [f"s{i}" for i in range(20)]
    high = score_ats(
        required=skills,
        preferred=["p"],
        resume_text=" ".join(skills) + " p",
        unsupported=[],
        **PERFECT_FIXED,
    )
    assert high.score == pytest.approx(100.0)
    assert high.confidence == "HIGH"
    assert high.auto_apply_eligible

    low = score_ats(required=skills, preferred=["p"], resume_text="s1 s2", unsupported=[], **FIXED)
    assert low.score < 90
    assert low.auto_apply_eligible is False


def test_degenerate_empty_jd() -> None:
    result = score_ats(required=[], preferred=[], resume_text="", unsupported=[], **FIXED)
    assert result.confidence == "LOW"
    assert result.auto_apply_eligible is False
    assert any("no preferred skills" in note for note in result.notes)
