import json

from tests.conftest import ROOT
from typer.testing import CliRunner

from career_agent.cli import app

runner = CliRunner()
PROFILE = str(ROOT / "profile" / "master_profile.yaml")
GOOD_JOB = str(ROOT / "examples" / "jpmorgan_hyderabad.yaml")
BAD_JOB = str(ROOT / "examples" / "senior_ml_riyadh.yaml")


def test_profile_shows_never_printable_skills():
    result = runner.invoke(app, ["profile", "--profile", PROFILE])
    assert result.exit_code == 0
    assert "LEARNING" in result.stdout
    assert "blocks them from ever being printed" in result.stdout


def test_analyse_recommends_a_good_match():
    result = runner.invoke(app, ["analyse", GOOD_JOB, "--profile", PROFILE])
    assert result.exit_code == 0
    assert "APPLY" in result.stdout


def test_analyse_rejects_an_impossible_role_with_reasons():
    result = runner.invoke(app, ["analyse", BAD_JOB, "--profile", PROFILE])
    assert result.exit_code == 0
    assert "DO_NOT_APPLY" in result.stdout
    assert "Hard rejects" in result.stdout
    assert "PhD" in result.stdout


def test_tailor_exits_nonzero_when_validation_blocks(tmp_path):
    """The repos are not yet marked shipped, so the gate must refuse to
    finalise - and the CLI must exit non-zero so a pipeline notices."""
    result = runner.invoke(app, ["tailor", GOOD_JOB, "--profile", PROFILE, "-o", str(tmp_path)])
    assert result.exit_code == 1
    assert "VALIDATION FAILED" in result.stdout
    written = list(tmp_path.glob("RESUME_*.json"))
    assert len(written) == 1
    saved = json.loads(written[0].read_text())
    assert saved["validation"]["passed"] is False


def test_track_then_pipeline_then_export(tmp_path):
    tracker = str(tmp_path / "apps.jsonl")
    first = runner.invoke(app, ["track", GOOD_JOB, "--profile", PROFILE, "--tracker", tracker])
    assert first.exit_code == 0 and "added" in first.stdout

    again = runner.invoke(app, ["track", GOOD_JOB, "--profile", PROFILE, "--tracker", tracker])
    assert "already tracked" in again.stdout

    csv_path = tmp_path / "out.csv"
    listing = runner.invoke(app, ["pipeline", "--tracker", tracker, "--export", str(csv_path)])
    assert listing.exit_code == 0
    assert "JPMorgan" in listing.stdout  # rich wraps long cells at narrow widths
    assert csv_path.exists()


def test_status_rejects_an_unknown_value(tmp_path):
    tracker = str(tmp_path / "apps.jsonl")
    runner.invoke(app, ["track", GOOD_JOB, "--profile", PROFILE, "--tracker", tracker])
    result = runner.invoke(app, ["status", "APP-0001-x", "NOT_A_STATUS", "--tracker", tracker])
    assert result.exit_code == 2
    assert "Unknown status" in result.stdout


def test_pipeline_is_empty_before_anything_is_tracked(tmp_path):
    result = runner.invoke(app, ["pipeline", "--tracker", str(tmp_path / "none.jsonl")])
    assert result.exit_code == 0
    assert "No applications tracked yet" in result.stdout
