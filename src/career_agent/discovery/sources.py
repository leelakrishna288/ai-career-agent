"""Public job-source adapters.

Employer ATS boards (Greenhouse, Lever, Ashby) publish documented,
unauthenticated JSON endpoints intended for exactly this use. Two remote-job
boards (Himalayas, Remotive) publish free public APIs whose terms require a
link back to the listing and naming the board as the source - the tracker
keeps the board URL and sets Platform to the board's name. Neither may be
re-published to another job board; this tool only writes to a private tracker.

LinkedIn, Naukri and Indeed are deliberately absent: their terms prohibit
automated collection and automated applying. Their jobs arrive through the
email alerts the user subscribes to.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from .http import JsonClient

log = logging.getLogger(__name__)


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
    employment_type: str = ""  # as published, e.g. "Full Time", "Contractor"
    # Community sources (Telegram, job blogs) only:
    category: str = ""  # "private" | "govt"
    safety: str = ""  # safety.SafetyResult.summary()
    safety_verdict: str = ""  # OK | CAUTION | SUSPICIOUS | DANGEROUS
    source_trust: str = ""  # HIGH | MEDIUM | LOW + reason
    source_url: str = ""  # the post / listing the lead came from
    source_name: str = ""


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
        if isinstance(value, (int, float)):
            # Lever sends epoch milliseconds, Himalayas epoch seconds.
            seconds = value / 1000 if value > 100_000_000_000 else value
            return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
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
                employment_type=cats.get("commitment") or "",
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
                employment_type=j.get("employmentType") or "",
            )
        )
    return out


HIMALAYAS_PAGES = 2  # 20 jobs per page; the API rate-limits heavier use


def fetch_himalayas(client: JsonClient, board: Board) -> list[RawPosting]:
    """Token format: "<keywords>|<country>". An empty country searches
    worldwide-remote roles only."""
    query, _, country = board.token.partition("|")
    params = f"q={quote(query.strip())}"
    params += f"&country={quote(country.strip())}" if country.strip() else "&worldwide=true"
    out: list[RawPosting] = []
    for page in range(1, HIMALAYAS_PAGES + 1):
        url = f"https://himalayas.app/jobs/api/search?{params}&page={page}"
        data = client.request("GET", url) or {}
        jobs = data.get("jobs") or []
        for j in jobs:
            places = [p for p in j.get("locationRestrictions") or [] if p]
            link = j.get("applicationLink") or j.get("guid") or ""
            sal = ""
            if j.get("minSalary") or j.get("maxSalary"):
                sal = (
                    f"{j.get('currency') or ''} {j.get('minSalary') or ''}-"
                    f"{j.get('maxSalary') or ''} {j.get('salaryPeriod') or ''}"
                ).strip()
            out.append(
                RawPosting(
                    company=(j.get("companyName") or "").strip(),
                    title=(j.get("title") or "").strip(),
                    location=", ".join(places) if places else "Worldwide",
                    url=link,
                    external_id=f"himalayas-{j.get('guid') or link}",
                    platform="Himalayas",
                    description=html_to_text(j.get("description") or ""),
                    posted=_iso_date(j.get("pubDate")),
                    workplace_hint="remote",
                    salary_text=sal,
                    extra_locations=places[1:],
                    employment_type=j.get("employmentType") or "",
                )
            )
        if len(jobs) < 20:
            break
    return out


def fetch_remotive(client: JsonClient, board: Board) -> list[RawPosting]:
    """Token = Remotive category slug. One call per run: Remotive asks for at
    most four fetches a day."""
    url = f"https://remotive.com/api/remote-jobs?category={quote(board.token)}"
    data = client.request("GET", url) or {}
    out = []
    for j in data.get("jobs", []):
        where = (j.get("candidate_required_location") or "").strip()
        out.append(
            RawPosting(
                company=(j.get("company_name") or "").strip(),
                title=(j.get("title") or "").strip(),
                location=where or "Worldwide",
                url=j.get("url") or "",
                external_id=f"remotive-{j.get('id')}",
                platform="Remotive",
                description=html_to_text(j.get("description") or ""),
                posted=_iso_date(j.get("publication_date")),
                workplace_hint="remote",
                salary_text=(j.get("salary") or "").strip(),
                employment_type=(j.get("job_type") or "").replace("_", " "),
            )
        )
    return out


def fetch_telegram(client: JsonClient, board: Board) -> list[RawPosting]:
    """Token = public channel handle. Returns private-sector leads and
    government leads (category="govt"); skipped posts are dropped here."""
    from datetime import date

    from . import safety
    from .telegram import (
        channel_trust,
        classify,
        fields,
        first_external_link,
        guess_company_role,
        parse_channel,
        post_age_days,
        split_blocks,
    )

    get_text = getattr(client, "get_text", None)
    if get_text is None:
        raise ValueError("telegram source needs an HTML-capable client")
    handle = board.token.strip().lstrip("@")
    page = get_text(f"https://t.me/s/{quote(handle)}")
    ch = parse_channel(handle, page)
    if not ch.has_preview:
        raise ValueError(
            f"t.me/{handle} has no public preview ({ch.subscribers or '?'} {ch.kind or 'members'}); "
            "it is a private group or has previews disabled - forward its posts to the Notion Job Inbox"
        )
    today = getattr(client, "today", None) or date.today()
    level, why = channel_trust(ch, today, TELEGRAM_MIN_SUBSCRIBERS)
    reputation = getattr(client, "reputation", None)
    out: list[RawPosting] = []
    for post in ch.posts:
        if post_age_days(post, today) > TELEGRAM_MAX_AGE_DAYS:
            continue
        for n, (text, links) in enumerate(split_blocks(post)):
            category, _ = classify(text)
            if category == "skip":
                continue
            link = first_external_link(links)
            if not link:
                continue
            verdict = safety.assess(link, text, reputation)
            if verdict.verdict == safety.DANGEROUS:
                continue
            company, role = guess_company_role(text)
            f = fields(text)
            out.append(
                RawPosting(
                    company=company or f"Unknown (see {post.url})",
                    title=role or text.splitlines()[0][:80],
                    location=f.get("location", ""),
                    url=link,
                    external_id=f"tg-{post.post_id}" + (f"-{n}" if n else ""),
                    platform="Telegram",
                    description=text,
                    posted=post.day,
                    category=category,
                    safety=verdict.summary(),
                    safety_verdict=verdict.verdict,
                    source_trust=f"{level} ({why})",
                    source_url=post.url,
                    source_name=f"Telegram {ch.title or handle}",
                )
            )
    return out


def fetch_jobsite(client: JsonClient, board: Board) -> list[RawPosting]:
    """Token = a listing page on a job blog whose robots.txt allows crawling
    and whose job pages publish schema.org JobPosting data. Each job's own
    "apply on company website" link becomes the Job URL."""
    from . import safety
    from .http import robots_allows
    from .webjobs import detail_links, parse_detail

    get_text = getattr(client, "get_text", None)
    if get_text is None:
        raise ValueError("jobsite source needs an HTML-capable client")
    if not robots_allows(client, board.token):  # type: ignore[arg-type]
        raise ValueError(f"robots.txt disallows {board.token}")
    listing = get_text(board.token)
    reputation = getattr(client, "reputation", None)
    pause = getattr(client, "_sleep", None)
    out: list[RawPosting] = []
    for url in detail_links(board.token, listing)[:JOBSITE_MAX_DETAILS]:
        if pause:
            pause(1.0)  # be polite: one page a second
        try:
            job = parse_detail(url, get_text(url))
        except Exception as exc:  # one bad page must not stop the listing
            log.warning("job page %s skipped: %s", url, exc)
            continue
        if job is None:
            continue
        verdict = safety.assess(job.apply_url, job.description, reputation)
        if verdict.verdict == safety.DANGEROUS:
            continue
        out.append(
            RawPosting(
                company=job.company,
                title=job.title,
                location=job.location,
                url=job.apply_url or url,
                external_id=f"site-{url}",
                platform="Other",
                description=job.description,
                posted=_iso_date(job.posted),
                country_hint=job.country,
                employment_type=job.employment_type if job.employment_type != "OTHER" else "",
                category="private",
                safety=verdict.summary(),
                safety_verdict=verdict.verdict,
                source_trust="",
                source_url=url,
                source_name=board.company,
            )
        )
    return out


TELEGRAM_MIN_SUBSCRIBERS = 5_000
TELEGRAM_MAX_AGE_DAYS = 3
JOBSITE_MAX_DETAILS = 15

FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "himalayas": fetch_himalayas,
    "remotive": fetch_remotive,
    "telegram": fetch_telegram,
    "jobsite": fetch_jobsite,
}
AGGREGATORS = {"himalayas", "remotive", "telegram", "jobsite"}
COMMUNITY = {"telegram", "jobsite"}


def fetch_board(client: JsonClient, board: Board) -> list[RawPosting]:
    try:
        fetcher = FETCHERS[board.platform]
    except KeyError:
        raise ValueError(f"unknown platform {board.platform!r} for {board.company}") from None
    return fetcher(client, board)
