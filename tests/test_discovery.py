"""Standalone discovery runtime: sources, extraction, gates, dedup, Notion sink."""

from __future__ import annotations

from datetime import date

import pytest

from career_agent.discovery.extract import (
    classify_country,
    classify_work_mode,
    extract_skills,
    extract_years,
    to_job_posting,
)
from career_agent.discovery.filters import compile_any, reachable, title_relevant
from career_agent.discovery.http import HttpError
from career_agent.discovery.notion_sink import MemorySink, NotionTrackerSink, render_report
from career_agent.discovery.pipeline import (
    BoardConfig,
    DiscoveryConfig,
    ExistingKey,
    run_discovery,
)
from career_agent.discovery.sources import (
    Board,
    RawPosting,
    fetch_ashby,
    fetch_board,
    fetch_greenhouse,
    fetch_lever,
    html_to_text,
)

TODAY = date(2026, 9, 17)

GH_JD = (
    "&lt;h3&gt;Requirements&lt;/h3&gt;&lt;ul&gt;&lt;li&gt;4+ years of experience building "
    "backend services in Java&lt;/li&gt;&lt;li&gt;Spring Boot, REST APIs, SQL&lt;/li&gt;"
    "&lt;li&gt;Experience integrating LLM APIs and RAG&lt;/li&gt;&lt;/ul&gt;"
    "&lt;h3&gt;Nice to have&lt;/h3&gt;&lt;ul&gt;&lt;li&gt;Kubernetes, LangChain&lt;/li&gt;&lt;/ul&gt;"
)

GREENHOUSE = {
    "jobs": [
        {
            "id": 111,
            "title": "Software Engineer, Java (GenAI)",
            "company_name": "Acme AI",
            "location": {"name": "Hyderabad, India"},
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/111",
            "content": GH_JD,
            "first_published": "2026-09-10T09:00:00-04:00",
            "offices": [{"name": "India"}],
        },
        {
            "id": 112,
            "title": "Principal Engineer",
            "location": {"name": "Hyderabad"},
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/112",
            "content": "",
        },
        {
            "id": 113,
            "title": "Backend Engineer",
            "location": {"name": "San Francisco, CA"},
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/113",
            "content": "5 years of experience with Go",
            "first_published": "2026-09-12T00:00:00Z",
        },
        {
            "id": 114,
            "title": "Software Engineer II",
            "location": {"name": "Bangalore"},
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/114",
            "content": "3 years of experience in Java",
            "first_published": "2026-06-01T00:00:00Z",
        },
    ]
}

LEVER = [
    {
        "id": "abc",
        "text": "Backend Developer",
        "categories": {"location": "India (Remote)", "allLocations": ["India (Remote)"]},
        "workplaceType": "remote",
        "country": "IN",
        "hostedUrl": "https://jobs.lever.co/termgrid/abc",
        "descriptionPlain": "Minimum 3 years of experience. Java, Kafka, PostgreSQL.",
        "lists": [{"text": "Preferred", "content": "<li>Docker</li>"}],
        "createdAt": 1789000000000,
    }
]

ASHBY = {
    "jobs": [
        {
            "id": "z1",
            "title": "AI Engineer",
            "location": "Dubai",
            "isListed": True,
            "isRemote": False,
            "workplaceType": "Hybrid",
            "address": {"postalAddress": {"addressCountry": "United Arab Emirates"}},
            "jobUrl": "https://jobs.ashbyhq.com/zeta/z1",
            "descriptionPlain": "2+ years of experience with Python, LLM, RAG, MCP.",
            "publishedAt": "2026-09-15T10:00:00.000+00:00",
            "shouldDisplayCompensationOnJobPostings": True,
            "compensation": {"compensationTierSummary": "AED 25K – 35K / month"},
        },
        {"id": "z2", "title": "AI Engineer", "isListed": False},
    ]
}


class FakeClient:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, body=None, headers=None):
        self.calls.append((method, url, body, headers))
        for prefix, value in self.routes.items():
            if url.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                return value(body) if callable(value) else value
        raise HttpError(404, url)


