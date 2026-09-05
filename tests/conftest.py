import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from career_agent.models import JobPosting  # noqa: E402
from career_agent.profile_store import load_profile  # noqa: E402


@pytest.fixture(scope="session")
def profile():
    return load_profile(ROOT / "profile" / "master_profile.yaml")


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
