"""``/api/shared-crew/capacity``, ``/api/shared-crew/work`` and work cancellation.

Read and write access follows the existing dashboard auth: these handlers run
behind ``token_auth_middleware`` like every other ``/api`` route and add no
authorization of their own, because every authorized collaborator has equal
rights. Cancellation is compare-and-set on the record's ``revision``; a stale
or illegal request answers 409 with the server's ``current`` record.

Shapes:

``GET /api/shared-crew/capacity``
    ``CapacityResponse.to_dict()`` for the viewer's principal, plus
    ``effective`` (every enforced limit with ``auto`` resolved), ``containment``
    (``ContainmentReport.to_dict()``) and ``fairness`` (algorithm, quantum and
    the starvation bound).
``GET /api/shared-crew/work?session_key=<key>``
    ``{"items": [AcceptedWork.to_public_dict(), ...]}`` for that session, or for
    the viewer's own work when ``session_key`` is absent.
``POST /api/shared-crew/work/{work_id}/cancel`` ``{"expected_revision": int}``
    200 with the next public record; 404 ``not_found``; 409 ``stale_revision``
    or ``invalid_transition`` with ``current``.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from kiro_crew.shared_crew import admission_gate
from kiro_crew.shared_crew.admission import (
    InvalidTransition,
    ReasonCode,
    StaleRevision,
    error_body,
    http_status,
)
from kiro_crew.shared_crew.admission_gate import coded_json_response
from kiro_crew.shared_crew.scheduler import DEFAULT_QUANTUM

CAPACITY_PATH = "/api/shared-crew/capacity"
WORK_PATH = "/api/shared-crew/work"
CANCEL_PATH = "/api/shared-crew/work/{work_id}/cancel"

FAIRNESS_DESCRIPTION: dict[str, Any] = {
    "algorithm": "deficit_round_robin",
    "quantum": DEFAULT_QUANTUM,
    "max_weight": 1,
    "within_session": "fifo",
    "starvation_bound": (
        "a continuously eligible principal is admitted within (P - 1) * (quantum + "
        "max_weight - 1) admissions of others, P = principals with eligible queued work"
    ),
}


async def api_capacity(request: web.Request) -> web.Response:
    service = await admission_gate.current_service()
    budget = admission_gate.actor_budget(request)
    body = service.capacity(budget).to_dict()
    body["effective"] = service.limits.to_dict()
    report = await service.containment_report() if service.enabled else None
    body["containment"] = None if report is None else report.to_dict()
    body["fairness"] = FAIRNESS_DESCRIPTION
    return web.json_response(body)


async def api_work(request: web.Request) -> web.Response:
    service = await admission_gate.current_service()
    session_key = request.query.get("session_key") or None
    if session_key is not None and len(session_key) > 512:
        return web.json_response(
            {"error": "session_key too long", "code": "invalid_filter"}, status=400
        )
    budget = None if session_key else admission_gate.actor_budget(request)
    items = await service.list_work(session_key=session_key, budget_key=budget)
    return web.json_response({"items": [w.to_public_dict() for w in items]})


async def api_work_cancel(request: web.Request) -> web.Response:
    service = await admission_gate.current_service()
    work_id = request.match_info.get("work_id", "")
    try:
        body = await request.json()
    except Exception:
        body = None
    expected = body.get("expected_revision") if isinstance(body, dict) else None
    if not isinstance(expected, int) or isinstance(expected, bool):
        return web.json_response(
            {"error": "expected_revision must be an integer", "code": "invalid_request"},
            status=400,
        )
    try:
        work = await service.cancel(work_id, expected_revision=expected)
    except KeyError:
        reason = ReasonCode.NOT_FOUND
        return coded_json_response(error_body(reason, "no such work"), status=http_status(reason))
    except (StaleRevision, InvalidTransition) as exc:
        reason = (
            ReasonCode.STALE_REVISION
            if isinstance(exc, StaleRevision)
            else ReasonCode.INVALID_TRANSITION
        )
        current = await service.current_work(work_id)
        return coded_json_response(
            error_body(reason, str(exc), current=current), status=http_status(reason)
        )
    return web.json_response(work.to_public_dict())


def register(app: web.Application) -> None:
    """Register the Shared Crew admission routes on *app* (wired by integration)."""
    app.router.add_get(CAPACITY_PATH, api_capacity)
    app.router.add_get(WORK_PATH, api_work)
    app.router.add_post(CANCEL_PATH, api_work_cancel)


__all__ = ["CANCEL_PATH", "CAPACITY_PATH", "WORK_PATH", "register"]
