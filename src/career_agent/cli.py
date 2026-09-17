"""Command line interface."""

from __future__ import annotations

import json
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

from .models import JobPosting, Status
from .profile_store import load_profile
from .scoring import MAX_POINTS, Scorer
from .tailor import ResumeTailor
from .tracker import Tracker

app = typer.Typer(add_completion=False, help="AI Career Agent - job analysis and resume tailoring.")
console = Console()

DEFAULT_TRACKER = Path("data/applications.jsonl")


def _load_job(path: Path) -> JobPosting:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw) if path.suffix.lower() == ".json" else yaml.safe_load(raw)
    return JobPosting.model_validate(data)


@app.command()
def profile(path: Path = typer.Option(None, "--profile", "-p")) -> None:
    """Show what the master profile can and cannot support."""
    p = load_profile(path)
    table = Table(title=f"{p.name} - {p.years_experience:.1f} years")
    table.add_column("Label")
    table.add_column("Skills", overflow="fold")
    by_label: dict[str, list[str]] = {}
    for s in p.skills:
        by_label.setdefault(s.label.value, []).append(s.name)
    for label in ("VERIFIED", "PARTIALLY_VERIFIED", "TRANSFERABLE", "LEARNING", "UNSUPPORTED"):
        if label in by_label:
            table.add_row(label, ", ".join(sorted(by_label[label])))
    console.print(table)
    console.print(
        "\n[yellow]LEARNING and UNSUPPORTED skills are recorded deliberately - "
        "the validation gate blocks them from ever being printed as competencies.[/yellow]"
    )


@app.command()
def analyse(
    job_file: Path = typer.Argument(..., exists=True),
    profile_path: Path = typer.Option(None, "--profile", "-p"),
) -> None:
    """Score a job posting against the master profile."""
    p = load_profile(profile_path)
    job = _load_job(job_file)
    result = Scorer(p).score(job)

    colour = {"APPLY": "green", "MAYBE": "yellow", "MANUAL_REVIEW": "yellow", "DO_NOT_APPLY": "red"}
    console.print(f"\n[bold]{result.company}[/bold] - {result.role}")
    console.print(
        f"[{colour[result.decision.value]}]{result.decision.value}[/] · "
        f"score {result.score}/100 ({result.band}) · ATS keyword coverage {result.ats_keyword_coverage}%\n"
    )

    table = Table(show_header=True)
    table.add_column("Component")
    table.add_column("Score", justify="right")
    table.add_column("Max", justify="right")
    for key, maximum in MAX_POINTS.items():
        table.add_row(
            key.replace("_", " "), f"{getattr(result.breakdown, key):.1f}", f"{maximum:.0f}"
        )
    console.print(table)

    if result.hard_rejects:
        console.print("\n[red]Hard rejects[/red]")
        for r in result.hard_rejects:
            console.print(f"  · {r}")
    if result.learnable_gaps:
        console.print(f"\n[yellow]Learnable gaps[/yellow]: {', '.join(result.learnable_gaps)}")
    if result.critical_gaps:
        console.print(f"[red]Critical gaps (do not claim)[/red]: {', '.join(result.critical_gaps)}")
    console.print("")
    for r in result.reasons:
        console.print(f"  · {r}")


@app.command()
def tailor(
    job_file: Path = typer.Argument(..., exists=True),
    out: Path = typer.Option(Path("resumes"), "--out", "-o"),
    profile_path: Path = typer.Option(None, "--profile", "-p"),
) -> None:
    """Generate a role-specific resume and run the validation gate."""
    p = load_profile(profile_path)
    job = _load_job(job_file)
    analysis = Scorer(p).score(job)
    resume = ResumeTailor(p).tailor(job, analysis)

    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{resume.resume_id}.json"
    path.write_text(resume.model_dump_json(indent=2), encoding="utf-8")

    console.print(f"\n[bold]{resume.resume_id}[/bold]")
    console.print(f"  match score : {resume.match_score}")
    console.print(f"  headline    : {resume.headline}")
    console.print(f"  written to  : {path}")
    for change in resume.changes:
        console.print(f"  [dim]· {change}[/dim]")

    if resume.is_final:
        console.print(
            "\n[green]VALIDATION PASSED[/green] - every claim traces to the master profile."
        )
    else:
        console.print("\n[red]VALIDATION FAILED - not marked final.[/red]")
        for issue in resume.validation.issues if resume.validation else []:
            style = "red" if issue.severity == "blocking" else "yellow"
            console.print(f"  [{style}]{issue.severity}[/] {issue.kind}: {issue.detail}")
        raise typer.Exit(1)


@app.command()
def track(
    job_file: Path = typer.Argument(..., exists=True),
    tracker_path: Path = typer.Option(DEFAULT_TRACKER, "--tracker"),
    profile_path: Path = typer.Option(None, "--profile", "-p"),
) -> None:
    """Add a job to the tracker (deduplicated)."""
    p = load_profile(profile_path)
    job = _load_job(job_file)
    result = Scorer(p).score(job)
    application, created = Tracker(tracker_path).add(job, result.score, result.decision)
    console.print(
        f"[{'green' if created else 'yellow'}]"
        f"{'added' if created else 'already tracked'}[/] {application.application_id} - "
        f"{application.company} / {application.role} (score {application.match_score})"
    )


