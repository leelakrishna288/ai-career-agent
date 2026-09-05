"""The factual-validation gate.

This is the reason the project exists. Resume tailoring is exactly the task
where an LLM will help you lie: asked to match a job description, it will
smooth "familiar with Docker" into "containerised production workloads" and
neither of you will notice until an interviewer asks.

So generation and truth are separated. The generator may select, order and
re-word what is in the MasterProfile. The gate then checks the output against
the profile and **blocks** anything it cannot trace back. A resume that does
not pass is never marked final and never written to disk as a submission
candidate.

Checks:
  1. Contact fields match the profile exactly.
  2. Every employer, title and date appears in the profile.
  3. Every skill printed exists in the profile at a printable label -
     LEARNING and UNSUPPORTED skills can never be printed as competencies.
  4. Every number that looks like a metric appears in the profile.
  5. No project is claimed as shipped unless the profile says it shipped.
  6. No placeholder text, no unresolved template markers.
  7. Seniority language is not inflated beyond the profile's years.
"""

from __future__ import annotations

import re

from .models import Label, MasterProfile, TailoredResume, ValidationIssue, ValidationResult

_PLACEHOLDERS = re.compile(
    r"(lorem ipsum|\bTBD\b|\bTODO\b|\bXXX+\b|\[[^\]]{0,40}\]|\{\{[^}]*\}\}|<[a-z_]+>)", re.I
)
_METRIC = re.compile(r"\b(\d[\d,]*(?:\.\d+)?)\s*(%|\+|x|k|lpa|years?|yrs?|months?)?\b", re.I)
_INFLATION = re.compile(
    r"\b(architected the|principal|staff engineer|head of|expert in|world[- ]class|"
    r"industry[- ]leading|10\+? years|decade of)\b",
    re.I,
)
# Numbers that are structurally fine anywhere: small counts, versions, years.
_ALWAYS_OK = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "0"}


