"""Orchestrator.

Flow:
  1. Scan the PR for issues (LLM). Issues found  -> post comments, never approve.
  2. No issues: post NOTHING; classify minor/major (rules + LLM).
       minor -> approve silently      major -> no approval (label only)
"""
from __future__ import annotations

import hashlib

from .ci import FAILED, GREEN, evaluate_ci
from .config import Config
from .llm import Reviewer, make_reviewer
from .models import SEVERITIES, Action, Decision, Issue, PullRequest, Risk, Signal
from .rules import added_line_numbers, evaluate

LABEL_APPROVED = "auto-approved"
LABEL_MANUAL = "needs-human-review"
LABEL_ISSUES = "issues-found"


def issue_key(i: Issue) -> str:
    """Stable id independent of line number, so shifted lines don't re-trigger comments."""
    return hashlib.sha1(f"{i.path}|{i.message.strip().lower()}".encode()).hexdigest()[:12]


def marker(i: Issue) -> str:
    return f"<!-- pr-gatekeeper:{issue_key(i)} -->"


class Gatekeeper:
    def __init__(self, cfg: Config | None = None, reviewer: Reviewer | None = None):
        self.cfg = cfg or Config()
        if reviewer is None and self.cfg.use_llm:
            reviewer = make_reviewer(self.cfg)
        self.reviewer = reviewer

    # ---- decision -------------------------------------------------------
    def decide(self, pr: PullRequest) -> Decision:
        signals = evaluate(pr, self.cfg)
        rules_major = any(s.risk is Risk.MAJOR for s in signals)

        if not self.cfg.use_llm or self.reviewer is None:
            # Rules-only mode: no issue scan possible.
            return self._classified(rules_major, signals, None, [])

        try:
            verdict = self.reviewer.review(pr)
        except Exception as e:  # fail closed: no comments, no approval
            signals.append(Signal(Risk.MAJOR, "llm_error", f"AI review failed: {e!r}"))
            return Decision(Action.ESCALATE, Risk.MAJOR, signals)

        issues = self._filter_issues(pr, verdict.issues)
        if issues:
            risk = Risk.MAJOR if rules_major or verdict.risk is Risk.MAJOR else Risk.MINOR
            return Decision(Action.REPORT_ISSUES, risk, signals, verdict, issues)

        if verdict.risk is Risk.MAJOR:
            signals.append(Signal(Risk.MAJOR, "llm_major", "AI reviewer classified change as major."))
            rules_major = True
        elif verdict.confidence < self.cfg.llm_min_confidence:
            signals.append(Signal(Risk.MAJOR, "llm_low_confidence",
                                  f"AI confidence {verdict.confidence:.2f} < {self.cfg.llm_min_confidence}."))
            rules_major = True
        return self._classified(rules_major, signals, verdict, [])

    @staticmethod
    def _classified(major: bool, signals, verdict, issues) -> Decision:
        if major:
            return Decision(Action.ESCALATE, Risk.MAJOR, signals, verdict, issues)
        return Decision(Action.APPROVE, Risk.MINOR, signals, verdict, issues)

    def _filter_issues(self, pr: PullRequest, issues: tuple[Issue, ...]) -> list[Issue]:
        floor = SEVERITIES.index(self.cfg.min_issue_severity)
        paths = {f.path for f in pr.files}
        out = [i for i in issues if i.path in paths and SEVERITIES.index(i.severity) >= floor]
        return sorted(out, key=lambda i: -SEVERITIES.index(i.severity))

    # ---- gate 4 ---------------------------------------------------------
    def apply_ci_gate(self, github, pr: PullRequest, decision: Decision) -> Decision:
        """Only an otherwise-approvable PR needs green CI. Fails closed on any API error."""
        if not self.cfg.require_ci_green or decision.action is not Action.APPROVE:
            return decision
        try:
            state, msg = evaluate_ci(github.get_check_states(pr.repo, pr.head_sha), self.cfg)
        except Exception as e:
            state, msg = "pending", f"Could not read CI status: {e!r}"
        if state == GREEN:
            decision.signals.append(Signal(Risk.MINOR, "ci_green", msg))
            return decision
        decision.signals.append(Signal(Risk.MAJOR, "ci_failed" if state == FAILED else "ci_pending", msg))
        decision.action = Action.ESCALATE if state == FAILED else Action.WAIT_CI
        return decision

    # ---- acting on GitHub -----------------------------------------------
    def build_review(self, pr: PullRequest, decision: Decision) -> tuple[str, list[dict]]:
        """Split issues into inline comments (valid diff lines) and body text."""
        valid = {f.path: added_line_numbers(f.patch) for f in pr.files}
        inline, leftover = [], []
        for i in decision.issues:
            if (i.line in valid.get(i.path, set())) and len(inline) < self.cfg.max_inline_comments:
                inline.append({"path": i.path, "line": i.line, "side": "RIGHT",
                               "body": f"**{i.severity}**: {i.message}\n\n{marker(i)}"})
            else:
                loc = f"`{i.path}:{i.line}`" if i.line else f"`{i.path}`"
                leftover.append(f"- **{i.severity}** {loc}: {i.message} {marker(i)}")
        body = f"pr-gatekeeper found {len(decision.issues)} issue(s)."
        if decision.llm and decision.llm.summary:
            body += f"\n\n{decision.llm.summary}"
        if leftover:
            body += "\n\n" + "\n".join(leftover)
        return body, inline

    def run(self, github, repo: str, number: int, dry_run: bool = False) -> Decision:
        pr = github.get_pull_request(
            repo, number,
            with_content=self.cfg.use_llm and self.cfg.include_file_context,
            max_file_chars=self.cfg.context_max_file_chars,
            max_total_chars=self.cfg.context_max_total_chars)
        decision = self.apply_ci_gate(github, pr, self.decide(pr))
        if dry_run:
            return decision

        if decision.action is Action.REPORT_ISSUES:
            seen = github.existing_markers(repo, number)
            decision.issues = [i for i in decision.issues if issue_key(i) not in seen]
            if not decision.issues:
                return decision   # everything already reported on an earlier run; stay quiet
            body, inline = self.build_review(pr, decision)
            github.submit_review(repo, number, pr.head_sha, self.cfg.issues_review_event, body, inline)
            github.add_labels(repo, number, [LABEL_ISSUES])
        elif decision.action is Action.APPROVE:
            github.submit_review(repo, number, pr.head_sha, "APPROVE", "", [])  # silent approval
            github.add_labels(repo, number, [LABEL_APPROVED])
            if self.cfg.auto_merge:
                try:
                    github.enable_auto_merge(repo, number, self.cfg.auto_merge_method)
                    decision.signals.append(Signal(Risk.MINOR, "auto_merge", "Auto-merge enabled."))
                except Exception as e:  # e.g. repo disallows auto-merge; approval already stands
                    decision.signals.append(Signal(Risk.MINOR, "auto_merge_failed", repr(e)))
        elif decision.action is Action.WAIT_CI:
            pass  # nothing to post; the gatekeeper re-runs when CI completes
        elif self.cfg.label_on_escalate:
            github.add_labels(repo, number, [LABEL_MANUAL])  # label only, no comment
        return decision
