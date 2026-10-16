"""Agent-session status and title (``agents.sessions.*``) on the real Slack client.

The real :class:`RealSlackClient` with a recording ``_web`` stand-in, so the
wire method and payload are observable: interface-level mocks cannot tell
``agents.sessions.setStatus`` from ``assistant.threads.setStatus``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.slack.agent_sessions import fence, status
from kiro_crew.slack.agent_sessions.status import (
    PROCESSING_REFRESH_SECS,
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_PROCESSING,
    SESSION_STATUS_SUSPENDED,
    SUSPENDED_STATUS_TEXTS,
    AgentSessionStatus,
    lifecycle_for_text,
    thread_ts_of,
)
from kiro_crew.slack.client import RealSlackClient

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


class SlackApiError(Exception):
    """Shaped like slack_sdk's: ``.response`` carries Slack's ``error``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"ok": False, "error": code}


class RecordingWeb:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.errors: dict[str, str] = {}
        self.responses: dict[str, dict[str, Any]] = {}

    async def api_call(self, method: str, *, params: Any = None, json: Any = None) -> Any:
        self.calls.append((method, dict(params if params is not None else (json or {}))))
        if method in self.errors:
            raise SlackApiError(self.errors[method])
        return self.responses.get(method, {"ok": True})

    async def chat_postMessage(self, **kwargs: Any) -> Any:  # noqa: N802 - slack_sdk name
        self.calls.append(("chat.postMessage", kwargs))
        return {"ts": "9.9"}

    async def chat_update(self, **kwargs: Any) -> Any:  # noqa: N802
        self.calls.append(("chat.update", kwargs))
        return {"ok": True}

    async def chat_postEphemeral(self, **kwargs: Any) -> Any:  # noqa: N802
        self.calls.append(("chat.postEphemeral", kwargs))
        return {"ok": True}

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_client(*, live: bool = True, notes: list | None = None, clock: Clock | None = None):
    web = RecordingWeb()
    client = RealSlackClient.__new__(RealSlackClient)
    client._web = web  # type: ignore[attr-defined]
    if live:
        sink = notes if notes is not None else []
        client.agent_sessions = AgentSessionStatus(
            client,
            notify=lambda: (lambda *a, **kw: sink.append((a, kw))),
            clock=clock or Clock(),
        )
    return client, web


def test_status_text_maps_to_the_lifecycle() -> None:
    assert lifecycle_for_text("") == SESSION_STATUS_ACTIVE
    assert lifecycle_for_text("is using bash") == SESSION_STATUS_PROCESSING
    assert lifecycle_for_text("is working on your request") == SESSION_STATUS_PROCESSING
    for text in SUSPENDED_STATUS_TEXTS:
        assert lifecycle_for_text(text) == SESSION_STATUS_SUSPENDED


def test_the_suspended_texts_are_the_ones_the_call_sites_set() -> None:
    """A reworded call site would silently read as ``processing`` again."""
    sites = {
        "Waiting for approval…": _SRC / "slack" / "handler_runtime" / "stream.py",
        "Awaiting review…": _SRC / "slack" / "handler_runtime" / "finalize.py",
    }
    assert set(sites) == set(SUSPENDED_STATUS_TEXTS)
    for text, path in sites.items():
        assert f'"{text}"' in path.read_text(encoding="utf-8"), (text, path)


def test_thread_ts_of_accepts_only_a_slack_thread() -> None:
    assert thread_ts_of("1700.000100") == "1700.000100"
    assert thread_ts_of("slack:1700.000100") == "1700.000100"
    assert thread_ts_of("slack:D0123ABC") is None  # a single-session DM names a channel
    assert thread_ts_of("chat-12-abc") is None  # a dashboard-owned session
    assert thread_ts_of("") is None


