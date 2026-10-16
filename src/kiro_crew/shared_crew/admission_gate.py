"""The one admission call every execution entry point makes.

An entry point builds nothing itself. It asks :func:`current_service` for the
gateway's :class:`AdmissionService` and calls :meth:`AdmissionService.admit`
(work a person starts) or :meth:`AdmissionService.admit_child` (work running
work starts). The answer is an :class:`Admission`:

* ``passthrough`` -- fair admission is off (``dashboard.shared_crew``); nothing
  is recorded and :meth:`Admission.run` just awaits the work. This is the
  single-operator default and it changes no existing behaviour.
* ``rejected`` -- refused with a :class:`ReasonCode`; nothing was accepted.
  :meth:`Admission.refusal_response` is the HTTP answer (``error_body``,
  ``Retry-After``).
* ``admitted`` or ``queued`` -- durably accepted (``work_store``) before the
  caller hears about it. :meth:`Admission.run` waits for a slot, revalidates
  expiry, policy and the caller's own check right before starting, runs the
  work inside its principal's containment slice with a deadline, records the
  terminal state and releases the slot once the work and its descendants are
  proven stopped.

Budget: the INITIATOR's principal (``ActorContext`` published by the auth
middleware under ``ACTOR_REQUEST_KEY``; Unknown when absent, so every
unattributed caller shares one bounded bucket). Never the session creator.
Children inherit the budget and root execution of the session that spawned
them (:meth:`AdmissionService.budget_for_session`). Automation with no recorded
initiator is charged to :data:`AUTOMATION_PRINCIPAL`, which has the same
limits as anyone.

Restart: :meth:`AdmissionService.ensure_ready` reconciles every non-terminal
record before the first admission of a process. Work proven never started is
re-queued only when a relauncher for its operation is registered, otherwise it
ends ``cancelled``; work that may have run ends ``unknown_outcome`` and is never
replayed; work whose runtime is still alive keeps its slot in ``reconciling``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Coroutine, Mapping, TypeVar

from kiro_crew.config.sections_shared_crew import EffectiveLimits
from kiro_crew.shared_crew import containment as _containment
from kiro_crew.shared_crew.accounting import SessionCreateRate, StorageAccount
from kiro_crew.shared_crew.admission import (
    CAPACITY_HOLDING,
    TERMINAL,
    AcceptedWork,
    AdmissionRequest,
    CapacityResponse,
    CapacityScope,
    ContainmentKind,
    InvalidTransition,
    Liveness,
    OperationKind,
    OperationState,
    ReasonCode,
    ResourceEstimate,
    RuntimeOwnershipRef,
    ScopeUsage,
    StaleRevision,
    error_body,
    http_status,
    retry_after_header,
)
from kiro_crew.shared_crew.codec import ContractError
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    ActorContext,
    EnforcementLevel,
    Principal,
    Transport,
)
from kiro_crew.shared_crew.scheduler import (
    Decision,
    FairScheduler,
    SchedulerLimits,
    Ticket,
    WorkClass,
)
from kiro_crew.shared_crew.work_store import IdempotencyConflict, WorkStore, new_work_id

logger = logging.getLogger(__name__)

T = TypeVar("T")
S = OperationState

#: The bounded owner of automation that records no initiator.
AUTOMATION_PRINCIPAL = Principal.system("automation")

#: Header a client sends per logical send, reused on retry.
IDEMPOTENCY_HEADER = "Idempotency-Key"

#: Refusal code when the durable store is unavailable (no ``ReasonCode`` names it).
TASK_STORE_UNAVAILABLE_CODE = "task_store_unavailable"

#: ``extra`` key naming why reconciliation ended a record, for operators.
RECONCILE_DETAIL_KEY = "reconcile_detail"
RECONCILE_RESTARTED_BEFORE_START = "host_restarted_before_start"
RECONCILE_RAN_BEFORE_RESTART = "host_restarted_while_running"

#: Operations whose work is a child of running work.
CHILD_OPERATIONS = frozenset({OperationKind.SUBAGENT})

#: Polling hints sent as ``Retry-After`` with each refusal.
_RETRY_AFTER: Mapping[ReasonCode, int] = {
    ReasonCode.PRINCIPAL_QUEUE_FULL: 10,
    ReasonCode.CREW_QUEUE_FULL: 10,
    ReasonCode.PRINCIPAL_ACTIVE_LIMIT: 10,
    ReasonCode.CREW_ACTIVE_LIMIT: 10,
    ReasonCode.STORAGE_LIMIT: 300,
    ReasonCode.CONTAINMENT_UNAVAILABLE: 30,
}

_SESSION_BINDINGS_MAX = 20000
#: How long a containment probe answers before it is taken again.
_REPORT_TTL_SECONDS = 60.0


# -- the actor ----------------------------------------------------------------


def actor_of(request: Any) -> ActorContext | None:
    """The trusted actor the auth middleware published, or None.

    Read only from ``request[ACTOR_REQUEST_KEY]``; never from a header, body
    or query value.
    """
    getter = getattr(request, "get", None)
    if getter is None:
        return None
    actor = getter(ACTOR_REQUEST_KEY)
    return actor if isinstance(actor, ActorContext) else None


def principal_of(request: Any) -> Principal:
    """The initiator principal of *request*; Unknown when none was established."""
    actor = actor_of(request)
    return actor.principal if actor is not None else Principal.unknown()


def idempotency_key_of(request: Any) -> str:
    """The client's ``Idempotency-Key`` header, or a fresh server key when absent."""
    headers = getattr(request, "headers", None)
    raw = headers.get(IDEMPOTENCY_HEADER, "") if headers is not None else ""
    return raw.strip() if isinstance(raw, str) and raw.strip() else "srv-" + new_work_id()


def request_digest(*parts: object) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(repr(part).encode("utf-8", "replace"))
        h.update(b"\x1f")
    return h.hexdigest()


# -- outcomes -------------------------------------------------------------------


def coded_json_response(
    body: Mapping[str, Any], *, status: int, headers: Mapping[str, str] | None = None
) -> Any:
    """An :func:`error_body` as an aiohttp JSON response, its ``code`` written out.

    Every branch spells ``error`` and ``code`` in the literal, so the
    repository's error-code contract can see the code at each response site.
    """
    from aiohttp import web

    error, code = str(body["error"]), str(body["code"])
    hdrs = dict(headers or {})
    capacity, current = body.get("capacity"), body.get("current")
    if capacity is not None and current is not None:
        return web.json_response(
            {"error": error, "code": code, "capacity": capacity, "current": current},
            status=status,
            headers=hdrs,
        )
    if capacity is not None:
        return web.json_response(
            {"error": error, "code": code, "capacity": capacity}, status=status, headers=hdrs
        )
    if current is not None:
        return web.json_response(
            {"error": error, "code": code, "current": current}, status=status, headers=hdrs
        )
    return web.json_response({"error": error, "code": code}, status=status, headers=hdrs)


