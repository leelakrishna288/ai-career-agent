"""Minimal, dependency-free HTTP client with timeouts, retries and backoff.

Only public, documented JSON endpoints are called. No scraping, no login, no
CAPTCHA handling - if an endpoint refuses, the source is reported as failed and
the run carries on with the others.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import uuid
from typing import Any, Protocol

log = logging.getLogger(__name__)

USER_AGENT = "ai-career-agent/1.3 (+https://github.com/leelakrishna288/ai-career-agent)"


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url
        self.body = body[:500]


class JsonClient(Protocol):
    def request(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any: ...


class TextClient(Protocol):
    def get_text(self, url: str, headers: dict[str, str] | None = None) -> str: ...


class UrllibJsonClient:
    """JSON over HTTPS with bounded retries on 429/5xx and network errors."""

    def __init__(
        self, timeout: float = 20.0, retries: int = 3, backoff: float = 1.5, sleep=time.sleep
    ):  # noqa: ANN001
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._sleep = sleep

    def request(self, method, url, body=None, headers=None):  # noqa: ANN001, ANN201
        if not url.startswith("https://"):
            raise ValueError(f"refusing non-HTTPS URL: {url}")
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if data is not None:
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)  # noqa: S310
            try:
                # Scheme is checked above: only https:// URLs reach urlopen.
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310  # nosec B310
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace") if exc.fp else ""
                last = HttpError(exc.code, url, text)
                if exc.code == 429 or exc.code >= 500:
                    wait = _retry_after(exc) or self.backoff * (2**attempt)
                    log.warning("HTTP %s from %s, retrying in %.1fs", exc.code, url, wait)
                    self._sleep(wait)
                    continue
                raise last from None
            except (urllib.error.URLError, TimeoutError) as exc:
                last = exc
                wait = self.backoff * (2**attempt)
                log.warning("network error %s for %s, retrying in %.1fs", exc, url, wait)
                self._sleep(wait)
        if last is None:  # pragma: no cover - loop always runs at least once
            raise RuntimeError(f"request to {url} failed without an error")
        raise last

    # -- HTML/text pages (Telegram public previews, official recruitment pages) --
    def get_text(self, url: str, headers: dict[str, str] | None = None) -> str:  # noqa: D401
        if not url.startswith("https://"):
            raise ValueError(f"refusing non-HTTPS URL: {url}")
        hdrs = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
        hdrs.update(headers or {})
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, headers=hdrs, method="GET")  # noqa: S310
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310  # nosec B310
                    raw = resp.read(3_000_000)
                    charset = resp.headers.get_content_charset() or "utf-8"
                    return raw.decode(charset, "replace")
            except urllib.error.HTTPError as exc:
                last = HttpError(exc.code, url)
                if exc.code == 429 or exc.code >= 500:
                    self._sleep(_retry_after(exc) or self.backoff * (2**attempt))
                    continue
                raise last from None
            except (urllib.error.URLError, TimeoutError) as exc:
                last = exc
                self._sleep(self.backoff * (2**attempt))
        if last is None:  # pragma: no cover
            raise RuntimeError(f"request to {url} failed without an error")
        raise last

    def post_multipart(
        self,
        url: str,
        field: str,
        filename: str,
        content: bytes,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Single multipart/form-data POST (Notion file upload). No retries: a
        half-sent upload is simply abandoned and expires on Notion's side."""
        if not url.startswith("https://"):
            raise ValueError(f"refusing non-HTTPS URL: {url}")
        boundary = "----careeragent" + uuid.uuid4().hex
        safe_name = filename.replace('"', "")
        body = (
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field}"; filename="{safe_name}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
            + content
            + f"\r\n--{boundary}--\r\n".encode()
        )
        hdrs = {
            "User-Agent": USER_AGENT,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        hdrs.update(headers or {})
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310  # nosec B310
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace") if exc.fp else ""
            raise HttpError(exc.code, url, text) from None


def robots_allows(client: TextClient, url: str, agent: str = USER_AGENT) -> bool:
    """Honour robots.txt. A missing or unreadable robots.txt allows the fetch,
    which is the standard interpretation."""
    parts = urllib.parse.urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        text = client.get_text(robots_url)
    except HttpError as exc:
        return exc.status != 401 and exc.status != 403
    except Exception:  # network trouble reading robots.txt: do not guess "blocked"
        return True
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(text.splitlines())
    return rp.can_fetch(agent, url)


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    value = exc.headers.get("Retry-After") if exc.headers else None
    try:
        return float(value) if value else None
    except ValueError:
        return None
