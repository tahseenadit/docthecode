from app.pr_events import Known, detect_events, updated_ts


def pr(n, updated, state="open", sha="a", title="Fix", draft=False, merged_at=None):
    return {"number": n, "updated_at": updated, "state": state, "head": {"sha": sha}, "title": title,
            "draft": draft, "merged_at": merged_at}


T0 = updated_ts(pr(0, "2026-11-11T10:00:00Z"))


def names(events):
    return [(a, p["number"]) for a, p in events]


def test_new_pr_opens_doc_pr_but_drafts_and_old_prs_wait():
    events, cursor = detect_events([
        pr(1, "2026-11-11T10:05:00Z"),
        pr(2, "2026-11-11T10:06:00Z", draft=True),
        pr(3, "2026-11-11T09:00:00Z"),            # updated before DocTheCode started watching
        pr(4, "2026-11-11T10:07:00Z", state="closed"),
    ], {}, T0)
    assert names(events) == [("opened", 1)]
    assert cursor == updated_ts(pr(0, "2026-11-11T10:07:00Z"))


def test_new_commits_title_change_close_merge_reopen():
    known = {1: Known("open", "a", "Docs: Fix"), 2: Known("open", "b", "Docs: Other"),
             3: Known("closed", "c", "Docs: X"), 4: Known("open", "d", "Docs: Y")}
    events, _ = detect_events([
        pr(1, "2026-11-11T10:01:00Z", sha="a2", title="Fix v2"),
        pr(2, "2026-11-11T10:02:00Z", state="closed", sha="b", title="Other", merged_at="2026-11-11T10:02:00Z"),
        pr(3, "2026-11-11T10:03:00Z", sha="c", title="X"),
        pr(4, "2026-11-11T10:04:00Z", state="closed", sha="d", title="Y"),
    ], known, T0)
    assert names(events) == [("synchronize", 1), ("edited", 1), ("closed", 2), ("reopened", 3), ("closed", 4)]
    assert events[2][1]["merged"] is True and events[4][1]["merged"] is False


def test_nothing_changed_means_no_events():
    known = {1: Known("open", "a", "Docs: Fix")}
    events, _ = detect_events([pr(1, "2026-11-11T10:01:00Z")], known, T0)  # e.g. a comment bumped updated_at
    assert events == []


def test_draft_marked_ready_opens_doc_pr():
    events, _ = detect_events([pr(5, "2026-11-11T10:09:00Z", draft=False)], {}, T0)
    assert names(events) == [("opened", 5)]


def test_events_are_oldest_first():
    events, _ = detect_events([pr(2, "2026-11-11T10:09:00Z"), pr(1, "2026-11-11T10:01:00Z")], {}, T0)
    assert names(events) == [("opened", 1), ("opened", 2)]
