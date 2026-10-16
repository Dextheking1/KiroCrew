"""Adapters between the admission gate and the entry points that call it.

Each adapter is the whole of what one entry point adds: build the request from
what the handler already validated, call the gate, and translate the answer
into that entry point's own response or bookkeeping. They hold no state; the
gate (``admission_gate``) holds all of it.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable, Coroutine

from kiro_crew.shared_crew import admission_gate as _gate
from kiro_crew.shared_crew.admission import (
    TASKQ_STATE_MAP,
    AcceptedWork,
    OperationKind,
    OperationState,
    ReasonCode,
)

logger = logging.getLogger(__name__)

#: Response header naming the accepted work of a streamed chat send.
WORK_ID_HEADER = "X-Shared-Crew-Work"
WORK_STATE_HEADER = "X-Shared-Crew-Work-State"


# -- chat turns ------------------------------------------------------------------

#: Slot keys whose dispatched turn is still waiting for admission.
_PENDING_SLOTS: set[str] = set()


def mark_admission_pending(slot_key: str, pending: bool) -> None:
    if pending:
        _PENDING_SLOTS.add(slot_key)
    else:
        _PENDING_SLOTS.discard(slot_key)


def admission_pending(slot_key: str) -> bool:
    """Whether *slot_key*'s turn is accepted but not yet started. In memory."""
    return slot_key in _PENDING_SLOTS


async def admit_chat_turn(
    request: Any, *, session_key: str, slot_key: str, message: str
) -> _gate.Admission:
    """Admit one chat turn charged to the request's initiator."""
    service = await _gate.current_service()
    return await service.admit(
        budget_key=_gate.actor_budget(request),
        session_key=session_key or slot_key,
        operation=OperationKind.CHAT_TURN,
        idempotency_key=_gate.idempotency_key_of(request),
        digest=_gate.request_digest("chat", slot_key, message),
        request_bytes=len(message.encode("utf-8", "replace")),
    )


def admission_receipt(admission: _gate.Admission) -> dict[str, Any] | None:
    """The ``admission`` key a send receipt carries, or None with admission off."""
    if admission.passthrough or admission.work is None:
        return None
    return {"state": admission.work.state.value, "work": admission.work.to_public_dict()}


def admission_headers(admission: _gate.Admission) -> dict[str, str]:
    if admission.passthrough or admission.work is None:
        return {}
    return {WORK_ID_HEADER: admission.work.work_id, WORK_STATE_HEADER: admission.work.state.value}


async def run_admitted_turn(
    admission: _gate.Admission,
    coro: Coroutine[Any, Any, Any],
    *,
    revalidate: Callable[[], Awaitable[ReasonCode | None]] | None = None,
    on_ended: Callable[[AcceptedWork], None] | None = None,
) -> Any:
    """Run a turn through its admission; an unstarted end is reported, not raised."""
    try:
        return await admission.run(coro, revalidate=revalidate)
    except _gate.AdmissionEnded as ended:
        logger.info(
            "shared crew: turn %s ended before it ran (%s, %s)",
            ended.work.work_id,
            ended.work.state.value,
            ended.work.reason.value if ended.work.reason else "",
        )
        if on_ended is not None:
            on_ended(ended.work)
        return None


async def admitted_chat_turn(
    slot_key: str,
    admission: _gate.Admission,
    turn: Coroutine[Any, Any, Any],
    *,
    still_live: Callable[[], bool],
    on_ended: Callable[[], Any],
) -> Any:
    """Run a dispatched chat turn through its admission: wait, revalidate, run.

    While it waits the slot is marked pending (:func:`admission_pending`) so a
    later send cannot steer into a turn that has not started. Right before the
    turn runs the slot must still be live; a closed slot ends the work unstarted.
    """
    mark_admission_pending(slot_key, True)

    async def _revalidate() -> ReasonCode | None:
        mark_admission_pending(slot_key, False)
        return None if still_live() else ReasonCode.CANCELLED_BY_USER

    try:
        return await run_admitted_turn(
            admission, turn, revalidate=_revalidate, on_ended=lambda _w: on_ended()
        )
    finally:
        mark_admission_pending(slot_key, False)


def admitted_or_direct(
    slot_key: str,
    admission: _gate.Admission,
    turn: Coroutine[Any, Any, Any],
    *,
    still_live: Callable[[], bool],
    on_ended: Callable[[], Any],
) -> Coroutine[Any, Any, Any]:
    """*turn* itself when *admission* is a pass-through, else :func:`admitted_chat_turn`."""
    if admission.passthrough:
        return turn
    return admitted_chat_turn(slot_key, admission, turn, still_live=still_live, on_ended=on_ended)