class TestToggleOff:
    """No driver attached: exactly the calls main makes."""

    @pytest.mark.asyncio
    async def test_status_and_title_use_assistant_threads(self) -> None:
        client, web = make_client(live=False)
        await client.set_thread_status("C1", "1700.1", "is using bash")
        await client.set_thread_status("C1", "1700.1", "")
        await client.set_thread_title("C1", "slack:1700.1", "Trip")
        assert web.calls == [
            (
                "assistant.threads.setStatus",
                {"channel_id": "C1", "thread_ts": "1700.1", "status": "is using bash"},
            ),
            (
                "assistant.threads.setStatus",
                {"channel_id": "C1", "thread_ts": "1700.1", "status": ""},
            ),
            (
                "assistant.threads.setTitle",
                {"channel_id": "C1", "thread_ts": "slack:1700.1", "title": "Trip"},
            ),
        ]

    @pytest.mark.asyncio
    async def test_session_status_is_a_no_op(self) -> None:
        client, web = make_client(live=False)
        assert await client.set_session_status("C1", "1700.1", SESSION_STATUS_SUSPENDED) is False
        assert await status.set_session_status(client, "C1", None, SESSION_STATUS_ACTIVE) is False
        assert web.calls == []

    def test_configure_leaves_the_client_alone_when_off(self) -> None:
        client, _ = make_client(live=False)

        class Orch:
            slack = client
            _cfg = type("Cfg", (), {"slack": type("S", (), {"agent_sessions": False})()})()

        assert status.configure(Orch()) is None
        assert client.agent_sessions is None

    def test_configure_attaches_the_driver_when_on(self) -> None:
        client, _ = make_client(live=False)

        class Orch:
            slack = client
            dashboard_state = None
            _cfg = type("Cfg", (), {"slack": type("S", (), {"agent_sessions": True})()})()

        driver = status.configure(Orch())
        assert isinstance(driver, AgentSessionStatus)
        assert client.agent_sessions is driver


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_a_turn_reads_processing_then_active(self) -> None:
        client, web = make_client()
        await client.set_thread_status("C1", "1700.1", "is working on your request")
        await client.set_thread_status("C1", "1700.1", "")
        assert web.calls == [
            (
                "agents.sessions.setStatus",
                {"channel_id": "C1", "status": "processing", "thread_ts": "1700.1"},
            ),
            (
                "agents.sessions.setStatus",
                {"channel_id": "C1", "status": "active", "thread_ts": "1700.1"},
            ),
        ]
        assert not any(m.startswith("assistant.threads") for m in web.methods())

    @pytest.mark.asyncio
    async def test_repeat_processing_is_not_resent(self) -> None:
        client, web = make_client()
        for text in ("is working on your request", "is using bash", "is_typing", "is using read"):
            await client.set_thread_status("C1", "1700.1", text)
        assert web.methods() == ["agents.sessions.setStatus"]

    @pytest.mark.asyncio
    async def test_an_approval_wait_reads_suspended_then_processing(self) -> None:
        client, web = make_client()
        await client.set_thread_status("C1", "1700.1", "is working on your request")
        await client.set_thread_status("C1", "1700.1", "Waiting for approval…")
        await client.set_thread_status("C1", "1700.1", "is using bash")
        assert [p["status"] for _, p in web.calls] == ["processing", "suspended", "processing"]

    @pytest.mark.asyncio
    async def test_clearing_a_status_never_set_creates_no_session(self) -> None:
        """setStatus creates a session; an error path's clear must not."""
        client, web = make_client()
        await client.set_thread_status("C1", "1700.1", "")
        assert web.calls == []

    @pytest.mark.asyncio
    async def test_a_whole_channel_session_omits_thread_ts(self) -> None:
        client, web = make_client()
        assert await status.set_session_status(client, "C1", None, SESSION_STATUS_PROCESSING)
        assert web.calls == [
            ("agents.sessions.setStatus", {"channel_id": "C1", "status": "processing"})
        ]

    @pytest.mark.asyncio
    async def test_an_org_install_routes_by_the_channel_team(self) -> None:
        client, web = make_client()
        client.record_channel_team("C1", "T9")
        await client.set_thread_status("C1", "1700.1", "is using bash")
        await client.set_thread_title("C1", "slack:1700.1", "Trip")
        assert all(p.get("team_id") == "T9" for _, p in web.calls)

    @pytest.mark.asyncio
    async def test_a_root_message_falls_back_to_the_old_call(self) -> None:
        """No thread, no thread session: identical to main's (failing) call."""
        client, web = make_client()
        await client.set_thread_status("D1", "", "is using bash")
        assert web.methods() == ["assistant.threads.setStatus"]


