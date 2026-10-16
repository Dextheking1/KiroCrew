"""Tests for turning a saved Coder profile into a prepared remote session."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import session_environment as se
from kiro_crew.coder import client as client_mod
from kiro_crew.coder import remote_worker, session_browse, session_launch
from kiro_crew.coder.client import CoderClientError, CoderWorkspace
from kiro_crew.coder.registry import WorkspaceBindingRegistry
from kiro_crew.config import live, paths
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.handlers import session_environments as handlers
from kiro_crew.dashboard.handlers import terminal
from kiro_crew.platform_compat import file_lock
from kiro_crew.remote_session import workspace_tools as wt
from kiro_crew.session_allocation_parts import get_or_create
from kiro_crew.session_environment import SessionEnvironmentUnavailable
from kiro_crew.testing.clock import ManualClock

URL = "https://coder.example"


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), 30))

    return wrapper


def _config(**overrides):
    profile = SimpleNamespace(id="dev", name="Dev", template="micro", workdir="/workspace/app")
    values = {"coder_url": URL, "profiles": [profile]}
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeCoder:
    def __init__(self, *, owner="devuser", owner_id="u-1", workspaces=None, signed_in=True):
        self.owner, self.owner_id, self.signed_in = owner, owner_id, signed_in
        self.workspaces = dict(workspaces or {})
        self.calls: list[str] = []

    async def current_user(self):
        if not self.signed_in:
            raise CoderClientError("signed out")
        return self.owner, self.owner_id

    async def get_workspace(self, name):
        self.calls.append(f"get {name}")
        return self.workspaces.get(name)

    async def create_workspace(self, *, name, template, preset, stop_after_minutes):
        self.calls.append(f"create {name} {template} {stop_after_minutes}m")
        ws = CoderWorkspace(f"uuid-{name}", name, self.owner_id, template, "running", "")
        self.workspaces[name] = ws
        return ws

    async def start_workspace(self, name):
        self.calls.append(f"start {name}")
        ws = self.workspaces[name]
        self.workspaces[name] = CoderWorkspace(ws.uuid, name, ws.owner, ws.template, "running", "")
        return self.workspaces[name]


def _scripted(*outcomes):
    """An async stand-in answering each call with the next outcome, raising exceptions."""
    queue = iter(outcomes)

    async def answer(*_args, **_kwargs):
        value = next(queue)
        if isinstance(value, Exception):
            raise value
        return value

    return answer


def _envs(tmp_path, fake, config=None):
    return session_launch.CoderSessionEnvironments(
        config or _config(), tmp_path, coder_bin="coder", client_factory=lambda b, u: fake
    )


def _home(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)


def _choices(tmp_path):
    return session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json")


async def _remote(tmp_path, monkeypatch, key):
    """A prepared remote ``key`` on the live config; returns the workspace name."""
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(session_environments=_config()))
    monkeypatch.setattr(session_launch, "resolve_coder_bin", lambda: "coder")
    fake = FakeCoder()
    prepared = await _envs(tmp_path, fake).prepare(key, "dev")
    monkeypatch.setattr(session_launch.CoderSessionEnvironments, "_client", lambda self: fake)
    _choices(tmp_path).set(key, "dev")
    return prepared.workspace


@sync
async def test_first_prepare_reserves_then_creates_with_autostop(tmp_path):
    fake = FakeCoder()
    prepared = await _envs(tmp_path, fake).prepare("sess-a", "dev")
    assert prepared.workdir == "/workspace/app"
    assert prepared.workspace.startswith("crewenv-devuser-")
    assert any(c.startswith("create ") and c.endswith(" 60m") for c in fake.calls)
    assert prepared.launch.shell.argv("true")[:6] == [
        "coder",
        "--url",
        URL,
        "ssh",
        "--disable-autostart",
        prepared.workspace,
    ]
    binding = _envs(tmp_path, fake).registry.get_by_session("sess-a")
    assert binding.workspace_uuid == f"uuid-{prepared.workspace}" and binding.state == "running"


@sync
async def test_second_prepare_reuses_and_starts_a_stopped_workspace(tmp_path):
    fake = FakeCoder()
    first = await _envs(tmp_path, fake).prepare("sess-a", "dev")
    fake.workspaces[first.workspace] = replace(fake.workspaces[first.workspace], status="stopped")
    fake.calls.clear()
    second = await _envs(tmp_path, fake).prepare("sess-a", "dev")
    assert second.workspace == first.workspace
    assert fake.calls == [f"get {first.workspace}", f"start {first.workspace}"]


@pytest.mark.parametrize(
    "field, value, reason",
    [("owner", "mallory", "another Coder user"), ("uuid", "other-uuid", "names another")],
)
@sync
async def test_refuses_a_workspace_whose_identity_changed(tmp_path, field, value, reason):
    fake = FakeCoder()
    first = await _envs(tmp_path, fake).prepare("sess-a", "dev")
    fake.workspaces[first.workspace] = replace(fake.workspaces[first.workspace], **{field: value})
    with pytest.raises(SessionEnvironmentUnavailable, match=reason):
        await _envs(tmp_path, fake).prepare("sess-a", "dev")


@sync
async def test_rebound_workspace_is_refused_before_start(tmp_path):
    for field, value, reason in [
        ("uuid", "replacement", "names another"),
        ("owner", "another-user", "another Coder user"),
        ("template", "another-template", "different template"),
    ]:
        for status in ("stopped", "failed", "canceled"):
            fake = FakeCoder()
            envs = _envs(tmp_path / field / status, fake)
            first = await envs.prepare("sess-a", "dev")
            replacement = replace(fake.workspaces[first.workspace], status=status, **{field: value})
            fake.workspaces[first.workspace] = replacement
            fake.calls.clear()
            with pytest.raises(SessionEnvironmentUnavailable, match=reason):
                await envs.prepare("sess-a", "dev")
            assert fake.calls == [f"get {first.workspace}"]
            assert fake.workspaces[first.workspace] == replacement


@sync
async def test_workspace_identity_is_checked_again_after_start(tmp_path, monkeypatch):
    fake = FakeCoder()
    envs = _envs(tmp_path, fake)
    first = await envs.prepare("sess-a", "dev")
    original = fake.workspaces[first.workspace]
    fake.workspaces[first.workspace] = replace(original, status="stopped")
    start = AsyncMock(return_value=replace(original, uuid="replacement"))
    monkeypatch.setattr(fake, "start_workspace", start)
    with pytest.raises(SessionEnvironmentUnavailable, match="names another"):
        await envs.prepare("sess-a", "dev")
    start.assert_awaited_once_with(first.workspace)
    assert envs.registry.get_by_session("sess-a").workspace_uuid == original.uuid


@sync
async def test_never_recreates_a_workspace_deleted_outside_crew(tmp_path):
    fake = FakeCoder()
    first = await _envs(tmp_path, fake).prepare("sess-a", "dev")
    del fake.workspaces[first.workspace]
    with pytest.raises(SessionEnvironmentUnavailable, match="deleted outside"):
        await _envs(tmp_path, fake).prepare("sess-a", "dev")


@sync
async def test_signed_out_and_unknown_profile_and_no_url_are_clear_errors(tmp_path):
    with pytest.raises(SessionEnvironmentUnavailable, match="coder login"):
        await _envs(tmp_path, FakeCoder(signed_in=False)).prepare("s", "dev")
    with pytest.raises(SessionEnvironmentUnavailable, match="not configured"):
        await _envs(tmp_path, FakeCoder()).prepare("s", "nope")
    with pytest.raises(SessionEnvironmentUnavailable, match="Coder URL"):
        await _envs(tmp_path, FakeCoder(), _config(coder_url="")).prepare("s", "dev")


def test_choices_round_trip_owner_only_and_fail_closed(tmp_path):
    path = tmp_path / "coder" / "session-choices.json"
    choices = session_launch.SessionEnvironmentChoices(path)
    assert choices.get("k") is None
    choices.set("k", "dev")
    assert choices.get("k") == "dev"
    if hasattr(os, "getuid"):
        assert path.stat().st_mode & 0o077 == 0
    choices.set("k", None)
    assert choices.get("k") is None
    with pytest.raises(ValueError):
        choices.set("k", "bad id; rm")
    path.write_text("{not json")
    with pytest.raises(SessionEnvironmentUnavailable):
        choices.get("k")


@sync
async def test_prepare_for_session_is_none_without_a_choice(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _home(monkeypatch, tmp_path)
    assert await session_launch.prepare_for_session("sess-a") is None
    assert await session_launch.prepare_for_session(None) is None


@sync
async def test_pool_bypass_reads_the_choice_and_fails_closed(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    assert await get_or_create._session_runs_remotely("k") is False
    _choices(tmp_path).set("k", "dev")
    assert await get_or_create._session_runs_remotely("k") is True
    assert await get_or_create._session_runs_remotely("other") is False
    (tmp_path / "coder" / "session-choices.json").write_text(json.dumps({"version": 9}))
    assert await get_or_create._session_runs_remotely("other") is True


@sync
async def test_login_runner_drops_an_empty_token(tmp_path, monkeypatch):
    seen = {}

    async def fake_run(self, argv, env, cwd):
        seen.update(env)
        assert cwd == tmp_path
        return b"ok"

    monkeypatch.setattr(session_launch.CoderClient, "_run", fake_run)
    await session_launch._login_runner(
        ["coder"], {"CODER_SESSION_TOKEN": "", "CODER_URL": URL}, tmp_path
    )
    assert "CODER_SESSION_TOKEN" not in seen and seen["CODER_URL"] == URL


@sync
async def test_login_runner_accepts_coder_json_larger_than_the_guest_cap(tmp_path):
    # `coder list` JSON can exceed the 64 KB guest cap; the Coder client's bound applies.
    big = 200 * 1024
    out = await session_launch._login_runner(
        [sys.executable, "-c", f"import sys; sys.stdout.write('x' * {big})"], {}, tmp_path
    )
    assert len(out) == big > remote_worker._OUTPUT_MAX
    with pytest.raises(remote_worker.RemoteWorkerError, match="too much data"):
        await remote_worker.run_collect([sys.executable, "-c", f"print('x' * {big})"], b"", None)


@pytest.mark.parametrize("returncode", [0, 7])
@sync
async def test_login_runner_discards_stderr_and_bounds_stdout(
    tmp_path, monkeypatch, caplog, returncode
):
    secret = b"unlabelled-credential-canary"
    stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
    stdout.feed_data(b"x" * 1024)
    stdout.feed_eof()
    stderr.feed_data(secret * 1024)
    stderr.feed_eof()
    process = SimpleNamespace(
        stdout=stdout,
        stderr=stderr,
        wait=AsyncMock(return_value=returncode),
        returncode=returncode,
        communicate=AsyncMock(return_value=(b"x" * 1024, secret * 1024)),
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(client_mod, "create_subprocess_limited", spawn)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(client_mod, "_OUTPUT_MAX_BYTES", 64)
    drains = []
    original_drain = session_launch.CoderClient._drain_bounded

    async def drain(stream, limit):
        result = await original_drain(stream, limit)
        drains.append((stream, limit, result))
        return result

    monkeypatch.setattr(session_launch.CoderClient, "_drain_bounded", staticmethod(drain))
    reason = r"failed \(exit 7\)" if returncode else "returned too much data"
    with pytest.raises(CoderClientError, match=reason) as exc:
        await session_launch._login_runner(["coder", "list"], {}, tmp_path)
    assert secret.decode() not in str(exc.value) + caplog.text
    assert exc.value.__cause__ is None
    assert (stderr, 0, (b"", True)) in drains
    assert (stdout, 64, (b"x" * 64, True)) in drains
    assert stderr.at_eof() and stdout.at_eof()
    assert spawn.call_args.kwargs["cwd"] == str(tmp_path)


@contextlib.asynccontextmanager
async def _client(tmp_path, monkeypatch, *, owner=True):
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        handlers,
        "_profiles",
        lambda: [{"id": "dev", "name": "Dev", "provider": "coder", "workdir": "/w"}],
    )
    monkeypatch.setattr(handlers, "effective_session_key", lambda slot: f"dashboard:{slot.key}")

    async def gate(request, operation):
        return None if owner else web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(handlers, "require_owner_dashboard_request", gate)

    spawned: list[object] = []
    monkeypatch.setattr(
        chat_runner, "schedule_eager_spawn", lambda state, slot: spawned.append(slot)
    )
    slot = SimpleNamespace(key="main", _lock=asyncio.Lock(), _pending_reset_history_key=None)
    app = web.Application()
    app["state"] = SimpleNamespace(_slots={"main": slot})
    app.router.add_get("/s/{slot}", handlers.api_chat_slot_environment_get)
    app.router.add_put("/s/{slot}", handlers.api_chat_slot_environment_put)
    app.router.add_get("/b/{slot}", handlers.api_chat_slot_environment_browse)
    app.router.add_put("/w/{slot}", handlers.api_chat_slot_environment_file_put)
    app.router.add_post("/check", handlers.api_session_environments_check)
    app.router.add_get("/ws", handlers.api_session_environments_workspaces_get)
    client = TestClient(TestServer(app))
    await client.start_server()
    client.slot = slot  # type: ignore[attr-defined]
    client.spawned = spawned  # type: ignore[attr-defined]
    try:
        yield client
    finally:
        await client.close()


async def _env_of(client):
    return await (await client.get("/s/main")).json()


def _choose(client, profile, slot="main"):
    return client.put(f"/s/{slot}", json={"profile": profile})


async def _refused_as_locked(client, profile):
    r = await _choose(client, profile)
    return r.status == 409 and (await r.json())["code"] == "environment_locked"


def _has_a_message(slot):
    slot.total_messages, slot.messages = 1, [{"role": "user"}]


@sync
async def test_endpoint_round_trip_and_validation(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch) as client:
        assert (await _env_of(client))["profile"] is None
        r = await _choose(client, "dev")
        assert r.status == 200 and (await r.json())["applies"] == "next_start"
        assert (await _env_of(client))["profile"] == "dev"
        assert (await _choose(client, "nope")).status == 400
        assert (await _choose(client, 5)).status == 400
        assert (await _choose(client, "dev", "ghost")).status == 404
        assert (await _choose(client, None)).status == 200
        assert (await _env_of(client))["profile"] is None


@sync
async def test_a_changed_choice_resets_the_idle_runtime_and_prewarms_only_local(
    tmp_path, monkeypatch
):
    async with _client(tmp_path, monkeypatch) as client:
        slot, spawned = client.slot, client.spawned
        # Local -> remote resets and prewarms nothing (that would start billed compute).
        assert (await _choose(client, "dev")).status == 200
        assert slot._pending_reset_history_key == "dashboard:main" and spawned == []
        # Re-picking the same environment re-arms no reset.
        slot._pending_reset_history_key = None
        assert (await _choose(client, "dev")).status == 200
        assert slot._pending_reset_history_key is None
        # Remote -> local resets and may prewarm.
        assert (await _choose(client, None)).status == 200
        assert slot._pending_reset_history_key == "dashboard:main" and spawned == [slot]


@sync
async def test_switching_to_remote_drops_a_local_prewarm(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch) as client:
        prewarm = asyncio.ensure_future(asyncio.Event().wait())
        client.slot._eager_spawn_task = prewarm
        removed: list[str] = []
        sessions = SimpleNamespace(
            has_session=lambda key: True, remove=AsyncMock(side_effect=removed.append)
        )
        client.server.app["state"].sessions = sessions
        assert (await _choose(client, "dev")).status == 200
        assert prewarm.cancelled() and removed == ["dashboard:main"]


def _verified_workspace(tmp_path, session_key):
    registry = WorkspaceBindingRegistry(tmp_path / "coder" / "session-envs.json")
    binding = registry.allocate(
        session_key,
        template="micro",
        preset="",
        prefix="crewenv",
        owner_name="devuser",
        deployment_id=URL,
    )
    registry.replace(replace(binding, workspace_uuid="uuid-1", state="running"))


@sync
async def test_environment_is_fixed_once_the_session_has_a_message(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch) as client:
        slot = client.slot
        assert (await _choose(client, "dev")).status == 200
        _verified_workspace(tmp_path, "dashboard:main")
        _has_a_message(slot)
        body = await _env_of(client)
        assert body["locked"] is True and body["profile"] == "dev"
        assert await _refused_as_locked(client, None)
        # Re-asserting the current choice is not a change, so not refused.
        assert (await _choose(client, "dev")).status == 200
        assert (await _env_of(client))["profile"] == "dev"


@sync
async def test_a_remote_session_whose_workspace_never_started_can_move_here(tmp_path, monkeypatch):
    """Every turn failed before compute, so the session is not stuck remote."""
    async with _client(tmp_path, monkeypatch) as client:
        slot = client.slot
        assert (await _choose(client, "dev")).status == 200
        _has_a_message(slot)
        assert (await _env_of(client))["locked"] is False
        assert (await _choose(client, None)).status == 200
        # Back on this machine with messages: fixed again, both ways.
        body = await _env_of(client)
        assert body["profile"] is None and body["locked"] is True
        assert await _refused_as_locked(client, "dev")


@sync
async def test_an_unreadable_registry_keeps_the_environment_fixed(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch) as client:
        slot = client.slot
        assert (await _choose(client, "dev")).status == 200
        (tmp_path / "coder" / "session-envs.json").write_text("{not json")
        _has_a_message(slot)
        assert await _refused_as_locked(client, None)


@sync
async def test_endpoint_is_owner_only(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch, owner=False) as client:
        assert (await client.get("/s/main")).status == 403
        assert (await _choose(client, "dev")).status == 403
        assert not (tmp_path / "coder" / "session-choices.json").exists()


def test_start_progress_reports_stage_and_total_elapsed_then_clears():
    se.clear_start_stage("sess-p")
    assert se.start_progress("sess-p") is None
    se.report_start_stage("sess-p", "checking")
    se.report_start_stage("sess-p", "bogus")  # unknown stages are ignored
    se.report_start_stage("", "checking")  # keyless reports are ignored
    assert se.start_progress("sess-p")["stage"] == "checking"
    se.report_start_stage("sess-p", "provisioning")
    progress = se.start_progress("sess-p")
    assert progress["stage"] == "provisioning"
    assert progress["elapsed_s"] >= progress["stage_elapsed_s"] >= 0
    se.clear_start_stage("sess-p")
    assert se.start_progress("sess-p") is None


def test_start_progress_lists_every_stage_entered_even_between_polls():
    """A stage can finish between polls; a skipped stage is one never entered."""
    key = "sess-entered"
    se.clear_start_stage(key)
    try:
        for stage in ("checking", "connecting", "connecting", "starting_agent"):
            se.report_start_stage(key, stage)
        progress = se.start_progress(key)
        assert progress["stage"] == "starting_agent"
        # In order, once each; provisioning never ran.
        assert progress["entered"] == ["checking", "connecting", "starting_agent"]
    finally:
        se.clear_start_stage(key)
    # A new start begins a new list.
    se.report_start_stage(key, "checking")
    try:
        assert se.start_progress(key)["entered"] == ["checking"]
    finally:
        se.clear_start_stage(key)


def test_a_start_for_a_session_already_attached_here_is_a_reconnect():
    key = "sess-reconnect"
    se.clear_start_stage(key)
    se.forget_remote_workspace(key)
    try:
        se.report_start_stage(key, "checking")
        assert se.start_progress(key)["reconnect"] is False  # first connection
        se.remember_remote_workspace(key, "Dev", "/w")  # attached mid-start
        se.report_start_stage(key, "starting_agent")
        assert se.start_progress(key)["reconnect"] is False  # decided at start
        se.clear_start_stage(key)
        se.report_start_stage(key, "checking")  # a later restart reconnects
        assert se.start_progress(key)["reconnect"] is True
    finally:
        se.clear_start_stage(key)
        se.forget_remote_workspace(key)


def test_start_progress_is_bounded():
    for i in range(se._START_PROGRESS_MAX + 10):
        se.report_start_stage(f"leak-{i}", "checking")
    try:
        assert len(se._start_progress) <= se._START_PROGRESS_MAX
    finally:
        for i in range(se._START_PROGRESS_MAX + 10):
            se.clear_start_stage(f"leak-{i}")


@sync
async def test_prepare_reports_provisioning_only_when_compute_must_start(tmp_path, monkeypatch):
    fake = FakeCoder()
    seen: list[str] = []
    monkeypatch.setattr(session_launch, "report_start_stage", lambda key, stage: seen.append(stage))
    await _envs(tmp_path, fake).prepare("sess-q", "dev")  # creates
    assert seen == ["provisioning"]
    seen.clear()
    await _envs(tmp_path, fake).prepare("sess-q", "dev")  # already running
    assert seen == []


@sync
async def test_endpoint_reports_a_start_in_flight(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch) as client:
        try:
            assert (await _env_of(client))["starting"] is None
            se.report_start_stage("dashboard:main", "provisioning")
            body = await _env_of(client)
            assert body["starting"]["stage"] == "provisioning"
        finally:
            se.clear_start_stage("dashboard:main")


def test_subagent_inherits_the_root_owner_and_a_local_parent_writes_nothing(tmp_path):
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "choices.json")
    assert choices.inherit("sub-1", "dashboard:local") is None
    assert not (tmp_path / "choices.json").exists()
    choices.set("dashboard:root", "dev")
    assert choices.inherit("sub-1", "dashboard:root") == "dev"
    assert choices.resolve("sub-1") == ("dev", "dashboard:root")
    # A subagent of a subagent lands in the ROOT session's workspace.
    assert choices.inherit("sub-2", "sub-1") == "dev"
    assert choices.resolve("sub-2") == ("dev", "dashboard:root")
    assert choices.get("sub-2") == "dev"
    with pytest.raises(ValueError):
        choices.inherit("dashboard:root", "dashboard:root")


def test_a_malformed_inheritance_row_fails_closed(tmp_path):
    path = tmp_path / "choices.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": {"sub": {"provider": "coder", "configuration": "dev", "parent": 7}},
            }
        )
    )
    with pytest.raises(SessionEnvironmentUnavailable):
        session_launch.SessionEnvironmentChoices(path).resolve("sub")


def test_a_nested_continuation_keeps_the_root_workspace_after_its_parent_ends(tmp_path):
    """Root -> A -> B; A and B finish; B's continuation re-inherits from A."""
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "choices.json")
    choices.set("dashboard:root", "dev")
    assert choices.inherit("subagent:a", "dashboard:root") == "dev"
    assert choices.inherit("subagent:b", "subagent:a") == "dev"
    choices.forget_inherited("subagent:b")
    choices.forget_inherited("subagent:a")
    assert choices.inherit("subagent:b", "subagent:a") == "dev"
    assert choices.resolve("subagent:b") == ("dev", "dashboard:root")


