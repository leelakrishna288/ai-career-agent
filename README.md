# AI Career Agent

**Deterministic job-analysis and resume-tailoring tools, exposed over the Model
Context Protocol.** An LLM client orchestrates them; it never produces the
score and it never decides what is true about the candidate.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green)

---

## The problem this solves

Resume tailoring is the single task where an LLM will most reliably help you
lie. Ask it to match a job description and "familiar with Docker" becomes
"containerised production workloads", "did a course on Kubernetes" becomes
"Kubernetes", and neither of you notices until an interviewer asks a follow-up
question you cannot answer.

The fix is not a better prompt. It is a structural separation:

> **The model may decide what to emphasise. It may never decide what is true.**

Everything printable about the candidate lives in one `MasterProfile`, and every
skill in it carries an evidence label. The tailoring engine may reorder,
select and re-word — it may not add. A **validation gate** then checks the
generated resume against the profile independently and blocks anything it
cannot trace back.

```
                    ┌──────────────────────┐
   job posting ────▶│  Scorer              │──▶ score / decision / gaps
                    │  (deterministic)     │    (learnable vs critical)
                    └──────────────────────┘
                               │
   MasterProfile ─────────────▶│
   (labelled claims)           ▼
                    ┌──────────────────────┐
                    │  ResumeTailor        │──▶ tailored resume
                    │  select + reorder    │
                    └──────────┬───────────┘
                               ▼
                    ┌──────────────────────┐
                    │  ValidationGate      │──▶ FINAL, or BLOCKED with reasons
                    │  independent check   │
                    └──────────────────────┘
```

---

## Evidence labels

Every skill is recorded with how well it is actually supported:

| Label | Meaning | Printable? |
|---|---|---|
| `VERIFIED` | Demonstrable — shipped code, or core to a job held | yes |
| `PARTIALLY_VERIFIED` | Real but narrower than the word implies | yes |
| `TRANSFERABLE` | Adjacent experience, honestly framed | yes |
| `LEARNING` | Being learned. Not a competency yet | **no** |
| `UNSUPPORTED` | Not held | **no** |
| `UNKNOWN` | Unconfirmed | **no** |

The interesting part is that `LEARNING` and `UNSUPPORTED` skills are stored
*deliberately*. The system needs to know they exist so it can refuse to print
them — and so that when a posting asks for one, it can tell you honestly
whether it is a gap of weeks or a gap of years.

```bash
$ career-agent profile
┌────────────────────┬──────────────────────────────────────────────────┐
│ LEARNING           │ LangChain, Spring Boot                           │
│ UNSUPPORTED        │ Kubernetes, Machine learning, Microservices      │
└────────────────────┴──────────────────────────────────────────────────┘
LEARNING and UNSUPPORTED skills are recorded deliberately - the validation
gate blocks them from ever being printed as competencies.
```

---

## Scoring

Deterministic, weighted, 100 points. Same input, same score — which is what
makes it comparable across a hundred applications instead of drifting with
whatever the model felt like that day.

| Component | Max |
|---|---:|
| Technical fit (required weighted 4× preferred) | 25 |
| Experience against stated requirement | 20 |
| AI / GenAI relevance | 15 |
| Backend relevance | 10 |
| Seniority alignment | 10 |
| Location | 5 |
| Compensation signal | 5 |
| Career growth signal | 5 |
| Learnability of the gaps | 5 |

**Hard rejects run first and short-circuit.** A role requiring a PhD you do not
have is not a low score, it is not a job — and pretending otherwise wastes the
only genuinely scarce resource in a job search, which is the attention you can
give each application.

```bash
$ career-agent analyse examples/jpmorgan_hyderabad.yaml

JPMorgan Chase - Software Engineer III - Java/Python - AIML
APPLY · score 87.5/100 (Strong) · ATS keyword coverage 88.9%

Learnable gaps: LangGraph
```

```bash
$ career-agent analyse examples/senior_ml_riyadh.yaml

Example Analytics - Principal Machine Learning Engineer
DO_NOT_APPLY · score 21.8/100 (Low Priority)

Hard rejects
  · Requires 10+ years; profile has 4.7 - a 5.3 year gap.
  · Mandatory qualification not held: PhD in Machine Learning
  · Title implies a seniority band above this profile.
  · Three or more required skills are multi-year gaps, not learnable ones.
Critical gaps (do not claim): PyTorch, TensorFlow, MLOps, Kubernetes
```

