"""Tests for ats_confidence - pure functions, no network."""
from __future__ import annotations

from ats_confidence import MIN_SKILLS, score_ats

FIXED = {
    "title_points": 13,
    "experience_points": 15,
    "responsibility_points": 8,
    "education_points": 5,
    "parse_points": 10,
}


def test_word_boundary_matching():
    r = score_ats(
        required=["Go", "C#", "Java", "R"],
        preferred=["Node.js", "AI"],
        resume_text="Google Cloud, Java 21, Rust experience, AI-900 certification",
        unsupported=["C#", "Node.js"],
        **FIXED,
    )
    assert "Java" in r.matched, r.matched
    assert "Go" not in r.matched, "'Go' must not match inside 'Google'"
    assert "R" not in r.matched, "'R' must not match inside 'Rust'"
    assert "C#" in r.missing_unsupported and "Node.js" in r.missing_unsupported
    print("word-boundary matching OK - Go/Google, R/Rust, C# handled")


def test_ceiling_excludes_unsupported():
    r = score_ats(
        required=["Java", "REST", "C#", "Node.js", "Bedrock", "NoSQL", "SQL", "Python"],
        preferred=["Docker", "Kafka"],
        resume_text="Java, REST APIs, SQL, Python, Docker",
        unsupported=["C#", "Node.js", "Bedrock", "NoSQL", "Kafka"],
        **FIXED,
    )
    # 4/8 required present, 4 unsupported -> keyword can never exceed 4/8
    assert abs(r.score - r.ceiling) < 0.6, (r.score, r.ceiling)
    assert r.at_ceiling, "with only unsupported skills missing, the resume is at its ceiling"
    assert "Kafka" in r.missing_unsupported
    assert any("unreachable truthfully" in n for n in r.notes)
    print(f"ceiling OK - score {r.score}, ceiling {r.ceiling}, at_ceiling={r.at_ceiling}")


def test_under_tailored_is_below_ceiling():
    r = score_ats(
        required=["Java", "REST", "SQL", "Docker", "Kafka", "Redis", "Git", "Maven"],
        preferred=["Linux", "JIRA"],
        resume_text="Java only",
        unsupported=[],
        **FIXED,
    )
    assert r.ceiling > r.score + 5 and not r.at_ceiling
    assert len(r.missing_claimable) == 9
    print(f"under-tailored OK - score {r.score} well below ceiling {r.ceiling} -> re-tailor")


def test_thin_jd_artefacts_are_flagged():
    """Replays the four real tracker rows that carried impossible scores."""
    # PradeepIT: 2 skills extracted, both matched -> a clean 100 that means nothing
    r = score_ats(
        required=["Java", "SQL"],
        preferred=[],
        resume_text="Java, SQL",
        unsupported=[],
        title_points=15,
        experience_points=15,
        responsibility_points=10,
        education_points=5,
        parse_points=10,
    )
    # 98 not 100 here only because this module scores an absent preferred-skill
    # list at 0.8 of maximum; the artefact is the same - a near-perfect score
    # from a 2-skill denominator.
    assert r.score >= 95, r.score
    assert r.confidence == "LOW"
    assert r.auto_apply_eligible is False, "a LOW-confidence 100 must never auto-apply"
    assert any("too small to measure" in n for n in r.notes)
    # the exactly-50.0 and exactly-66.7 Greenhouse artefacts
    for req, hit in ((["Java", "Python"], "Java"), (["Java", "Python", "Go"], "Java Python")):
        a = score_ats(required=req, preferred=[], resume_text=hit, unsupported=[], **FIXED)
        assert a.confidence == "LOW"
        assert a.auto_apply_eligible is False
    print("thin-JD OK - a 2-skill JD scoring 100 is LOW confidence and cannot auto-apply")


def test_confidence_bands():
    def conf(n_req, n_pref):
        return score_ats(
            required=[f"s{i}" for i in range(n_req)],
            preferred=[f"p{i}" for i in range(n_pref)],
            resume_text="",
            unsupported=[],
            **FIXED,
        ).confidence

    assert conf(3, 2) == "LOW"
    assert conf(MIN_SKILLS - 1, 0) == "LOW"
    assert conf(MIN_SKILLS, 0) == "MEDIUM"
    assert conf(10, 5) == "MEDIUM"
    assert conf(20, 5) == "HIGH"
    print(f"confidence bands OK - LOW < {MIN_SKILLS} <= MEDIUM < {MIN_SKILLS * 2} <= HIGH")


def test_auto_apply_gate():
    high = score_ats(
        required=[f"s{i}" for i in range(20)],
        preferred=["p"],
        resume_text=" ".join(f"s{i}" for i in range(20)) + " p",
        unsupported=[],
        title_points=15,
        experience_points=15,
        responsibility_points=10,
        education_points=5,
        parse_points=10,
    )
    assert high.score == 100.0 and high.confidence == "HIGH" and high.auto_apply_eligible
    low = score_ats(
        required=[f"s{i}" for i in range(20)],
        preferred=["p"],
        resume_text="s1 s2",
        unsupported=[],
        **FIXED,
    )
    assert low.score < 90 and low.auto_apply_eligible is False
    print("auto-apply gate OK - needs >=90 AND measurable confidence")


def test_empty_and_degenerate_input():
    r = score_ats(required=[], preferred=[], resume_text="", unsupported=[], **FIXED)
    assert r.confidence == "LOW" and r.score == 8 + 51 and not r.auto_apply_eligible, r
    assert any("no preferred skills" in n for n in r.notes)
    print("degenerate input OK - empty JD scores only the fixed components, flagged LOW")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\nALL {len(tests)} ATS CONFIDENCE TESTS PASS")
