"""Public ATS job-board adapters: Greenhouse, Lever, Ashby.

All three publish documented, unauthenticated JSON endpoints intended for
exactly this use (embedding a company's open roles elsewhere). LinkedIn and
Naukri are deliberately absent: their terms prohibit automated collection.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from .http import JsonClient


@dataclass(frozen=True)
class Board:
    platform: str  # greenhouse | lever | ashby
    token: str
    company: str

    @property
    def label(self) -> str:
        return f"{self.platform}:{self.token}"


@dataclass
class RawPosting:
    company: str
    title: str
    location: str
    url: str
    external_id: str
    platform: str  # Greenhouse | Lever | Ashby (Notion select values)
    description: str = ""
    posted: str = ""  # ISO date
    workplace_hint: str = ""  # remote | hybrid | onsite | ""
    country_hint: str = ""
    salary_text: str = ""
    extra_locations: list[str] = field(default_factory=list)


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")


def html_to_text(raw: str) -> str:
    """Greenhouse double-escapes its HTML, so unescape before stripping tags."""
    if not raw:
        return ""
    text = html.unescape(html.unescape(raw))
    text = re.sub(r"(?i)<\s*(br|/p|/li|/h\d|/div)\s*/?>", "\n", text)
    text = re.sub(r"(?i)<\s*li[^>]*>", "\n- ", text)
    text = _TAG.sub(" ", text)
    text = _WS.sub(" ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _iso_date(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, (int, float)):  # Lever: epoch milliseconds
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except (ValueError, OSError):
        return ""


def fetch_greenhouse(client: JsonClient, board: Board) -> list[RawPosting]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{quote(board.token)}/jobs?content=true"
    data = client.request("GET", url) or {}
    out = []
    for j in data.get("jobs", []):
        offices = [o.get("name", "") for o in j.get("offices") or [] if o.get("name")]
        out.append(
            RawPosting(
                company=j.get("company_name") or board.company,
                title=(j.get("title") or "").strip(),
                location=((j.get("location") or {}).get("name") or "").strip(),
                url=j.get("absolute_url") or "",
                external_id=f"gh-{j.get('id')}",
                platform="Greenhouse",
                description=html_to_text(j.get("content") or ""),
                posted=_iso_date(j.get("first_published") or j.get("updated_at")),
                extra_locations=offices,
            )
        )
    return out


def fetch_lever(client: JsonClient, board: Board) -> list[RawPosting]:
    url = f"https://api.lever.co/v0/postings/{quote(board.token)}?mode=json"
    data = client.request("GET", url) or []
    out = []
    for j in data:
        cats = j.get("categories") or {}
        lists = "\n".join(
            f"{item.get('text', '')}\n{html_to_text(item.get('content', ''))}"
            for item in j.get("lists") or []
        )
        desc = "\n".join(
            x
            for x in (
                j.get("descriptionPlain") or html_to_text(j.get("description", "")),
                lists,
                j.get("additionalPlain", ""),
            )
            if x
        )
        salary = ""
        sr = j.get("salaryRange") or {}
        if sr.get("min") or sr.get("max"):
            salary = f"{sr.get('currency', '')} {sr.get('min', '')}-{sr.get('max', '')} {sr.get('interval', '')}".strip()
        out.append(
            RawPosting(
                company=board.company,
                title=(j.get("text") or "").strip(),
                location=(cats.get("location") or "").strip(),
                url=j.get("hostedUrl") or "",
                external_id=f"lever-{j.get('id')}",
                platform="Lever",
                description=desc,
                posted=_iso_date(j.get("createdAt")),
                workplace_hint=(j.get("workplaceType") or "").lower().replace("unspecified", ""),
                country_hint=(j.get("country") or "").upper(),
                salary_text=salary,
                extra_locations=list(cats.get("allLocations") or []),
            )
        )
    return out


def fetch_ashby(client: JsonClient, board: Board) -> list[RawPosting]:
    url = (
        f"https://api.ashbyhq.com/posting-api/job-board/{quote(board.token)}"
        "?includeCompensation=true"
    )
    data = client.request("GET", url) or {}
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        addr = (j.get("address") or {}).get("postalAddress") or {}
        wt = (j.get("workplaceType") or "").lower()
        if not wt and j.get("isRemote"):
            wt = "remote"
        comp = j.get("compensation") or {}
        salary = ""
        if j.get("shouldDisplayCompensationOnJobPostings"):
            salary = comp.get("compensationTierSummary") or ""
        secondary = [
            s.get("location", "") for s in j.get("secondaryLocations") or [] if isinstance(s, dict)
        ]
        out.append(
            RawPosting(
                company=board.company,
                title=(j.get("title") or "").strip(),
                location=(j.get("location") or "").strip(),
                url=j.get("jobUrl") or "",
                external_id=f"ashby-{j.get('id')}",
                platform="Ashby",
                description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
                posted=_iso_date(j.get("publishedAt")),
                workplace_hint=wt,
                country_hint=addr.get("addressCountry") or "",
                salary_text=salary or "",
                extra_locations=[s for s in secondary if s],
            )
        )
    return out


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


def fetch_board(client: JsonClient, board: Board) -> list[RawPosting]:
    try:
        fetcher = FETCHERS[board.platform]
    except KeyError:
        raise ValueError(f"unknown platform {board.platform!r} for {board.company}") from None
    return fetcher(client, board)