def gh_client():
    return FakeClient(
        {
            "https://boards-api.greenhouse.io/v1/boards/acme/": GREENHOUSE,
            "https://api.lever.co/v0/postings/termgrid": LEVER,
            "https://api.ashbyhq.com/posting-api/job-board/zeta": ASHBY,
        }
    )


def config(**kw):
    base = {
        "boards": [
            BoardConfig(platform="greenhouse", token="acme", company="Acme AI"),
            BoardConfig(platform="lever", token="termgrid", company="Termgrid"),
            BoardConfig(platform="ashby", token="zeta", company="Zeta"),
            BoardConfig(platform="greenhouse", token="gone", company="Gone Inc"),
        ],
        "include_titles": ["software engineer", "backend", "ai engineer"],
        "exclude_titles": ["principal"],
    }
    base.update(kw)
    return DiscoveryConfig(**base)


# -- sources -------------------------------------------------------------------
def test_html_to_text_unescapes_double_escaped_greenhouse_html():
    text = html_to_text(GH_JD)
    assert "<" not in text and "4+ years of experience" in text
    assert "- Spring Boot" in text


def test_greenhouse_adapter_maps_fields():
    jobs = fetch_greenhouse(gh_client(), Board("greenhouse", "acme", "Acme"))
    assert len(jobs) == 4
    j = jobs[0]
    assert j.company == "Acme AI" and j.platform == "Greenhouse"
    assert j.external_id == "gh-111" and j.posted == "2026-09-10"
    assert j.extra_locations == ["India"]


def test_lever_adapter_maps_fields_and_lists():
    [j] = fetch_lever(gh_client(), Board("lever", "termgrid", "Termgrid"))
    assert j.workplace_hint == "remote" and j.country_hint == "IN"
    assert "Docker" in j.description and j.posted.startswith("2026-")


def test_ashby_adapter_skips_unlisted_and_reads_compensation():
    jobs = fetch_ashby(gh_client(), Board("ashby", "zeta", "Zeta"))
    assert len(jobs) == 1
    assert jobs[0].salary_text.startswith("AED") and jobs[0].workplace_hint == "hybrid"


def test_unknown_platform_rejected():
    with pytest.raises(ValueError):
        fetch_board(gh_client(), Board("workday", "x", "X"))


# -- extraction ----------------------------------------------------------------
def test_years_takes_smallest_explicit_minimum():
    assert extract_years("3-5 years of experience; 8+ years of Java experience")[:2] == (3.0, 5.0)
    assert extract_years("at least 6 yrs in the field")[0] == 6.0
    assert extract_years("fast-paced team")[0] == 0.0


def test_skills_split_at_preferred_heading_and_ignore_ambiguous_words():
    req, pref = extract_skills("Must have\nJava, Kafka. Go to market fast.\nNice to have\nDocker")
    assert "java" in req and "kafka" in req and "go" not in req
    assert pref == ["docker"]


def raw(**kw):
    base = {
        "company": "X",
        "title": "Software Engineer",
        "location": "",
        "url": "https://x/1",
        "external_id": "x-1",
        "platform": "Greenhouse",
    }
    base.update(kw)
    return RawPosting(**base)


def test_hybrid_is_never_classified_as_remote():
    r = raw(location="Remote / Hybrid - Bangalore", workplace_hint="remote")
    assert classify_work_mode(r, classify_country(r))[1] == "Hybrid"


def test_remote_india_and_country_detection():
    r = raw(location="India (Remote)", workplace_hint="remote", country_hint="IN")
    country = classify_country(r)
    assert country == "India"
    assert classify_work_mode(r, country)[1] == "Remote-India"
    assert classify_country(raw(location="Riyadh, KSA")) == "Saudi Arabia"
    assert classify_country(raw(location="Somewhere")) == ""


def test_unknown_work_mode_stays_unknown():
    r = raw(location="Pune")
    assert classify_work_mode(r, "India")[1] == "Unknown"


