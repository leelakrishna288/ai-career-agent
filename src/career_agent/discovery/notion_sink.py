"""Notion Application Tracker sink (Notion API version 2025-09-03).

The token comes from the NOTION_TOKEN environment variable only - never a
file, never a log line. The integration must be shared with the
"AI Career Agent" page in Notion before it can see the tracker.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from ..models import normalise_role
from .http import JsonClient
from .notion_resume_attachment import NotionAttacher, NotionAttachError
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
        attacher=None,
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
        self._token = token
        self._pause = pause
        self._sleep = sleep
        self._attacher = attacher

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
            "Purpose": _select(rec.purpose),
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
                (
                    "Find the employer's own application page (this is a job-board copy), "
                    "then run the company trust check and tailor the resume"
                    if rec.raw.platform in ("Himalayas", "Remotive")
                    else "Run company trust check, then tailor resume (Claude daily run or manual)"
                )
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
        return page.get("id") or page.get("url", "")

    # -- resume attachment ----------------------------------------------------
    def update_properties(self, page_ref: str, properties: dict[str, Any]) -> None:
        self._call("PATCH", f"/pages/{page_id_of(page_ref)}", {"properties": properties})

    def append_blocks(self, page_ref: str, blocks: list[dict[str, Any]]) -> None:
        pid = page_id_of(page_ref)
        for i in range(0, len(blocks), 90):  # Notion accepts at most 100 children per call
            self._call("PATCH", f"/blocks/{pid}/children", {"children": blocks[i : i + 90]})

    def attach_resume(self, page_ref: str, filename: str, content: bytes) -> bool:
        """Attach the tailored resume DOCX to the row's `Resume Attachment` property.

        Returns True only when Notion confirmed the attachment. A False means the
        row has no attachment, so the caller must not report the resume as
        prepared on the assumption that one exists (SYSTEM_SPEC v1.8 13.3).
        """
        page_id = page_id_of(page_ref)
        try:
            attacher = self._attacher or NotionAttacher  # resolved late, so tests can patch it
            attacher(self._token).upload_resume_bytes(page_id, filename, content)
            return True
        except NotionAttachError as exc:
            log.warning("resume not attached to %s: %s", page_id, exc)
            return False

    def pending_rows(self, since: str, limit: int = 20) -> list[dict[str, str]]:
        """Rows waiting for an application, newest analysis first (for the digest)."""
        body: dict[str, Any] = {
            "page_size": 100,
            "filter": {
                "and": [
                    {"property": "Date Found", "date": {"on_or_after": since}},
                    {
                        "or": [
                            {"property": "Status", "select": {"equals": s}}
                            for s in ("MATCHED", "RESUME_PREPARED", "READY_FOR_REVIEW", "APPROVED")
                        ]
                    },
                    {
                        "or": [
                            {"property": "Decision", "select": {"equals": d}}
                            for d in ("APPLY", "MAYBE")
                        ]
                    },
                ]
            },
            "sorts": [{"property": "Match Score", "direction": "descending"}],
        }
        data = self._call("POST", f"/data_sources/{self.ds}/query", body) or {}
        out = []
        for page in data.get("results", [])[:limit]:
            props = page.get("properties", {})
            out.append(
                {
                    "company": _plain(props.get("Company")),
                    "role": _plain(props.get("Role")),
                    "url": _plain(props.get("Job URL")),
                    "score": _num(props.get("Match Score")),
                    "ats": _num(props.get("ATS Estimate")),
                    "status": _sel(props.get("Status")),
                    "verdict": _sel(props.get("Company Verdict")),
                    "purpose": _sel(props.get("Purpose")),
                    "platform": _sel(props.get("Platform")),
                    "next": _plain(props.get("Next Action")),
                    "page": page.get("url", ""),
                }
            )
        return out

    def write_report(self, parent_page_id: str, title: str, markdown: str) -> str:
        blocks = []
        for line in markdown.splitlines():
            if not line.strip():
                continue
            if line.startswith("## "):
                blocks.append({"type": "heading_2", "heading_2": _rt(line[3:])})
            elif line.startswith("- "):
                blocks.append(
                    {"type": "bulleted_list_item", "bulleted_list_item": _rt_links(line[2:])}
                )
            elif not line.startswith("# "):
                blocks.append({"type": "paragraph", "paragraph": _rt_links(line)})
        body = {
            "parent": {"page_id": parent_page_id},
            "properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}},
            "children": blocks[:100],
        }
        page = self._call("POST", "/pages", body) or {}
        rest = blocks[100:]
        if rest and page.get("id"):
            self.append_blocks(page["id"], rest)
        return page.get("url", "")


def _rt(text: str) -> dict[str, Any]:
    return {"rich_text": [{"type": "text", "text": {"content": text[:TEXT_LIMIT]}}]}


_URL = re.compile(r"https://[^\s)>\]]+")


def _rt_links(text: str) -> dict[str, Any]:
    """Rich text where every https URL is a clickable link."""
    parts: list[dict[str, Any]] = []
    pos = 0
    for m in _URL.finditer(text):
        if m.start() > pos:
            parts.append({"type": "text", "text": {"content": text[pos : m.start()][:TEXT_LIMIT]}})
        url = m.group(0).rstrip(".,;")
        parts.append(
            {"type": "text", "text": {"content": url[:TEXT_LIMIT], "link": {"url": url[:2000]}}}
        )
        pos = m.start() + len(url)
    if pos < len(text):
        parts.append({"type": "text", "text": {"content": text[pos:][:TEXT_LIMIT]}})
    return {"rich_text": parts[:100] or [{"type": "text", "text": {"content": ""}}]}


_HEXRUN = re.compile(r"[0-9a-f]{32,}")


def page_id_of(ref: str) -> str:
    """Accept a page id (with or without dashes) or a Notion page URL."""
    compact = ref.split("?", 1)[0].replace("-", "").lower()
    runs = _HEXRUN.findall(compact)
    return runs[-1][-32:] if runs else ref


def _num(prop: dict[str, Any] | None) -> float:
    return float((prop or {}).get("number") or 0.0)


def _sel(prop: dict[str, Any] | None) -> str:
    return ((prop or {}).get("select") or {}).get("name", "")


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
        f"- Skipped: {report.irrelevant_title} off-target titles, "
        f"{report.not_permanent} not permanent roles, {report.too_old} too old, "
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
    for company, role, score, decision, url, mode, purpose in top:
        lines.append(f"- {score:.0f} {decision} — {company} — {role} — {mode} — {purpose} — {url}")
    if report.boards_failed:
        lines.append("## Boards that failed")
        lines += [f"- {b}" for b in report.boards_failed]
    if report.write_errors:
        lines.append("## Write errors")
        lines += [f"- {e}" for e in report.write_errors]
    lines.append("## Next action for Leela")
    lines.append(
        "- The Claude daily run checks each company and prepares resumes; the laptop "
        "submission run then applies to company-portal roles scoring 75+ with a GENUINE "
        "company (SYSTEM_SPEC 11a). Rows outside that rule need Status = APPROVED."
    )
    return "\n".join(lines) + "\n"
