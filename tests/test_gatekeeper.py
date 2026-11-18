from pr_gatekeeper import (Action, Config, FileChange, Gatekeeper, LLMVerdict,
                           PullRequest, Risk)
from pr_gatekeeper.models import Issue
from pr_gatekeeper.rules import added_line_numbers
from pr_gatekeeper.llm import parse_verdict
from pr_gatekeeper.rules import glob_match


def pr(files, **kw):
    base = dict(repo="o/r", number=1, title="t", body="", author="dev",
                head_sha="abc", base_ref="main", files=tuple(files))
    base.update(kw)
    return PullRequest(**base)


def f(path, add=3, dele=1, patch="+x = 1", status="modified"):
    return FileChange(path, status, add, dele, patch)


class FakeReviewer:
    def __init__(self, verdict=None, exc=None):
        self.verdict, self.exc, self.calls = verdict, exc, 0

    def review(self, _pr):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.verdict


MINOR_OK = LLMVerdict(Risk.MINOR, 0.95, "typo fix")


def gk(reviewer=None, **cfg):
    return Gatekeeper(Config(**cfg), reviewer or FakeReviewer(MINOR_OK))


def test_small_change_approved():
    d = gk().decide(pr([f("src/app.py")]))
    assert d.action is Action.APPROVE and d.risk is Risk.MINOR


def test_large_clean_change_escalates_silently():
    d = gk().decide(pr([f("src/app.py", add=200)]))
    assert d.action is Action.ESCALATE and not d.issues


def test_docs_only_gets_relaxed_limit():
    assert gk().decide(pr([f("docs/a.md", add=200, dele=0)])).action is Action.APPROVE


def test_sensitive_path_escalates():
    d = gk().decide(pr([f(".github/workflows/ci.yml")]))
    assert d.action is Action.ESCALATE
    assert any(s.code == "sensitive_path" for s in d.signals)


def test_dependency_change_escalates():
    assert gk().decide(pr([f("requirements.txt")])).action is Action.ESCALATE
    assert gk().decide(pr([f("svc/package.json")])).action is Action.ESCALATE


def test_risky_pattern_escalates():
    d = gk().decide(pr([f("src/a.py", patch='+API_KEY = "sk-abcdef123456"')]))
    assert any(s.code == "risky_pattern" for s in d.signals)
    assert d.action is Action.ESCALATE


def test_missing_patch_escalates():
    assert gk().decide(pr([f("img.png", patch=None)])).action is Action.ESCALATE


def test_deleted_file_escalates():
    assert gk().decide(pr([f("src/a.py", status="removed")])).action is Action.ESCALATE


def test_draft_and_blocked_author_escalate():
    assert gk().decide(pr([f("a.py")], draft=True)).action is Action.ESCALATE
    assert gk(never_auto_approve_authors=["dev"]).decide(pr([f("a.py")])).action is Action.ESCALATE


def test_llm_major_escalates():
    r = FakeReviewer(LLMVerdict(Risk.MAJOR, 0.9, "changes auth flow"))
    assert gk(r).decide(pr([f("a.py")])).action is Action.ESCALATE


def test_llm_low_confidence_escalates():
    r = FakeReviewer(LLMVerdict(Risk.MINOR, 0.5, "unsure"))
    assert gk(r).decide(pr([f("a.py")])).action is Action.ESCALATE


def test_llm_error_fails_closed():
    d = gk(FakeReviewer(exc=RuntimeError("boom"))).decide(pr([f("a.py")]))
    assert d.action is Action.ESCALATE and any(s.code == "llm_error" for s in d.signals)


def test_rules_only_mode():
    assert gk(use_llm=False).decide(pr([f("a.py")])).action is Action.APPROVE


def test_parse_verdict_malformed_is_major():
    v = parse_verdict("sure, looks fine!")
    assert v.risk is Risk.MAJOR and v.confidence == 0.0


def test_parse_verdict_ok():
    v = parse_verdict('```json\n{"risk":"minor","confidence":0.9,"summary":"s","concerns":[]}\n```')
    assert v.risk is Risk.MINOR and v.confidence == 0.9


def test_glob_match():
    assert glob_match("Dockerfile", ["**/Dockerfile*"])
    assert glob_match("a/b/Dockerfile.dev", ["**/Dockerfile*"])
    assert glob_match("a/auth/x.py", ["**/auth/**"])
    assert not glob_match("src/author.py", ["**/auth/**"])


PATCH = "@@ -1,2 +1,3 @@\n ctx\n+bad = 1\n ctx2"


