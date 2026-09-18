"""Tests for notion_resume_attachment - no network, fake transport."""
from __future__ import annotations

import json
import os
import tempfile

import notion_resume_attachment as na


class FakeTransport:
    """Records calls and replays a scripted list of (status, body) responses."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, url, method, headers, body):
        self.calls.append({"url": url, "method": method, "headers": headers, "body": body})
        status, payload = self.script.pop(0)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return status, raw


UPLOAD_OK = (200, {"id": "up_123", "upload_url": "https://api.notion.com/v1/file_uploads/up_123/send"})
SENT_OK = (200, {"id": "up_123", "status": "uploaded"})
PATCH_OK = (200, {"object": "page", "id": "page_1"})


def resume_file(content=b"PK\x03\x04 fake docx bytes", name="bswift_AI_Engineer_II_2026-09-18.docx"):
    d = tempfile.mkdtemp()
    p = os.path.join(d, name)
    with open(p, "wb") as fh:
        fh.write(content)
    return p


def test_happy_path():
    t = FakeTransport([UPLOAD_OK, SENT_OK, PATCH_OK])
    a = na.NotionAttacher("secret_tok", transport=t)
    upload_id = a.upload_resume("page_1", resume_file())
    assert upload_id == "up_123"
    assert len(t.calls) == 3

    c1, c2, c3 = t.calls
    assert c1["url"].endswith("/file_uploads") and c1["method"] == "POST"
    b1 = json.loads(c1["body"])
    assert b1["filename"].endswith(".docx")
    assert b1["content_type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ), b1["content_type"]

    assert c2["url"] == UPLOAD_OK[1]["upload_url"]
    assert c2["headers"]["Content-Type"].startswith("multipart/form-data; boundary=----notion")
    boundary = c2["headers"]["Content-Type"].split("boundary=")[1]
    assert c2["body"].startswith(f"--{boundary}\r\n".encode())
    assert c2["body"].endswith(f"\r\n--{boundary}--\r\n".encode())
    assert b'name="file"; filename="bswift_AI_Engineer_II_2026-09-18.docx"' in c2["body"]
    assert b"PK\x03\x04 fake docx bytes" in c2["body"]

    assert c3["method"] == "PATCH" and c3["url"].endswith("/pages/page_1")
    prop = json.loads(c3["body"])["properties"]["Resume Attachment"]
    assert prop["files"][0]["type"] == "file_upload"
    assert prop["files"][0]["file_upload"]["id"] == "up_123"
    for c in t.calls:
        assert c["headers"]["Notion-Version"] == "2022-06-28"
        assert c["headers"]["Authorization"] == "Bearer secret_tok"
    print("happy path OK - 3 calls, multipart well-formed, property shape correct")


def test_retry_then_success():
    t = FakeTransport([(429, {"message": "rate limited"}), UPLOAD_OK, SENT_OK, PATCH_OK])
    na.MAX_ATTEMPTS, orig = 4, na.MAX_ATTEMPTS
    import time as _t

    slept = []
    real_sleep, _t.sleep = _t.sleep, lambda s: slept.append(s)
    try:
        a = na.NotionAttacher("tok", transport=t)
        assert a.upload_resume("page_1", resume_file()) == "up_123"
    finally:
        _t.sleep = real_sleep
        na.MAX_ATTEMPTS = orig
    assert len(t.calls) == 4 and slept == [1], (len(t.calls), slept)
    print("retry OK - 429 retried with backoff, then succeeded")


def test_hard_failure_not_retried():
    t = FakeTransport([(400, {"message": "content_type not supported"})])
    a = na.NotionAttacher("tok", transport=t)
    try:
        a.upload_resume("page_1", resume_file())
    except na.NotionAttachError as exc:
        assert "create_upload failed" in str(exc) and "400" in str(exc)
        assert len(t.calls) == 1, "a 400 must not be retried"
        print("hard failure OK - 400 raised immediately, error text carries the reason")
        return
    raise AssertionError("expected NotionAttachError")


def test_upload_status_not_uploaded():
    t = FakeTransport([UPLOAD_OK, (200, {"id": "up_123", "status": "pending"})])
    a = na.NotionAttacher("tok", transport=t)
    try:
        a.upload_resume("page_1", resume_file())
    except na.NotionAttachError as exc:
        assert "expected 'uploaded'" in str(exc)
        print("status guard OK - a non-'uploaded' status is a failure, not a silent pass")
        return
    raise AssertionError("expected NotionAttachError")


def test_missing_and_empty_file():
    a = na.NotionAttacher("tok", transport=FakeTransport([]))
    for path, expect in ((os.path.join(tempfile.mkdtemp(), "nope.docx"), "not found"),
                         (resume_file(content=b""), "is empty")):
        try:
            a.upload_resume("page_1", path)
            raise AssertionError("expected NotionAttachError")
        except na.NotionAttachError as exc:
            assert expect in str(exc), str(exc)
    print("input guards OK - missing file and zero-byte file both refuse before any HTTP call")


def test_no_token_and_bad_response():
    try:
        na.NotionAttacher("")
        raise AssertionError("expected NotionAttachError")
    except na.NotionAttachError:
        pass
    t = FakeTransport([(200, b"<html>not json</html>")])
    try:
        na.NotionAttacher("tok", transport=t).create_upload("r.docx", "application/pdf")
        raise AssertionError("expected NotionAttachError")
    except na.NotionAttachError as exc:
        assert "non-JSON" in str(exc)
    t = FakeTransport([(200, {"id": "up_1"})])  # upload_url missing
    try:
        na.NotionAttacher("tok", transport=t).create_upload("r.docx", "application/pdf")
        raise AssertionError("expected NotionAttachError")
    except na.NotionAttachError as exc:
        assert "no id/upload_url" in str(exc)
    print("response guards OK - empty token, non-JSON body and missing upload_url all raise")


def test_non_https_refused():
    try:
        na._urllib_transport("http://api.notion.com/v1/pages", "GET", {}, None)
        raise AssertionError("expected NotionAttachError")
    except na.NotionAttachError as exc:
        assert "non-https" in str(exc)
    print("transport guard OK - plain http refused")


def test_property_name_override():
    orig = na.ATTACHMENT_PROPERTY
    na.ATTACHMENT_PROPERTY = "Some Other Files"
    try:
        t = FakeTransport([PATCH_OK])
        na.NotionAttacher("tok", transport=t).attach("page_1", "up_9", "r.docx")
        assert "Some Other Files" in json.loads(t.calls[0]["body"])["properties"]
    finally:
        na.ATTACHMENT_PROPERTY = orig
    print("property override OK - RESUME_ATTACHMENT_PROPERTY is honoured")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\nALL {len(tests)} ATTACHMENT TESTS PASS")