class AdmissionEnded(Exception):
    """The work ended before it ran (expired, cancelled, revalidation refused)."""

    def __init__(self, work: AcceptedWork) -> None:
        super().__init__(work.state.value)
        self.work = work


@dataclass
class _Live:
    work: AcceptedWork
    ticket: Ticket
    request: AdmissionRequest
    admitted: asyncio.Event = field(default_factory=asyncio.Event)
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_cb: Callable[[], Any] | None = None
    cancel_reason: ReasonCode | None = None
    root_work_id: str = ""
    children: set[str] = field(default_factory=set)
    #: The work's own execution finished; capacity may still wait on children.
    self_stopped: bool = False
    #: Reconciliation: runtime ownership not yet proven stopped.
    awaiting_proof: bool = False


@dataclass
class Admission:
    """The answer to one admission call. See the module docstring."""

    service: AdmissionService | None
    decision: str
    work: AcceptedWork | None = None
    reason: ReasonCode | None = None
    capacity: CapacityResponse | None = None
    detail: str = ""
    store_unavailable: bool = False
    replayed: bool = False

    @property
    def passthrough(self) -> bool:
        return self.decision == "passthrough"

    @property
    def rejected(self) -> bool:
        return self.decision == Decision.REJECTED.value

    @property
    def queued(self) -> bool:
        return self.decision == Decision.QUEUED.value

    @property
    def admitted(self) -> bool:
        return self.decision == Decision.ADMITTED.value

    def refusal_body(self) -> dict[str, Any]:
        if self.store_unavailable:
            return {
                "error": "The durable work queue is unavailable.",
                "code": TASK_STORE_UNAVAILABLE_CODE,
            }
        reason = self.reason or ReasonCode.CREW_ACTIVE_LIMIT
        body = error_body(
            reason,
            self.detail or reason.value.replace("_", " "),
            capacity=self.capacity,
            current=self.work if reason is ReasonCode.IDEMPOTENCY_CONFLICT else None,
        )
        return body

    def refusal_status(self) -> int:
        if self.store_unavailable:
            return 503
        return http_status(self.reason or ReasonCode.CREW_ACTIVE_LIMIT)

    def refusal_headers(self) -> dict[str, str]:
        return retry_after_header(self.capacity)

    def refusal_response(self) -> Any:
        return coded_json_response(
            self.refusal_body(), status=self.refusal_status(), headers=self.refusal_headers()
        )

    def public_work(self) -> dict[str, Any] | None:
        return None if self.work is None else self.work.to_public_dict()

    async def wait_admitted(self) -> AcceptedWork:
        """Wait until the work holds a slot. :class:`AdmissionEnded` if it ended first."""
        if self.passthrough or self.service is None or self.work is None:
            raise RuntimeError("no accepted work to wait for")
        return await self.service.wait_admitted(self.work.work_id)

    async def run(
        self,
        coro: Coroutine[Any, Any, T],
        *,
        revalidate: Callable[[], Awaitable[ReasonCode | None]] | None = None,
    ) -> T:
        """Run *coro* as this work. See :meth:`AdmissionService.run`."""
        if self.passthrough or self.service is None:
            return await coro
        if self.work is None or self.rejected:
            coro.close()
            raise RuntimeError("refused work cannot run")
        return await self.service.run(self.work.work_id, coro, revalidate=revalidate)

    async def attach_child(self, execution_id: str, *, session_key: str = "") -> None:
        """Mark an admitted child as started under *execution_id*."""
        if self.passthrough or self.service is None or self.work is None:
            return
        await self.service.attach_child(self.work.work_id, execution_id, session_key=session_key)

    async def abandon(self, *, reason: ReasonCode | None = None) -> None:
        """An admitted child never started (the spawn failed): end it and free its slot."""
        if self.passthrough or self.service is None or self.work is None:
            return
        await self.service.abandon(self.work.work_id, reason=reason)


Relauncher = Callable[[Mapping[str, Any], Admission], Awaitable[None]]
LivenessProbe = Callable[[RuntimeOwnershipRef], Liveness]
ChildProbe = Callable[[str], "OperationState | None"]
CancelHook = Callable[[AcceptedWork], Any]


def _systemd_unit_liveness(ref: RuntimeOwnershipRef) -> Liveness:
    """ALIVE while the work's systemd scope unit is active; STOPPED otherwise."""
    unit = ref.containment_unit
    if not unit or not unit.endswith(".scope"):
        # No unit of its own: an in-gateway execution, which cannot outlive the
        # process that ran it.
        return Liveness.STOPPED
    state = _containment.unit_active_state(unit)
    if state is None:
        return Liveness.UNKNOWN
    if state in ("active", "activating", "deactivating", "reloading"):
        return Liveness.ALIVE
    if state in ("inactive", "failed", "unknown", ""):
        return Liveness.STOPPED
    return Liveness.UNKNOWN