def test_a_finished_subagent_ends_only_its_inherited_row_and_ttl_prunes_it(tmp_path, monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch, session_launch)
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "choices.json")
    assert choices.forget_inherited("sub-1") is False  # no file, no lock dir
    choices.set("dashboard:root", "dev")
    choices.inherit("sub-1", "dashboard:root")
    assert choices.forget_inherited("sub-1") is True
    assert choices.resolve("sub-1") == ("dev", "dashboard:root")  # kept for continuations
    clock.advance(session_launch.ENDED_ROW_TTL_SECS + 1)
    choices.inherit("sub-2", "dashboard:root")  # any write prunes aged-out rows
    assert choices.resolve("sub-1") is None and choices.resolve("sub-2") is not None
    # A session's own choice is never an inherited row.
    assert choices.forget_inherited("dashboard:root") is False
    assert choices.get("dashboard:root") == "dev"


@sync
async def test_forget_subagent_environment_is_best_effort(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    choices = _choices(tmp_path)
    choices.set("dashboard:root", "dev")
    assert await session_launch.inherit_environment_for_subagent("dashboard:root", "sub-9")
    await session_launch.forget_subagent_environment("sub-9")
    assert "ended" in json.loads(choices.path.read_text())["sessions"]["sub-9"]
    choices.path.write_text("{not json")
    await session_launch.forget_subagent_environment("sub-9")  # logged, not raised


@sync
async def test_prepare_waits_for_a_registry_lock_held_by_a_worker_thread(tmp_path):
    """On-loop calls would refuse while a sweeper thread briefly holds the lock."""
    fake = FakeCoder()
    envs = _envs(tmp_path, fake)
    envs.registry.path.parent.mkdir(parents=True, exist_ok=True)
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with envs.registry._open_lock() as fd:
            with file_lock(fd.fileno(), exclusive=True, required=True):
                held.set()
                release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(10)
        asyncio.get_running_loop().call_later(0.3, release.set)
        prepared = await envs.prepare("sess-a", "dev")
    finally:
        release.set()
        holder.join(10)
    assert envs.registry.get_by_session("sess-a").workspace_uuid == f"uuid-{prepared.workspace}"


@sync
async def test_an_inherited_session_prepares_its_owners_workspace(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    _choices(tmp_path).set("dashboard:root", "dev")
    assert await session_launch.inherit_environment_for_subagent("dashboard:root", "sub-9")
    assert not await session_launch.inherit_environment_for_subagent("dashboard:other", "sub-10")
    seen = []

    async def fake_prepare(self, key, profile):
        seen.append((key, profile))
        return "prepared"

    monkeypatch.setattr(session_launch.CoderSessionEnvironments, "prepare", fake_prepare)

    monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(session_environments=_config()))
    assert await session_launch.prepare_for_session("sub-9") == "prepared"
    assert seen == [("dashboard:root", "dev")]
    assert await session_launch.prepare_for_session("sub-10") is None


def test_browse_is_read_only_and_root_confined(tmp_path):
    root = tmp_path / "root"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_bytes(b"print(1)\n")
    ws = wt.Workspace(str(root))
    listing = wt.browse(ws, {"op": "list", "path": "."})
    assert listing["entries"] == [{"name": "src", "dir": True}] and listing["path"] == "."
    read = wt.browse(ws, {"op": "read", "path": "src/a.py"})
    assert read["content"] == "print(1)\n" and read["binary"] is False
    assert "outside the workspace" in wt.browse(ws, {"op": "read", "path": "../x"})["error"]
    # No write/run op exists on this entry point, whatever is asked.
    for op in ("write", "write_file", "run_command", "edit_file"):
        assert wt.browse(ws, {"op": op, "path": "src/a.py", "content": "x"})["error"]
    assert (root / "src" / "a.py").read_text() == "print(1)\n"


@sync
async def test_browse_session_runs_the_guest_browse_through_coder(tmp_path, monkeypatch):
    await _remote(tmp_path, monkeypatch, "dashboard:remote")
    with pytest.raises(session_browse.NotRemote):
        await session_browse.browse_session("dashboard:local", "list", ".")
    seen = {}

    async def fake_run(argv, stdin, env, **limits):
        seen.update(argv=argv, stdin=stdin, limits=limits)
        return b'{"root": "/workspace/app", "path": ".", "entries": []}'

    monkeypatch.setattr(session_browse, "run_collect", fake_run)
    out = await session_browse.browse_session("dashboard:remote", "list", "src/../x; rm -rf /")
    assert out["entries"] == []
    assert seen["argv"][:5] == ["coder", "--url", URL, "ssh", "--disable-autostart"]
    # The path rides stdin as JSON, never the command line.
    assert "rm -rf" not in " ".join(seen["argv"]) and b"rm -rf" in seen["stdin"]
    assert "--browse" in seen["argv"][-1] and "/workspace/app" in seen["argv"][-1]
    with pytest.raises(ValueError):
        await session_browse.browse_session("dashboard:remote", "write", ".")


@sync
async def test_browse_endpoint_maps_failures(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch) as client:
        fake_browse = _scripted(
            session_browse.NotRemote("x"),
            session_browse.WorkspaceNotRunning("stopped"),
            {"error": "outside the workspace"},
            {"root": "/r", "path": ".", "entries": [{"name": "a", "dir": False}]},
        )
        monkeypatch.setattr(session_browse, "browse_session", fake_browse)
        assert (await (await client.get("/b/main")).json())["code"] == "not_remote"
        assert (await (await client.get("/b/main")).json())["code"] == "workspace_stopped"
        assert (await client.get("/b/main?path=..")).status == 400
        body = await (await client.get("/b/main?op=list")).json()
        assert body["entries"][0]["name"] == "a"


@sync
async def test_browse_endpoint_forwards_a_rename_source(tmp_path, monkeypatch):
    from kiro_crew.coder import session_browse

    calls = []

    async def fake_browse(*args):
        calls.append(args[1:])
        return {"root": "/r", "git": True, "output": ""}

    async with _client(tmp_path, monkeypatch) as client:
        monkeypatch.setattr(session_browse, "browse_session", fake_browse)
        await client.get("/b/main?op=git_diff&path=new.txt&from=old.txt")
        await client.get("/b/main?op=git_diff&path=new.txt")
    assert calls == [("git_diff", "new.txt", "old.txt"), ("git_diff", "new.txt", None)]


def test_write_request_saves_refuses_stale_and_stays_in_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_bytes(b"one\n")
    ws = wt.Workspace(str(root))
    assert wt.write_request(ws, {"path": "a.txt", "content": "two\n", "expected": "one\n"})["ok"]
    assert (root / "a.txt").read_text() == "two\n"
    stale = wt.write_request(ws, {"path": "a.txt", "content": "three\n", "expected": "one\n"})
    assert stale["conflict"] is True and (root / "a.txt").read_text() == "two\n"
    assert "outside" in wt.write_request(ws, {"path": "../x", "content": "x"})["error"]
    assert wt.write_request(ws, {"path": "new/b.txt", "content": "b"})["ok"]


def test_git_views_are_read_only_and_handle_non_repos(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    ws = wt.Workspace(str(root))
    assert wt.browse(ws, {"op": "git_status"})["git"] is False
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
    }
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "first"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, env=env)
    (root / "f.txt").write_text("x\n")
    status = wt.browse(ws, {"op": "git_status"})
    assert status["git"] is True and "?? f.txt" in status["output"]
    assert "first" in wt.browse(ws, {"op": "git_log"})["output"]
    assert wt.browse(ws, {"op": "git_bodies", "path": "../outside"})["error"]


