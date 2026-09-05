"""Deterministic job-match scoring.

Same input, same score, every time. The LLM never produces the number - it
reads the breakdown and writes the covering explanation. That separation is
what makes the score comparable across a hundred applications instead of
drifting with whatever the model felt like that day.

Hard rejects run first and short-circuit. A role that requires a qualification
you do not hold is not a low score, it is not a job - and pretending otherwise
wastes the only genuinely scarce resource in a job search, which is the
attention you can give each application.
"""

from __future__ import annotations

import re

from .models import (
    MAX_POINTS,
    Decision,
    JobAnalysis,
    JobPosting,
    Label,
    MasterProfile,
    ScoreBreakdown,
    SkillMatch,
    WorkMode,
)

AI_TERMS = {
    "genai",
    "generative ai",
    "llm",
    "llms",
    "agentic",
    "agent",
    "agents",
    "rag",
    "retrieval augmented generation",
    "mcp",
    "model context protocol",
    "langchain",
    "langgraph",
    "llamaindex",
    "crewai",
    "vector database",
    "vector db",
    "embeddings",
    "prompt engineering",
    "context engineering",
    "openai",
    "anthropic",
    "claude",
    "bedrock",
    "vertex ai",
    "azure openai",
    "fine-tuning",
    "guardrails",
    "evaluation",
    "semantic kernel",
    "hugging face",
    "pinecone",
    "pgvector",
    "chroma",
    "weaviate",
}
BACKEND_TERMS = {
    "java",
    "spring",
    "spring boot",
    "rest",
    "rest api",
    "microservices",
    "api",
    "sql",
    "oracle",
    "postgresql",
    "backend",
    "jax-rs",
    "jersey",
    "fastapi",
    "python",
    "ci/cd",
    "docker",
    "kubernetes",
    "maven",
    "junit",
}
SENIOR_TITLE = re.compile(r"\b(principal|staff|architect|head of|director|vp|manager)\b", re.I)

PREFERRED_LOCATIONS = {
    "remote": 5.0,
    "hyderabad": 5.0,
    "dubai": 5.0,
    "abu dhabi": 4.5,
    "uae": 4.5,
    "riyadh": 4.0,
    "saudi": 4.0,
    "qatar": 4.0,
    "doha": 4.0,
    "bangalore": 3.5,
    "bengaluru": 3.5,
    "chennai": 3.0,
    "pune": 3.0,
    "india": 3.0,
}

# Skills a backend engineer with this profile can realistically reach interview
# competence on within a few weeks of deliberate work.
LEARNABLE = {
    "langchain",
    "langgraph",
    "llamaindex",
    "crewai",
    "semantic kernel",
    "pinecone",
    "weaviate",
    "chroma",
    "pgvector",
    "fastapi",
    "docker",
    "prompt engineering",
    "rag",
    "vector database",
    "embeddings",
    "langsmith",
    "langfuse",
    "streamlit",
    "pytest",
    "terraform",
    "airflow",
    "kafka",
    "graphql",
    "redis",
    "mongodb",
}
# Skills that are years of work, not weeks. Naming them honestly is the point.
CRITICAL = {
    "kubernetes",
    "machine learning",
    "deep learning",
    "pytorch",
    "tensorflow",
    "mlops",
    "distributed systems",
    "data engineering",
    "spark",
    "scala",
    "go",
    "rust",
    "c++",
    ".net",
    "react",
    "angular",
    "system design at scale",
}


def _norm(term: str) -> str:
    return re.sub(r"\s+", " ", term.strip().lower())