@app.command()
def status(
    application_id: str = typer.Argument(...),
    new_status: str = typer.Argument(...),
    note: str = typer.Option("", "--note"),
    tracker_path: Path = typer.Option(DEFAULT_TRACKER, "--tracker"),
) -> None:
    """Move an application to a new status."""
    try:
        application = Tracker(tracker_path).update_status(
            application_id, Status(new_status.upper()), note
        )
    except ValueError:
        console.print(f"[red]Unknown status.[/red] Valid: {', '.join(s.value for s in Status)}")
        raise typer.Exit(2) from None
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from None
    console.print(f"{application.application_id} -> [green]{application.status.value}[/green]")


@app.command()
def pipeline(
    tracker_path: Path = typer.Option(DEFAULT_TRACKER, "--tracker"),
    export: Path = typer.Option(None, "--export", help="Also write a CSV"),
) -> None:
    """Show the application pipeline."""
    t = Tracker(tracker_path)
    applications = t.all()
    if not applications:
        console.print("[yellow]No applications tracked yet.[/yellow]")
        return
    table = Table(title=f"Pipeline - {len(applications)} applications")
    for col in ("ID", "Company", "Role", "Score", "Decision", "Status"):
        table.add_column(col, overflow="fold")
    for a in applications:
        table.add_row(
            a.application_id,
            a.company,
            a.role[:44],
            f"{a.match_score:.0f}",
            a.decision.value,
            a.status.value,
        )
    console.print(table)
    console.print(f"\n{json.dumps(t.pipeline())}")
    if export:
        console.print(f"CSV written to {t.export_csv(export)}")


