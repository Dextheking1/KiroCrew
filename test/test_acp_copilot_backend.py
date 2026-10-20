"""The Copilot CLI harness: its routing guards, its launch MCP config, its child env.

Copilot CLI 1.0.100 is routed by an ACP v1 session config option (``allow_all``),
which Crew writes ``off`` on every session/new and session/load. Three things hold a
session there afterwards, and each is pinned here against the live corpus under
``test/fixtures/acp_frames/copilot/`` where the corpus carries the frame:

* the prompt-command allowlist refuses a ``/<command>`` the harness would run itself
  before the prompt reaches the wire (``ACP_BACKEND_PROMPT_COMMAND_ALLOWLIST``);
* the mid-session tripwire stops the harness when it reports ``allow_all`` on or the
  ``autopilot`` mode (``ACP_BACKEND_FORBIDDEN_MODES``);
* the child never inherits ``COPILOT_ALLOW_ALL`` or ``COPILOT_ASSISTED_APPROVAL``.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import acp_backends
from kiro_crew.acp.client import AcpClient, AcpToolGateUnroutable
from kiro_crew.acp.harness import copilot as copilot_mod
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.agent_sdk import tool_gate as gate
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_FORBIDDEN_MODES,
    ACP_BACKEND_PROMPT_COMMAND_ALLOWLIST,
    prompt_command_issue,
    session_config_drift_issue,
)

COPILOT = ACP_BACKEND_COPILOT
_CORPUS = Path(__file__).parent / "fixtures" / "acp_frames" / "copilot"
_AUTOPILOT = "https://agentclientprotocol.com/protocol/session-modes#autopilot"
_AGENT = "https://agentclientprotocol.com/protocol/session-modes#agent"


def _frames(name: str) -> list[dict]:
    lines = (_CORPUS / name).read_text().splitlines()
    return [json.loads(line) for line in lines[1:] if line.strip()]


def _updates(name: str, kind: str) -> list[dict]:
    return [
        f
        for f in _frames(name)
        if f.get("method") == "session/update"
        and f["params"]["update"].get("sessionUpdate") == kind
    ]


def _message(frame: dict) -> JsonRpcMessage:
    return JsonRpcMessage(**{k: frame[k] for k in ("method", "params")})


# ── the prompt-command allowlist ─────────────────────────────────────────────

# Each of these was MEASURED turning Copilot CLI 1.0.100's allow_all on, or (the
# zero-width one) is refused on the fail-closed side of the parser's stripping.
_MEASURED_ESCALATIONS = (
    " /allow-all on",
    "\n/allow-all on",
    "/ALLOW-ALL on",
    "/allow-all",
    "/yolo",
    "/Yolo",
    "/permissions allow-all",
    "/allow-all   on",
    "/yolo\ton",
    "\t/yolo",
    "\u00a0/yolo",
    "\u3000/yolo",
    "\u200b/allow-all on",
    "/autopilot on",
)

# Each of these was measured reaching the MODEL as ordinary text.
_MEASURED_PASS_THROUGH = (
    "/yolo/x",
    "/yolo.",
    "/yolo,",
    "//yolo",
    "/ yolo",
    "hello",
    "please read /etc/hosts",
    "/etc/hosts has a typo",
    "",
)


def test_an_underscore_name_is_refused_although_the_harness_did_not_run_it() -> None:
    """``/allow_all on`` reached the model on 1.0.100, but the grammar admits ``_``:
    refusing it costs a resent prompt, admitting a future command spelled that way
    would cost the gate."""
    assert prompt_command_issue(COPILOT, ["/allow_all on"])


@pytest.mark.parametrize("text", _MEASURED_ESCALATIONS)
def test_a_command_that_moves_the_permission_state_is_refused(text: str) -> None:
    issue = prompt_command_issue(COPILOT, [text])
    assert issue, text
    assert "approval mode" in issue


@pytest.mark.parametrize("text", _MEASURED_PASS_THROUGH)
def test_text_the_harness_sends_to_the_model_is_not_refused(text: str) -> None:
    assert prompt_command_issue(COPILOT, [text]) == ""


@pytest.mark.parametrize("name", sorted(ACP_BACKEND_PROMPT_COMMAND_ALLOWLIST[COPILOT]))
def test_every_allowlisted_command_passes_in_any_case(name: str) -> None:
    assert prompt_command_issue(COPILOT, [f"/{name}"]) == ""
    assert prompt_command_issue(COPILOT, [f"  /{name.upper()} with args"]) == ""


def test_a_command_this_build_has_never_seen_fails_closed() -> None:
    assert prompt_command_issue(COPILOT, ["/some-future-command"])


def test_every_text_block_is_checked_not_only_the_first() -> None:
    assert prompt_command_issue(COPILOT, ["look at this", "/yolo"])


def test_the_allowlist_never_admits_a_permission_command() -> None:
    """A guard on the table itself: none of the measured escalation names may be added."""
    allowed = ACP_BACKEND_PROMPT_COMMAND_ALLOWLIST[COPILOT]
    assert not allowed & {
        "allow-all",
        "yolo",
        "permissions",
        "autopilot",
        "mode",
        "clear",
        "plan",
        "research",
    }


@pytest.mark.parametrize("backend", sorted(acp_backends.ACP_BACKENDS_KNOWN - {COPILOT}))
def test_a_backend_without_a_row_is_never_refused(backend: str) -> None:
    assert prompt_command_issue(backend, ["/yolo"]) == ""


def _prompt_client(tmp_path: Path, backend: str) -> tuple[AcpClient, list]:
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    client._process = MagicMock(returncode=None)
    client._session_id = "copilot-session-1"
    sent: list = []

    async def _send(method: str, params: dict) -> int:
        sent.append((method, params))
        return len(sent)

    client._send_request = AsyncMock(side_effect=_send)
    client._settle_orphan_steers = AsyncMock()
    return client, sent


@pytest.mark.asyncio
async def test_a_refused_prompt_never_reaches_the_wire(tmp_path: Path) -> None:
    client, sent = _prompt_client(tmp_path, COPILOT)
    client._turn_done.clear()

    with pytest.raises(AcpError) as raised:
        await client._send_prompt("/yolo")

    assert sent == []
    assert "/yolo" in str(raised.value)
    # Nothing is in flight, so a caller waiting on the turn's end is released.
    assert client._turn_done.is_set()


@pytest.mark.asyncio
async def test_an_allowed_prompt_is_sent(tmp_path: Path) -> None:
    client, sent = _prompt_client(tmp_path, COPILOT)
    await client._send_prompt("/compact")
    assert [method for method, _ in sent] == ["session/prompt"]


@pytest.mark.asyncio
async def test_the_kiro_path_sends_a_slash_prompt_unchanged(tmp_path: Path) -> None:
    """harness-parity H13: the guard is a table lookup the kiro path never matches."""
    client, sent = _prompt_client(tmp_path, acp_backends.ACP_BACKEND_KIRO)
    await client._send_prompt("/yolo")
    assert [method for method, _ in sent] == ["session/prompt"]


# ── the mid-session tripwire ─────────────────────────────────────────────────


def test_the_corpus_shows_the_harness_reporting_allow_all_on() -> None:
    """The premise: the frame the tripwire reads is one Copilot CLI really sends."""
    values = [
        option.get("currentValue")
        for frame in _updates("handshake-live.jsonl", "config_option_update")
        for option in frame["params"]["update"]["configOptions"]
        if option.get("id") == "allow_all"
    ]
    assert "on" in values and "off" in values, values
    modes = [
        f["params"]["update"]["currentModeId"]
        for f in _updates("handshake-live.jsonl", "current_mode_update")
    ]
    assert _AUTOPILOT in modes and _AGENT in modes, modes


def test_allow_all_on_and_autopilot_are_drift_and_the_routed_state_is_not() -> None:
    for frame in _updates("handshake-live.jsonl", "config_option_update"):
        update = frame["params"]["update"]
        values = {o.get("id"): o.get("currentValue") for o in update["configOptions"]}
        drifted = values.get("allow_all") != "off" or values.get("mode") == _AUTOPILOT
        assert bool(session_config_drift_issue(COPILOT, update)) is drifted, values
    for frame in _updates("handshake-live.jsonl", "current_mode_update"):
        update = frame["params"]["update"]
        issue = session_config_drift_issue(COPILOT, update)
        assert bool(issue) is (update["currentModeId"] == _AUTOPILOT)


@pytest.mark.parametrize("backend", sorted(acp_backends.ACP_BACKENDS_KNOWN - {COPILOT}))
def test_drift_is_never_read_for_a_backend_without_a_row(backend: str) -> None:
    update = {"sessionUpdate": "current_mode_update", "currentModeId": _AUTOPILOT}
    assert session_config_drift_issue(backend, update) == ""


def test_every_forbidden_mode_backend_is_session_config_routed() -> None:
    """The tripwire reads the routing table's option, so a row elsewhere would be inert."""
    for backend in ACP_BACKEND_FORBIDDEN_MODES:
        assert gate.routing_for(backend) is gate.Routing.SESSION_CONFIG


