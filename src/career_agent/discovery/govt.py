"""Government IT jobs: official recruitment pages + Telegram leads -> the
Notion "Govt Job Tracker" (kept separate from private-sector applications).

What this can and cannot know
-----------------------------
It finds NEW recruitment links on official pages and posts. It does not read
the PDF notification, so age limits, qualifying marks, GATE requirements and
dates are never guessed: each new row says exactly what Leela (or the Claude
daily run) must read before deciding. Bank and non-CS posts are filtered out.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import urljoin, urlsplit

from . import safety
from .http import TextClient, robots_allows
from .notion_sink import _plain, _rt_links
from .sources import RawPosting
from .telegram import BANK_WORDS, GOVT_CS_WORDS, GOVT_NOT_CS
from .webjobs import anchors

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"

RECRUIT = re.compile(
    r"(?i)(recruit|advertisement|advt|notification|vacanc|walk[- ]?in|engagement of|"
    r"applications? (are )?invited|project (engineer|associate|scientist)|scientist|"
    r"engineer|officer|analyst|programmer|developer|consultant)"
)
NOT_ADVERT = re.compile(
    r"(?i)(result|answer key|marks|shortlist|merit list|admit card|call letter|tender|rti\b|"
    r"archive|cancel+ed|interview schedule|document verification|syllabus only|"
    r"e-?procurement|auction|press release)"
)
MUST_CHECK = (
    "Open the official notification and check: your date of birth vs the age cut-off date; "
    "B.Tech % (you have GPA 7.8/10 - apply the organisation's CGPA-to-% rule); "
    "category relaxation; post-degree experience; GATE requirement; last date."
)


@dataclass
class GovtLead:
    post: str
    organisation: str
    url: str
    source: str  # "Official site" | "Telegram"
    verification: str  # PARTIALLY VERIFIED | UNVERIFIED
    notes: str
    found: str
    category: str = "Other"


@dataclass
class GovtReport:
    pages_ok: int = 0
    pages_failed: list[str] = field(default_factory=list)
    new: list[GovtLead] = field(default_factory=list)
    skipped: int = 0
    write_errors: list[str] = field(default_factory=list)
    open_deadlines: list[dict[str, str]] = field(default_factory=list)


def _years(today: date) -> tuple[str, ...]:
    return (str(today.year), str(today.year + 1), f"{today.year}-{str(today.year + 1)[2:]}")


def scan_page(org: str, url: str, page: str, cs_org: bool, today: date) -> list[GovtLead]:
    leads: list[GovtLead] = []
    seen: set[str] = set()
    years = _years(today)
    for href, text in anchors(page):
        if not href or href.startswith(("mailto:", "javascript:", "#", "tel:")):
            continue
        link = urljoin(url, href).split("#", 1)[0]
        if not link.startswith("https://") or link in seen:
            continue
        blob = f"{text} {link}"
        if not RECRUIT.search(blob) or NOT_ADVERT.search(blob) or BANK_WORDS.search(blob):
            continue
        if not any(y in blob for y in years):
            continue  # old adverts stay on these pages for years
        if not cs_org and (not GOVT_CS_WORDS.search(blob) or GOVT_NOT_CS.search(blob)):
            continue
        seen.add(link)
        leads.append(
            GovtLead(
                post=(text or urlsplit(link).path.rsplit("/", 1)[-1])[:180],
                organisation=org,
                url=link,
                source="Official site",
                verification="PARTIALLY VERIFIED",
                notes=(
                    f"Link found on the official page {url} on {today.isoformat()}. "
                    "The notification itself has not been read yet."
                ),
                found=today.isoformat(),
            )
        )
    return leads


def lead_from_telegram(raw: RawPosting, today: date) -> GovtLead:
    first = next((ln.strip() for ln in raw.description.splitlines() if ln.strip()), "")
    org = raw.company if not raw.company.startswith("Unknown") else first[:60]
    return GovtLead(
        post=(raw.title or first)[:180],
        organisation=org,
        url=raw.url,
        source="Telegram",
        verification="UNVERIFIED",
        notes=(
            f"{raw.source_name} post {raw.source_url}; channel trust {raw.source_trust}; "
            f"{raw.safety}. Find the notification on the organisation's official site "
            "before trusting any detail. Post text: " + raw.description[:900]
        ),
        found=today.isoformat(),
    )


class GovtSink:
    def __init__(
        self,
        client: Any,
        token: str,
        data_source_id: str,
        pause: float = 0.35,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not token or not data_source_id:
            raise ValueError("Govt tracker needs NOTION_TOKEN and a data source id")
        self.client = client
        self.ds = data_source_id.replace("collection://", "")
        self._headers = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION}
        self._pause = pause
        self._sleep = sleep

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        out = self.client.request(method, f"{API}{path}", body=body, headers=self._headers)
        self._sleep(self._pause)
        return out

    def _rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = None
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = self._call("POST", f"/data_sources/{self.ds}/query", body) or {}
            rows += data.get("results", [])
            if not data.get("has_more"):
                return rows
            cursor = data.get("next_cursor")

    def known(self) -> set[str]:
        out: set[str] = set()
        for page in self._rows():
            props = page.get("properties", {})
            for name in ("Notification URL", "Apply URL"):
                if v := _plain(props.get(name)):
                    out.add(v.rstrip("/").lower())
            notes = _plain(props.get("Notes"))
            out |= {u.rstrip("/").lower() for u in re.findall(r"https://\S+", notes)}
            title = _plain(props.get("Post")) + "|" + _plain(props.get("Organisation"))
            out.add(title.lower())
        return out

    def open_deadlines(self, today: date) -> list[dict[str, str]]:
        out = []
        for page in self._rows():
            props = page.get("properties", {})
            status = ((props.get("Status") or {}).get("select") or {}).get("name", "")
            last = ((props.get("Last Date") or {}).get("date") or {}).get("start", "")
            if status in ("SKIPPED", "EXPIRED", "APPLIED", "RESULT") or not last:
                continue
            if last[:10] < today.isoformat():
                continue
            out.append(
                {
                    "post": _plain(props.get("Post")),
                    "last": last[:10],
                    "fit": ((props.get("Fit") or {}).get("select") or {}).get("name", ""),
                    "status": status,
                    "url": _plain(props.get("Notification URL")) or _plain(props.get("Apply URL")),
                    "page": page.get("url", ""),
                }
            )
        return sorted(out, key=lambda r: r["last"])

    def add(self, lead: GovtLead) -> str:
        def txt(v: str) -> dict[str, Any]:
            return (
                {"rich_text": [{"type": "text", "text": {"content": v[:2000]}}]}
                if v
                else {"rich_text": []}
            )

        props = {
            "Post": {"title": [{"type": "text", "text": {"content": lead.post[:2000]}}]},
            "Organisation": txt(lead.organisation),
            "Category": {"select": {"name": lead.category}},
            "Notification URL": {"url": lead.url or None},
            "Verification": {"select": {"name": lead.verification}},
            "Status": {"select": {"name": "NEW"}},
            "Source": {"select": {"name": lead.source}},
            "Found": {"date": {"start": lead.found}},
            "GATE Required": {"select": {"name": "Unclear"}},
            "Leela Must Check": txt(MUST_CHECK),
            "Notes": txt(lead.notes),
        }
        body = {
            "parent": {"type": "data_source_id", "data_source_id": self.ds},
            "properties": props,
            "children": [{"type": "paragraph", "paragraph": _rt_links(lead.notes[:1900])}],
        }
        page = self._call("POST", "/pages", body) or {}
        return page.get("url", "")


def run_govt(
    watch: list[Any],
    client: TextClient,
    telegram_leads: list[RawPosting],
    sink: GovtSink | None,
    today: date | None = None,
    max_new: int = 25,
    reputation: safety.ReputationChecker | None = None,
) -> GovtReport:
    today = today or date.today()
    rep = GovtReport()
    known = sink.known() if sink else set()
    candidates: list[GovtLead] = []
    per_org: dict[str, int] = {}
    for w in watch:
        try:
            if not robots_allows(client, w.url):
                rep.pages_failed.append(f"{w.org}: robots.txt disallows {w.url}")
                continue
            page = client.get_text(w.url)
        except Exception as exc:
            rep.pages_failed.append(f"{w.org}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        rep.pages_ok += 1
        for lead in scan_page(w.org, w.url, page, w.cs_org, today):
            if per_org.get(w.org, 0) >= 4:
                rep.skipped += 1
                continue
            per_org[w.org] = per_org.get(w.org, 0) + 1
            candidates.append(lead)
    for raw in telegram_leads:
        check = safety.assess(raw.url, raw.description, reputation)
        if check.verdict == safety.DANGEROUS:
            rep.skipped += 1
            continue
        candidates.append(lead_from_telegram(raw, today))
    for lead in candidates:
        key_url = lead.url.rstrip("/").lower()
        key_title = f"{lead.post}|{lead.organisation}".lower()
        if key_url in known or key_title in known:
            rep.skipped += 1
            continue
        if len(rep.new) >= max_new:
            rep.skipped += 1
            continue
        known |= {key_url, key_title}
        if sink:
            try:
                sink.add(lead)
            except Exception as exc:
                rep.write_errors.append(f"{lead.organisation}: {exc}")
                continue
        rep.new.append(lead)
    if sink:
        try:
            rep.open_deadlines = sink.open_deadlines(today)
        except Exception as exc:
            rep.write_errors.append(f"reading open deadlines: {exc}")
    return rep