Gaps are split into **learnable** (weeks of deliberate work) and **critical**
(years). Conflating the two is how people either give up on winnable roles or
waste months on unwinnable ones.

---

## The validation gate

Seven checks, each blocking:

1. **Skill provenance** — every printed skill exists in the profile at a
   printable label. A `LEARNING` skill named in the posting is still refused.
2. **Employment integrity** — employer, title and dates must match a profile
   record exactly. Inflating a title at a real employer is caught.
3. **Project provenance** — no unknown projects; a public URL is refused unless
   the profile marks the project shipped, because a link that 404s in front of a
   recruiter is worse than no link.
4. **Metric verification** — any number presented as an achievement must appear
   in the profile as a whole token.
5. **No placeholders** — no `TBD`, no `[COMPANY]`, no unresolved templates.
6. **No seniority inflation** — "principal", "architected the", "expert in" are
   blocked against a 4.7-year profile.
7. **Required fields** — no empty headline or summary.

A resume that fails is returned with `is_final: false` and every blocking
reason listed. There is no override flag. If the gate is wrong, the fix is to
correct the profile, not to bypass the check.

---

## MCP server

```bash
career-agent-mcp        # JSON-RPC 2.0 over stdio
```

| Tool | Determinism |
|---|---|
| `analyse_job` | fully deterministic |
| `tailor_resume` | selection + reordering, then gated |
| `validate_resume` | fully deterministic |
| `track_application` | deduplicated, append-only |
| `update_application_status` | append-only history |
| `show_pipeline` | read |
| `profile_summary` | read |

The server's `initialize` instructions tell the client, in as many words, that
it may not decide what is true about the candidate and must report blocking
issues rather than working around them.

---

## Tracker

Append-only JSONL. Two rules, both learned the expensive way:

- **Never overwrite history.** Status changes append; the previous state stays
  readable. A tracker that silently rewrites itself cannot answer "when did this
  go quiet?", which is the only question that matters when a pipeline stalls.
- **Never create a duplicate.** Deduplication on company + normalised role +
  canonical URL, so the same job on LinkedIn and on the company careers page is
  one row. `Senior AI Engineer II` and `AI Engineer` at the same company on the
  same URL are the same job; `?utm_source=linkedin` is not a different posting.

---

## Quick start

```bash
pip install -e ".[dev]"

career-agent profile                                   # what is and isn't claimable
career-agent analyse examples/jpmorgan_hyderabad.yaml  # score a posting
career-agent tailor  examples/jpmorgan_hyderabad.yaml  # generate + validate
career-agent track   examples/jpmorgan_hyderabad.yaml  # add to the pipeline
career-agent pipeline --export applications.csv
pytest -q                                              # 68 tests
```

No API key, no network, no cost. Nothing here calls a model — that is the
client's job.

---

## Tests found real bugs

Worth recording, because it is the argument for writing them:

- **Substring metric matching.** An invented "47% latency reduction" passed the
  gate because `47` appears inside the LinkedIn URL slug `a94479167`.
  Fixed with whole-token numeric extraction.
- **Alias-blind skill promotion.** A posting asking for `MCP` failed to promote
  the skill stored as `Model Context Protocol`, burying the most relevant item
  on the resume. Fixed by matching aliases.
- **Punctuation surviving normalisation.** `Sr. Java Developer` left a bare `.`
  token, so it would not deduplicate against `Java Developer`.

---

## Limitations

- **It does not find jobs.** Ingestion is by URL, file or paste. LinkedIn
  prohibits unauthorised automated collection, and this project does not do it.
- **It never submits an application.** Preparation is automated; submission is a
  human action.
- **The ATS score is an estimate** of overlap between the profile and a
  posting's stated skills. It is not any employer's actual ATS score and is
  labelled as such everywhere it appears.
- **The master profile is never modified automatically.** An agent that can
  silently edit its own source of truth has no source of truth.

## Licence

Apache-2.0.
