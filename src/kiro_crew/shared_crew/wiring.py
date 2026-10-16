"""Gateway startup wiring between the Shared Crew modules.

Each behaviour module exposes a hook and stays unaware of its neighbours. This
module connects them once, at gateway start:

* every capability source registers with :func:`capabilities.register_capability_source`,
  so ``GET /api/shared-crew/capabilities`` reports what is actually running;
* the admission scheduler becomes the People page's active/queued usage reader;
* every accepted-work write is pushed to dashboard tabs as a ``shared_crew.work``
  frame (:func:`work_broadcaster`).

Every source answers from in-memory state and performs no I/O, as the
capability aggregator requires. :func:`install` is idempotent.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Mapping

from kiro_crew.shared_crew import admission_gate, capabilities, people, work_store
from kiro_crew.shared_crew.admission import AcceptedWork
from kiro_crew.shared_crew.capabilities import CapabilityContribution
from kiro_crew.shared_crew.collab import work_frame
from kiro_crew.shared_crew.contracts import CapabilityState, EnforcementLevel

SOURCE_IDENTITY = "shared_crew.identity"
SOURCE_PRESENCE = "shared_crew.presence"
SOURCE_ADMISSION = "shared_crew.admission"

_LOCK = threading.Lock()


def identity_source() -> CapabilityContribution:
    """Attribution is recorded on every send site.

    A user row written by an authorized request's handler names that request's
    server-derived author (``attribution.with_request_author``); the queue drain
    keeps the queued entry's stamp; channel rows name the bounded Unknown
    principal and automation rows the automation principal. No authoritative
    directory exists: observed people are not a membership list.
    """
    return CapabilityContribution(
        states={
            "attribution": CapabilityState.AVAILABLE,
            "authoritative_directory": CapabilityState.UNAVAILABLE,
        },
        membership_link="",
    )


def presence_source() -> CapabilityContribution:
    """Presence runs in the gateway's WebSocket layer for every dashboard user."""
    return CapabilityContribution(states={"presence": CapabilityState.AVAILABLE})


def admission_source() -> CapabilityContribution:
    """Queueing and containment as the admission service last measured them."""
    service = admission_gate.installed_service()
    if service is None or not service.enabled:
        return CapabilityContribution(
            states={
                "queueing": CapabilityState.DISABLED,
                "containment": CapabilityState.DISABLED,
            },
            containment_level=EnforcementLevel.NONE,
        )
    report = service.cached_containment_report()
    if report is None:
        return CapabilityContribution(
            states={
                "queueing": CapabilityState.AVAILABLE,
                "containment": CapabilityState.UNAVAILABLE,
            },
            containment_level=EnforcementLevel.NONE,
        )
    return CapabilityContribution(
        states={
            "queueing": CapabilityState.AVAILABLE,
            "containment": report.capability_state(),
        },
        containment_level=report.level,
    )


def admission_usage() -> Mapping[str, Mapping[str, int]]:
    """``principal id -> {"active", "queued"}`` from the fair scheduler.

    Raises when admission is off, so the People page reports usage as
    unavailable rather than as zero.
    """
    service = admission_gate.installed_service()
    if service is None or not service.enabled:
        raise LookupError("fair admission is off")
    snap = service.scheduler.snapshot()
    principals = snap.get("principals")
    out: dict[str, dict[str, int]] = {}
    if isinstance(principals, Mapping):
        for key, row in principals.items():
            if isinstance(row, Mapping):
                out[str(key)] = {
                    "active": int(row.get("root_active", 0)) + int(row.get("child_active", 0)),
                    "queued": int(row.get("queued", 0)),
                }
    return out


def work_broadcaster(state: Any) -> Callable[[AcceptedWork], None]:
    """A work-store listener that pushes each record to the dashboard's sockets.

    The frame names the tab showing the session (``dashboard_slot_key``) so the
    work list, which reads by tab key, receives it.
    """
    from kiro_crew.dashboard.chat_utils import dashboard_slot_key

    def broadcast(work: AcceptedWork) -> None:
        try:
            slot_key = dashboard_slot_key(work.session_key)
        except Exception:  # noqa: BLE001 -- an unresolvable key still sends the frame
            slot_key = ""
        frame = work_frame(work, slot_key=slot_key)
        state.broadcast_ws(frame["type"], frame)

    return broadcast


_SOURCES = (
    (
        SOURCE_IDENTITY,
        ("attribution", "authoritative_directory", "membership_link"),
        identity_source,
    ),
    (SOURCE_PRESENCE, ("presence",), presence_source),
    (SOURCE_ADMISSION, ("queueing", "containment", "containment_level"), admission_source),
)


def install(state: Any = None) -> None:
    """Register every Shared Crew capability source and the usage reader once.

    With *state* (the gateway's dashboard state), accepted-work writes are also
    pushed to its sockets.
    """
    with _LOCK:
        registered = capabilities.registered_capability_sources()
        for name, fields, source in _SOURCES:
            if name not in registered:
                capabilities.register_capability_source(name, fields, source)
        people.register_usage_source(admission_usage)
        if state is not None:
            work_store.set_change_listener(work_broadcaster(state))


def uninstall() -> None:
    """Remove what :func:`install` registered (gateway shutdown, tests)."""
    with _LOCK:
        for name, _fields, _source in _SOURCES:
            capabilities.unregister_capability_source(name)
        people.register_usage_source(None)
        work_store.set_change_listener(None)


__all__ = [
    "admission_source",
    "admission_usage",
    "identity_source",
    "install",
    "presence_source",
    "uninstall",
    "work_broadcaster",
]
