"""ESTIMATED ATS compatibility (SYSTEM_SPEC section 7).

This is our own deterministic estimate of how well a tailored resume lines up
with a posting. It is NOT the score any employer's applicant tracking system
produces, and every report labels it "ESTIMATED".

Points (total 100):
  required-skill coverage 35 · preferred-skill coverage 10 · title alignment 15
  experience alignment 15 · responsibility alignment 10 · education/cert 5
  parse/format risk 10

The estimate reads the resume that will actually be sent. Coverage can only go
up by printing skills the profile already supports - the ValidationGate blocks
anything else - so a low number is a signal to skip or review the job, never a
reason to add claims.
"""

from __future__ import annotations

import re

from .models import ATSEstimate, JobPosting, MasterProfile, TailoredResume
from .scoring import _contains_term, _norm

# Below this many named skills the keyword denominator is too small to measure: a JD
# naming two skills scores 100 when both match. Threshold and bands mirror
# scripts/ats_confidence.py, which found four such artefacts in the live tracker.
MIN_JD_SKILLS = 8

POINTS = {
    "required_skills": 35.0,
    "preferred_skills": 10.0,
    "title": 15.0,
    "experience": 15.0,
    "responsibilities": 10.0,
    "education": 5.0,
    "format": 10.0,
}

AI_TITLE = re.compile(r"(?i)\b(ai|genai|gen ai|generative|llm|agentic|agent|ml|machine learning)\b")
BACKEND_TITLE = re.compile(
    r"(?i)\b(backend|back[- ]end|java|software|sde|developer|api|platform|integration|engineer)\b"
)
_ADVANCED_DEGREE = re.compile(r"(?i)\b(master'?s?|m\.?tech|m\.?e\.?|ph\.?d|mba|m\.?s\.?)\b")
_WORD = re.compile(r"[a-z][a-z0-9+#./-]{3,}")
_STOP = {
    "with",
    "that",
    "this",
    "will",
    "your",
    "have",
    "from",
    "they",
    "their",
    "about",
    "work",
    "team",
    "teams",
    "what",
    "which",
    "into",
    "role",
    "within",
    "across",
    "using",
    "able",
    "strong",
    "experience",
    "years",
    "including",
    "other",
    "such",
    "must",
    "should",
    "also",
    "more",
    "well",
    "good",
    "great",
    "join",
    "company",
    "help",
    "you'll",
    "we're",
    "based",
    "opportunity",
    "candidates",
    "candidate",
    "equal",
    "employer",
    "benefits",
    "apply",
    "please",
    "status",
    "applicants",
    "location",
    "office",
    "working",
    "ability",
    "skills",
    "knowledge",
    "understanding",
    "people",
    "every",
    "where",
    "while",
    "these",
    "those",
    "being",
    "there",
    "would",
    "could",
    "like",
    "make",
    "want",
    "need",
    "part",
    "both",
}


def resume_text(resume: TailoredResume) -> str:
    parts = [resume.headline, resume.summary]
    parts += [s for items in resume.skills.values() for s in items]
    for e in resume.experience:
        parts += [e.title, e.employer, *e.bullets]
    for p in resume.projects:
        parts += [p.name, p.stack, *p.bullets]
    parts += resume.certifications + resume.education
    return "\n".join(parts).lower()


class ATSEstimator:
    def __init__(self, profile: MasterProfile):
        self.profile = profile

    def _covered(self, term: str, text: str, printed: set[str]) -> bool:
        t = _norm(term)
        if not t:
            return True
        if _contains_term(text, t):
            return True
        skill = self.profile.skill(t)
        return bool(skill and skill.printable and skill.name.lower() in printed)

    def estimate(self, job: JobPosting, resume: TailoredResume) -> ATSEstimate:
        text = resume_text(resume)
        printed = {s.lower() for items in resume.skills.values() for s in items}
        notes: list[str] = []
        comp: dict[str, float] = {}

        req = [t for t in job.required_skills if t.strip()]
        pref = [t for t in job.preferred_skills if t.strip()]
        miss_req = [t for t in req if not self._covered(t, text, printed)]
        miss_pref = [t for t in pref if not self._covered(t, text, printed)]
        if req:
            comp["required_skills"] = (
                POINTS["required_skills"] * (len(req) - len(miss_req)) / len(req)
            )
        else:
            comp["required_skills"] = POINTS["required_skills"] * 0.7
            notes.append(
                "No named required skills in the posting; required-skill points set to 70%."
            )
        if pref:
            comp["preferred_skills"] = (
                POINTS["preferred_skills"] * (len(pref) - len(miss_pref)) / len(pref)
            )
        else:
            # Without a preferred list, preferred points follow required coverage.
            comp["preferred_skills"] = POINTS["preferred_skills"] * (
                comp["required_skills"] / POINTS["required_skills"]
            )

        comp["title"] = self._title(job.role, resume.headline)
        comp["experience"] = self._experience(job, notes)
        comp["responsibilities"] = self._responsibilities(job.description, text)
        comp["education"] = self._education(job)
        comp["format"] = POINTS["format"]
        notes.append(
            "Format points assume the generated single-column DOCX (no tables, images or columns)."
        )
        named = len(req) + len(pref)
        if named < MIN_JD_SKILLS:
            confidence = "LOW"
            notes.append(
                f"Only {named} skills were named in the posting (minimum {MIN_JD_SKILLS}); "
                "the denominator is too small to measure, so this estimate is not a "
                "measurement and the row is excluded from the auto-submit rule."
            )
        elif named < MIN_JD_SKILLS * 2:
            confidence = "MEDIUM"
        else:
            confidence = "HIGH"
        total = round(sum(comp.values()), 1)
        return ATSEstimate(
            total=total,
            components={k: round(v, 1) for k, v in comp.items()},
            confidence=confidence,
            missing_required=miss_req,
            missing_preferred=miss_pref,
            notes=notes,
        )

    @staticmethod
    def _title(role: str, headline: str) -> float:
        role_ai = bool(AI_TITLE.search(role))
        role_be = bool(BACKEND_TITLE.search(role))
        head_ai = bool(AI_TITLE.search(headline)) or "genai" in headline.lower()
        head_be = bool(BACKEND_TITLE.search(headline))
        if (role_ai and head_ai) or (role_be and not role_ai and head_be):
            return POINTS["title"]
        if role_ai or role_be:
            return POINTS["title"] * 0.6
        return POINTS["title"] * 0.3

    def _experience(self, job: JobPosting, notes: list[str]) -> float:
        have, need = self.profile.years_experience, job.min_years
        if need <= 0:
            notes.append("Posting states no minimum years; experience points set to 80%.")
            return POINTS["experience"] * 0.8
        if have >= need:
            return POINTS["experience"]
        gap = need - have
        if gap <= 1:
            return POINTS["experience"] * 0.6
        if gap <= 2:
            return POINTS["experience"] * 0.25
        return 0.0

    @staticmethod
    def _responsibilities(description: str, text: str) -> float:
        words = {w for w in _WORD.findall(description.lower()) if w not in _STOP}
        if not words:
            return POINTS["responsibilities"] * 0.7
        hits = sum(1 for w in words if w in text)
        # A resume never echoes most of a JD; 40% overlap already reads as aligned.
        return POINTS["responsibilities"] * min(1.0, (hits / len(words)) / 0.40)

    def _education(self, job: JobPosting) -> float:
        for q in job.mandatory_qualifications:
            if _ADVANCED_DEGREE.search(q):
                return 0.0
        return POINTS["education"]
