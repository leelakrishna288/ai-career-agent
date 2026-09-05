"""Domain model.

The design rule this project exists to enforce: an LLM may decide *what to
emphasise*, but it may never decide *what is true*. Every fact that can appear
on a generated resume must already exist in the MasterProfile. The
ValidationGate proves it before anything is written.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Label(str, Enum):
    """Evidence label for every claim. UNKNOWN never silently becomes VERIFIED."""

    VERIFIED = "VERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    TRANSFERABLE = "TRANSFERABLE"
    LEARNING = "LEARNING"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


PRINTABLE_LABELS = {Label.VERIFIED, Label.PARTIALLY_VERIFIED, Label.TRANSFERABLE}


class WorkMode(str, Enum):
    REMOTE_WORLDWIDE = "remote_worldwide"
    REMOTE_COUNTRY = "remote_country"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class Decision(str, Enum):
    APPLY = "APPLY"
    MAYBE = "MAYBE"
    DO_NOT_APPLY = "DO_NOT_APPLY"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class Status(str, Enum):
    DISCOVERED = "DISCOVERED"
    ANALYZING = "ANALYZING"
    QUALIFIED = "QUALIFIED"
    REJECTED = "REJECTED"
    RESUME_PREPARED = "RESUME_PREPARED"
    READY = "READY"
    APPROVED = "APPROVED"
    SUBMITTED = "SUBMITTED"
    CONFIRMATION_RECEIVED = "CONFIRMATION_RECEIVED"
    RECRUITER_CONTACTED = "RECRUITER_CONTACTED"
    ASSESSMENT = "ASSESSMENT"
    INTERVIEW = "INTERVIEW"
    OFFER = "OFFER"
    WITHDRAWN = "WITHDRAWN"
    BLOCKED = "BLOCKED"
    DUPLICATE = "DUPLICATE"
    EXPIRED = "EXPIRED"


class Skill(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    label: Label = Label.UNKNOWN
    years: float = 0.0
    evidence: str = ""
    aliases: tuple[str, ...] = ()

    @property
    def printable(self) -> bool:
        return self.label in PRINTABLE_LABELS

    def matches(self, term: str) -> bool:
        t = term.strip().lower()
        return t == self.name.lower() or t in {a.lower() for a in self.aliases}


class Experience(BaseModel):
    employer: str
    title: str
    start: str
    end: str = "Present"
    location: str = ""
    bullets: list[str] = Field(default_factory=list)
    label: Label = Label.VERIFIED


class ProjectEntry(BaseModel):
    name: str
    url: str = ""
    stack: str = ""
    bullets: list[str] = Field(default_factory=list)
    label: Label = Label.VERIFIED
    shipped: bool = False


class MasterProfile(BaseModel):
    """The single source of truth. Nothing may be printed that is not here."""

    name: str
    email: str
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    years_experience: float = 0.0
    skills: list[Skill] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[ProjectEntry] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)
    summary_sentences: list[str] = Field(default_factory=list)
    notice_period_days: int = 0
    current_ctc_lpa: float | None = None
    expected_ctc_lpa: float | None = None

    def skill(self, term: str) -> Skill | None:
        for s in self.skills:
            if s.matches(term):
                return s
        return None

    def printable_skills(self) -> list[Skill]:
        return [s for s in self.skills if s.printable]

    def claim_corpus(self) -> str:
        """Everything the profile asserts, flattened, for the validation gate."""
        parts = [self.name, self.email, self.phone, self.location, self.linkedin, self.github]
        parts += [s.name for s in self.skills] + [a for s in self.skills for a in s.aliases]
        for e in self.experience:
            parts += [e.employer, e.title, e.start, e.end, e.location, *e.bullets]
        for p in self.projects:
            parts += [p.name, p.url, p.stack, *p.bullets]
        parts += self.certifications + self.education + self.summary_sentences
        return " \n".join(x for x in parts if x).lower()


class JobPosting(BaseModel):
    job_id: str = ""
    company: str
    role: str
    location: str = ""
    country: str = ""
    work_mode: WorkMode = WorkMode.UNKNOWN
    url: str = ""
    source: str = ""
    posted: str = ""
    min_years: float = 0.0
    max_years: float = 0.0
    salary_text: str = ""
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    mandatory_qualifications: list[str] = Field(default_factory=list)
    description: str = ""

    @field_validator("company", "role")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("company and role must not be empty")
        return v.strip()

    def model_post_init(self, __context) -> None:  # noqa: ANN001
        if not self.job_id:
            object.__setattr__(
                self,
                "job_id",
                "JOB-"
                + hashlib.sha256(
                    f"{self.company}|{normalise_role(self.role)}|{canonical_url(self.url)}".encode()
                ).hexdigest()[:12],
            )


_ROLE_NOISE = re.compile(
    r"\b(senior|sr|junior|jr|lead|staff|principal|i{1,3}|iv|v|1|2|3|4|5|"
    r"full[- ]?time|contract|remote|hybrid|onsite|urgent|hiring|immediate)\b",
    re.I,
)


def normalise_role(role: str) -> str:
    """Collapse title noise so the same job posted twice deduplicates."""
    cleaned = _ROLE_NOISE.sub(" ", role.lower())
    cleaned = re.sub(r"[^a-z0-9+#. ]+", " ", cleaned)
    # Drop tokens with no alphanumeric content - "Sr." leaves a bare "." behind
    # after noise removal, which would stop two spellings of the same job from
    # deduplicating.
    return " ".join(t for t in cleaned.split() if any(c.isalnum() for c in t))


def canonical_url(url: str) -> str:
    """Strip tracking parameters and fragments so the same posting shared two
    ways is recognised as one job."""
    if not url:
        return ""
    url = url.split("#", 1)[0].strip().rstrip("/")
    base, sep, query = url.partition("?")
    if not sep:
        return base.lower()
    keep = [
        p
        for p in query.split("&")
        if p
        and not p.split("=", 1)[0]
        .lower()
        .startswith(("utm_", "gh_src", "src", "ref", "trk", "fbclid", "gclid"))
    ]
    return (base + ("?" + "&".join(sorted(keep)) if keep else "")).lower()


class SkillMatch(BaseModel):
    skill: str
    required: bool
    status: str  # matched | partial | transferable | missing
    label: Label = Label.UNKNOWN
    note: str = ""


class ScoreBreakdown(BaseModel):
    technical: float = 0.0
    experience: float = 0.0
    ai_relevance: float = 0.0
    backend_relevance: float = 0.0
    seniority: float = 0.0
    location: float = 0.0
    compensation: float = 0.0
    growth: float = 0.0
    learnability: float = 0.0

    @property
    def total(self) -> float:
        return round(
            self.technical
            + self.experience
            + self.ai_relevance
            + self.backend_relevance
            + self.seniority
            + self.location
            + self.compensation
            + self.growth
            + self.learnability,
            1,
        )


MAX_POINTS = {
    "technical": 25.0,
    "experience": 20.0,
    "ai_relevance": 15.0,
    "backend_relevance": 10.0,
    "seniority": 10.0,
    "location": 5.0,
    "compensation": 5.0,
    "growth": 5.0,
    "learnability": 5.0,
}


class JobAnalysis(BaseModel):
    job_id: str
    company: str
    role: str
    score: float
    breakdown: ScoreBreakdown
    band: str
    decision: Decision
    reasons: list[str] = Field(default_factory=list)
    hard_rejects: list[str] = Field(default_factory=list)
    matched: list[SkillMatch] = Field(default_factory=list)
    missing_required: list[str] = Field(default_factory=list)
    learnable_gaps: list[str] = Field(default_factory=list)
    critical_gaps: list[str] = Field(default_factory=list)
    ats_keyword_coverage: float = 0.0
    analysed_at: datetime = Field(default_factory=utcnow)


class ValidationIssue(BaseModel):
    severity: str  # blocking | warning
    kind: str
    detail: str


class ValidationResult(BaseModel):
    passed: bool
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def blocking(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "blocking"]


class TailoredResume(BaseModel):
    resume_id: str
    version: int = 1
    created: date = Field(default_factory=lambda: utcnow().date())
    job_id: str = ""
    company: str = ""
    role: str = ""
    match_score: float = 0.0
    headline: str = ""
    summary: str = ""
    skills: dict[str, list[str]] = Field(default_factory=dict)
    projects: list[ProjectEntry] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)
    keywords_targeted: list[str] = Field(default_factory=list)
    changes: list[str] = Field(default_factory=list)
    validation: ValidationResult | None = None

    @property
    def is_final(self) -> bool:
        return bool(self.validation and self.validation.passed)


class Application(BaseModel):
    application_id: str
    job_id: str
    company: str
    role: str
    location: str = ""
    url: str = ""
    source: str = ""
    date_found: str = ""
    match_score: float = 0.0
    decision: Decision = Decision.MAYBE
    status: Status = Status.DISCOVERED
    resume_id: str = ""
    notes: str = ""
    history: list[str] = Field(default_factory=list)