# -- subagents -------------------------------------------------------------------


def subagent_probe(manager: Any) -> Callable[[str], OperationState | None]:
    """A child probe answering a subagent run's state from the manager, then the task row.

    None means still running (or not decidable yet). Runs off-loop (the gate
    calls it through ``asyncio.to_thread``), so the task-row read is allowed.
    """

    def probe(agent_id: str) -> OperationState | None:
        agents = getattr(manager, "_agents", None)
        info = agents.get(agent_id) if isinstance(agents, dict) else None
        if info is not None:
            if not getattr(info, "done", False):
                return None
            return OperationState.FAILED if getattr(info, "error", "") else OperationState.COMPLETED
        store = None
        try:
            store = manager.taskq_store()
        except Exception:
            store = None
        if store is not None:
            try:
                row = store.get(agent_id)
            except Exception:
                row = None
            if row is not None:
                mapped = TASKQ_STATE_MAP.get(str(row.state))
                return mapped if mapped is not None and mapped.value in _TERMINAL_VALUES else None
        # Neither the manager nor the queue knows it: it may have run, and is gone.
        return OperationState.UNKNOWN_OUTCOME

    return probe


_TERMINAL_VALUES = frozenset(
    {"completed", "failed", "cancelled", "expired", "rejected", "unknown_outcome"}
)


def subagent_cancel(manager: Any, loop: Any) -> Callable[[AcceptedWork], None]:
    """A child cancel hook: stop the subagent run the record names."""

    def cancel(work: AcceptedWork) -> None:
        agent_id = str(work.extra.get("execution_id") or "")
        stop = getattr(manager, "cancel", None)
        if not agent_id or stop is None:
            return
        try:
            loop.create_task(stop(agent_id))
        except Exception:
            logger.debug("shared crew: subagent cancel failed", exc_info=True)

    return cancel


async def admit_subagent(request: Any, state: Any, parent_session: str) -> _gate.Admission:
    """Admit a spawn as a child of whatever runs in *parent_session*."""
    service = await _gate.current_service()
    if not service.enabled:
        return _gate.Admission(service, "passthrough")
    manager = getattr(state, "subagents", None)
    if manager is not None:
        service.set_child_probe(subagent_probe(manager))
        service.set_child_cancel(subagent_cancel(manager, asyncio.get_running_loop()))
    internal = request.get("internal_auth") is True
    admission = await service.admit_child(
        parent_session_key=parent_session,
        fallback_budget=_gate.actor_budget(request, automation=internal),
    )
    if admission.admitted and admission.work is not None:
        await service.prepare_principal_slice(admission.work.budget_key)
    return admission


async def admit_app_spawn(manager: Any, app: str) -> _gate.Admission:
    """Admit an app's background spawn (``apps.spawn_sdk``) as a child.

    An app spawns with no request and no parent turn, so the child is charged
    to the app's bounded system principal, the one ``actor_budget`` names for
    an app token. The caller attaches the started run or abandons the slot.
    Outside a gateway (no installed service) this is a pass-through.
    """
    if _gate.installed_service() is None:
        return _gate.Admission(None, "passthrough")
    service = await _gate.current_service()
    if not service.enabled:
        return _gate.Admission(service, "passthrough")
    if manager is not None:
        service.set_child_probe(subagent_probe(manager))
        service.set_child_cancel(subagent_cancel(manager, asyncio.get_running_loop()))
    from kiro_crew.shared_crew.contracts import Principal

    admission = await service.admit_child(
        parent_session_key=f"app:{app}",
        fallback_budget=Principal.system(f"app:{app}"[:64]).id,
    )
    if admission.admitted and admission.work is not None:
        await service.prepare_principal_slice(admission.work.budget_key)
    return admission


# -- cron -------------------------------------------------------------------------


