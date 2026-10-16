"""Provider-neutral pull-request readiness observations."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

from kiro_crew.monitoring.models import (
    MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET,
    MAX_MONITOR_CHECK_IDENTITY_CHARS,
    MAX_MONITOR_CONDITION_KEY_CHARS,
    MAX_MONITOR_CONDITIONS,
    PULL_REQUEST_CHECK_FIELDS,
    PULL_REQUEST_MERGEABILITY,
    PULL_REQUEST_MONITOR_KINDS,
    PULL_REQUEST_PASSED_TOTAL_PREFIX,
    PULL_REQUEST_REVIEW_DECISIONS,
    PULL_REQUEST_STATES,
    PULL_REQUEST_SUPERSEDED_CHECK_FIELD,
    PULL_REQUEST_SUPERSEDED_INCOMPLETE_IDENTITY,
    MonitorCondition,
    MonitorObservation,
    MonitorObservationStatus,
    MonitorProbeResult,
    MonitorResetsOn,
    MonitorSeverity,
    ProviderErrorKind,
)
from kiro_crew.security import redact

# Every state a provider may report, which is exactly the set of canonical check
# buckets: the bucket names and the states are one vocabulary, so a state added to
# one of them cannot go missing from the other.
PULL_REQUEST_CHECK_STATES = frozenset(
    (*PULL_REQUEST_CHECK_FIELDS, PULL_REQUEST_SUPERSEDED_CHECK_FIELD)
)
MAX_PULL_REQUEST_HEAD_REVISION_CHARS = 128

_URL_IN_CHECK_IDENTITY_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_HEAD_REVISION_RE = re.compile(rf"^[0-9a-fA-F]{{1,{MAX_PULL_REQUEST_HEAD_REVISION_CHARS}}}$")

_PROVIDER_ERROR_REASONS = {
    ProviderErrorKind.RATE_LIMITED: "provider_rate_limited",
    ProviderErrorKind.AUTHENTICATION: "provider_authentication",
    ProviderErrorKind.AUTHORIZATION: "provider_authorization",
    ProviderErrorKind.NOT_FOUND: "provider_not_found",
    ProviderErrorKind.TRANSIENT: "provider_transient",
}


def opaque_provider_check_identity(namespace: str, raw_identity: object) -> str:
    """Return a stable identity without retaining provider-controlled display text."""
    digest = hashlib.sha256(str(raw_identity).encode("utf-8")).hexdigest()[:16]
    return f"{namespace}:{digest}"


class PullRequestProviderError(Exception):
    """A provider failure carrying only its safe retry category."""

    def __init__(self, kind: ProviderErrorKind) -> None:
        super().__init__(kind.value)
        self.kind = kind


def classify_provider_error_text(raw: str) -> ProviderErrorKind:
    """Classify CLI diagnostics without retaining or returning their text.

    One marker set per category, for every reader of every provider CLI. Two sets
    for one binary is two answers to the same question: they drift in both
    directions, and the reader holding the shorter list waits where the other
    retries. ``secondary rate`` and ``abuse detection`` are GitHub's own wording for
    a budget refusal and live here rather than beside one caller; a provider that
    never emits them is unaffected, and one that starts to is classified correctly.
    """
    lowered = raw.lower()
    if any(
        marker in lowered
        for marker in (
            "http 429",
            "rate limit",
            "secondary rate",
            "abuse detection",
            "too many requests",
            "throttled",
        )
    ):
        return ProviderErrorKind.RATE_LIMITED
    if any(
        marker in lowered
        for marker in (
            "http 401",
            "unauthorized",
            "not logged in",
            "authentication",
            "invalid token",
            "expired token",
            "revoked token",
        )
    ):
        return ProviderErrorKind.AUTHENTICATION
    if any(marker in lowered for marker in ("http 404", "not found", "does not exist")):
        return ProviderErrorKind.NOT_FOUND
    if any(
        marker in lowered for marker in ("http 403", "forbidden", "permission", "access denied")
    ):
        return ProviderErrorKind.AUTHORIZATION
    return ProviderErrorKind.TRANSIENT


def provider_failure_result(error: PullRequestProviderError) -> PullRequestProbeResult:
    """Convert a safe typed provider failure into a generic monitor result."""
    return provider_error_result(error.kind, _PROVIDER_ERROR_REASONS[error.kind])


@dataclass(frozen=True)
class PullRequestCheck:
    """One normalized, bounded provider check."""

    identity: str
    state: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, str) or not self.identity:
            raise ValueError("check identity must be a non-empty string")
        if self.state not in PULL_REQUEST_CHECK_STATES:
            raise ValueError("check state is not supported")
        normalized = " ".join(
            "".join(
                (
                    " "
                    if unicodedata.category(character).startswith("C")
                    or unicodedata.category(character) in {"Zl", "Zp"}
                    else character
                )
                for character in self.identity
            ).split()
        )
        identity = redact(_URL_IN_CHECK_IDENTITY_RE.sub("[provider-url]", normalized))
        if not identity:
            raise ValueError("check identity must remain non-empty after redaction")
        if len(identity) > MAX_MONITOR_CHECK_IDENTITY_CHARS:
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
            prefix_length = MAX_MONITOR_CHECK_IDENTITY_CHARS - len(digest) - 1
            identity = f"{identity[:prefix_length]}#{digest}"
        object.__setattr__(self, "identity", identity)


@dataclass(frozen=True)
class PullRequestFacts:
    """Provider-native state normalized into the shared readiness vocabulary."""

    kind: str
    target: str
    state: str
    draft: bool
    head_revision: str
    mergeability: str
    review_decision: str
    checks: tuple[PullRequestCheck, ...]
    unresolved_review_threads: int
    review_threads_complete: bool
    checks_complete: bool = True
    #: A stable digest over the pull request's PR-level (issue) comment bodies,
    #: or "" when there are none. Provider-specific: only the GitHub adapter reads
    #: them today. This is the surface a review bot's verdict comment actually
    #: lives on -- created_at frozen at PR open, body rewritten in place -- so it
    #: is the digest that catches the four bot verdicts a thread digest cannot see.
    pr_comment_body_digest: str = ""
    #: A stable digest over the comments and commented reviews a PERSON wrote: an
    #: account of user type that is not the pull request's author. "" when there
    #: are none, and ``None`` when they were not read completely this tick -- an
    #: adapter that cannot tell a person from a bot, or a refused or capped read.
    #: Only the GitHub adapter sets it today.
    person_comment_digest: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in PULL_REQUEST_MONITOR_KINDS:
            raise ValueError("kind is not a supported pull-request monitor")
        if not isinstance(self.target, str) or not self.target:
            raise ValueError("target must be a non-empty string")
        if self.state not in PULL_REQUEST_STATES:
            raise ValueError("state is not supported")
        if not isinstance(self.draft, bool):
            raise ValueError("draft must be a boolean")
        if not isinstance(self.head_revision, str) or (
            self.head_revision and _HEAD_REVISION_RE.fullmatch(self.head_revision) is None
        ):
            raise ValueError("head_revision must be bounded hexadecimal text")
        if self.mergeability not in PULL_REQUEST_MERGEABILITY:
            raise ValueError("mergeability is not supported")
        if self.review_decision not in PULL_REQUEST_REVIEW_DECISIONS:
            raise ValueError("review_decision is not supported")
        if not isinstance(self.checks, tuple) or any(
            not isinstance(check, PullRequestCheck) for check in self.checks
        ):
            raise ValueError("checks must be normalized pull-request checks")
        if (
            isinstance(self.unresolved_review_threads, bool)
            or not isinstance(self.unresolved_review_threads, int)
            or self.unresolved_review_threads < 0
        ):
            raise ValueError("unresolved_review_threads must be a non-negative integer")
        if not isinstance(self.review_threads_complete, bool):
            raise ValueError("review_threads_complete must be a boolean")
        if not isinstance(self.checks_complete, bool):
            raise ValueError("checks_complete must be a boolean")
        if not isinstance(self.pr_comment_body_digest, str):
            raise ValueError("pr_comment_body_digest must be a string")
        if self.person_comment_digest is not None and not isinstance(
            self.person_comment_digest, str
        ):
            raise ValueError("person_comment_digest must be a string or None")


@dataclass(frozen=True)
class PullRequestProbeResult(MonitorProbeResult):
    """Canonical facts and their generic monitor classification."""

    response: object | None
    canonical: dict[str, object]
    observation: MonitorObservation


def build_pull_request_probe_result(
    facts: PullRequestFacts,
    *,
    previous_observation: Mapping[str, object] | None = None,
    response: object | None = None,
    supplemental_provider_error: ProviderErrorKind | None = None,
) -> PullRequestProbeResult:
    """Build the shared canonical snapshot, fingerprint, and classification."""
    previous = _previous_reading(previous_observation)
    canonical = canonical_pull_request_facts(facts)
    person_digest = facts.person_comment_digest
    if person_digest is None:
        # Not read this tick, so the last reading's value stands. Dropping it
        # would make the next complete read look like a person had just written
        # every comment they ever wrote, and wake for it. With no value to carry
        # the key stays absent, which means "not known" and names no edge.
        carried = previous.get(PERSON_COMMENT_DIGEST_FIELD) if previous else None
        person_digest = carried if isinstance(carried, str) else None
    if person_digest is not None:
        canonical[PERSON_COMMENT_DIGEST_FIELD] = person_digest
    status, reason_code = classify_pull_request_facts(facts)
    fingerprint_facts = (
        actionable_fingerprint_facts(canonical)
        if status is MonitorObservationStatus.ACTIONABLE
        else canonical
    )
    previous_head = (
        previous_observation.get("head_revision")
        if isinstance(previous_observation, Mapping)
        else None
    )
    head_changed = (
        facts.state == "open"
        and isinstance(previous_head, str)
        and bool(previous_head)
        and bool(facts.head_revision)
        and previous_head != facts.head_revision
    )
    return PullRequestProbeResult(
        response=facts if response is None else response,
        canonical=canonical,
        observation=MonitorObservation(
            fingerprint_pull_request_facts(fingerprint_facts),
            status,
            supplemental_provider_error=supplemental_provider_error,
            reason_code=reason_code,
            head_changed=head_changed,
            # An ACTIONABLE subject names every condition it carries. Any other
            # subject names only the EDGES this reading crossed -- checks that
            # just settled, a person who just wrote -- because the decision
            # engine wakes on a condition, and a condition a PENDING subject
            # kept naming on every tick would be a level, not an edge.
            conditions=_merge_conditions(
                (
                    pull_request_conditions(canonical)
                    if status is MonitorObservationStatus.ACTIONABLE
                    else ()
                ),
                pull_request_edge_conditions(
                    canonical,
                    previous,
                    status,
                    person_read=facts.person_comment_digest is not None,
                ),
            ),
        ),
    )


#: The canonical key carrying :attr:`PullRequestFacts.person_comment_digest`. Its
#: PRESENCE is the fact "who wrote what was known": present with "" when a complete
#: read found no person, absent when nothing was ever read -- another provider, a
#: record written before the key existed, or a watch whose reads have all failed.
#: Absence in the previous reading therefore makes the next reading a baseline.
PERSON_COMMENT_DIGEST_FIELD = "person_comment_digest"


def _person_count(value: object) -> int:
    """The number of person-written entries a person digest covers."""
    if not isinstance(value, str) or ":" not in value:
        return 0
    count = value.split(":", 1)[0]
    return int(count) if count.isascii() and count.isdecimal() else 0


def _previous_reading(previous_observation: object) -> Mapping[str, object] | None:
    """The last canonical reading, or ``None`` when there is none to compare with.

    An empty record -- a watch that has never read its subject, or one just
    re-aimed -- is no reading, so no edge can be measured against it. That is what
    keeps a watch armed on an already-green pull request quiet on its first tick.
    """
    if not isinstance(previous_observation, Mapping):
        return None
    head = previous_observation.get("head_revision")
    if not isinstance(head, str) or not head:
        return None
    if not isinstance(previous_observation.get("checks"), Mapping):
        return None
    return previous_observation


def _checks_settled(canonical: Mapping[str, object]) -> bool:
    """Whether every check on the board finished and none failed.

    A board with no passed check is not settled: right after a push the host may
    report no checks at all for a few seconds, and an empty board read as settled
    would wake before CI started and then mask the real settle on the same head.
    A record without ``checks_complete`` predates the field and was only ever
    written from a complete board, the same legacy reading the projection uses.
    """
    checks = canonical.get("checks")
    if not isinstance(checks, Mapping) or canonical.get("checks_complete", True) is not True:
        return False
    for state in ("failed", "pending", "unknown"):
        values = checks.get(state)
        if not isinstance(values, (list, tuple)) or values:
            return False
    passed = checks.get("passed")
    return isinstance(passed, (list, tuple)) and bool(passed)


def _edge_key(prefix: str, *parts: object) -> str:
    """A bounded condition key naming one edge by what it was observed on."""
    encoded = json.dumps([str(part) for part in parts], separators=(",", ":"))
    return f"{prefix}:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:32]}"


def pull_request_edge_conditions(
    canonical: Mapping[str, object],
    previous: Mapping[str, object] | None,
    status: MonitorObservationStatus,
    *,
    person_read: bool,
) -> tuple[MonitorCondition, ...]:
    """Name the two things that just HAPPENED on an open pull request.

    Both are edges between the previous reading and this one, so a subject that
    stays the same names nothing on the next tick and the watch goes quiet again.
    Both are ``IMMEDIATE`` because an edge is reported on one tick only: held
    inside the coalescing floor, it would be gone from the next tick before the
    floor could release it.

    **Checks settled.** The previous reading had a check still running, unknown,
    failed or unread -- or described another head -- and this one has every check
    finished with none failed, while the subject is still PENDING (waiting for
    review or for mergeability). A review lane's verdict lands before its own
    check finishes, so this is the tick every verdict of the round is readable.
    It is keyed by the head AND the PR-level comment digest, which is the default
    dedupe: checks re-run and settle again on the same head with no comment
    changed is masked for the re-alert interval, while a re-run during which a
    lane re-posted its verdict wakes again. ``REVISION`` scope, so a new head
    starts clean. Draft subjects are left out: nothing is asked of a draft yet.

    **A person wrote.** The digest over comments and commented reviews written by
    a user account other than the author moved between two complete readings.
    Bots never move it and neither does the author, so a session commenting on
    its own pull request does not wake itself. Named in any open state, a draft
    included: a person's comment usually needs an answer. ``NEVER`` scope, the
    same reasoning as the comment-body condition. An unread digest (``person_read``
    false) names nothing; the digest is carried forward instead, so the next
    complete read compares against what was last really seen. A previous reading
    that never knew the digest is a baseline, and a digest covering fewer entries
    than before is a deletion: neither names the edge.

    The previous reading must exist. A first reading is a baseline, not an edge.
    """
    if previous is None or canonical.get("state") != "open":
        return ()
    if status not in {MonitorObservationStatus.PENDING, MonitorObservationStatus.ACTIONABLE}:
        return ()
    head = canonical.get("head_revision")
    conditions: list[MonitorCondition] = []
    if (
        status is MonitorObservationStatus.PENDING
        and canonical.get("draft") is False
        and _checks_settled(canonical)
        and (not _checks_settled(previous) or previous.get("head_revision") != head)
    ):
        conditions.append(
            MonitorCondition(
                key=_edge_key(
                    "checks_settled",
                    head,
                    canonical.get("pr_comment_body_digest", ""),
                ),
                severity=MonitorSeverity.IMMEDIATE,
                brief="every check finished and none failed",
                resets_on=MonitorResetsOn.REVISION,
            )
        )
    person = canonical.get(PERSON_COMMENT_DIGEST_FIELD)
    before = previous.get(PERSON_COMMENT_DIGEST_FIELD)
    if (
        person_read
        and isinstance(person, str)
        and person
        and isinstance(before, str)
        and person != before
        # Fewer entries than before is a deletion, which asks nothing of anyone.
        and _person_count(person) >= _person_count(before)
    ):
        conditions.append(
            MonitorCondition(
                key=_edge_key("person_comment", person),
                severity=MonitorSeverity.IMMEDIATE,
                brief="a person commented on the pull request",
                resets_on=MonitorResetsOn.NEVER,
            )
        )
    return tuple(conditions)


def _merge_conditions(
    *groups: tuple[MonitorCondition, ...],
) -> tuple[MonitorCondition, ...]:
    """Join condition groups, first key wins, within the per-observation cap."""
    seen: set[str] = set()
    merged: list[MonitorCondition] = []
    for group in groups:
        for condition in group:
            if condition.key not in seen:
                seen.add(condition.key)
                merged.append(condition)
    return tuple(merged[:MAX_MONITOR_CONDITIONS])


def provider_error_result(
    kind: ProviderErrorKind,
    reason_code: str,
) -> PullRequestProbeResult:
    """Return a provider-neutral error result with no durable raw payload."""
    return PullRequestProbeResult(
        response=None,
        canonical={},
        observation=MonitorObservation(
            "",
            MonitorObservationStatus.PROVIDER_ERROR,
            provider_error=kind,
            reason_code=reason_code,
        ),
    )


def canonical_pull_request_facts(facts: PullRequestFacts) -> dict[str, object]:
    """Project one exact bounded canonical fact object."""
    buckets = {
        state: sorted(check.identity for check in facts.checks if check.state == state)
        for state in PULL_REQUEST_CHECK_FIELDS
    }
    # A failed, pending or unknown bucket past the bound leaves the board
    # unmeasured: a verdict that could wake someone would be cut. The passed bucket
    # is the exception. Its rows carry no such verdict, so it is cut and COUNTED
    # instead, and a board with more passing checks than one bucket holds still
    # reads complete -- otherwise it reads PENDING forever and never settles.
    overflow = not facts.checks_complete or any(
        len(values) > MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET
        for state, values in buckets.items()
        if state != "passed"
    )
    checks = {
        state: values[:MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET] for state, values in buckets.items()
    }
    passed = buckets["passed"]
    if len(passed) > MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET:
        checks["passed"] = [
            *passed[: MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET - 1],
            f"{PULL_REQUEST_PASSED_TOTAL_PREFIX}{len(passed)}",
        ]
    if overflow:
        checks["unknown"] = [
            *checks["unknown"][: MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET - 1],
            "checks:incomplete",
        ]
    # Displaced rows are reported rather than discarded, so the report can show which
    # rows a suppressed wake was suppressed FOR. They are kept out of the overflow
    # test above on purpose: they carry no verdict, so however many of them a head
    # accumulates, every live row is still measured and the board is still complete.
    # The bucket is written only when it holds something, which is what keeps the
    # canonical shape -- and so the fingerprint -- unchanged for every other subject.
    # This is the one place the bucket is cut, and the cut says so: a reader that sees
    # a saturated list without a sentinel would take it for the whole list, and the
    # count derived from it for the whole count. The sentinel is spent inside the
    # bucket instead of on ``checks_complete`` on purpose -- these rows carry no
    # verdict, so losing some of them leaves the board fully measured.
    superseded = sorted(
        check.identity
        for check in facts.checks
        if check.state == PULL_REQUEST_SUPERSEDED_CHECK_FIELD
    )
    if len(superseded) > MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET:
        superseded = [
            *superseded[: MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET - 1],
            PULL_REQUEST_SUPERSEDED_INCOMPLETE_IDENTITY,
        ]
    if superseded:
        checks[PULL_REQUEST_SUPERSEDED_CHECK_FIELD] = superseded
    if facts.review_decision == "changes_requested":
        blocking_review = "changes_requested"
    elif facts.unresolved_review_threads:
        blocking_review = "unresolved_threads"
    elif not facts.review_threads_complete:
        blocking_review = "unknown"
    else:
        blocking_review = "none"
    canonical: dict[str, object] = {
        "blocking_review": blocking_review,
        "checks": checks,
        "checks_complete": not overflow,
        "draft": facts.draft,
        "head_revision": facts.head_revision,
        "kind": facts.kind,
        "mergeability": facts.mergeability,
        "review_decision": facts.review_decision,
        "review_threads_complete": facts.review_threads_complete,
        "state": facts.state,
        "target": facts.target,
        "unresolved_review_threads": facts.unresolved_review_threads,
    }
    # Present ONLY when there is a digest to carry, so a subject with no PR-level
    # comment bodies keeps the exact canonical shape every provider shared before
    # this field existed -- a hard requirement, because the shape is pinned by
    # full-dict equality tests and hashed into the fingerprint. It is deliberately
    # NOT in ``PULL_REQUEST_OBSERVATION_FIELDS``: that list drives the public
    # projection, which fail-closes on an absent field and would inject an
    # always-present key into the projected observation. This is a per-condition
    # wake signal that ``pull_request_conditions`` reads, not a projected public
    # fact.
    if facts.pr_comment_body_digest:
        canonical["pr_comment_body_digest"] = facts.pr_comment_body_digest
    return canonical


def actionable_fingerprint_facts(canonical: Mapping[str, object]) -> dict[str, object]:
    """Keep known blockers stable while unrelated unsettled facts churn."""
    checks = canonical.get("checks")
    if not isinstance(checks, Mapping):
        raise ValueError("canonical pull-request checks are malformed")
    blocking_review = canonical.get("blocking_review")
    mergeability = canonical.get("mergeability")
    return {
        "blocking_review": (
            blocking_review
            if blocking_review in {"changes_requested", "unresolved_threads"}
            else "none"
        ),
        "failed_checks": checks.get("failed"),
        "checks_complete": canonical.get("checks_complete"),
        "head_revision": canonical.get("head_revision"),
        "kind": canonical.get("kind"),
        "mergeability": (mergeability if mergeability in {"conflicting", "behind"} else "none"),
        "review_threads_complete": canonical.get("review_threads_complete"),
        "state": canonical.get("state"),
        "target": canonical.get("target"),
        "unresolved_review_threads": canonical.get("unresolved_review_threads"),
    }


def _check_condition_key(identity: str) -> str:
    """The dedupe key for one failing check, kept distinct under the length bound.

    Truncating to the bound is what a key must never do on its own: two check
    identities sharing a long prefix -- the shape a matrix job produces, where the
    varying part is the SUFFIX -- collapse to one key, and one key is one
    condition, so the second failure is masked and aged as the first and is never
    reported. Appending a digest of the whole identity keeps the key inside the
    bound while preserving what the bound would otherwise erase.
    """
    key = f"red:{identity}"
    if len(key) <= MAX_MONITOR_CONDITION_KEY_CHARS:
        return key
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{key[: MAX_MONITOR_CONDITION_KEY_CHARS - len(digest) - 1]}-{digest}"


def pull_request_conditions(canonical: Mapping[str, object]) -> tuple[MonitorCondition, ...]:
    """Name every actionable condition a pull request is carrying at once.

    One base derivation for all four pull-request kinds: every adapter funnels
    through :func:`build_pull_request_probe_result`, so the conditions come from
    the canonical facts rather than from any provider's response shape. An
    adapter whose evidence streams must stay in separate namespaces keeps them
    apart by CHECK IDENTITY -- the identity a provider puts in ``checks`` is what
    ends up inside ``red:<identity>`` -- so nothing here merges two streams that
    the provider kept apart.

    This is where the representation defect is repaired. ``blocking_review`` is a
    single precedence winner, so a pull request carrying BOTH
    ``changes_requested`` and unresolved review threads records only the first
    and the second is lost before anything can act on it. Here they are two
    conditions, each masked, aged and reset on its own, and both survive.

    Both are ``NEVER``: a review verdict and a review thread belong to the
    conversation, not to the commit under review, so a force-push must not
    replay them. A failing check is the opposite -- it is a property of the
    revision that dispatched it, so a new head genuinely clears it.

    ``conflict`` is the ``IMMEDIATE`` one. A conflicted pull request dispatches
    no checks, so the pending count the coalescing floor waits on never drains,
    and holding the wake would strand the owner for the whole floor on a signal
    that is already actionable. ``behind`` is not urgent in that way: the branch
    still builds, so waiting continues to observe something.

    Conditions are capped, and the cap is on the CHECK expansion alone because it
    is the only unbounded one: the canonical projection already bounds each check
    bucket, and the review conditions are three fixed keys.
    """
    checks = canonical.get("checks")
    if not isinstance(checks, Mapping):
        raise ValueError("canonical pull-request checks are malformed")
    conditions: list[MonitorCondition] = []
    failed = checks.get("failed")
    if isinstance(failed, (list, tuple)):
        for identity in list(failed)[:MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET]:
            if isinstance(identity, str) and identity:
                conditions.append(
                    MonitorCondition(
                        key=_check_condition_key(identity),
                        severity=MonitorSeverity.WAKE,
                        brief=f"check failed: {identity}",
                        resets_on=MonitorResetsOn.REVISION,
                    )
                )
    if canonical.get("review_decision") == "changes_requested":
        conditions.append(
            MonitorCondition(
                key="changes_requested",
                severity=MonitorSeverity.WAKE,
                brief="a reviewer requested changes",
                resets_on=MonitorResetsOn.NEVER,
            )
        )
    unresolved = canonical.get("unresolved_review_threads")
    if isinstance(unresolved, int) and not isinstance(unresolved, bool) and unresolved > 0:
        conditions.append(
            MonitorCondition(
                key="unresolved_threads",
                severity=MonitorSeverity.WAKE,
                brief=f"{unresolved} unresolved review threads",
                resets_on=MonitorResetsOn.NEVER,
            )
        )
    mergeability = canonical.get("mergeability")
    if mergeability == "conflicting":
        conditions.append(
            MonitorCondition(
                key="conflict",
                severity=MonitorSeverity.IMMEDIATE,
                brief="the branch conflicts with its target",
                resets_on=MonitorResetsOn.REVISION,
            )
        )
    elif mergeability == "behind":
        conditions.append(
            MonitorCondition(
                key="behind",
                severity=MonitorSeverity.WAKE,
                brief="the branch is behind its target",
                resets_on=MonitorResetsOn.REVISION,
            )
        )
    comment_digest = canonical.get("pr_comment_body_digest")
    if isinstance(comment_digest, str) and comment_digest:
        # The digest is inside the KEY, not only the brief: the engine dedupes
        # per condition key, so a stable key with a changing brief would be
        # masked and never wake again. A bot rewrites a verdict comment IN PLACE
        # -- created_at does not move -- so a count or a newest-timestamp probe
        # cannot see it, only a digest over the bodies can. WAKE / NEVER: a
        # comment belongs to the conversation, not the commit, so a force-push
        # must not replay it (the same reasoning as the two review conditions
        # above). Fail-closed lives in the provider: it emits "" (so this key is
        # absent) on an incomplete comment read, so an empty/absent digest is the
        # incomplete-read signal and no condition is emitted. There is no separate
        # ``pr_comments_complete`` canonical field because an always-present key
        # would break the pinned full-canonical shape and the public projection,
        # and PR-level comment completeness has no bearing on readiness anyway.
        conditions.append(
            MonitorCondition(
                key=f"review_comment_bodies:{comment_digest}",
                severity=MonitorSeverity.WAKE,
                brief="pull request comments changed",
                resets_on=MonitorResetsOn.NEVER,
            )
        )
    # Deduplicate by key while keeping order: a provider is free to report two
    # checks under one identity, and two conditions under one key is one
    # condition the engine would mask and age twice.
    seen: set[str] = set()
    unique: list[MonitorCondition] = []
    for condition in conditions:
        if condition.key in seen:
            continue
        seen.add(condition.key)
        unique.append(condition)
    return tuple(unique[:MAX_MONITOR_CONDITIONS])


def classify_pull_request_facts(
    facts: PullRequestFacts,
) -> tuple[MonitorObservationStatus, str]:
    """Apply the one cross-provider review-readiness precedence."""
    if facts.state == "merged":
        return MonitorObservationStatus.SUCCESS, "pull_request_merged"
    if facts.state == "closed":
        return MonitorObservationStatus.BLOCKED, "pull_request_closed"
    if facts.state != "open" or not facts.head_revision:
        return MonitorObservationStatus.PENDING, "pull_request_state_unknown"
    if facts.draft:
        return MonitorObservationStatus.PENDING, "pull_request_draft"
    # Every branch below names the state it reads, so a state none of them names --
    # a terminal, non-blocking one -- is excluded from actionable AND from pending by
    # construction: displaced rows neither wake the session nor hold it open.
    check_states = {check.state for check in facts.checks}
    if "failed" in check_states:
        return MonitorObservationStatus.ACTIONABLE, "checks_failed"
    if facts.review_decision == "changes_requested":
        return MonitorObservationStatus.ACTIONABLE, "changes_requested"
    if facts.unresolved_review_threads:
        return MonitorObservationStatus.ACTIONABLE, "unresolved_review_threads"
    if facts.mergeability == "conflicting":
        return MonitorObservationStatus.ACTIONABLE, "merge_conflict"
    if facts.mergeability == "behind":
        return MonitorObservationStatus.ACTIONABLE, "branch_behind"
    if not facts.checks_complete:
        return MonitorObservationStatus.PENDING, "checks_incomplete"
    if "pending" in check_states:
        return MonitorObservationStatus.PENDING, "checks_pending"
    if "unknown" in check_states:
        return MonitorObservationStatus.PENDING, "checks_unknown"
    if not facts.review_threads_complete:
        return MonitorObservationStatus.PENDING, "review_threads_incomplete"
    if facts.review_decision == "unknown":
        return MonitorObservationStatus.PENDING, "review_state_unknown"
    if facts.review_decision == "review_required":
        return MonitorObservationStatus.PENDING, "review_required"
    if facts.mergeability in {"pending", "blocked"}:
        return MonitorObservationStatus.PENDING, "mergeability_pending"
    return MonitorObservationStatus.SUCCESS, "review_ready"


def fingerprint_pull_request_facts(canonical: Mapping[str, object]) -> str:
    """Hash the stable canonical JSON representation."""
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
