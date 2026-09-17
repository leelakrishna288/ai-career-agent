"""Discovery pipeline: fetch -> normalise -> gate -> dedup -> score -> record."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, Field

from ..models import Decision, JobAnalysis, MasterProfile, normalise_role, same_posting
from ..scoring import AI_TERMS, Scorer
from .extract import Extracted, to_job_posting
from .filters import compile_any, reachable, title_relevant
from .http import JsonClient
from .locations import PRACTICE, LocationTiers
from .sources import AGGREGATORS, Board, RawPosting, fetch_board

log = logging.getLogger(__name__)


class BoardConfig(BaseModel):
    platform: str
    token: str
    company: str


class DiscoveryConfig(BaseModel):
    boards: list[BoardConfig] = Field(default_factory=list)
    include_titles: list[str] = Field(default_factory=list)
    exclude_titles: list[str] = Field(default_factory=list)
    allowed_countries: list[str] = Field(
        default_factory=lambda: ["India", "UAE", "Qatar", "Saudi Arabia"]
    )
    max_posting_age_days: int = 45
    max_new_per_run: int = 40
    max_new_per_company: int = 6
    # Leela is looking for a permanent role; gig, contract and part-time
    # listings are skipped before scoring.
    exclude_employment_types: list[str] = Field(
        default_factory=lambda: [
            "contract",
            "contractor",
            "freelance",
            "part time",
            "part-time",
            "parttime",
            "intern",
            "internship",
            "temporary",
        ]
    )
    target_locations: list[str] = Field(default_factory=list)
    practice_locations: list[str] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path | str) -> DiscoveryConfig:
        return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


@dataclass
class ExistingKey:
    company: str
    normalized_role: str
    url: str
    external_id: str = ""


@dataclass
class NewRecord:
    ex: Extracted
    raw: RawPosting
    analysis: JobAnalysis
    status: str
    variant: str
    notes: str
    purpose: str = ""


class TrackerSink(Protocol):
    def existing_keys(self) -> list[ExistingKey]: ...

    def add(self, record: NewRecord) -> str: ...


@dataclass
class RunReport:
    run_date: str
    boards_ok: int = 0
    boards_failed: list[str] = field(default_factory=list)
    fetched: int = 0
    irrelevant_title: int = 0
    too_old: int = 0
    unreachable: int = 0
    duplicates: int = 0
    not_permanent: int = 0
    capped: int = 0
    # (company, role, score, decision, url, work mode, purpose)
    recorded: list[tuple[str, str, float, str, str, str, str]] = field(default_factory=list)
    write_errors: list[str] = field(default_factory=list)

    def counts(self, decision: str) -> int:
        return sum(1 for r in self.recorded if r[3] == decision)

    @property
    def ok(self) -> bool:
        return not self.write_errors and self.boards_ok > 0


def _company_key(name: str) -> str:
    return "".join(c for c in name.lower() if c.isalnum())[:12]


def _is_aggregator_url(url: str) -> bool:
    u = url.lower()
    return any(host in u for host in ("himalayas.app", "remotive.com"))


def is_duplicate(ex: Extracted, raw: RawPosting, existing: list[ExistingKey]) -> bool:
    role = normalise_role(ex.job.role)
    comp = _company_key(ex.job.company)
    for k in existing:
        # A job-board copy of an employer posting has a different URL, so the
        # same company + role is enough when either side came from a board.
        if (
            _company_key(k.company) == comp
            and k.normalized_role == role
            and (_is_aggregator_url(k.url) or _is_aggregator_url(raw.url))
        ):
            return True
        if k.external_id and k.external_id == raw.external_id:
            return True
        if k.url and raw.url and k.url.rstrip("/").lower() == raw.url.rstrip("/").lower():
            return True
        if (
            _company_key(k.company) == comp
            and k.normalized_role == role
            and (not k.url or not raw.url or same_posting(k.url, raw.url))
        ):
            return True
    return False


def choose_variant(ex: Extracted) -> str:
    text = f"{ex.job.role} {ex.job.description}".lower()
    return (
        "B-AI+Java"
        if any(
            t in text for t in ("genai", "llm", "agentic", "generative ai", " ai ", "rag", "mcp")
        )
        or any(t in AI_TERMS for t in ex.job.required_skills)
        else "C-JavaBackend"
    )


def status_for(decision: Decision) -> str:
    return {
        Decision.APPLY: "MATCHED",
        Decision.MAYBE: "MATCHED",
        Decision.MANUAL_REVIEW: "ANALYZING",
        Decision.DO_NOT_APPLY: "REJECTED",
    }[decision]


def run_discovery(
    config: DiscoveryConfig,
    profile: MasterProfile,
    client: JsonClient,
    sink: TrackerSink,
    today: date | None = None,
    fetch: Callable[[JsonClient, Board], list[RawPosting]] = fetch_board,
) -> RunReport:
    today = today or date.today()
    report = RunReport(run_date=today.isoformat())
    include = compile_any(config.include_titles)
    exclude = compile_any(config.exclude_titles)
    not_permanent = compile_any(config.exclude_employment_types)
    allowed = set(config.allowed_countries)
    cutoff = (today - timedelta(days=config.max_posting_age_days)).isoformat()
    scorer = Scorer(profile)
    tiers = LocationTiers(config.target_locations, config.practice_locations)
    existing = sink.existing_keys()
    seen_this_run: list[ExistingKey] = []
    candidates: list[tuple[JobAnalysis, Extracted, RawPosting, str]] = []

    for bc in config.boards:
        board = Board(bc.platform.lower(), bc.token, bc.company)
        try:
            postings = fetch(client, board)
        except Exception as exc:  # one bad board must not stop the run
            log.warning("board %s failed: %s", board.label, exc)
            report.boards_failed.append(f"{bc.company} ({board.label}): {exc}")
            continue
        report.boards_ok += 1
        report.fetched += len(postings)
        for raw in postings:
            ok, _ = title_relevant(raw.title, include, exclude)
            if not ok or not raw.title:
                report.irrelevant_title += 1
                continue
            if not_permanent and raw.employment_type and not_permanent.search(raw.employment_type):
                report.not_permanent += 1
                continue
            if raw.posted and raw.posted < cutoff:
                report.too_old += 1
                continue
            ex = to_job_posting(raw)
            reach, reach_note = reachable(ex, allowed)
            if not reach:
                report.unreachable += 1
                continue
            if is_duplicate(ex, raw, existing + seen_this_run):
                report.duplicates += 1
                continue
            seen_this_run.append(
                ExistingKey(ex.job.company, normalise_role(ex.job.role), raw.url, raw.external_id)
            )
            candidates.append((scorer.score(ex.job), ex, raw, reach_note))

    # Best matches first, so the per-run cap never drops a strong role in
    # favour of a weak one, and no single large employer floods the tracker.
    candidates.sort(key=lambda c: -c[0].score)
    per_company: dict[str, int] = {}
    for analysis, ex, raw, reach_note in candidates:
        key = _company_key(ex.job.company)
        if (
            len(report.recorded) >= config.max_new_per_run
            or per_company.get(key, 0) >= config.max_new_per_company
        ):
            report.capped += 1
            continue
        purpose = tiers.purpose(ex)
        origin = (
            f"Discovered by the standalone runtime via {raw.platform} (listing: {raw.url}). "
            "Apply on the employer's own site where the listing links to one."
            if raw.platform.lower() in AGGREGATORS
            else "Discovered by the standalone runtime from the employer's public ATS API."
        )
        notes = "; ".join(
            x
            for x in (
                reach_note,
                "PRACTICE location - interview practice only" if purpose == PRACTICE else "",
                origin + " Company trust check (SYSTEM_SPEC 3a) and tailored resume still pending.",
            )
            if x
        )
        record = NewRecord(
            ex=ex,
            raw=raw,
            analysis=analysis,
            status=status_for(analysis.decision),
            variant=choose_variant(ex),
            notes=notes,
            purpose=purpose,
        )
        try:
            sink.add(record)
        except Exception as exc:
            report.write_errors.append(f"{ex.job.company} / {ex.job.role}: {exc}")
            continue
        per_company[key] = per_company.get(key, 0) + 1
        report.recorded.append(
            (
                ex.job.company,
                ex.job.role,
                analysis.score,
                analysis.decision.value,
                raw.url,
                ex.work_mode_label,
                purpose,
            )
        )
    return report