class TestTitle:
    @pytest.mark.asyncio
    async def test_a_session_key_title_renames_the_bare_thread(self) -> None:
        client, web = make_client()
        await client.set_thread_title("C1", "slack:1700.1", "Bora Bora trip prep")
        assert web.calls == [
            (
                "agents.sessions.rename",
                {"channel_id": "C1", "title": "Bora Bora trip prep", "thread_ts": "1700.1"},
            )
        ]
        assert client.agent_sessions.last_title("C1", "1700.1") == "Bora Bora trip prep"

    @pytest.mark.asyncio
    async def test_a_key_naming_no_thread_renames_nothing(self) -> None:
        client, web = make_client()
        await client.set_thread_title("D1", "slack:D0123", "Flat DM")
        await client.set_thread_title("C1", "chat-1-abc", "Dashboard")
        assert web.calls == []


class TestDegrade:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", ["feature_disabled", "missing_scope", "not_allowed"])
    async def test_a_disabling_answer_falls_back_and_notifies_once(self, code: str) -> None:
        notes: list = []
        client, web = make_client(notes=notes)
        web.errors["agents.sessions.setStatus"] = code
        await client.set_thread_status("C1", "1700.1", "is using bash")
        await client.set_thread_status("C1", "1700.2", "is using bash")
        await client.set_thread_title("C1", "slack:1700.2", "Trip")
        assert web.methods() == [
            "agents.sessions.setStatus",
            "assistant.threads.setStatus",  # the very call that failed still lands
            "assistant.threads.setStatus",
            "assistant.threads.setTitle",
        ]
        assert len(notes) == 1
        assert code in notes[0][0][2]
        assert client.agent_sessions.disabled_reason == code
        assert status.driver_of(client) is None

    @pytest.mark.asyncio
    async def test_a_per_call_error_does_not_disable(self) -> None:
        notes: list = []
        client, web = make_client(notes=notes)
        web.errors["agents.sessions.setStatus"] = "not_authorized"
        await client.set_thread_status("C1", "1700.1", "is using bash")
        assert web.methods() == ["agents.sessions.setStatus"]
        assert client.agent_sessions.active and notes == []

    @pytest.mark.asyncio
    async def test_a_missing_stop_subscription_is_reported_once(self) -> None:
        notes: list = []
        client, web = make_client(notes=notes)
        web.responses["agents.sessions.setStatus"] = {
            "ok": True,
            "warning": "missing_agent_session_stopped_event_subscription",
        }
        await client.set_thread_status("C1", "1700.1", "is using bash")
        await client.set_thread_status("C1", "1700.2", "is using bash")
        assert len(notes) == 1 and "Stop" in notes[0][0][1]
        assert client.agent_sessions.active