def _tripwire_client(backend: str = COPILOT) -> tuple[AcpClient, list]:
    client = AcpClient.__new__(AcpClient)
    client._acp_backend = backend
    client._session_id = "copilot-session-1"
    client._session_key = ""
    killed: list = []

    async def _kill(force: bool = False) -> None:
        killed.append(force)

    client._kill_process = _kill  # type: ignore[method-assign]
    return client, killed


def test_the_tripwire_stops_a_harness_that_turned_allow_all_on() -> None:
    frame = next(
        f
        for f in _updates("handshake-live.jsonl", "config_option_update")
        if any(
            o.get("id") == "allow_all" and o.get("currentValue") == "on"
            for o in f["params"]["update"]["configOptions"]
        )
    )
    client, killed = _tripwire_client()
    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(client._tripwire_session_config(_message(frame)))
    assert killed == [True]


def test_the_tripwire_stops_a_harness_that_entered_autopilot() -> None:
    frame = next(
        f
        for f in _updates("handshake-live.jsonl", "current_mode_update")
        if f["params"]["update"]["currentModeId"] == _AUTOPILOT
    )
    client, killed = _tripwire_client()
    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(client._tripwire_session_config(_message(frame)))
    assert killed == [True]


def test_the_routed_state_reported_again_is_a_no_op() -> None:
    client, killed = _tripwire_client()
    for frame in _updates("turn-live.jsonl", "config_option_update") + _updates(
        "handshake-live.jsonl", "current_mode_update"
    ):
        if frame["params"]["update"].get("currentModeId") == _AUTOPILOT:
            continue
        asyncio.run(client._tripwire_session_config(_message(frame)))
    assert killed == []


