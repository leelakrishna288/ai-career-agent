"""Pytest configuration: shared fixtures, plus import paths for `src/` and `scripts/`.

Two things have to be on `sys.path` before any test imports run:

* `src/` - so `career_agent` is importable without an editable install.
* `scripts/` - so the standalone helpers (`notion_resume_attachment`,
  `notion_tracker_backup`, `skill_gap_radar`, `ats_confidence`) are importable
  no matter how pytest is invoked. Without this, `import notion_tracker_backup`
  raises ModuleNotFoundError at collection time and every job fails in seconds,
  which is what happened on 2026-09-18.

The `scripts/` helper is idempotent and additive: it never removes an existing
`sys.path` entry, so it is safe alongside any tox or PYTHONPATH setup.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


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

import pytest  # noqa: E402

from career_agent.models import JobPosting  # noqa: E402
from career_agent.profile_store import load_profile  # noqa: E402


@pytest.fixture(scope="session")
def repo_root():
    """Repository root. A fixture rather than a module-level constant imported
    across test files - `from tests.conftest import ROOT` relies on pytest's
    rootdir landing on sys.path, which is not guaranteed across versions."""
    return ROOT


@pytest.fixture(scope="session")
def profile():
    return load_profile(ROOT / "tests" / "fixtures" / "profile_fixture.yaml")


@pytest.fixture
def ai_job():
    return JobPosting(
        company="JPMorgan Chase",
        role="Software Engineer III - Java/Python - AIML",
        location="Hyderabad",
        country="India",
        min_years=3,
        url="https://example.test/jobs/1",
        required_skills=["Python", "Java", "MCP", "REST API", "AWS"],
        preferred_skills=["RAG", "vector databases", "Docker"],
        description="Build agentic AI services with LLMs, RAG and MCP on AWS.",
    )


@pytest.fixture
def impossible_job():
    return JobPosting(
        company="Example Analytics",
        role="Principal Machine Learning Engineer",
        location="Riyadh",
        country="Saudi Arabia",
        min_years=10,
        required_skills=["PyTorch", "TensorFlow", "MLOps", "Kubernetes", "distributed systems"],
        mandatory_qualifications=["PhD in Machine Learning"],
    )
