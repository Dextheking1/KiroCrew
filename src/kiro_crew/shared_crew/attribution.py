"""Session creator and message author stamping.

A slot carries its :class:`SessionAttribution` on the attribute
:data:`SLOT_ATTRIBUTION_ATTR`. Three states:

* attribute absent or ``None``: a slot this process created and nobody has
  stamped yet;
* ``LEGACY_MISSING``: a slot read back from a metadata line with no
  ``attribution`` key. It stays legacy forever: it is never written back and no
  later stamp replaces it, so a legacy session never gains a fabricated
  creator;
* ``RECORDED`` or ``UNATTRIBUTED``: stamped once, written to the metadata line by
  ``metadata_codec``'s ``attribution`` row, and never replaced
  (:func:`~kiro_crew.shared_crew.contracts.keep_creator`).

The metadata line is read back verbatim on every restore, and every other save
writer carries the key unchanged because no writer lists it as owned.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

from kiro_crew.shared_crew.codec import SCHEMA_VERSION
from kiro_crew.shared_crew.contracts import (
    MESSAGE_AUTHOR_META_KEY,
    ActorContext,
    AttributionAvailability,
    Principal,
    PrincipalKind,
    SessionAttribution,
    keep_creator,
    principal_or_unknown,
)

logger = logging.getLogger(__name__)

SLOT_ATTRIBUTION_ATTR = "_attribution"


def slot_attribution(slot: Any) -> SessionAttribution | None:
    """The slot's attribution, or ``None`` when it was never stamped or read."""
    value = getattr(slot, SLOT_ATTRIBUTION_ATTR, None)
    return value if isinstance(value, SessionAttribution) else None


def _set(slot: Any, value: SessionAttribution) -> bool:
    try:
        setattr(slot, SLOT_ATTRIBUTION_ATTR, value)
    except AttributeError:
        # A slot class that does not declare the attribute cannot hold a
        # creator; the session then persists with no attribution and reads back
        # as legacy, which loses a label and grants nothing.
        logger.debug("slot %r cannot hold attribution", getattr(slot, "key", "?"))
        return False
    return True


def attribution_for(actor: ActorContext, *, now: float | None = None) -> SessionAttribution:
    """The attribution a session created by *actor* records."""
    principal = actor.principal
    availability = (
        AttributionAvailability.UNATTRIBUTED
        if principal.is_unknown
        else AttributionAvailability.RECORDED
    )
    return SessionAttribution(
        creator=principal,
        availability=availability,
        created_at=time.time() if now is None else now,
    )


def stamp_creator(
    slot: Any, actor: ActorContext, *, now: float | None = None
) -> SessionAttribution:
    """Record *actor* as the slot's creator unless the slot already has one.

    Returns the attribution the slot holds afterwards. A legacy slot keeps its
    legacy value and a stamped slot keeps its first creator.
    """
    existing = slot_attribution(slot)
    kept = keep_creator(existing, attribution_for(actor, now=now))
    if kept is not existing:
        _set(slot, kept)
    return kept


def stamp_creator_if_fresh(
    slot: Any, actor: ActorContext, *, prior_user_turns: int, now: float | None = None
) -> SessionAttribution | None:
    """Stamp the creator on the first accepted message of a never-stamped slot.

    For a slot created by a path that does not stamp at creation. The sender of
    the first user message is who started the conversation; a slot that
    already holds user turns was started by someone this process did not see,
    so it is left unstamped and reads back as legacy.
    """
    existing = slot_attribution(slot)
    if existing is not None:
        return existing
    if prior_user_turns > 0:
        return None
    return stamp_creator(slot, actor, now=now)


def restore_from_metadata(slot: Any, meta: Mapping[str, Any]) -> SessionAttribution:
    """Read the stored attribution onto a slot hydrated from *meta*."""
    value = SessionAttribution.from_metadata(meta)
    _set(slot, value)
    return value


def metadata_value(slot: Any) -> dict[str, Any] | None:
    """The ``attribution`` value a save writes, or ``None`` to write nothing.

    A legacy slot writes nothing, so the line keeps its absence and no marker
    is invented for it.
    """
    value = slot_attribution(slot)
    if value is None or value.availability is AttributionAvailability.LEGACY_MISSING:
        return None
    return value.to_dict()


def public_attribution(value: SessionAttribution) -> dict[str, Any]:
    """The browser-facing attribution: the creator without issuer or subject."""
    return {
        "schema_version": SCHEMA_VERSION,
        "availability": value.availability.value,
        "creator": value.creator.to_public_dict(),
        "created_at": value.created_at,
    }


def public_attribution_of_slot(slot: Any) -> dict[str, Any]:
    value = slot_attribution(slot)
    return public_attribution(value if value is not None else SessionAttribution.legacy())


def public_attribution_from_meta(meta: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(meta, Mapping):
        return public_attribution(SessionAttribution.legacy())
    return public_attribution(SessionAttribution.from_metadata(meta))


def creator_of_public(raw: object) -> Principal:
    """The creator principal named by a public attribution value."""
    if not isinstance(raw, Mapping):
        return Principal.unknown()
    return principal_or_unknown(raw.get("creator"))


def author_meta(actor: ActorContext) -> dict[str, Any]:
    """The value stored under ``meta[MESSAGE_AUTHOR_META_KEY]`` for a message.

    The public shape: a message row reaches every collaborator's browser, so it
    carries the id and label only.
    """
    return actor.principal.to_public_dict()


def stamp_message_author(meta: dict[str, Any], actor: ActorContext) -> dict[str, Any]:
    """Set the author on a message ``meta`` dict the server is about to accept.

    Overwrites any value already present: a client never names the author, so
    a client-supplied ``author`` in the incoming meta is discarded here.
    """
    meta[MESSAGE_AUTHOR_META_KEY] = author_meta(actor)
    return meta


def message_author(meta: Mapping[str, Any] | None) -> Principal:
    """The author of a stored message; a message written before authors is Unknown."""
    if not isinstance(meta, Mapping):
        return Principal.unknown()
    return principal_or_unknown(meta.get(MESSAGE_AUTHOR_META_KEY))


def is_person(principal: Principal) -> bool:
    return principal.kind is PrincipalKind.HUMAN
