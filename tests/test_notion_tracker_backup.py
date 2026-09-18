"""Tests for notion_tracker_backup - pytest-native, no network, no module-level code."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

import notion_tracker_backup as backup


def make_page(i: int, archived: bool = False) -> dict[str, Any]:
    """A page shaped like the real tracker rows, covering every property type in use."""
    return {
        "id": f"p{i}",
        "url": f"https://notion.so/p{i}",
        "created_time": f"2026-09-{10 + i:02d}T00:00:00.000Z",
        "last_edited_time": "2026-09-17T00:00:00.000Z",
        "archived": archived,
        "properties": {
            "Company + Role": {"type": "title", "title": [{"plain_text": f"Dscout - SE #{i}"}]},
            "Company": {"type": "rich_text", "rich_text": [{"plain_text": 'Ac"me, Inc\nIndia'}]},
            "Application ID": {"type": "unique_id", "unique_id": {"prefix": None, "number": 100 + i}},
            "Match Score": {"type": "number", "number": 88.8},
            "Status": {"type": "select", "select": {"name": "RESUME_PREPARED"}},
            "Decision": {"type": "select", "select": None},
            "Date Found": {"type": "date", "date": {"start": "2026-09-18"}},
            "Job URL": {"type": "url", "url": "https://boards.greenhouse.io/x"},
            "Recruiter Email": {"type": "email", "email": None},
            "Resume Attachment": {"type": "files", "files": [{"name": f"R_{i}.docx"}]},
            "Created": {"type": "created_time", "created_time": "2026-09-17T10:00:00.000Z"},
        },
    }


def fake_query(batches: list[dict[str, Any]]):
    """Replay scripted API pages in order."""
    state = {"n": 0}

    def query(cursor: str | None) -> dict[str, Any]:
        batch = batches[state["n"]]
        state["n"] += 1
        return batch

    return query


@pytest.mark.parametrize(
    ("prop", "expected"),
    [
        ({"type": "title", "title": [{"plain_text": "Dscout"}]}, "Dscout"),
        ({"type": "rich_text", "rich_text": [{"plain_text": "a"}, {"plain_text": "b"}]}, "ab"),
        ({"type": "rich_text", "rich_text": []}, ""),
        ({"type": "number", "number": 88.8}, "88.8"),
        ({"type": "number", "number": None}, ""),
        ({"type": "select", "select": {"name": "APPLY"}}, "APPLY"),
        ({"type": "select", "select": None}, ""),
        ({"type": "status", "status": {"name": "READY"}}, "READY"),
        ({"type": "multi_select", "multi_select": [{"name": "Kafka"}, {"name": "AWS"}]}, "Kafka, AWS"),
        ({"type": "date", "date": {"start": "2026-09-18", "end": None}}, "2026-09-18"),
        (
            {"type": "date", "date": {"start": "2026-09-18", "end": "2026-09-20"}},
            "2026-09-18 -> 2026-09-20",
        ),
        ({"type": "date", "date": None}, ""),
        ({"type": "url", "url": "https://x/y"}, "https://x/y"),
        ({"type": "url", "url": None}, ""),
        ({"type": "email", "email": "a@b.com"}, "a@b.com"),
        ({"type": "checkbox", "checkbox": False}, "FALSE"),
        ({"type": "files", "files": [{"name": "r.docx", "file": {"url": "https://s3"}}]}, "r.docx"),
        ({"type": "files", "files": []}, ""),
        ({"type": "unique_id", "unique_id": {"prefix": None, "number": 118}}, "118"),
        ({"type": "unique_id", "unique_id": {"prefix": "APP", "number": 118}}, "APP118"),
        (
            {"type": "created_time", "created_time": "2026-09-17T10:00:00.000Z"},
            "2026-09-17T10:00:00.000Z",
        ),
        ({"type": "people", "people": [{"name": "Leela"}]}, "Leela"),
        ({"type": "relation", "relation": [{"id": "abc"}, {"id": "def"}]}, "abc, def"),
        ({"type": "formula", "formula": {"type": "string", "string": "x"}}, "x"),
        ({"type": "formula", "formula": {"type": "boolean", "boolean": True}}, "TRUE"),
        ({"type": "formula", "formula": {"type": "number", "number": 0}}, "0"),
        ({"type": "rollup", "rollup": {"type": "number", "number": 3}}, "3"),
        (
            {
                "type": "rollup",
                "rollup": {
                    "type": "array",
                    "array": [{"type": "number", "number": 1}, {"type": "number", "number": 2}],
                },
            },
            "1; 2",
        ),
        ({"type": "phone_number", "phone_number": None}, ""),
        ({"type": "some_future_type", "some_future_type": {"x": 1}}, ""),
        ("not-a-dict", ""),
    ],
)
def test_flatten_property(prop: Any, expected: str) -> None:
    assert backup.flatten_property(prop) == expected


def test_flatten_page_adds_metadata_columns() -> None:
    row = backup.flatten_page(make_page(1))
    assert row["_page_id"] == "p1"
    assert row["_archived"] == "FALSE"
    assert row["Company"] == 'Ac"me, Inc\nIndia'
    assert row["Application ID"] == "101"
    assert row["Resume Attachment"] == "R_1.docx"


def test_fetch_all_follows_cursors() -> None:
    pages = backup.fetch_all(
        fake_query(
            [
                {"results": [make_page(1), make_page(2)], "has_more": True, "next_cursor": "c1"},
                {"results": [make_page(3)], "has_more": False},
            ]
        )
    )
    assert len(pages) == 3


def test_fetch_all_caps_runaway_has_more(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup, "MAX_PAGES", 5)
    pages = backup.fetch_all(
        lambda c: {"results": [make_page(9)], "has_more": True, "next_cursor": "z"}
    )
    assert len(pages) == 5


def test_fetch_all_stops_when_cursor_missing() -> None:
    pages = backup.fetch_all(lambda c: {"results": [make_page(9)], "has_more": True})
    assert len(pages) == 1


def test_run_writes_csv_json_and_latest(tmp_path: Path) -> None:
    out = tmp_path / "backups"
    rc = backup.run(
        fake_query(
            [
                {"results": [make_page(1), make_page(2)], "has_more": True, "next_cursor": "c1"},
                {"results": [make_page(3)], "has_more": False},
            ]
        ),
        out_dir=str(out),
    )
    assert rc == 0

    csv_files = sorted(out.glob("tracker_*.csv"))
    assert len(csv_files) == 1
    assert (out / "latest.csv").exists()

    with csv_files[0].open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 3
    # commas, quotes and newlines inside a value survive the round trip
    assert rows[0]["Company"] == 'Ac"me, Inc\nIndia'
    assert rows[0]["Application ID"] == "101"

    header = (out / "latest.csv").read_text(encoding="utf-8").splitlines()[0].split(",")
    assert header[:2] == ["Application ID", "Company"]

    raw = json.loads(min(out.glob("tracker_*.json")).read_text(encoding="utf-8"))
    assert raw[0]["properties"]["Company"]["type"] == "rich_text"


def test_run_refuses_to_write_an_empty_backup(tmp_path: Path) -> None:
    out = tmp_path / "empty"
    rc = backup.run(fake_query([{"results": [], "has_more": False}]), out_dir=str(out))
    assert rc == 2
    assert not out.exists(), "an empty result must not overwrite or create a backup"


def test_run_keeps_archived_rows_and_flags_them(tmp_path: Path) -> None:
    out = tmp_path / "arch"
    backup.run(
        fake_query([{"results": [make_page(1), make_page(2, archived=True)], "has_more": False}]),
        out_dir=str(out),
    )
    with (out / "latest.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2
    assert rows[1]["_archived"] == "TRUE"


def test_main_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    assert backup.main() == 1
