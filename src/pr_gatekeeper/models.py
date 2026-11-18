"""Shared data types."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Risk(str, Enum):
    MINOR = "minor"
    MAJOR = "major"


class Action(str, Enum):
    REPORT_ISSUES = "report_issues"  # issues found: post comments, never approve
    APPROVE = "approve"              # clean + minor: agent approves silently
    ESCALATE = "escalate"            # clean + major (or CI red): no comment, no approval, human decides
    WAIT_CI = "wait_ci"              # clean + minor but CI not finished: do nothing, re-run later


@dataclass(frozen=True)
class FileChange:
    path: str
    status: str            # added | modified | removed | renamed
    additions: int
    deletions: int
    patch: str | None = None   # None => GitHub omitted it (binary / too large)
    content: str | None = None  # full post-change file, used only as LLM context

    @property
    def changed_lines(self) -> int:
        return self.additions + self.deletions


@dataclass(frozen=True)
class PullRequest:
    repo: str              # "owner/name"
    number: int
    title: str
    body: str
    author: str
    head_sha: str
    base_ref: str
    files: tuple[FileChange, ...]
    draft: bool = False


@dataclass(frozen=True)
class Signal:
    """One reason that influenced the risk decision."""
    risk: Risk
    code: str              # stable machine-readable id, e.g. "sensitive_path"
    message: str


SEVERITIES = ("nit", "warning", "blocker")


@dataclass(frozen=True)
class Issue:
    path: str
    line: int | None       # line in the NEW file; None => not anchored
    severity: str          # nit | warning | blocker
    message: str


@dataclass(frozen=True)
class LLMVerdict:
    risk: Risk
    confidence: float      # 0..1
    summary: str
    concerns: tuple[str, ...] = ()
    issues: tuple[Issue, ...] = ()


@dataclass
class Decision:
    action: Action
    risk: Risk
    signals: list[Signal] = field(default_factory=list)
    llm: LLMVerdict | None = None
    issues: list[Issue] = field(default_factory=list)   # already filtered by severity

    def render(self) -> str:
        """Human/CLI summary of the decision (not what gets posted on the PR)."""
        head = {
            Action.REPORT_ISSUES: f"💬 **{len(self.issues)} issue(s) found**: comments will be posted, no approval.",
            Action.APPROVE: "✅ **Clean + minor**: auto-approve (no comment posted).",
            Action.ESCALATE: "🛑 **No auto-approval**: human review required (no comment posted).",
            Action.WAIT_CI: "⏳ **Clean + minor, waiting on CI**: nothing posted; re-run when checks finish.",
        }[self.action]
        lines = [head, ""]
        for i in self.issues:
            loc = f"{i.path}:{i.line}" if i.line else i.path
            lines.append(f"- [{i.severity}] {loc}: {i.message}")
        if self.issues:
            lines.append("")
        if self.signals:
            lines.append("**Classification signals**")
            for s in self.signals:
                lines.append(f"- `{s.code}` ({s.risk.value}): {s.message}")
            lines.append("")
        if self.llm:
            lines.append(f"**AI review** ({self.llm.risk.value}, confidence {self.llm.confidence:.2f}): "
                         f"{self.llm.summary}")
        return "\n".join(lines).strip()
