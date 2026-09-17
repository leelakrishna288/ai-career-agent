"""Link and post safety checks for community job sources (Telegram, job blogs).

Deterministic checks always run. Two optional reputation checks run only when
their free API keys are configured as secrets:
  - Google Safe Browsing Lookup API v4   (GOOGLE_SAFE_BROWSING_KEY)
  - VirusTotal URL report v3             (VIRUSTOTAL_API_KEY; free tier 4/min)
Without a key the result says "not checked" - it never pretends a scan ran.

Verdicts:
  DANGEROUS  - never recorded as a job (malware/phishing hit, executable
               download, IP-address host, payment or fee request)
  SUSPICIOUS - recorded but flagged; never applied to without Leela
  CAUTION    - aggregator/blog or shortened link: the employer's own page must
               be found before applying
  OK         - https link on an employer ATS, company careers site or an
               official government domain
"""

from __future__ import annotations

import base64
import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .http import JsonClient

DANGEROUS = "DANGEROUS"
SUSPICIOUS = "SUSPICIOUS"
CAUTION = "CAUTION"
OK = "OK"
_ORDER = {OK: 0, CAUTION: 1, SUSPICIOUS: 2, DANGEROUS: 3}

SHORTENERS = {
    "bit.ly",
    "tinyurl.com",
    "cutt.ly",
    "rb.gy",
    "shorturl.at",
    "is.gd",
    "goo.gl",
    "tiny.cc",
    "rebrand.ly",
    "t.ly",
    "ow.ly",
    "buff.ly",
    "shorte.st",
    "adf.ly",
    "linktr.ee",
    "surl.li",
    "bitly.com",
    "v.gd",
    "clck.ru",
    "gplinks.co",
    "shrinkme.io",
}
RISKY_TLDS = {
    "xyz",
    "top",
    "click",
    "buzz",
    "icu",
    "tk",
    "ml",
    "ga",
    "cf",
    "gq",
    "work",
    "rest",
    "cam",
    "zip",
    "mov",
    "loan",
    "win",
    "bid",
    "review",
    "country",
    "kim",
    "party",
    "stream",
}
DOWNLOAD_EXT = re.compile(r"\.(apk|exe|msi|scr|bat|cmd|jar|zip|rar|7z|dmg|iso|vbs|ps1)(\?|$)", re.I)
TRUSTED_HOST_SUFFIXES = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "myworkdaysite.com",
    "smartrecruiters.com",
    "icims.com",
    "taleo.net",
    "successfactors.com",
    "successfactors.eu",
    "oraclecloud.com",
    "workable.com",
    "recruitee.com",
    "jobvite.com",
    "bamboohr.com",
    "gov.in",
    "nic.in",
    "ac.in",
    "edu.in",
    "res.in",
    "ernet.in",
    "org.in",
    "linkedin.com",
    "naukri.com",
    "indeed.com",
    "foundit.in",
    "instahyre.com",
    "cutshort.io",
    "hirist.tech",
    "wellfound.com",
    "naukrigulf.com",
    "bayt.com",
    "gulftalent.com",
    "accenture.com",
    "infosys.com",
    "tcs.com",
    "wipro.com",
    "ibm.com",
    "microsoft.com",
    "google.com",
    "amazon.jobs",
    "oracle.com",
    "deloitte.com",
    "capgemini.com",
    "cognizant.com",
    # Government / PSU organisations on non-.gov.in domains
    "cdac.in",
    "bel-india.in",
    "bhel.in",
    "bhel.com",
    "ecil.co.in",
    "stpi.in",
    "concorindia.co.in",
    "rfcl.co.in",
    "nspcl.co.in",
    "railtel.in",
    "becil.com",
    "aai.aero",
    "cdot.in",
    "ntpc.co.in",
    "bdl-india.in",
    "iith.ac.in",
    "iiit.ac.in",
    "sebi.gov.in",
    "isro.gov.in",
    "jobapply.in",
    "digialm.com",
    "ibtexamination.com",
)
# Job-blog / repost sites: usually genuine reposts, but the employer's own page
# must be found and used instead.
AGGREGATOR_HOSTS = {
    "offcampusjobs4u.com",
    "offcampusjobsindia.com",
    "freshersworld.com",
    "fresherslive.com",
    "freshersnow.com",
    "jobs4fresher.com",
    "job4freshers.co.in",
    "fresherjobsadda.in",
    "freejobalert.com",
    "sarkariresult.com",
    "indgovtjobs.in",
    "govtjobs.io",
    "kickcharm.com",
    "dotaware.com",
    "jobformore.com",
    "talentd.in",
    "careerpower.in",
    "testbook.com",
    "adda247.com",
    "sarkarinaukriblog.com",
    "recrenza.com",
}
MESSAGING_HOSTS = {"wa.me", "api.whatsapp.com", "chat.whatsapp.com", "t.me", "telegram.me"}
SCAM_TEXT = re.compile(
    r"(?i)(registration fee|security deposit|refundable deposit|processing fee|training fee|"
    r"pay\s*(rs\.?|₹|inr)\s*\d|payment of\s*(rs\.?|₹)|send money|upi id|"
    r"earn\s*(rs\.?|₹)?\s*\d+[k]?\s*(per|/)\s*(day|hour|task)|daily earning|"
    r"like and subscribe|task[- ]based (job|work)|investment required|guaranteed job|"
    r"100% placement guarantee|job guarantee|exam help|test help|"
    r"(coding|online) (test|exam|assessment)s?\s*(&|and)?\s*(interview)?\s*(help|assistance|support)|"
    r"interview help|proxy (interview|exam)|exam cleared successfully|"
    r"(assessment|exam) (solving|solutions?) service)"
)
SOFT_FLAGS = re.compile(
    r"(?i)(dm (me|for)|whatsapp (me|only|number)|limited seats|hurry up|only today|"
    r"paid promotion|premium membership|join our paid|lifetime access|launch offer)"
)


