"""Turn a raw posting into a structured JobPosting, deterministically.

Nothing here guesses. Years are read only from explicit phrases, skills only
from a fixed vocabulary, work mode only from explicit words. Anything not
found stays empty/UNKNOWN - the scorer treats missing data as neutral rather
than inventing it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import JobPosting, WorkMode
from ..scoring import AI_TERMS, BACKEND_TERMS, CRITICAL, LEARNABLE
from .sources import RawPosting

EXTRA_TERMS = {
    "aws",
    "azure",
    "gcp",
    "google cloud",
    "spring",
    "hibernate",
    "jpa",
    "kafka",
    "rabbitmq",
    "graphql",
    "grpc",
    "mysql",
    "mongodb",
    "elasticsearch",
    "redis",
    "terraform",
    "jenkins",
    "github actions",
    "git",
    "linux",
    "typescript",
    "javascript",
    "node.js",
    "react",
    "angular",
    "go",
    "golang",
    "rust",
    "scala",
    "c++",
    "c#",
    ".net",
    "kubernetes",
    "docker",
    "microservices",
    "system design",
    "distributed systems",
    "oauth",
    "oidc",
    "security",
    "sast",
    "dast",
    "pl/sql",
    "unit testing",
    "tdd",
    "agile",
    "machine learning",
    "deep learning",
    "pytorch",
    "tensorflow",
    "mlops",
    "spark",
    "airflow",
    "data engineering",
    "nlp",
    "openapi",
    "swagger",
    "observability",
    "prometheus",
    "grafana",
    "opentelemetry",
    "java 17",
    "java 21",
    "multithreading",
    "concurrency",
}
VOCAB = sorted(AI_TERMS | BACKEND_TERMS | LEARNABLE | CRITICAL | EXTRA_TERMS, key=len, reverse=True)
_SHORT_AMBIGUOUS = {
    "go",
    "rest",
    "api",
    "agent",
    "agents",
    "evaluation",
    "security",
    "git",
    "sql",
    "backend",
}

_PREFERRED_HEAD = re.compile(
    r"(?im)^\s*[-*]?\s*(preferred|nice[- ]to[- ]have|bonus|good to have|pluses|"
    r"what would be great|additional (skills|qualifications)|it would be great)"
)
_YEARS = re.compile(
    r"(?i)(\d{1,2})\s*(?:\+|plus)?\s*(?:-|–|to)?\s*(\d{1,2})?\s*\+?\s*"
    r"(?:years?|yrs?)(?:\s+of)?(?:\s+\w+){0,4}?\s+(?:experience|exp\b)"
)
_MIN_YEARS_ALT = re.compile(
    r"(?i)(?:minimum|at least|min\.?)\s+(?:of\s+)?(\d{1,2})\s*(?:years?|yrs?)"
)

COUNTRY_WORDS = {
    "India": (
        "india",
        "hyderabad",
        "bangalore",
        "bengaluru",
        "chennai",
        "pune",
        "mumbai",
        "gurgaon",
        "gurugram",
        "noida",
        "delhi",
        "kolkata",
        "ahmedabad",
        "kochi",
    ),
    "UAE": ("uae", "united arab emirates", "dubai", "abu dhabi", "sharjah"),
    "Qatar": ("qatar", "doha"),
    "Saudi Arabia": ("saudi", "riyadh", "jeddah", "dammam", "ksa"),
    "USA": (
        "united states",
        "usa",
        "u.s.",
        ", ca",
        ", ny",
        "new york",
        "san francisco",
        "seattle",
        "austin",
        "boston",
        "chicago",
        "remote - us",
        "remote, us",
        "us-remote",
    ),
    "Other": (
        "brazil",
        "sao paulo",
        "são paulo",
        "canada",
        "toronto",
        "vancouver",
        "montreal",
        "argentina",
        "buenos aires",
        "mexico",
        "colombia",
        "bogota",
        "chile",
        "peru",
        "hong kong",
        "china",
        "shanghai",
        "beijing",
        "singapore",
        "japan",
        "tokyo",
        "korea",
        "seoul",
        "australia",
        "sydney",
        "melbourne",
        "new zealand",
        "philippines",
        "manila",
        "vietnam",
        "indonesia",
        "malaysia",
        "kuala lumpur",
        "thailand",
        "bangkok",
        "israel",
        "tel aviv",
        "south africa",
        "nigeria",
        "kenya",
        "egypt",
        "cairo",
        "turkey",
        "istanbul",
        "pakistan",
        "bangladesh",
        "sri lanka",
        "costa rica",
        "uruguay",
        "latam",
        "apac",
        "taiwan",
    ),
    "Europe": (
        "london",
        "united kingdom",
        "uk",
        "germany",
        "berlin",
        "munich",
        "paris",
        "france",
        "amsterdam",
        "netherlands",
        "dublin",
        "ireland",
        "spain",
        "madrid",
        "barcelona",
        "poland",
        "warsaw",
        "portugal",
        "lisbon",
        "europe",
        "emea",
        "sweden",
        "stockholm",
        "switzerland",
        "zurich",
        "italy",
        "milan",
    ),
}
_US_TOKEN = re.compile(r"(^|[\s,(|])(us|u\.s\.a?)(\s*[-,)|]|$)")
ISO_COUNTRY = {
    "IN": "India",
    "AE": "UAE",
    "QA": "Qatar",
    "SA": "Saudi Arabia",
    "US": "USA",
    "GB": "Europe",
    "DE": "Europe",
    "FR": "Europe",
    "NL": "Europe",
    "IE": "Europe",
    "ES": "Europe",
    "PL": "Europe",
    "PT": "Europe",
}


@dataclass
class Extracted:
    job: JobPosting
    country: str  # Notion Country select value or "Other"/"" when unknown
    work_mode_label: str  # Notion Work Mode select value
    experience_text: str
    extra_locations: list[str] = field(default_factory=list)


def _has_term(text: str, term: str) -> bool:
    """Whole-term match. Plain substring matching made "rust" fire on "trust"
    and "scala" on "scalable" in live postings, inventing critical gaps."""
    return re.search(rf"(?<![\w#+]){re.escape(term)}(?![\w#+])", text) is not None


def extract_skills(description: str) -> tuple[list[str], list[str]]:
    """(required, preferred) vocabulary terms, split at a 'preferred' heading."""
    text = description.lower()
    m = _PREFERRED_HEAD.search(description)
    req_text, pref_text = (text[: m.start()], text[m.start() :]) if m else (text, "")
    required: list[str] = []
    preferred: list[str] = []
    for term in VOCAB:
        if term in _SHORT_AMBIGUOUS and term not in ("sql", "git"):  # generic words, not skills
            continue  # too generic to count as a named requirement
        in_req, in_pref = _has_term(req_text, term), _has_term(pref_text, term)
        if in_req and not any(term in r for r in required):
            required.append(term)
        elif in_pref and not any(term in p for p in preferred + required):
            preferred.append(term)
    return required[:25], preferred[:15]


def extract_years(description: str) -> tuple[float, float, str]:
    found = []
    for m in _YEARS.finditer(description):
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else 0
        if 0 < lo <= 25:
            found.append((lo, hi, m.group(0)))
    for m in _MIN_YEARS_ALT.finditer(description):
        lo = int(m.group(1))
        if 0 < lo <= 25:
            found.append((lo, 0, m.group(0)))
    if not found:
        return 0.0, 0.0, ""
    # The smallest explicit minimum is the one that gates eligibility; larger
    # numbers usually describe a specific sub-skill ("8 years of Java").
    lo, hi, phrase = min(found, key=lambda f: f[0])
    return float(lo), float(hi), phrase.strip()


def classify_country(raw: RawPosting) -> str:
    if raw.country_hint:
        hint = raw.country_hint.strip()
        if hint.upper() in ISO_COUNTRY:
            return ISO_COUNTRY[hint.upper()]
        for name, words in COUNTRY_WORDS.items():
            if any(w in hint.lower() for w in words):
                return name
    blob = " | ".join([raw.location, *raw.extra_locations]).lower()
    hits = [name for name, words in COUNTRY_WORDS.items() if any(w in blob for w in words)]
    if _US_TOKEN.search(blob) and "USA" not in hits:
        hits.append("USA")
    if "India" in hits:
        return "India"  # an India location anywhere in the list makes it reachable
    for pref in ("UAE", "Qatar", "Saudi Arabia"):
        if pref in hits:
            return pref
    if hits:
        return hits[0]
    if re.search(r"\b(anywhere|worldwide|global)\b", blob):
        return "Remote-Worldwide"
    return ""


def classify_work_mode(raw: RawPosting, country: str) -> tuple[WorkMode, str]:
    """Hybrid is never reported as remote. Unknown stays Unknown."""
    blob = f"{raw.location} {raw.title} {' '.join(raw.extra_locations)}".lower()
    hint = raw.workplace_hint.lower()
    desc = raw.description.lower()
    if hint == "hybrid" or "hybrid" in blob or re.search(r"\bhybrid\b", desc[:1500]):
        return WorkMode.HYBRID, "Hybrid"
    remote = hint == "remote" or "remote" in blob
    if remote:
        if country == "Remote-Worldwide" or re.search(r"\b(anywhere|worldwide)\b", blob):
            return WorkMode.REMOTE_WORLDWIDE, "Remote-Worldwide"
        if country == "India":
            return WorkMode.REMOTE_COUNTRY, "Remote-India"
        if re.search(r"\b(time ?zone|est|pst|cet|overlap)\b", desc):
            return WorkMode.REMOTE_COUNTRY, "Remote-Timezone-Restricted"
        return WorkMode.REMOTE_COUNTRY, "Remote-Country-Specific"
    if hint in ("onsite", "on-site") or re.search(r"\b(on-?site|in[- ]office)\b", blob):
        return WorkMode.ONSITE, "Onsite"
    return WorkMode.UNKNOWN, "Unknown"


def to_job_posting(raw: RawPosting) -> Extracted:
    required, preferred = extract_skills(raw.description)
    lo, hi, phrase = extract_years(raw.description)
    country = classify_country(raw)
    mode, mode_label = classify_work_mode(raw, country)
    job = JobPosting(
        company=raw.company,
        role=raw.title,
        location=raw.location,
        country=country,
        work_mode=mode,
        url=raw.url,
        source=raw.platform,
        posted=raw.posted,
        min_years=lo,
        max_years=hi,
        salary_text=raw.salary_text,
        required_skills=required,
        preferred_skills=preferred,
        description=raw.description[:6000],
    )
    return Extracted(
        job=job,
        country=country,
        work_mode_label=mode_label,
        experience_text=phrase,
        extra_locations=list(raw.extra_locations),
    )