class FakeGitHub:
    def __init__(self, p):
        self.p, self.reviews, self.labels = p, [], []

    seen: set = set()
    checks = {"build": "success"}
    merged = None
    merge_error = None

    def get_check_states(self, *_):
        return self.checks

    def enable_auto_merge(self, repo, n, method):
        if self.merge_error:
            raise self.merge_error
        self.merged = (n, method)

    def get_pull_request(self, *a, **k):
        return self.p

    def existing_markers(self, *_):
        return self.seen

    def submit_review(self, repo, n, sha, event, body, comments=None):
        self.reviews.append((sha, event, body, comments))

    def add_labels(self, repo, n, labels):
        self.labels += labels


def issue_reviewer(*issues, risk=Risk.MINOR):
    return FakeReviewer(LLMVerdict(risk, 0.95, "found stuff", (), tuple(issues)))


def test_clean_minor_approves_silently():
    gh = FakeGitHub(pr([f("a.py")]))
    gk().run(gh, "o/r", 1)
    assert gh.reviews == [("abc", "APPROVE", "", [])] and gh.labels == ["auto-approved"]


def test_clean_major_posts_nothing_but_label():
    gh = FakeGitHub(pr([f("a.py", add=500)]))
    d = gk().run(gh, "o/r", 1)
    assert d.action is Action.ESCALATE
    assert gh.reviews == [] and gh.labels == ["needs-human-review"]


def test_issues_post_inline_comments_and_never_approve():
    gh = FakeGitHub(pr([f("a.py", patch=PATCH)]))
    d = gk(issue_reviewer(Issue("a.py", 2, "blocker", "SQL injection"))).run(gh, "o/r", 1)
    assert d.action is Action.REPORT_ISSUES
    (sha, event, body, comments), = gh.reviews
    assert event == "COMMENT" and comments[0]["line"] == 2 and comments[0]["path"] == "a.py"
    assert gh.labels == ["issues-found"]


def test_issue_on_non_diff_line_goes_to_body():
    gh = FakeGitHub(pr([f("a.py", patch=PATCH)]))
    gk(issue_reviewer(Issue("a.py", 99, "warning", "odd"))).run(gh, "o/r", 1)
    (_, _, body, comments), = gh.reviews
    assert comments == [] and "odd" in body


def test_nits_filtered_by_default_so_pr_stays_clean():
    d = gk(issue_reviewer(Issue("a.py", 2, "nit", "rename var"))).decide(pr([f("a.py", patch=PATCH)]))
    assert d.action is Action.APPROVE and not d.issues


def test_issue_for_unknown_file_ignored():
    d = gk(issue_reviewer(Issue("zzz.py", 1, "blocker", "x"))).decide(pr([f("a.py")]))
    assert d.action is Action.APPROVE


def test_issues_reported_even_when_rules_say_major():
    d = gk(issue_reviewer(Issue("a.py", 2, "warning", "bug"))).decide(
        pr([f("a.py", add=500, patch=PATCH)]))
    assert d.action is Action.REPORT_ISSUES and d.risk is Risk.MAJOR


def test_dry_run_posts_nothing():
    gh = FakeGitHub(pr([f("a.py")]))
    gk().run(gh, "o/r", 1, dry_run=True)
    assert gh.reviews == [] and gh.labels == []


def test_added_line_numbers():
    assert added_line_numbers("@@ -1,2 +10,3 @@\n ctx\n+new\n-old\n+new2") == {11, 12}


def test_already_reported_issue_not_reposted():
    from pr_gatekeeper.gatekeeper import issue_key
    i = Issue("a.py", 2, "blocker", "SQL injection")
    gh = FakeGitHub(pr([f("a.py", patch=PATCH)]))
    gh.seen = {issue_key(i)}
    d = gk(issue_reviewer(i)).run(gh, "o/r", 1)
    assert d.action is Action.REPORT_ISSUES and gh.reviews == [] and gh.labels == []


def test_new_issue_posted_with_marker_and_old_skipped():
    from pr_gatekeeper.gatekeeper import issue_key
    old, new = Issue("a.py", 2, "blocker", "old bug"), Issue("a.py", 2, "warning", "new bug")
    gh = FakeGitHub(pr([f("a.py", patch=PATCH)]))
    gh.seen = {issue_key(old)}
    gk(issue_reviewer(old, new)).run(gh, "o/r", 1)
    (_, _, _, comments), = gh.reviews
    assert len(comments) == 1 and "new bug" in comments[0]["body"] and "pr-gatekeeper:" in comments[0]["body"]


def test_prompt_includes_file_context_but_size_check_is_diff_only():
    from pr_gatekeeper.llm import build_prompt
    p = pr([FileChange("a.py", "modified", 1, 0, "+x", content="line1\nline2")])
    out = build_prompt(p, 10_000)
    assert "2: line2" in out and "SURROUNDING CONTEXT" in out
    assert build_prompt(p, 5) is None


def test_ci_failed_blocks_approval_and_labels():
    gh = FakeGitHub(pr([f("a.py")]))
    gh.checks = {"build": "failure", "lint": "success"}
    d = gk().run(gh, "o/r", 1)
    assert d.action is Action.ESCALATE and gh.reviews == [] and gh.labels == ["needs-human-review"]