def test_git_diff_view_does_not_run_a_repository_textconv(tmp_path):
    import shlex
    import shutil
    import subprocess
    import sys

    from kiro_crew.remote_session import workspace_tools as wt

    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    root = tmp_path / "repo"
    root.mkdir()
    marker = tmp_path / "textconv-ran"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
    }

    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, env=env, cwd=root)

    git("init", "-q")
    (root / ".gitattributes").write_text("*.txt diff=probe\n")
    (root / "f.txt").write_text("one\n")
    git("add", ".")
    git("commit", "-q", "-m", "first")
    script = tmp_path / "probe.py"
    script.write_text(
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
        "sys.stdout.write(open(sys.argv[1]).read())\n"
    )
    git(
        "config", "diff.probe.textconv", f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    )
    (root / "f.txt").write_text("two\n")
    diff = wt.browse(wt.Workspace(str(root)), {"op": "git_bodies", "path": "f.txt"})
    assert diff["git"] is True and diff["old"] == "one\n" and diff["new"] == "two\n"
    assert not marker.exists()


@pytest.mark.parametrize("op", ["git_status", "git_bodies", "git_log"])
def test_git_views_do_not_run_a_repository_filter_driver(tmp_path, op):
    import shlex
    import shutil
    import subprocess
    import sys

    from kiro_crew.remote_session import workspace_tools as wt

    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    root = tmp_path / "repo"
    root.mkdir()
    marker = tmp_path / "filter-ran"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
    }

    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, env=env, cwd=root)

    git("init", "-q")
    (root / ".gitattributes").write_text("*.txt filter=probe\n")
    (root / "f.txt").write_text("one\n")
    git("add", ".")
    git("commit", "-q", "-m", "first")
    script = tmp_path / "probe.py"
    script.write_text(
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
        "sys.stdout.write(sys.stdin.read())\n"
    )
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    for key in ("clean", "smudge"):
        git("config", f"filter.probe.{key}", command)
    git("config", "filter.probe.required", "true")
    (root / "f.txt").write_text("two\n")
    # Bump the mtime so status must re-read the file through the index.
    os.utime(root / "f.txt", (1, 1))
    result = wt.browse(wt.Workspace(str(root)), {"op": op, "path": "f.txt"})
    assert result["git"] is True, result
    assert not marker.exists()


