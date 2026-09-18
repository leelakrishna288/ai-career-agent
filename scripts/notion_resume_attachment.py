"""Upload a tailored resume to Notion and attach it to a tracker row.

Fixes the defect found on 2026-09-17: the Application Tracker had no
`files` property, so the daily run's DOCX upload had nowhere to land and the
row only ever received the resume text. The property
`Resume Attachment` (type `file`) was added to data source
790bcdd9-8ff0-445c-b64d-2e1064b4de1e on 2026-09-17; this is the writer for it.

Three Notion calls, in order:
  1. POST /v1/file_uploads            -> {id, upload_url}
  2. POST <upload_url>  multipart     -> status "uploaded"
  3. PATCH /v1/pages/{page_id}        -> sets the files property to that upload

Stdlib only. The HTTP transport is injected, so every branch is testable
without network access.

NOT YET VERIFIED AGAINST THE LIVE API - this sandbox cannot reach
api.notion.com. The first real run is the test. Failure modes to expect:
a 400 on step 1 if the content type is rejected, and a 413/400 on step 2 if
the file exceeds the workspace's per-file limit (5 MiB on free workspaces).
Both are reported, never swallowed.
"""
from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from typing import Any

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
ATTACHMENT_PROPERTY = os.environ.get("RESUME_ATTACHMENT_PROPERTY", "Resume Attachment")
RETRY_STATUS = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 4

# (url, method, headers, body) -> (status, bytes)
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class NotionAttachError(RuntimeError):
    """Raised when a step fails in a way that is not worth retrying."""


def _urllib_transport(
    url: str, method: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, bytes]:
    if not url.startswith("https://"):
        raise NotionAttachError(f"refusing a non-https URL: {url}")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def build_multipart(filename: str, content: bytes, content_type: str) -> tuple[bytes, str]:
    """Return (body, boundary) for a single-file multipart/form-data upload."""
    boundary = f"----notion{uuid.uuid4().hex}"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + content + tail, boundary


def guess_content_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    if guessed:
        return guessed
    if filename.endswith(".docx"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return "application/octet-stream"


class NotionAttacher:
    def __init__(self, token: str, transport: Transport | None = None) -> None:
        if not token:
            raise NotionAttachError("NOTION_TOKEN is empty")
        self._token = token
        self._transport = transport or _urllib_transport

    # ------------------------------------------------------------------ #
    def _call(
        self,
        url: str,
        method: str,
        headers: dict[str, str],
        body: bytes | None,
        step: str,
    ) -> dict[str, Any]:
        last = ""
        for attempt in range(MAX_ATTEMPTS):
            status, raw = self._transport(url, method, headers, body)
            if 200 <= status < 300:
                if not raw:
                    return {}
                try:
                    return json.loads(raw.decode())
                except json.JSONDecodeError as exc:
                    raise NotionAttachError(f"{step}: non-JSON response: {raw[:200]!r}") from exc
            detail = raw.decode("utf-8", "replace")[:300]
            last = f"HTTP {status}: {detail}"
            if status in RETRY_STATUS and attempt < MAX_ATTEMPTS - 1:
                time.sleep(2**attempt)
                continue
            raise NotionAttachError(f"{step} failed - {last}")
        raise NotionAttachError(f"{step} failed after {MAX_ATTEMPTS} attempts - {last}")

    def _json_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ #
    def create_upload(self, filename: str, content_type: str) -> tuple[str, str]:
        body = json.dumps({"filename": filename, "content_type": content_type}).encode()
        data = self._call(
            f"{NOTION_API}/file_uploads", "POST", self._json_headers(), body, "create_upload"
        )
        upload_id, upload_url = data.get("id"), data.get("upload_url")
        if not upload_id or not upload_url:
            raise NotionAttachError(f"create_upload: no id/upload_url in response: {data}")
        return upload_id, upload_url

    def send_bytes(self, upload_url: str, filename: str, content: bytes, content_type: str) -> None:
        body, boundary = build_multipart(filename, content, content_type)
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        data = self._call(upload_url, "POST", headers, body, "send_bytes")
        status = data.get("status")
        if status and status != "uploaded":
            raise NotionAttachError(f"send_bytes: upload status is {status!r}, expected 'uploaded'")

    def attach(self, page_id: str, upload_id: str, filename: str) -> dict[str, Any]:
        payload = {
            "properties": {
                ATTACHMENT_PROPERTY: {
                    "files": [
                        {
                            "type": "file_upload",
                            "name": filename,
                            "file_upload": {"id": upload_id},
                        }
                    ]
                }
            }
        }
        return self._call(
            f"{NOTION_API}/pages/{page_id}",
            "PATCH",
            self._json_headers(),
            json.dumps(payload).encode(),
            "attach",
        )

    # ------------------------------------------------------------------ #
    def upload_resume(self, page_id: str, path: str) -> str:
        """Upload `path` and attach it to `page_id`. Returns the upload id.

        Raises NotionAttachError on any failure. The caller must treat a
        failure as "resume text only" and must not mark the row READY on the
        assumption that an attachment exists (SYSTEM_SPEC v1.8 13.3).
        """
        if not os.path.isfile(path):
            raise NotionAttachError(f"resume file not found: {path}")
        with open(path, "rb") as fh:
            content = fh.read()
        if not content:
            raise NotionAttachError(f"resume file is empty: {path}")
        filename = os.path.basename(path)
        content_type = guess_content_type(filename)

        upload_id, upload_url = self.create_upload(filename, content_type)
        self.send_bytes(upload_url, filename, content, content_type)
        self.attach(page_id, upload_id, filename)
        return upload_id
