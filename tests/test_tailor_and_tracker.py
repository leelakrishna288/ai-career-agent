from pathlib import Path

from career_agent.models import Decision, JobPosting, Status
from career_agent.scoring import Scorer
from career_agent.tailor import ResumeTailor, resume_id
from career_agent.tracker import Tracker


class TestTailoring:
    def _tailored(self, profile, job):
        return ResumeTailor(profile).tailor(job, Scorer(profile).score(job))

    def test_only_profile_skills_are_ever_printed(self, profile, ai_job):
        resume = self._tailored(profile, ai_job)
        printable = {s.name for s in profile.printable_skills()}
        printed = {s for group in resume.skills.values() for s in group}
        assert printed <= printable

    def test_no_learning_labelled_skill_leaks_through(self, profile, ai_job):
        printed = {
            s.lower() for group in self._tailored(profile, ai_job).skills.values() for s in group
        }
        assert "spring boot" not in printed
        assert "langchain" not in printed
        assert "kubernetes" not in printed

    def test_requested_skills_are_promoted_not_invented(self, profile, ai_job):
        """The posting asks for Python, Java, MCP, REST API and AWS. All five
        must be promoted, MCP included - it is stored under its full name
        'Model Context Protocol' with 'mcp' as an alias, so alias-blind
        promotion would bury the single most relevant skill on the resume."""
        resume = self._tailored(profile, ai_job)
        top = resume.skills["Most relevant to this role"]
        assert {"Python", "Java", "REST API", "AWS", "Model Context Protocol"} <= set(top)
        # and nothing was invented to satisfy the posting
        assert set(top) <= {s.name for s in profile.printable_skills()}

    def test_headline_mirrors_an_ai_posting(self, profile):
        job = JobPosting(company="X", role="Agentic AI Engineer", min_years=3)
        assert "AI Engineer" in self._tailored(profile, job).headline

    def test_headline_mirrors_a_backend_posting(self, profile):
        job = JobPosting(company="X", role="Java Backend Engineer", min_years=3)
        assert "Java Backend" in self._tailored(profile, job).headline

    def test_changes_are_recorded_for_review(self, profile, ai_job):
        assert self._tailored(profile, ai_job).changes

    def test_critical_gaps_are_recorded_as_deliberately_omitted(self, profile):
        job = JobPosting(
            company="X", role="AI Engineer", min_years=3, required_skills=["Python", "Kubernetes"]
        )
        resume = self._tailored(profile, job)
        assert any("Did NOT add" in c for c in resume.changes)

    def test_validation_runs_automatically(self, profile, ai_job):
        assert self._tailored(profile, ai_job).validation is not None

    def test_unshipped_project_urls_block_finalisation(self, profile, ai_job):
        """Until the repos are actually pushed, the gate refuses to mark the
        resume final. This is the behaviour, not a bug."""
        resume = self._tailored(profile, ai_job)
        assert not resume.is_final
        assert any(i.kind == "unshipped_project_url" for i in resume.validation.blocking)

    def test_resume_id_is_stable_and_filesystem_safe(self):
        rid = resume_id("JPMorgan Chase & Co.", "Software Engineer III - Java/Python")
        assert " " not in rid and "/" not in rid and "&" not in rid
        assert rid.startswith("RESUME_")


class TestTracker:
    def test_duplicate_jobs_do_not_create_two_rows(self, tmp_path):
        t = Tracker(tmp_path / "t.jsonl")
        a, created_a = t.add(
            JobPosting(
                company="Acme", role="Senior AI Engineer", url="https://x.co/1?utm_source=li"
            )
        )
        b, created_b = t.add(JobPosting(company="Acme", role="AI Engineer", url="https://x.co/1"))
        assert created_a and not created_b
        assert a.application_id == b.application_id
        assert len(t.all()) == 1

    def test_history_is_appended_never_overwritten(self, tmp_path):
        t = Tracker(tmp_path / "t.jsonl")
        app, _ = t.add(JobPosting(company="Acme", role="AI Engineer"))
        t.update_status(app.application_id, Status.QUALIFIED)
        t.update_status(app.application_id, Status.SUBMITTED, "applied via careers page")
        history = t.all()[0].history
        assert len(history) == 3
        assert "DISCOVERED -> QUALIFIED" in history[1]
        assert "applied via careers page" in history[2]

    def test_state_survives_a_reload(self, tmp_path):
        path = tmp_path / "t.jsonl"
        t = Tracker(path)
        app, _ = t.add(JobPosting(company="Acme", role="AI Engineer"))
        t.update_status(app.application_id, Status.INTERVIEW)
        reloaded = Tracker(path)
        assert reloaded.all()[0].status is Status.INTERVIEW
        assert len(reloaded.all()) == 1

    def test_unknown_application_raises(self, tmp_path):
        import pytest

        with pytest.raises(KeyError):
            Tracker(tmp_path / "t.jsonl").update_status("APP-9999", Status.OFFER)

    def test_pipeline_counts_by_status(self, tmp_path):
        t = Tracker(tmp_path / "t.jsonl")
        for i in range(3):
            t.add(JobPosting(company=f"Co{i}", role="AI Engineer", url=f"https://x.co/{i}"))
        a = t.all()[0]
        t.update_status(a.application_id, Status.SUBMITTED)
        assert t.pipeline() == {"DISCOVERED": 2, "SUBMITTED": 1}

    def test_csv_export_has_a_header_and_a_row_per_application(self, tmp_path):
        t = Tracker(tmp_path / "t.jsonl")
        t.add(JobPosting(company="Acme", role="AI Engineer"), 82.0, Decision.APPLY)
        csv_path = t.export_csv(tmp_path / "out.csv")
        lines = Path(csv_path).read_text().strip().splitlines()
        assert len(lines) == 2
        assert lines[0].startswith("application_id,job_id,company,role")
