"""Back up the Notion Application Tracker to CSV + raw JSON in this repository.

Read-only against Notion. Writes:
    backups/tracker/tracker_<YYYY-MM-DD>.csv
    backups/tracker/tracker_<YYYY-MM-DD>.json   (raw API pages, lossless)
    backups/tracker/latest.csv

Why: the Notion tracker is the only copy of the application history
(117 rows as of 2026-09-17). A bulk edit or a bad run is currently
unrecoverable. SYSTEM_SPEC v1.8 §13.6.

Stdlib only, so the workflow needs no dependency install.
Notion API version 2022-06-28 (stable `databases/{id}/query` endpoint).
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

NOTION_VERSION = "2022-06-28"
DATABASE_ID = os.environ.get("TRACKER_DATABASE_ID", "a6978c04-f98a-4736-bc11-3c2969a1997e")
OUT_DIR = os.environ.get("BACKUP_DIR", "backups/tracker")
PAGE_SIZE = 100
MAX_PAGES = 100  # 10,000 rows ceiling; guards against a has_more loop

# Column order for human readability; anything else is appended alphabetically-by-arrival.
PREFERRED = [
    "Application ID", "Company", "Role", "Status", "Decision", "Purpose",
    "Match Score", "Match Class", "ATS Estimate", "Technical Match",
    "Experience Match", "AI Match", "Company Trust Score", "Company Verdict",
    "Platform", "Work Mode", "Country", "Location", "Date Found", "Date Posted",
    "Applied Date", "Deadline", "Job URL", "Canonical URL", "Official Company URL",
    "Resume Version", "Resume File", "Resume Attachment", "Missing Skills",
    "Next Action", "Notes",
]


# --------------------------------------------------------------------------- #
# property flattening
# --------------------------------------------------------------------------- #
def _plain(items: list[dict[str, Any]] | None) -> str:
    return "".join(i.get("plain_text", "") for i in (items or []))


def flatten_property(prop: Any) -> str:
    if not isinstance(prop, dict):
        return ""
    ptype = prop.get("type", "")
    val = prop.get(ptype)

    if ptype in ("title", "rich_text"):
        return _plain(val)
    if ptype == "number":
        return "" if val is None else str(val)
    if ptype in ("select", "status"):
        return (val or {}).get("name", "") if isinstance(val, dict) else ""
    if ptype == "multi_select":
        return ", ".join(o.get("name", "") for o in (val or []))
    if ptype == "date":
        if not val:
            return ""
        start, end = val.get("start") or "", val.get("end") or ""
        return f"{start} -> {end}" if end else start
    if ptype in ("url", "email", "phone_number"):
        return val or ""
    if ptype == "checkbox":
        return "TRUE" if val else "FALSE"
    if ptype == "files":
        names = [
            (f.get("name") or (f.get("external") or {}).get("url") or "")
            for f in (val or [])
        ]
        return ", ".join(n for n in names if n)
    if ptype == "unique_id":
        if not val:
            return ""
        prefix, num = val.get("prefix") or "", val.get("number")
        return f"{prefix}{num}" if prefix else ("" if num is None else str(num))
    if ptype in ("created_time", "last_edited_time"):
        return val or ""
    if ptype == "people":
        return ", ".join(p.get("name") or p.get("id", "") for p in (val or []))
    if ptype in ("created_by", "last_edited_by"):
        return (val or {}).get("name") or (val or {}).get("id", "") if isinstance(val, dict) else ""
    if ptype == "relation":
        return ", ".join(r.get("id", "") for r in (val or []))
    if ptype == "formula":
        if not val:
            return ""
        ftype = val.get("type", "")
        inner = val.get(ftype)
        if ftype == "date":
            return (inner or {}).get("start", "") if inner else ""
        if isinstance(inner, bool):
            return "TRUE" if inner else "FALSE"
        return "" if inner is None else str(inner)
    if ptype == "rollup":
        if not val:
            return ""
        rtype = val.get("type", "")
        if rtype == "number":
            n = val.get("number")
            return "" if n is None else str(n)
        if rtype == "array":
            return "; ".join(flatten_property(x) for x in val.get("array") or [])
        if rtype == "date":
            return (val.get("date") or {}).get("start", "")
        return ""
    return ""


def flatten_page(page: dict[str, Any]) -> dict[str, str]:
    row = {
        "_page_id": page.get("id", ""),
        "_page_url": page.get("url", ""),
        "_created_time": page.get("created_time", ""),
        "_last_edited_time": page.get("last_edited_time", ""),
        "_archived": "TRUE" if page.get("archived") else "FALSE",
    }
    for name, prop in (page.get("properties") or {}).items():
        row[name] = flatten_property(prop)
    return row


# --------------------------------------------------------------------------- #
# fetch + assemble
# --------------------------------------------------------------------------- #
def fetch_all(query_fn: Callable[[str | None], dict[str, Any]]) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    cursor, calls = None, 0
    while True:
        data = query_fn(cursor)
        pages.extend(data.get("results", []))
        calls += 1
        if not data.get("has_more") or calls >= MAX_PAGES:
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return pages


def to_csv(pages: list[dict[str, Any]]) -> tuple[str, int, list[str]]:
    rows = [flatten_page(p) for p in pages]
    keys: list[str] = []
    seen: set[str] = set()
    for k in PREFERRED:
        if any(k in r for r in rows):
            keys.append(k)
            seen.add(k)
    for r in rows:
        for k in r:
            if k not in seen:
                keys.append(k)
                seen.add(k)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for r in sorted(rows, key=lambda x: x.get("_created_time", "")):
        writer.writerow(r)
    return buf.getvalue(), len(rows), keys


def notion_query(token: str) -> Callable[[str | None], dict[str, Any]]:
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    if not url.startswith("https://api.notion.com/"):  # scheme/host pinned, see nosec below
        raise ValueError(f"refusing to call a non-Notion URL: {url}")
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    def query(cursor: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"page_size": PAGE_SIZE}
        if cursor:
            body["start_cursor"] = cursor
        payload = json.dumps(body).encode()
        last_err: Exception | None = None
        for attempt in range(4):
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc:  # 429 / 5xx are worth retrying
                last_err = exc
                if exc.code in (429, 500, 502, 503, 504):
                    time.sleep(2 ** attempt)
                    continue
                detail = exc.read().decode("utf-8", "replace")[:300]
                raise RuntimeError(f"Notion HTTP {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                last_err = exc
                time.sleep(2 ** attempt)
        raise RuntimeError(f"Notion query failed after retries: {last_err}")

    return query


def run(query_fn: Callable[[str | None], dict[str, Any]], out_dir: str = OUT_DIR) -> int:
    pages = fetch_all(query_fn)
    if not pages:
        print("ERROR: 0 rows returned. Refusing to write an empty backup "
              "(it would look like data loss).", file=sys.stderr)
        return 2

    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).date().isoformat()
    csv_text, count, keys = to_csv(pages)

    csv_path = os.path.join(out_dir, f"tracker_{stamp}.csv")
    json_path = os.path.join(out_dir, f"tracker_{stamp}.json")
    latest_path = os.path.join(out_dir, "latest.csv")

    for path in (csv_path, latest_path):
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(csv_text)
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(pages, fh, indent=1, ensure_ascii=False, sort_keys=True)

    # verify what was written, rather than assuming
    with open(csv_path, encoding="utf-8", newline="") as fh:
        reread = list(csv.DictReader(fh))
    if len(reread) != count:
        print(f"ERROR: wrote {count} rows but re-read {len(reread)}", file=sys.stderr)
        return 3

    print(f"Backed up {count} rows, {len(keys)} columns -> {csv_path}")
    print(f"Raw JSON -> {json_path}")
    return 0


def main() -> int:
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("ERROR: NOTION_TOKEN is not set", file=sys.stderr)
        return 1
    return run(notion_query(token))


if __name__ == "__main__":
    raise SystemExit(main())
