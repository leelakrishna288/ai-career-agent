"""Tests for notion_resume_attachment - pytest-native, fake transport, no network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from career_agent.discovery import notion_resume_attachment as na

UPLOAD_OK = (
    200,
    {"id": "up_123", "upload_url": "https://api.notion.com/v1/file_uploads/up_123/send"},
)
SENT_OK = (200, {"id": "up_123", "status": "uploaded"})
PATCH_OK = (200, {"object": "page", "id": "page_1"})
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class FakeTransport:
    """Records calls and replays scripted (status, body) responses."""

    def __init__(self, script: list[tuple[int, Any]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, url: str, method: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        self.calls.append({"url": url, "method": method, "headers": headers, "body": body})
        status, payload = self.script.pop(0)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return status, raw


@pytest.fixture
def resume(tmp_path: Path) -> str:
    path = tmp_path / "bswift_AI_Engineer_II_2026-09-18.docx"
    path.write_bytes(b"PK\x03\x04 fake docx bytes")
    return str(path)


def test_happy_path_makes_three_correct_calls(resume: str) -> None:
    transport = FakeTransport([UPLOAD_OK, SENT_OK, PATCH_OK])
    upload_id = na.NotionAttacher("secret_tok", transport=transport).upload_resume("page_1", resume)

    assert upload_id == "up_123"
    assert len(transport.calls) == 3
    create, send, patch = transport.calls

    assert create["url"].endswith("/file_uploads")
    assert create["method"] == "POST"
    body = json.loads(create["body"])
    assert body["filename"].endswith(".docx")
    assert body["content_type"] == DOCX

    assert send["url"] == UPLOAD_OK[1]["upload_url"]
    boundary = send["headers"]["Content-Type"].split("boundary=")[1]
    assert send["body"].startswith(f"--{boundary}\r\n".encode())
    assert send["body"].endswith(f"\r\n--{boundary}--\r\n".encode())
    assert b'name="file"; filename="bswift_AI_Engineer_II_2026-09-18.docx"' in send["body"]
    assert b"PK\x03\x04 fake docx bytes" in send["body"]

    assert patch["method"] == "PATCH"
    assert patch["url"].endswith("/pages/page_1")
    prop = json.loads(patch["body"])["properties"]["Resume Attachment"]
    assert prop["files"][0]["type"] == "file_upload"
    assert prop["files"][0]["file_upload"]["id"] == "up_123"

    for call in transport.calls:
        assert call["headers"]["Notion-Version"] == "2022-06-28"
        assert call["headers"]["Authorization"] == "Bearer secret_tok"


def test_retries_429_with_backoff(resume: str, monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(na.time, "sleep", lambda s: slept.append(s))
    transport = FakeTransport([(429, {"message": "rate limited"}), UPLOAD_OK, SENT_OK, PATCH_OK])

    assert na.NotionAttacher("tok", transport=transport).upload_resume("page_1", resume) == "up_123"
    assert len(transport.calls) == 4
    assert slept == [1]


def test_4xx_is_not_retried(resume: str) -> None:
    transport = FakeTransport([(400, {"message": "content_type not supported"})])
    with pytest.raises(na.NotionAttachError, match="create_upload failed"):
        na.NotionAttacher("tok", transport=transport).upload_resume("page_1", resume)
    assert len(transport.calls) == 1


def test_non_uploaded_status_is_a_failure(resume: str) -> None:
    transport = FakeTransport([UPLOAD_OK, (200, {"id": "up_123", "status": "pending"})])
    with pytest.raises(na.NotionAttachError, match="expected 'uploaded'"):
        na.NotionAttacher("tok", transport=transport).upload_resume("page_1", resume)


def test_missing_file_refused_before_any_http(tmp_path: Path) -> None:
    transport = FakeTransport([])
    with pytest.raises(na.NotionAttachError, match="not found"):
        na.NotionAttacher("tok", transport=transport).upload_resume(
            "page_1", str(tmp_path / "nope.docx")
        )
    assert transport.calls == []


def test_empty_file_refused_before_any_http(tmp_path: Path) -> None:
    empty = tmp_path / "empty.docx"
    empty.write_bytes(b"")
    transport = FakeTransport([])
    with pytest.raises(na.NotionAttachError, match="is empty"):
        na.NotionAttacher("tok", transport=transport).upload_resume("page_1", str(empty))
    assert transport.calls == []


def test_empty_token_refused() -> None:
    with pytest.raises(na.NotionAttachError):
        na.NotionAttacher("")


def test_non_json_response() -> None:
    transport = FakeTransport([(200, b"<html>not json</html>")])
    with pytest.raises(na.NotionAttachError, match="non-JSON"):
        na.NotionAttacher("tok", transport=transport).create_upload("r.docx", DOCX)


def test_missing_upload_url() -> None:
    transport = FakeTransport([(200, {"id": "up_1"})])
    with pytest.raises(na.NotionAttachError, match="no id/upload_url"):
        na.NotionAttacher("tok", transport=transport).create_upload("r.docx", DOCX)


def test_plain_http_refused() -> None:
    with pytest.raises(na.NotionAttachError, match="non-https"):
        na._urllib_transport("http://api.notion.com/v1/pages", "GET", {}, None)


def test_property_name_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(na, "ATTACHMENT_PROPERTY", "Some Other Files")
    transport = FakeTransport([PATCH_OK])
    na.NotionAttacher("tok", transport=transport).attach("page_1", "up_9", "r.docx")
    assert "Some Other Files" in json.loads(transport.calls[0]["body"])["properties"]


@pytest.mark.parametrize(
    ("filename", "expected"),
    [("r.docx", DOCX), ("r.pdf", "application/pdf"), ("r.unknown", "application/octet-stream")],
)
def test_content_type_guessing(filename: str, expected: str) -> None:
    assert na.guess_content_type(filename) == expected
