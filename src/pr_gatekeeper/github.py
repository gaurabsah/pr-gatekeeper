"""Minimal GitHub REST client (only what the gatekeeper needs)."""
from __future__ import annotations

import os
import re
from dataclasses import replace

import httpx

from .models import FileChange, PullRequest

MARKER_RE = re.compile(r"pr-gatekeeper:([0-9a-f]{12})")


class GitHubClient:
    def __init__(self, token: str | None = None, api_url: str = "https://api.github.com",
                 client: httpx.Client | None = None):
        token = token or os.environ.get("GITHUB_TOKEN")
        if not token and client is None:
            raise RuntimeError("GITHUB_TOKEN is required")
        self.http = client or httpx.Client(
            base_url=api_url, timeout=30,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )

    def get_file_content(self, repo: str, path: str, ref: str, max_chars: int) -> str | None:
        r = self.http.get(f"/repos/{repo}/contents/{path}", params={"ref": ref},
                          headers={"Accept": "application/vnd.github.raw+json"})
        if r.status_code != 200:
            return None
        try:
            text = r.text
        except Exception:
            return None
        return text if len(text) <= max_chars and "\x00" not in text else None

    def existing_markers(self, repo: str, number: int) -> set[str]:
        """Dedupe keys of issues pr-gatekeeper already reported on this PR."""
        found: set[str] = set()
        for kind in ("comments", "reviews"):
            page = 1
            while True:
                r = self.http.get(f"/repos/{repo}/pulls/{number}/{kind}",
                                  params={"per_page": 100, "page": page})
                r.raise_for_status()
                batch = r.json()
                for item in batch:
                    found.update(MARKER_RE.findall(item.get("body") or ""))
                if len(batch) < 100:
                    break
                page += 1
        return found

    def get_pull_request(self, repo: str, number: int, *, with_content: bool = False,
                         max_file_chars: int = 20_000, max_total_chars: int = 80_000) -> PullRequest:
        pr = self.http.get(f"/repos/{repo}/pulls/{number}")
        pr.raise_for_status()
        d = pr.json()
        files: list[FileChange] = []
        page = 1
        while True:
            r = self.http.get(f"/repos/{repo}/pulls/{number}/files",
                              params={"per_page": 100, "page": page})
            r.raise_for_status()
            batch = r.json()
            files += [FileChange(f["filename"], f["status"], f["additions"],
                                 f["deletions"], f.get("patch")) for f in batch]
            if len(batch) < 100:
                break
            page += 1
        sha = d["head"]["sha"]
        if with_content:
            budget = max_total_chars
            for idx, f in enumerate(files):
                if f.status == "removed" or f.patch is None or budget <= 0:
                    continue
                text = self.get_file_content(repo, f.path, sha, min(max_file_chars, budget))
                if text is not None:
                    budget -= len(text)
                    files[idx] = replace(f, content=text)
        return PullRequest(
            repo=repo, number=number, title=d["title"], body=d.get("body") or "",
            author=d["user"]["login"], head_sha=sha, base_ref=d["base"]["ref"],
            files=tuple(files), draft=bool(d.get("draft")),
        )

    def submit_review(self, repo: str, number: int, head_sha: str, event: str, body: str,
                      comments: list[dict] | None = None) -> None:
        """event: APPROVE | COMMENT | REQUEST_CHANGES. Pinned to head_sha so a
        later push can't inherit an approval given to different code.
        comments: inline comments [{path, line, side, body}]."""
        payload = {"commit_id": head_sha, "event": event, "body": body}
        if comments:
            payload["comments"] = comments
        r = self.http.post(f"/repos/{repo}/pulls/{number}/reviews", json=payload)
        r.raise_for_status()

    def get_check_states(self, repo: str, sha: str) -> dict[str, str]:
        """name -> success | failure | pending, from check runs plus legacy commit statuses."""
        from .ci import normalize_check_run, normalize_commit_status
        states: dict[str, str] = {}
        page = 1
        while True:
            r = self.http.get(f"/repos/{repo}/commits/{sha}/check-runs",
                              params={"per_page": 100, "page": page})
            r.raise_for_status()
            runs = r.json().get("check_runs", [])
            for c in runs:
                new = normalize_check_run(c["status"], c.get("conclusion"))
                old = states.get(c["name"])
                # a re-run can leave several runs with one name: the worst one counts
                rank = {"failure": 2, "pending": 1, "success": 0}
                if old is None or rank[new] > rank[old]:
                    states[c["name"]] = new
            if len(runs) < 100:
                break
            page += 1
        r = self.http.get(f"/repos/{repo}/commits/{sha}/status")
        r.raise_for_status()
        for s in r.json().get("statuses", []):
            states.setdefault(s["context"], normalize_commit_status(s["state"]))
        return states

    def enable_auto_merge(self, repo: str, number: int, method: str = "SQUASH") -> None:
        """Turn on GitHub native auto-merge; branch protection still gates the actual merge."""
        pr = self.http.get(f"/repos/{repo}/pulls/{number}")
        pr.raise_for_status()
        q = ("mutation($id:ID!,$m:PullRequestMergeMethod!){enablePullRequestAutoMerge("
             "input:{pullRequestId:$id,mergeMethod:$m}){pullRequest{number}}}")
        r = self.http.post("/graphql", json={"query": q, "variables": {"id": pr.json()["node_id"], "m": method}})
        r.raise_for_status()
        if r.json().get("errors"):
            raise RuntimeError(r.json()["errors"][0].get("message", "auto-merge failed"))

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        r = self.http.post(f"/repos/{repo}/issues/{number}/labels", json={"labels": labels})
        r.raise_for_status()