class Scorer:
    def __init__(self, profile: MasterProfile):
        self.profile = profile
        self._printable = {_norm(s.name) for s in profile.printable_skills()}
        for s in profile.printable_skills():
            self._printable |= {_norm(a) for a in s.aliases}

    # ------------------------------------------------------------------
    def hard_rejects(self, job: JobPosting) -> list[str]:
        out: list[str] = []
        gap = job.min_years - self.profile.years_experience
        if gap > 3:
            out.append(
                f"Requires {job.min_years:.0f}+ years; profile has "
                f"{self.profile.years_experience:.1f} - a {gap:.1f} year gap."
            )
        for qual in job.mandatory_qualifications:
            if not self._holds(qual):
                out.append(f"Mandatory qualification not held: {qual}")
        if SENIOR_TITLE.search(job.role) and self.profile.years_experience < 8:
            out.append(f"Title '{job.role}' implies a seniority band above this profile.")
        missing_required = [
            s
            for s in job.required_skills
            if self._status(s)[0] == "missing" and _norm(s) in CRITICAL
        ]
        if len(missing_required) >= 3:
            out.append(
                "Three or more required skills are multi-year gaps, not learnable ones: "
                + ", ".join(missing_required[:5])
            )
        return out

    def _holds(self, term: str) -> bool:
        t = _norm(term)
        if any(t in c.lower() for c in self.profile.certifications):
            return True
        if any(t in e.lower() for e in self.profile.education):
            return True
        return t in self._printable

    def _status(self, term: str) -> tuple[str, Label, str]:
        skill = self.profile.skill(term)
        if skill and skill.printable:
            if skill.label is Label.VERIFIED:
                return "matched", skill.label, skill.evidence
            if skill.label is Label.PARTIALLY_VERIFIED:
                return "partial", skill.label, skill.evidence
            return "transferable", skill.label, skill.evidence
        if skill:  # present but LEARNING/UNSUPPORTED - honest, not printable
            return "missing", skill.label, skill.evidence
        t = _norm(term)
        # substring match against a printable skill, e.g. "REST APIs" vs "REST API"
        if any(t in p or p in t for p in self._printable if len(p) > 3):
            return "partial", Label.TRANSFERABLE, "partial term match"
        return "missing", Label.UNKNOWN, ""

    # ------------------------------------------------------------------
    def score(self, job: JobPosting) -> JobAnalysis:
        rejects = self.hard_rejects(job)
        matches: list[SkillMatch] = []
        for term in job.required_skills:
            status, label, note = self._status(term)
            matches.append(
                SkillMatch(skill=term, required=True, status=status, label=label, note=note)
            )
        for term in job.preferred_skills:
            status, label, note = self._status(term)
            matches.append(
                SkillMatch(skill=term, required=False, status=status, label=label, note=note)
            )

        b = ScoreBreakdown(
            technical=self._technical(matches),
            experience=self._experience(job),
            ai_relevance=self._term_overlap(job, AI_TERMS, MAX_POINTS["ai_relevance"]),
            backend_relevance=self._term_overlap(
                job, BACKEND_TERMS, MAX_POINTS["backend_relevance"]
            ),
            seniority=self._seniority(job),
            location=self._location(job),
            compensation=self._compensation(job),
            growth=self._growth(job),
            learnability=self._learnability(matches),
        )

        missing_required = [m.skill for m in matches if m.required and m.status == "missing"]
        learnable = [s for s in missing_required if _norm(s) in LEARNABLE]
        critical = [s for s in missing_required if _norm(s) in CRITICAL]

        total = b.total
        band = (
            "Excellent"
            if total >= 90
            else "Strong"
            if total >= 80
            else "Good"
            if total >= 70
            else "Possible"
            if total >= 60
            else "Low Priority"
        )

        if rejects:
            decision = Decision.DO_NOT_APPLY
        elif total >= 75 and len(critical) == 0:
            decision = Decision.APPLY
        elif total >= 60 and len(critical) <= 1:
            decision = Decision.MAYBE
        elif total >= 60:
            decision = Decision.MANUAL_REVIEW
        else:
            decision = Decision.DO_NOT_APPLY

        return JobAnalysis(
            job_id=job.job_id,
            company=job.company,
            role=job.role,
            score=total,
            breakdown=b,
            band=band,
            decision=decision,
            reasons=self._reasons(job, b, matches, learnable, critical),
            hard_rejects=rejects,
            matched=matches,
            missing_required=missing_required,
            learnable_gaps=learnable,
            critical_gaps=critical,
            ats_keyword_coverage=self._ats_coverage(job),
        )

    # -- components -----------------------------------------------------
    def _technical(self, matches: list[SkillMatch]) -> float:
        required = [m for m in matches if m.required]
        preferred = [m for m in matches if not m.required]
        if not required and not preferred:
            return MAX_POINTS["technical"] * 0.5
        weight = {"matched": 1.0, "partial": 0.6, "transferable": 0.4, "missing": 0.0}
        req_score = sum(weight[m.status] for m in required) / len(required) if required else 1.0
        pref_score = (
            sum(weight[m.status] for m in preferred) / len(preferred) if preferred else req_score
        )
        return round(MAX_POINTS["technical"] * (0.8 * req_score + 0.2 * pref_score), 2)

    def _experience(self, job: JobPosting) -> float:
        have, need = self.profile.years_experience, job.min_years
        if need <= 0:
            return MAX_POINTS["experience"] * 0.8
        ratio = have / need
        if ratio >= 1.0:
            return MAX_POINTS["experience"]
        return round(MAX_POINTS["experience"] * max(0.0, ratio**1.5), 2)

    def _term_overlap(self, job: JobPosting, vocab: set[str], cap: float) -> float:
        text = " ".join(
            [job.role, job.description, *job.required_skills, *job.preferred_skills]
        ).lower()
        present = {t for t in vocab if t in text}
        if not present:
            return 0.0
        covered = sum(
            1 for t in present if t in self._printable or any(t in p for p in self._printable)
        )
        return round(cap * min(1.0, 0.35 + 0.65 * covered / len(present)), 2)

    def _seniority(self, job: JobPosting) -> float:
        if SENIOR_TITLE.search(job.role):
            return 2.0
        if job.min_years and abs(job.min_years - self.profile.years_experience) <= 1.5:
            return MAX_POINTS["seniority"]
        if job.min_years and job.min_years <= self.profile.years_experience:
            return MAX_POINTS["seniority"] * 0.8
        return MAX_POINTS["seniority"] * 0.5

    def _location(self, job: JobPosting) -> float:
        blob = f"{job.location} {job.country}".lower()
        if job.work_mode in (WorkMode.REMOTE_WORLDWIDE, WorkMode.REMOTE_COUNTRY):
            return MAX_POINTS["location"]
        best = max((v for k, v in PREFERRED_LOCATIONS.items() if k in blob), default=1.0)
        return round(best, 2)

    @staticmethod
    def _compensation(job: JobPosting) -> float:
        # Undisclosed salary is the norm, not a negative signal. Neutral score.
        return MAX_POINTS["compensation"] * (0.9 if job.salary_text else 0.6)

    @staticmethod
    def _growth(job: JobPosting) -> float:
        text = f"{job.role} {job.description}".lower()
        signals = (
            "ai",
            "genai",
            "agentic",
            "llm",
            "platform",
            "greenfield",
            "architecture",
            "ownership",
        )
        hits = sum(1 for s in signals if s in text)
        return round(MAX_POINTS["growth"] * min(1.0, 0.4 + 0.15 * hits), 2)

    @staticmethod
    def _learnability(matches: list[SkillMatch]) -> float:
        missing = [m.skill for m in matches if m.required and m.status == "missing"]
        if not missing:
            return MAX_POINTS["learnability"]
        learnable = sum(1 for s in missing if _norm(s) in LEARNABLE)
        return round(MAX_POINTS["learnability"] * learnable / len(missing), 2)

    def _ats_coverage(self, job: JobPosting) -> float:
        terms = {_norm(t) for t in job.required_skills + job.preferred_skills if t.strip()}
        if not terms:
            return 0.0
        covered = sum(
            1
            for t in terms
            if t in self._printable or any(t in p or p in t for p in self._printable if len(p) > 3)
        )
        return round(100.0 * covered / len(terms), 1)

    def _reasons(self, job, b, matches, learnable, critical) -> list[str]:  # noqa: ANN001
        out = [
            f"Technical fit {b.technical:.1f}/{MAX_POINTS['technical']:.0f} across "
            f"{sum(1 for m in matches if m.required)} required and "
            f"{sum(1 for m in matches if not m.required)} preferred skills.",
            f"Experience {self.profile.years_experience:.1f} years against a stated "
            f"{job.min_years:.0f}+ requirement.",
            f"ATS keyword coverage {self._ats_coverage(job):.0f}% "
            "(an estimate of this profile's overlap with the posting's stated skills, "
            "not any employer's actual ATS score).",
        ]
        if learnable:
            out.append("Learnable gaps (weeks, not years): " + ", ".join(learnable))
        if critical:
            out.append("Critical gaps (multi-year, do not claim these): " + ", ".join(critical))
        return out
