"""Pytest configuration.

Makes the standalone helpers in `scripts/` importable from the test suite no
matter how pytest is invoked - from the repo root, from inside `tests/`, with
`-p no:cacheprovider`, or with an explicit rootdir. Without this,
`import notion_tracker_backup` raises ModuleNotFoundError at collection time
and every job fails in seconds, which is what happened on 2026-09-18.

Place this file at `tests/conftest.py`. A copy at the repo root is harmless and
also works; the directory walk below finds `scripts/` from either location.

Idempotent and additive: it never removes an existing sys.path entry, so it is
safe alongside any src-layout, tox or PYTHONPATH setup already in place.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _add_scripts_to_path() -> None:
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        candidate = parent / "scripts"
        if candidate.is_dir():
            path = str(candidate)
            if path not in sys.path:
                sys.path.insert(0, path)
            return


_add_scripts_to_path()
