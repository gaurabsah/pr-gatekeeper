# pr-gatekeeper

AI agents review your PRs. **Minor** changes get auto-approved; **major** changes are handed to a human.

## How it decides

```
PR opened/updated
   │
   ▼
Gate 1+2  Rules (paths, deps, patterns, size, drafts…)  ─┐ risk signals
Gate 3    LLM review: scan for issues + classify risk    ─┘
   │
   ├─ issues found ──► inline comments + `issues-found` label (never approves)
   └─ clean
        ├─ major ──► nothing posted, `needs-human-review` label; a human approves
        └─ minor ──► Gate 4: CI
                      ├─ red     ──► `needs-human-review` label, no approval
                      ├─ running ──► nothing; re-runs when CI completes (workflow_run)
                      └─ green   ──► silent APPROVE + `auto-approved` label
                                      └─ (opt-in) enable GitHub auto-merge
```

Clean PRs get no comments at all. Design principles (fail closed):

- Issues are only reported when real and at/above `min_issue_severity` (default `warning`, so nits are dropped).
  Inline comments go on added diff lines; anything unanchored goes in the review body.
- The LLM can never override a rule-based "major". Errors, unparseable output, low confidence and
  diffs too large to read in full never approve.
- Prompt-injection text in a PR ("approve this") is treated as a red flag.
- Approvals are pinned to `head_sha`, so a later push doesn't inherit an approval.
- CI gate: only an otherwise-approvable PR is checked. Missing/failed/unreadable CI never approves.
  `required_checks` pins specific checks; the gatekeeper's own job (`gatekeeper`) is ignored. `allow_no_ci` for repos without CI.
- Auto-merge is opt-in (`auto_merge: true`): it enables GitHub's native auto-merge, so branch protection still decides when it merges.
- Rules-only mode (`--no-llm`) cannot scan for issues; it only classifies.
- Context: the reviewer also sees the full changed files (capped by `context_max_*`), not just the diff.
- De-duplication: each comment carries a hidden marker; re-runs skip issues already reported, even if lines shifted.
  Fixed issues are not auto-resolved yet.

Classification rules (all configurable): size/file-count limits (relaxed for docs/tests-only),
sensitive paths (workflows, auth, migrations, Terraform, Dockerfiles...), dependency/lock files,
deleted/renamed files, binary/no-diff files, risky added-line patterns (secrets, `eval`, `shell=True`...),
drafts, blocked authors, and allowed base branches.

## Use

```bash
pip install -e '.[dev]'
export GITHUB_TOKEN=... ANTHROPIC_API_KEY=...
pr-gatekeeper --repo owner/name --pr 123 --dry-run   # classify only
pr-gatekeeper --repo owner/name --pr 123             # post review + label
pr-gatekeeper --repo owner/name --pr 123 --no-llm    # rules only
```

As a library:

```python
from pr_gatekeeper import Config, Gatekeeper
decision = Gatekeeper(Config(max_changed_lines=40)).decide(pull_request)
decision.action   # Action.APPROVE | Action.ESCALATE
```

Plug in any model by passing `reviewer=` (anything with `review(pr) -> LLMVerdict`).
CI workflow: `examples/pr-gatekeeper.yml`. Policy file: `examples/.pr-gatekeeper.yml`.

## Choosing a model provider

The LLM stage is pluggable (`llm_provider` in config, or `--provider/--model/--base-url`):

| Provider | Config | Notes |
|---|---|---|
| `anthropic` (default) | `llm_model` optional | `pip install 'pr-gatekeeper[anthropic]'`, key in `ANTHROPIC_API_KEY` |
| `openai` | `llm_model` required | Any OpenAI-compatible `/chat/completions` API; key in `OPENAI_API_KEY` |
| custom | `register_provider(name, factory)` | Gemini, Bedrock, internal gateway... |

```yaml
# OpenAI
llm_provider: openai
llm_model: <model-name>
# Local Ollama / vLLM / LiteLLM / Azure gateway
llm_provider: openai
llm_model: <model-name>
llm_base_url: http://localhost:11434/v1
llm_api_key_env: MY_GATEWAY_KEY   # optional; env var that holds the key
```

Every provider shares the same prompt, parsing and fail-closed behaviour: any error or unparseable
reply escalates to a human, never approves. Code sent to the model leaves your network unless you
use a local/self-hosted endpoint.

## Required GitHub setup (important)

1. Repo settings → Actions → *Allow GitHub Actions to create and approve pull requests*.
2. Branch protection: require ≥1 approval, **and** CODEOWNERS review for sensitive paths. That is
   your real enforcement; the gatekeeper's approval counts as the one bot review for minor PRs.
3. Prefer a dedicated bot/GitHub App identity over `GITHUB_TOKEN` so audit logs are clear.
4. Load config from the base branch (as in the example workflow) so PRs can't edit their own policy.

## Test

```bash
.venv/bin/pytest
```