class AdmissionService:
    """Principal- and Crew-level fair admission over durable accepted work."""

    def __init__(
        self,
        store: WorkStore | None,
        *,
        limits: EffectiveLimits,
        clock: Callable[[], float] = time.time,
        containment_probe: Callable[[], _containment.ContainmentReport] | None = None,
        slices: _containment.SliceManager | None = None,
        liveness: LivenessProbe = _systemd_unit_liveness,
        child_probe: ChildProbe | None = None,
        storage: StorageAccount | None = None,
    ) -> None:
        self._store = store
        self._limits = limits
        self._clock = clock
        self._scheduler = FairScheduler(_scheduler_limits(limits), clock=clock)
        self._containment_probe = containment_probe or (
            lambda: _containment.probe_containment(
                hard_limits=self._limits.hard_limits,
                slices_enabled=self._limits.containment_enabled,
            )
        )
        self._report: _containment.ContainmentReport | None = None
        self._report_at = 0.0
        self._slices = slices
        self._liveness = liveness
        self._child_probe = child_probe
        self._storage = storage or (StorageAccount(store.tasks, clock=clock) if store else None)
        self._create_rate = SessionCreateRate(limits.session_creates_per_minute)
        self._live: dict[str, _Live] = {}
        self._sessions: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._relaunchers: dict[OperationKind, Relauncher] = {}
        self._probes: dict[OperationKind, ChildProbe] = {}
        self._cancels: dict[OperationKind, CancelHook] = {}
        self._lock = asyncio.Lock()
        self._ready = False
        self._promotions: set[asyncio.Task[None]] = set()
        self._relaunches: set[asyncio.Future[None]] = set()

    # -- configuration --------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._limits.enabled

    @property
    def limits(self) -> EffectiveLimits:
        return self._limits

    @property
    def scheduler(self) -> FairScheduler:
        return self._scheduler

    def set_limits(self, limits: EffectiveLimits) -> None:
        self._limits = limits
        self._create_rate.set_rate(limits.session_creates_per_minute)
        self._report = None
        self._schedule_promotions(self._scheduler.set_limits(_scheduler_limits(limits)))

    def set_child_probe(self, probe: ChildProbe | None) -> None:
        self._child_probe = probe

    def register_probe(self, operation: OperationKind, probe: ChildProbe | None) -> None:
        """Answer the state of *operation*'s external runs by execution id (off-loop)."""
        if probe is None:
            self._probes.pop(operation, None)
        else:
            self._probes[operation] = probe

    def _probe_for(self, live: _Live) -> ChildProbe | None:
        if live.ticket.work_class is WorkClass.CHILD:
            return self._child_probe
        return self._probes.get(live.work.operation)

    def register_relauncher(self, operation: OperationKind, fn: Relauncher) -> None:
        self._relaunchers[operation] = fn

    def policy_version(self) -> str:
        """Changes when a setting that decides whether queued work may start changes."""
        lim = self._limits
        return request_digest(lim.enabled, lim.hard_limits, lim.containment_enabled)[:16]

    async def containment_report(self, *, refresh: bool = False) -> _containment.ContainmentReport:
        stale = time.monotonic() - self._report_at >= _REPORT_TTL_SECONDS
        if self._report is None or refresh or stale:
            self._report = await asyncio.to_thread(self._containment_probe)
            self._report_at = time.monotonic()
            if _SERVICE is self:
                _containment.publish_report(self._report if self.enabled else None)
        return self._report

    def cached_containment_report(self) -> _containment.ContainmentReport | None:
        return self._report

    # -- session bindings (budget inheritance) -------------------------------

    def bind_session(self, session_key: str, budget_key: str, root_work_id: str = "") -> None:
        if not session_key:
            return
        self._sessions[session_key] = (budget_key, root_work_id)
        self._sessions.move_to_end(session_key)
        while len(self._sessions) > _SESSION_BINDINGS_MAX:
            self._sessions.popitem(last=False)

    def budget_for_session(self, session_key: str) -> str | None:
        """The principal whose work is (or last was) running in *session_key*. In memory."""
        bound = self._sessions.get(session_key)
        return bound[0] if bound else None

    def root_for_session(self, session_key: str) -> str:
        bound = self._sessions.get(session_key)
        return bound[1] if bound else ""

    # -- admission --------------------------------------------------------------

    async def ensure_ready(self) -> None:
        """Reconcile durable work once per process, before anything new is admitted."""
        if self._ready or not self.enabled:
            return
        async with self._lock:
            if self._ready:
                return
            if self._store is not None:
                await self._reconcile_locked()
            self._ready = True

    def _refusal(
        self,
        reason: ReasonCode,
        budget_key: str,
        *,
        scope: CapacityScope | None = None,
        detail: str = "",
        work: AcceptedWork | None = None,
        retry_after: int | None = None,
    ) -> Admission:
        return Admission(
            self,
            Decision.REJECTED.value,
            work=work,
            reason=reason,
            capacity=self.capacity(
                budget_key,
                reason=reason,
                limiting_scope=scope,
                retry_after=retry_after if retry_after is not None else _RETRY_AFTER.get(reason),
            ),
            detail=detail,
        )

    async def admit(
        self,
        *,
        budget_key: str,
        session_key: str,
        operation: OperationKind,
        idempotency_key: str,
        digest: str = "",
        request_bytes: int = 0,
        payload: Mapping[str, Any] | None = None,
        parent_execution_id: str = "",
        estimate: ResourceEstimate | None = None,
        allow_queue: bool = True,
    ) -> Admission:
        """Admit, queue or refuse one root execution charged to *budget_key*."""
        if not self.enabled:
            return Admission(self, "passthrough")
        await self.ensure_ready()
        work_class = WorkClass.CHILD if operation in CHILD_OPERATIONS else WorkClass.ROOT
        now = self._clock()
        lim = self._limits
        expires_at = now + lim.queue_expiry_seconds
        try:
            request = AdmissionRequest(
                idempotency_key=idempotency_key,
                budget_key=budget_key,
                session_key=session_key,
                operation=operation,
                submitted_at=now,
                expires_at=expires_at,
                policy_version=self.policy_version(),
                parent_execution_id=parent_execution_id,
                estimate=estimate or ResourceEstimate(),
                request_digest=digest,
            )
        except ContractError as exc:
            return Admission(
                self, Decision.REJECTED.value, reason=ReasonCode.REQUEST_TOO_LARGE, detail=str(exc)
            )
        if self._store is None:
            return Admission(self, Decision.REJECTED.value, store_unavailable=True)
        async with self._lock:
            existing = await self._existing(request)
            if existing is not None:
                return existing
            if request_bytes > lim.max_request_bytes:
                return self._refusal(ReasonCode.REQUEST_TOO_LARGE, budget_key)
            report = await self.containment_report()
            if report.refusal_reason is not None:
                return self._refusal(
                    ReasonCode.CONTAINMENT_UNAVAILABLE,
                    budget_key,
                    detail=f"containment unavailable: {report.refusal_reason}",
                )
            if self._storage is not None and await self._storage.would_exceed(
                budget_key, request_bytes, lim.principal_storage_bytes
            ):
                return self._refusal(ReasonCode.STORAGE_LIMIT, budget_key)
            work_id = new_work_id()
            ticket = Ticket(
                work_id=work_id,
                budget_key=budget_key,
                session_key=session_key,
                work_class=work_class,
                slots=request.estimate.slots,
                weight=request.estimate.weight,
                expires_at=expires_at,
            )
            result = self._scheduler.submit(ticket, allow_queue=allow_queue)
            if result.decision is Decision.REJECTED:
                return self._refusal(
                    result.reason or ReasonCode.CREW_QUEUE_FULL,
                    budget_key,
                    scope=result.limiting_scope,
                )
            state = S.ADMITTED if result.decision is Decision.ADMITTED else S.QUEUED
            root = self.root_for_session(session_key) if work_class is WorkClass.CHILD else ""
            try:
                work, inserted = await self._store.accept(
                    request,
                    state=state,
                    payload=payload,
                    work_id=work_id,
                    deadline_at=(
                        self._deadline_for(work_class, now) if state is S.ADMITTED else None
                    ),
                    extra={"root_work_id": root} if root else None,
                )
            except IdempotencyConflict as exc:
                self._undo(ticket)
                return Admission(
                    self,
                    Decision.REJECTED.value,
                    work=exc.current,
                    reason=ReasonCode.IDEMPOTENCY_CONFLICT,
                    capacity=self.capacity(budget_key, reason=ReasonCode.IDEMPOTENCY_CONFLICT),
                )
            except Exception:
                logger.warning("shared crew: accepting work failed", exc_info=True)
                self._undo(ticket)
                return Admission(self, Decision.REJECTED.value, store_unavailable=True)
            if not inserted:
                self._undo(ticket)
                return self._replay(work)
            live = _Live(work=work, ticket=ticket, request=request, root_work_id=root)
            self._live[work_id] = live
            if root and root in self._live:
                self._live[root].children.add(work_id)
            if state is S.ADMITTED:
                live.admitted.set()
            if request_bytes > 0 and self._storage is not None:
                try:
                    await self._storage.charge(budget_key, request_bytes)
                except Exception:
                    logger.debug("shared crew: storage charge failed", exc_info=True)
        self._schedule_promotions(result.also_admitted)
        return Admission(
            self,
            Decision.ADMITTED.value if state is S.ADMITTED else Decision.QUEUED.value,
            work=work,
            reason=result.reason,
            capacity=self.capacity(
                budget_key, reason=result.reason, limiting_scope=result.limiting_scope
            ),
        )

    async def admit_child(
        self,
        *,
        parent_session_key: str,
        fallback_budget: str,
        operation: OperationKind = OperationKind.SUBAGENT,
        idempotency_key: str | None = None,
        digest: str = "",
    ) -> Admission:
        """Admit a child of the work running in *parent_session_key*, charged to its initiator."""
        budget = self.budget_for_session(parent_session_key) or fallback_budget
        return await self.admit(
            budget_key=budget,
            session_key=parent_session_key or "subagent",
            operation=operation,
            idempotency_key=idempotency_key or "srv-" + new_work_id(),
            digest=digest,
            parent_execution_id=self.root_for_session(parent_session_key),
        )

    def check_session_create(self, budget_key: str) -> Admission | None:
        """A refusal when *budget_key* is over its session-creation rate, else None."""
        if not self.enabled:
            return None
        allowed, retry = self._create_rate.allow(budget_key)
        if allowed:
            return None
        return self._refusal(ReasonCode.SESSION_CREATE_RATE, budget_key, retry_after=retry)

    async def charge_storage(self, budget_key: str, nbytes: int) -> None:
        if self.enabled and self._storage is not None and nbytes:
            await self._storage.charge(budget_key, nbytes)

    async def _existing(self, request: AdmissionRequest) -> Admission | None:
        assert self._store is not None
        rows = await self._store.tasks.run(self._store.tasks.work_by_scope, request.scope)
        if rows is None:
            return None
        work = await self._store.get(rows)
        if work is None:
            return None
        if work.request_digest != request.request_digest:
            return Admission(
                self,
                Decision.REJECTED.value,
                work=work,
                reason=ReasonCode.IDEMPOTENCY_CONFLICT,
                capacity=self.capacity(request.budget_key, reason=ReasonCode.IDEMPOTENCY_CONFLICT),
            )
        return self._replay(work)

    def _replay(self, work: AcceptedWork) -> Admission:
        live = self._live.get(work.work_id)
        current = live.work if live is not None else work
        decision = Decision.QUEUED.value if current.state is S.QUEUED else Decision.ADMITTED.value
        return Admission(
            self,
            decision,
            work=current,
            capacity=self.capacity(current.budget_key),
            replayed=True,
        )

    def _undo(self, ticket: Ticket) -> None:
        if self._scheduler.cancel_queued(ticket.work_id) is None:
            self._schedule_promotions(self._scheduler.release(ticket.work_id).admitted)

    def _deadline_for(self, work_class: WorkClass, now: float) -> float:
        lim = self._limits
        span = (
            lim.child_deadline_seconds
            if work_class is WorkClass.CHILD
            else lim.root_deadline_seconds
        )
        return now + span

    # -- promotion of queued work ------------------------------------------------

    def _schedule_promotions(self, tickets: tuple[Ticket, ...] | list[Ticket]) -> None:
        for ticket in tickets:
            try:
                task = asyncio.get_running_loop().create_task(self._promote(ticket.work_id))
            except RuntimeError:
                continue
            self._promotions.add(task)
            task.add_done_callback(self._promotions.discard)

    async def _promote(self, work_id: str) -> None:
        live = self._live.get(work_id)
        if live is None or self._store is None:
            return
        if live.work.state is not S.QUEUED:
            live.admitted.set()
            return
        try:
            live.work = await self._store.advance(
                live.work,
                S.ADMITTED,
                deadline_at=self._deadline_for(live.ticket.work_class, self._clock()),
            )
        except (StaleRevision, InvalidTransition):
            current = await self._store.get(work_id)
            if current is not None:
                live.work = current
        except Exception:
            logger.warning("shared crew: promoting %s failed", work_id, exc_info=True)
            return
        live.admitted.set()

    async def drain_promotions(self) -> None:
        """Await every in-flight promotion write (tests and shutdown)."""
        while self._promotions:
            await asyncio.gather(*list(self._promotions), return_exceptions=True)

    async def drain_relaunches(self) -> None:
        """Await every relaunched execution (tests and shutdown)."""
        while self._relaunches:
            await asyncio.gather(*list(self._relaunches), return_exceptions=True)

    async def wait_admitted(self, work_id: str) -> AcceptedWork:
        live = self._live.get(work_id)
        if live is None:
            raise KeyError(work_id)
        while True:
            if live.work.state in TERMINAL:
                raise AdmissionEnded(live.work)
            if live.admitted.is_set() and live.work.state is not S.QUEUED:
                return live.work
            waiters = [
                asyncio.ensure_future(live.admitted.wait()),
                asyncio.ensure_future(live.ended.wait()),
            ]
            try:
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waiters:
                    w.cancel()

    # -- running ---------------------------------------------------------------

    async def run(
        self,
        work_id: str,
        coro: Coroutine[Any, Any, T],
        *,
        revalidate: Callable[[], Awaitable[ReasonCode | None]] | None = None,
    ) -> T:
        """Wait for a slot, revalidate, run *coro* in its principal's slice, settle the record.

        Raises :class:`AdmissionEnded` (with *coro* closed, never started) when
        the work expired, was cancelled or failed revalidation before it ran.
        """
        live = self._live.get(work_id)
        if live is None or self._store is None:
            coro.close()
            current = await self._store.get(work_id) if self._store is not None else None
            if current is None:
                raise KeyError(work_id)
            # A replayed handle for work that already ran: never run it twice.
            raise AdmissionEnded(current)
        if live.work.state not in (S.QUEUED, S.ADMITTED):
            coro.close()
            raise AdmissionEnded(live.work)
        try:
            await self.wait_admitted(work_id)
        except BaseException:
            coro.close()
            raise
        refusal = await self._revalidate(live, revalidate)
        if refusal is not None:
            coro.close()
            terminal = S.EXPIRED if refusal is ReasonCode.QUEUE_EXPIRED else S.CANCELLED
            await self._end(live, terminal, reason=refusal)
            raise AdmissionEnded(live.work)
        unit = await self._prepare_slice(live)
        runtime = RuntimeOwnershipRef(
            execution_id=work_id,
            root_execution_id=live.root_work_id or work_id,
            parent_execution_id=live.request.parent_execution_id,
            containment=ContainmentKind.SYSTEMD_SCOPE if unit else ContainmentKind.NONE,
            containment_unit=unit,
            liveness=Liveness.ALIVE,
            reconciled_at=None,
        )
        try:
            live.work = await self._store.advance(live.work, S.RUNNING, runtime=runtime)
        except (StaleRevision, InvalidTransition):
            coro.close()
            current = await self._store.get(work_id)
            if current is not None:
                live.work = current
            raise AdmissionEnded(live.work) from None
        self.bind_session(live.work.session_key, live.work.budget_key, work_id)
        from kiro_crew.shared_crew.turn_gate import mark_admitted

        with mark_admitted(work_id):
            inner = _containment.create_task_in_scope(coro, live.work.budget_key)
        live.cancel_cb = inner.cancel
        outcome: OperationState = S.COMPLETED
        try:
            return await asyncio.shield(inner)
        except asyncio.CancelledError:
            if not inner.done():
                inner.cancel()
                await asyncio.gather(inner, return_exceptions=True)
            outcome = S.CANCELLED
            raise
        except BaseException:
            outcome = S.FAILED
            raise
        finally:
            live.cancel_cb = None
            live.self_stopped = True
            await self._settle(live, outcome)

    async def _revalidate(
        self,
        live: _Live,
        revalidate: Callable[[], Awaitable[ReasonCode | None]] | None,
    ) -> ReasonCode | None:
        if live.cancel_reason is not None:
            return live.cancel_reason
        if live.work.state is S.QUEUED and self._clock() >= live.work.expires_at:
            return ReasonCode.QUEUE_EXPIRED
        if live.request.policy_version != self.policy_version():
            return ReasonCode.POLICY_CHANGED
        report = await self.containment_report()
        if report.refusal_reason is not None:
            return ReasonCode.CONTAINMENT_UNAVAILABLE
        if revalidate is not None:
            try:
                return await revalidate()
            except Exception:
                logger.warning("shared crew: revalidation raised; refusing", exc_info=True)
                return ReasonCode.GRANT_REVOKED
        return None

    async def _prepare_slice(self, live: _Live) -> str:
        """Apply the principal's slice off-loop; return its unit name, or "" if not applied."""
        return await self.prepare_principal_slice(live.work.budget_key)

    async def prepare_principal_slice(self, budget_key: str) -> str:
        """Size *budget_key*'s slice off-loop so its spawns land there; "" when not applied."""
        lim = self._limits
        if not lim.enabled or self._slices is None or not lim.containment_enabled:
            return ""
        report = await self.containment_report()
        if not report.available:
            return ""
        name = _containment.principal_slice_name(budget_key)
        limits = _containment.SliceLimits(
            cpu_quota_percent=lim.principal_cpu_quota_percent,
            memory_max_mb=lim.principal_memory_max_mb,
            tasks_max=lim.principal_tasks_max,
        )
        crew = _containment.SliceLimits(cpu_quota_percent=lim.crew_cpu_quota_percent)
        slices = self._slices

        def _apply() -> bool:
            # The Crew's own CPUQuota sits on the instance slice, above every principal's.
            if crew.cpu_quota_percent > 0 and report.cpu:
                slices.apply(_containment.instance_slice_name(), crew, cpu_delegated=True)
            return slices.apply(name, limits, cpu_delegated=report.cpu)

        ok = await asyncio.to_thread(_apply)
        return name if ok else ""

    async def _settle(self, live: _Live, outcome: OperationState) -> None:
        """Record *live*'s terminal state and release its slot once allowed."""
        assert self._store is not None
        state = live.work.state
        target = outcome
        reason: ReasonCode | None = None
        if live.cancel_reason is not None:
            target = S.CANCELLED
            reason = live.cancel_reason
        try:
            if state is S.RUNNING and target is S.CANCELLED:
                live.work = await self._store.advance(live.work, S.CANCELLING, reason=reason)
            stopped = self._stopped_ref(live)
            if live.work.state not in TERMINAL:
                live.work = await self._store.advance(
                    live.work, target, reason=reason, runtime=stopped
                )
        except (StaleRevision, InvalidTransition):
            current = await self._store.get(live.work.work_id)
            if current is not None:
                live.work = current
        except Exception:
            logger.warning("shared crew: settling %s failed", live.work.work_id, exc_info=True)
        live.ended.set()
        self._maybe_release(live)

    def _stopped_ref(self, live: _Live) -> RuntimeOwnershipRef:
        base = live.work.runtime or RuntimeOwnershipRef(execution_id=live.work.work_id)
        alive_children = sum(1 for c in live.children if c in self._live)
        return replace(
            base,
            liveness=Liveness.STOPPED,
            descendants_alive=alive_children,
            reconciled_at=self._clock(),
        )

    def _maybe_release(self, live: _Live) -> None:
        """Free *live*'s slot if it is terminal and nothing it started still runs."""
        work_id = live.work.work_id
        if live.work.state not in TERMINAL or live.awaiting_proof:
            return
        if any(c in self._live for c in live.children):
            return
        self._live.pop(work_id, None)
        self._schedule_promotions(self._scheduler.release(work_id).admitted)
        root = self._live.get(live.root_work_id) if live.root_work_id else None
        if root is not None:
            root.children.discard(work_id)
            self._maybe_release(root)

    async def _end(self, live: _Live, state: OperationState, *, reason: ReasonCode | None) -> None:
        """Move unstarted or child work straight to a terminal *state*."""
        assert self._store is not None
        if live.work.state is S.QUEUED:
            self._scheduler.cancel_queued(live.work.work_id)
        try:
            if live.work.state is S.RUNNING and state is S.CANCELLED:
                live.work = await self._store.advance(live.work, S.CANCELLING, reason=reason)
            if live.work.state not in TERMINAL:
                stopped = self._stopped_ref(live) if live.work.runtime is not None else None
                live.work = await self._store.advance(
                    live.work, state, reason=reason, runtime=stopped
                )
        except (StaleRevision, InvalidTransition):
            current = await self._store.get(live.work.work_id)
            if current is not None:
                live.work = current
        live.self_stopped = True
        live.ended.set()
        self._maybe_release(live)

    # -- children ----------------------------------------------------------------

    async def attach_child(self, work_id: str, execution_id: str, *, session_key: str = "") -> None:
        live = self._live.get(work_id)
        if live is None or self._store is None:
            return
        runtime = RuntimeOwnershipRef(
            execution_id=execution_id,
            parent_execution_id=live.request.parent_execution_id,
            root_execution_id=live.root_work_id,
            liveness=Liveness.ALIVE,
        )
        try:
            live.work = await self._store.advance(
                live.work, S.RUNNING, runtime=runtime, extra={"execution_id": execution_id}
            )
        except (StaleRevision, InvalidTransition):
            return
        if session_key:
            self.bind_session(session_key, live.work.budget_key, live.root_work_id or work_id)

    async def abandon(self, work_id: str, *, reason: ReasonCode | None = None) -> None:
        live = self._live.get(work_id)
        if live is None:
            return
        await self._end(live, S.FAILED, reason=reason)

    # -- cancellation ------------------------------------------------------------

    async def cancel(self, work_id: str, *, expected_revision: int) -> AcceptedWork:
        """Cancel *work_id* at *expected_revision*. Raises the contract's conflict errors."""
        if self._store is None:
            raise KeyError(work_id)
        live = self._live.get(work_id)
        current = live.work if live is not None else await self._store.get(work_id)
        if current is None:
            raise KeyError(work_id)
        if current.revision != expected_revision:
            raise StaleRevision(expected_revision, current.revision)
        if current.state in TERMINAL or current.state is S.CANCELLING:
            raise InvalidTransition(current.state, S.CANCELLED)
        if live is None:
            # A durable record this process does not run (reconciling after a
            # restart). Only a legal direct move is taken.
            return await self._store.advance(
                current, S.CANCELLED, reason=ReasonCode.CANCELLED_BY_USER
            )
        if current.state is S.RECONCILING:
            # Being resolved against runtime ownership; its outcome is not ours to pick.
            raise InvalidTransition(current.state, S.CANCELLED)
        live.cancel_reason = ReasonCode.CANCELLED_BY_USER
        if current.state in (S.QUEUED, S.ADMITTED):
            await self._end(live, S.CANCELLED, reason=ReasonCode.CANCELLED_BY_USER)
            return live.work
        live.work = await self._store.advance(
            live.work, S.CANCELLING, reason=ReasonCode.CANCELLED_BY_USER
        )
        if live.cancel_cb is not None:
            live.cancel_cb()
        else:
            hook = self._cancel_hook_for(live)
            if hook is not None:
                hook(live.work)
        return live.work

    _child_cancel: CancelHook | None = None

    def set_child_cancel(self, hook: CancelHook | None) -> None:
        self._child_cancel = hook

    def register_cancel(self, operation: OperationKind, hook: CancelHook | None) -> None:
        if hook is None:
            self._cancels.pop(operation, None)
        else:
            self._cancels[operation] = hook

    def _cancel_hook_for(self, live: _Live) -> CancelHook | None:
        if live.ticket.work_class is WorkClass.CHILD:
            return self._child_cancel
        return self._cancels.get(live.work.operation)

    # -- maintenance -------------------------------------------------------------

    async def tick(self, now: float | None = None) -> dict[str, int]:
        """Expire queued work, enforce deadlines, settle finished children. Idempotent."""
        if not self.enabled or self._store is None:
            return {}
        at = self._clock() if now is None else now
        counts = {"expired": 0, "deadline": 0, "children": 0, "proven": 0}
        for ticket in self._scheduler.expire(at):
            live = self._live.get(ticket.work_id)
            if live is not None:
                await self._end(live, S.EXPIRED, reason=ReasonCode.QUEUE_EXPIRED)
                counts["expired"] += 1
        for live in list(self._live.values()):
            work = live.work
            if (
                work.state is S.RUNNING
                and work.deadline_at is not None
                and at >= work.deadline_at
                and live.cancel_reason is None
            ):
                live.cancel_reason = ReasonCode.RUN_DEADLINE
                counts["deadline"] += 1
                if live.cancel_cb is not None:
                    try:
                        live.work = await self._store.advance(
                            live.work, S.CANCELLING, reason=ReasonCode.RUN_DEADLINE
                        )
                    except (StaleRevision, InvalidTransition):
                        pass
                    live.cancel_cb()
                else:
                    hook = self._cancel_hook_for(live)
                    if hook is not None:
                        try:
                            live.work = await self._store.advance(
                                live.work, S.CANCELLING, reason=ReasonCode.RUN_DEADLINE
                            )
                        except (StaleRevision, InvalidTransition):
                            pass
                        hook(live.work)
        for live in list(self._live.values()):
            probe = self._probe_for(live)
            if probe is None or live.cancel_cb is not None:
                continue
            exec_id = str(live.work.extra.get("execution_id") or "")
            if live.work.state not in (S.RUNNING, S.CANCELLING, S.RECONCILING) or not exec_id:
                continue
            state = await asyncio.to_thread(probe, exec_id)
            if state is not None and state in TERMINAL:
                await self._settle_child(live, state)
                counts["children"] += 1
        for live in list(self._live.values()):
            if live.awaiting_proof and live.work.runtime is not None:
                liveness = await asyncio.to_thread(self._liveness, live.work.runtime)
                if liveness is Liveness.STOPPED:
                    await self._finish_reconcile(live)
                    counts["proven"] += 1
        return counts

    async def _settle_child(self, live: _Live, state: OperationState) -> None:
        assert self._store is not None
        target = S.UNKNOWN_OUTCOME if state is S.EXPIRED else state
        try:
            if target is S.UNKNOWN_OUTCOME and live.work.state in (S.RUNNING, S.CANCELLING):
                live.work = await self._store.advance(live.work, S.RECONCILING)
            if target is S.CANCELLED and live.work.state is S.RUNNING:
                live.work = await self._store.advance(live.work, S.CANCELLING)
            live.work = await self._store.advance(
                live.work, target, reason=live.cancel_reason, runtime=self._stopped_ref(live)
            )
        except (StaleRevision, InvalidTransition):
            current = await self._store.get(live.work.work_id)
            if current is not None:
                live.work = current
        live.awaiting_proof = False
        live.self_stopped = True
        live.ended.set()
        self._maybe_release(live)

    # -- reconciliation ---------------------------------------------------------

    async def _reconcile_locked(self) -> None:
        assert self._store is not None
        rows = await self._store.list(states=[s for s in S if s not in TERMINAL], limit=100000)
        for work in rows:
            try:
                await self._reconcile_one(work)
            except Exception:
                logger.warning("shared crew: reconciling %s failed", work.work_id, exc_info=True)
        try:
            await self._store.tasks.run(self._store.purge_sync)
        except Exception:
            logger.debug("shared crew: purge failed", exc_info=True)

    async def _reconcile_one(self, work: AcceptedWork) -> None:
        assert self._store is not None
        detail_started = {RECONCILE_DETAIL_KEY: RECONCILE_RAN_BEFORE_RESTART}
        work_class = WorkClass.CHILD if work.operation in CHILD_OPERATIONS else WorkClass.ROOT
        ticket = Ticket(
            work_id=work.work_id,
            budget_key=work.budget_key,
            session_key=work.session_key,
            work_class=work_class,
            expires_at=work.expires_at,
        )
        request = AdmissionRequest(
            idempotency_key="reconcile-" + work.work_id[-24:],
            budget_key=work.budget_key,
            session_key=work.session_key,
            operation=work.operation,
            submitted_at=work.accepted_at,
            expires_at=max(work.expires_at, work.accepted_at),
            policy_version=self.policy_version(),
            request_digest=work.request_digest,
        )
        if work.state in (S.QUEUED, S.ADMITTED):
            # Proven never started: ``running`` is written before any launch.
            work = await self._store.advance(work, S.RECONCILING)
            relaunch = self._relaunchers.get(work.operation)
            payload = await self._store.tasks.run(self._store.payload_sync, work.work_id)
            if relaunch is not None and payload is not None and work.expires_at > self._clock():
                work = await self._store.advance(work, S.QUEUED)
                result = self._scheduler.submit(ticket)
                live = _Live(work=work, ticket=ticket, request=request)
                self._live[work.work_id] = live
                if result.decision is Decision.REJECTED:
                    await self._end(live, S.CANCELLED, reason=result.reason)
                    return
                if result.decision is Decision.ADMITTED:
                    self._schedule_promotions((ticket,))
                admission = Admission(self, result.decision.value, work=work)
                task: asyncio.Future[None] = asyncio.ensure_future(relaunch(payload, admission))
                self._relaunches.add(task)
                task.add_done_callback(self._relaunches.discard)
                return
            await self._store.advance(
                work,
                S.CANCELLED,
                extra={RECONCILE_DETAIL_KEY: RECONCILE_RESTARTED_BEFORE_START},
            )
            return
        if work.state in (S.RUNNING, S.CANCELLING):
            work = await self._store.advance(work, S.RECONCILING, extra=detail_started)
        # RECONCILING: holds capacity until proven stopped.
        self._scheduler.hold(ticket)
        live = _Live(work=work, ticket=ticket, request=request, awaiting_proof=True)
        live.root_work_id = str(work.extra.get("root_work_id") or "")
        self._live[work.work_id] = live
        if work_class is WorkClass.CHILD or (
            work.operation in self._probes and work.extra.get("execution_id")
        ):
            # Its owner (the subagent queue, the task runner, the workflow
            # registry) reconciles the run; the probe settles this record.
            live.awaiting_proof = False
            return
        runtime = work.runtime or RuntimeOwnershipRef(execution_id=work.work_id)
        liveness = await asyncio.to_thread(self._liveness, runtime)
        if liveness is Liveness.STOPPED:
            await self._finish_reconcile(live)

    async def _finish_reconcile(self, live: _Live) -> None:
        """A reconciling root whose runtime is proven stopped: it may have run; never replay."""
        assert self._store is not None
        prior_cancel = live.work.reason is ReasonCode.CANCELLED_BY_USER
        target = S.CANCELLED if prior_cancel else S.UNKNOWN_OUTCOME
        try:
            live.work = await self._store.advance(
                live.work, target, runtime=self._stopped_ref(live)
            )
        except (StaleRevision, InvalidTransition):
            pass
        live.awaiting_proof = False
        live.self_stopped = True
        live.ended.set()
        self._maybe_release(live)

    # -- reads ---------------------------------------------------------------------

    def capacity(
        self,
        budget_key: str,
        *,
        reason: ReasonCode | None = None,
        limiting_scope: CapacityScope | None = None,
        retry_after: int | None = None,
    ) -> CapacityResponse:
        lim = self._limits
        if not lim.enabled:
            return CapacityResponse(
                principal=ScopeUsage(), crew=ScopeUsage(), enforcement=EnforcementLevel.NONE
            )
        principal, crew = self._scheduler.usage(budget_key)
        return CapacityResponse(
            principal=principal,
            crew=crew,
            enforcement=EnforcementLevel.BEST_EFFORT,
            limiting_scope=limiting_scope,
            reason=reason,
            retry_after_seconds=retry_after,
            queue_expiry_seconds=lim.queue_expiry_seconds,
            run_deadline_seconds=lim.root_deadline_seconds,
        )

    def live_work(self, work_id: str) -> AcceptedWork | None:
        live = self._live.get(work_id)
        return live.work if live is not None else None

    async def current_work(self, work_id: str) -> AcceptedWork | None:
        live = self.live_work(work_id)
        if live is not None or self._store is None:
            return live
        return await self._store.get(work_id)

    def holds_capacity(self, work_id: str) -> bool:
        return self._scheduler.is_active(work_id)

    async def list_work(
        self, *, session_key: str | None = None, budget_key: str | None = None, limit: int = 200
    ) -> list[AcceptedWork]:
        if self._store is None:
            return []
        rows = await self._store.list(session_key=session_key, budget_key=budget_key, limit=limit)
        return [self._live[w.work_id].work if w.work_id in self._live else w for w in rows]

    def holding_states(self) -> frozenset[OperationState]:
        return CAPACITY_HOLDING