@pytest.mark.parametrize("backend", sorted(acp_backends.ACP_BACKENDS_KNOWN - {COPILOT}))
def test_the_tripwire_is_inert_on_every_other_backend(backend: str) -> None:
    frame = {
        "method": "session/update",
        "params": {
            "sessionId": "s",
            "update": {"sessionUpdate": "current_mode_update", "currentModeId": _AUTOPILOT},
        },
    }
    client, killed = _tripwire_client(backend)
    client._kill_process = None  # type: ignore[assignment]  # would raise if reached
    asyncio.run(client._tripwire_session_config(_message(frame)))
    assert killed == []


def test_the_tripwire_runs_on_every_reader_the_goose_one_does() -> None:
    import inspect

    from kiro_crew.acp import client_turn

    source = inspect.getsource(client_turn)
    assert "await self._tripwire_goose_mode(msg)" not in source
    assert source.count("await self._tripwire_permission_state(msg)") >= 3


# ── the session-config route itself ──────────────────────────────────────────


def test_session_new_advertises_the_value_crew_writes() -> None:
    new = next(f for f in _frames("handshake-live.jsonl") if f.get("id") == 2)
    assert gate.session_config_issue(COPILOT, new["result"]["configOptions"]) == ""


def test_session_load_advertises_it_too() -> None:
    load = next(
        f
        for f in _frames("session-load-live.jsonl")
        if isinstance(f.get("result"), dict) and "configOptions" in f["result"]
    )
    assert gate.session_config_issue(COPILOT, load["result"]["configOptions"]) == ""


