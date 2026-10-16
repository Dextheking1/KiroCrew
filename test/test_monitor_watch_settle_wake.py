"""When a structured pull-request watch wakes its session, replayed end to end.

Every reading here goes through the real GitHub probe (fed by a fake ``gh`` that
answers each GraphQL document from a small mutable model of one pull request),
the real probe-result builder and the real decision engine. The driver persists
what the production record persists after a decision: the canonical reading, the
fingerprint, and the re-alert stamp of a wake.

The shape replayed is a real one: a pull request in this repository carrying 112
checks (109 check runs and 3 commit statuses), whose review lanes rewrite their
verdict comments in place while CI runs, that then waits hours for an approval
and gets a comment from a person in the meantime.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field

from kiro_crew.monitoring.decision import decide_monitor, stamp_monitor_alerted
from kiro_crew.monitoring.github_pull_request import GitHubPullRequestProvider
from kiro_crew.monitoring.models import (
    MonitorBudgets,
    MonitorDecision,
    MonitorObservationStatus,
    MonitorState,
)

_TARGET = "https://github.com/acme/widgets/pull/20852"
_HEAD = "61ed60365ff740d79829c388a81d1c8d541bb9fd"
_NEW_HEAD = "0123456789abcdef0123456789abcdef01234567"
_PR_AUTHOR = "pr-author"
_PERSON = {"__typename": "User", "login": "a-person"}
_BOT = {"__typename": "Bot", "login": "github-actions"}
_AUTHOR = {"__typename": "User", "login": _PR_AUTHOR}
_CADENCE = 120.0
_CURSOR_RE = re.compile(r"^c(\d+)=(.*)$")


@dataclass
class _PullRequest:
    """The mutable facts of one pull request, as the fake host serves them."""

    head: str = _HEAD
    draft: bool = False
    review_decision: str = "REVIEW_REQUIRED"
    merge_state: str = "BLOCKED"
    #: check-run name -> "passed" | "pending" | "failed"
    runs: dict[str, str] = field(default_factory=dict)
    #: status context -> "passed" | "pending" | "failed"
    statuses: dict[str, str] = field(default_factory=dict)
    comments: list[dict[str, object]] = field(default_factory=list)
    reviews: list[dict[str, object]] = field(default_factory=list)
    #: When set, the comment read answers with a GraphQL error for this subject.
    comments_unreadable: bool = False
    #: Overrides the rollup's advertised total (to exceed the paging cap).
    rollup_total: int | None = None


def _run_row(name: str, state: str) -> dict[str, object]:
    if state == "pending":
        return {"__typename": "CheckRun", "name": name, "status": "IN_PROGRESS", "conclusion": None}
    conclusion = "SUCCESS" if state == "passed" else "FAILURE"
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": "COMPLETED",
        "conclusion": conclusion,
    }


def _status_row(context: str, state: str) -> dict[str, object]:
    wire = {"passed": "SUCCESS", "pending": "PENDING", "failed": "FAILURE"}[state]
    return {"__typename": "StatusContext", "context": context, "state": wire}


class _FakeGitHub:
    """Answer each GraphQL document from the one pull request it models.

    Routed by what the document SELECTS, not by call order, so the probe is free
    to batch, page or add a read without the fixture having to know.
    """

    def __init__(self, pr: _PullRequest) -> None:
        self.pr = pr

    def __call__(self, argv: Sequence[str], **_: object) -> subprocess.CompletedProcess[str]:
        document = next(arg[len("query=") :] for arg in argv if arg.startswith("query="))
        cursor = None
        for arg in argv:
            match = _CURSOR_RE.match(arg)
            if match:
                cursor = match.group(2)
        node = self._answer(document, cursor)
        if node is None:
            payload: dict[str, object] = {
                "data": {"s0": {"pullRequest": None}},
                "errors": [{"type": "INTERNAL", "path": ["s0", "pullRequest"], "message": "x"}],
            }
        else:
            payload = {"data": {"s0": {"pullRequest": node}}}
        return subprocess.CompletedProcess(list(argv), 0, stdout=json.dumps(payload), stderr="")

    def _answer(self, document: str, cursor: str | None) -> dict[str, object] | None:
        pr = self.pr
        if "statusCheckRollup" in document:
            rows = [_run_row(name, state) for name, state in sorted(pr.runs.items())]
            rows += [_status_row(name, state) for name, state in sorted(pr.statuses.items())]
            start = int(cursor) if cursor else 0
            page = rows[start : start + 100]
            has_next = start + 100 < len(rows)
            return {
                "headRefOid": pr.head,
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "oid": pr.head,
                                "statusCheckRollup": {
                                    "contexts": {
                                        "totalCount": (
                                            len(rows)
                                            if pr.rollup_total is None
                                            else pr.rollup_total
                                        ),
                                        "pageInfo": {
                                            "hasNextPage": has_next,
                                            "endCursor": str(start + 100) if has_next else None,
                                        },
                                        "nodes": page,
                                    }
                                },
                            }
                        }
                    ]
                },
            }
        if "reviewThreads" in document:
            return {"reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": []}}
        if "comments(" in document:
            if pr.comments_unreadable:
                return None
            node: dict[str, object] = {
                "comments": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": deepcopy(pr.comments),
                }
            }
            if "reviews(" in document:
                node["reviews"] = {
                    "totalCount": len(pr.reviews),
                    "nodes": deepcopy(pr.reviews),
                }
            return node
        return {
            "number": 20852,
            "state": "OPEN",
            "isDraft": pr.draft,
            "headRefOid": pr.head,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": pr.merge_state,
            "reviewDecision": pr.review_decision,
            "author": {"login": _PR_AUTHOR},
        }


class _Watch:
    """One armed watch: probe, decide, persist -- what one production tick does."""

    def __init__(self, pr: _PullRequest) -> None:
        self.pr = pr
        self.provider = GitHubPullRequestProvider(
            resolver=lambda: "gh",
            runner=_FakeGitHub(pr),
        )
        self.state = MonitorState(
            kind="github_pull_request",
            target=_TARGET,
            objective="review_ready",
            created_ts=0.0,
            budgets=MonitorBudgets(max_runtime_secs=86_400),
            cadence_secs=int(_CADENCE),
        )
        self.now = 0.0
        self.decisions: list[MonitorDecision] = []
        self.last_reason = ""

    def tick(self) -> MonitorDecision:
        self.now += _CADENCE
        state = self.state
        result = self.provider.probe(
            (_TARGET,),
            previous_observations={_TARGET: deepcopy(state.last_observation)},
        )[_TARGET]
        observation = result.observation
        decision = decide_monitor(state, observation, now=self.now).decision
        if observation.status is not MonitorObservationStatus.PROVIDER_ERROR:
            state.last_observation = deepcopy(result.canonical)
            state.last_fingerprint = observation.fingerprint
            state.last_observation_status = observation.status
        state.last_decision = decision
        if decision is MonitorDecision.WAKE_ACTIONABLE:
            stamp_monitor_alerted(state, now=self.now)
        self.last_reason = observation.reason_code
        self.decisions.append(decision)
        return decision

    def ticks(self, count: int) -> list[MonitorDecision]:
        return [self.tick() for _ in range(count)]

    @property
    def wakes(self) -> int:
        return self.decisions.count(MonitorDecision.WAKE_ACTIONABLE)

    @property
    def active(self) -> bool:
        return self.decisions[-1] in {
            MonitorDecision.NO_CHANGE,
            MonitorDecision.RECORD_ONLY,
            MonitorDecision.WAKE_ACTIONABLE,
        }


def _board(runs: int, statuses: int, state: str = "passed") -> _PullRequest:
    return _PullRequest(
        runs={f"lane-{index:03d}": state for index in range(runs)},
        statuses={f"status-{index}": state for index in range(statuses)},
    )


def _bot_verdicts(version: int) -> list[dict[str, object]]:
    return [
        {"body": f"lane {lane} verdict v{version}", "author": _BOT}
        for lane in ("gpt", "opus", "first-principles")
    ]


def _settle(pr: _PullRequest) -> None:
    pr.runs = dict.fromkeys(pr.runs, "passed")
    pr.statuses = dict.fromkeys(pr.statuses, "passed")


# --------------------------------------------------------------------------- #
# STEP 0: the three misses, each on its own.
# --------------------------------------------------------------------------- #


def test_a_board_of_112_passing_checks_reads_complete() -> None:
    watch = _Watch(_board(109, 3))
    watch.tick()
    canonical = watch.state.last_observation
    assert canonical["checks_complete"] is True
    assert watch.last_reason == "review_required"
    checks = canonical["checks"]
    assert isinstance(checks, dict)
    assert checks["unknown"] == []
    assert checks["pending"] == []


def test_checks_settling_on_a_pr_that_waits_for_review_wakes_once() -> None:
    pr = _board(5, 1, "pending")
    watch = _Watch(pr)
    assert watch.ticks(2) == [MonitorDecision.RECORD_ONLY, MonitorDecision.NO_CHANGE]
    _settle(pr)
    assert watch.tick() is MonitorDecision.WAKE_ACTIONABLE
    assert watch.last_reason == "review_required"
    assert set(watch.ticks(5)) == {MonitorDecision.NO_CHANGE}


def test_a_person_comment_on_a_pr_that_waits_for_review_wakes_once() -> None:
    pr = _board(5, 1)
    watch = _Watch(pr)
    watch.ticks(3)
    assert watch.wakes == 0
    pr.comments.append({"body": "HOLD: please wait for the design review", "author": _PERSON})
    assert watch.tick() is MonitorDecision.WAKE_ACTIONABLE
    assert set(watch.ticks(5)) == {MonitorDecision.NO_CHANGE}


# --------------------------------------------------------------------------- #
# The replayed window.
# --------------------------------------------------------------------------- #


def test_replay_of_the_112_check_window_wakes_exactly_three_times() -> None:
    pr = _board(109, 3, "pending")
    pr.comments = _bot_verdicts(0)
    watch = _Watch(pr)

    # CI runs; the review lanes rewrite their verdict comments in place.
    for version in (1, 2, 3):
        pr.comments = _bot_verdicts(version)
        watch.tick()
    assert watch.wakes == 0

    _settle(pr)  # settle 1
    assert watch.tick() is MonitorDecision.WAKE_ACTIONABLE

    # 8 checks re-run on the same head while one lane re-posts its verdict.
    for name in list(pr.runs)[:8]:
        pr.runs[name] = "pending"
    for version in (4, 5):
        pr.comments = _bot_verdicts(version)
        assert watch.tick() is not MonitorDecision.WAKE_ACTIONABLE

    _settle(pr)  # settle 2
    assert watch.tick() is MonitorDecision.WAKE_ACTIONABLE

    assert set(watch.ticks(36)) == {MonitorDecision.NO_CHANGE}

    pr.comments.append({"body": "HOLD until the design is agreed", "author": _PERSON})
    assert watch.tick() is MonitorDecision.WAKE_ACTIONABLE

    assert set(watch.ticks(12)) == {MonitorDecision.NO_CHANGE}
    assert watch.wakes == 3
    assert watch.active
    assert watch.state.outcome is None
    assert watch.state.stall_streak == 0


# --------------------------------------------------------------------------- #
# Nearby inputs that reach the same code.
# --------------------------------------------------------------------------- #


def test_settling_with_one_failed_check_wakes_once_on_the_failure_path() -> None:
    pr = _board(5, 1, "pending")
    watch = _Watch(pr)
    watch.tick()
    _settle(pr)
    pr.runs["lane-000"] = "failed"
    assert watch.tick() is MonitorDecision.WAKE_ACTIONABLE
    assert watch.last_reason == "checks_failed"
    assert set(watch.ticks(4)) == {MonitorDecision.NO_CHANGE}
    assert watch.wakes == 1


def test_a_draft_settling_does_not_wake() -> None:
    pr = _board(5, 1, "pending")
    pr.draft = True
    watch = _Watch(pr)
    watch.tick()
    _settle(pr)
    watch.ticks(3)
    assert watch.wakes == 0


def test_a_watch_armed_on_an_already_green_pr_does_not_wake_on_its_first_reading() -> None:
    pr = _board(109, 3)
    pr.comments = [{"body": "an old note from a person", "author": _PERSON}]
    watch = _Watch(pr)
    assert watch.tick() is MonitorDecision.RECORD_ONLY
    assert set(watch.ticks(3)) == {MonitorDecision.NO_CHANGE}


def test_settling_after_a_force_push_wakes_for_the_new_head() -> None:
    pr = _board(5, 1, "pending")
    watch = _Watch(pr)
    watch.tick()
    _settle(pr)
    assert watch.tick() is MonitorDecision.WAKE_ACTIONABLE
    pr.head = _NEW_HEAD
    pr.runs = dict.fromkeys(pr.runs, "pending")
    assert watch.tick() is not MonitorDecision.WAKE_ACTIONABLE
    _settle(pr)
    assert watch.tick() is MonitorDecision.WAKE_ACTIONABLE
    assert watch.wakes == 2


def test_a_new_head_reported_with_no_checks_yet_does_not_wake() -> None:
    """Right after a push the host can report an empty board for a moment."""
    pr = _board(5, 1)
    watch = _Watch(pr)
    watch.tick()
    pr.head = _NEW_HEAD
    runs, statuses = pr.runs, pr.statuses
    pr.runs, pr.statuses = {}, {}
    assert watch.tick() is not MonitorDecision.WAKE_ACTIONABLE
    pr.runs, pr.statuses = dict.fromkeys(runs, "pending"), statuses
    watch.tick()
    _settle(pr)
    assert watch.tick() is MonitorDecision.WAKE_ACTIONABLE
    assert watch.wakes == 1


def test_a_re_settle_with_no_comment_change_does_not_wake_again() -> None:
    pr = _board(5, 1, "pending")
    watch = _Watch(pr)
    watch.tick()
    _settle(pr)
    assert watch.tick() is MonitorDecision.WAKE_ACTIONABLE
    pr.runs["lane-000"] = "pending"
    watch.tick()
    _settle(pr)
    assert watch.tick() is MonitorDecision.RECORD_ONLY
    assert watch.wakes == 1


def test_a_person_editing_an_old_comment_in_place_wakes() -> None:
    pr = _board(5, 1)
    pr.comments = [{"body": "looks fine", "author": _PERSON}]
    watch = _Watch(pr)
    watch.ticks(2)
    pr.comments[0]["body"] = "HOLD: actually, wait"
    assert watch.tick() is MonitorDecision.WAKE_ACTIONABLE
    assert watch.tick() is MonitorDecision.NO_CHANGE


def test_a_person_commented_review_with_a_body_wakes() -> None:
    pr = _board(5, 1)
    watch = _Watch(pr)
    watch.ticks(2)
    pr.reviews.append({"body": "please split this", "author": _PERSON})
    assert watch.tick() is MonitorDecision.WAKE_ACTIONABLE
    assert watch.tick() is MonitorDecision.NO_CHANGE


def test_a_commented_review_with_no_body_does_not_wake() -> None:
    pr = _board(5, 1)
    watch = _Watch(pr)
    watch.ticks(2)
    pr.reviews.append({"body": "", "author": _PERSON})
    assert watch.tick() is MonitorDecision.NO_CHANGE


def test_the_pr_authors_own_comment_does_not_wake() -> None:
    pr = _board(5, 1)
    watch = _Watch(pr)
    watch.ticks(2)
    pr.comments.append({"body": "rebased, ready again", "author": _AUTHOR})
    pr.reviews.append({"body": "self note", "author": {"__typename": "User", "login": "PR-Author"}})
    assert watch.tick() is MonitorDecision.RECORD_ONLY
    assert watch.wakes == 0


def test_bot_comments_never_wake_while_checks_run_or_after() -> None:
    pr = _board(5, 1, "pending")
    watch = _Watch(pr)
    for version in range(4):
        pr.comments = _bot_verdicts(version)
        watch.tick()
    assert watch.wakes == 0
    _settle(pr)
    assert watch.tick() is MonitorDecision.WAKE_ACTIONABLE
    pr.comments = _bot_verdicts(9)
    assert watch.tick() is MonitorDecision.RECORD_ONLY
    assert watch.wakes == 1


def test_an_unreadable_comment_read_names_no_person_and_recovers_once() -> None:
    pr = _board(5, 1)
    pr.comments = [{"body": "first", "author": _PERSON}]
    watch = _Watch(pr)
    watch.ticks(2)
    pr.comments_unreadable = True
    pr.comments.append({"body": "second", "author": _PERSON})
    assert watch.tick() is not MonitorDecision.WAKE_ACTIONABLE
    pr.comments_unreadable = False
    assert watch.tick() is MonitorDecision.WAKE_ACTIONABLE
    assert watch.tick() is MonitorDecision.NO_CHANGE
    assert watch.wakes == 1


def test_an_unreadable_comment_read_with_no_new_comment_does_not_wake_on_recovery() -> None:
    pr = _board(5, 1)
    pr.comments = [{"body": "first", "author": _PERSON}]
    watch = _Watch(pr)
    watch.ticks(2)
    pr.comments_unreadable = True
    watch.tick()
    pr.comments_unreadable = False
    assert watch.tick() is not MonitorDecision.WAKE_ACTIONABLE
    assert watch.wakes == 0


def test_a_person_comment_on_an_actionable_pr_wakes_once() -> None:
    pr = _board(5, 1)
    pr.runs["lane-000"] = "failed"
    watch = _Watch(pr)
    assert watch.tick() is MonitorDecision.WAKE_ACTIONABLE
    assert watch.tick() is MonitorDecision.NO_CHANGE
    pr.comments.append({"body": "why did this fail?", "author": _PERSON})
    assert watch.tick() is MonitorDecision.WAKE_ACTIONABLE
    assert watch.tick() is MonitorDecision.NO_CHANGE
    assert watch.wakes == 2


def test_a_failed_bucket_past_the_bound_is_still_actionable_and_incomplete() -> None:
    watch = _Watch(_board(101, 0, "failed"))
    assert watch.tick() is MonitorDecision.WAKE_ACTIONABLE
    assert watch.last_reason == "checks_failed"
    assert watch.state.last_observation["checks_complete"] is False


def test_a_pending_bucket_past_the_bound_is_still_incomplete() -> None:
    watch = _Watch(_board(101, 0, "pending"))
    watch.tick()
    assert watch.last_reason == "checks_incomplete"


def test_a_board_past_the_row_cap_is_still_incomplete() -> None:
    pr = _board(300, 0)
    pr.rollup_total = 401
    watch = _Watch(pr)
    watch.tick()
    assert watch.last_reason == "checks_incomplete"
    assert watch.state.last_observation["checks_complete"] is False


def test_the_cut_passed_bucket_counts_and_moves_the_fingerprint() -> None:
    pr = _board(109, 3)
    watch = _Watch(pr)
    watch.tick()
    passed = watch.state.last_observation["checks"]["passed"]  # type: ignore[index]
    assert len(passed) == 100
    assert passed[-1] == "passed:total:112"
    before = watch.state.last_fingerprint
    pr.statuses["status-extra"] = "passed"
    watch.tick()
    assert watch.state.last_observation["checks"]["passed"][-1] == "passed:total:113"  # type: ignore[index]
    assert watch.state.last_fingerprint != before


def test_an_approval_after_settle_takes_the_success_path() -> None:
    pr = _board(5, 1, "pending")
    watch = _Watch(pr)
    watch.tick()
    _settle(pr)
    assert watch.tick() is MonitorDecision.WAKE_ACTIONABLE
    pr.review_decision = "APPROVED"
    pr.merge_state = "CLEAN"
    assert watch.tick() is MonitorDecision.STOP_SUCCESS


def test_an_old_person_comment_does_not_wake_after_a_failed_first_comment_read() -> None:
    pr = _board(5, 1)
    pr.comments = [{"body": "an old note", "author": _PERSON}]
    pr.comments_unreadable = True
    watch = _Watch(pr)
    watch.tick()
    pr.comments_unreadable = False
    assert watch.ticks(3).count(MonitorDecision.WAKE_ACTIONABLE) == 0


def test_a_record_from_before_the_person_digest_is_a_baseline() -> None:
    pr = _board(5, 1)
    pr.comments = [{"body": "an old note", "author": _PERSON}]
    watch = _Watch(pr)
    watch.tick()
    watch.state.last_observation.pop("person_comment_digest")
    assert watch.ticks(3).count(MonitorDecision.WAKE_ACTIONABLE) == 0
    pr.comments.append({"body": "a new note", "author": _PERSON})
    assert watch.tick() is MonitorDecision.WAKE_ACTIONABLE


def test_a_person_deleting_one_of_their_comments_does_not_wake() -> None:
    pr = _board(5, 1)
    pr.comments = [
        {"body": "one", "author": _PERSON},
        {"body": "two", "author": _PERSON},
    ]
    watch = _Watch(pr)
    watch.ticks(2)
    pr.comments.pop()
    assert watch.tick() is MonitorDecision.RECORD_ONLY
    assert watch.wakes == 0


def test_a_failure_right_after_a_settle_wake_wakes_at_once() -> None:
    pr = _board(5, 1, "pending")
    watch = _Watch(pr)
    watch.tick()
    _settle(pr)
    assert watch.tick() is MonitorDecision.WAKE_ACTIONABLE
    pr.runs["lane-001"] = "failed"
    assert watch.tick() is MonitorDecision.WAKE_ACTIONABLE


def test_the_judge_reports_the_real_passed_count_of_a_cut_bucket() -> None:
    from kiro_crew.autonudge_judge import render_pr_checks

    watch = _Watch(_board(109, 3))
    watch.tick()
    assert "passed 112" in render_pr_checks(watch.state.last_observation)


def test_a_real_check_carrying_the_sentinel_prefix_is_not_read_as_a_count() -> None:
    from kiro_crew.monitoring.models import pull_request_passed_count

    values = [f"check-{index:03d}" for index in range(99)] + ["passed:total:7"]
    assert pull_request_passed_count(values) == 100
