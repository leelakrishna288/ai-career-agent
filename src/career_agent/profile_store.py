"""Loading and saving the master profile."""

from __future__ import annotations

from pathlib import Path

import yaml

from .models import MasterProfile

DEFAULT_PROFILE = Path(__file__).resolve().parents[2] / "profile" / "master_profile.yaml"


def load_profile(path: Path | str | None = None) -> MasterProfile:
    p = Path(path or DEFAULT_PROFILE)
    if not p.is_file():
        raise FileNotFoundError(f"master profile not found: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return MasterProfile.model_validate(data)


def save_profile(profile: MasterProfile, path: Path | str) -> Path:
    """Explicit save only. The master profile is never modified automatically -
    an agent that can silently edit its own source of truth has no source of
    truth."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        yaml.safe_dump(profile.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return p
