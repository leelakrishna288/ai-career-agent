"""MCP server exposing the career-agent tools.

The division of labour is the point: these tools are **deterministic**. The
scoring rubric, the deduplication, the validation gate and the tracker produce
the same answer every time. An LLM client orchestrates them and writes the
prose around the result - it never produces the score and it never decides what
is true about the candidate.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__
from .models import Decision, JobPosting, Status
from .profile_store import load_profile
from .scoring import Scorer
from .tailor import ResumeTailor
from .tracker import Tracker

PROTOCOL_VERSION = "2025-06-18"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "analyse_job",
        "description": (
            "Score a job posting against the master profile using a deterministic weighted "
            "rubric (technical 25, experience 20, AI relevance 15, backend 10, seniority 10, "
            "location 5, compensation 5, growth 5, learnability 5). Returns the score "
            "breakdown, an APPLY / MAYBE / DO_NOT_APPLY / MANUAL_REVIEW decision, hard-reject "
            "reasons, and gaps split into learnable versus critical."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"job": {"type": "object", "description": "JobPosting object."}},
            "required": ["job"],
            "additionalProperties": False,
        },
    },
    {
        "name": "tailor_resume",
        "description": (
            "Generate a role-specific resume by reordering and selecting from the master "
            "profile, then run the factual-validation gate over the result. Never adds a "
            "skill, employer, metric or certification that is not already in the profile. "
            "A resume that fails validation is returned with is_final=false and the blocking "
            "issues listed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job": {"type": "object"},
                "version": {"type": "integer", "minimum": 1, "default": 1},
            },
            "required": ["job"],
            "additionalProperties": False,
        },
    },
    {
        "name": "validate_resume",
        "description": (
            "Run the factual-validation gate over a resume object independently. Use this to "
            "check a resume that was edited by hand or generated elsewhere."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"resume": {"type": "object"}},
            "required": ["resume"],
            "additionalProperties": False,
        },
    },
    {
        "name": "track_application",
        "description": (
            "Add a job to the application tracker, or return the existing entry. Deduplicates "
            "on company + normalised role + canonical URL, so the same posting seen on two "
            "boards is one row. Never overwrites history."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job": {"type": "object"},
                "score": {"type": "number", "default": 0},
                "decision": {"type": "string", "enum": [d.value for d in Decision]},
            },
            "required": ["job"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_application_status",
        "description": "Move an application to a new status. Appends to history; never rewrites it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "application_id": {"type": "string"},
                "status": {"type": "string", "enum": [s.value for s in Status]},
                "note": {"type": "string"},
            },
            "required": ["application_id", "status"],
            "additionalProperties": False,
        },
    },
    {
        "name": "show_pipeline",
        "description": "Return every tracked application and a count by status.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "profile_summary",
        "description": (
            "Return what the master profile can and cannot support: printable skills by label, "
            "and the skills deliberately recorded as LEARNING or UNSUPPORTED so they are never "
            "printed as competencies."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


class ToolError(Exception):
    pass


class CareerAgentMCPServer:
    def __init__(self, profile_path: Path | None = None, tracker_path: Path | None = None):
        self.profile = load_profile(profile_path)
        self.scorer = Scorer(self.profile)
        self.tailor = ResumeTailor(self.profile)
        self.tracker = Tracker(tracker_path or Path("data/applications.jsonl"))
        self.handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "analyse_job": self._analyse,
            "tailor_resume": self._tailor,
            "validate_resume": self._validate,
            "track_application": self._track,
            "update_application_status": self._update,
            "show_pipeline": self._pipeline,
            "profile_summary": self._profile_summary,
        }

    # ---------------- tools ----------------
    @staticmethod
    def _job(args: dict[str, Any]) -> JobPosting:
        try:
            return JobPosting.model_validate(args.get("job") or {})
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"invalid job posting: {exc}") from exc

    def _analyse(self, args: dict[str, Any]) -> dict[str, Any]:
        return json.loads(self.scorer.score(self._job(args)).model_dump_json())

    def _tailor(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._job(args)
        analysis = self.scorer.score(job)
        resume = self.tailor.tailor(job, analysis, version=int(args.get("version", 1)))
        return {
            "resume": json.loads(resume.model_dump_json()),
            "is_final": resume.is_final,
            "blocking_issues": [
                i.detail for i in (resume.validation.blocking if resume.validation else [])
            ],
            "note": (
                "is_final is false when the validation gate found a claim that cannot be traced "
                "to the master profile. Fix the profile or the resume - never bypass the gate."
            ),
        }

    def _validate(self, args: dict[str, Any]) -> dict[str, Any]:
        from .models import TailoredResume  # noqa: PLC0415

        try:
            resume = TailoredResume.model_validate(args.get("resume") or {})
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"invalid resume object: {exc}") from exc
        result = self.tailor.gate.validate(resume)
        return json.loads(result.model_dump_json())

    def _track(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._job(args)
        decision = Decision(args["decision"]) if args.get("decision") else Decision.MAYBE
        app, created = self.tracker.add(job, float(args.get("score", 0)), decision)
        return {
            "application": json.loads(app.model_dump_json()),
            "created": created,
            "note": "created=false means this job was already tracked; no duplicate was made.",
        }

    def _update(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            app = self.tracker.update_status(
                str(args["application_id"]), Status(args["status"]), str(args.get("note", ""))
            )
        except KeyError as exc:
            raise ToolError(str(exc)) from exc
        except ValueError as exc:
            raise ToolError(f"invalid status: {exc}") from exc
        return json.loads(app.model_dump_json())

    def _pipeline(self, _: dict[str, Any]) -> dict[str, Any]:
        return {
            "counts": self.tracker.pipeline(),
            "applications": [json.loads(a.model_dump_json()) for a in self.tracker.all()],
        }

    def _profile_summary(self, _: dict[str, Any]) -> dict[str, Any]:
        by_label: dict[str, list[str]] = {}
        for s in self.profile.skills:
            by_label.setdefault(s.label.value, []).append(s.name)
        return {
            "name": self.profile.name,
            "years_experience": self.profile.years_experience,
            "skills_by_label": by_label,
            "printable_labels": ["VERIFIED", "PARTIALLY_VERIFIED", "TRANSFERABLE"],
            "never_printable": [s.name for s in self.profile.skills if not s.printable],
            "note": (
                "Skills under never_printable are recorded on purpose. The system knows they "
                "exist and knows they must not appear as competencies on a resume."
            ),
        }

    # ---------------- JSON-RPC ----------------
    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method, req_id = request.get("method", ""), request.get("id")
        if method == "initialize":
            return self._ok(
                req_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "ai-career-agent", "version": __version__},
                    "instructions": (
                        "Deterministic job-analysis and resume-tailoring tools. You may decide what "
                        "to emphasise; you may never decide what is true about the candidate. Every "
                        "generated resume passes a validation gate against the master profile - if "
                        "is_final is false, report the blocking issues rather than working around them."
                    ),
                },
            )
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "ping":
            return self._ok(req_id, {})
        if method == "tools/list":
            return self._ok(req_id, {"tools": TOOLS})
        if method == "tools/call":
            params = request.get("params") or {}
            handler = self.handlers.get(params.get("name", ""))
            if handler is None:
                return self._err(req_id, -32602, f"unknown tool: {params.get('name')}")
            try:
                result = handler(params.get("arguments") or {})
            except ToolError as exc:
                return self._ok(
                    req_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True}
                )
            except Exception as exc:  # noqa: BLE001
                return self._ok(
                    req_id,
                    {
                        "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                        "isError": True,
                    },
                )
            return self._ok(
                req_id,
                {
                    "content": [
                        {"type": "text", "text": json.dumps(result, indent=2, default=str)}
                    ],
                    "isError": False,
                },
            )
        return self._err(req_id, -32601, f"method not found: {method}")

    @staticmethod
    def _ok(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _err(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    def serve_stdio(self, stdin=None, stdout=None) -> None:  # noqa: ANN001
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        for line in stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                stdout.write(json.dumps(self._err(None, -32700, "parse error")) + "\n")
                stdout.flush()
                continue
            response = self.handle(request)
            if response is not None:
                stdout.write(json.dumps(response, default=str) + "\n")
                stdout.flush()


def main() -> None:  # pragma: no cover
    CareerAgentMCPServer().serve_stdio()


if __name__ == "__main__":  # pragma: no cover
    main()
