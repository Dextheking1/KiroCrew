"""``POST /api/chat/slots/{slot}/elicitation`` -- a person answers an MCP elicitation.

The card the chat runner rendered (``chat_turn/elicitation_prompt.py``) is
answered here. The body names the request (``request_id``), the ``action``
(``accept`` / ``decline`` / ``cancel``) and, for an accepted form, the
``content`` -- which is validated against the fields the server asked for BEFORE
the pending future is resolved, so a value the schema refuses comes back as a
400 the card can show, and the ask stays open for a corrected answer.

Only the dashboard owner's own browser may answer. An elicitation is the MCP
server's statement that a PERSON must decide; an agent on the internal secret or
an app token answering it would be exactly the automation the server asked to
exclude, so both are refused outright rather than judged by a grant.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web

from kiro_crew.acp.elicitation import (
    ELICITATION_ACCEPT,
    ELICITATION_ACTIONS,
    ELICITATION_MODE_URL,
    ElicitationContentError,
    ElicitationRequest,
    validate_form_content,
)
from kiro_crew.dashboard.handlers._shared_request_body import read_bounded_json
from kiro_crew.dashboard.slot_ownership import slot_not_found
from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: The row role the chat runner appends for an elicitation card.
ELICITATION_ROW_ROLE = "elicitation"


def _bad_request(code: str, error: str) -> web.Response:
    return web.json_response({"error": error, "code": code}, status=400)


def _elicitation_row(slot: Any, request_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The slot's elicitation row carrying *request_id*, with its parsed meta."""
    for message in reversed(slot.messages):
        if message.get("role") != ELICITATION_ROW_ROLE:
            continue
        try:
            meta = json.loads(message.get("cls") or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(meta, dict) and meta.get("request_id") == request_id:
            return message, meta
    return None


async def api_chat_slot_elicitation(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/elicitation -- resolve a pending MCP elicitation."""
    state: DashboardState = request.app["state"]
    # The dashboard owner's browser session carries a PRESENT, empty ``app``
    # claim; an internal-secret caller carries ``internal_auth`` instead and an
    # app token carries its name. Only the first is a person at the card.
    if request.get("internal_auth") is True or request.get("app", None) != "":
        return web.json_response(
            {"error": "only the dashboard user can answer an elicitation", "code": "forbidden"},
            status=403,
        )
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None
    request_id = body.get("request_id")
    action = body.get("action")
    if not isinstance(request_id, str) or not request_id:
        return _bad_request("invalid_request_id", "request_id is required")
    if action not in ELICITATION_ACTIONS:
        return _bad_request("invalid_action", "action must be accept, decline or cancel")
    fut = slot._approval_futures.get(request_id)
    found = _elicitation_row(slot, request_id)
    if fut is None or fut.done() or found is None:
        return web.json_response(
            {"error": "no pending elicitation", "code": "elicitation_not_pending"}, status=404
        )
    row, meta = found
    answer: dict[str, Any] | None = None
    if action == ELICITATION_ACCEPT and meta.get("mode") != ELICITATION_MODE_URL:
        try:
            answer = validate_form_content(ElicitationRequest.from_dict(meta), body.get("content"))
        except ElicitationContentError as exc:
            return _bad_request("invalid_content", str(exc))
        # The runner reads the validated answer back off the row once the
        # future resolves; the row is the one place both sides can see.
        meta["answer"] = answer
        row["cls"] = json.dumps(meta)
    logger.info(
        "elicitation answered from the dashboard [slot=%s action=%s req=%s]",
        name,
        action,
        request_id,
    )
    fut.set_result(action)
    return web.json_response({"ok": True, "action": action})
