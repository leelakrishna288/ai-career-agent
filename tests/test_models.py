from career_agent.models import JobPosting, canonical_url, normalise_role


class TestDeduplication:
    def test_title_noise_is_normalised_away(self):
        assert normalise_role("Senior AI Engineer II") == normalise_role("AI Engineer")
        assert normalise_role("Sr. Java Developer (Remote)") == normalise_role("Java Developer")

    def test_tracking_parameters_are_stripped(self):
        assert canonical_url("https://X.co/j/1?utm_source=li&id=9#top") == "https://x.co/j/1?id=9"
        assert canonical_url("https://x.co/j/1/") == "https://x.co/j/1"

    def test_meaningful_parameters_are_kept(self):
        assert "id=9" in canonical_url("https://x.co/j?id=9&gclid=abc")
        assert "gclid" not in canonical_url("https://x.co/j?id=9&gclid=abc")

    def test_same_job_two_boards_gets_one_id(self):
        a = JobPosting(
            company="Acme", role="Senior AI Engineer II", url="https://x.co/1?utm_source=linkedin"
        )
        b = JobPosting(company="Acme", role="AI Engineer", url="https://x.co/1")
        assert a.job_id == b.job_id

    def test_different_jobs_get_different_ids(self):
        a = JobPosting(company="Acme", role="AI Engineer", url="https://x.co/1")
        b = JobPosting(company="Acme", role="Data Engineer", url="https://x.co/2")
        assert a.job_id != b.job_id

    def test_empty_company_or_role_is_rejected(self):
        import pytest

        with pytest.raises(ValueError):
            JobPosting(company="  ", role="AI Engineer")


class TestProfile:
    def test_learning_and_unsupported_skills_are_not_printable(self, profile):
        never = {s.name for s in profile.skills if not s.printable}
        assert {
            "Spring Boot",
            "LangChain",
            "Kubernetes",
            "Machine learning",
            "Microservices",
        } <= never

    def test_alias_lookup_works(self, profile):
        assert profile.skill("mcp") is not None
        assert profile.skill("retrieval augmented generation") is not None
        assert profile.skill("sso") is not None

    def test_claim_corpus_contains_every_metric_used_in_bullets(self, profile):
        corpus = profile.claim_corpus()
        for token in ("65+", "70+", "15 production", "121 controls"):
            assert token.split()[0].lower() in corpus