def _scheduler_limits(lim: EffectiveLimits) -> SchedulerLimits:
    return SchedulerLimits(
        principal_active=max(1, lim.principal_active),
        principal_children=max(1, lim.principal_children),
        principal_queue=max(0, lim.principal_queue),
        crew_active=max(1, lim.crew_active),
        crew_children=max(1, lim.crew_children),
        crew_queue=max(0, lim.crew_queue),
    )


# -- the gateway's service --------------------------------------------------------

_SERVICE: AdmissionService | None = None
_SERVICE_LOOP: asyncio.AbstractEventLoop | None = None
_CONFIG_TTL_SECONDS = 5.0
_last_config_at = 0.0
_ticker: asyncio.Task[None] | None = None


def install_service(service: AdmissionService | None) -> None:
    """Install the process's service (gateway startup, tests). None removes it."""
    global _SERVICE, _SERVICE_LOOP, _last_config_at
    _SERVICE = service
    _last_config_at = time.monotonic()
    from kiro_crew.taskq import lanes

    if service is None:
        _SERVICE_LOOP = None
        lanes.set_lane_resolver(None)
        _containment.install_slice_manager(None)
        _containment.publish_report(None)
        return
    _containment.publish_report(service._report if service.enabled else None)
    lanes.set_lane_resolver(service.budget_for_session if service.enabled else None)
    _containment.install_slice_manager(
        service._slices if service.enabled and service.limits.containment_enabled else None
    )


