"""Standalone job discovery runtime.

Runs without any LLM subscription: public ATS job-board APIs in, deterministic
scoring in the middle, the Notion application tracker out. Designed for a
GitHub Actions schedule, so it keeps working while the laptop is off.
"""

from .pipeline import DiscoveryConfig, RunReport, run_discovery

__all__ = ["DiscoveryConfig", "RunReport", "run_discovery"]
