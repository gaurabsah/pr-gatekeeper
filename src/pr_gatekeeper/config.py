"""Policy configuration. Defaults are deliberately conservative."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Config:
    # Size limits for auto-approval.
    max_changed_lines: int = 50
    max_files: int = 5

    # Any touched path matching these globs => major (human must approve).
    sensitive_paths: list[str] = field(default_factory=lambda: [
        ".github/workflows/**", "**/Dockerfile*", "**/*.tf", "**/terraform/**",
        "**/migrations/**", "**/auth/**", "**/security/**", "**/*secret*",
        "**/*credential*", "**/.env*", "CODEOWNERS", ".github/CODEOWNERS",
    ])
    # Dependency / lock files: changes are major by default.
    dependency_files: list[str] = field(default_factory=lambda: [
        "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
        "requirements*.txt", "pyproject.toml", "poetry.lock", "uv.lock",
        "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "pom.xml", "build.gradle*",
    ])
    # Paths that are always safe on their own (docs, tests...). Used for the
    # "docs/tests only" fast-path, which relaxes the size limits.
    low_risk_paths: list[str] = field(default_factory=lambda: [
        "docs/**", "**/*.md", "**/*.rst", "**/*.txt", "tests/**", "**/test_*.py", "**/*.test.*",
    ])
    low_risk_max_changed_lines: int = 300

    # Regexes matched against added lines; a hit => major.
    risky_patterns: list[str] = field(default_factory=lambda: [
        r"(?i)\b(password|passwd|secret|api[_-]?key|token)\b\s*[:=]\s*['\"][^'\"]{6,}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\beval\(", r"\bexec\(", r"subprocess\.\w+\(.*shell\s*=\s*True",
        r"(?i)\bdrop\s+table\b", r"(?i)\bdisable.*(auth|ssl|verify|csrf)",
    ])

    # Authors whose PRs are never auto-approved (e.g. new contributors) / always escalated.
    never_auto_approve_authors: list[str] = field(default_factory=list)
    # Only auto-approve PRs targeting these base branches (empty => any).
    allowed_base_branches: list[str] = field(default_factory=list)

    # LLM stage.
    use_llm: bool = True
    llm_provider: str = "anthropic"          # anthropic | openai (any OpenAI-compatible API) | custom
    llm_model: str | None = None             # anthropic defaults to claude-sonnet-5-5; others must set it
    llm_base_url: str | None = None          # e.g. http://localhost:11434/v1 for Ollama
    llm_api_key_env: str | None = None       # env var holding the key (default per provider)
    llm_min_confidence: float = 0.8
    max_diff_chars: int = 60_000  # larger diffs are escalated, never truncated-and-approved

    # Gate 4: CI must be green before approving.
    require_ci_green: bool = True
    required_checks: list[str] = field(default_factory=list)   # empty => every check must pass
    ignore_checks: list[str] = field(default_factory=lambda: ["gatekeeper"])  # this job's own name
    allow_no_ci: bool = False          # repos with no CI at all
    # Opt-in: after approving, enable GitHub native auto-merge (branch protection still decides when).
    auto_merge: bool = False
    auto_merge_method: str = "SQUASH"  # SQUASH | MERGE | REBASE

    # Extra context: send full changed files (not just diffs) to the reviewer.
    include_file_context: bool = True
    context_max_file_chars: int = 20_000
    context_max_total_chars: int = 80_000

    # Issue reporting.
    min_issue_severity: str = "warning"      # nit | warning | blocker; lower ones are dropped
    max_inline_comments: int = 20
    issues_review_event: str = "COMMENT"     # or "REQUEST_CHANGES"
    label_on_escalate: bool = True           # label only; never a comment

    # Reviewer identity safety: also escalate drafts.
    escalate_drafts: bool = True

    @classmethod
    def load(cls, path: str | Path | None) -> "Config":
        if path is None or not Path(path).exists():
            return cls()
        data = yaml.safe_load(Path(path).read_text()) or {}
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unknown config keys: {sorted(unknown)}")
        return cls(**data)
