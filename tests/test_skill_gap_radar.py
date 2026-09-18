from skill_gap_radar import normalise as n, rank, render

cases = [
    ("langchain, bedrock", ["langchain/langgraph","aws hands-on"]),
    ("claude", ["claude"]),
    (None, []),
    ("", []),
    ("Kafka and event-driven architecture; MongoDB; Docker/Terraform/Kubernetes; Spring Boot depth (Leela self-reports low confidence); gRPC",
     ["kafka","nosql","docker","terraform","kubernetes","spring boot","grpc"]),
    ("Node.js/TypeScript (unsupported); Amazon Bedrock (preferred, not held); hands-on AWS beyond the Cloud Practitioner cert (DynamoDB, Lambda, API Gateway, SQS, EventBridge); C# (not held - Java is the stated alternative)",
     ["node.js","typescript","aws hands-on","c#/.net"]),
    ("Kubernetes (D), gRPC (D), production microservices at scale (E - must not claim), Go (optional), Firebase (optional)",
     ["kubernetes","grpc","microservices","go"]),
    ("spring boot, mongodb, angular, mysql, redis, agile",
     ["spring boot","nosql","react","postgres/mysql","redis","agile"]),
]
fails=0
for i,(inp,exp) in enumerate(cases):
    got=n(inp)
    if got!=exp:
        fails+=1; print(f"FAIL {i}\n  in : {inp}\n  exp: {exp}\n  got: {got}")
print(f"{len(cases)-fails}/{len(cases)} normalise cases pass")

# de-dup within a row: several AWS services = one AWS gap
assert n("Lambda, DynamoDB, SQS, EventBridge").count("aws hands-on")==1
# weighting: same gap, different match scores
rows=[{"Company":"A","Match Score":90,"Missing Skills":"kafka"},
      {"Company":"B","Match Score":60,"Missing Skills":"kafka"},
      {"Company":"C","Match Score":85,"Missing Skills":"docker"}]
r=rank(rows)
assert r[0].skill=="kafka" and abs(r[0].weight-1.5)<1e-9 and r[0].count==2, r[0]
assert r[1].skill=="docker" and abs(r[1].weight-0.85)<1e-9
# a single high-match role outranks nothing; empty input is safe
assert rank([])==[]
assert rank([{"Company":"X","Match Score":None,"Missing Skills":None}])==[]
assert "| # | Gap |" in render(r)
print("weighting, de-dup, empty-input and render all OK")
raise SystemExit(1 if fails else 0)
