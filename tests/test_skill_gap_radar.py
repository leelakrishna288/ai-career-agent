"""Tests for skill_gap_radar - pytest-native, pure functions, no network."""

from __future__ import annotations

import pytest
from skill_gap_radar import (
    Gap,
    normalise,
    pending_packs,
    rank,
    render,
    render_queue,
    slug,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # short comma lists, as the Himalayas pipeline writes them
        ("langchain, bedrock", ["langchain/langgraph", "aws hands-on"]),
        ("claude", ["claude"]),
        (None, []),
        ("", []),
        (
            "spring boot, mongodb, angular, mysql, redis, agile",
            ["spring boot", "nosql", "react", "postgres/mysql", "redis", "agile"],
        ),
        # long prose with qualifiers, as the Claude runs write them
        (
            (
                "Kafka and event-driven architecture; MongoDB; Docker/Terraform/Kubernetes; "
                "Spring Boot depth (Leela self-reports low confidence); gRPC"
            ),
            ["kafka", "nosql", "docker", "terraform", "kubernetes", "spring boot", "grpc"],
        ),
        (
            (
                "Node.js/TypeScript (unsupported); Amazon Bedrock (preferred, not held); "
                "hands-on AWS beyond the Cloud Practitioner cert (DynamoDB, Lambda, API "
                "Gateway, SQS, EventBridge); C# (not held - Java is the stated alternative)"
            ),
            ["node.js", "typescript", "aws hands-on", "c#/.net"],
        ),
        (
            (
                "Kubernetes (D), gRPC (D), production microservices at scale "
                "(E - must not claim), Go (optional), Firebase (optional)"
            ),
            ["kubernetes", "grpc", "microservices", "go"],
        ),
    ],
)
def test_normalise(raw: str | None, expected: list[str]) -> None:
    assert normalise(raw) == expected


def test_several_aws_services_count_as_one_gap() -> None:
    assert normalise("Lambda, DynamoDB, SQS, EventBridge").count("aws hands-on") == 1


def test_rank_weights_by_match_score() -> None:
    ranked = rank(
        [
            {"Company": "A", "Match Score": 90, "Missing Skills": "kafka"},
            {"Company": "B", "Match Score": 60, "Missing Skills": "kafka"},
            {"Company": "C", "Match Score": 85, "Missing Skills": "docker"},
        ]
    )
    assert ranked[0].skill == "kafka"
    assert ranked[0].count == 2
    assert ranked[0].weight == pytest.approx(1.5)
    assert ranked[1].skill == "docker"
    assert ranked[1].weight == pytest.approx(0.85)


def test_rank_handles_empty_and_null_input() -> None:
    assert rank([]) == []
    assert rank([{"Company": "X", "Match Score": None, "Missing Skills": None}]) == []


def test_render_emits_a_table() -> None:
    ranked = rank([{"Company": "A", "Match Score": 80, "Missing Skills": "docker"}])
    out = render(ranked)
    assert "| # | Gap |" in out
    assert "docker" in out


def test_slug_is_a_safe_filename_stem() -> None:
    assert slug("postgres/mysql") == "postgres-mysql"
    assert slug("AWS hands-on") == "aws-hands-on"
    assert slug("langchain/langgraph") == "langchain-langgraph"
    assert slug("c#/.net") == "c-net"
    assert slug("///") == "unnamed"


def test_pending_packs_skips_gaps_that_already_have_one() -> None:
    ranked = [Gap("kubernetes", 29.6, ["a"]), Gap("docker", 14.6, ["b"]), Gap("kafka", 13.6, ["c"])]
    # matching is on the slug, so a file name, a bare stem and odd casing all count
    pending = pending_packs(ranked, ["kubernetes.md", "Docker"], top=3)
    assert [g.skill for g in pending] == ["kafka"]


def test_pending_packs_applies_the_window_before_the_filter() -> None:
    """The queue must shrink as packs are written, not pull a low-weight tail forward."""
    ranked = [Gap(s, 10.0 - i, ["r"]) for i, s in enumerate(["a", "b", "c", "d", "e"])]
    assert [g.skill for g in pending_packs(ranked, [], top=2)] == ["a", "b"]
    assert [g.skill for g in pending_packs(ranked, ["a"], top=2)] == ["b"]
    assert pending_packs(ranked, ["a", "b"], top=2) == []


def test_render_queue_names_the_path_and_flags_standing_priorities() -> None:
    out = render_queue([Gap("vector database", 5.0, ["r"]), Gap("docker", 14.6, ["x", "y"])], top=2)
    assert "`07_Learning/vector-database.md`" in out
    assert "`07_Learning/docker.md`" in out
    # vector database is on the SYSTEM_SPEC 9 standing list; docker is not
    vector_row = next(ln for ln in out.splitlines() if "vector database" in ln)
    docker_row = next(ln for ln in out.splitlines() if "| docker |" in ln)
    assert vector_row.rstrip().endswith("| yes |")
    assert docker_row.rstrip().endswith("|  |")
    # it names the work; it never pretends to have written a pack
    assert "nine" in out and "interview questions" in out


def test_render_queue_says_so_when_nothing_is_outstanding() -> None:
    assert "Nothing queued" in render_queue([], top=5)
