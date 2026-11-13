from app.github.signatures import sign, verify
from app.services.gate import GateInput, ReviewInfo, evaluate


def r(who, state, sha, t):
    return ReviewInfo(who, state, sha, t)


def test_agent_running_blocks():
    res = evaluate(GateInput("open", "running", "abc"))
    assert res.status == "in_progress" and not res.passing


def test_waiting_for_review():
    res = evaluate(GateInput("open", "done", "abc"))
    assert res.status == "in_progress" and "Waiting" in res.title


def test_approval_on_current_head_passes():
    res = evaluate(GateInput("open", "done", "abc", (r("ada", "approved", "abc", 1),)))
    assert res.passing and res.conclusion == "success"


def test_stale_approval_is_dismissed():
    res = evaluate(GateInput("open", "done", "def", (r("ada", "approved", "abc", 1),)))
    assert not res.passing and "dismissed" in res.summary


def test_latest_review_per_user_wins():
    reviews = (r("ada", "changes_requested", "abc", 1), r("ada", "approved", "abc", 2))
    assert evaluate(GateInput("open", "done", "abc", reviews)).passing
    reviews = (r("ada", "approved", "abc", 1), r("bob", "changes_requested", "abc", 2))
    res = evaluate(GateInput("open", "done", "abc", reviews))
    assert res.conclusion == "failure" and "bob" in res.summary


def test_comments_do_not_override_verdict():
    reviews = (r("ada", "approved", "abc", 1), r("ada", "commented", "abc", 2))
    assert evaluate(GateInput("open", "done", "abc", reviews)).passing


def test_required_approvals():
    reviews = (r("ada", "approved", "abc", 1),)
    assert not evaluate(GateInput("open", "done", "abc", reviews, required_approvals=2)).passing
    reviews += (r("bob", "approved", "abc", 2),)
    assert evaluate(GateInput("open", "done", "abc", reviews, required_approvals=2)).passing


def test_no_changes_policy():
    g = GateInput("open", "no_changes", "abc", has_doc_changes=False)
    assert not evaluate(g).passing
    assert evaluate(GateInput("open", "no_changes", "abc", has_doc_changes=False, pass_when_no_changes=True)).passing


def test_closed_and_merged():
    assert evaluate(GateInput("merged", "done", "abc")).passing
    assert evaluate(GateInput("closed", "done", "abc")).conclusion == "failure"


def test_failed_agent_can_still_be_approved():
    assert evaluate(GateInput("open", "failed", "abc")).conclusion == "failure"
    assert evaluate(GateInput("open", "failed", "abc", (r("ada", "approved", "abc", 1),))).passing


def test_webhook_signature():
    body = b'{"action":"opened"}'
    assert verify("s3cret", body, sign("s3cret", body))
    assert not verify("s3cret", body, sign("other", body))
    assert not verify("s3cret", body, None)
    assert not verify("", body, sign("", body))