# -- gates ---------------------------------------------------------------------
def test_title_gate():
    inc, exc = compile_any(["engineer"]), compile_any(["principal"])
    assert title_relevant("Software Engineer", inc, exc)[0]
    assert not title_relevant("Principal Engineer", inc, exc)[0]
    assert not title_relevant("Account Executive", inc, exc)[0]
    assert compile_any([]) is None


def test_work_authorisation_gate_runs_before_scoring():
    ex = to_job_posting(raw(location="London, United Kingdom"))
    ok, why = reachable(ex, {"India"})
    assert not ok and "Europe" in why
    ok, why = reachable(to_job_posting(raw(location="Mars")), {"India"})
    assert ok and "unknown" in why


# -- pipeline ------------------------------------------------------------------
def test_end_to_end_pipeline(profile):
    sink = MemorySink()
    rep = run_discovery(config(), profile, gh_client(), sink, today=TODAY)
    assert rep.boards_ok == 3
    assert len(rep.boards_failed) == 1 and "Gone Inc" in rep.boards_failed[0]
    assert rep.fetched == 6
    assert rep.irrelevant_title == 1  # principal
    assert rep.unreachable == 1  # San Francisco
    assert rep.too_old == 1  # June posting
    companies = sorted(r.ex.job.company for r in sink.records)
    assert companies == ["Acme AI", "Termgrid", "Zeta"]
    acme = next(r for r in sink.records if r.ex.job.company == "Acme AI")
    assert acme.ex.job.min_years == 4.0
    assert acme.variant == "B-AI+Java"
    assert acme.status in {"MATCHED", "ANALYZING", "REJECTED"}
    zeta = next(r for r in sink.records if r.ex.job.company == "Zeta")
    assert zeta.ex.country == "UAE" and zeta.ex.work_mode_label == "Hybrid"
    assert rep.ok


def test_pipeline_never_duplicates_existing_or_same_run_rows(profile):
    existing = [ExistingKey("Acme AI", "", "https://job-boards.greenhouse.io/acme/jobs/111/")]
    client = gh_client()
    client.routes["https://api.lever.co/v0/postings/copy"] = LEVER  # same job twice in one run
    cfg = config(
        boards=config().boards + [BoardConfig(platform="lever", token="copy", company="Termgrid")]
    )
    sink = MemorySink(existing)
    rep = run_discovery(cfg, profile, client, sink, today=TODAY)
    assert rep.duplicates == 2
    assert [r.ex.job.company for r in sink.records].count("Termgrid") == 1
    assert "Acme AI" not in [r.ex.job.company for r in sink.records]


def test_pipeline_cap_and_write_errors_are_reported(profile):
    class Failing(MemorySink):
        def add(self, record):
            raise RuntimeError("notion down")

    rep = run_discovery(config(max_new_per_run=1), profile, gh_client(), Failing(), today=TODAY)
    assert len(rep.write_errors) == 3 and not rep.ok
    rep = run_discovery(config(max_new_per_run=1), profile, gh_client(), MemorySink(), today=TODAY)
    assert len(rep.recorded) == 1 and rep.capped == 2


def test_config_loads_shipped_file(repo_root):
    cfg = DiscoveryConfig.load(repo_root / "config" / "discovery.yaml")
    assert len(cfg.boards) >= 20
    assert {b.platform for b in cfg.boards} == {"greenhouse", "lever", "ashby"}
    assert "India" in cfg.allowed_countries


# -- Notion sink ---------------------------------------------------------------
def notion_routes(pages, created):
    def query(body):
        start = int(body.get("start_cursor") or 0)
        chunk = pages[start : start + 1]
        more = start + 1 < len(pages)
        return {"results": chunk, "has_more": more, "next_cursor": str(start + 1) if more else None}

    def create(body):
        created.append(body)
        return {"url": f"https://notion.so/p{len(created)}"}

    return {
        "https://api.notion.com/v1/data_sources/": query,
        "https://api.notion.com/v1/pages": create,
    }


def page(company, role, url, ext=""):
    t = lambda v: {"type": "rich_text", "rich_text": [{"plain_text": v}]}  # noqa: E731
    return {
        "properties": {
            "Company": t(company),
            "Role": t(role),
            "Job URL": {"type": "url", "url": url},
            "External Job ID": t(ext),
        }
    }