@dataclass
class SafetyResult:
    verdict: str = OK
    reasons: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)

    def raise_to(self, verdict: str, reason: str) -> None:
        if _ORDER[verdict] > _ORDER[self.verdict]:
            self.verdict = verdict
        self.reasons.append(reason)

    def summary(self) -> str:
        head = f"Link safety {self.verdict}"
        if self.reasons:
            head += ": " + "; ".join(dict.fromkeys(self.reasons))
        if self.checks:
            head += " [" + ", ".join(self.checks) + "]"
        return head


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _suffix_match(host: str, suffixes) -> bool:  # noqa: ANN001
    return any(host == s or host.endswith("." + s) for s in suffixes)


def check_text(text: str, result: SafetyResult | None = None) -> SafetyResult:
    result = result or SafetyResult()
    if m := SCAM_TEXT.search(text or ""):
        result.raise_to(DANGEROUS, f"payment/scam wording '{m.group(0)}'")
    if m := SOFT_FLAGS.search(text or ""):
        result.raise_to(SUSPICIOUS, f"pressure/promotion wording '{m.group(0)}'")
    return result


def check_url(url: str, result: SafetyResult | None = None) -> SafetyResult:
    result = result or SafetyResult()
    if not url:
        result.raise_to(SUSPICIOUS, "no application link")
        return result
    parts = urlsplit(url)
    host = host_of(url)
    if parts.scheme != "https":
        result.raise_to(SUSPICIOUS, "link is not https")
    if not host:
        result.raise_to(SUSPICIOUS, "link has no host")
        return result
    try:
        ipaddress.ip_address(host)
        result.raise_to(DANGEROUS, "link points at a bare IP address")
    except ValueError:
        pass
    if DOWNLOAD_EXT.search(parts.path):
        result.raise_to(DANGEROUS, "link downloads a program or archive")
    if host.startswith("xn--") or ".xn--" in host:
        result.raise_to(SUSPICIOUS, "internationalised (look-alike) domain")
    if host.rsplit(".", 1)[-1] in RISKY_TLDS:
        result.raise_to(SUSPICIOUS, f"high-abuse domain ending .{host.rsplit('.', 1)[-1]}")
    if host in SHORTENERS:
        result.raise_to(CAUTION, "shortened link hides the destination")
    elif host in MESSAGING_HOSTS:
        result.raise_to(SUSPICIOUS, "application goes through a chat app, not a careers site")
    elif _suffix_match(host, AGGREGATOR_HOSTS):
        result.raise_to(CAUTION, f"job-blog repost ({host}) - use the employer's own page")
    elif not _suffix_match(host, TRUSTED_HOST_SUFFIXES) and not re.search(
        r"(^|\.)(careers?|jobs?)\.", host
    ):
        result.raise_to(CAUTION, f"unrecognised domain {host} - verify it belongs to the employer")
    return result


