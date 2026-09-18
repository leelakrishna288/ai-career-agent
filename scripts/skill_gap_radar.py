"""Skill Gap Radar - turn the tracker's Missing Skills column into learning priorities.

Closes the SYSTEM_SPEC 23 loop: gaps were recorded on every row but nothing ever
read them back, so the learning plan was chosen by hand and last refreshed
2026-09-13.

What it does: reads Missing Skills across scored rows, normalises the two
formats the tracker actually contains (short comma lists from the Himalayas
pipeline, long semicolon prose from the Claude runs), weights each gap by the
match score of the roles that wanted it, and ranks them.

Weighting: a gap on an 85-match role matters more than the same gap on a
62-match role, because the first is a job Leela could plausibly get. Weight is
match_score / 100, summed across rows, so both frequency and quality count.

Output is a ranked list, not a plan. What to study is still Leela's decision -
this only stops the ranking being guesswork.

Stdlib only, pure functions, no network in the ranking path.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

# Phrases that describe a gap's status rather than naming a skill.
NOISE = re.compile(
    r"\b(unsupported|not held|self-reported|low confidence|transferable|optional|"
    r"critical gap|cert only|must not claim|flagged as|closed|leela|is the stated|"
    r"alternative|specifically|beyond the|depth|familiarity|breadth|at scale|"
    r"in production|at production level|at interview)\b",
    re.IGNORECASE,
)
# Parenthetical qualifiers: "(E - must not claim)", "(B/D)", "(learning)".
PAREN = re.compile(r"\([^)]*\)")
# Class markers left in some rows: "(D)", "(E-unsupported)".
SPLIT = re.compile(r"[;,]|\band\b|/(?![A-Za-z]*\+)")

CANON = {
    "k8s": "kubernetes",
    "kubernetes": "kubernetes",
    "docker": "docker",
    "spring boot": "spring boot",
    "spring": "spring boot",
    "spring framework": "spring boot",
    "spring mvc": "spring boot",
    "spring data jpa": "spring boot",
    "kafka": "kafka",
    "apache kafka": "kafka",
    "rabbitmq": "kafka",
    "event-driven architecture": "kafka",
    "event-driven": "kafka",
    "mongodb": "nosql",
    "cassandra": "nosql",
    "nosql": "nosql",
    "redis": "redis",
    "memcache": "redis",
    "microservices": "microservices",
    "microservices architecture": "microservices",
    "production microservices": "microservices",
    "distributed systems": "distributed systems",
    "distributed systems design": "distributed systems",
    "system design": "system design",
    "typescript": "typescript",
    "node.js": "node.js",
    "node": "node.js",
    "react": "react",
    "angular": "react",
    "frontend": "frontend",
    "langchain": "langchain/langgraph",
    "langgraph": "langchain/langgraph",
    "langfuse": "llm observability",
    "langsmith": "llm observability",
    "braintrust": "llm observability",
    "llm observability": "llm observability",
    "bedrock": "aws hands-on",
    "amazon bedrock": "aws hands-on",
    "aws": "aws hands-on",
    "lambda": "aws hands-on",
    "dynamodb": "aws hands-on",
    "sqs": "aws hands-on",
    "eventbridge": "aws hands-on",
    "api gateway": "aws hands-on",
    "ecs": "aws hands-on",
    "s3": "aws hands-on",
    "ec2": "aws hands-on",
    "terraform": "terraform",
    "postgresql": "postgres/mysql",
    "mysql": "postgres/mysql",
    "pgvector": "vector database",
    "vector database": "vector database",
    "fastapi": "fastapi",
    "fine-tuning": "fine-tuning",
    "pytorch": "pytorch",
    "c#": "c#/.net",
    ".net": "c#/.net",
    "semantic kernel": "c#/.net",
    "hibernate": "hibernate",
    "grpc": "grpc",
    "agile": "agile",
    "rust": "rust",
    "go": "go",
    "claude": "claude",
    "llms": "llm fundamentals",
    "prompt engineering": "prompt engineering",
}


@dataclass
class Gap:
    skill: str
    weight: float = 0.0
    roles: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.roles)


def normalise(raw: str | None) -> list[str]:
    """Split one Missing Skills cell into canonical skill names."""
    if not raw:
        return []
    text = PAREN.sub(" ", raw)
    out: list[str] = []
    for part in SPLIT.split(text):
        token = part.strip().strip(".-\u2014 ").lower()
        if not token or len(token) > 60:
            continue
        if NOISE.search(token):
            # Keep a leading skill name that happens to carry a qualifier,
            # e.g. "spring boot depth" -> "spring boot".
            token = NOISE.sub(" ", token).strip()
            if not token:
                continue
        token = re.sub(r"\s+", " ", token)
        canon = CANON.get(token)
        if canon is None:
            # try the longest known key contained in the token
            hits = [v for k, v in CANON.items() if re.search(rf"\b{re.escape(k)}\b", token)]
            canon = max(hits, key=len) if hits else None
        if canon:
            out.append(canon)
    # de-duplicate within one row: a role wanting Lambda and SQS is one AWS gap
    seen: list[str] = []
    for s in out:
        if s not in seen:
            seen.append(s)
    return seen


def rank(rows: list[dict]) -> list[Gap]:
    """rows: [{'Company','Role','Match Score','Missing Skills'}] -> ranked gaps."""
    gaps: dict[str, Gap] = defaultdict(lambda: Gap(skill=""))
    for row in rows:
        score = row.get("Match Score") or 0
        label = f"{row.get('Company', '?')} ({score:g})"
        for skill in normalise(row.get("Missing Skills")):
            g = gaps[skill]
            g.skill = skill
            g.weight += float(score) / 100.0
            g.roles.append(label)
    return sorted(gaps.values(), key=lambda g: (-g.weight, g.skill))


def render(ranked: list[Gap], top: int = 12) -> str:
    lines = ["| # | Gap | Roles | Weighted | Wanted by |", "|---|---|---|---|---|"]
    for i, g in enumerate(ranked[:top], 1):
        who = ", ".join(g.roles[:4]) + (" …" if g.count > 4 else "")
        lines.append(f"| {i} | {g.skill} | {g.count} | {g.weight:.2f} | {who} |")
    return "\n".join(lines)