async def admitted_cron_dispatch(job: Any, dispatch: Coroutine[Any, Any, Any]) -> Any:
    """Run one cron message dispatch through fair admission.

    Charged to the principal the job recorded as its initiator, else to the
    bounded automation owner. A refused or expired fire is skipped like a
    governance denial: recorded on the job, never fed to the auto-pause counter.
    With fair admission off this awaits *dispatch* unchanged.
    """
    try:
        return await _gate.admit_and_run(
            dispatch,
            budget_key=_gate.recorded_principal(getattr(job, "created_by_principal", "")),
            session_key=f"cron:{job.id}",
            operation=OperationKind.CRON,
        )
    except _gate.AdmissionRefused as refused:
        reason = refused.admission.reason
        job.last_status = "error"
        job.last_error = (
            f"fair admission refused this run ({reason.value if reason else 'refused'})"
        )
    except _gate.AdmissionEnded as ended:
        job.last_status = "error"
        job.last_error = (
            f"fair admission ended this run before it started ({ended.work.state.value})"
        )
    logger.info("cron %s: %s", getattr(job, "id", "?"), job.last_error)
    return None


# -- background runs (task runner, workflows) ---------------------------------------

_TASKRUNNER_TERMINAL: dict[str, OperationState] = {
    "completed": OperationState.COMPLETED,
    "done": OperationState.COMPLETED,
    "failed": OperationState.FAILED,
    "cancelled": OperationState.CANCELLED,
    # A paused run holds no runtime; a restart is new work.
    "paused": OperationState.CANCELLED,
}

_WORKFLOW_TERMINAL: dict[str, OperationState] = {
    "finished": OperationState.COMPLETED,
    "failed": OperationState.FAILED,
    "cancelled": OperationState.CANCELLED,
    "paused": OperationState.CANCELLED,
}


def taskrunner_probe(runner: Any) -> Callable[[str], OperationState | None]:
    def probe(task_id: str) -> OperationState | None:
        runs = getattr(runner, "_runs", None)
        run = runs.get(task_id) if isinstance(runs, dict) else None
        if run is None:
            return OperationState.UNKNOWN_OUTCOME
        return _TASKRUNNER_TERMINAL.get(str(getattr(run, "status", "")))

    return probe


def workflow_probe(service: Any) -> Callable[[str], OperationState | None]:
    def probe(run_id: str) -> OperationState | None:
        try:
            snap = service.status(run_id)
        except Exception:
            return None
        if not isinstance(snap, dict):
            return OperationState.UNKNOWN_OUTCOME
        return _WORKFLOW_TERMINAL.get(str(snap.get("status") or ""))

    return probe


def _cancel_hook(cancel: Callable[[str], Any]) -> Callable[[AcceptedWork], None]:
    def hook(work: AcceptedWork) -> None:
        run_id = str(work.extra.get("execution_id") or "")
        if not run_id:
            return
        try:
            result = cancel(run_id)
            if inspect.isawaitable(result):
                asyncio.ensure_future(result)
        except Exception:
            logger.debug("shared crew: cancelling run %s failed", run_id, exc_info=True)

    return hook


async def admit_background_run(
    request: Any,
    *,
    operation: OperationKind,
    session_key: str,
    probe: Callable[[str], OperationState | None] | None,
    cancel: Callable[[str], Any] | None = None,
) -> _gate.Admission:
    """Admit a background run that starts now or is refused; it never queues here.

    The caller starts the run only when admitted, then calls
    ``attach_child(run_id)``; the gate's tick settles the record through
    *probe* and enforces the run deadline through *cancel*.
    """
    service = await _gate.current_service()
    if not service.enabled:
        return _gate.Admission(service, "passthrough")
    if probe is not None:
        service.register_probe(operation, probe)
    if cancel is not None:
        service.register_cancel(operation, _cancel_hook(cancel))
    admission = await service.admit(
        budget_key=_gate.actor_budget(request, automation=request.get("internal_auth") is True),
        session_key=session_key or operation.value,
        operation=operation,
        idempotency_key=_gate.idempotency_key_of(request),
        digest=_gate.request_digest(operation.value, session_key),
        allow_queue=False,
    )
    if admission.admitted and admission.work is not None:
        await service.prepare_principal_slice(admission.work.budget_key)
    return admission


__all__ = [
    "admit_app_spawn",
    "admit_background_run",
    "admitted_chat_turn",
    "admitted_or_direct",
    "admitted_cron_dispatch",
    "taskrunner_probe",
    "workflow_probe",
    "WORK_ID_HEADER",
    "WORK_STATE_HEADER",
    "admission_headers",
    "admission_pending",
    "admission_receipt",
    "admit_chat_turn",
    "admit_subagent",
    "mark_admission_pending",
    "run_admitted_turn",
    "subagent_cancel",
    "subagent_probe",
]
