"""Fair admission for every chat turn, wherever it starts.

``POST /api/chat`` admits its turn before the user row is written. Every other
producer of a chat turn (the queue drain, regenerate, rewind, thread replies,
channel transports, app agents, nudges, the OpenAI shim and more) reaches the
same ``_run_chat`` without a request in hand. Its exit guard calls
:func:`gate_turn`, so all of them pass through one admission check.

* A turn already running inside admitted work (a chat send, a cron dispatch)
  is not admitted twice: :func:`mark_admitted` binds the work id in a context
  variable the admitted task inherits.
* A nested turn (``_prompt_depth > 0``) runs inside its parent's admission.
* With fair admission off, or before the gateway built its service, the turn
  runs exactly as before.

The budget is the turn's initiator. A producer that knows the person sets it
with :func:`initiated_by` (the queue drain reads the queued entry's server
stamped author). An automation actor (``app``, ``cron``, ``gateway``, ...) is
charged to the bounded automation principal. Anything else is the bounded
Unknown bucket: unattributed work is never charged to an unlimited account.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from typing import Any, Callable, Coroutine, Iterable, Iterator, Mapping, TypeVar

from kiro_crew.shared_crew import admission_entry
from kiro_crew.shared_crew import admission_gate as _gate
from kiro_crew.shared_crew.admission import OperationKind, ReasonCode
from kiro_crew.shared_crew.contracts import Principal
from kiro_crew.shared_crew.work_store import new_work_id

logger = logging.getLogger(__name__)

T = TypeVar("T")

_ADMITTED_WORK: contextvars.ContextVar[str] = contextvars.ContextVar(
    "shared_crew_admitted_work", default=""
)
_INITIATOR: contextvars.ContextVar[str] = contextvars.ContextVar(
    "shared_crew_turn_initiator", default=""
)
_IN_CHANNEL_TURN: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "shared_crew_in_channel_turn", default=False
)

#: Turn actors that are a person. Every other recorded actor is automation.
_PERSON_ACTORS = frozenset({"", "user"})

#: Error-row CSS class, shared with the runner's own failure rows.
_ERROR_CLS = "msg msg-err"

#: Turn outcomes (``metrics.turns.turn_outcome``) that settle the work ``failed``.
_FAULT_OUTCOMES = frozenset({"error", "timeout", "stall_exhausted"})


@contextlib.contextmanager
def mark_admitted(work_id: str) -> Iterator[None]:
    """Run the enclosed task creation as admitted work *work_id*."""
    token = _ADMITTED_WORK.set(work_id)
    try:
        yield
    finally:
        _ADMITTED_WORK.reset(token)


@contextlib.contextmanager
def initiated_by(principal_id: str) -> Iterator[None]:
    """Charge turns started inside the block to *principal_id*."""
    token = _INITIATOR.set(principal_id)
    try:
        yield
    finally:
        _INITIATOR.reset(token)


def queued_initiator(entries: Iterable[Mapping[str, Any]]) -> str:
    """The one person every live queue entry names as its author, else ``""``."""
    from kiro_crew.dashboard.slot_queue_repository import RESTORED_QUEUE_KEY
    from kiro_crew.shared_crew.attribution import is_person, message_author

    authors: set[str] = set()
    for entry in entries:
        if entry.get(RESTORED_QUEUE_KEY):
            return ""
        meta = entry.get("meta")
        author = message_author(meta if isinstance(meta, Mapping) else None)
        if not is_person(author):
            return ""
        authors.add(author.id)
    return authors.pop() if len(authors) == 1 else ""


def admission_active() -> bool:
    """Whether the installed admission service enforces fair admission. No I/O."""
    service = _gate.installed_service()
    return service is not None and service.enabled


def in_channel_turn() -> bool:
    """Whether the caller already runs inside :func:`run_channel_turn`."""
    return _IN_CHANNEL_TURN.get()


def capacity_note(reason: object) -> str:
    """The one-line answer a refused message gets on its own surface."""
    label = str(getattr(reason, "value", reason) or "capacity").replace("_", " ")
    return f"This message did not start: Crew capacity ({label}). Try again later."


def admitted_work() -> str:
    return _ADMITTED_WORK.get()


def turn_budget(kwargs: Mapping[str, Any], initiator: str = "") -> str:
    """The budget key a turn with these ``_run_chat`` keywords is charged to."""
    if initiator:
        return initiator
    actor = kwargs.get("_turn_actor") or ""
    if actor not in _PERSON_ACTORS or kwargs.get("_synthetic_payload") is True:
        return _gate.AUTOMATION_PRINCIPAL.id
    return Principal.unknown().id


def turn_failure(stop_reason: str | None, new_rows: Iterable[object]) -> str:
    """Why a turn that returned normally failed, or ``""`` when it did not.

    *stop_reason* is the terminal event's stop reason, or None when the turn
    reached no terminal event. With one, a fault outcome is a failure. Without
    one, the turn failed when it wrote an error row (a runtime that could not
    start, a dead transport); a local command with no row is not a failure.
    """
    from kiro_crew.metrics.turns import turn_outcome

    if stop_reason is not None:
        outcome = turn_outcome(stop_reason)
        return f"turn ended: {outcome}" if outcome in _FAULT_OUTCOMES else ""
    for row in new_rows:
        if isinstance(row, Mapping) and row.get("role") == "error":
            return "turn ended on an error with no terminal event"
    return ""


def _rows_since(slot: Any, start_total: int) -> list[object]:
    messages = getattr(slot, "messages", None)
    total = getattr(slot, "total_messages", None)
    if not isinstance(messages, list) or not isinstance(total, int):
        return []
    added = total - start_total
    return list(messages[-added:]) if added > 0 else []


def _judge_turn(
    slot: Any, work_id: str, start_total: int, stop_reason: Callable[[], str | None] | None
) -> None:
    """Report a failed turn to its admitted work, so it settles ``failed``."""
    if not work_id or stop_reason is None:
        return
    try:
        detail = turn_failure(stop_reason(), _rows_since(slot, start_total))
    except Exception:  # noqa: BLE001 -- judging is best effort; the turn already ended
        logger.debug("shared crew: could not judge a turn's outcome", exc_info=True)
        return
    service = _gate.installed_service()
    if detail and service is not None:
        service.note_failure(work_id, detail)


def _note_not_started(slot: Any, reason: ReasonCode | None) -> None:
    try:
        slot.append(
            "error", "⚠ " + capacity_note(reason or ReasonCode.CREW_ACTIVE_LIMIT), _ERROR_CLS
        )
    except Exception:  # noqa: BLE001 -- the note is best effort
        logger.debug("shared crew: could not note an unstarted turn", exc_info=True)


async def admit_turn(
    *,
    budget_key: str,
    session_key: str,
    text: str,
    operation: OperationKind = OperationKind.CHAT_TURN,
) -> _gate.Admission | None:
    """Admit one turn charged to *budget_key*; None when it runs unadmitted.

    None means fair admission is off, the gateway has not built its service, or
    the caller already runs inside admitted work.
    """
    if _ADMITTED_WORK.get() or not admission_active():
        return None
    service = await _gate.current_service()
    if not service.enabled:
        return None
    admission = await service.admit(
        budget_key=budget_key,
        session_key=session_key,
        operation=operation,
        idempotency_key="srv-" + new_work_id(),
        digest=_gate.request_digest(operation.value, session_key, text),
        request_bytes=len(text.encode("utf-8", "replace")),
    )
    return None if admission.passthrough else admission


async def gate_turn(
    state: Any,
    slot: Any,
    message: object,
    kwargs: Mapping[str, Any],
    run: Callable[[], Coroutine[Any, Any, None]],
    *,
    stop_reason: Callable[[], str | None] | None = None,
) -> None:
    """Run *run()* as one admitted chat turn, or note why it did not start.

    Both marks are consumed here, in the turn's own task: a task the turn
    creates later (the queue drain starting the next entry) copies this
    context, and must face admission itself rather than inherit this turn's.
    *stop_reason* reads the turn's terminal stop reason once it returned (None
    for no terminal event); a failed turn settles its work ``failed``.
    """
    admitted = _ADMITTED_WORK.get()
    initiator = _INITIATOR.get()
    _ADMITTED_WORK.set("")
    _INITIATOR.set("")
    nested = int(kwargs.get("_prompt_depth") or 0) > 0
    if admitted or nested:
        start_total = getattr(slot, "total_messages", 0)
        await run()
        if not nested:
            _judge_turn(slot, admitted, start_total, stop_reason)
        return
    from kiro_crew.dashboard.chat_slack import slot_is_live
    from kiro_crew.dashboard.chat_utils import effective_session_key

    admission = await admit_turn(
        budget_key=turn_budget(kwargs, initiator),
        session_key=effective_session_key(slot) or slot.key,
        text=message if isinstance(message, str) else "",
    )
    if admission is None:
        await run()
        return
    if admission.rejected:
        _note_not_started(slot, admission.reason)
        return

    def _ended() -> None:
        work = admission.work
        _note_not_started(slot, work.reason if work is not None else None)
        state.push_slots_update()

    async def _judged() -> None:
        work_id, start_total = admitted_work(), getattr(slot, "total_messages", 0)
        await run()
        _judge_turn(slot, work_id, start_total, stop_reason)

    await admission_entry.admitted_chat_turn(
        slot.key,
        admission,
        _judged(),
        still_live=lambda: slot_is_live(state, slot),
        on_ended=_ended,
    )


async def run_channel_turn(
    *,
    session_key: str,
    text: str,
    automation: bool,
    run: Callable[[], Coroutine[Any, Any, T]],
    refused: Callable[[ReasonCode | None], Coroutine[Any, Any, T]],
) -> T:
    """Run one channel transport turn through admission.

    A channel sender has no Shared Crew principal, so a person's message is
    charged to the bounded Unknown bucket and a monitor wake to automation.
    *refused* answers a turn that was refused or ended before it ran.
    """
    budget = _gate.AUTOMATION_PRINCIPAL.id if automation else Principal.unknown().id

    async def _inside() -> T:
        token = _IN_CHANNEL_TURN.set(True)
        try:
            return await run()
        finally:
            _IN_CHANNEL_TURN.reset(token)

    admission = await admit_turn(
        budget_key=budget,
        session_key=session_key,
        text=text,
        operation=OperationKind.CHANNEL_TURN,
    )
    if admission is None:
        return await _inside()
    if admission.rejected:
        return await refused(admission.reason)
    coro = _inside()
    try:
        return await admission.run(coro)
    except _gate.AdmissionEnded as ended:
        return await refused(ended.work.reason)


async def run_slack_message(
    handle_message: Callable[..., Coroutine[Any, Any, None]], args: Mapping[str, Any]
) -> None:
    """Run one native Slack message (``slack.handler.handle_message``) through admission.

    *args* are the handler's own keyword arguments; the handler re-enters with
    them inside the admitted run. A refused message gets a reply in its thread.
    """
    slack = args["slack"]
    channel = args["channel"]
    reply_ts = args.get("thread_ts") or args["msg_ts"]

    async def _refused(reason: ReasonCode | None) -> None:
        try:
            await slack.post_message(channel, capacity_note(reason), reply_ts)
        except Exception:  # noqa: BLE001 -- the note is best effort
            logger.debug("shared crew: slack capacity note failed", exc_info=True)

    await run_channel_turn(
        session_key=f"slack:{reply_ts}",
        text=str(args.get("text") or ""),
        automation=False,
        run=lambda: handle_message(**args),
        refused=_refused,
    )


__all__ = [
    "admission_active",
    "admit_turn",
    "admitted_work",
    "capacity_note",
    "in_channel_turn",
    "gate_turn",
    "initiated_by",
    "mark_admitted",
    "queued_initiator",
    "run_channel_turn",
    "run_slack_message",
    "turn_budget",
    "turn_failure",
]