def test_notion_sink_paginates_and_writes_valid_properties(profile):
    created = []
    client = FakeClient(
        notion_routes(
            [
                page("Acme AI", "Software Engineer, Java (GenAI)", "https://other/1"),
                page("Other", "Dev", "https://job-boards.greenhouse.io/acme/jobs/999", "gh-111"),
            ],
            created,
        )
    )
    client.routes.update(dict(gh_client().routes))
    sink = NotionTrackerSink(client, "secret-token", "collection://ds-1", sleep=lambda s: None)
    keys = sink.existing_keys()
    assert len(keys) == 2 and keys[1].external_id == "gh-111"
    rep = run_discovery(config(), profile, client, sink, today=TODAY)
    assert rep.duplicates >= 1
    assert len(created) == len(rep.recorded) == 2
    body = created[0]
    assert body["parent"] == {"type": "data_source_id", "data_source_id": "ds-1"}
    props = body["properties"]
    for name in (
        "Company + Role",
        "Status",
        "Decision",
        "Match Score",
        "Platform",
        "Discovered By",
        "Company Verdict",
        "Work Mode",
        "Country",
    ):
        assert name in props
    assert props["Company Verdict"]["select"]["name"] == "UNVERIFIED"
    assert props["Decision"]["select"]["name"] in {
        "APPLY",
        "MAYBE",
        "DO NOT APPLY",
        "MANUAL REVIEW",
    }
    auth = [c[3]["Authorization"] for c in client.calls if c[3]]
    assert all(a == "Bearer secret-token" for a in auth)


def test_notion_sink_requires_configuration():
    with pytest.raises(ValueError):
        NotionTrackerSink(FakeClient({}), "", "ds")
    with pytest.raises(ValueError):
        NotionTrackerSink(FakeClient({}), "tok", "")


def test_notion_report_page(profile):
    created = []
    client = FakeClient(notion_routes([], created))
    sink = NotionTrackerSink(client, "t", "ds", sleep=lambda s: None)
    url = sink.write_report("parent-1", "Run", "# T\n## Summary\n- a\ntext")
    assert url and created[0]["parent"] == {"page_id": "parent-1"}
    kinds = [b["type"] for b in created[0]["children"]]
    assert kinds == ["heading_2", "bulleted_list_item", "paragraph"]


def test_public_report_hides_companies(profile):
    sink = MemorySink()
    rep = run_discovery(config(), profile, gh_client(), sink, today=TODAY)
    full, public = render_report(rep), render_report(rep, public=True)
    assert "Termgrid" in full or "Acme" in full or "None this run" in full
    assert "Termgrid" not in public and "Acme" not in public and "Gone" not in public


