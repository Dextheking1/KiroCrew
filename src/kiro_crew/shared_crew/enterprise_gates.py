"""Production call sites for the enterprise grant and budget gates.

``enterprise.GrantGate`` and ``enterprise.BudgetGate`` decide; this module is
where the gateway asks them. :func:`enterprise_gate_middleware` runs inner to
token auth, after the request's actor is published, and checks two things:

* **shared access.** A request from a person reaching the Crew through a shared
  path (a tailnet peer or an enterprise identity) passes ``GrantGate.decide``
  once a grant authority is composed. The local operator, Crew automation and
  apps are not shared access and are not asked.
* **inference.** A request that starts new model work (:data:`CHARGEABLE_ROUTES`)
  passes ``BudgetGate.check`` once a budget provider is composed. Model work
  with no request (cron schedules, channel turns) asks
  :func:`check_background_inference` at its entry point.

With the public default seam neither gate is composed, so both checks return
``not_configured`` and every request proceeds exactly as before: the disabled
default is a no-op, and the capabilities report shows each concern unavailable.

A refusal is a 403 JSON body naming a machine-readable ``reason``. The gates
are rebuilt only when the composed providers change, so a grant cached by one
request serves the next.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from aiohttp import web

from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    ActorContext,
    Principal,
    PrincipalKind,
    Provenance,
)
from kiro_crew.shared_crew.enterprise import (
    BudgetGate,
    EnterpriseSeams,
    GrantGate,
    OperationKind,
    resolve_enterprise,
)

logger = logging.getLogger(__name__)

REASON_NOT_CONFIGURED = "not_configured"
REASON_ALLOWED = "allowed"
ERROR_SHARED_ACCESS_REFUSED = "shared_access_refused"
ERROR_INFERENCE_REFUSED = "inference_refused"

#: Provenance that marks a person reaching the Crew through a shared path.
SHARED_PROVENANCE = frozenset(
    {Provenance.TAILSCALE_SERVE, Provenance.TAILSCALE_DIRECT, Provenance.ENTERPRISE}
)

#: ``(pattern, operation)`` for every POST that starts new model work over HTTP.
CHARGEABLE_ROUTES: tuple[tuple[re.Pattern[str], OperationKind], ...] = tuple(
    (re.compile(pattern), op)
    for pattern, op in (
        (r"\A/api/chat\Z", OperationKind.NEW_TURN),
        (r"\A/v1/chat/completions\Z", OperationKind.NEW_TURN),
        (r"\A/api/chat/slots/[^/]+/continue\Z", OperationKind.NEW_TURN),
        (r"\A/api/chat/slots/[^/]+/side/turn\Z", OperationKind.NEW_TURN),
        (r"\A/api/chat/threads/[^/]+/reply\Z", OperationKind.NEW_TURN),
        (r"\A/api/members/[^/]+/thread\Z", OperationKind.NEW_TURN),
        (r"\A/api/chat/slots/[^/]+/regenerate\Z", OperationKind.RETRY),
        (r"\A/api/chat/slots/[^/]+/edit-resend\Z", OperationKind.RETRY),
        (r"\A/api/taskrunner/[^/]+/retry\Z", OperationKind.RETRY),
        (r"\A/api/spawn\Z", OperationKind.SPAWN),
        (r"\A/api/spawn/adaptive\Z", OperationKind.SPAWN),
        (r"\A/api/spawn/[^/]+/continue\Z", OperationKind.SPAWN),
        (r"\A/api/crons/[^/]+/run\Z", OperationKind.AUTOMATION_RUN),
        (r"\A/api/taskrunner/plan\Z", OperationKind.AUTOMATION_RUN),
        (r"\A/api/taskrunner/[^/]+/execute\Z", OperationKind.AUTOMATION_RUN),
    )
)


def chargeable_operation(method: str, path: str) -> Optional[OperationKind]:
    """The operation a request starts, or ``None`` when it starts no model work."""
    if method != "POST":
        return None
    for pattern, op in CHARGEABLE_ROUTES:
        if pattern.match(path):
            return op
    return None


@dataclass(frozen=True)
class GateVerdict:
    allowed: bool
    reason: str
    #: False when no provider is composed, so nothing was enforced.
    enforced: bool = False


_NOT_CONFIGURED = GateVerdict(True, REASON_NOT_CONFIGURED)


def _crew_id_of(seams: EnterpriseSeams) -> str:
    binding = None
    if seams.crew_binding is not None:
        try:
            binding = seams.crew_binding.current_binding()
        except Exception:  # noqa: BLE001 -- an unreadable binding names no Crew
            logger.info("crew binding unreadable", exc_info=True)
    if binding is not None:
        return binding.crew.crew_id
    return seams.deployment.deployment_ref or "local"


class EnterpriseGates:
    """The grant and budget gates built from one set of checked providers."""

    def __init__(self, seams: EnterpriseSeams) -> None:
        self.seams = seams
        self._grants: Optional[GrantGate] = None
        self._budget: Optional[BudgetGate] = None
        if seams.grant_authority is not None:
            self._grants = GrantGate(seams.grant_authority, crew_id=_crew_id_of(seams))
        if seams.inference_budget is not None:
            self._budget = BudgetGate(seams.inference_budget, seams.deployment)

    @property
    def configured(self) -> bool:
        return self._grants is not None or self._budget is not None

    def check_shared_access(self, principal: Principal, now: float) -> GateVerdict:
        """Whether *principal* may use this Crew through a shared path."""
        if self._grants is None or not is_shared_access(principal):
            return _NOT_CONFIGURED
        decision = self._grants.decide(principal, now)
        return GateVerdict(decision.allowed, decision.reason.value, enforced=True)

    def check_inference(self, operation: OperationKind) -> GateVerdict:
        """Whether *operation* may start new model work under the budget."""
        if self._budget is None:
            return _NOT_CONFIGURED
        decision = self._budget.check(operation)
        return GateVerdict(decision.allowed, decision.reason.value, enforced=True)


def check_background_inference(
    operation: OperationKind,
    gates: Callable[[], EnterpriseGates] | None = None,
) -> GateVerdict:
    """``BudgetGate.check`` for model work that does not arrive over HTTP.

    Cron schedules and channel turns start model work with no request the
    middleware could see, so their entry points ask here. With no budget
    provider composed this is ``not_configured`` and the work proceeds; an
    unreadable seam composes nothing, exactly as in the middleware.
    """
    try:
        current = (gates or current_gates)()
    except Exception:  # noqa: BLE001 -- an unreadable seam composes nothing
        logger.warning("shared crew enterprise gates unavailable", exc_info=True)
        return _NOT_CONFIGURED
    return current.check_inference(operation)


async def check_background_inference_off_loop(operation: OperationKind) -> GateVerdict:
    """:func:`check_background_inference` with the seam read (config, files) off the loop."""
    return await asyncio.to_thread(check_background_inference, operation)


def is_shared_access(principal: Principal) -> bool:
    """A person reaching the Crew through a tailnet or enterprise identity."""
    return principal.kind is PrincipalKind.HUMAN and principal.provenance in SHARED_PROVENANCE


_LOCK = threading.Lock()
_CACHED: Optional[tuple[tuple[int, int, int], EnterpriseGates]] = None


def current_gates(resolver: Callable[[], EnterpriseSeams] = resolve_enterprise) -> EnterpriseGates:
    """The gates for the composed providers, reused while they stay the same."""
    global _CACHED
    seams = resolver()
    key = (id(seams.grant_authority), id(seams.inference_budget), id(seams.crew_binding))
    with _LOCK:
        if _CACHED is not None and _CACHED[0] == key:
            return _CACHED[1]
        gates = EnterpriseGates(seams)
        _CACHED = (key, gates)
        return gates


def _reset_for_tests() -> None:
    global _CACHED
    with _LOCK:
        _CACHED = None


def _refusal(error: str, reason: str) -> web.Response:
    return web.json_response({"error": error, "reason": reason}, status=403)


def _actor_principal(request: web.Request) -> Optional[Principal]:
    actor = request.get(ACTOR_REQUEST_KEY)
    return actor.principal if isinstance(actor, ActorContext) else None


def make_enterprise_gate_middleware(
    gates: Callable[[], EnterpriseGates] = current_gates,
    clock: Callable[[], float] = time.time,
) -> Any:
    """The middleware that asks both gates; *gates* and *clock* are seams for tests."""

    @web.middleware
    async def enterprise_gate_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
        try:
            current = gates()
        except Exception:  # noqa: BLE001 -- an unreadable seam composes nothing
            logger.warning("shared crew enterprise gates unavailable", exc_info=True)
            return await handler(request)
        if not current.configured:
            return await handler(request)
        principal = _actor_principal(request)
        if principal is not None:
            verdict = current.check_shared_access(principal, clock())
            if not verdict.allowed:
                return _refusal(ERROR_SHARED_ACCESS_REFUSED, verdict.reason)
        operation = chargeable_operation(request.method, request.path)
        if operation is not None:
            verdict = current.check_inference(operation)
            if not verdict.allowed:
                return _refusal(ERROR_INFERENCE_REFUSED, verdict.reason)
        return await handler(request)

    return enterprise_gate_middleware


def install(app: web.Application) -> None:
    """Append the gate middleware, inner to token auth and the slot checkpoint."""
    app.middlewares.append(make_enterprise_gate_middleware())


__all__ = [
    "CHARGEABLE_ROUTES",
    "check_background_inference",
    "check_background_inference_off_loop",
    "EnterpriseGates",
    "GateVerdict",
    "chargeable_operation",
    "current_gates",
    "install",
    "is_shared_access",
    "make_enterprise_gate_middleware",
]