def test_ci_pending_waits_silently():
    gh = FakeGitHub(pr([f("a.py")]))
    gh.checks = {"build": "pending"}
    d = gk().run(gh, "o/r", 1)
    assert d.action is Action.WAIT_CI and gh.reviews == [] and gh.labels == []


def test_no_ci_reported_waits_unless_allowed():
    gh = FakeGitHub(pr([f("a.py")])); gh.checks = {}
    assert gk().decide(pr([f("a.py")])).action is Action.APPROVE  # decide() is pre-CI
    assert gk().run(gh, "o/r", 1).action is Action.WAIT_CI
    assert gk(allow_no_ci=True).run(gh, "o/r", 1).action is Action.APPROVE


def test_own_check_ignored_and_required_checks():
    gh = FakeGitHub(pr([f("a.py")]))
    gh.checks = {"gatekeeper": "pending", "build": "success"}
    assert gk().run(gh, "o/r", 1).action is Action.APPROVE
    gh = FakeGitHub(pr([f("a.py")])); gh.checks = {"build": "success"}
    assert gk(required_checks=["build", "e2e"]).run(gh, "o/r", 1).action is Action.WAIT_CI


def test_ci_error_fails_closed():
    class Boom(FakeGitHub):
        def get_check_states(self, *_):
            raise RuntimeError("api down")
    gh = Boom(pr([f("a.py")]))
    assert gk().run(gh, "o/r", 1).action is Action.WAIT_CI and gh.reviews == []


def test_ci_not_consulted_when_issues_found():
    gh = FakeGitHub(pr([f("a.py", patch=PATCH)])); gh.checks = {"build": "failure"}
    d = gk(issue_reviewer(Issue("a.py", 2, "blocker", "bug"))).run(gh, "o/r", 1)
    assert d.action is Action.REPORT_ISSUES and len(gh.reviews) == 1


def test_auto_merge_opt_in_after_approval_only():
    gh = FakeGitHub(pr([f("a.py")]))
    gk().run(gh, "o/r", 1)
    assert gh.merged is None
    gh = FakeGitHub(pr([f("a.py")]))
    gk(auto_merge=True).run(gh, "o/r", 1)
    assert gh.merged == (1, "SQUASH")
    gh = FakeGitHub(pr([f("a.py", add=500)]))
    gk(auto_merge=True).run(gh, "o/r", 1)
    assert gh.merged is None


def test_auto_merge_failure_keeps_approval():
    gh = FakeGitHub(pr([f("a.py")])); gh.merge_error = RuntimeError("not allowed")
    d = gk(auto_merge=True).run(gh, "o/r", 1)
    assert d.action is Action.APPROVE and gh.reviews and any(s.code == "auto_merge_failed" for s in d.signals)


def test_openai_compatible_reviewer_parses_response():
    import httpx
    from pr_gatekeeper.llm import OpenAICompatibleReviewer
    seen = {}

    def handler(req):
        seen["url"], seen["body"] = str(req.url), req.read()
        return httpx.Response(200, json={"choices": [{"message": {"content":
            '{"risk":"minor","confidence":0.9,"summary":"ok","issues":[{"path":"a.py","line":2,"severity":"blocker","message":"bug"}]}'}}]})
    http = httpx.Client(base_url="http://llm.local/v1", transport=httpx.MockTransport(handler))
    r = OpenAICompatibleReviewer(Config(llm_provider="openai", llm_model="m"), http=http)
    v = r.review(pr([f("a.py", patch=PATCH)]))
    assert seen["url"] == "http://llm.local/v1/chat/completions" and b'"model":"m"' in seen["body"].replace(b" ", b"")
    assert v.risk is Risk.MINOR and v.issues[0].message == "bug"


def test_openai_requires_model_and_unknown_provider_errors():
    import pytest
    from pr_gatekeeper.llm import make_reviewer
    with pytest.raises(ValueError):
        make_reviewer(Config(llm_provider="openai"))
    with pytest.raises(ValueError):
        make_reviewer(Config(llm_provider="nope"))


def test_register_custom_provider():
    from pr_gatekeeper.llm import make_reviewer, register_provider
    register_provider("fake", lambda cfg: FakeReviewer(MINOR_OK))
    assert make_reviewer(Config(llm_provider="fake")).review(None) is MINOR_OK


def test_provider_error_fails_closed_via_gatekeeper():
    import httpx
    from pr_gatekeeper.llm import OpenAICompatibleReviewer
    http = httpx.Client(base_url="http://x/v1",
                        transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    rev = OpenAICompatibleReviewer(Config(llm_provider="openai", llm_model="m"), http=http)
    d = Gatekeeper(Config(), rev).decide(pr([f("a.py")]))
    assert d.action is Action.ESCALATE and any(s.code == "llm_error" for s in d.signals)
