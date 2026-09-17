"""Notion Application Tracker sink (Notion API version 2025-09-03).

The token comes from the NOTION_TOKEN environment variable only - never a
file, never a log line. The integration must be shared with the
"AI Career Agent" page in Notion before it can see the tracker.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..models import normalise_role
from .http import JsonClient
from .pipeline import ExistingKey, NewRecord, RunReport

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
TEXT_LIMIT = 2000


def _text(value: str) -> dict[str, Any]:
    return (
        {"rich_text": [{"type": "text", "text": {"content": value[:TEXT_LIMIT]}}]}
        if value
        else {"rich_text": []}
    )


def _select(value: str) -> dict[str, Any]:
    return {"select": {"name": value}} if value else {"select": None}


def _plain(prop: dict[str, Any] | None) -> str:
    if not prop:
        return ""
    kind = prop.get("type")
    if kind in ("title", "rich_text"):
        return "".join(p.get("plain_text", "") for p in prop.get(kind) or [])
    if kind == "url":
        return prop.get("url") or ""
    return ""


class NotionTrackerSink:
    def __init__(
        self,
        client: JsonClient,
        token: str,
        data_source_id: str,
        pause: float = 0.35,
        sleep=time.sleep,
    ):  # noqa: ANN001
        if not token:
            raise ValueError("NOTION_TOKEN is not set")
        if not data_source_id:
            raise ValueError("Notion data source id is not set")
        self.client = client
        self.ds = data_source_id.replace("collection://", "")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
        }
        self._pause = pause
        self._sleep = sleep

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        out = self.client.request(method, f"{API}{path}", body=body, headers=self._headers)
        self._sleep(self._pause)  # Notion allows ~3 requests/second
        return out

    # -- reading ------------------------------------------------------------
    def existing_keys(self) -> list[ExistingKey]:
        keys: list[ExistingKey] = []
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = self._call("POST", f"/data_sources/{self.ds}/query", body) or {}
            for page in data.get("results", []):
                props = page.get("properties", {})
                company = _plain(props.get("Company"))
                role = _plain(props.get("Role"))
                keys.append(
                    ExistingKey(
                        company=company,
                        # Recompute rather than trust the stored column: older rows
                        # were normalised by hand and do not follow normalise_role.
                        normalized_role=normalise_role(role)
                        or _plain(props.get("Normalized Role")),
                        url=_plain(props.get("Job URL")) or _plain(props.get("Canonical URL")),
                        external_id=_plain(props.get("External Job ID")),
                    )
                )
            if not data.get("has_more"):
                return keys
            cursor = data.get("next_cursor")

    # -- writing ------------------------------------------------------------
    @staticmethod
    def properties(rec: NewRecord, today: str) -> dict[str, Any]:
        job, a = rec.ex.job, rec.analysis
        b = a.breakdown
        reason = " ".join(a.hard_rejects + a.reasons)
        props: dict[str, Any] = {
            "Company + Role": {
                "title": [
                    {
                        "type": "text",
                        "text": {"content": f"{job.company} — {job.role}"[:TEXT_LIMIT]},
                    }
                ]
            },
            "Company": _text(job.company),
            "Role": _text(job.role),
            "Normalized Role": _text(normalise_role(job.role)),
            "Location": _text(job.location),
            "Country": _select(
                rec.ex.country
                if rec.ex.country
                in {"India", "UAE", "Qatar", "Saudi Arabia", "USA", "Europe", "Remote-Worldwide"}
                else "Other"
            ),
            "Work Mode": _select(rec.ex.work_mode_label),
            "Platform": _select(rec.raw.platform),
            "Discovered By": _select("Standalone runtime"),
            "Job URL": {"url": rec.raw.url or None},
            "Canonical URL": {"url": rec.raw.url or None},
            "External Job ID": _text(rec.raw.external_id),
            "Date Found": {"date": {"start": today}},
            "Experience Required": _text(rec.ex.experience_text or "not stated"),
            "Salary Stated": _text(job.salary_text),
            "Salary Confidence": _select("STATED" if job.salary_text else "UNKNOWN"),
            "Match Score": {"number": a.score},
            "Match Class": _select(a.band),
            "Technical Match": {"number": b.technical},
            "Experience Match": {"number": b.experience},
            "AI Match": {"number": b.ai_relevance},
            "ATS Estimate": {"number": a.ats_keyword_coverage},
            "Decision": _select(a.decision.value.replace("_", " ")),
            "Decision Reason": _text(reason),
            "Missing Skills": _text(", ".join(a.missing_required)),
            "Status": _select(rec.status),
            "Resume Variant": _select(rec.variant if rec.status != "REJECTED" else ""),
            "Verification": _select("REAL"),
            "Source Confidence": _select("HIGH"),
            "Company Verdict": _select("UNVERIFIED"),
            "Google Maps Presence": _select("Not checked"),
            "Next Action": _text(
                "Run company trust check, then tailor resume (Claude daily run or manual)"
                if rec.status == "MATCHED"
                else ""
            ),
            "Notes": _text(rec.notes),
        }
        if job.posted:
            props["Date Posted"] = {"date": {"start": job.posted}}
        return props

    def add(self, record: NewRecord) -> str:
        from datetime import date

        body = {
            "parent": {"type": "data_source_id", "data_source_id": self.ds},
            "properties": self.properties(record, date.today().isoformat()),
        }
        page = self._call("POST", "/pages", body) or {}
        return page.get("url", "")

    def write_report(self, parent_page_id: str, title: str, markdown: str) -> str:
        blocks = []
        for line in markdown.splitlines():
            if not line.strip():
                continue
            if line.startswith("## "):
                blocks.append({"type": "heading_2", "heading_2": _rt(line[3:])})
            elif line.startswith("- "):
                blocks.append({"type": "bulleted_list_item", "bulleted_list_item": _rt(line[2:])})
            elif not line.startswith("# "):
                blocks.append({"type": "paragraph", "paragraph": _rt(line)})
        body = {
            "parent": {"page_id": parent_page_id},
            "properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}},
            "children": blocks[:100],
        }
        page = self._call("POST", "/pages", body) or {}
        return page.get("url", "")


def _rt(text: str) -> dict[str, Any]:
    return {"rich_text": [{"type": "text", "text": {"content": text[:TEXT_LIMIT]}}]}


class MemorySink:
    """In-memory / JSONL-free sink for dry runs and tests."""

    def __init__(self, existing: list[ExistingKey] | None = None):
        self._existing = list(existing or [])
        self.records: list[NewRecord] = []

    def existing_keys(self) -> list[ExistingKey]:
        return list(self._existing)

    def add(self, record: NewRecord) -> str:
        self.records.append(record)
        return ""


def render_report(report: RunReport, public: bool = False) -> str:
    """Markdown run report. `public=True` omits companies, roles and scores so
    a public CI log never exposes the job search."""
    lines = [
        f"# Standalone discovery run — {report.run_date}",
        "## Summary",
        f"- Boards fetched: {report.boards_ok} ok, {len(report.boards_failed)} failed",
        f"- Postings read: {report.fetched}",
        f"- Skipped: {report.irrelevant_title} off-target titles, {report.too_old} too old, "
        f"{report.unreachable} unreachable on work authorisation, {report.duplicates} already tracked, "
        f"{report.capped} over the per-run cap",
        f"- New rows written: {len(report.recorded)} "
        f"(APPLY {report.counts('APPLY')}, MAYBE {report.counts('MAYBE')}, "
        f"MANUAL REVIEW {report.counts('MANUAL_REVIEW')}, DO NOT APPLY {report.counts('DO_NOT_APPLY')})",
        f"- Write errors: {len(report.write_errors)}",
    ]
    if public:
        lines.append("Details are in the Notion tracker (omitted here because CI logs are public).")
        return "\n".join(lines) + "\n"
    top = sorted((r for r in report.recorded if r[3] in ("APPLY", "MAYBE")), key=lambda r: -r[2])
    lines.append("## New matches (company trust check still pending)")
    if not top:
        lines.append("- None this run.")
    for company, role, score, decision, url, mode in top:
        lines.append(f"- {score:.0f} {decision} — {company} — {role} — {mode} — {url}")
    if report.boards_failed:
        lines.append("## Boards that failed")
        lines += [f"- {b}" for b in report.boards_failed]
    if report.write_errors:
        lines.append("## Write errors")
        lines += [f"- {e}" for e in report.write_errors]
    lines.append("## Next action for Leela")
    lines.append(
        "- Nothing is applied automatically. Review MATCHED rows in Notion after the "
        "company check, then set Status = APPROVED on the ones you want."
    )
    return "\n".join(lines) + "\n"
