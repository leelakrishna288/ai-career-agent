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


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
