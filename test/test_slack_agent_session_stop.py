"""Slack's native Stop (``agent_session_stopped``) and user renames.

The handlers run against a session-manager double that records ``stop_turn``,
the real router helpers (busy hold, queue owner tags, flat-DM key), and the
real Slack client over a recording ``_web`` -- so "nothing is posted after
Stop" is checked on the wire, not on a mock.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kiro_crew.config.loader import KiroCrewConfig, MessagingConfig, SlackConfig
from kiro_crew.session_lifecycle import STOP_DECLINED_COMPACTING_TEXT
from kiro_crew.slack import events
from kiro_crew.slack.agent_sessions import fence, session_events
from kiro_crew.slack.agent_sessions.status import SESSION_STATUS_PROCESSING

from test_slack_agent_session_status import make_client


class FakeSessions:
    def __init__(
        self,
        *,
        live: set[str] | None = None,
        owners: dict[str, str] | None = None,
        compacting: set[str] | None = None,
        outcome: str = "soft",
    ) -> None:
        self.live = live or set()
        self.owners = owners or {}
        self.compacting = compacting or set()
        self.outcome = outcome
        self.stops: list[tuple[str, dict[str, Any]]] = []
        self.on_stop: Any = None

    @staticmethod
    def _fold(key: str) -> str:
        return key if ":" in key or "-" in key else f"slack:{key}"

    def has_session(self, key: str) -> bool:
        return self._fold(key) in self.live

    def get_session_for_thread(self, ts: str) -> str | None:
        return self.owners.get(ts)

    def is_compacting(self, key: str) -> bool:
        return self._fold(key) in self.compacting

    def dequeue(self, key: str) -> Any:
        return None

    async def stop_turn(self, key: str, **kwargs: Any) -> str:
        self.stops.append((key, kwargs))
        if self.on_stop is not None:
            await self.on_stop()
        if self._fold(key) in self.compacting and not kwargs.get("force"):
            return "compacting"
        return self.outcome if self.has_session(key) else "idle"


class Orch:
    def __init__(self, sessions: FakeSessions, *, flat: bool = False, live: bool = True) -> None:
        self._cfg = KiroCrewConfig(
            slack=SlackConfig(dm_single_session=flat), messaging=MessagingConfig(use_transport=True)
        )
        self.sessions = sessions
        self.slack, self.web = make_client(live=live)
        self._session_tasks: dict[str, Any] = {}
        self._pending_queue: dict[str, Any] = {}
        self._handler_tasks: set[Any] = set()
        self.conv_log = None


class SelRecorder:
    def __init__(self) -> None:
        self.access: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []

    def log_api_access(self, **kw: Any) -> None:
        self.access.append(kw)

    def log_tool_invocation(self, **kw: Any) -> None:
        self.tools.append(kw)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> SelRecorder:
    recorder = SelRecorder()
    monkeypatch.setattr(session_events, "sel", lambda: recorder)
    monkeypatch.setattr(events, "is_owner", lambda uid: uid == "UOWNER")
    monkeypatch.setattr(events, "is_allowed_user", lambda uid: uid in {"UOWNER", "U1"})
    session_events._SEEN_STOPS.clear()
    return recorder


def stop_event(**kw: Any) -> dict[str, Any]:
    base = {
        "type": "agent_session_stopped",
        "channel": "C1",
        "thread_ts": "1700.1",
        "user": "U1",
        "event_ts": "1800.1",
        "streaming_message_ts": [],
    }
    base.update(kw)
    return base


async def _working(orch: Orch, channel: str = "C1", thread: str = "1700.1") -> None:
    await orch.slack.set_session_status(channel, thread, SESSION_STATUS_PROCESSING)
    orch.web.calls.clear()


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_during_streaming_stops_the_turn_and_posts_nothing_after(
        self, _env: SelRecorder
    ) -> None:
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        await _working(orch)
        started, release = asyncio.Event(), asyncio.Event()

        async def turn() -> None:
            reset = fence.enter_turn("C1", "1700.1")
            try:
                await orch.slack.post_message("C1", "partial…", "1700.1")
                started.set()
                await release.wait()
                # What a cancelled turn's tail would do: seal, footer, clear.
                await orch.slack.stop_stream("C1", "1700.5", "partial answer")
                await orch.slack.post_blocks("C1", [], "⏱ 12s", "1700.1")
                await orch.slack.set_thread_status("C1", "1700.1", "")
            finally:
                fence.exit_turn(reset)

        task = asyncio.ensure_future(turn())
        await started.wait()
        # The cancel lands while stop_turn waits for the ack; the turn's tail then
        # runs to its end (not cancelled here, so the fence is what keeps it quiet).
        sessions.on_stop = _async(release.set)
        outcome = await session_events.handle_stopped(
            orch, stop_event(streaming_message_ts=["1700.5"])
        )
        await asyncio.gather(task, return_exceptions=True)
        assert outcome == "soft"
        assert [k for k, _ in sessions.stops] == ["1700.1"]
        assert "owned_by" in sessions.stops[0][1]
        # The streamed partial stays; after Stop only the seal and the status move.
        assert orch.web.calls == [
            ("chat.postMessage", orch.web.calls[0][1]),
            ("chat.stopStream", {"channel": "C1", "ts": "1700.5"}),
            (
                "agents.sessions.setStatus",
                {"channel_id": "C1", "status": "active", "thread_ts": "1700.1"},
            ),
        ]
        assert any(a["outcome"] == "allowed" for a in _env.access)
        assert _env.tools[-1]["outcome"] == "soft"

    @pytest.mark.asyncio
    async def test_the_running_task_is_cancelled_and_untracked(self) -> None:
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        task = asyncio.ensure_future(asyncio.sleep(60))
        orch._session_tasks["1700.1"] = task
        assert await session_events.handle_stopped(orch, stop_event()) == "soft"
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled() and "1700.1" not in orch._session_tasks

    @pytest.mark.asyncio
    async def test_stop_while_an_approval_waits(self) -> None:
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        await orch.slack.set_thread_status("C1", "1700.1", "Waiting for approval…")
        orch.web.calls.clear()
        assert await session_events.handle_stopped(orch, stop_event()) == "soft"
        assert orch.web.methods() == ["agents.sessions.setStatus"]
        assert orch.web.calls[0][1]["status"] == "active"

    @pytest.mark.asyncio
    async def test_stop_on_an_idle_thread_of_ours_only_settles_the_status(self) -> None:
        sessions = FakeSessions()  # nothing running any more
        orch = Orch(sessions)
        await _working(orch)
        assert await session_events.handle_stopped(orch, stop_event()) == "idle"
        assert [p["status"] for _, p in orch.web.calls] == ["active"]

    @pytest.mark.asyncio
    async def test_a_redelivered_stop_is_dropped(self) -> None:
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        await _working(orch)
        assert await session_events.handle_stopped(orch, stop_event()) == "soft"
        assert await session_events.handle_stopped(orch, stop_event()) == "duplicate"
        assert len(sessions.stops) == 1

    @pytest.mark.asyncio
    async def test_a_second_press_is_idempotent(self) -> None:
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        await _working(orch)
        await session_events.handle_stopped(orch, stop_event())
        sessions.live.clear()
        assert await session_events.handle_stopped(orch, stop_event(event_ts="1800.2")) == "idle"
        assert [p["status"] for _, p in orch.web.calls] == ["active", "active"]

    @pytest.mark.asyncio
    async def test_a_thread_the_bot_has_no_session_in_is_left_alone(
        self, _env: SelRecorder
    ) -> None:
        sessions = FakeSessions()
        orch = Orch(sessions)
        assert await session_events.handle_stopped(orch, stop_event()) == "not_ours"
        assert sessions.stops == [] and orch.web.calls == []  # no setStatus: it would create one
        assert _env.access[-1]["outcome"] == "no_session"

    @pytest.mark.asyncio
    async def test_a_stop_from_someone_not_allowed_stops_nothing(self, _env: SelRecorder) -> None:
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        await _working(orch)
        assert await session_events.handle_stopped(orch, stop_event(user="UOTHER")) == "denied"
        assert sessions.stops == [] and orch.web.calls == []
        assert _env.access[-1] == {
            "caller": "UOTHER",
            "operation": "slack.agent_session_stopped",
            "outcome": "denied",
            "source": "slack",
            "resources": "C1:1700.1",
            "error": "unauthorized sender",
        }

    @pytest.mark.asyncio
    async def test_a_dm_thread_stops_like_a_channel_thread(self) -> None:
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        await _working(orch, channel="D1")
        assert await session_events.handle_stopped(orch, stop_event(channel="D1")) == "soft"
        assert [k for k, _ in sessions.stops] == ["1700.1"]

    @pytest.mark.asyncio
    async def test_a_single_session_dm_stops_the_flat_key_and_mutes_the_whole_dm(self) -> None:
        sessions = FakeSessions(live={"slack:D1"})
        orch = Orch(sessions, flat=True)
        await _working(orch, channel="D1", thread="1700.9")
        root_turn = fence.enter_turn("D1", "1700.2")  # a turn replying in another thread
        try:
            outcome = await session_events.handle_stopped(
                orch, stop_event(channel="D1", thread_ts="1700.9")
            )
            assert await orch.slack.post_message("D1", "late", "1700.2") == ""
        finally:
            fence.exit_turn(root_turn)
        assert outcome == "soft"
        assert [k for k, _ in sessions.stops] == ["slack:D1"]

    @pytest.mark.asyncio
    async def test_a_dashboard_linked_thread_stops_its_owner_without_flat_dms(self) -> None:
        """The turn runs in the dashboard session that owns the thread."""
        sessions = FakeSessions(live={"chat-7-abc"}, owners={"1700.1": "chat-7-abc"})
        orch = Orch(sessions)
        await _working(orch)
        assert await session_events.handle_stopped(orch, stop_event()) == "soft"
        assert [k for k, _ in sessions.stops] == ["chat-7-abc"]

    @pytest.mark.asyncio
    async def test_a_self_derived_owner_is_not_a_binding(self) -> None:
        sessions = FakeSessions(live={"slack:1700.1"}, owners={"1700.1": "slack:1700.1"})
        orch = Orch(sessions)
        await _working(orch)
        await session_events.handle_stopped(orch, stop_event())
        assert [k for k, _ in sessions.stops] == ["1700.1"]

    @pytest.mark.asyncio
    async def test_stop_during_compaction_is_declined_then_forced(self) -> None:
        sessions = FakeSessions(live={"slack:1700.1"}, compacting={"slack:1700.1"})
        orch = Orch(sessions)
        await _working(orch)
        turn = fence.enter_turn("C1", "1700.1")
        try:
            assert await session_events.handle_stopped(orch, stop_event()) == "compacting"
            # Declined before any side effect: no stop_turn, the turn still talks.
            assert sessions.stops == []
            assert await orch.slack.post_message("C1", "still working", "1700.1") == "9.9"
        finally:
            fence.exit_turn(turn)
        assert (
            "chat.postEphemeral",
            {
                "channel": "C1",
                "user": "U1",
                "text": STOP_DECLINED_COMPACTING_TEXT,
                "thread_ts": "1700.1",
            },
        ) in orch.web.calls
        assert "agents.sessions.setStatus" not in orch.web.methods()  # still working
        outcome = await session_events.handle_stopped(orch, stop_event(event_ts="1800.2"))
        assert sessions.stops[-1][1].get("force") is True
        assert outcome == "soft"

    @pytest.mark.asyncio
    async def test_a_declined_stop_with_no_warning_delivered_arms_nothing(self) -> None:
        sessions = FakeSessions(live={"slack:1700.1"}, compacting={"slack:1700.1"})
        orch = Orch(sessions)
        orch.web.chat_postEphemeral = _raise  # type: ignore[method-assign]
        await session_events.handle_stopped(orch, stop_event())
        await session_events.handle_stopped(orch, stop_event(event_ts="1800.2"))
        assert sessions.stops == []  # the second press is a first press again

    @pytest.mark.asyncio
    async def test_a_whole_channel_stop_goes_to_the_registered_handler(self) -> None:
        sessions = FakeSessions()
        orch = Orch(sessions)
        seen: list[tuple[str, str]] = []

        async def claim(o: Any, event: dict, user: str) -> bool:
            seen.append((event["channel"], user))
            return event["channel"] == "CCODE"

        session_events.register_channel_stop_handler(claim)
        session_events.register_channel_stop_handler(claim)  # idempotent
        try:
            ev = stop_event(channel="CCODE", thread_ts=None)
            assert await session_events.handle_stopped(orch, ev) == "channel"
            assert (
                await session_events.handle_stopped(orch, stop_event(thread_ts=None, event_ts="2"))
                == "not_ours"
            )
            assert (
                await session_events.handle_stopped(
                    orch, stop_event(channel="CCODE", thread_ts=None, user="UOTHER", event_ts="3")
                )
                == "denied"
            )
        finally:
            session_events.unregister_channel_stop_handler(claim)
        assert seen == [("CCODE", "U1"), ("C1", "U1")]

    @pytest.mark.asyncio
    async def test_with_the_feature_off_a_stop_still_stops_but_sends_no_status(self) -> None:
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions, live=False)
        assert await session_events.handle_stopped(orch, stop_event()) == "soft"
        assert orch.web.calls == []


class TestDispatchGate:
    @pytest.mark.asyncio
    async def test_no_workspace_id_is_refused(
        self, _env: SelRecorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch = Orch(FakeSessions(live={"slack:1700.1"}))
        session_events.dispatch(orch, {}, stop_event())
        assert orch._handler_tasks == set()
        assert _env.access[-1]["error"] == "missing_team_id"

    @pytest.mark.asyncio
    async def test_a_disallowed_workspace_is_refused(
        self, _env: SelRecorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch = Orch(FakeSessions(live={"slack:1700.1"}))
        _gate(monkeypatch, allowed=False)
        session_events.dispatch(orch, {"team_id": "TBAD"}, stop_event())
        assert orch._handler_tasks == set()
        assert _env.access[-1]["error"] == "enterprise_origin_mismatch"

    @pytest.mark.asyncio
    async def test_an_allowed_workspace_reaches_the_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        _gate(monkeypatch, allowed=True)
        session_events.dispatch(orch, {"team_id": "T1"}, stop_event())
        await asyncio.gather(*orch._handler_tasks)
        assert [k for k, _ in sessions.stops] == ["1700.1"]

    @pytest.mark.asyncio
    async def test_the_socket_listener_routes_session_events_here(self) -> None:
        import inspect

        src = inspect.getsource(events.init_socket_mode)
        assert "event_type in AGENT_SESSION_EVENTS" in src
        assert "dispatch_agent_session_event(orch, req.payload or {}, event)" in src


class TestTitleChanged:
    def _event(self, **kw: Any) -> dict[str, Any]:
        base = {
            "type": "agent_session_title_changed",
            "channel": "C1",
            "thread_ts": "1700.1",
            "title": "Bora Bora trip prep",
            "previous_title": "Scuba",
            "user": "U1",
        }
        base.update(kw)
        return base

    @pytest.mark.asyncio
    async def test_a_rename_titles_the_dashboard_session(self, _env: SelRecorder) -> None:
        from kiro_crew.messaging import auto_title

        orch = Orch(FakeSessions())
        orch.conv_log = TitleLog()
        assert await session_events.handle_title_changed(orch, self._event()) == "renamed"
        assert orch.conv_log.titles == [("slack:1700.1", "Bora Bora trip prep")]
        assert auto_title._titled.get("slack:1700.1") == auto_title.TITLE_KIND_MANUAL
        assert _env.access[-1]["outcome"] == "allowed"

    @pytest.mark.asyncio
    async def test_a_linked_thread_titles_its_owner(self) -> None:
        orch = Orch(FakeSessions(owners={"1700.1": "chat-7-abc"}))
        orch.conv_log = TitleLog()
        await session_events.handle_title_changed(orch, self._event())
        assert orch.conv_log.titles == [("chat-7-abc", "Bora Bora trip prep")]

    @pytest.mark.asyncio
    async def test_a_rename_by_someone_not_allowed_is_not_mirrored(self, _env: SelRecorder) -> None:
        orch = Orch(FakeSessions())
        orch.conv_log = TitleLog()
        assert await session_events.handle_title_changed(orch, self._event(user="UX")) == "denied"
        assert orch.conv_log.titles == []
        assert _env.access[-1]["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_our_own_rename_coming_back_is_ignored(self) -> None:
        orch = Orch(FakeSessions())
        orch.conv_log = TitleLog()
        await orch.slack.set_thread_title("C1", "slack:1700.1", "Bora Bora trip prep")
        assert await session_events.handle_title_changed(orch, self._event()) == "echo"
        assert orch.conv_log.titles == []

    @pytest.mark.asyncio
    async def test_a_credential_in_the_title_is_redacted(self) -> None:
        orch = Orch(FakeSessions())
        orch.conv_log = TitleLog()
        secret = "AKIA" + "ABCDEFGHIJKLMNOP"
        await session_events.handle_title_changed(orch, self._event(title=f"keys {secret}"))
        assert secret not in orch.conv_log.titles[0][1]

    @pytest.mark.asyncio
    async def test_a_restricted_session_records_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.messaging import privacy_mode

        monkeypatch.setattr(privacy_mode, "is_restricted", lambda key: True)
        orch = Orch(FakeSessions())
        orch.conv_log = TitleLog()
        assert await session_events.handle_title_changed(orch, self._event()) == "restricted"
        assert orch.conv_log.titles == []


class TitleLog:
    def __init__(self) -> None:
        self.titles: list[tuple[str, str]] = []

    def set_title(self, key: str, title: str) -> None:
        self.titles.append((key, title))


def _gate(monkeypatch: pytest.MonkeyPatch, *, allowed: bool) -> None:
    class Gate:
        def check_message_origin(self, team_id: str) -> bool:
            return allowed

    class Ctx:
        slack_gate = Gate()

    monkeypatch.setattr(events, "current_context", lambda: Ctx())


def _async(fn: Any) -> Any:
    async def _run() -> None:
        fn()
        await asyncio.sleep(0)

    return _run


async def _raise(**kwargs: Any) -> Any:
    raise RuntimeError("ephemeral failed")