@app.command()
def discover(
    config_path: Path = typer.Option(Path("config/discovery.yaml"), "--config", "-c"),
    profile_path: Path = typer.Option(None, "--profile", "-p"),
    notion: bool = typer.Option(False, "--notion", help="Write new rows to the Notion tracker"),
    report_dir: Path = typer.Option(Path("reports"), "--report-dir"),
    public_summary: Path = typer.Option(
        None, "--public-summary", help="Also write a counts-only summary (safe for public CI logs)"
    ),
) -> None:
    """Standalone discovery: public ATS boards -> gates -> score -> tracker.

    Needs no LLM. With --notion, reads NOTION_TOKEN, NOTION_DATA_SOURCE_ID and
    (optionally) NOTION_REPORT_PAGE_ID from the environment.
    """
    import logging
    import os
    from datetime import date

    from .discovery.http import UrllibJsonClient
    from .discovery.notion_sink import MemorySink, NotionTrackerSink, render_report
    from .discovery.pipeline import DiscoveryConfig, run_discovery

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = DiscoveryConfig.load(config_path)
    client = UrllibJsonClient()
    sink: NotionTrackerSink | MemorySink
    if notion:
        try:
            sink = NotionTrackerSink(
                client,
                os.environ.get("NOTION_TOKEN", ""),
                os.environ.get("NOTION_DATA_SOURCE_ID", ""),
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from None
    else:
        sink = MemorySink()
        console.print("[yellow]Dry run: nothing is written to Notion (use --notion).[/yellow]")

    report = run_discovery(cfg, load_profile(profile_path), client, sink)
    markdown = render_report(report)
    report_dir.mkdir(parents=True, exist_ok=True)
    out = report_dir / f"discovery_{date.today().isoformat()}.md"
    out.write_text(markdown, encoding="utf-8")
    console.print(markdown)
    if public_summary:
        public_summary.write_text(render_report(report, public=True), encoding="utf-8")
    page_id = os.environ.get("NOTION_REPORT_PAGE_ID", "")
    if notion and page_id and isinstance(sink, NotionTrackerSink):
        try:
            url = sink.write_report(page_id, f"Standalone discovery — {report.run_date}", markdown)
            console.print(f"Notion report page: {url}")
        except Exception as exc:  # the rows are already written; do not fail the run
            console.print(f"[red]Could not write the Notion report page: {exc}[/red]")
    if report.boards_ok == 0:
        console.print("[red]Every board failed - see the report.[/red]")
        raise typer.Exit(1)
    if report.write_errors:
        raise typer.Exit(1)


@app.command()
def daily(
    config_path: Path = typer.Option(Path("config/discovery.yaml"), "--config", "-c"),
    profile_path: Path = typer.Option(None, "--profile", "-p"),
    notion: bool = typer.Option(False, "--notion", help="Read/write the Notion trackers"),
    email: bool = typer.Option(False, "--email", help="Email the digest (needs GMAIL_* env)"),
    out_dir: Path = typer.Option(Path("reports"), "--out", help="Digest and resume files"),
    public_summary: Path = typer.Option(None, "--public-summary"),
    save_resumes: bool = typer.Option(
        False, "--save-resumes", help="Also write DOCX files to --out (never in public CI)"
    ),
) -> None:
    """The full daily run without Claude: discover (ATS boards, job boards,
    Telegram, job blogs) -> score -> tailored resumes with ESTIMATED ATS ->
    government IT jobs -> digest in Notion and email. Never submits anything.

    Environment: NOTION_TOKEN, NOTION_DATA_SOURCE_ID, NOTION_DIGEST_PAGE_ID,
    NOTION_GOVT_DATA_SOURCE_ID (all optional when set in the config),
    GMAIL_USER, GMAIL_APP_PASSWORD, DIGEST_TO, GOOGLE_SAFE_BROWSING_KEY,
    VIRUSTOTAL_API_KEY.
    """
    import logging
    import os
    from datetime import date

    from .discovery.daily import DailyResult, build_digest, prepare_resumes, send_email, since
    from .discovery.govt import GovtSink, run_govt
    from .discovery.http import UrllibJsonClient
    from .discovery.notion_sink import MemorySink, NotionTrackerSink, render_report
    from .discovery.pipeline import DiscoveryConfig, run_discovery
    from .discovery.safety import ReputationChecker

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    env = os.environ.get
    cfg = DiscoveryConfig.load(config_path)
    prof = load_profile(profile_path)
    client = UrllibJsonClient()
    client.reputation = ReputationChecker(  # type: ignore[attr-defined]
        client, env("GOOGLE_SAFE_BROWSING_KEY", ""), env("VIRUSTOTAL_API_KEY", "")
    )
    today = date.today()
    sink: NotionTrackerSink | MemorySink
    govt_sink = None
    report_page = env("NOTION_DIGEST_PAGE_ID", "") or cfg.notion.report_page_id
    if notion:
        token = env("NOTION_TOKEN", "")
        try:
            sink = NotionTrackerSink(client, token, env("NOTION_DATA_SOURCE_ID", ""))
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from None
        govt_ds = env("NOTION_GOVT_DATA_SOURCE_ID", "") or cfg.notion.govt_data_source_id
        if govt_ds:
            govt_sink = GovtSink(client, token, govt_ds)
    else:
        sink = MemorySink()
        console.print("[yellow]Dry run: nothing is written to Notion (use --notion).[/yellow]")

    report = run_discovery(cfg, prof, client, sink, today=today)
    result = DailyResult()
    prepare_resumes(report, prof, cfg, sink if notion else None, result)
    if notion and isinstance(sink, NotionTrackerSink):
        try:
            result.pending = sink.pending_rows(since(today))
        except Exception as exc:
            result.errors.append(f"reading pending rows: {exc}")
    govt = run_govt(
        cfg.govt_watch,
        client,
        report.govt_leads,
        govt_sink,
        today=today,
        max_new=cfg.max_govt_new_per_run,
        reputation=client.reputation,  # type: ignore[attr-defined]
    )
    digest = build_digest(today.isoformat(), report, result, govt)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"digest_{today.isoformat()}.md").write_text(digest, encoding="utf-8")
    if save_resumes:
        for r in result.resumes:
            (out_dir / r.filename).write_bytes(r.docx)
    console.print(digest)

    failures: list[str] = list(report.write_errors) + result.errors + govt.write_errors
    if notion and report_page and isinstance(sink, NotionTrackerSink):
        try:
            url = sink.write_report(report_page, f"Daily jobs — {today.isoformat()}", digest)
            console.print(f"Notion digest page: {url}")
        except Exception as exc:
            failures.append(f"Notion digest page: {exc}")
    if email:
        user, password = env("GMAIL_USER", ""), env("GMAIL_APP_PASSWORD", "")
        if not user or not password:
            console.print(
                "[yellow]Email skipped: GMAIL_USER / GMAIL_APP_PASSWORD not set.[/yellow]"
            )
        else:
            ready = [r for r in result.resumes if r.ready][:8]
            n_new = len([r for r in result.resumes if r.ready])
            subject = (
                f"[Career Agent] {today.isoformat()}: {n_new} ready, "
                f"{len(govt.new)} govt, {report.community_manual} leads"
            )
            try:
                send_email(
                    subject,
                    digest,
                    user,
                    password,
                    env("DIGEST_TO", "") or user,
                    [(r.filename, r.docx) for r in ready],
                )
                console.print("Digest emailed.")
            except Exception as exc:
                failures.append(f"email: {type(exc).__name__}: {exc}")
    if public_summary:
        public_summary.write_text(
            render_report(report, public=True) + f"- Resumes prepared: {len(result.resumes)} "
            f"({sum(r.ready for r in result.resumes)} ready)\n"
            f"- Government: {govt.pages_ok} official pages read, {len(govt.new)} new links\n"
            f"- Other errors: {len(failures)}\n",
            encoding="utf-8",
        )
    for f in failures:
        console.print(f"[red]{f}[/red]")
    if report.boards_ok == 0 or failures:
        raise typer.Exit(1)


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