class ReputationChecker:
    """Optional Safe Browsing / VirusTotal lookups. Each check is skipped (and
    said to be skipped) when its key is absent or the call fails."""

    def __init__(self, client: JsonClient, safe_browsing_key: str = "", virustotal_key: str = ""):
        self.client = client
        self.sb_key = safe_browsing_key
        self.vt_key = virustotal_key
        self._cache: dict[str, SafetyResult] = {}

    def check(self, url: str, result: SafetyResult) -> SafetyResult:
        if not url.startswith("https://"):
            return result
        if self.sb_key:
            self._safe_browsing(url, result)
        else:
            result.checks.append("Safe Browsing not configured")
        if self.vt_key:
            self._virustotal(url, result)
        else:
            result.checks.append("VirusTotal not configured")
        return result

    def _safe_browsing(self, url: str, result: SafetyResult) -> None:
        body: dict[str, Any] = {
            "client": {"clientId": "ai-career-agent", "clientVersion": "1.3"},
            "threatInfo": {
                "threatTypes": [
                    "MALWARE",
                    "SOCIAL_ENGINEERING",
                    "UNWANTED_SOFTWARE",
                    "POTENTIALLY_HARMFUL_APPLICATION",
                ],
                "platformTypes": ["ANY_PLATFORM"],
                "threatEntryTypes": ["URL"],
                "threatEntries": [{"url": url}],
            },
        }
        try:
            data = self.client.request(
                "POST",
                "https://safebrowsing.googleapis.com/v4/threatMatches:find",
                body=body,
                headers={"X-Goog-Api-Key": self.sb_key},
            )
        except Exception as exc:  # a failed lookup is reported, never treated as clean
            result.checks.append(f"Safe Browsing failed ({type(exc).__name__})")
            return
        if data and data.get("matches"):
            kinds = sorted({m.get("threatType", "?") for m in data["matches"]})
            result.raise_to(DANGEROUS, "Google Safe Browsing: " + ", ".join(kinds))
        result.checks.append(
            "Safe Browsing clean" if not (data and data.get("matches")) else "Safe Browsing HIT"
        )

    def _virustotal(self, url: str, result: SafetyResult) -> None:
        url_id = base64.urlsafe_b64encode(url.encode()).decode().strip("=")
        try:
            data = self.client.request(
                "GET",
                f"https://www.virustotal.com/api/v3/urls/{url_id}",
                headers={"x-apikey": self.vt_key},
            )
        except Exception as exc:
            result.checks.append(f"VirusTotal no report ({type(exc).__name__})")
            return
        stats = ((data or {}).get("data", {}).get("attributes", {}) or {}).get(
            "last_analysis_stats", {}
        )
        bad = int(stats.get("malicious", 0)) + int(stats.get("suspicious", 0))
        if int(stats.get("malicious", 0)) >= 2:
            result.raise_to(DANGEROUS, f"VirusTotal: {bad} engines flag this link")
        elif bad:
            result.raise_to(SUSPICIOUS, f"VirusTotal: {bad} engine(s) flag this link")
        result.checks.append(f"VirusTotal {bad} flags")


def assess(url: str, text: str = "", reputation: ReputationChecker | None = None) -> SafetyResult:
    result = check_text(text)
    check_url(url, result)
    if reputation and result.verdict != DANGEROUS:
        reputation.check(url, result)
    return result
