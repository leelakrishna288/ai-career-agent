"""The daily run that needs no Claude: resumes for new matches, the digest
(Notion page + optional email), and the government-jobs section.

Nothing here submits an application. Company portals are applied to by
Leela, or by the laptop run under her standing rule; LinkedIn, Naukri and
Indeed are always a manual click.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import date, timedelta
from email.message import EmailMessage
from typing import Any

from ..models import Decision, MasterProfile
from ..render import ats_summary_lines, markdown_to_blocks, resume_filename, to_docx, to_markdown
from ..scoring import Scorer
from ..tailor import ResumeTailor
from .govt import GovtReport
from .notion_sink import _rt_links
from .pipeline import DiscoveryConfig, NewRecord, RunReport

log = logging.getLogger(__name__)

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
EMPLOYER_PLATFORMS = {"Greenhouse", "Lever", "Ashby", "Workday", "Company Career Page"}


@dataclass
class ResumeOutcome:
    company: str
    role: str
    url: str
    purpose: str
    score: float
    ats: float
    ready: bool
    filename: str
    missing: list[str]
    auto_submit_eligible: bool
    docx: bytes = b""
    board_copy: bool = False
    # None = never attempted (no tracker row); True/False = what Notion said
    attached: bool | None = None
    error: str = ""


@dataclass
class DailyResult:
    resumes: list[ResumeOutcome] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _needs_resume(rec: NewRecord) -> bool:
    return (
        rec.analysis.decision in (Decision.APPLY, Decision.MAYBE)
        and rec.status == "MATCHED"
        and not rec.raw.source_url  # community leads have no full JD yet
        and len(rec.ex.job.description) >= 300
    )


def prepare_resumes(
    report: RunReport,
    profile: MasterProfile,
    cfg: DiscoveryConfig,
    sink: Any | None,
    result: DailyResult,
) -> None:
    tailor = ResumeTailor(profile)
    scorer = Scorer(profile)
    todo = [(r, ref) for r, ref in report.records if _needs_resume(r)]
    todo.sort(key=lambda x: -x[0].analysis.score)
    for rec, ref in todo[: cfg.max_resumes_per_run]:
        job = rec.ex.job
        try:
            resume = tailor.tailor(job, scorer.score(job))
            ats = resume.ats.total if resume.ats else 0.0
            # A LOW-confidence estimate is not a measurement: the JD named too few
            # skills for the denominator to mean anything, so the row must never clear
            # the auto-submit bar however high the number looks.
            measurable = resume.ats is not None and resume.ats.confidence != "LOW"
            ready = resume.ready(cfg.min_ats)
            filename = resume_filename(resume)
            docx = to_docx(resume, profile)
            outcome = ResumeOutcome(
                company=job.company,
                role=job.role,
                url=rec.raw.url,
                purpose=rec.purpose,
                score=rec.analysis.score,
                ats=ats,
                ready=ready,
                filename=filename,
                missing=list(resume.ats.missing_required if resume.ats else []),
                auto_submit_eligible=ready
                and rec.analysis.score >= cfg.auto_submit_min_match
                and rec.raw.platform in EMPLOYER_PLATFORMS
                and measurable,
                docx=docx,
                board_copy=rec.raw.platform not in EMPLOYER_PLATFORMS,
            )
        except Exception as exc:  # one resume must not stop the rest
            result.errors.append(f"resume {job.company} / {job.role}: {exc}")
            continue
        result.resumes.append(outcome)
        if sink is None or not ref:
            continue
        try:
            _attach(sink, ref, rec, resume, outcome, profile, cfg)
        except Exception as exc:
            outcome.error = str(exc)[:200]
            result.errors.append(f"Notion resume write {job.company}: {exc}")


def _landed(attached: bool) -> str:
    return "(attached to this row)" if attached else "(NOT attached - text only)"


def _attach(sink, ref, rec, resume, outcome, profile, cfg) -> None:  # noqa: ANN001
    if outcome.ready:
        nxt = f"Resume READY (ATS {outcome.ats:.0f}). " + (
            "Meets the auto-submit bar once the company check says GENUINE."
            if outcome.auto_submit_eligible
            else f"Apply yourself at {outcome.url} or set Status = APPROVED."
        )
    else:
        nxt = (
            f"Resume NOT READY: ESTIMATED ATS {outcome.ats:.0f} < {cfg.min_ats:.0f}. "
            "It cannot be raised truthfully"
            + (f" (missing: {', '.join(outcome.missing[:6])})" if outcome.missing else "")
            + ". Decide: skip, or approve anyway."
        )
    # Attach first: the Status below must not claim a prepared resume unless the
    # row actually carries the DOCX (SYSTEM_SPEC v1.8 13.3).
    attached = sink.attach_resume(ref, outcome.filename, outcome.docx)
    outcome.attached = attached

    props: dict[str, Any] = {
        "ATS Estimate": {"number": outcome.ats},
        "Resume Version": {"rich_text": [{"type": "text", "text": {"content": resume.resume_id}}]},
        "Resume File": {
            "rich_text": [
                {"type": "text", "text": {"content": f"{outcome.filename} {_landed(attached)}"}}
            ]
        },
        "Next Action": {"rich_text": [{"type": "text", "text": {"content": nxt[:1900]}}]},
    }
    if outcome.ready and resume.is_final and attached:
        props["Status"] = {"select": {"name": "RESUME_PREPARED"}}
    sink.update_properties(ref, props)
    blocks: list[dict[str, Any]] = [
        {
            "type": "callout",
            "callout": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": "\n".join(ats_summary_lines(resume, cfg.min_ats))[:1900]
                        },
                    }
                ],
                "icon": {"type": "emoji", "emoji": "✅" if outcome.ready else "⚠️"},
            },
        },
        {"type": "paragraph", "paragraph": _rt_links(f"Apply here: {outcome.url}")},
    ]
    if not attached:  # the text version below is still complete
        blocks.append(
            {
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {
                                "content": (
                                    "The DOCX could not be attached to this row; "
                                    "the full resume text follows."
                                )[:1900]
                            },
                        }
                    ]
                },
            }
        )
    blocks.append({"type": "divider", "divider": {}})
    blocks += markdown_to_blocks(to_markdown(resume, profile))
    sink.append_blocks(ref, blocks)


# -- digest -------------------------------------------------------------------
def build_digest(
    run_date: str,
    report: RunReport,
    result: DailyResult,
    govt: GovtReport | None,
) -> str:
    L: list[str] = [f"# Daily jobs — {run_date}"]
    L.append("## Apply now (resume READY, ESTIMATED ATS ≥ 90)")
    ready = [r for r in result.resumes if r.ready]
    for r in sorted(ready, key=lambda r: (r.purpose != "TARGET", -r.score)):
        auto = " · meets auto-submit bar after company check" if r.auto_submit_eligible else ""
        copy = " · job-board copy: apply on the employer's page it links to" if r.board_copy else ""
        L.append(
            f"- {r.purpose} · {r.company} — {r.role} · match {r.score:.0f} · ATS {r.ats:.0f}{auto}{copy} · {r.url}"
        )
    if not ready:
        L.append("- None today.")
    weak = [r for r in result.resumes if not r.ready]
    if weak:
        L.append("## Matched, but the resume cannot truthfully reach ATS 90 (your call)")
        for r in weak:
            miss = f" · missing {', '.join(r.missing[:5])}" if r.missing else ""
            L.append(
                f"- {r.company} — {r.role} · match {r.score:.0f} · ATS {r.ats:.0f}{miss} · {r.url}"
            )
    unattached = [r for r in result.resumes if r.attached is False]
    if unattached:
        L.append("## Resume text only — the DOCX did not attach (SYSTEM_SPEC §13.3)")
        for r in unattached:
            L.append(
                f"- {r.company} — {r.role} · {r.filename} · not marked RESUME_PREPARED · {r.url}"
            )
    leads = [(rec, ref) for rec, ref in report.records if rec.raw.source_url]
    if leads:
        L.append("## Community leads to check (Telegram / job blogs)")
        for rec, _ in leads[:25]:
            L.append(
                f"- {rec.ex.job.company} — {rec.ex.job.role} · {rec.raw.safety_verdict or 'n/a'} · "
                f"{rec.raw.source_name} ({rec.raw.source_trust.split(' ')[0] or 'n/a'} trust) · "
                f"apply link {rec.raw.url} · post {rec.raw.source_url}"
            )
    if result.pending:
        L.append("## Still waiting from earlier runs (top by match)")
        for p in result.pending[:15]:
            L.append(
                f"- {p['purpose'] or '-'} · {p['company']} — {p['role']} · match {p['score']:.0f} · "
                f"{p['status']} · company {p['verdict'] or 'UNVERIFIED'} · {p['url']}"
            )
    L.append("## Government IT jobs")
    if govt is None:
        L.append("- Government tracker not configured.")
    else:
        if govt.new:
            L.append(f"- New today: {len(govt.new)}")
            for g in govt.new[:20]:
                L.append(f"- NEW · {g.organisation} — {g.post} · {g.source} · {g.url}")
        else:
            L.append("- No new government CS/IT links today.")
        for d in govt.open_deadlines[:15]:
            L.append(
                f"- Deadline {d['last']} · {d['post']} · {d['fit'] or '-'} · {d['url'] or d['page']}"
            )
        if govt.pages_failed:
            L.append(f"- Official pages not reachable today: {len(govt.pages_failed)}")
    L.append("## Run health")
    L.append(
        f"- Sources ok {report.boards_ok}, failed {len(report.boards_failed)}; postings read "
        f"{report.fetched}; new rows {len(report.recorded)}; fresher-only skipped {report.fresher_only}; "
        f"errors {len(report.write_errors) + len(result.errors)}"
    )
    for b in report.boards_failed[:10]:
        L.append(f"- Source failed: {b}")
    L.append("## Reminders")
    L.append(
        "- LinkedIn, Naukri and Indeed jobs are never auto-applied: click Easy Apply yourself using the answer pack."
    )
    L.append(
        "- Government rows: open the official notification and check age, B.Tech %, GATE and last date before applying."
    )
    return "\n".join(L) + "\n"


def send_email(
    subject: str,
    body: str,
    user: str,
    password: str,
    to: str,
    attachments: list[tuple[str, bytes]] | None = None,
    host: str = "smtp.gmail.com",
    port: int = 465,
    smtp_factory: Any = smtplib.SMTP_SSL,
) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)
    for name, data in attachments or []:
        msg.add_attachment(
            data, maintype="application", subtype=DOCX_TYPE.split("/", 1)[1], filename=name
        )
    context = ssl.create_default_context()
    with smtp_factory(host, port, context=context, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)


def since(today: date, days: int = 14) -> str:
    return (today - timedelta(days=days)).isoformat()
