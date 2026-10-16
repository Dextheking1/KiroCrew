"""The dashboard gateway's side of fair admission: process stop, queue entries, notes.

The admission gate holds no dashboard state. The gateway installs three hooks
(:class:`kiro_crew.shared_crew.admission_gate.HostHooks`) so that:

* work the gateway's own stop interrupts is recorded ``cancelled``, not
  ``completed`` (the process-shutdown flag ``chat_turn.turn_marker`` reads);
* a message parked behind a busy session expires only once its queue entry is
  gone (a person removed the card), never while it still waits its turn;
* a chat turn that reconciliation ended after a restart says so in its own
  session, so a lost reply is never silent.

Every hook runs on the event loop and reads memory only.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from kiro_crew.shared_crew import admission_gate as _gate
from kiro_crew.shared_crew.admission import AcceptedWork, OperationKind, OperationState
from kiro_crew.shared_crew.admission_entry import QUEUED_WORK_META_KEY

logger = logging.getLogger(__name__)

_ERROR_CLS = "msg msg-err"
#: Session history keys this process has seen recorded on disk (bounded).
_RECORDED: set[str] = set()
_RECORDED_MAX = 20000

#: The note a session shows for its turn that a restart ended.
NOTE_UNKNOWN = "⚠ The Crew restarted while this message was running; its reply may be incomplete."
NOTE_NOT_STARTED = "⚠ The Crew restarted before this message started. Send it again to run it."


def shutdown_probe(state: Any) -> Callable[[], bool]:
    def stopping() -> bool:
        from kiro_crew.dashboard.chat_turn.turn_marker import _gateway_shutdown_requested

        return bool(_gateway_shutdown_requested(state))

    return stopping


def parked_probe(state: Any) -> Callable[[str], bool]:
    def present(work_id: str) -> bool:
        slots = getattr(state, "_slots", None)
        for slot in list(slots.values()) if isinstance(slots, dict) else ():
            for entry in getattr(slot, "_queue", None) or ():
                meta = entry.get("meta") if isinstance(entry, dict) else None
                if isinstance(meta, dict) and meta.get(QUEUED_WORK_META_KEY) == work_id:
                    return True
        return False

    return present


def _slot_for(state: Any, session_key: str) -> Any:
    from kiro_crew.dashboard.chat_utils import effective_session_key

    slots = getattr(state, "_slots", None)
    for slot in list(slots.values()) if isinstance(slots, dict) else ():
        try:
            if slot.key == session_key or effective_session_key(slot) == session_key:
                return slot
        except Exception:  # noqa: BLE001 -- a slot that cannot be keyed is skipped
            continue
    return None


def reconcile_note(state: Any) -> Callable[[AcceptedWork], None]:
    def note(work: AcceptedWork) -> None:
        if work.operation is not OperationKind.CHAT_TURN:
            return
        if work.extra.get(_gate.PARKED_KEY):
            # Its queue entry was saved with the session and runs again from there.
            return
        if work.state is OperationState.UNKNOWN_OUTCOME:
            text = NOTE_UNKNOWN
        elif work.state is OperationState.CANCELLED and work.reason is None:
            text = NOTE_NOT_STARTED
        else:
            return
        slot = _slot_for(state, work.session_key)
        if slot is None:
            return
        try:
            slot.append("error", text, _ERROR_CLS)
        except Exception:  # noqa: BLE001 -- the note is best effort
            logger.debug("shared crew: restart note failed", exc_info=True)

    return note


async def ensure_slot_recorded(state: Any, slot: Any) -> bool:
    """Write a never-saved session's metadata line before its work is accepted.

    Accepted work is durable at once while a new session's first save is
    deferred; writing the line first means a crash in between cannot leave a
    work record naming a session that is gone. A session that already has a
    line is left as it is.
    Off-loop write; True when the session has a line afterwards.
    """
    log = getattr(state, "conversation_log", None)
    if log is None:
        return False
    from kiro_crew.dashboard.chat_utils import drained_to_thread, slot_history_key
    from kiro_crew.dashboard.slot_persistence import metadata_codec

    try:
        history_key = slot_history_key(slot)
        if history_key in _RECORDED:
            return True
        line = {
            "_type": "metadata",
            **metadata_codec.encode(
                slot,
                folds=metadata_codec.SaveFolds(
                    memory_mode=getattr(slot, "memory_mode", "persistent"),
                    created_at=slot.created_at,
                    tab_id=getattr(slot, "_tab_id", None),
                ),
            ),
        }
        await drained_to_thread(log.update_metadata_if, history_key, line, lambda meta: not meta)
    except Exception:  # noqa: BLE001 -- the send still runs; the periodic save retries
        logger.warning("shared crew: could not record session %s", slot.key, exc_info=True)
        return False
    _RECORDED.add(history_key)
    while len(_RECORDED) > _RECORDED_MAX:
        _RECORDED.pop()
    return True


def install(state: Any) -> None:
    """Install the gateway's hooks on every admission service it builds."""
    _gate.install_host_hooks(
        _gate.HostHooks(
            shutting_down=shutdown_probe(state),
            parked_present=parked_probe(state),
            reconciled=reconcile_note(state),
        )
    )


def uninstall() -> None:
    _gate.install_host_hooks(None)


__all__ = [
    "ensure_slot_recorded",
    "NOTE_NOT_STARTED",
    "NOTE_UNKNOWN",
    "install",
    "parked_probe",
    "reconcile_note",
    "shutdown_probe",
    "uninstall",
]
