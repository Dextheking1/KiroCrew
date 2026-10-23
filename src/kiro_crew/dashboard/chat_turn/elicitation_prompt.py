"""An MCP server's elicitation a dashboard turn puts to its user: the card, the wait
and the answer sent back.

The server, not Kiro Crew, declared that a person must confirm or fill something
in before the tool call it gates may go on. So no trust tier, hook or grant
answers here: a person does, or the window runs out and the ask is cancelled.
The row's ``request_id`` is registered as a pending approval on the slot (the
same registry the permission card uses), so the Board's Blocked lane, Continue's
409 and a Stop all treat it as the open question it is; the answer arrives
through ``POST /api/chat/slots/{slot}/elicitation``, which validates a form's
content against the requested fields before it resolves the future.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from kiro_crew.acp.elicitation import (
    ELICITATION_ACCEPT,
    ELICITATION_CANCEL,
    ELICITATION_DECLINE,
    ELICITATION_MODE_URL,
    ElicitationContentError,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        logger,
        redact_credentials,
        redact_exfiltration_urls,
        tool_approval_timeout_secs,
    )
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn

#: The elicitation answer ``read_elicitation_answer`` reads off a resolved future, and
#: what each of the permission registry's own outcomes means for an elicitation: a
#: Stop or a reject button is a person saying no; the generic approve has no form
#: content to send, so it accepts only a URL ask.
_FUTURE_DECLINES = frozenset({"rejected", "rejected_once"})
_FUTURE_APPROVES = frozenset({"approved", "approved_trust_reads"})


def read_elicitation_answer(outcome: str, meta: dict[str, Any]) -> tuple[str, dict | None]:
    """The elicitation action and content a resolved future's *outcome* stands for.

    The elicitation endpoint resolves the future with the action itself and
    leaves the validated content on the row (``meta["answer"]``). Any other
    resolver speaks the permission registry's vocabulary and is mapped: a
    rejection declines, an approval accepts a URL ask (there is nothing to fill
    in) and CANCELS a form ask, since accepting a form without its answers would
    send the server content the person never gave.
    """
    if outcome == ELICITATION_ACCEPT:
        answer = meta.get("answer")
        return ELICITATION_ACCEPT, answer if isinstance(answer, dict) else None
    if outcome == ELICITATION_DECLINE or outcome in _FUTURE_DECLINES:
        return ELICITATION_DECLINE, None
    if outcome in _FUTURE_APPROVES and meta.get("mode") == ELICITATION_MODE_URL:
        return ELICITATION_ACCEPT, None
    return ELICITATION_CANCEL, None


async def _on_elicitation_request(turn: _ChatTurn, event) -> None:
    """Put the elicitation to the user: the card, the wait and what the answer does."""
    client = turn.client
    slot = turn.slot
    state = turn.state
    request_id = str(event.request_id)
    payload = dict(event.elicitation or {})
    # The message and every field label are server-authored and rendered to
    # the person: the same display redaction every backend-authored title gets.
    safe_message, _ = redact_exfiltration_urls(str(payload.get("message") or event.title or ""))
    safe_message, _ = redact_credentials(safe_message)
    meta: dict[str, Any] = {
        **payload,
        "request_id": request_id,
        "tool_call_id": event.tool_call_id or "",
        "message": safe_message,
    }
    # Pre-declined when there is no window to wait in, like a permission row.
    if tool_approval_timeout_secs() <= 0:
        meta["resolved"] = ELICITATION_CANCEL
    row = slot.append("elicitation", safe_message, json.dumps(meta))
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str] = loop.create_future()
    slot.register_approval(request_id, fut, row)
    # After registering, so the slots snapshot already reads pending_approval.
    state.push_slots_update()
    window = min(state.approval_timeout_for(slot), tool_approval_timeout_secs())
    action = ELICITATION_CANCEL
    answer: dict | None = None
    try:
        if fut.done():
            action, answer = read_elicitation_answer(fut.result(), meta)
        elif window <= 0:
            logger.warning(
                "Cancelling MCP elicitation %r without waiting: no turn budget left",
                request_id,
            )
        else:
            outcome = await asyncio.wait_for(fut, timeout=window)
            # The endpoint wrote the validated answer onto the row; re-read it.
            try:
                meta = json.loads(row.get("cls") or "{}")
            except (json.JSONDecodeError, TypeError):
                pass
            action, answer = read_elicitation_answer(outcome, meta)
    except asyncio.TimeoutError:
        logger.warning("MCP elicitation %r unanswered for %.0fs; cancelling it", request_id, window)
    finally:
        owns = slot.unregister_approval(request_id, fut)
        try:
            await client.answer_elicitation(event.request_id, action, answer)
        except ElicitationContentError:
            # The endpoint validated before resolving, so this is a row someone
            # else wrote: decline rather than send content the schema refuses.
            logger.warning("MCP elicitation %r answer does not fit its form; declining", request_id)
            action, answer = ELICITATION_DECLINE, None
            await client.answer_elicitation(event.request_id, action, None)
        except Exception:
            logger.warning("Failed to answer MCP elicitation %r", request_id, exc_info=True)
        if owns:
            slot.mark_elicitation_resolved(request_id, action, answer)
            slot._dirty = True
            state.broadcast_ws(
                "elicitation_resolved",
                {"id": request_id, "action": action, "slot": slot.key},
            )
            state.push_slots_update()