def installed_service() -> AdmissionService | None:
    """The installed service without building one; None before first use. No I/O."""
    return _SERVICE


def _load_limits() -> EffectiveLimits:
    from kiro_crew.acp.runtime_start import _resolve_session_start_concurrency
    from kiro_crew.config.sections_shared_crew import effective_limits, load_shared_crew_config

    return effective_limits(
        load_shared_crew_config(), auto_crew_concurrency=_resolve_session_start_concurrency()
    )


def _open_default_store() -> WorkStore | None:
    from kiro_crew.config.paths import data_home
    from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable

    try:
        tasks = TaskStore(TaskStore.default_path(data_home())).open()
    except TaskStoreUnavailable:
        logger.warning("shared crew: the durable work store is unavailable", exc_info=True)
        return None
    return WorkStore(tasks)


async def current_service() -> AdmissionService:
    """The gateway's admission service, built from config on first use.

    Config is re-read off-loop at most every few seconds, so a limit change
    applies without a restart. With fair admission off the service answers
    ``passthrough`` and opens no store.
    """
    global _last_config_at, _SERVICE_LOOP
    service = _SERVICE
    loop = asyncio.get_running_loop()
    if service is not None and _SERVICE_LOOP is not loop:
        # Built on another event loop (a restarted loop, a test): its locks and
        # events belong there, so this loop gets its own service.
        service = None
    now = time.monotonic()
    if service is not None and now - _last_config_at < _CONFIG_TTL_SECONDS:
        return service
    _last_config_at = now
    try:
        limits = await asyncio.to_thread(_load_limits)
    except Exception:
        logger.warning("shared crew: config unreadable; keeping the current limits", exc_info=True)
        if service is not None:
            return service
        from kiro_crew.config.sections_shared_crew import SharedCrewConfig, effective_limits

        limits = effective_limits(SharedCrewConfig(), auto_crew_concurrency=2)
    if service is None:
        store = await asyncio.to_thread(_open_default_store) if limits.enabled else None
        service = AdmissionService(store, limits=limits, slices=_containment.SliceManager())
        install_service(service)
        _SERVICE_LOOP = loop
        _start_ticker(service)
        return service
    if limits != service.limits:
        if limits.enabled and service._store is None:
            store = await asyncio.to_thread(_open_default_store)
            service = AdmissionService(store, limits=limits, slices=service._slices)
            install_service(service)
            _SERVICE_LOOP = loop
            _start_ticker(service)
            return service
        service.set_limits(limits)
        install_service(service)
        _SERVICE_LOOP = loop
        if _ticker is None or _ticker.done():
            _start_ticker(service)
    return service


