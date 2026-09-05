"""The validation gate is the heart of this project, so it gets the most tests."""

import pytest

from career_agent.models import Experience, ProjectEntry, TailoredResume
from career_agent.validation import ValidationGate


@pytest.fixture
def gate(profile):
    return ValidationGate(profile)


def base_resume(profile, **overrides):
    data = {
        "resume_id": "TEST_V01",
        "headline": "AI Engineer | Agentic Systems",
        "summary": profile.summary_sentences[0],
        "skills": {"Core": ["Python", "Java"]},
        "experience": list(profile.experience),
        "projects": [],
        "certifications": list(profile.certifications),
        "education": list(profile.education),
    }
    data.update(overrides)
    return TailoredResume(**data)


class TestSkillClaims:
    def test_clean_resume_passes(self, profile, gate):
        assert gate.validate(base_resume(profile)).passed

    def test_invented_skill_is_blocked(self, profile, gate):
        result = gate.validate(base_resume(profile, skills={"Core": ["Rust", "Elixir"]}))
        assert not result.passed
        assert {i.kind for i in result.blocking} == {"unknown_skill"}

    def test_learning_labelled_skill_is_blocked(self, profile, gate):
        """This is the exact failure the gate exists to prevent: the profile
        records Spring Boot at LEARNING, so it can never be printed."""
        result = gate.validate(base_resume(profile, skills={"Core": ["Spring Boot"]}))
        assert not result.passed
        assert any(
            i.kind == "unsupported_skill" and "Spring Boot" in i.detail for i in result.blocking
        )

    def test_unsupported_skill_is_blocked(self, profile, gate):
        result = gate.validate(base_resume(profile, skills={"Core": ["Kubernetes"]}))
        assert not result.passed
        assert any("Kubernetes" in i.detail for i in result.blocking)

    def test_langchain_cannot_be_printed(self, profile, gate):
        """The single most demanded skill she does not have. If the gate lets
        this through, the gate is useless."""
        result = gate.validate(base_resume(profile, skills={"AI": ["LangChain"]}))
        assert not result.passed


class TestEmploymentClaims:
    def test_invented_employer_is_blocked(self, profile, gate):
        fake = Experience(
            employer="Google", title="Senior AI Engineer", start="Jan 2020", end="Present"
        )
        result = gate.validate(base_resume(profile, experience=[fake]))
        assert not result.passed
        assert any(i.kind == "employment_mismatch" for i in result.blocking)

    def test_inflated_title_at_a_real_employer_is_blocked(self, profile, gate):
        real = profile.experience[0]
        inflated = real.model_copy(update={"title": "Senior AI Architect"})
        assert not gate.validate(base_resume(profile, experience=[inflated])).passed

    def test_changed_dates_are_blocked(self, profile, gate):
        real = profile.experience[0]
        stretched = real.model_copy(update={"start": "Jan 2018"})
        assert not gate.validate(base_resume(profile, experience=[stretched])).passed


class TestProjectClaims:
    def test_unknown_project_is_blocked(self, profile, gate):
        fake = ProjectEntry(name="Distributed Trading Engine", bullets=["Built it."])
        assert not gate.validate(base_resume(profile, projects=[fake])).passed

    def test_url_for_an_unshipped_project_is_blocked(self, profile, gate):
        """A GitHub link a recruiter clicks and gets a 404 on is worse than no
        link. The profile marks a project shipped only once it is actually
        pushed."""
        source = profile.projects[0]
        assert source.shipped is False
        result = gate.validate(base_resume(profile, projects=[source]))
        assert not result.passed
        assert any(i.kind == "unshipped_project_url" for i in result.blocking)

    def test_shipped_project_with_matching_url_passes(self, profile, gate):
        shipped = profile.projects[0].model_copy(update={"shipped": True})
        # the gate checks against the profile, so update the profile too
        gate.profile.projects[0] = shipped
        gate_2 = ValidationGate(gate.profile)
        assert gate_2.validate(base_resume(profile, projects=[shipped])).passed
        gate.profile.projects[0] = shipped.model_copy(update={"shipped": False})

    def test_mismatched_url_is_blocked(self, profile, gate):
        tampered = profile.projects[0].model_copy(
            update={"url": "https://github.com/someone-else/repo", "shipped": True}
        )
        assert not gate.validate(base_resume(profile, projects=[tampered])).passed


class TestTextAndMetrics:
    def test_placeholder_text_is_blocked(self, profile, gate):
        r = base_resume(profile, summary="Experienced engineer at [COMPANY] delivering TBD.")
        assert not gate.validate(r).passed

    def test_seniority_inflation_is_blocked(self, profile, gate):
        exp = profile.experience[0].model_copy(
            update={"bullets": ["Architected the platform end to end as a principal engineer."]}
        )
        result = gate.validate(base_resume(profile, experience=[exp]))
        assert not result.passed
        assert any(i.kind == "seniority_inflation" for i in result.blocking)

    def test_invented_metric_is_blocked(self, profile, gate):
        exp = profile.experience[0].model_copy(
            update={"bullets": ["Reduced latency by 47% across 900 microservices."]}
        )
        result = gate.validate(base_resume(profile, experience=[exp]))
        assert not result.passed
        assert any(i.kind == "unverified_metric" for i in result.blocking)

    def test_real_metrics_from_the_profile_pass(self, profile, gate):
        assert gate.validate(base_resume(profile)).passed

    def test_empty_headline_is_blocked(self, profile, gate):
        assert not gate.validate(base_resume(profile, headline="")).passed
