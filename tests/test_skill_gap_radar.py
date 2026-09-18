"""Tests for skill_gap_radar - pytest-native, pure functions, no network."""

from __future__ import annotations

import pytest
from skill_gap_radar import normalise, rank, render


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