@pytest.mark.parametrize("op,path", [("git_status", ""), ("git_bodies", "sub")])
def test_git_views_never_run_a_submodule_filter_driver(tmp_path, op, path):
    import shlex
    import shutil
    import subprocess
    import sys

    from kiro_crew.remote_session import workspace_tools as wt

    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    marker = tmp_path / "submodule-filter-ran"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "empty-config"),
    }

    def git(cwd, *args):
        subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env, capture_output=True)

    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q")
    (seed / ".gitattributes").write_text("*.txt filter=probe\n")
    (seed / "f.txt").write_text("one\n")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "seed")
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(seed), "sub")
    git(root, "commit", "-q", "-m", "add sub")
    script = tmp_path / "probe.py"
    script.write_text(
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
        "sys.stdout.write(sys.stdin.read())\n"
    )
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    # The driver lives only in the submodule's own config, never the superproject's.
    git(root / "sub", "config", "filter.probe.clean", command)
    (root / "sub" / "f.txt").write_text("two\n")
    os.utime(root / "sub" / "f.txt", (1, 1))
    result = wt.browse(wt.Workspace(str(root)), {"op": op, "path": path})
    assert result["git"] is True, result
    assert not marker.exists()


def _plain_git_repo(tmp_path):
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "empty-config"),
    }

    def git(cwd, *args):
        return subprocess.run(
            ["git", "-C", str(cwd), *args], check=True, env=env, capture_output=True
        ).stdout.decode()

    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    return root, git


