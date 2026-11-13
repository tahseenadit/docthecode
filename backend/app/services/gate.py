"""The merge gate: decides what the GitHub check run on the code PR should say.

Pure function so it is easy to reason about and test. The rule set:

  * An approval only counts if it was given on the doc PR's *current* head commit.
    If the agent (or a human) pushes new doc commits after approval, the approval is
    stale and the code PR is blocked again, just like "dismiss stale reviews".
  * Each reviewer's latest review wins. Any outstanding "changes requested" blocks.
  * While the agent is working the check stays in progress (which blocks merging
    when the check is required by branch protection).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

CheckStatus = Literal["queued", "in_progress", "completed"]
Conclusion = Literal["success", "failure", "neutral", "action_required"] | None


@dataclass(frozen=True)
class ReviewInfo:
    reviewer: str
    state: Literal["approved", "changes_requested", "commented"]
    doc_head_sha: str
    created_at: float


@dataclass(frozen=True)
class GateInput:
    pr_state: Literal["open", "merged", "closed"]
    agent_status: Literal["idle", "queued", "running", "done", "failed", "no_changes"]
    doc_head_sha: str
    reviews: tuple[ReviewInfo, ...] = ()
    required_approvals: int = 1
    pass_when_no_changes: bool = False
    has_doc_changes: bool = True


@dataclass(frozen=True)
class GateResult:
    status: CheckStatus
    conclusion: Conclusion
    title: str
    summary: str

    @property
    def passing(self) -> bool:
        return self.status == "completed" and self.conclusion in ("success", "neutral")


def latest_reviews(reviews: Iterable[ReviewInfo]) -> dict[str, ReviewInfo]:
    latest: dict[str, ReviewInfo] = {}
    for r in sorted(reviews, key=lambda r: r.created_at):
        if r.state == "commented":
            continue
        latest[r.reviewer] = r
    return latest


def evaluate(g: GateInput) -> GateResult:
    if g.pr_state == "merged":
        return GateResult("completed", "success", "Documentation merged", "The documentation PR has been merged.")
    if g.pr_state == "closed":
        return GateResult("completed", "failure", "Documentation PR closed",
                          "The documentation PR was closed without merging. Reopen it in DocTheCode to continue.")
    if g.agent_status in ("queued", "running"):
        return GateResult("in_progress", None, "Agent is updating documentation",
                          "The DocTheCode agent is drafting documentation changes for this PR.")

    latest = latest_reviews(g.reviews)
    blocking = [r for r in latest.values() if r.state == "changes_requested"]
    if blocking:
        who = ", ".join(sorted(r.reviewer for r in blocking))
        return GateResult("completed", "failure", "Documentation changes requested",
                          f"{who} requested changes to the documentation.")

    current = [r for r in latest.values() if r.state == "approved" and r.doc_head_sha == g.doc_head_sha]
    stale = [r for r in latest.values() if r.state == "approved" and r.doc_head_sha != g.doc_head_sha]
    if len(current) >= g.required_approvals:
        who = ", ".join(sorted(r.reviewer for r in current))
        return GateResult("completed", "success", "Documentation approved", f"Approved by {who}.")

    if g.agent_status == "no_changes" and not g.has_doc_changes and g.pass_when_no_changes:
        return GateResult("completed", "neutral", "No documentation changes needed",
                          "The agent found nothing to update and this repo allows that to pass.")

    if g.agent_status == "failed":
        return GateResult("completed", "failure", "Documentation agent failed",
                          "The agent could not update the docs. Edit the doc PR in DocTheCode and request review.")

    need = g.required_approvals - len(current)
    summary = f"Waiting for {need} documentation approval{'s' if need != 1 else ''} in DocTheCode."
    if stale:
        summary += " Earlier approvals were dismissed because the docs changed since."
    if g.agent_status == "no_changes":
        summary += " The agent found nothing to update; approve to confirm."
    return GateResult("in_progress", None, "Waiting for documentation review", summary)