def test_cli_discover_dry_run(monkeypatch, tmp_path, repo_root):
    from typer.testing import CliRunner

    import career_agent.discovery.http as http_mod
    from career_agent.cli import app

    monkeypatch.setattr(
        http_mod.UrllibJsonClient,
        "request",
        lambda self, m, u, body=None, headers=None: gh_client().request(m, u),
    )
    cfg = tmp_path / "d.yaml"
    cfg.write_text(
        "boards:\n  - {platform: greenhouse, token: acme, company: Acme AI}\n"
        "include_titles: [software engineer]\nmax_posting_age_days: 100000\n",
        encoding="utf-8",
    )
    pub = tmp_path / "pub.md"
    result = CliRunner().invoke(
        app,
        [
            "discover",
            "-c",
            str(cfg),
            "--report-dir",
            str(tmp_path / "r"),
            "--public-summary",
            str(pub),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert list((tmp_path / "r").glob("discovery_*.md"))
    assert "Acme" not in pub.read_text(encoding="utf-8")


def test_cli_discover_fails_when_every_board_fails(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    import career_agent.discovery.http as http_mod
    from career_agent.cli import app

    def boom(self, m, u, body=None, headers=None):
        raise HttpError(404, u)

    monkeypatch.setattr(http_mod.UrllibJsonClient, "request", boom)
    cfg = tmp_path / "d.yaml"
    cfg.write_text("boards:\n  - {platform: lever, token: x, company: X}\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["discover", "-c", str(cfg), "--report-dir", str(tmp_path)])
    assert result.exit_code == 1


def test_cli_discover_notion_requires_token(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from career_agent.cli import app

    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    cfg = tmp_path / "d.yaml"
    cfg.write_text("boards: []\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["discover", "-c", str(cfg), "--notion"])
    assert result.exit_code == 2


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_client_retries_then_succeeds(monkeypatch):
    import io
    import urllib.error

    from career_agent.discovery import http as http_mod

    calls = {"n": 0}

    def fake_urlopen(req, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(
                req.full_url, 429, "slow", {"Retry-After": "0"}, io.BytesIO(b"")
            )
        if calls["n"] == 2:
            raise urllib.error.URLError("reset")
        return _Resp(b'{"ok": true}')

    monkeypatch.setattr(http_mod.urllib.request, "urlopen", fake_urlopen)
    client = http_mod.UrllibJsonClient(sleep=lambda s: None)
    assert client.request("GET", "https://example.test/x") == {"ok": True}
    assert calls["n"] == 3


def test_http_client_raises_on_404_and_refuses_http(monkeypatch):
    import io
    import urllib.error

    from career_agent.discovery import http as http_mod

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b"missing"))

    monkeypatch.setattr(http_mod.urllib.request, "urlopen", fake_urlopen)
    client = http_mod.UrllibJsonClient(sleep=lambda s: None)
    with pytest.raises(HttpError) as err:
        client.request("GET", "https://example.test/x")
    assert err.value.status == 404
    with pytest.raises(ValueError):
        client.request("GET", "http://example.test/x")


def test_non_target_countries_are_unreachable_found_in_live_data():
    # Live replay on 2026-09-17 leaked Brazil, Canada, Argentina and "US - Orlando"
    # through the work-authorisation gate as "country unknown".
    for loc in (
        "Brazil - Sao Paulo",
        "Canada - Toronto",
        "Buenos Aires, Argentina",
        "US - Orlando",
        "China - Hong Kong",
    ):
        ex = to_job_posting(raw(location=loc))
        assert not reachable(ex, {"India", "UAE"})[0], loc
    assert classify_country(raw(location="India, Singapore")) == "India"
    assert classify_country(raw(location="Austin, US")) == "USA"
    assert classify_country(raw(location="Business Park")) == ""


def test_best_scores_survive_the_cap_and_one_employer_cannot_flood(profile):
    big = {
        "jobs": [
            {
                "id": i,
                "title": "Software Engineer",
                "location": {"name": "Pune, India"},
                "absolute_url": f"https://job-boards.greenhouse.io/big/jobs/{i}",
                "content": "Must have Cobol" if i else GH_JD,
                "first_published": "2026-09-15",
            }
            for i in range(10)
        ]
    }
    client = FakeClient({"https://boards-api.greenhouse.io/v1/boards/big/": big})
    cfg = config(
        boards=[BoardConfig(platform="greenhouse", token="big", company="Big")],
        max_new_per_run=5,
        max_new_per_company=3,
    )
    sink = MemorySink()
    rep = run_discovery(cfg, profile, client, sink, today=TODAY)
    assert len(sink.records) == 3 and rep.capped == 7
    assert sink.records[0].raw.external_id == "gh-0"  # the strongest JD is written first
    scores = [r.analysis.score for r in sink.records]
    assert scores == sorted(scores, reverse=True)


def test_skill_terms_match_whole_words_only_found_in_live_data():
    # Live replay: "trust" produced a Rust gap and "scalable" a Scala gap.
    req, _ = extract_skills(
        "Build trusted, scalable services. Backend experience with C++ and Node.js"
    )
    assert "rust" not in req and "scala" not in req and "backend" not in req
    assert "c++" in req and "node.js" in req
    req, _ = extract_skills("We use Rust and Scala.")
    assert {"rust", "scala"} <= set(req)
