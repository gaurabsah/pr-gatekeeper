"""Deterministic first-pass risk rules. No network, no LLM."""
from __future__ import annotations

import re
from functools import lru_cache

from .config import Config
from .models import PullRequest, Risk, Signal


@lru_cache(maxsize=512)
def _glob_re(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3
        elif pattern.startswith("**", i):
            out.append(".*"); i += 2
        elif pattern[i] == "*":
            out.append("[^/]*"); i += 1
        elif pattern[i] == "?":
            out.append("[^/]"); i += 1
        else:
            out.append(re.escape(pattern[i])); i += 1
    return re.compile("^" + "".join(out) + "$")


def glob_match(path: str, patterns: list[str]) -> bool:
    return any(_glob_re(p).match(path) for p in patterns)


def _added_lines(patch: str) -> list[str]:
    return [l[1:] for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++")]


def added_line_numbers(patch: str | None) -> set[int]:
    """New-file line numbers of added lines in a unified diff (valid inline-comment targets)."""
    nums: set[int] = set()
    cur = 0
    for l in (patch or "").splitlines():
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", l)
        if m:
            cur = int(m.group(1))
        elif l.startswith("+") and not l.startswith("+++"):
            nums.add(cur); cur += 1
        elif l.startswith("-") or l.startswith("\\"):
            continue
        else:
            cur += 1
    return nums


def evaluate(pr: PullRequest, cfg: Config) -> list[Signal]:
    """Return signals. Any MAJOR signal means a human must approve."""
    sigs: list[Signal] = []
    major = lambda code, msg: sigs.append(Signal(Risk.MAJOR, code, msg))
    minor = lambda code, msg: sigs.append(Signal(Risk.MINOR, code, msg))

    if pr.draft and cfg.escalate_drafts:
        major("draft", "PR is a draft.")
    if pr.author in cfg.never_auto_approve_authors:
        major("author_blocked", f"Author {pr.author} is not eligible for auto-approval.")
    if cfg.allowed_base_branches and pr.base_ref not in cfg.allowed_base_branches:
        major("base_branch", f"Base branch '{pr.base_ref}' is not auto-approvable.")
    if not pr.files:
        major("no_files", "No changed files reported.")

    total = sum(f.changed_lines for f in pr.files)
    all_low_risk = bool(pr.files) and all(glob_match(f.path, cfg.low_risk_paths) for f in pr.files)
    line_limit = cfg.low_risk_max_changed_lines if all_low_risk else cfg.max_changed_lines

    if total > line_limit:
        major("too_large", f"{total} changed lines exceeds limit of {line_limit}.")
    if len(pr.files) > cfg.max_files and not all_low_risk:
        major("too_many_files", f"{len(pr.files)} files exceeds limit of {cfg.max_files}.")

    patterns = [re.compile(p) for p in cfg.risky_patterns]
    for f in pr.files:
        if glob_match(f.path, cfg.sensitive_paths):
            major("sensitive_path", f"{f.path} is in a sensitive area.")
        if glob_match(f.path.rsplit("/", 1)[-1], cfg.dependency_files) or glob_match(f.path, cfg.dependency_files):
            major("dependency_change", f"{f.path} changes dependencies.")
        if f.status in ("removed", "renamed"):
            major("structural_change", f"{f.path} was {f.status}.")
        if f.patch is None and f.changed_lines > 0:
            major("no_patch", f"{f.path} has no readable diff (binary or too large).")
            continue
        for line in _added_lines(f.patch or ""):
            hit = next((p.pattern for p in patterns if p.search(line)), None)
            if hit:
                major("risky_pattern", f"{f.path}: added line matches risky pattern `{hit}`.")
                break

    if not any(s.risk is Risk.MAJOR for s in sigs):
        minor("within_limits",
              f"{len(pr.files)} file(s), {total} changed line(s)"
              + (" (docs/tests only)" if all_low_risk else "") + ".")
    return sigs