async def startup_check() -> _containment.ContainmentReport | None:
    """Probe containment at gateway start; in hard-limit mode a missing piece is fatal.

    Returns the report (None with admission off). With ``hard_limits`` on and a
    prerequisite missing, logs the machine reason at ERROR: from then on every
    admission and every runtime start is refused (``runtime_start``'s backstop),
    so nothing runs without its limits.
    """
    service = await current_service()
    if not service.enabled:
        return None
    report = await service.containment_report(refresh=True)
    if report.refusal_reason is not None:
        logger.error(
            "shared crew: hard-limit mode is on and containment is unavailable (%s); "
            "no work will be admitted and no runtime will start until it is fixed",
            report.refusal_reason,
        )
    elif not report.available:
        logger.warning(
            "shared crew: per-person containment is unavailable (%s); limits are "
            "admission counts only",
            report.reason,
        )
    return report


def _start_ticker(service: AdmissionService, *, period: float = 1.0) -> None:
    global _ticker
    if _ticker is not None and not _ticker.done():
        _ticker.cancel()
    _ticker = None
    if not service.enabled:
        return

    async def _loop() -> None:
        while _SERVICE is service and service.enabled:
            try:
                await service.tick()
            except Exception:
                logger.debug("shared crew: tick failed", exc_info=True)
            await asyncio.sleep(period)

    try:
        _ticker = asyncio.get_running_loop().create_task(_loop())
    except RuntimeError:
        _ticker = None


