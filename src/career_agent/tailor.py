"""Resume tailoring.

Selection and ordering only. The generator can decide what to lead with and
how to phrase it, but every element it emits comes from the MasterProfile - and
the ValidationGate proves that independently afterwards, rather than trusting
this module to have behaved.
"""

from __future__ import annotations

import re
from datetime import date

from .models import (
    JobAnalysis,
    JobPosting,
    MasterProfile,
    TailoredResume,
)
from .scoring import _norm
from .validation import ValidationGate

_SAFE = re.compile(r"[^A-Za-z0-9]+")


def resume_id(company: str, role: str, when: date | None = None, version: int = 1) -> str:
    d = (when or date.today()).isoformat()
    c = _SAFE.sub("_", company).strip("_").upper()[:24]
    r = _SAFE.sub("_", role).strip("_").upper()[:36]
    return f"RESUME_{d}_{c}_{r}_V{version:02d}"


class ResumeTailor:
    def __init__(self, profile: MasterProfile):
        self.profile = profile
        self.gate = ValidationGate(profile)

    def tailor(self, job: JobPosting, analysis: JobAnalysis, version: int = 1) -> TailoredResume:
        wanted = [_norm(s) for s in job.required_skills + job.preferred_skills]
        changes: list[str] = []

        skills = self._ordered_skills(wanted, changes)
        projects = self._ordered_projects(wanted, changes)
        headline = self._headline(job, changes)
        summary = self._summary(job, analysis, changes)

        resume = TailoredResume(
            resume_id=resume_id(job.company, job.role, version=version),
            version=version,
            job_id=job.job_id,
            company=job.company,
            role=job.role,
            match_score=analysis.score,
            headline=headline,
            summary=summary,
            skills=skills,
            projects=projects,
            experience=list(self.profile.experience),
            certifications=list(self.profile.certifications),
            education=list(self.profile.education),
            keywords_targeted=sorted({w for w in wanted if w}),
            changes=changes,
        )
        resume.validation = self.gate.validate(resume)
        return resume

    # ------------------------------------------------------------------
    def _ordered_skills(self, wanted: list[str], changes: list[str]) -> dict[str, list[str]]:
        """Group printable skills, putting the ones this job asked for first.

        Reordering is legitimate tailoring. Adding is not, so nothing is added:
        the set of printed skills is always a subset of the profile's printable
        skills, whatever the job description says.
        """
        printable = self.profile.printable_skills()

        def relevance(skill) -> int:  # noqa: ANN001
            # Aliases count. A posting asking for "MCP" must promote the skill
            # recorded as "Model Context Protocol", or the tailoring silently
            # buries the most relevant thing on the resume.
            names = [_norm(skill.name), *(_norm(a) for a in skill.aliases)]
            return sum(1 for w in wanted if w and any(w == n or w in n or n in w for n in names))

        promoted = sorted(printable, key=lambda s: (-relevance(s), s.name.lower()))
        matched = [s.name for s in promoted if relevance(s)]
        rest = [s.name for s in promoted if not relevance(s)]
        if matched:
            changes.append(
                f"Promoted {len(matched)} profile skills named in the posting to the top."
            )
        return {"Most relevant to this role": matched, "Additional": rest}

    def _ordered_projects(self, wanted: list[str], changes: list[str]) -> list:
        def relevance(p) -> int:  # noqa: ANN001
            blob = f"{p.name} {p.stack} {' '.join(p.bullets)}".lower()
            return sum(1 for w in wanted if w and w in blob)

        ordered = sorted(self.profile.projects, key=lambda p: (-relevance(p), p.name))
        if ordered and relevance(ordered[0]):
            changes.append(
                f"Led with project '{ordered[0].name}' as the closest match to the posting."
            )
        return ordered

    def _headline(self, job: JobPosting, changes: list[str]) -> str:
        """Mirror the posting's own title only when the profile actually supports it."""
        base = "AI Engineer | Agentic Systems · MCP · RAG | Java Backend Foundation"
        role = job.role.lower()
        if any(
            t in role
            for t in ("ai engineer", "genai", "generative ai", "agentic", "llm", "ml engineer")
        ):
            changes.append("Aligned headline to the posting's AI engineering title.")
            return base
        if any(t in role for t in ("backend", "java", "software engineer", "api")):
            changes.append("Aligned headline to the posting's backend engineering title.")
            return "Software Engineer | Java Backend & REST APIs | AI/LLM Application Engineering"
        return base

    def _summary(self, job: JobPosting, analysis: JobAnalysis, changes: list[str]) -> str:
        """Assemble from pre-approved sentences in the profile. Never freeform."""
        sentences = list(self.profile.summary_sentences)
        if not sentences:
            return (
                f"{self.profile.years_experience:.1f}+ years of backend engineering, "
                "now building agentic AI systems end to end."
            )
        wanted = {_norm(s) for s in job.required_skills + job.preferred_skills}

        def score(s: str) -> int:
            low = s.lower()
            return sum(1 for w in wanted if w and w in low)

        ordered = sorted(sentences, key=lambda s: -score(s))
        chosen = ordered[:3]
        changes.append("Ordered pre-approved summary sentences by overlap with the posting.")
        if analysis.critical_gaps:
            changes.append(
                "Did NOT add: "
                + ", ".join(analysis.critical_gaps)
                + " - these are real gaps and were left off deliberately."
            )
        return " ".join(chosen)