def test_copilot_reads_routed() -> None:
    verdict, _why = gate.routing_verdict(COPILOT)
    assert verdict is gate.Verdict.ROUTED
    assert gate.is_enforced(COPILOT)


# ── the launch MCP config and the child environment ──────────────────────────


def _stdio(name: str, command: str, **env: str) -> dict:
    return {
        "name": name,
        "command": command,
        "args": ["-m", "x"],
        "env": [{"name": k, "value": v} for k, v in env.items()],
    }


def test_only_stdio_elements_go_to_the_launch_config() -> None:
    document = copilot_mod.copilot_mcp_config(
        [
            _stdio("crew", sys.executable, CREW_TOKEN="t"),
            # A url element stays on the wire even if it also names a command.
            {
                "name": "remote",
                "type": "http",
                "url": "https://example.invalid/mcp",
                "command": sys.executable,
                "headers": [],
            },
            {"name": "", "command": sys.executable},
            "not-an-element",
        ]
    )
    assert document == {
        "mcpServers": {
            "crew": {
                "type": "local",
                "command": sys.executable,
                "args": ["-m", "x"],
                "env": {"CREW_TOKEN": "t"},
                "tools": ["*"],
            }
        }
    }


def test_no_stdio_element_means_no_file() -> None:
    assert copilot_mod.copilot_mcp_config([]) is None
    assert copilot_mod.copilot_mcp_config([{"name": "r", "url": "https://x.invalid"}]) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_the_launch_config_is_owner_only_and_removed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(copilot_mod, "config_dir", lambda: tmp_path)
    path = copilot_mod.write_copilot_mcp_config({"mcpServers": {}})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    assert json.loads(Path(path).read_text()) == {"mcpServers": {}}
    copilot_mod.discard_copilot_mcp_config(path)
    assert not os.path.exists(path)
    copilot_mod.discard_copilot_mcp_config(path)  # already gone is the same outcome


def test_a_config_left_by_a_dead_gateway_is_swept(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(copilot_mod, "config_dir", lambda: tmp_path)
    directory = Path(copilot_mod._config_directory())
    stale = directory / "copilot-mcp-999999999-left.json"
    foreign = directory / "auth-signin-999999999-not-ours.json"
    foreign.write_text("{}")
    stale.write_text("{}")
    monkeypatch.setattr(copilot_mod.platform_compat, "pid_exists", lambda pid: False)
    fresh = copilot_mod.write_copilot_mcp_config({"mcpServers": {}})
    assert not stale.exists()
    # The staging directory is shared with the gateway's own auth-store temps.
    assert foreign.exists()
    assert os.path.exists(fresh)


def test_the_child_never_inherits_a_permission_source() -> None:
    env = {name: "true" for name in copilot_mod.COPILOT_DROPPED_ENV}
    env["PATH"] = "/usr/bin"
    copilot_mod.CopilotLaunch().apply_spawn_env(env)
    assert env == {"PATH": "/usr/bin"}
    assert {"COPILOT_ALLOW_ALL", "COPILOT_CLI_DIST_DIR", "COPILOT_PLAN_THEN_AUTOPILOT"} <= set(
        copilot_mod.COPILOT_DROPPED_ENV
    )


# ── repository configuration Copilot CLI would run unasked ───────────────────


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src" / "pkg").mkdir(parents=True)
    return root


@pytest.mark.parametrize(
    "relative",
    [
        ".github/hooks/sessionStart.json",
        ".github/hooks/probe.json",
        ".github/hooks/Other.JSON",
        ".github/copilot/settings.json",
        ".github/copilot/settings.local.json",
        ".mcp.json",
        ".github/mcp.json",
        ".github/lsp.json",
        ".github/extensions/ext/extension.mjs",
        ".github/plugin/plugin.json",
        ".claude-plugin/plugin.json",
    ],
)
def test_a_project_with_its_own_copilot_config_is_refused(tmp_path, relative: str) -> None:
    root = _repo(tmp_path)
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}")
    # From the root and from a subdirectory: the harness reads the git root's config.
    for cwd in (root, root / "src" / "pkg"):
        issue = copilot_mod.copilot_repo_config_issue(cwd)
        assert issue and "without asking for permission" in issue, cwd
        assert "only to the Copilot CLI harness" in issue