def test_a_diff_of_a_key_with_a_middle_line_removed_shows_no_key_line(tmp_path):
    import base64
    import secrets

    from kiro_crew.coder.session_browse import _git_diff_from_bodies
    from kiro_crew.platform import redact_via_context
    from kiro_crew.remote_session import workspace_tools as wt

    root, git = _plain_git_repo(tmp_path)
    # Random text in PEM framing, wrapped at 32 columns; never a real key.
    blob = base64.b64encode(secrets.token_bytes(600)).decode()
    body = [blob[i : i + 32] for i in range(0, len(blob), 32)]
    pem = ["-----BEGIN RSA PRIVATE KEY-----", *body, "-----END RSA PRIVATE KEY-----"]
    (root / "id_rsa").write_text("\n".join(pem) + "\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "key")
    del pem[len(pem) // 2]
    (root / "id_rsa").write_text("\n".join(pem) + "\n")
    # The trigger is real: git's own patch carries header-free key lines that
    # the shared redactor, read a line at a time, leaves untouched.
    raw = git(root, "diff", "HEAD", "--", "id_rsa")
    hunk = [line[1:] for line in raw.split("\n") if line[:1] in " -" and line[1:] in body]
    assert hunk and all(redact_via_context(line) == line for line in hunk)

    bodies = wt.browse(wt.Workspace(str(root)), {"op": "git_bodies", "path": "id_rsa"})
    output = _git_diff_from_bodies(bodies, "id_rsa")["output"]
    assert not [line for line in body if line in output]


def test_an_untracked_nested_repository_is_a_failed_diff_not_an_empty_one(tmp_path):
    from kiro_crew.remote_session import workspace_tools as wt

    root, git = _plain_git_repo(tmp_path)
    (root / "f.txt").write_text("one\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "seed")
    nested = root / "nested"
    nested.mkdir()
    git(nested, "init", "-q")
    (nested / "x.txt").write_text("content\n")
    ws = wt.Workspace(str(root))
    assert "?? nested/" in wt.browse(ws, {"op": "git_status"})["output"]
    result = wt.browse(ws, {"op": "git_bodies", "path": "nested/"})
    assert result["git"] is False and result["code"] == "git_failed", result
    assert "output" not in result


def test_git_reads_never_lazy_fetch_a_missing_promisor_blob(tmp_path, monkeypatch):
    import subprocess
    import sys

    from kiro_crew.remote_session import workspace_tools as wt

    seed, git = _plain_git_repo(tmp_path)
    try:
        git(seed, "--no-lazy-fetch", "config", "--list")
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 129 and b"unknown option" in exc.stderr:
            pytest.skip("git 2.44 or later is required for the no-lazy-fetch flag")
        raise
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-config"))
    monkeypatch.setenv("GIT_NO_LAZY_FETCH", "0")
    (seed / "f.txt").write_text("one\n", encoding="utf-8")
    git(seed, "add", "f.txt")
    git(seed, "commit", "-q", "-m", "seed")
    sha = git(seed, "rev-parse", "HEAD:f.txt").strip()
    git(seed, "config", "uploadpack.allowFilter", "true")
    root = tmp_path / "partial"
    git(
        seed,
        "-c",
        "protocol.file.allow=always",
        "clone",
        "--filter=blob:none",
        "--no-checkout",
        seed.as_uri(),
        str(root),
    )
    git(root, "read-tree", "HEAD")
    assert f"?{sha}" in git(root, "rev-list", "--objects", "--all", "--missing=print")
    (root / "f.txt").write_text("two\n", encoding="utf-8")
    marker = tmp_path / "lazy-fetch-ran"
    script = tmp_path / "probe.py"
    script.write_text(
        "import pathlib\n" + f"pathlib.Path({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    # remote-ext escapes spaces with %, not shell quoting; no network is used.
    command = " ".join(
        s.replace("%", "%%").replace(" ", "% ") for s in (sys.executable, str(script))
    )
    git(root, "config", "remote.origin.url", f"ext::{command}")
    git(root, "config", "protocol.ext.allow", "always")
    with pytest.raises(subprocess.CalledProcessError) as absent:
        git(root, "--no-lazy-fetch", "cat-file", "blob", sha)
    assert absent.value.returncode == 128
    assert b"bad file" in absent.value.stderr
    assert not marker.exists()

    ws = wt.Workspace(str(root))
    for op in ("git_bodies", "git_status", "git_log"):
        result = wt.browse(ws, {"op": op, "path": "f.txt"})
        assert not marker.exists(), f"{op} ran the promisor transport"
        if op == "git_bodies":
            assert result["git"] is False and result["code"] == "git_failed", result
            assert "bad file" in result["error_text"]
            assert not {"old", "new", "output"}.intersection(result)
        else:
            assert result["git"] is True, result
            assert result["output"]


@pytest.mark.parametrize("op", ["git_status", "git_log", "git_bodies"])
@pytest.mark.parametrize(
    "key,value",
    [
        (None, None),
        ("extensions.partialClone", "origin"),
        ("remote.origin.promisor", "true"),
        ("remote.origin.promisor", "false"),
        ("remote.another.partialclonefilter", "blob:none"),
        ("remote.another.partialclonefilter", ""),
    ],
)
def test_old_git_refuses_only_bodies_in_a_partial_clone(tmp_path, monkeypatch, op, key, value):
    from kiro_crew.remote_session import workspace_tools as wt

    root, git = _plain_git_repo(tmp_path)
    (root / "f.txt").write_text("one\n", encoding="utf-8")
    git(root, "add", "f.txt")
    git(root, "commit", "-q", "-m", "seed")
    (root / "f.txt").write_text("two\n", encoding="utf-8")
    if key is not None:
        git(root, "config", key, value)
    real_run = wt._run_grouped
    calls = []
    envs = []

    def old_git(argv, **kwargs):
        calls.append(argv)
        envs.append(dict(kwargs["env"]))
        assert kwargs["env"]["GIT_NO_LAZY_FETCH"] == "1"
        if "--no-lazy-fetch" in argv:
            assert "config" in argv
            return SimpleNamespace(
                returncode=129, stdout=b"", stderr=b"unknown option: --no-lazy-fetch\n"
            )
        return real_run(argv, **kwargs)

    monkeypatch.setattr(wt, "_run_grouped", old_git)
    result = wt.browse(wt.Workspace(str(root)), {"op": op, "path": "f.txt"})
    assert sum("--no-lazy-fetch" in argv for argv in calls) == 1
    if key is not None and op == "git_bodies":
        assert result["git"] is False and result["code"] == "git_failed", result
        assert "git 2.44 or later is needed" in result["error_text"]
        assert len(calls) == 2 and all("config" in argv for argv in calls)
        return
    assert result["git"] is True, result
    if op == "git_bodies":
        assert (result["old"], result["new"]) == ("one\n", "two\n")
    elif op == "git_status":
        assert " M f.txt\0" in result["output"]
        assert ("--no-renames" in calls[-1]) is (key is not None)
    else:
        assert result["output"].endswith("\tseed")
    # Every read after the probe runs with lazy fetching's transport refused.
    assert all((env.get("GIT_ALLOW_PROTOCOL") == "") is (key is not None) for env in envs[2:])
    assert envs[2:]


def _old_git_without_lazy_fetch_guards(wt, monkeypatch):
    real_run = wt._run_grouped

    def old_git(argv, **kwargs):
        if "--no-lazy-fetch" in argv:
            return SimpleNamespace(
                returncode=129, stdout=b"", stderr=b"unknown option: --no-lazy-fetch\n"
            )
        # Git before 2.44 ignores GIT_NO_LAZY_FETCH as well as lacking the flag.
        env = {k: v for k, v in kwargs.pop("env").items() if k != "GIT_NO_LAZY_FETCH"}
        return real_run(argv, env=env, **kwargs)

    monkeypatch.setattr(wt, "_run_grouped", old_git)


@pytest.mark.parametrize("filter_spec", ["blob:none", "tree:0"])
def test_old_git_in_a_partial_clone_never_lazy_fetches(tmp_path, monkeypatch, filter_spec):
    import sys

    from kiro_crew.remote_session import workspace_tools as wt

    seed, git = _plain_git_repo(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-config"))
    (seed / "a.txt").write_text("one\ntwo\nthree\nfour\nfive\nsix\n", encoding="utf-8")
    git(seed, "add", "a.txt")
    git(seed, "commit", "-q", "-m", "first")
    (seed / "a.txt").write_text("one\ntwo\nthree\nfour\nfive\nsix\nseven\n", encoding="utf-8")
    git(seed, "commit", "-q", "-am", "second")
    git(seed, "config", "uploadpack.allowFilter", "true")
    root = tmp_path / "partial"
    git(
        seed,
        "-c",
        "protocol.file.allow=always",
        "clone",
        "-q",
        f"--filter={filter_spec}",
        seed.as_uri(),
        str(root),
    )
    # HEAD now names the first commit, whose blob (and, in a treeless clone,
    # tree) this clone never fetched; the index keeps the second commit's.
    git(root, "reset", "-q", "--soft", "HEAD~1")
    git(root, "mv", "a.txt", "b.txt")
    with (root / "b.txt").open("a", encoding="utf-8") as handle:
        handle.write("eight\n")
    git(root, "add", "b.txt")
    marker = tmp_path / "lazy-fetch-ran"
    script = tmp_path / "probe.py"
    script.write_text(
        "import pathlib\n" + f"pathlib.Path({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    # remote-ext escapes spaces with %, not shell quoting; no network is used.
    command = " ".join(
        s.replace("%", "%%").replace(" ", "% ") for s in (sys.executable, str(script))
    )
    git(root, "config", "remote.origin.url", f"ext::{command}")
    git(root, "config", "protocol.ext.allow", "always")
    _old_git_without_lazy_fetch_guards(wt, monkeypatch)
    ws = wt.Workspace(str(root))

    status = wt.browse(ws, {"op": "git_status"})
    assert not marker.exists()
    if filter_spec == "blob:none":
        # Without rename detection status needs only trees, which it has.
        assert status["git"] is True, status
        assert "\0D  a.txt\0A  b.txt\0" in status["output"]
    else:
        # The missing HEAD tree is refused, not fetched.
        assert status["git"] is False and status["code"] == "git_failed", status
    log = wt.browse(ws, {"op": "git_log"})
    assert log["git"] is True and log["output"].endswith("\tfirst"), log
    assert wt.browse(ws, {"op": "git_bodies", "path": "b.txt"})["code"] == "git_failed"
    assert not marker.exists()


def test_old_git_refuses_a_failed_partial_clone_config_check(tmp_path, monkeypatch):
    from kiro_crew.remote_session import workspace_tools as wt

    calls = []

    def old_git(argv, **kwargs):
        calls.append(argv)
        assert "config" in argv
        if "--no-lazy-fetch" in argv:
            return SimpleNamespace(
                returncode=129, stdout=b"", stderr=b"unknown option: --no-lazy-fetch\n"
            )
        return SimpleNamespace(returncode=128, stdout=b"", stderr=b"bad config")

    monkeypatch.setattr(wt, "_run_grouped", old_git)
    result = wt.browse(wt.Workspace(str(tmp_path)), {"op": "git_status"})
    assert result["git"] is False and result["code"] == "git_failed"
    assert result["error_text"] == "git partial clone config not checked"
    assert len(calls) == 2


def test_a_filter_driver_that_cannot_be_named_stops_the_read(tmp_path):
    import shutil
    import subprocess

    from kiro_crew.remote_session import workspace_tools as wt

    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    # A driver name holding "=" cannot be overridden through `-c`.
    subprocess.run(
        ["git", "-C", str(root), "config", "filter.a=b.clean", "touch pwned"], check=True
    )
    result = wt.browse(wt.Workspace(str(root)), {"op": "git_status"})
    assert result == {
        "root": str(root),
        "git": False,
        "code": "git_failed",
        "error_text": "git filter config not checked",
    }


@sync
async def test_write_endpoint_maps_conflict_and_success(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.sel.sel", lambda: SimpleNamespace(log_api_access=lambda **k: None)
    )
    async with _client(tmp_path, monkeypatch) as client:
        fake_write = _scripted(
            {"error": "the file changed since it was opened", "conflict": True},
            {"ok": True, "path": "a.txt"},
            ValueError("bad path"),
            session_browse.NotRemote("x"),
            session_browse.WorkspaceNotRunning("stopped"),
            SessionEnvironmentUnavailable("down"),
            {"error": "outside the workspace"},
        )
        monkeypatch.setattr(session_browse, "write_session_file", fake_write)
        body = {"path": "a.txt", "content": "x", "expected": "y"}
        r = await client.put("/w/main", json=body)
        assert r.status == 409 and (await r.json())["code"] == "file_changed"
        assert (await (await client.put("/w/main", json=body)).json())["ok"] is True
        assert (await client.put("/w/main", json={"path": 3, "content": "x"})).status == 400
        assert (await client.put("/w/main", json={**body, "expected": 5})).status == 400
        mapped = [(400, "invalid_request"), (409, "not_remote"), (409, "workspace_stopped")]
        mapped += [(502, "unavailable"), (400, "path_refused")]
        for status, code in mapped:
            r = await client.put("/w/main", json=body)
            assert (r.status, (await r.json())["code"]) == (status, code)


class ProbingCoder(FakeCoder):
    async def probe(self, *, template, preset):
        if template != "micro":
            raise CoderClientError(f"no template {template}")


@sync
async def test_check_endpoint_reports_sign_in_and_each_profile(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch) as client:
        fake = ProbingCoder(signed_in=False)
        gpu = SimpleNamespace(id="gpu", name="GPU", template="gpu", workdir="/w")
        configs = iter(
            [_config(coder_url=""), _config(), _config(profiles=[_config().profiles[0], gpu])]
        )
        monkeypatch.setattr(
            KiroCrewConfig, "load", lambda: SimpleNamespace(session_environments=next(configs))
        )
        monkeypatch.setattr(session_launch.CoderSessionEnvironments, "_client", lambda self: fake)
        # Git login rows come from their own reader; pinned here so this test reads
        # only the Coder half (the stubbed config iterator feeds the Coder reads).
        git = {"github.com": "signed_in"}
        monkeypatch.setattr(
            "kiro_crew.remote_session.git_gateway.git_readiness", AsyncMock(return_value=git)
        )
        body = await (await client.post("/check")).json()
        assert body == {
            "coder": {"ok": False, "error": "no Coder URL is saved"},
            "profiles": [],
            "git": git,
        }
        body = await (await client.post("/check")).json()
        assert "coder login" in body["coder"]["error"] and body["profiles"] == []
        fake.signed_in = True
        body = await (await client.post("/check")).json()
        assert body["coder"] == {"ok": True, "owner": "devuser"}
        assert body["profiles"] == [
            {"id": "dev", "ok": True},
            {"id": "gpu", "ok": False, "error": "no template gpu"},
        ]


@sync
async def test_check_endpoint_reports_a_missing_coder_cli_instead_of_failing(tmp_path, monkeypatch):
    def missing(self):
        raise SessionEnvironmentUnavailable("the coder CLI is not installed on this machine")

    async with _client(tmp_path, monkeypatch) as client:
        monkeypatch.setattr(
            KiroCrewConfig, "load", lambda: SimpleNamespace(session_environments=_config())
        )
        monkeypatch.setattr(session_launch.CoderSessionEnvironments, "_client", missing)
        resp = await client.post("/check")
        assert resp.status == 200
        body = await resp.json()
        assert body["coder"]["ok"] is False and "not installed" in body["coder"]["error"]
        assert body["profiles"] == []


@sync
async def test_workspaces_endpoint_lists_owned_rows_and_survives_a_corrupt_registry(
    tmp_path, monkeypatch
):
    async with _client(tmp_path, monkeypatch) as client:
        _verified_workspace(tmp_path, "dashboard:chat-7")
        _verified_workspace(tmp_path, "slack:T1")
        rows = (await (await client.get("/ws")).json())["workspaces"]
        assert sorted(r["slot"] for r in rows) == ["", "chat-7"]
        assert {r["state"] for r in rows} == {"running"} and rows[0]["template"] == "micro"
        assert all(r["workspace"].startswith("crewenv-") for r in rows)
        (tmp_path / "coder" / "session-envs.json").write_text("{not json")
        assert (await (await client.get("/ws")).json()) == {"workspaces": []}


@sync
async def test_remote_terminal_argv_is_coder_ssh_without_autostart(tmp_path, monkeypatch):
    workspace = await _remote(tmp_path, monkeypatch, "dashboard:t")
    with pytest.raises(session_browse.NotRemote):
        await session_browse.remote_terminal_argv("dashboard:local")
    argv = await session_browse.remote_terminal_argv("dashboard:t")
    assert argv == ["coder", "--url", URL, "ssh", "--disable-autostart", workspace]


@sync
async def test_terminal_remote_slot_never_falls_back_to_a_local_shell(tmp_path, monkeypatch):
    async def not_remote(key):
        raise session_browse.NotRemote(key)

    request = SimpleNamespace(
        app={"state": SimpleNamespace(_slots={"main": SimpleNamespace(key="main")})}
    )
    argv, why = await terminal._remote_terminal_argv(request, "ghost")
    assert argv is None and "Unknown chat" in why
    monkeypatch.setattr(session_browse, "remote_terminal_argv", not_remote)
    argv, why = await terminal._remote_terminal_argv(request, "main")
    assert argv is None and "runs on this machine" in why

    async def remote(key):
        return ["coder", "ssh", "--disable-autostart", "crewenv-x"]

    monkeypatch.setattr(session_browse, "remote_terminal_argv", remote)
    argv, why = await terminal._remote_terminal_argv(request, "main")
    assert argv == ["coder", "ssh", "--disable-autostart", "crewenv-x"] and why == ""


def test_the_browse_mode_has_no_metrics_op(tmp_path):
    assert wt.browse(wt.Workspace(str(tmp_path)), {"op": "stats"}) == {
        "error": "op must be list, read, git_status, git_log or git_bodies"
    }


@sync
async def test_unsaved_work_on_a_stopped_workspace_is_unknown_and_never_starts_it(
    tmp_path, monkeypatch
):
    async def fake_resolve(key):
        return session_browse._Target(SimpleNamespace(workspace="crewenv-x"), "/w", "b", tmp_path)

    requests = []

    async def stopped(target, mode, request):
        requests.append((mode, request))
        raise session_browse.WorkspaceNotRunning("stopped")

    monkeypatch.setattr(session_browse, "_resolve", fake_resolve)
    monkeypatch.setattr(session_browse, "_ask", stopped)
    assert await session_browse.unsaved_work("k") is None
    # One read-only git view, nothing that could start compute.
    assert requests == [("browse", {"op": "git_status", "path": "."})]

    async def unreachable(target, mode, request):
        raise SessionEnvironmentUnavailable("the remote workspace did not answer")

    monkeypatch.setattr(session_browse, "_ask", unreachable)
    assert await session_browse.unsaved_work("k") is None

    async def running(target, mode, request):
        return {"git": True, "output": "## main...origin/main [ahead 2]\n M a.py\n"}

    monkeypatch.setattr(session_browse, "_ask", running)
    assert await session_browse.unsaved_work("k") == "1 uncommitted file, 2 unpushed commits"


@pytest.mark.parametrize("deleted", [False, True])
def test_git_blob_reads_stay_bounded_and_send_no_over_cap_body(tmp_path, monkeypatch, deleted):
    import subprocess

    from kiro_crew.remote_session import workspace_tools as wt

    root, git = _plain_git_repo(tmp_path)
    body = b"a whole line\n" * wt.MAX_READ_BYTES
    path = root / "large.txt"
    path.write_bytes(body)
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "large blob")
    if deleted:
        path.unlink()
    real_popen = subprocess.Popen
    reads, killed, spawned = [], [], []
    stop_read = wt._stop_read

    class BoundedPipe:
        def __init__(self, stream):
            self.stream = stream
            self.total = 0

        def read(self, size=-1):
            assert 0 < size <= wt.MAX_READ_BYTES + 1 - self.total
            chunk = self.stream.read(size)
            self.total += len(chunk)
            reads.append(self.total)
            return chunk

        def close(self):
            self.stream.close()

    class BlobProcess(real_popen):
        def __init__(self, argv, **kwargs):
            super().__init__(argv, **kwargs)
            self.is_blob = "cat-file" in argv and "blob" in argv
            if self.is_blob:
                spawned.append((argv, kwargs))
                self.stdout = BoundedPipe(self.stdout)

        def communicate(self, *args, **kwargs):
            assert not self.is_blob, "cat-file must not buffer its whole blob in communicate"
            return super().communicate(*args, **kwargs)

    def kill(proc):
        if proc.is_blob:
            killed.append(proc.returncode)
        stop_read(proc)

    monkeypatch.setattr(wt.subprocess, "Popen", BlobProcess)
    monkeypatch.setattr(wt, "_stop_read", kill)
    result = wt.browse(wt.Workspace(str(root)), {"op": "git_bodies", "path": "large.txt"})
    assert result["git"] is True and result["truncated"] is True
    assert reads and max(reads) == wt.MAX_READ_BYTES + 1
    assert killed == [None]
    assert "old" not in result and "new" not in result
    assert result["old_mode"] == "100644"
    assert result["new_mode"] == (None if deleted else "100644")
    assert len(spawned) == 1
    argv, kwargs = spawned[0]
    assert "--literal-pathspecs" in argv and "core.fsmonitor=" in argv
    assert kwargs["env"]["GIT_NO_LAZY_FETCH"] == "1"
    assert kwargs["start_new_session"] is True and kwargs["cwd"] == str(root)


@pytest.mark.parametrize("op", ["git_log", "git_status"])
def test_a_huge_history_or_status_read_is_streamed_to_the_cap(tmp_path, monkeypatch, op):
    import subprocess

    from kiro_crew.remote_session import workspace_tools as wt

    root, git = _plain_git_repo(tmp_path)
    huge = "s" * (2 * wt.MAX_READ_BYTES)
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    git(root, "add", ".")
    # One commit subject, or one untracked file list, larger than the read cap.
    (tmp_path / "message").write_text(huge, encoding="utf-8")
    git(root, "commit", "-q", "-F", str(tmp_path / "message"))
    for i in range(2 * wt.MAX_READ_BYTES // 200):
        (root / f"untracked-{i:05d}-{'n' * 180}").write_text("", encoding="utf-8")
    real_popen = subprocess.Popen
    buffered = []

    class Streamed(real_popen):
        def communicate(self, *args, **kwargs):
            buffered.append(self.args)
            return super().communicate(*args, **kwargs)

    monkeypatch.setattr(wt.subprocess, "Popen", Streamed)
    result = wt.browse(wt.Workspace(str(root)), {"op": op})
    assert result["git"] is True and result["truncated"] is True, result
    assert 0 < len(result["output"].encode()) <= wt.MAX_READ_BYTES
    # Only the small config probe may buffer; the final read is streamed and capped.
    assert not any(("log" if op == "git_log" else "status") in argv for argv in buffered)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX guest workspaces")
def test_a_guest_with_timeout_but_no_git_reports_git_missing(tmp_path, monkeypatch):
    import shutil

    from kiro_crew.remote_session import workspace_tools as wt

    timer = shutil.which("timeout")
    if timer is None:
        pytest.skip("needs coreutils timeout")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "timeout").symlink_to(timer)
    monkeypatch.setenv("PATH", str(bin_dir))
    for op in ("git_status", "git_log", "git_bodies"):
        result = wt.browse(wt.Workspace(str(tmp_path)), {"op": op, "path": "a.txt"})
        assert result["code"] == "git_failed", result
        assert result["error_text"] == "git is not installed", result


def test_git_blob_stream_timeout_stops_before_reaping(tmp_path, monkeypatch):
    import io
    import subprocess
    import threading

    from kiro_crew.remote_session import workspace_tools as wt
    from kiro_crew.testing.ids import UNALLOCATABLE_PID

    events = []
    release = threading.Event()

    class WaitingPipe(io.BytesIO):
        def read(self, size=-1):
            assert size == wt.MAX_READ_BYTES + 1
            assert release.wait(30), "group cleanup did not release the pipe reader"
            return b""

    def wait(timeout):
        events.append("reaped")
        process.returncode = -9
        return -9

    process = SimpleNamespace(
        pid=UNALLOCATABLE_PID,
        returncode=None,
        stdout=WaitingPipe(),
        stderr=io.BytesIO(),
        wait=wait,
    )

    def kill(proc):
        assert proc is process and proc.returncode is None
        events.append("stopped")
        release.set()

    def spawn(argv, **kwargs):
        assert kwargs["start_new_session"] is True
        return process

    # Without coreutils timeout the read's own deadline is the only one.
    monkeypatch.setattr(wt.shutil, "which", lambda name: None)
    monkeypatch.setattr(wt.subprocess, "Popen", spawn)
    monkeypatch.setattr(wt, "_stop_read", kill)
    with pytest.raises(subprocess.TimeoutExpired):
        wt._run_grouped(
            ["git", "cat-file", "blob", "a" * 40],
            cwd=str(tmp_path),
            env={},
            timeout=0,
            stderr=subprocess.PIPE,
            stdout_limit=wt.MAX_READ_BYTES + 1,
        )
    assert events == ["stopped", "reaped"]
    assert process.stdout.closed and process.stderr.closed


def _over_cap_lines(prefix, count):
    return b"".join(f"{prefix}-{i:04d} ".encode() + b"x" * 84 + b"\n" for i in range(count))


@pytest.mark.parametrize("change", ["insert", "shrink", "both"])
def test_an_over_cap_tracked_file_sends_no_body_and_no_hunks(tmp_path, change):
    from kiro_crew.coder.session_browse import _git_diff_from_bodies
    from kiro_crew.remote_session import workspace_tools as wt

    root, git = _plain_git_repo(tmp_path)
    old = _over_cap_lines("original", 2000 if change == "insert" else 3000)
    new = {
        "insert": _over_cap_lines("inserted", 50) + old,
        "shrink": old[: old.index(b"original-0010")],
        "both": _over_cap_lines("replaced", 3000),
    }[change]
    assert len(old) + len(new) > wt.MAX_READ_BYTES
    (root / "large.txt").write_bytes(old)
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "seed")
    (root / "large.txt").write_bytes(new)
    result = wt.browse(wt.Workspace(str(root)), {"op": "git_bodies", "path": "large.txt"})
    assert result["truncated"] is True and result["git"] is True
    assert result["path"] == "large.txt"
    assert (result["old_mode"], result["new_mode"]) == ("100644", "100644")
    assert "old" not in result and "new" not in result
    view = _git_diff_from_bodies(result, "large.txt")
    assert view["truncated"] is True and view["output"] == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_an_over_cap_file_still_shows_its_mode_and_rename_header(tmp_path):
    from kiro_crew.remote_session import workspace_tools as wt

    body = _over_cap_lines("original", 3000).decode()
    root, git = _committed_file(tmp_path, "a.sh", body)
    git(root, "mv", "a.sh", "b.sh")
    (root / "b.sh").write_text(body + "tail\n", encoding="utf-8")
    (root / "b.sh").chmod(0o755)
    result = wt.browse(
        wt.Workspace(str(root)), {"op": "git_bodies", "path": "b.sh", "from": "a.sh"}
    )
    assert result["truncated"] is True and result["old_path"] == "a.sh"
    patch = _gateway_diff(root, "b.sh", "a.sh")
    assert patch == (
        "diff --git a/a.sh b/b.sh\nold mode 100644\nnew mode 100755\n"
        "rename from a.sh\nrename to b.sh\n"
    )
    (root / "added.txt").write_text(body, encoding="utf-8")
    assert _gateway_diff(root, "added.txt") == (
        "diff --git a/added.txt b/added.txt\nnew file mode 100644\n"
    )


def test_a_truncated_reply_carrying_bodies_builds_no_hunks():
    from kiro_crew.coder.session_browse import _git_diff_from_bodies

    old = "".join(f"line {i}\n" for i in range(20))
    reply = {"git": True, "path": "f", "truncated": True, "old": old, "new": old[:40]}
    view = _git_diff_from_bodies(reply, "f")
    assert view == {"git": True, "truncated": True, "output": ""}
    reply.update(old_mode="100644", new_mode=None)
    output = _git_diff_from_bodies(reply, "f")["output"]
    assert output == "diff --git a/f b/f\ndeleted file mode 100644\n"
    assert "line" not in output


def _committed_file(tmp_path, name="tracked.sh", body="echo hi\n"):
    root, git = _plain_git_repo(tmp_path)
    (root / name).write_text(body, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "seed")
    return root, git


def _gateway_diff(root, path, source=None):
    from kiro_crew.coder.session_browse import _git_diff_from_bodies
    from kiro_crew.remote_session import workspace_tools as wt

    request = {"op": "git_bodies", "path": path}
    if source is not None:
        request["from"] = source
    result = wt.browse(wt.Workspace(str(root)), request)
    assert result["git"] is True, result
    return _git_diff_from_bodies(result, path)["output"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_a_mode_only_change_shows_git_mode_lines(tmp_path):
    import subprocess

    root, git = _committed_file(tmp_path)
    (root / "tracked.sh").chmod(0o755)
    patch = _gateway_diff(root, "tracked.sh")
    assert patch == "diff --git a/tracked.sh b/tracked.sh\nold mode 100644\nnew mode 100755\n"
    real = subprocess.run(
        ["git", "-C", str(root), "diff", "--no-color", "HEAD", "--", "tracked.sh"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert real.startswith(patch)
    (root / "tracked.sh").write_text("echo bye\n", encoding="utf-8")
    patch = _gateway_diff(root, "tracked.sh")
    assert "old mode 100644\nnew mode 100755\n" in patch
    assert "-echo hi\n+echo bye\n" in patch


def test_a_mode_change_git_ignores_is_not_shown(tmp_path):
    root, git = _committed_file(tmp_path)
    git(root, "config", "core.fileMode", "false")
    (root / "tracked.sh").chmod(0o755)
    assert _gateway_diff(root, "tracked.sh") == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_new_and_deleted_files_name_their_mode(tmp_path):
    root, git = _committed_file(tmp_path)
    (root / "added.sh").write_text("x\n", encoding="utf-8")
    (root / "added.sh").chmod(0o755)
    assert "\nnew file mode 100755\n--- /dev/null\n+++ b/added.sh\n" in _gateway_diff(
        root, "added.sh"
    )
    (root / "tracked.sh").unlink()
    assert "\ndeleted file mode 100644\n--- a/tracked.sh\n+++ /dev/null\n" in _gateway_diff(
        root, "tracked.sh"
    )


def test_a_renamed_file_diffs_against_its_source(tmp_path):
    body = "".join(f"line {i}\n" for i in range(20))
    root, git = _committed_file(tmp_path, "a.txt", body)
    git(root, "mv", "a.txt", "b.txt")
    (root / "b.txt").write_text(body.replace("line 7\n", "line seven\n"), encoding="utf-8")
    # Without the source the HEAD side is absent: a whole-file addition.
    assert "--- /dev/null" in _gateway_diff(root, "b.txt")
    patch = _gateway_diff(root, "b.txt", "a.txt")
    assert patch.startswith(
        "diff --git a/a.txt b/b.txt\nrename from a.txt\nrename to b.txt\n"
        "--- a/a.txt\n+++ b/b.txt\n"
    ), patch
    changed = [line for line in patch.splitlines()[5:] if line[:1] in "+-"]
    assert changed == ["-line 7", "+line seven"]
    # A pure rename is still a difference.
    (root / "b.txt").write_text(body, encoding="utf-8")
    assert _gateway_diff(root, "b.txt", "a.txt") == (
        "diff --git a/a.txt b/b.txt\nrename from a.txt\nrename to b.txt\n"
    )


def test_a_rename_source_is_confined_to_the_workspace(tmp_path):
    from kiro_crew.remote_session import workspace_tools as wt

    root, git = _committed_file(tmp_path, "a.txt", "x\n")
    ws = wt.Workspace(str(root))
    for source in ("../outside.txt", str(tmp_path / "outside.txt")):
        result = wt.browse(ws, {"op": "git_bodies", "path": "a.txt", "from": source})
        assert "outside" in result["error"] and "old" not in result, result
    refused = wt.browse(ws, {"op": "git_bodies", "path": "a", "from": 1})
    assert "from must be a path" in refused["error"]
    # A source HEAD does not hold reads the target's own entry.
    result = wt.browse(ws, {"op": "git_bodies", "path": "a.txt", "from": "gone.txt"})
    assert "old_path" not in result and result["old"] == "x\n"


@pytest.mark.parametrize(
    "attributes,autocrlf,normalise",
    [
        ("* text eol=crlf", "false", True),
        ("* text", "false", True),
        ("* text=auto", "false", True),
        ("* eol=crlf", "false", True),
        ("* eol=lf", "false", True),
        ("", "true", True),
        ("", "input", True),
        ("* -text eol=crlf", "true", False),
        ("", "false", False),
    ],
)
def test_git_body_crlf_normalisation_matches_git(
    tmp_path, monkeypatch, attributes, autocrlf, normalise
):
    from kiro_crew.coder.session_browse import _git_diff_from_bodies
    from kiro_crew.remote_session import workspace_tools as wt

    root, git = _plain_git_repo(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-config"))
    git(root, "config", "core.autocrlf", autocrlf)
    (root / ".gitattributes").write_text(attributes + "\n", encoding="utf-8")
    (root / "f.txt").write_bytes(b"one\ntwo\nthree\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "seed")
    (root / "f.txt").write_bytes(b"one\r\nchanged\r\nthree\r\n")
    result = wt.browse(wt.Workspace(str(root)), {"op": "git_bodies", "path": "f.txt"})
    expected = "one\nchanged\nthree\n" if normalise else "one\r\nchanged\r\nthree\r\n"
    assert result["new"] == expected
    assert result["old"] == "one\ntwo\nthree\n" and result["truncated"] is False
    patch = _git_diff_from_bodies(result, "f.txt")["output"]
    if normalise:
        changes = [
            s
            for s in patch.splitlines()
            if s[:1] in ("+", "-") and not s.startswith(("+++", "---"))
        ]
        assert changes == ["-two", "+changed"]


def test_git_auto_text_keeps_crlf_when_a_nul_is_beyond_binary_probe(tmp_path):
    from kiro_crew.remote_session import workspace_tools as wt

    root, git = _plain_git_repo(tmp_path)
    (root / ".gitattributes").write_bytes(b"* text=auto\n")
    body = b"line\r\n" * 2000 + b"\0tail\r\n"
    (root / "f.txt").write_bytes(body)
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "seed")
    result = wt.browse(wt.Workspace(str(root)), {"op": "git_bodies", "path": "f.txt"})
    assert result["old"] == result["new"] == body.decode()
    assert result["truncated"] is False


@pytest.mark.parametrize("attributes,autocrlf", [("* text=auto", "false"), ("", "true")])
def test_auto_crlf_keeps_a_file_committed_with_crlf_as_git_does(
    tmp_path, monkeypatch, attributes, autocrlf
):
    root, git = _plain_git_repo(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-config"))
    # Commit the CRLF blob first, then turn automatic conversion on.
    (root / "f.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "seed")
    git(root, "config", "core.autocrlf", autocrlf)
    (root / ".gitattributes").write_text(attributes + "\n", encoding="utf-8")
    git(root, "add", ".gitattributes")
    git(root, "commit", "-q", "-m", "attributes")
    (root / "f.txt").write_bytes(b"one\r\nchanged\r\nthree\r\n")
    patch = _gateway_diff(root, "f.txt")
    lines = patch.split("\n")
    removed = [line for line in lines if line.startswith("-") and line != "--- a/f.txt"]
    added = [line for line in lines if line.startswith("+") and line != "+++ b/f.txt"]
    assert removed == ["-two\r"] and added == ["+changed\r"], patch
    real = git(root, "diff", "--no-color", "--numstat", "HEAD", "--", "f.txt")
    assert real.split()[:2] == ["1", "1"]


@pytest.mark.parametrize("on_disk", [True, False])
def test_a_gitlink_replaced_by_staged_files_is_refused_before_any_diff(
    tmp_path, monkeypatch, on_disk
):
    import shutil
    import subprocess

    from kiro_crew.remote_session import workspace_tools as wt

    root, git = _plain_git_repo(tmp_path)
    (root / "keep.txt").write_text("x\n", encoding="utf-8")
    git(root, "add", "keep.txt")
    git(root, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},sub")
    git(root, "commit", "-q", "-m", "gitlink")
    git(root, "rm", "-q", "--cached", "sub")
    (root / "sub").mkdir()
    for i in range(3):
        (root / "sub" / f"f{i}.txt").write_text("text\n" * 10, encoding="utf-8")
    git(root, "add", "sub")
    if not on_disk:
        shutil.rmtree(root / "sub")
    real_popen = subprocess.Popen
    spawned, buffered = [], []

    class Recording(real_popen):
        def __init__(self, argv, **kwargs):
            spawned.append(list(argv))
            super().__init__(argv, **kwargs)

        def communicate(self, *args, **kwargs):
            buffered.append(self.args)
            return super().communicate(*args, **kwargs)

    monkeypatch.setattr(wt.subprocess, "Popen", Recording)
    result = wt.browse(wt.Workspace(str(root)), {"op": "git_bodies", "path": "sub"})
    assert result["code"] == "git_failed", result
    assert result["error_text"] == "a file path is required, not a directory"
    # The subtree's patch is never built, and no body read is buffered whole.
    assert not any("diff" in argv for argv in spawned), spawned
    assert not any("ls-files" in argv for argv in buffered), buffered


@pytest.mark.parametrize("large", [False, True])
def test_a_gitlink_replaced_by_a_file_uses_the_bounded_worktree_read(tmp_path, monkeypatch, large):
    import subprocess

    from kiro_crew.remote_session import workspace_tools as wt

    root, git = _plain_git_repo(tmp_path)
    (root / "keep.txt").write_text("x\n", encoding="utf-8")
    git(root, "add", "keep.txt")
    commit = "1" * 40
    git(root, "update-index", "--add", "--cacheinfo", f"160000,{commit},sub")
    git(root, "commit", "-q", "-m", "gitlink")
    body = b"replacement\n" * (wt.MAX_READ_BYTES if large else 3)
    (root / "sub").write_bytes(body)
    real_popen = subprocess.Popen
    spawned = []

    class Recording(real_popen):
        def __init__(self, argv, **kwargs):
            spawned.append(list(argv))
            super().__init__(argv, **kwargs)

    monkeypatch.setattr(wt.subprocess, "Popen", Recording)
    result = wt.browse(wt.Workspace(str(root)), {"op": "git_bodies", "path": "sub"})
    assert result["git"] is True, result
    assert not any("diff" in argv for argv in spawned), spawned
    assert (result["old_mode"], result["new_mode"]) == ("160000", "100644")
    assert result["truncated"] is large
    if large:
        assert "old" not in result and "new" not in result
    else:
        assert result["old"] == f"Subproject commit {commit}\n"
        assert result["new"] == body.decode()


@pytest.mark.parametrize("side", ["new", "old"])
def test_an_over_cap_file_holding_a_pem_sends_no_key_line(tmp_path, side):
    import base64

    from kiro_crew.coder.session_browse import _git_diff_from_bodies
    from kiro_crew.remote_session import workspace_tools as wt

    root, git = _plain_git_repo(tmp_path)
    blob = base64.b64encode(bytes(range(256)) * 4).decode()
    key_lines = [blob[i : i + 32] for i in range(0, len(blob), 32)]
    ordinary = "ordinary-" + "x" * 110 + "\n"
    prefix = ordinary * ((wt.MAX_READ_BYTES // 2 - 256) // len(ordinary))
    pem = "\n".join(
        "    " + line
        for line in ["-----BEGIN RSA PRIVATE KEY-----", *key_lines, "-----END RSA PRIVATE KEY-----"]
    )
    content = prefix + "  key: |\n" + pem + "\n" + ordinary * (wt.MAX_READ_BYTES // len(ordinary))
    path = root / "config.yaml"
    path.write_text(content, encoding="utf-8")
    if side == "old":
        git(root, "add", "config.yaml")
        git(root, "commit", "-q", "-m", "seed")
        path.unlink()
    bodies = wt.browse(wt.Workspace(str(root)), {"op": "git_bodies", "path": "config.yaml"})
    assert bodies["truncated"] is True
    assert "old" not in bodies and "new" not in bodies
    assert not any(line in json.dumps(bodies) for line in key_lines)
    view = _git_diff_from_bodies(bodies, "config.yaml")
    assert view["truncated"] is True
    assert not any(line in view["output"] for line in key_lines)


@pytest.mark.parametrize("side", ["new", "old"])
def test_an_indented_pem_with_no_footer_in_a_complete_file_shows_no_key_line(
    tmp_path, monkeypatch, side
):
    import base64
    import secrets

    from kiro_crew.coder import session_browse
    from kiro_crew.remote_session import workspace_tools as wt
    from kiro_crew.security import REDACTED_CREDENTIAL_TAG

    root, git = _plain_git_repo(tmp_path)
    blob = base64.b64encode(secrets.token_bytes(768)).decode()
    key_lines = [blob[i : i + 32] for i in range(0, len(blob), 32)]
    content = "config:\n  key: |\n" + "".join(
        "    " + line + "\n" for line in ["-----BEGIN RSA PRIVATE KEY-----", *key_lines]
    )
    path = root / "config.yaml"
    path.write_text(content, encoding="utf-8")
    if side == "old":
        git(root, "add", "config.yaml")
        git(root, "commit", "-q", "-m", "seed")
        path.unlink()
    bodies = wt.browse(wt.Workspace(str(root)), {"op": "git_bodies", "path": "config.yaml"})
    assert bodies["truncated"] is False and bodies[side] == content
    output = session_browse._git_diff_from_bodies(bodies, "config.yaml")["output"]
    assert "config:" in output and REDACTED_CREDENTIAL_TAG in output
    assert not any(line in output for line in key_lines)
    # The shared redactor alone masks a footerless key only through unindented
    # lines, so without the withholding step this complete file leaks.
    monkeypatch.setattr(session_browse, "_withhold_unclosed_pem", lambda body: body)
    leaked = session_browse._git_diff_from_bodies(bodies, "config.yaml")["output"]
    assert any(line in leaked for line in key_lines)


def test_a_complete_pem_uses_whole_body_redaction(tmp_path):
    import base64

    from kiro_crew.coder.session_browse import _git_diff_from_bodies
    from kiro_crew.platform import redact_via_context
    from kiro_crew.remote_session import workspace_tools as wt
    from kiro_crew.security import REDACTED_CREDENTIAL_TAG

    root, _git = _plain_git_repo(tmp_path)
    blob = base64.b64encode(bytes(range(256))).decode()
    key_lines = [blob[i : i + 32] for i in range(0, len(blob), 32)]
    pem = "\n".join(
        ["-----BEGIN RSA PRIVATE KEY-----", *key_lines, "-----END RSA PRIVATE KEY-----"]
    )
    ordinary = "ordinary-" + "x" * 110 + "\n"
    (root / "f").write_text(pem + "\n" + ordinary * 20, encoding="utf-8")
    bodies = wt.browse(wt.Workspace(str(root)), {"op": "git_bodies", "path": "f"})
    assert bodies["truncated"] is False and "-----END RSA PRIVATE KEY-----" in bodies["new"]
    clean = redact_via_context(bodies["new"])
    assert REDACTED_CREDENTIAL_TAG in clean
    output = _git_diff_from_bodies(bodies, "f")["output"]
    assert not any(line in output for line in key_lines)
    assert "+" + ordinary in output
    assert [line[1:] for line in output.splitlines() if line.startswith("+")][
        1:
    ] == clean.splitlines()
