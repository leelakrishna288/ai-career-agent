"""Append-safe application tracker.

Two rules, both learned the expensive way:

* **Never overwrite history.** Status changes append to a history list; the
  previous state is still readable. A tracker that silently rewrites itself
  cannot answer "when did this go quiet?", which is the only question that
  matters when you are diagnosing a stalled pipeline.
* **Never create a duplicate application.** Deduplication is on company +
  normalised role + canonical URL, so the same job seen on LinkedIn and on the
  company's own careers page is one row, not two.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    Application,
    Decision,
    JobPosting,
    Status,
    canonical_url,
    normalise_role,
    same_posting,
)

FIELDS = [
    "application_id",
    "job_id",
    "company",
    "role",
    "location",
    "url",
    "source",
    "date_found",
    "match_score",
    "decision",
    "status",
    "resume_id",
    "notes",
]


class Tracker:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._apps: dict[str, Application] = {}
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                app = Application.model_validate_json(line)
                self._apps[app.application_id] = app

    def _append(self, app: Application) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(app.model_dump_json() + "\n")

    # ------------------------------------------------------------------
    @staticmethod
    def dedupe_key(company: str, role: str, url: str) -> str:
        """The strict key. Kept for inspection and tests; `find` is deliberately
        looser, because job boards mangle URLs more than they mangle titles."""
        return f"{company.strip().lower()}|{normalise_role(role)}|{canonical_url(url)}"

    @staticmethod
    def _company_role_key(company: str, role: str) -> str:
        return f"{company.strip().lower()}|{normalise_role(role)}"

    def find(self, company: str, role: str, url: str = "") -> Application | None:
        """Two postings are the same job when the company and the normalised role
        match AND the URLs do not contradict each other. Title noise is stripped
        ("Sr. Software Engineer II ... - Remote" == "Senior Software Engineer");
        URL noise is stripped separately (see models.same_posting)."""
        key = self._company_role_key(company, role)
        for app in self._apps.values():
            if self._company_role_key(app.company, app.role) != key:
                continue
            if same_posting(app.url, url):
                return app
        return None

    def add(
        self, job: JobPosting, score: float = 0.0, decision: Decision = Decision.MAYBE
    ) -> tuple[Application, bool]:
        """Returns (application, created). Never creates a duplicate."""
        existing = self.find(job.company, job.role, job.url)
        if existing is not None:
            return existing, False
        app = Application(
            application_id=f"APP-{len(self._apps) + 1:04d}-{job.job_id[-6:]}",
            job_id=job.job_id,
            company=job.company,
            role=job.role,
            location=job.location,
            url=job.url,
            source=job.source,
            date_found=datetime.now(timezone.utc).date().isoformat(),
            match_score=score,
            decision=decision,
            status=Status.DISCOVERED,
            history=[f"{datetime.now(timezone.utc).isoformat()} DISCOVERED"],
        )
        self._apps[app.application_id] = app
        self._append(app)
        return app, True

    def update_status(self, application_id: str, status: Status, note: str = "") -> Application:
        app = self._apps.get(application_id)
        if app is None:
            raise KeyError(f"unknown application: {application_id}")
        entry = f"{datetime.now(timezone.utc).isoformat()} {app.status.value} -> {status.value}"
        if note:
            entry += f" ({note})"
        updated = app.model_copy(update={"status": status, "history": [*app.history, entry]})
        self._apps[application_id] = updated
        self._append(updated)
        return updated

    def all(self) -> list[Application]:
        return sorted(self._apps.values(), key=lambda a: a.application_id)

    def pipeline(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for app in self._apps.values():
            counts[app.status.value] = counts.get(app.status.value, 0) + 1
        return counts

    def export_csv(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
            writer.writeheader()
            for app in self.all():
                row = json.loads(app.model_dump_json())
                writer.writerow({k: row.get(k, "") for k in FIELDS})
        return path