def test_a_project_without_it_starts(tmp_path) -> None:
    root = _repo(tmp_path)
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.json").write_text("{}")
    (root / ".github" / "hooks").mkdir()
    (root / ".github" / "hooks" / "README.md").write_text("not a hook")
    (root / ".github" / "copilot").mkdir()
    (root / ".github" / "copilot" / "instructions.json").write_text("{}")
    assert copilot_mod.copilot_repo_config_issue(root / "src") == ""
    assert copilot_mod.copilot_repo_config_issue("") == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_a_symlinked_ancestor_is_checked_on_both_chains(tmp_path) -> None:
    real = _repo(tmp_path)
    (real / ".mcp.json").write_text("{}")
    link = tmp_path / "link"
    link.symlink_to(real / "src")
    assert copilot_mod.copilot_repo_config_issue(link / "pkg")


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX modes")
def test_an_ancestor_crew_cannot_list_is_not_a_finding(tmp_path) -> None:
    locked = tmp_path / "locked"
    work = locked / "sub" / "work"
    work.mkdir(parents=True)
    locked.chmod(0o311)
    try:
        assert copilot_mod.copilot_repo_config_issue(work) == ""
        # A named file under it is still found: the harness opens it by name too.
        locked.chmod(0o711)
        (locked / ".mcp.json").write_text("{}")
        locked.chmod(0o311)
        assert copilot_mod.copilot_repo_config_issue(work)
    finally:
        locked.chmod(0o755)


def test_the_walk_stops_at_the_git_root(tmp_path) -> None:
    (tmp_path / ".github" / "hooks").mkdir(parents=True)
    (tmp_path / ".github" / "hooks" / "outer.json").write_text("{}")
    root = _repo(tmp_path)
    assert copilot_mod.copilot_repo_config_issue(root / "src") == ""
    # Without a git root it keeps walking, so the outer hooks are found.
    plain = tmp_path / "plain" / "dir"
    plain.mkdir(parents=True)
    assert copilot_mod.copilot_repo_config_issue(plain)


@pytest.mark.asyncio
async def test_a_project_with_repo_hooks_is_refused_before_anything_is_written(
    tmp_path, monkeypatch
) -> None:
    from kiro_crew.acp.harness.base import SpawnContext

    root = _repo(tmp_path)
    (root / ".github" / "hooks").mkdir(parents=True)
    (root / ".github" / "hooks" / "h.json").write_text("{}")
    monkeypatch.setattr(copilot_mod, "config_dir", lambda: tmp_path / "home")
    client = AcpClient(work_dir=root, acp_backend=COPILOT)
    ctx = SpawnContext.__new__(SpawnContext)
    object.__setattr__(ctx, "session", client)
    object.__setattr__(ctx, "work_dir", root)
    object.__setattr__(ctx, "sandbox_mode", "auto")

    with pytest.raises(AcpToolGateUnroutable) as raised:
        await copilot_mod.CopilotLaunch().resolve_spawn(ctx)
    assert "h.json" in str(raised.value)
    assert list((tmp_path / "home").glob("auth-store-staging/copilot-mcp-*")) == []


# ── the review fixes: read-back, drain restore, pre-prompt drift, file lifetime ──


def _options(allow_all: str, mode: str = _AGENT) -> list[dict]:
    new = next(f for f in _frames("handshake-live.jsonl") if f.get("id") == 2)
    options = json.loads(json.dumps(new["result"]["configOptions"]))
    for option in options:
        if option["id"] == "allow_all":
            option["currentValue"] = allow_all
        if option["id"] == "mode":
            option["currentValue"] = mode
    return options


