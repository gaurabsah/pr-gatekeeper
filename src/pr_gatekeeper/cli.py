from __future__ import annotations

import argparse
import sys

from .config import Config
from .gatekeeper import Gatekeeper
from .github import GitHubClient
from .models import Action


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pr-gatekeeper")
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--pr", required=True, type=int)
    p.add_argument("--config", default=".pr-gatekeeper.yml")
    p.add_argument("--dry-run", action="store_true", help="classify only; don't post a review")
    p.add_argument("--no-llm", action="store_true", help="rules only")
    p.add_argument("--provider", help="anthropic | openai (any OpenAI-compatible API)")
    p.add_argument("--model")
    p.add_argument("--base-url", help="API base URL, e.g. http://localhost:11434/v1")
    a = p.parse_args(argv)

    cfg = Config.load(a.config)
    cfg.llm_provider = a.provider or cfg.llm_provider
    cfg.llm_model = a.model or cfg.llm_model
    cfg.llm_base_url = a.base_url or cfg.llm_base_url
    if a.no_llm:
        cfg.use_llm = False
    decision = Gatekeeper(cfg).run(GitHubClient(), a.repo, a.pr, dry_run=a.dry_run)
    print(decision.render())
    # Exit 0 either way so CI doesn't go red on "needs a human"; use labels/outputs to branch.
    return 0 if decision.action in (Action.APPROVE, Action.ESCALATE) else 1


if __name__ == "__main__":
    sys.exit(main())
