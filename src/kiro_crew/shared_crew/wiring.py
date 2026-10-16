"""Gateway startup wiring between the Shared Crew modules.

Each behaviour module exposes a hook and stays unaware of its neighbours. This
module connects them once, at gateway start:

* every capability source registers with :func:`capabilities.register_capability_source`,
  so ``GET /api/shared-crew/capabilities`` reports what is actually running;
* the admission scheduler becomes the People page's active/queued usage reader.

Every source answers from in-memory state and performs no I/O, as the
capability aggregator requires. :func:`install` is idempotent.
"""

from __future__ import annotations

import threading
from typing import Mapping

from kiro_crew.shared_crew import admission_gate, capabilities, people
from kiro_crew.shared_crew.capabilities import CapabilityContribution
from kiro_crew.shared_crew.contracts import CapabilityState, EnforcementLevel

SOURCE_IDENTITY = "shared_crew.identity"
SOURCE_PRESENCE = "shared_crew.presence"
SOURCE_ADMISSION = "shared_crew.admission"

_LOCK = threading.Lock()


def identity_source() -> CapabilityContribution:
    """Attribution is recorded on the dashboard chat and session-create paths.

    Channel transports, regenerate and other send sites keep their legacy rows
    without an author, so attribution is ``degraded`` rather than ``available``.
    No authoritative directory exists: observed people are not a membership list.
    """
    return CapabilityContribution(
        states={
            "attribution": CapabilityState.DEGRADED,
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


_SOURCES = (
    (
        SOURCE_IDENTITY,
        ("attribution", "authoritative_directory", "membership_link"),
        identity_source,
    ),
    (SOURCE_PRESENCE, ("presence",), presence_source),
    (SOURCE_ADMISSION, ("queueing", "containment", "containment_level"), admission_source),
)


def install() -> None:
    """Register every Shared Crew capability source and the usage reader once."""
    with _LOCK:
        registered = capabilities.registered_capability_sources()
        for name, fields, source in _SOURCES:
            if name not in registered:
                capabilities.register_capability_source(name, fields, source)
        people.register_usage_source(admission_usage)


def uninstall() -> None:
    """Remove what :func:`install` registered (gateway shutdown, tests)."""
    with _LOCK:
        for name, _fields, _source in _SOURCES:
            capabilities.unregister_capability_source(name)
        people.register_usage_source(None)


__all__ = [
    "admission_source",
    "admission_usage",
    "identity_source",
    "install",
    "presence_source",
    "uninstall",
]
