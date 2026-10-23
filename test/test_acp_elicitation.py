"""MCP elicitation on the ACP client: the wire shapes, the fail-closed parse, the
answer validation, and the handle's three outcomes (decline unrenderable, decline
without a consumer, yield and answer with one).

Both wires are driven: standard ACP ``elicitation/create`` (fields flat on params)
and KAS ``_kiro/mcp/elicitation`` (fields nested under ``params.elicitation``).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import elicitation as el
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    EVENT_ELICITATION_REQUEST,
    METHOD_ELICITATION_COMPLETE,
    METHOD_ELICITATION_CREATE,
    METHOD_KAS_MCP_ELICITATION,
    JsonRpcMessage,
)

_SID = "sess_elicit"

_FORM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "confirm": {"type": "boolean", "title": "Confirm", "description": "Delete it"},
        "reason": {"type": "string", "maxLength": 40},
        "count": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2},
        "colour": {
            "type": "string",
            "oneOf": [{"const": "r", "title": "Red"}, {"const": "g", "title": "Green"}],
        },
        "tags": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}},
    },
    "required": ["confirm"],
}


def _acp_form(rid: int = 5, **extra: Any) -> JsonRpcMessage:
    params = {
        "sessionId": _SID,
        "toolCallId": "toolu_1",
        "mode": "form",
        "message": "Really delete?",
        "requestedSchema": _FORM_SCHEMA,
    }
    params.update(extra)
    return JsonRpcMessage(id=rid, method=METHOD_ELICITATION_CREATE, params=params)


def _kas_form(rid: int = 6) -> JsonRpcMessage:
    return JsonRpcMessage(
        id=rid,
        method=METHOD_KAS_MCP_ELICITATION,
        params={
            "sessionId": _SID,
            "toolCallId": "toolu_2",
            "elicitation": {"mode": "form", "message": "Really?", "requestedSchema": _FORM_SCHEMA},
        },
    )


def _kas_url(rid: int = 8, url: str = "https://example.com/confirm") -> JsonRpcMessage:
    return JsonRpcMessage(
        id=rid,
        method=METHOD_KAS_MCP_ELICITATION,
        params={
            "sessionId": _SID,
            "toolCallId": "toolu_3",
            "elicitation": {
                "mode": "url",
                "message": "Go confirm",
                "url": url,
                "elicitationId": "e1",
            },
        },
    )


# ── parse ──


def test_both_wires_parse_to_the_same_request_shape() -> None:
    acp = el.parse_elicitation_request(_acp_form())
    kas = el.parse_elicitation_request(_kas_form())
    assert acp.is_form and kas.is_form
    assert [f.name for f in acp.fields] == [f.name for f in kas.fields]
    assert acp.tool_call_id == "toolu_1" and kas.tool_call_id == "toolu_2"
    by_name = {f.name: f for f in acp.fields}
    assert by_name["confirm"].kind == el.FIELD_KIND_BOOLEAN and by_name["confirm"].required
    assert by_name["reason"].max_length == 40 and not by_name["reason"].required
    assert by_name["count"].kind == el.FIELD_KIND_INTEGER and by_name["count"].default == 2
    assert [o.label for o in by_name["colour"].options] == ["Red", "Green"]
    assert by_name["tags"].kind == el.FIELD_KIND_MULTI_ENUM


def test_a_mode_less_form_takes_the_mcp_default() -> None:
    """The MCP server may omit ``mode``; a ``requestedSchema`` means form."""
    msg = _acp_form()
    del msg.params["mode"]
    assert el.parse_elicitation_request(msg).is_form


def test_url_mode_parses_and_refuses_a_non_web_url() -> None:
    req = el.parse_elicitation_request(_kas_url())
    assert req.mode == el.ELICITATION_MODE_URL and req.url.startswith("https://")
    assert req.elicitation_id == "e1"
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(_kas_url(url="javascript:alert(1)"))
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(_kas_url(url="file:///etc/passwd"))


@pytest.mark.parametrize(
    "prop",
    [
        {"type": "object", "properties": {}},
        {"type": "array", "items": {"type": "string"}},
        {"type": "null"},
        {"type": "string", "enum": [1, 2]},
        {"type": "string", "enum": []},
        {},
    ],
)
def test_a_property_this_client_cannot_render_is_refused(prop: dict) -> None:
    schema = {"type": "object", "properties": {"x": prop}}
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(_acp_form(requestedSchema=schema))


def test_bounds_and_missing_parts_are_refused() -> None:
    too_many = {
        "type": "object",
        "properties": {f"p{i}": {"type": "string"} for i in range(el.PROPERTIES_MAX + 1)},
    }
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(_acp_form(requestedSchema=too_many))
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(_acp_form(message=""))
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(_acp_form(sessionId=""))
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(_acp_form(mode="hologram"))
    missing_required = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["b"],
    }
    with pytest.raises(el.ElicitationParseError):
        el.parse_elicitation_request(_acp_form(requestedSchema=missing_required))
    # A notification on the method is not a request (no id): nothing to answer.
    notif = JsonRpcMessage(method=METHOD_ELICITATION_COMPLETE, params={"elicitationId": "e1"})
    assert not el.is_elicitation_request(notif)
    assert not el.is_elicitation_request(
        JsonRpcMessage(method=METHOD_ELICITATION_CREATE, params={})
    )


def test_the_message_is_bounded_for_display() -> None:
    req = el.parse_elicitation_request(_acp_form(message="x" * (el.MESSAGE_MAX_CHARS + 50)))
    assert len(req.message) == el.MESSAGE_MAX_CHARS


def test_as_dict_round_trips_through_from_dict() -> None:
    req = el.parse_elicitation_request(_acp_form())
    back = el.ElicitationRequest.from_dict(req.as_dict())
    assert back.fields == req.fields
    assert back.mode == req.mode and back.message == req.message
    url = el.parse_elicitation_request(_kas_url())
    assert el.ElicitationRequest.from_dict(url.as_dict()).url == url.url


# ── validate ──


def test_form_content_is_checked_and_shaped() -> None:
    req = el.parse_elicitation_request(_acp_form())
    out = el.validate_form_content(
        req, {"confirm": True, "reason": "ok", "count": 3.0, "colour": "g", "tags": ["a", "a", "b"]}
    )
    assert out == {"confirm": True, "reason": "ok", "count": 3, "colour": "g", "tags": ["a", "b"]}
    # An optional field left out is omitted, not defaulted.
    assert el.validate_form_content(req, {"confirm": False}) == {"confirm": False}


@pytest.mark.parametrize(
    "content",
    [
        {},  # required missing
        {"confirm": "yes"},  # wrong type
        {"confirm": True, "count": 9},  # above maximum
        {"confirm": True, "count": 2.5},  # not whole
        {"confirm": True, "reason": "x" * 41},  # too long
        {"confirm": True, "colour": "blue"},  # not offered
        {"confirm": True, "tags": ["z"]},  # not offered
        {"confirm": True, "extra": 1},  # unknown field
    ],
)
def test_content_the_schema_refuses_is_an_error(content: dict) -> None:
    req = el.parse_elicitation_request(_acp_form())
    with pytest.raises(el.ElicitationContentError):
        el.validate_form_content(req, content)


def test_response_carries_content_only_on_accept() -> None:
    assert el.elicitation_response(el.ELICITATION_ACCEPT, {"a": 1}) == {
        "action": "accept",
        "content": {"a": 1},
    }
    assert el.elicitation_response(el.ELICITATION_DECLINE, {"a": 1}) == {"action": "decline"}
    assert el.elicitation_response(el.ELICITATION_CANCEL) == {"action": "cancel"}
    with pytest.raises(ValueError):
        el.elicitation_response("maybe")


# ── the session handle ──


class _Runtime:
    def __init__(self) -> None:
        self.pid = None
        self.is_alive = MagicMock(return_value=True)
        self.send_notification = AsyncMock()
        self.send_request = AsyncMock(return_value=1)
        self.send_response = AsyncMock()
        self.send_error = AsyncMock()
        self.supports_image_prompt = False
        self.acp_backend = ""
        self._last_activity = time.monotonic()

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        return None


def _handle() -> tuple[AcpSessionHandle, _Runtime]:
    runtime = _Runtime()
    return AcpSessionHandle(_SID, asyncio.Queue(), runtime), runtime


@pytest.mark.asyncio
async def test_handle_declines_when_its_consumer_cannot_ask_a_person() -> None:
    handle, runtime = _handle()
    assert handle.accepts_elicitation is False
    assert await handle._handle_elicitation_request(_kas_form()) is None
    runtime.send_response.assert_awaited_once_with(6, {"action": "decline"})
    assert handle._pending_elicitations == {}


@pytest.mark.asyncio
async def test_handle_declines_an_unrenderable_request_even_with_a_consumer() -> None:
    handle, runtime = _handle()
    handle.accepts_elicitation = True
    assert await handle._handle_elicitation_request(_kas_url(url="javascript:x")) is None
    runtime.send_response.assert_awaited_once_with(8, {"action": "decline"})
    runtime.send_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_yields_the_event_and_sends_the_validated_answer() -> None:
    handle, runtime = _handle()
    handle.accepts_elicitation = True
    event = await handle._handle_elicitation_request(_kas_form())
    assert event is not None and event.kind == EVENT_ELICITATION_REQUEST
    assert event.request_id == 6 and event.tool_call_id == "toolu_2"
    assert event.elicitation and event.elicitation["mode"] == "form"
    assert [f["name"] for f in event.elicitation["fields"]][:2] == ["confirm", "reason"]
    runtime.send_response.assert_not_awaited()
    # A bad answer leaves the ask pending; nothing goes on the wire.
    with pytest.raises(el.ElicitationContentError):
        await handle.answer_elicitation(6, el.ELICITATION_ACCEPT, {"confirm": "nope"})
    assert 6 in handle._pending_elicitations
    runtime.send_response.assert_not_awaited()
    assert await handle.answer_elicitation(6, el.ELICITATION_ACCEPT, {"confirm": True, "count": 4})
    runtime.send_response.assert_awaited_once_with(
        6, {"action": "accept", "content": {"confirm": True, "count": 4}}
    )
    assert handle._pending_elicitations == {}
    # Answered once: a second answer finds nothing pending.
    assert not await handle.answer_elicitation(6, el.ELICITATION_DECLINE)


@pytest.mark.asyncio
async def test_handle_cancels_what_is_left_pending() -> None:
    handle, runtime = _handle()
    handle.accepts_elicitation = True
    await handle._handle_elicitation_request(_kas_form(rid=20))
    await handle._handle_elicitation_request(_acp_form(rid=21))
    assert await handle.cancel_pending_elicitations() == 2
    assert sorted(c.args for c in runtime.send_response.await_args_list) == [
        (20, {"action": "cancel"}),
        (21, {"action": "cancel"}),
    ]
    assert await handle.cancel_pending_elicitations() == 0


@pytest.mark.asyncio
async def test_a_url_answer_carries_no_content() -> None:
    handle, runtime = _handle()
    handle.accepts_elicitation = True
    await handle._handle_elicitation_request(_kas_url(rid=30))
    assert await handle.answer_elicitation(30, el.ELICITATION_ACCEPT, {"ignored": True})
    runtime.send_response.assert_awaited_once_with(30, {"action": "accept"})