def _routing_client(tmp_path: Path, answer: list[dict]) -> AcpClient:
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    client._acp_config_options = _options("on")

    async def _write(config_id: str, value: str) -> None:
        assert (config_id, value) == ("allow_all", "off")
        client._acp_config_options = answer

    client.set_config_option = _write  # type: ignore[method-assign]
    return client


@pytest.mark.asyncio
async def test_a_write_the_harness_answers_with_allow_all_still_on_is_refused(tmp_path) -> None:
    client = _routing_client(tmp_path, _options("on"))
    with pytest.raises(AcpToolGateUnroutable):
        await client._apply_session_permission_routing()


@pytest.mark.asyncio
async def test_only_config_reports_from_before_the_answer_are_dropped(tmp_path) -> None:
    client = _routing_client(tmp_path, _options("off"))
    stale = _buffered({"sessionUpdate": "config_option_update", "configOptions": _options("on")})
    oauth = JsonRpcMessage(method="_kiro.dev/mcp/oauth_request", params={"serverName": "x"})
    client._mcp_notifications.extend([stale, oauth])
    client._mcp_report_frame_floor = 2
    await client._apply_session_permission_routing()
    live = _buffered({"sessionUpdate": "config_option_update", "configOptions": _options("on")})
    client._mcp_notifications.append(live)

    client._drop_superseded_config_frames()

    # The stale restored report is gone, the OAuth frame stays, the live one stays,
    # and the report floor still marks the same frame (the live one).
    assert client._mcp_notifications == [oauth, live]
    assert client._mcp_report_frame_floor == 1


@pytest.mark.asyncio
async def test_a_mode_report_from_before_the_answer_is_kept(tmp_path) -> None:
    """The allow_all write does not move the mode, so its answer supersedes no mode."""
    client = _routing_client(tmp_path, _options("off"))
    mode = _buffered({"sessionUpdate": "current_mode_update", "currentModeId": _AUTOPILOT})
    client._mcp_notifications.append(mode)
    await client._apply_session_permission_routing()
    client._drop_superseded_config_frames()
    assert client._mcp_notifications == [mode]


@pytest.mark.asyncio
async def test_a_mode_report_the_start_drain_reads_still_stops_the_session(tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    client._session_id = "copilot-session-1"
    client._acp_config_options = _options("off")
    client._mcp_notifications.append(
        _buffered({"sessionUpdate": "current_mode_update", "currentModeId": _AUTOPILOT})
    )
    client._read_message = AsyncMock(return_value=None)
    killed: list = []

    async def _kill(force: bool = False) -> None:
        killed.append(force)

    client._kill_process = _kill  # type: ignore[method-assign]
    await client._drain_notifications(duration=0.05, idle_exit=0.05)
    assert client._mcp_notifications == []
    with pytest.raises(AcpToolGateUnroutable):
        await client._refuse_session_config_drift()
    assert killed == [True]


@pytest.mark.asyncio
async def test_the_kiro_drain_keeps_no_mode_reports(tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=acp_backends.ACP_BACKEND_KIRO)
    client._mcp_notifications.append(
        _buffered({"sessionUpdate": "current_mode_update", "currentModeId": _AUTOPILOT})
    )
    client._read_message = AsyncMock(return_value=None)
    await client._drain_notifications(duration=0.05, idle_exit=0.05)
    assert client._drain_mode_reports == []


def _buffered(update: dict) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update", params={"sessionId": "copilot-session-1", "update": update}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    ["cached-on", "buffered-on", "buffered-autopilot"],
)
async def test_a_move_no_reader_saw_stops_the_next_prompt(tmp_path, state: str) -> None:
    client, sent = _prompt_client(tmp_path, COPILOT)
    client._acp_config_options = _options("off")
    killed: list = []

    async def _kill(force: bool = False) -> None:
        killed.append(force)

    client._kill_process = _kill  # type: ignore[method-assign]
    if state == "cached-on":
        client._acp_config_options = _options("on")
    elif state == "buffered-on":
        client._mcp_notifications.append(
            _buffered({"sessionUpdate": "config_option_update", "configOptions": _options("on")})
        )
    else:
        client._mcp_notifications.append(
            _buffered({"sessionUpdate": "current_mode_update", "currentModeId": _AUTOPILOT})
        )

    with pytest.raises(AcpToolGateUnroutable):
        await client._send_prompt("hello")
    assert sent == [] and killed == [True]


