"""Job-blog pages that publish schema.org JobPosting data (JSON-LD).

Only the structured data the site publishes for search engines is read, plus
the page's own "apply on company website" link. robots.txt is checked by the
caller before anything is fetched.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

_LD = re.compile(r"(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>")
_MD = re.compile(r"[#*_`>]+")


@dataclass
class SiteJob:
    title: str
    company: str
    location: str
    country: str
    posted: str
    employment_type: str
    description: str
    apply_url: str


class _Anchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._text = []

    def handle_data(self, data):  # noqa: ANN001
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag == "a" and self._href is not None:
            self.items.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def anchors(page: str) -> list[tuple[str, str]]:
    p = _Anchors()
    p.feed(page)
    p.close()
    return p.items


def detail_links(listing_url: str, page: str) -> list[str]:
    host = urlsplit(listing_url).netloc
    seen: dict[str, None] = {}
    for href, _ in anchors(page):
        url = urljoin(listing_url, href).split("#", 1)[0]
        parts = urlsplit(url)
        if parts.netloc != host or parts.query:
            continue
        if re.fullmatch(r"/jobs?/[a-z0-9][a-z0-9-]{5,}/?", parts.path):
            seen.setdefault(url, None)
    return list(seen)


def _job_ld(page: str) -> dict | None:
    for m in _LD.finditer(page):
        try:
            data = json.loads(html.unescape(m.group(1)).strip())
        except ValueError:
            continue
        for item in data if isinstance(data, list) else data.get("@graph", [data]):
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def parse_detail(url: str, page: str) -> SiteJob | None:
    ld = _job_ld(page)
    if not ld:
        return None
    org = ld.get("hiringOrganization") or {}
    loc = ld.get("jobLocation") or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    addr = (loc.get("address") or {}) if isinstance(loc, dict) else loc
    if isinstance(addr, list):
        addr = addr[0] if addr else {}
    if isinstance(addr, str):  # some pages publish a plain string address
        addr = {"addressLocality": addr}
    if not isinstance(addr, dict):
        addr = {}
    country = addr.get("addressCountry") or ""
    if isinstance(country, dict):
        country = country.get("name", "")
    site = urlsplit(url).netloc.lower()
    apply_url = ""
    for href, text in anchors(page):
        if not href.startswith("https://"):
            continue
        host = urlsplit(href).netloc.lower()
        if host == site or host in ("t.me", "telegram.me") or "googletagmanager" in host:
            continue
        if re.search(r"(?i)apply", text):
            apply_url = href
            break
    desc = _MD.sub("", str(ld.get("description") or ""))
    return SiteJob(
        title=str(ld.get("title") or "").strip(),
        company=(str(org.get("name") or "") if isinstance(org, dict) else str(org)).strip(),
        location=str(addr.get("addressLocality") or "").strip(),
        country=str(country).strip(),
        posted=str(ld.get("datePosted") or ""),
        employment_type=str(ld.get("employmentType") or ""),
        description=desc.strip(),
        apply_url=apply_url,
    )
