"""The dashboard half of MCP elicitation: the card's row, the answer endpoint and the
mapping from a resolved future back to the action the driver sends.

``POST /api/chat/slots/{slot}/elicitation`` is driven through the real handler on an
aiohttp ``TestClient``; the auth middleware is stood in by the middlewares below.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_turn.elicitation_prompt import read_elicitation_answer
from kiro_crew.dashboard.handlers.chat_elicitation import api_chat_slot_elicitation
from kiro_crew.dashboard.state import SlotOrigin

_FIELDS = [
    {"name": "confirm", "kind": "boolean", "required": True, "title": "Confirm"},
    {"name": "count", "kind": "integer", "required": False, "minimum": 1, "maximum": 5},
    {
        "name": "colour",
        "kind": "enum",
        "required": False,
        "options": [{"value": "r", "label": "Red"}],
    },
]


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    return st


@web.middleware
async def _dashboard_user(request: web.Request, handler):
    request["app"] = ""
    request["user"] = "local-app"
    return await handler(request)


@web.middleware
async def _internal_caller(request: web.Request, handler):
    request["internal_auth"] = True
    return await handler(request)


@web.middleware
async def _app_caller(request: web.Request, handler):
    request["app"] = "some-app"
    return await handler(request)


def _app(state, middleware) -> web.Application:
    app = web.Application(middlewares=[middleware])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/elicitation", api_chat_slot_elicitation)
    return app


def _slot_with_pending(state, request_id: str, *, mode: str = "form"):
    slot = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    fut = asyncio.get_running_loop().create_future()
    meta = {"request_id": request_id, "mode": mode, "message": "Really?"}
    if mode == "form":
        meta["fields"] = _FIELDS
    else:
        meta["url"] = "https://example.com/go"
    row = slot.append("elicitation", "Really?", json.dumps(meta))
    slot.register_approval(request_id, fut, row)
    return slot, fut, row


@pytest.mark.asyncio
async def test_a_valid_form_answer_is_validated_written_to_the_row_and_resolves(state):
    _slot, fut, row = _slot_with_pending(state, "rid-1")
    async with TestClient(TestServer(_app(state, _dashboard_user))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/elicitation",
            json={
                "request_id": "rid-1",
                "action": "accept",
                "content": {"confirm": True, "count": 3.0},
            },
        )
        assert resp.status == 200, await resp.text()
    assert fut.result() == "accept"
    assert json.loads(row["cls"])["answer"] == {"confirm": True, "count": 3}
    assert read_elicitation_answer(fut.result(), json.loads(row["cls"])) == (
        "accept",
        {"confirm": True, "count": 3},
    )


@pytest.mark.asyncio
async def test_content_the_schema_refuses_is_a_400_and_the_ask_stays_open(state):
    _slot, fut, row = _slot_with_pending(state, "rid-2")
    async with TestClient(TestServer(_app(state, _dashboard_user))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/elicitation",
            json={
                "request_id": "rid-2",
                "action": "accept",
                "content": {"confirm": True, "count": 9},
            },
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["code"] == "invalid_content" and "count" in body["error"]
        resp = await client.post(
            "/api/chat/slots/selected/elicitation",
            json={"request_id": "rid-2", "action": "accept", "content": {}},
        )
        assert resp.status == 400
    assert not fut.done()
    assert "answer" not in json.loads(row["cls"])


@pytest.mark.asyncio
async def test_decline_needs_no_content_and_a_second_answer_is_404(state):
    _slot, fut, _row = _slot_with_pending(state, "rid-3")
    async with TestClient(TestServer(_app(state, _dashboard_user))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/elicitation",
            json={"request_id": "rid-3", "action": "decline"},
        )
        assert resp.status == 200
        assert fut.result() == "decline"
        resp = await client.post(
            "/api/chat/slots/selected/elicitation",
            json={"request_id": "rid-3", "action": "accept"},
        )
        assert resp.status == 404


@pytest.mark.asyncio
async def test_a_url_ask_accepts_without_content(state):
    _slot, fut, _row = _slot_with_pending(state, "rid-4", mode="url")
    async with TestClient(TestServer(_app(state, _dashboard_user))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/elicitation",
            json={"request_id": "rid-4", "action": "accept"},
        )
        assert resp.status == 200
    assert fut.result() == "accept"


@pytest.mark.asyncio
async def test_a_permission_id_cannot_be_answered_through_the_elicitation_route(state):
    """The route answers elicitation rows only: a permission future under the same
    registry is not an ask a form can accept."""
    slot = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    fut = asyncio.get_running_loop().create_future()
    row = slot.append("permission", "Tool", json.dumps({"request_id": "perm-1"}))
    slot.register_approval("perm-1", fut, row)
    async with TestClient(TestServer(_app(state, _dashboard_user))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/elicitation",
            json={"request_id": "perm-1", "action": "accept"},
        )
        assert resp.status == 404
    assert not fut.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("middleware", [_internal_caller, _app_caller])
async def test_only_the_dashboard_user_may_answer(state, middleware):
    """The server asked for a PERSON; an agent on the internal secret or an app
    token answering would be the automation the ask exists to exclude."""
    _slot, fut, _row = _slot_with_pending(state, "rid-5")
    async with TestClient(TestServer(_app(state, middleware))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/elicitation",
            json={"request_id": "rid-5", "action": "accept", "content": {"confirm": True}},
        )
        assert resp.status == 403
    assert not fut.done()


@pytest.mark.asyncio
async def test_bad_bodies_are_400(state):
    _slot_with_pending(state, "rid-6")
    async with TestClient(TestServer(_app(state, _dashboard_user))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/elicitation", json={"request_id": "rid-6", "action": "maybe"}
        )
        assert resp.status == 400
        resp = await client.post("/api/chat/slots/selected/elicitation", json={"action": "accept"})
        assert resp.status == 400


def test_a_resolved_future_maps_to_the_action_the_driver_sends() -> None:
    """The permission registry's own outcomes (a Stop, the generic approve) are
    read as elicitation actions: a rejection declines, an approve accepts only a
    URL ask, and a form approved without content is cancelled, never accepted."""
    form = {"mode": "form"}
    url = {"mode": "url"}
    assert read_elicitation_answer("decline", form) == ("decline", None)
    assert read_elicitation_answer("rejected", form) == ("decline", None)
    assert read_elicitation_answer("rejected_once", url) == ("decline", None)
    assert read_elicitation_answer("approved", url) == ("accept", None)
    assert read_elicitation_answer("approved", form) == ("cancel", None)
    assert read_elicitation_answer("accept", {"mode": "form", "answer": {"a": 1}}) == (
        "accept",
        {"a": 1},
    )
    assert read_elicitation_answer("accept", form) == ("accept", None)
    assert read_elicitation_answer("whatever", form) == ("cancel", None)
