from career_agent.models import Decision, JobPosting
from career_agent.scoring import MAX_POINTS, Scorer


class TestScoring:
    def test_score_is_deterministic(self, profile, ai_job):
        s = Scorer(profile)
        assert s.score(ai_job).model_dump(exclude={"analysed_at"}) == s.score(ai_job).model_dump(
            exclude={"analysed_at"}
        )

    def test_score_stays_within_bounds(self, profile, ai_job, impossible_job):
        for job in (ai_job, impossible_job):
            result = Scorer(profile).score(job)
            assert 0 <= result.score <= sum(MAX_POINTS.values()) == 100

    def test_well_matched_ai_role_scores_apply(self, profile, ai_job):
        result = Scorer(profile).score(ai_job)
        assert result.decision is Decision.APPLY
        assert result.score >= 75
        assert not result.hard_rejects

    def test_impossible_role_is_hard_rejected(self, profile, impossible_job):
        result = Scorer(profile).score(impossible_job)
        assert result.decision is Decision.DO_NOT_APPLY
        assert len(result.hard_rejects) >= 3

    def test_missing_phd_is_a_hard_reject(self, profile):
        job = JobPosting(
            company="X",
            role="AI Engineer",
            min_years=3,
            mandatory_qualifications=["PhD in Computer Science"],
        )
        assert any("PhD" in r for r in Scorer(profile).score(job).hard_rejects)

    def test_gaps_are_split_into_learnable_and_critical(self, profile):
        job = JobPosting(
            company="X",
            role="AI Engineer",
            min_years=3,
            required_skills=["Python", "LangGraph", "Kubernetes"],
        )
        result = Scorer(profile).score(job)
        assert "LangGraph" in result.learnable_gaps
        assert "Kubernetes" in result.critical_gaps

    def test_learning_labelled_skills_count_as_missing_not_matched(self, profile):
        """Spring Boot is on the profile at LEARNING. It must never be scored as a match."""
        job = JobPosting(
            company="X", role="Java Developer", min_years=3, required_skills=["Spring Boot"]
        )
        result = Scorer(profile).score(job)
        assert result.missing_required == ["Spring Boot"]

    def test_remote_scores_full_location_points(self, profile):
        from career_agent.models import WorkMode

        job = JobPosting(
            company="X",
            role="AI Engineer",
            work_mode=WorkMode.REMOTE_WORLDWIDE,
            location="Anywhere",
        )
        assert Scorer(profile).score(job).breakdown.location == MAX_POINTS["location"]

    def test_gulf_locations_score_well(self, profile):
        for city in ("Dubai", "Riyadh", "Abu Dhabi", "Doha"):
            job = JobPosting(company="X", role="AI Engineer", location=city)
            assert Scorer(profile).score(job).breakdown.location >= 4.0

    def test_undisclosed_salary_is_not_punished_hard(self, profile, ai_job):
        """Most postings do not disclose salary; treating that as a red flag
        would reject most of the market."""
        assert Scorer(profile).score(ai_job).breakdown.compensation >= 3.0

    def test_ats_coverage_is_a_percentage(self, profile, ai_job):
        assert 0 <= Scorer(profile).score(ai_job).ats_keyword_coverage <= 100

    def test_no_stated_skills_does_not_crash(self, profile):
        result = Scorer(profile).score(JobPosting(company="X", role="AI Engineer"))
        assert result.score > 0
