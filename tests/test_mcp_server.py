import io
import json

import pytest
from tests.conftest import ROOT

from career_agent.mcp_server import PROTOCOL_VERSION, TOOLS, CareerAgentMCPServer


@pytest.fixture
def server(tmp_path):
    return CareerAgentMCPServer(ROOT / "profile" / "master_profile.yaml", tmp_path / "apps.jsonl")


def call(server, name, args=None):
    return server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args or {}},
        }
    )


def payload(response):
    return json.loads(response["result"]["content"][0]["text"])


JOB = {
    "company": "JPMorgan Chase",
    "role": "Software Engineer III - AIML",
    "location": "Hyderabad",
    "min_years": 3,
    "url": "https://example.test/1",
    "required_skills": ["Python", "Java", "MCP", "AWS"],
    "preferred_skills": ["RAG", "Docker"],
}


class TestProtocol:
    def test_initialize(self, server):
        r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert r["result"]["protocolVersion"] == PROTOCOL_VERSION
        assert "never decide what is true" in r["result"]["instructions"]

    def test_tools_list_matches_handlers(self, server):
        assert {t["name"] for t in TOOLS} == set(server.handlers)

    def test_every_tool_schema_is_consistent(self):
        for tool in TOOLS:
            schema = tool["inputSchema"]
            assert schema["type"] == "object"
            for required in schema.get("required", []):
                assert required in schema["properties"]

    def test_notification_gets_no_response(self, server):
        assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    def test_unknown_tool_errors(self, server):
        assert call(server, "nope")["error"]["code"] == -32602

    def test_malformed_json_line(self, server):
        out = io.StringIO()
        server.serve_stdio(io.StringIO("{oops}"), out)
        assert json.loads(out.getvalue())["error"]["code"] == -32700


class TestTools:
    def test_analyse_job(self, server):
        d = payload(call(server, "analyse_job", {"job": JOB}))
        assert d["decision"] in {"APPLY", "MAYBE", "MANUAL_REVIEW", "DO_NOT_APPLY"}
        assert 0 <= d["score"] <= 100
        assert "breakdown" in d

    def test_analyse_rejects_a_malformed_job(self, server):
        assert call(server, "analyse_job", {"job": {"role": "x"}})["result"]["isError"] is True

    def test_tailor_returns_validation_state(self, server):
        d = payload(call(server, "tailor_resume", {"job": JOB}))
        assert "is_final" in d and "blocking_issues" in d
        assert "never bypass the gate" in d["note"]

    def test_profile_summary_lists_never_printable_skills(self, server):
        d = payload(call(server, "profile_summary", {}))
        assert {"Spring Boot", "LangChain", "Kubernetes"} <= set(d["never_printable"])

    def test_track_then_dedupe(self, server):
        first = payload(call(server, "track_application", {"job": JOB, "score": 80}))
        second = payload(call(server, "track_application", {"job": JOB, "score": 80}))
        assert first["created"] is True
        assert second["created"] is False

    def test_update_status_then_pipeline(self, server):
        app = payload(call(server, "track_application", {"job": JOB}))["application"]
        payload(
            call(
                server,
                "update_application_status",
                {"application_id": app["application_id"], "status": "SUBMITTED"},
            )
        )
        assert payload(call(server, "show_pipeline", {}))["counts"] == {"SUBMITTED": 1}

    def test_invalid_status_is_a_tool_error(self, server):
        app = payload(call(server, "track_application", {"job": JOB}))["application"]
        r = call(
            server,
            "update_application_status",
            {"application_id": app["application_id"], "status": "MADE_UP"},
        )
        assert r["result"]["isError"] is True

    def test_validate_resume_standalone_blocks_an_invented_skill(self, server):
        r = payload(
            call(
                server,
                "validate_resume",
                {
                    "resume": {
                        "resume_id": "X",
                        "headline": "AI Engineer",
                        "summary": "Engineer.",
                        "skills": {"Core": ["Rust"]},
                    }
                },
            )
        )
        assert r["passed"] is False