class AdmissionRefused(Exception):
    """:func:`admit_and_run` was refused; ``admission`` carries the reason and body."""

    def __init__(self, admission: Admission) -> None:
        super().__init__((admission.reason or ReasonCode.CREW_ACTIVE_LIMIT).value)
        self.admission = admission


async def admit_and_run(
    coro: Coroutine[Any, Any, T],
    *,
    budget_key: str,
    session_key: str,
    operation: OperationKind,
    idempotency_key: str | None = None,
    digest: str = "",
    revalidate: Callable[[], Awaitable[ReasonCode | None]] | None = None,
) -> T:
    """Admit and run *coro* as one root execution; the shape a background caller needs.

    With fair admission off this is ``await coro``. A refusal closes *coro*
    unstarted and raises :class:`AdmissionRefused`; work that ends before it
    runs raises :class:`AdmissionEnded`.
    """
    service = await current_service()
    admission = await service.admit(
        budget_key=budget_key,
        session_key=session_key,
        operation=operation,
        idempotency_key=idempotency_key or "srv-" + new_work_id(),
        digest=digest,
    )
    if admission.rejected:
        coro.close()
        raise AdmissionRefused(admission)
    return await admission.run(coro, revalidate=revalidate)


def recorded_principal(value: object) -> str:
    """*value* when it is a well-formed principal id a record stored, else automation's."""
    if isinstance(value, str) and value.startswith("p_"):
        try:
            return Principal(id=value, kind=AUTOMATION_PRINCIPAL.kind).id
        except ContractError:
            pass
    return AUTOMATION_PRINCIPAL.id