@pytest.mark.asyncio
async def test_the_routed_state_lets_the_prompt_through(tmp_path) -> None:
    client, sent = _prompt_client(tmp_path, COPILOT)
    client._acp_config_options = _options("off")
    client._mcp_notifications.append(
        _buffered({"sessionUpdate": "config_option_update", "configOptions": _options("off")})
    )
    await client._send_prompt("hello")
    assert [method for method, _ in sent] == ["session/prompt"]


@pytest.mark.asyncio
async def test_the_launch_config_goes_when_initialize_answers_or_fails(tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    path = tmp_path / "cfg.json"
    path.write_text("{}")
    client._copilot_mcp_config_path = str(path)
    client._send_request = AsyncMock(return_value=1)
    client._wait_for_response = AsyncMock(side_effect=AcpError("boom"))
    with pytest.raises(AcpError):
        await client._initialize_session()
    assert not path.exists()
    assert client._copilot_mcp_config_path == ""


@pytest.mark.asyncio
async def test_a_refused_spawn_removes_the_config_it_wrote(tmp_path, monkeypatch) -> None:
    from kiro_crew.acp import launch as launch_mod
    from kiro_crew.acp.harness.base import SpawnContext

    monkeypatch.setattr(copilot_mod, "config_dir", lambda: tmp_path)
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)

    async def _resolve(_backend):
        return ("copilot", ["copilot", "--acp"], "copilot", "copilot")

    async def _prepare() -> None:
        return None

    async def _refuse(*_a, **_k):
        raise AcpToolGateUnroutable("no sandbox")

    monkeypatch.setattr(launch_mod, "resolve_self_served_launch", _resolve)
    monkeypatch.setattr(launch_mod, "_run_preflight_bounded", _refuse)
    client._prepare_session_mcp = _prepare  # type: ignore[method-assign]
    client._session_mcp_servers = lambda: [_stdio("crew", sys.executable, T="t")]  # type: ignore
    ctx = SpawnContext.__new__(SpawnContext)
    object.__setattr__(ctx, "session", client)
    object.__setattr__(ctx, "sandbox_mode", "auto")

    with pytest.raises(AcpToolGateUnroutable):
        await copilot_mod.CopilotLaunch().resolve_spawn(ctx)
    assert list((tmp_path / "auth-store-staging").glob("copilot-mcp-*.json")) == []
    assert client._copilot_mcp_config_path == ""


def test_a_spawn_that_never_reached_initialize_leaves_no_config(tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)
    path = tmp_path / "left.json"
    path.write_text("{}")
    client._copilot_mcp_config_path = str(path)
    client._reset_state()
    assert not path.exists() and client._copilot_mcp_config_path == ""


def test_a_git_pointer_git_would_reject_does_not_end_the_walk(tmp_path) -> None:
    (tmp_path / ".github" / "hooks").mkdir(parents=True)
    (tmp_path / ".github" / "hooks" / "outer.json").write_text("{}")
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / ".git").write_text("gitdir: does/not/exist\n")
    assert copilot_mod.copilot_repo_config_issue(inner)
    # A worktree pointer whose target exists is a root, so the outer hooks are not read.
    (tmp_path / "real.git").mkdir()
    (inner / ".git").write_text(f"gitdir: {tmp_path / 'real.git'}\n")
    assert copilot_mod.copilot_repo_config_issue(inner) == ""


def test_a_failed_write_leaves_no_partial_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(copilot_mod, "config_dir", lambda: tmp_path)
    with pytest.raises(TypeError):
        copilot_mod.write_copilot_mcp_config({"mcpServers": {"x": object()}})
    assert list((tmp_path / "auth-store-staging").glob("copilot-mcp-*.json")) == []