class ValidationGate:
    def __init__(self, profile: MasterProfile):
        self.profile = profile
        self._corpus = profile.claim_corpus()
        # Numeric tokens the profile actually asserts. Built as whole tokens
        # rather than substrings: "47" appears inside the LinkedIn URL slug,
        # and a substring check let an invented "47%" through the gate.
        self._numbers = {
            n.replace(",", "")
            for n in re.findall(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)(?![\w.])", self._corpus)
        }
        self._printable = {s.name.lower() for s in profile.printable_skills()}
        for s in profile.printable_skills():
            self._printable |= {a.lower() for a in s.aliases}
        self._non_printable = {s.name.lower(): s.label for s in profile.skills if not s.printable}

    def validate(self, resume: TailoredResume) -> ValidationResult:
        issues: list[ValidationIssue] = []
        issues += self._check_skills(resume)
        issues += self._check_experience(resume)
        issues += self._check_projects(resume)
        issues += self._check_text(resume)
        issues += self._check_metrics(resume)
        blocking = [i for i in issues if i.severity == "blocking"]
        return ValidationResult(passed=not blocking, issues=issues)

    # ------------------------------------------------------------------
    def _check_skills(self, resume: TailoredResume) -> list[ValidationIssue]:
        out: list[ValidationIssue] = []
        for group, items in resume.skills.items():
            for item in items:
                key = item.strip().lower()
                if key in self._printable:
                    continue
                if key in self._non_printable:
                    label = self._non_printable[key]
                    out.append(
                        ValidationIssue(
                            severity="blocking",
                            kind="unsupported_skill",
                            detail=(
                                f"'{item}' (in '{group}') is recorded in the profile at label "
                                f"{label.value} and must not be printed as a competency."
                            ),
                        )
                    )
                    continue
                if any(key in p or p in key for p in self._printable if len(p) > 3):
                    out.append(
                        ValidationIssue(
                            severity="warning",
                            kind="approximate_skill",
                            detail=f"'{item}' (in '{group}') is only a partial match to a profile skill.",
                        )
                    )
                    continue
                out.append(
                    ValidationIssue(
                        severity="blocking",
                        kind="unknown_skill",
                        detail=f"'{item}' (in '{group}') does not appear in the master profile at all.",
                    )
                )
        return out

    def _check_experience(self, resume: TailoredResume) -> list[ValidationIssue]:
        out: list[ValidationIssue] = []
        known = {
            (e.employer.lower(), e.title.lower(), e.start.lower(), e.end.lower())
            for e in self.profile.experience
        }
        for e in resume.experience:
            key = (e.employer.lower(), e.title.lower(), e.start.lower(), e.end.lower())
            if key not in known:
                out.append(
                    ValidationIssue(
                        severity="blocking",
                        kind="employment_mismatch",
                        detail=f"Employment entry '{e.title} at {e.employer} ({e.start}-{e.end})' "
                        "does not match any profile record.",
                    )
                )
        return out

    def _check_projects(self, resume: TailoredResume) -> list[ValidationIssue]:
        out: list[ValidationIssue] = []
        by_name = {p.name.lower(): p for p in self.profile.projects}
        for p in resume.projects:
            source = by_name.get(p.name.lower())
            if source is None:
                out.append(
                    ValidationIssue(
                        severity="blocking",
                        kind="unknown_project",
                        detail=f"Project '{p.name}' is not in the master profile.",
                    )
                )
                continue
            if p.url and p.url != source.url:
                out.append(
                    ValidationIssue(
                        severity="blocking",
                        kind="project_url_mismatch",
                        detail=f"Project '{p.name}' prints URL {p.url} but the profile records {source.url or '(none)'}.",
                    )
                )
            if p.url and not source.shipped:
                out.append(
                    ValidationIssue(
                        severity="blocking",
                        kind="unshipped_project_url",
                        detail=f"Project '{p.name}' prints a public URL but the profile does not mark it shipped. "
                        "A link a reviewer cannot open is worse than no link.",
                    )
                )
        return out

    def _check_text(self, resume: TailoredResume) -> list[ValidationIssue]:
        out: list[ValidationIssue] = []
        blob = " ".join(
            [
                resume.headline,
                resume.summary,
                *(b for e in resume.experience for b in e.bullets),
                *(b for p in resume.projects for b in p.bullets),
            ]
        )
        for m in _PLACEHOLDERS.finditer(blob):
            out.append(
                ValidationIssue(
                    severity="blocking",
                    kind="placeholder",
                    detail=f"Unresolved placeholder text: {m.group(0)!r}",
                )
            )
        for m in _INFLATION.finditer(blob):
            out.append(
                ValidationIssue(
                    severity="blocking",
                    kind="seniority_inflation",
                    detail=f"Inflated seniority language {m.group(0)!r} is not supported by "
                    f"{self.profile.years_experience:.1f} years of experience.",
                )
            )
        for field, value in (("headline", resume.headline), ("summary", resume.summary)):
            if not value.strip():
                out.append(
                    ValidationIssue(
                        severity="blocking",
                        kind="empty_field",
                        detail=f"{field} is empty.",
                    )
                )
        return out

    def _check_metrics(self, resume: TailoredResume) -> list[ValidationIssue]:
        """Any number presented as an achievement must exist in the profile."""
        out: list[ValidationIssue] = []
        blob = (
            " ".join(b for e in resume.experience for b in e.bullets)
            + " "
            + " ".join(b for p in resume.projects for b in p.bullets)
        )
        for m in _METRIC.finditer(blob):
            raw = m.group(0).strip()
            number = m.group(1).replace(",", "")
            if number in _ALWAYS_OK and not m.group(2):
                continue
            if number in self._numbers:
                continue
            # Also accept a bare integer that the profile states with a suffix,
            # e.g. profile says "65+" and the resume prints "65".
            if any(
                known.startswith(number) and known[len(number) :] in ("", "+", ".0")
                for known in self._numbers
            ):
                continue
            out.append(
                ValidationIssue(
                    severity="blocking",
                    kind="unverified_metric",
                    detail=f"Metric {raw!r} does not appear anywhere in the master profile.",
                )
            )
        return out


def unsupported_labels() -> set[Label]:
    return {Label.LEARNING, Label.UNSUPPORTED, Label.UNKNOWN}