def scope_principal_for_session(session_key: str) -> str | None:
    """The principal a spawn from *session_key* runs as, for the containment slice. In memory."""
    service = _SERVICE
    if service is None or not service.enabled or not session_key:
        return None
    return service.budget_for_session(session_key)


def actor_budget(request: Any, *, automation: bool = False) -> str:
    """The budget key for *request*: its actor's principal, automation's, or Unknown."""
    actor = actor_of(request)
    if actor is not None and not actor.principal.is_unknown:
        if automation and actor.transport is Transport.INTERNAL:
            return AUTOMATION_PRINCIPAL.id
        return actor.principal.id
    if automation:
        return AUTOMATION_PRINCIPAL.id
    return Principal.unknown().id


__all__ = [
    "AUTOMATION_PRINCIPAL",
    "Admission",
    "AdmissionEnded",
    "AdmissionRefused",
    "AdmissionService",
    "IDEMPOTENCY_HEADER",
    "RECONCILE_DETAIL_KEY",
    "TASK_STORE_UNAVAILABLE_CODE",
    "actor_budget",
    "coded_json_response",
    "actor_of",
    "admit_and_run",
    "current_service",
    "idempotency_key_of",
    "install_service",
    "installed_service",
    "principal_of",
    "recorded_principal",
    "request_digest",
    "scope_principal_for_session",
    "startup_check",
]