@pytest.mark.asyncio
async def test_a_drift_refusal_releases_a_waiter(tmp_path) -> None:
    client, sent = _prompt_client(tmp_path, COPILOT)
    client._acp_config_options = _options("on")

    async def _kill(force: bool = False) -> None:
        return None

    client._kill_process = _kill  # type: ignore[method-assign]
    client._turn_done.clear()
    with pytest.raises(AcpToolGateUnroutable):
        await client._send_prompt("hello")
    assert client._turn_done.is_set() and sent == []


def test_the_frame_path_check_reaches_the_copilot_tripwire() -> None:
    frame = next(
        f
        for f in _updates("handshake-live.jsonl", "current_mode_update")
        if f["params"]["update"]["currentModeId"] == _AUTOPILOT
    )
    client, killed = _tripwire_client()
    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(client._tripwire_permission_state(_message(frame)))
    assert killed == [True]


def test_the_launch_config_lives_in_the_masked_staging_directory(tmp_path, monkeypatch) -> None:
    """Every agent sandbox masks this directory whole and the read gate fences it."""
    from kiro_crew import sandbox_leaves
    from kiro_crew.security.paths import is_sensitive_path

    monkeypatch.setattr(copilot_mod, "config_dir", lambda: tmp_path)
    path = Path(copilot_mod.write_copilot_mcp_config({"mcpServers": {}}))
    try:
        assert path.parent == tmp_path / sandbox_leaves._AUTH_STORE_STAGING_LEAF
        assert path.name.startswith("copilot-mcp-")
    finally:
        copilot_mod.discard_copilot_mcp_config(str(path))
    home = Path.home()
    assert is_sensitive_path(str(home / ".kiro" / "crew" / "auth-store-staging" / path.name))


@pytest.mark.asyncio
async def test_the_spawn_hands_the_config_back_to_this_child_alone(tmp_path, monkeypatch) -> None:
    from kiro_crew.acp import launch as launch_mod
    from kiro_crew.acp.harness.base import SpawnContext

    monkeypatch.setattr(copilot_mod, "config_dir", lambda: tmp_path / "home")
    client = AcpClient(work_dir=tmp_path, acp_backend=COPILOT)

    async def _resolve(_backend):
        return ("copilot", ["copilot", "--acp"], "copilot", "copilot")

    async def _prepare() -> None:
        return None

    async def _mask(*_a, **_k):
        return ("/home/x/.config/gh",)

    monkeypatch.setattr(launch_mod, "resolve_self_served_launch", _resolve)
    monkeypatch.setattr(launch_mod, "_run_preflight_bounded", _mask)
    client._prepare_session_mcp = _prepare  # type: ignore[method-assign]
    client._session_mcp_servers = lambda: [_stdio("crew", sys.executable, T="t")]  # type: ignore
    ctx = SpawnContext.__new__(SpawnContext)
    object.__setattr__(ctx, "session", client)
    object.__setattr__(ctx, "work_dir", tmp_path)
    object.__setattr__(ctx, "sandbox_mode", "auto")

    plan = await copilot_mod.CopilotLaunch().resolve_spawn(ctx)
    try:
        assert plan.extra_expose_files == (client._copilot_mcp_config_path,)
        assert plan.argv[-1] == "@" + client._copilot_mcp_config_path
        assert "auth-store-staging" in client._copilot_mcp_config_path
    finally:
        await client._discard_copilot_mcp_config()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_the_config_path_is_the_resolved_spelling(tmp_path, monkeypatch) -> None:
    """The sandbox carve-out matches the realpath spelling, so the file must use it."""
    real = tmp_path / "real-home"
    real.mkdir()
    link = tmp_path / "linked-home"
    link.symlink_to(real)
    monkeypatch.setattr(copilot_mod, "config_dir", lambda: link)
    path = copilot_mod.write_copilot_mcp_config({"mcpServers": {}})
    try:
        assert path.startswith(str(real.resolve()))
    finally:
        copilot_mod.discard_copilot_mcp_config(path)
