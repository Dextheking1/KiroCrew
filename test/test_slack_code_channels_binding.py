"""Slack Code channels: the bound dashboard session, the mirror bridges, turn hooks,
the native status subscriber, and the durable store.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.slack import features
from kiro_crew.slack.code_channels import binding as binding_mod
from kiro_crew.slack.code_channels import hooks, status
from kiro_crew.slack.code_channels.binding import (
    SessionSpec,
    create_bound_session,
    mirror_post_thread,
    notify_turn_end,
    notify_turn_start,
)
from kiro_crew.slack.code_channels.client import CodeChannelsClient
from kiro_crew.slack.code_channels.store import (
    ROUTE_CODE_CHANNEL,
    Binding,
    CodeChannelStore,
    Route,
    set_store,
)

BINDING = Binding(channel_id="C0CODE", team_id="T1", session_key="chat-1-1", anchor_ts="300.000001")


@pytest.fixture
def bound(tmp_path: Any, monkeypatch: pytest.MonkeyPatch):
    store = CodeChannelStore(tmp_path / "slack-code-channels.json")
    store.put_binding(BINDING)
    set_store(store)
    toggle = {"on": True}
    features.record_granted_scopes([features.REQUIRED_SCOPE[features.CODE_CHANNELS]])
    monkeypatch.setattr(
        binding_mod,
        "live_config",
        lambda _orch=None: SimpleNamespace(slack=SimpleNamespace(code_channels=toggle["on"])),
    )
    yield toggle
    set_store(None)
    features.reset_capabilities()


# ── Store ────────────────────────────────────────────────────────────────────


def test_route_decisions_are_first_write_wins_and_destinations_monotonic(tmp_path: Any) -> None:
    store = CodeChannelStore(tmp_path / "s.json")
    first = store.record_decision(Route("sid", "C1", "1.0", decision=ROUTE_CODE_CHANNEL, title="A"))
    again = store.record_decision(Route("sid", "C1", "1.0", decision="stay"))
    assert again == first
    assert store.record_destination("sid", "C9").channel_id == "C9"
    assert store.record_destination("sid", "C8").channel_id == "C9"
    reloaded = CodeChannelStore(tmp_path / "s.json")
    assert reloaded.get_route("sid").channel_id == "C9"
    assert reloaded.get_route("sid").decision == ROUTE_CODE_CHANNEL


def test_bindings_survive_a_restart_and_put_never_overwrites(tmp_path: Any) -> None:
    store = CodeChannelStore(tmp_path / "s.json")
    store.put_binding(BINDING)
    other = Binding(channel_id="C0CODE", team_id="T1", session_key="chat-2-2", anchor_ts="1.0")
    assert store.put_binding(other).session_key == "chat-1-1"
    assert CodeChannelStore(tmp_path / "s.json").get_binding("C0CODE").session_key == "chat-1-1"
    store.replace_binding(other)
    assert CodeChannelStore(tmp_path / "s.json").get_binding("C0CODE").session_key == "chat-2-2"


@pytest.mark.parametrize(
    "content", ["{not json", "[]", '{"bindings": {"C1": {"channel_id": "C2"}}}', "{}"]
)
def test_unreadable_or_mismatched_store_reads_as_empty(tmp_path: Any, content: str) -> None:
    path = tmp_path / "s.json"
    path.write_text(content)
    store = CodeChannelStore(path)
    assert store.bindings() == [] and store.get_route("x") is None


def test_routes_expire_and_are_capped(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.slack.code_channels import store as store_mod

    monkeypatch.setattr(store_mod, "MAX_ROUTES", 3)
    store = CodeChannelStore(tmp_path / "s.json")
    now = time.time()
    old = now - store_mod.ROUTE_TTL_SECS - 1
    store.record_decision(Route("old", "C1", "1.0", decision="stay", created_at=old))
    for i in range(4):
        store.record_decision(Route(f"r{i}", "C1", f"{i}.0", decision="stay", created_at=now + i))
    kept = {sid for sid in ("old", "r0", "r1", "r2", "r3") if store.get_route(sid)}
    assert kept == {"r1", "r2", "r3"}  # expired dropped, then the oldest past the cap
    assert len(json.loads((tmp_path / "s.json").read_text())["routes"]) == 3


def test_update_binding_changes_named_fields_atomically_and_persists(tmp_path: Any) -> None:
    store = CodeChannelStore(tmp_path / "s.json")
    store.put_binding(BINDING)
    updated = store.update_binding("C0CODE", wrapped_up_at=12.5, channel_name="fix-login")
    assert updated is not None and updated.wrapped_up_at == 12.5
    assert updated.session_key == BINDING.session_key  # untouched fields kept
    reloaded = CodeChannelStore(tmp_path / "s.json").get_binding("C0CODE")
    assert reloaded is not None and reloaded.channel_name == "fix-login"
    assert reloaded.wrapped_up_at == 12.5 and reloaded.archived_at == 0.0
    assert store.update_binding("C0NONE", archived_at=1.0) is None


@pytest.mark.parametrize("bad", [{"channel_id": "C9"}, {"no_such_field": 1}])
def test_update_binding_refuses_unknown_fields_and_the_key(tmp_path: Any, bad: dict) -> None:
    store = CodeChannelStore(tmp_path / "s.json")
    store.put_binding(BINDING)
    with pytest.raises(TypeError):
        store.update_binding("C0CODE", **bad)
    assert store.get_binding("C0CODE") == CodeChannelStore(tmp_path / "s.json").get_binding(
        "C0CODE"
    )


@pytest.mark.parametrize(
    "write",
    [
        lambda s: s.put_binding(
            Binding(channel_id="C0NEW", team_id="T", session_key="k", anchor_ts="1.0")
        ),
        lambda s: s.update_binding("C0CODE", wrapped_up_at=1.0),
        lambda s: s.drop_binding("C0CODE"),
        lambda s: s.record_decision(Route("sid", "C1", "1.0", decision=ROUTE_CODE_CHANNEL)),
    ],
)
def test_a_failed_write_raises_and_changes_nothing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, write: Any
) -> None:
    from kiro_crew.slack.code_channels import store as store_mod

    store = CodeChannelStore(tmp_path / "s.json")
    store.put_binding(BINDING)

    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store_mod, "atomic_write", boom)
    with pytest.raises(store_mod.StoreWriteError):
        write(store)
    # Nothing was published: memory still matches the file.
    assert store.get_binding("C0CODE") == _on_disk(store)
    assert store.get_binding("C0NEW") is None and store.get_route("sid") is None


def _on_disk(store: CodeChannelStore) -> Binding | None:
    return CodeChannelStore(store.path).get_binding("C0CODE")


def test_reads_are_lock_free_snapshots(tmp_path: Any) -> None:
    store = CodeChannelStore(tmp_path / "s.json")
    store.put_binding(BINDING)
    with store._lock:  # a writer holding the lock never blocks a reader
        assert store.get_binding("C0CODE") is not None
        assert store.bindings()


def test_a_record_written_before_the_new_fields_loads_unchanged(tmp_path: Any) -> None:
    path = tmp_path / "s.json"
    old = {
        "channel_id": "C0CODE",
        "team_id": "T1",
        "session_key": "chat-1-1",
        "anchor_ts": "300.000001",
        "created_at": 5.0,
    }
    path.write_text(json.dumps({"bindings": {"C0CODE": old}}))
    binding = CodeChannelStore(path).get_binding("C0CODE")
    assert binding is not None and binding.session_key == "chat-1-1"
    assert (binding.channel_name, binding.archived_at, binding.wrapped_up_at) == ("", 0.0, 0.0)


def test_store_file_is_plain_json(tmp_path: Any) -> None:
    store = CodeChannelStore(tmp_path / "s.json")
    store.put_binding(BINDING)
    data = json.loads((tmp_path / "s.json").read_text())
    assert data["bindings"]["C0CODE"]["anchor_ts"] == "300.000001"


# ── Mirror bridges ───────────────────────────────────────────────────────────


def test_mirror_posts_top_level_only_for_the_code_channels_own_session(
    bound: dict[str, bool],
) -> None:
    assert mirror_post_thread("C0CODE", "300.000001") is None
    # Another thread of the same channel, another channel, no channel: unchanged.
    assert mirror_post_thread("C0CODE", "999.0") == "999.0"
    assert mirror_post_thread("C0OTHER", "300.000001") == "300.000001"
    assert mirror_post_thread("", "5.0") == "5.0"


def test_mirror_is_unchanged_with_the_toggle_off(bound: dict[str, bool]) -> None:
    bound["on"] = False
    assert mirror_post_thread("C0CODE", "300.000001") == "300.000001"


@pytest.mark.asyncio
async def test_turn_hooks_fire_once_each_with_the_binding(bound: dict[str, bool]) -> None:
    seen: list[tuple[str, str, str, str]] = []

    async def on_start(b: Binding, e: hooks.TurnEvent) -> None:
        seen.append(("start", b.channel_id, e.source, e.outcome))

    async def on_end(b: Binding, e: hooks.TurnEvent) -> None:
        seen.append(("end", b.channel_id, e.source, e.outcome))

    hooks.on_turn_start(on_start)
    hooks.on_turn_start(on_start)  # idempotent
    hooks.on_turn_end(on_end)
    try:
        assert await notify_turn_start("C0CODE", "300.000001", "chat-1-1", source="slack") is True
        await notify_turn_end("C0CODE", "300.000001", "chat-1-1", source="slack", outcome="stopped")
        assert await notify_turn_start("C0OTHER", "1.0", "k", source="dashboard") is False
        bound["on"] = False
        assert await notify_turn_start("C0CODE", "300.000001", "chat-1-1", source="slack") is False
    finally:
        hooks.remove_hook(on_start)
        hooks.remove_hook(on_end)
    assert seen == [("start", "C0CODE", "slack", ""), ("end", "C0CODE", "slack", "stopped")]


@pytest.mark.asyncio
async def test_a_failing_or_stalled_hook_never_breaks_the_turn(
    bound: dict[str, bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks, "HOOK_TIMEOUT_SECS", 0.05)
    ran: list[str] = []

    async def boom(_b: Binding, _e: hooks.TurnEvent) -> None:
        raise RuntimeError("subscriber bug")

    async def stall(_b: Binding, _e: hooks.TurnEvent) -> None:
        await asyncio.Event().wait()  # never set: only the hook timeout ends it

    async def after(_b: Binding, _e: hooks.TurnEvent) -> None:
        ran.append("after")

    for hook in (boom, stall, after):
        hooks.on_turn_start(hook)
    try:
        assert await notify_turn_start("C0CODE", "300.000001", "chat-1-1", source="slack") is True
    finally:
        for hook in (boom, stall, after):
            hooks.remove_hook(hook)
    assert ran == ["after"]


# ── Native status ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_is_processing_during_a_turn_and_active_after(bound: dict[str, bool]) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def api_call(method: str, **kwargs: Any) -> Any:
        calls.append((method, kwargs["json"]))
        return {"ok": True}

    status.set_client_factory(lambda: CodeChannelsClient(api_call))
    hooks.on_turn_start(status._on_start)
    hooks.on_turn_end(status._on_end)
    try:
        await notify_turn_start("C0CODE", "300.000001", "chat-1-1", source="slack")
        assert "C0CODE" in status._refreshers
        await notify_turn_end("C0CODE", "300.000001", "chat-1-1", source="slack", outcome="ok")
        assert "C0CODE" not in status._refreshers
    finally:
        hooks.remove_hook(status._on_start)
        hooks.remove_hook(status._on_end)
        status.set_client_factory(None)
    assert calls == [
        ("agents.sessions.setStatus", {"channel_id": "C0CODE", "status": "processing"}),
        ("agents.sessions.setStatus", {"channel_id": "C0CODE", "status": "active"}),
    ]


@pytest.mark.asyncio
async def test_status_write_failure_is_swallowed(bound: dict[str, bool]) -> None:
    class Err(Exception):
        response = {"ok": False, "error": "feature_disabled"}

    async def api_call(method: str, **kwargs: Any) -> Any:
        raise Err()

    status.set_client_factory(lambda: CodeChannelsClient(api_call))
    try:
        assert await status.set_status("C0CODE", "processing") is False
    finally:
        status.set_client_factory(None)


# ── Session settings ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_folder_project_dir_wins_over_the_origin_threads(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    state = SimpleNamespace(
        read_folders=_reader([{"id": "f1", "name": "F", "project_dir": str(project)}])
    )
    monkeypatch.setattr(binding_mod, "_origin_project", lambda ts: "/elsewhere")
    cfg = SimpleNamespace(
        agent=SimpleNamespace(default_agent=""),
        slack=SimpleNamespace(code_channel_defaults=SimpleNamespace(folder="f1")),
    )
    spec = await binding_mod.resolve_session_spec(state, cfg, origin_thread_ts="1.0")
    assert spec.project_dir == str(project.resolve())


@pytest.mark.asyncio
async def test_an_unusable_folder_project_falls_back_to_the_origin_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = SimpleNamespace(
        read_folders=_reader([{"id": "f1", "name": "F", "project_dir": "/no/such/dir"}])
    )
    monkeypatch.setattr(binding_mod, "_origin_project", lambda ts: "/work/origin-repo")
    cfg = SimpleNamespace(
        agent=SimpleNamespace(default_agent=""),
        slack=SimpleNamespace(code_channel_defaults=SimpleNamespace(folder="f1")),
    )
    spec = await binding_mod.resolve_session_spec(state, cfg, origin_thread_ts="1.0")
    assert spec.folder_id == "f1" and spec.project_dir == "/work/origin-repo"


def _reader(folders: list[dict[str, Any]]):
    async def read(fn: Any) -> Any:
        return fn(folders)

    return read


# ── The real dashboard session ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bound_session_is_filed_pinned_titled_persisted_and_linked(tmp_path: Any) -> None:
    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)
    state._folders.append({"id": "f1", "name": "Slack work", "parent_id": "", "order": 0})
    spec = SessionSpec(model="claude-sonnet-4.5", reasoning_effort="high", folder_id="f1")
    slot = await create_bound_session(
        state, spec, title="Fix flaky login test", channel_id="C0CODE", anchor_ts="300.000001"
    )
    assert state.get_slot(slot.key) is slot
    assert slot.folder_id == "f1"
    assert slot.model == "claude-sonnet-4.5" and slot.reasoning_effort == "high"
    assert slot.title == "Fix flaky login test" and slot._titled
    assert isinstance(slot.project, str)  # the dashboard default (may be "" in tests)
    # Slack replies in the channel find this slot through the anchor.
    assert state.get_linked_slot("300.000001") is slot
    # The map write is the debounced, off-loop one, awaited before returning.
    state.sessions.aflush.assert_awaited()
    from kiro_crew.dashboard.chat_utils_slots import slot_history_key

    meta = state.conversation_log.get_metadata(slot_history_key(slot))
    assert meta and meta.get("folder_id") == "f1"


@pytest.mark.asyncio
async def test_bound_session_drops_an_unknown_effort(tmp_path: Any) -> None:
    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)
    slot = await create_bound_session(
        state,
        SessionSpec(reasoning_effort="ludicrous"),
        title="t",
        channel_id="C1",
        anchor_ts="1.0",
    )
    assert slot.reasoning_effort in ("", None)
    assert slot.folder_id in ("", None)
