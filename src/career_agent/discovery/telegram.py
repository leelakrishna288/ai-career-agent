"""Public Telegram channel previews (https://t.me/s/<channel>).

Telegram publishes a read-only web preview for public channels; this module
reads that page like a browser would (no login, no bot, no Telegram API) and
turns posts into leads. Private groups and channels without a preview (for
example t.me/offcampusjobs_4u, which is a group) cannot be read this way and
are reported as such - they need forwarding into the Notion Job Inbox.

Every lead is labelled with the channel's own trust signals (subscriber count,
how recently it posted) and the post's link-safety verdict. Telegram posts are
never applied to directly: the employer's page is found first.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from html.parser import HTMLParser

from .extract import extract_years

GOVT_WORDS = re.compile(
    r"(?i)\b(govt|government|sarkari|psu|public sector|ministry|advt\.?\s*no|"
    r"advertisement\s*no|bhel|bel\b|ecil|isro|drdo|c-?dac|nielit|nic\b|upsc|"
    r"ssc\b|rrb|railway|aai\b|ntpc|ongc|gail|iocl|hal\b|bsnl|c-?dot|stpi|cris\b|becil|"
    r"csir|iit|nit|iiit|iiser|aiims|esic|central university|high court|psc\b|tspsc|tgpsc|appsc|"
    r"nspcl|rfcl|concor|sebi|uidai|meity|cert-in)\b"
)
BANK_WORDS = re.compile(
    r"(?i)\b(bank|banking|ibps|sbi|rbi|nabard|sidbi|probationary officer|clerk|"
    r"\bpo\b|insurance|lic\b|niacl|exim)\b"
)
GOVT_CS_WORDS = re.compile(
    r"(?i)(computer science|computer engineering|\bcse\b|\bc\.s\.e\b|information technology|"
    r"\b(it|i\.t\.)\s*(officer|engineer|manager|/|,|\))|\(it\)|software|\bmca\b|programmer|"
    r"system analyst|cyber|data science|computer)"
)
GOVT_NOT_CS = re.compile(
    r"(?i)\b(constable|police|teacher|tgt|pgt|nursing|nurse|staff nurse|medical officer|"
    r"doctor|pharmacist|driver|peon|multi tasking staff|\bmts\b|agniveer|gd\b|army gd|"
    r"iti apprentice|apprentice|anganwadi|forest guard|home guard|sweeper|cook|"
    r"lab attendant|court clerk|stenographer|typist|accountant|librarian|fireman)\b"
)
PRIVATE_ROLE_WORDS = re.compile(
    r"(?i)\b(java|backend|back[- ]end|software engineer|software developer|sde|"
    r"member of technical staff|api engineer|platform engineer|python developer|"
    r"ai engineer|genai|gen ai|generative ai|llm|agentic|ml engineer|"
    r"application developer|integration engineer)\b"
)
EXCLUDE_ROLE_WORDS = re.compile(
    r"(?i)\b(intern|internship|trainee|apprentice|sales|bpo|voice process|customer support|"
    r"telecaller|data entry|tutor|teacher|content writer|hr recruiter|marketing|"
    r"work from home earn|part[- ]time)\b"
)
FRESHER_ONLY = re.compile(
    r"(?i)(\b(20(2[3-9]))\s*(batch|passouts?|pass[- ]outs?|graduates?)\b|"
    r"batch\s*:?\s*20(2[3-9])|entry[- ]level|graduate (engineer )?trainee|new grad|campus hire|\bfreshers?\b(?![^.]{0,40}\bexperienced\b)|0\s*[-–]\s*[12]\s*(years?|yrs?))"
)
KEY_LINE = re.compile(
    r"(?im)^\s*(?:[\W_]{0,3})\s*(company(?: name)?|organi[sz]ation|role|job role|position|"
    r"post(?: name)?|designation|job title|location|job location|experience|qualification|"
    r"eligibility|salary|package|ctc|last date|apply link|apply)\s*[:\-–]\s*(.+)$"
)


@dataclass
class TgPost:
    post_id: str  # "<channel>/<n>"
    url: str
    when: str  # ISO datetime
    text: str
    links: list[str] = field(default_factory=list)
    forwarded_from: str = ""

    @property
    def day(self) -> str:
        return self.when[:10]


@dataclass
class ChannelPage:
    handle: str
    title: str = ""
    subscribers: int | None = None
    kind: str = ""  # subscribers | members | ""
    has_preview: bool = False
    posts: list[TgPost] = field(default_factory=list)


def parse_count(text: str) -> int | None:
    t = text.strip().replace(" ", "").replace(",", "").upper()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KM]?)", t)
    if not m:
        return None
    mult = {"": 1, "K": 1_000, "M": 1_000_000}[m.group(2)]
    return int(float(m.group(1)) * mult)


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.posts: list[TgPost] = []
        self._cur: TgPost | None = None
        self._text_depth = 0  # >0 while inside tgme_widget_message_text
        self._div_depth = 0
        self._text_parts: list[str] = []
        self._in_fwd = False
        self.title = ""
        self._in_title = False
        self.counters: list[tuple[str, str]] = []
        self._counter_val: str | None = None
        self._in_counter: str = ""

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "div":
            self._div_depth += 1
            if "data-post" in a and "tgme_widget_message " in cls + " ":
                self._flush()
                handle_post = a["data-post"]
                self._cur = TgPost(
                    post_id=handle_post, url=f"https://t.me/{handle_post}", when="", text=""
                )
                self._text_parts = []
            elif (
                "tgme_widget_message_text" in cls and self._cur is not None and not self._text_depth
            ):
                self._text_depth = self._div_depth
            elif "tgme_channel_info_header_title" in cls:
                self._in_title = True
        if tag == "br" and self._text_depth:
            self._text_parts.append("\n")
        if tag == "a" and self._cur is not None:
            href = a.get("href") or ""
            if "tgme_widget_message_forwarded_from_name" in cls:
                self._in_fwd = True
            elif self._text_depth and href.startswith("http"):
                self._cur.links.append(html.unescape(href))
        if tag == "time" and self._cur is not None and a.get("datetime"):
            self._cur.when = a["datetime"]
        if tag == "span":
            if "counter_value" in cls:
                self._in_counter = "value"
            elif "counter_type" in cls:
                self._in_counter = "type"

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag == "div":
            if self._text_depth and self._div_depth == self._text_depth:
                self._text_depth = 0
                if self._cur is not None:
                    self._cur.text = "".join(self._text_parts).strip()
            if self._in_title:
                self._in_title = False
            self._div_depth -= 1
        if tag == "a":
            self._in_fwd = False
        if tag == "span":
            self._in_counter = ""

    def handle_data(self, data):  # noqa: ANN001
        if self._text_depth:
            self._text_parts.append(data)
        if self._in_fwd and self._cur is not None:
            self._cur.forwarded_from += data
        if self._in_title:
            self.title += data
        if self._in_counter == "value":
            self._counter_val = data
        elif self._in_counter == "type" and self._counter_val is not None:
            self.counters.append((self._counter_val, data.strip()))
            self._counter_val = None

    def _flush(self) -> None:
        if self._cur is not None:
            self.posts.append(self._cur)
        self._cur = None

    def close(self) -> None:
        super().close()
        self._flush()


def parse_channel(handle: str, page: str) -> ChannelPage:
    p = _Parser()
    p.feed(page)
    p.close()
    out = ChannelPage(handle=handle, title=p.title.strip(), posts=p.posts)
    for value, kind in p.counters:
        if kind in ("subscribers", "subscriber", "members", "member"):
            out.subscribers = parse_count(value)
            out.kind = "members" if kind.startswith("member") else "subscribers"
            break
    out.has_preview = bool(p.posts)
    if not out.title:
        m = re.search(r'<meta property="og:title" content="([^"]*)"', page)
        out.title = html.unescape(m.group(1)) if m else ""
    if out.subscribers is None:
        m = re.search(r"([\d\s.,]+[KM]?)\s*(subscribers|members)", page)
        if m:
            out.subscribers = parse_count(m.group(1))
            out.kind = m.group(2)
    return out


def channel_trust(ch: ChannelPage, today: date, min_subscribers: int) -> tuple[str, str]:
    """(HIGH|MEDIUM|LOW, reason) from public signals only. These signals say
    the channel is active and followed, not that each post is true."""
    if not ch.has_preview:
        return "LOW", "no public preview (private group or preview disabled)"
    last = max((p.day for p in ch.posts if p.when), default="")
    stale = not last or (today - date.fromisoformat(last)).days > 14
    subs = ch.subscribers or 0
    reason = f"{subs:,} {ch.kind or 'subscribers'}; last post {last or 'unknown'}"
    if stale:
        return "LOW", reason + " (inactive >14 days)"
    if subs >= max(min_subscribers, 10_000):
        return "HIGH", reason
    if subs >= min_subscribers:
        return "MEDIUM", reason
    return "LOW", reason + f" (under {min_subscribers:,})"


def fields(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in KEY_LINE.finditer(text):
        key = m.group(1).lower()
        key = {
            "company name": "company",
            "organisation": "company",
            "organization": "company",
            "job role": "role",
            "position": "role",
            "post": "role",
            "post name": "role",
            "designation": "role",
            "job title": "role",
            "job location": "location",
            "eligibility": "qualification",
            "package": "salary",
            "ctc": "salary",
            "apply link": "apply",
        }.get(key, key)
        out.setdefault(key, m.group(2).strip()[:200])
    return out


def guess_company_role(text: str) -> tuple[str, str]:
    f = fields(text)
    company, role = f.get("company", ""), f.get("role", "")
    first = next((ln.strip(" *•-🔥📢✅🚀") for ln in text.splitlines() if ln.strip()), "")
    if not company:
        m = re.search(
            r"(?i)^\W*([A-Z][\w&.,' -]{1,40}?)\s+(?:is\s+)?(?:hiring|recruitment|careers|off ?campus)",
            first,
        )
        if m:
            company = m.group(1).strip()
    if not role:
        m = re.search(
            r"(?i)(?:hiring|for|recruitment)\s*(?:for)?\s*[:\-–]?\s*([A-Za-z][\w /&+().-]{2,60})",
            first,
        )
        role = (m.group(1).strip() if m else first)[:80]
    return company[:60], role[:100]


_SEP = re.compile(r"\n\s*[-=_─]{8,}\s*\n")
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"')]+")


def split_blocks(post: TgPost) -> list[tuple[str, list[str]]]:
    """A digest post lists several jobs separated by rule lines. Each block
    is classified on its own, with only the links written inside it."""
    parts = [p for p in _SEP.split(post.text) if p.strip()]
    if len(parts) <= 1:
        return [(post.text, list(post.links))]
    out = []
    for part in parts:
        links = [u.rstrip(".,;") for u in _URL_IN_TEXT.findall(part)]
        out.append((part.strip(), links))
    return out


def classify(text: str) -> tuple[str, str]:
    """("govt" | "private" | "skip", reason)."""
    if not text.strip():
        return "skip", "empty post"
    if GOVT_WORDS.search(text):
        if BANK_WORDS.search(text):
            return "skip", "bank/insurance exam (excluded)"
        if GOVT_NOT_CS.search(text) and not re.search(
            r"(?i)computer science|\bcse\b|information technology|software", text
        ):
            return "skip", "government post outside CS/IT"
        if not GOVT_CS_WORDS.search(text):
            return "skip", "government post, no CS/IT signal"
        return "govt", ""
    if EXCLUDE_ROLE_WORDS.search(text[:300]):
        return "skip", "internship/non-engineering/part-time"
    if not PRIVATE_ROLE_WORDS.search(text):
        return "skip", "not a target role family"
    lo, _, _ = extract_years(text)
    if FRESHER_ONLY.search(text) and lo < 2:
        return "skip", "fresher/early-batch hiring"
    if lo and lo > 7:
        return "skip", f"asks {lo:.0f}+ years"
    return "private", ""


def first_external_link(post: TgPost | list[str]) -> str:
    links = post if isinstance(post, list) else post.links
    for link in links:
        host = re.sub(r"^https?://(www\.)?", "", link).split("/")[0].lower()
        if host not in ("t.me", "telegram.me", "telegram.org"):
            return link
    return ""


def post_age_days(post: TgPost, today: date) -> int:
    try:
        return (today - datetime.fromisoformat(post.when.replace("Z", "+00:00")).date()).days
    except ValueError:
        return 999