class TestKeepalive:
    @pytest.mark.asyncio
    async def test_a_long_turn_resends_processing_and_an_ended_turn_goes_active(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = Clock()
        client, web = make_client(clock=clock)
        sleeps: list[float] = []
        real_sleep = asyncio.sleep

        async def fake_sleep(secs: float) -> None:
            sleeps.append(secs)
            clock.now += secs
            await real_sleep(0)

        monkeypatch.setattr(status.asyncio, "sleep", fake_sleep)
        reset = fence.enter_turn("C1", "1700.1")
        try:
            await client.set_thread_status("C1", "1700.1", "is using bash")
            for _ in range(5):
                await real_sleep(0)
            # Still inside the turn after one window: processing again.
            assert [p["status"] for _, p in web.calls][:2] == ["processing", "processing"]
            assert sleeps[0] == PROCESSING_REFRESH_SECS
        finally:
            fence.exit_turn(reset)
        for _ in range(10):
            await real_sleep(0)
        # The turn ended without clearing its status: the keepalive does it.
        assert [p["status"] for _, p in web.calls][-1] == "active"
        await client.agent_sessions.close()

    @pytest.mark.asyncio
    async def test_no_keepalive_outside_a_turn(self) -> None:
        client, _ = make_client()
        await client.set_thread_status("C1", "1700.1", "is using bash")
        record = client.agent_sessions._sessions[("C1", "1700.1")]
        assert record.keepalive is None


class TestWriteFence:
    @pytest.mark.asyncio
    async def test_a_stopped_turn_posts_nothing_and_the_next_turn_does(self) -> None:
        client, web = make_client()
        reset = fence.enter_turn("C1", "1700.1")
        try:
            await client.post_message("C1", "partial", "1700.1")
            assert fence.mute_turns("C1", "1700.1")
            assert await client.post_message("C1", "late answer", "1700.1") == ""
            await client.post_blocks("C1", [], "footer", "1700.1")
            await client.update_message("C1", "9.9", text="sealed")
            await client.post_ephemeral("C1", "U1", "hi", thread_ts="1700.1")
            assert await client.start_stream("C1", "1700.1") is None
            assert await client.append_stream("C1", "9.9", "more") is False
            assert await client.stop_stream("C1", "9.9", "final") is False
            await client.set_thread_status("C1", "1700.1", "is using bash")
            await client.set_thread_title("C1", "slack:1700.1", "late title")
        finally:
            fence.exit_turn(reset)
        assert web.methods() == ["chat.postMessage"]
        # A new turn in the same thread is a new scope.
        reset = fence.enter_turn("C1", "1700.1")
        try:
            await client.post_message("C1", "next answer", "1700.1")
        finally:
            fence.exit_turn(reset)
        assert web.methods() == ["chat.postMessage", "chat.postMessage"]

    @pytest.mark.asyncio
    async def test_tasks_the_turn_spawned_are_fenced_too(self) -> None:
        client, web = make_client()
        gate = asyncio.Event()

        async def background() -> None:
            await gate.wait()
            await client.post_message("C1", "from a child task", "1700.1")

        reset = fence.enter_turn("C1", "1700.1")
        try:
            child = asyncio.ensure_future(background())
            fence.mute_turns("C1", "1700.1")
        finally:
            fence.exit_turn(reset)
        gate.set()
        await child
        assert web.methods() == []

    @pytest.mark.asyncio
    async def test_a_stop_marks_only_its_own_thread(self) -> None:
        client, web = make_client()
        a = fence.enter_turn("C1", "1700.1")
        try:
            other = asyncio.ensure_future(_post_in_turn(client, "C1", "1700.2"))
            fence.mute_turns("C1", "1700.1")
            await other
            await client.post_message("C1", "muted", "1700.1")
        finally:
            fence.exit_turn(a)
        assert [kw["text"] for m, kw in web.calls] == ["other thread"]

    @pytest.mark.asyncio
    async def test_fenced_turn_reads_the_turn_shape(self) -> None:
        seen: list[Any] = []

        @fence.fenced_turn
        async def entry(slack, sessions, channel, text, thread_ts, msg_ts, user_id):  # noqa: ANN001
            seen.append((fence.current_scope().channel, fence.current_scope().thread_ts))

        await entry(None, None, "C1", "hi", None, "1700.5", "U1")
        await entry(
            None, None, channel="C2", text="hi", thread_ts="1700.6", msg_ts="1", user_id="U"
        )
        assert seen == [("C1", "1700.5"), ("C2", "1700.6")]
        assert fence.current_scope() is None


async def _post_in_turn(client: RealSlackClient, channel: str, thread_ts: str) -> None:
    reset = fence.enter_turn(channel, thread_ts)
    try:
        await asyncio.sleep(0)
        await client.post_message(channel, "other thread", thread_ts)
    finally:
        fence.exit_turn(reset)


class TestRendererApprovalPause:
    """The transport renderer marks a tool-approval wait ``suspended``."""

    @pytest.mark.asyncio
    async def test_an_approval_prompt_suspends_and_the_next_output_resumes(self) -> None:
        from kiro_crew.slack.renderer import SlackRenderer

        client, web = make_client()
        renderer = SlackRenderer(client, "C1", "1700.1", reactions_enabled=False)
        await renderer.on_turn_start()
        await renderer.on_prompt_choice([{"id": "allow", "label": "Allow"}], 1, tool_title="bash")
        await renderer.on_text_chunk("done")
        statuses = [p["status"] for m, p in web.calls if m == "agents.sessions.setStatus"]
        assert statuses == ["processing", "suspended", "processing"]
        await renderer.close()

    @pytest.mark.asyncio
    async def test_with_the_feature_off_the_prompt_sets_no_status(self) -> None:
        from kiro_crew.slack.renderer import SlackRenderer

        client, web = make_client(live=False)
        renderer = SlackRenderer(client, "C1", "1700.1", reactions_enabled=False)
        await renderer.on_prompt_choice([{"id": "allow", "label": "Allow"}], 1, tool_title="bash")
        assert [m for m in web.methods() if "status" in m.lower()] == []
        await renderer.close()
