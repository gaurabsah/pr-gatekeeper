"""Gate 4: CI must be green. Pure evaluation of normalized check states."""
from __future__ import annotations

from .config import Config

GREEN, FAILED, PENDING = "green", "failed", "pending"

# GitHub check-run conclusions -> normalized state
_OK = {"success", "neutral", "skipped"}


def normalize_check_run(status: str, conclusion: str | None) -> str:
    if status != "completed":
        return "pending"
    return "success" if conclusion in _OK else "failure"  # unknown/None conclusions fail closed


def normalize_commit_status(state: str) -> str:
    return {"success": "success", "pending": "pending"}.get(state, "failure")


def evaluate_ci(checks: dict[str, str], cfg: Config) -> tuple[str, str]:
    """checks: name -> success | failure | pending. Returns (state, message). Fails closed."""
    ignored = {n.lower() for n in cfg.ignore_checks}
    relevant = {n: s for n, s in checks.items() if n.lower() not in ignored}

    if cfg.required_checks:
        missing = [n for n in cfg.required_checks if n not in relevant]
        relevant = {n: s for n, s in relevant.items() if n in cfg.required_checks}
        if missing:
            return PENDING, f"Required check(s) not reported yet: {', '.join(missing)}."
    elif not relevant:
        if cfg.allow_no_ci:
            return GREEN, "No CI checks configured."
        return PENDING, "No CI checks reported yet."

    failed = sorted(n for n, s in relevant.items() if s == "failure")
    if failed:
        return FAILED, f"Failing check(s): {', '.join(failed)}."
    pending = sorted(n for n, s in relevant.items() if s != "success")
    if pending:
        return PENDING, f"Check(s) still running: {', '.join(pending)}."
    return GREEN, f"{len(relevant)} check(s) passed."
