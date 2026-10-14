"""A remote session never gets a tool that acts on the gateway machine.

Its engine runs here, so built-in file/shell tools, set_project, file_send, the
browser panel, pods and computer use are unmounted and refused in band.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import replace

import pytest

from kiro_crew import hooks, mcp_core
from kiro_crew import session_environment as se
from kiro_crew.acp.kas_agents import to_client_custom_agent
from kiro_crew.acp.kas_permissions import _mcp_pattern
from kiro_crew.acp.runtime import (
    _WORKSPACE_TOOLS_BY_BUILTIN,
    AcpRuntime,
    _without_local_only_grants,
)
from kiro_crew.coder.session_launch import SessionEnvironmentChoices
from kiro_crew.computer_use import tools as computer_tools
from kiro_crew.config import paths
from kiro_crew.mcp_tool_titles import _table
from kiro_crew.mcp_tools import artifacts
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.governance import may_skip_gate, parse_policy

WORKSPACE = {"name": "workspace", "command": "bridge"}


def _runtime() -> AcpRuntime:
    runtime = object.__new__(AcpRuntime)
    runtime._remote_workspace = {"server": WORKSPACE, "workdir": "/workspace/app"}
    return runtime


def _servers(*names: str) -> list[dict]:
    return [{"name": n, "command": n} for n in names]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    return tmp_path


def _bind(home, key: str, profile: str | None = "dev") -> None:
    SessionEnvironmentChoices(home / "coder" / "session-choices.json").set(key, profile)


def test_a_whole_core_grant_becomes_every_core_tool_except_the_local_only_ones():
    grants = _without_local_only_grants(["@kirocrew-core", "@kirocrew-cron"])
    core = {g.split("/", 1)[1] for g in grants if g.startswith("@kirocrew-core/")}
    assert core == set(_table()["kirocrew-core"]) - se.LOCAL_ONLY_CORE_TOOLS
    assert core and not core & se.LOCAL_ONLY_CORE_TOOLS
    assert "@kirocrew-cron" in grants


def test_explicit_and_wildcard_grants_cannot_readmit_a_local_only_tool():
    grants = _without_local_only_grants(
        [
            "@kirocrew-core/file_send",
            "@kirocrew-core/memory_recall",
            "@kirocrew-core/b*",
            "@kirocrew-core/pod_*",
            "@kirocrew-computer",
            "@kirocrew-computer/computer_click",
        ]
    )
    assert "@kirocrew-core/memory_recall" in grants
    assert not any(g.endswith(("/file_send", "/browser", "/pod_up", "/pod_down")) for g in grants)
    assert "@kirocrew-core/pod_ls" in grants  # read-only: kept
    assert not any("*" in g for g in grants)
    assert not any(g.startswith("@kirocrew-computer") for g in grants)


def test_agent_tools_drop_builtins_builtin_group_and_local_only_tools():
    runtime = _runtime()
    servers = _servers("kirocrew-core", "kirocrew-computer", "kirocrew-cron")
    wire = runtime._remote_session_servers(servers)
    assert [s["name"] for s in wire] == ["kirocrew-core", "kirocrew-cron", "workspace"]
    [star] = runtime._remote_workspace_agents([{"name": "a", "tools": ["*"]}], wire)
    [listed] = runtime._remote_workspace_agents(
        [
            {
                "name": "b",
                "tools": ["read", "shell", "@builtin", "@kirocrew-core", "@kirocrew-computer"],
            }
        ],
        wire,
    )
    for agent in (star, listed):
        tools = agent["tools"]
        assert "@workspace" in tools
        assert not {"read", "shell", "@builtin", "@kirocrew-core"} & set(tools)
        assert "@kirocrew-core/set_project" not in tools
        assert "@kirocrew-core/spawn_run" in tools
        assert not any(t.startswith("@kirocrew-computer") for t in tools)


def test_a_local_session_is_untouched():
    runtime = object.__new__(AcpRuntime)
    runtime._remote_workspace = None
    servers = _servers("kirocrew-core", "kirocrew-computer")
    agents = [{"name": "a", "tools": ["*"]}]
    assert runtime._remote_session_servers(servers) is servers
    assert runtime._remote_workspace_agents(agents, servers) is agents


def test_refusal_follows_the_choice_record(home):
    assert se.refuse_local_only_tool("dashboard:a", "kirocrew-core", "file_send") is None
    _bind(home, "dashboard:a")
    refusal = se.refuse_local_only_tool("dashboard:a", "kirocrew-core", "file_send")
    assert refusal and "remote workspace" in refusal
    assert se.refuse_local_only_tool("dashboard:a", "kirocrew-core", "memory_recall") is None
    assert se.refuse_local_only_tool("dashboard:a", "kirocrew-computer", "computer_click")
    # A subagent that inherited the environment is remote too.
    SessionEnvironmentChoices(home / "coder" / "session-choices.json").inherit(
        "subagent:x", "dashboard:a"
    )
    assert se.refuse_local_only_tool("subagent:x", "kirocrew-core", "browser")
    _bind(home, "dashboard:a", None)
    assert se.refuse_local_only_tool("dashboard:a", "kirocrew-core", "file_send") is None


def test_an_unreadable_choice_record_refuses(home):
    path = home / "coder" / "session-choices.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    assert se.refuse_local_only_tool("dashboard:a", "kirocrew-core", "set_project")
    assert se.refuse_local_only_tool("", "kirocrew-core", "set_project") is None


def test_core_dispatch_refuses_before_the_handler_runs(home, monkeypatch):
    _bind(home, "dashboard:a")
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:a")
    ran: list[str] = []
    monkeypatch.setattr(mcp_core, "dispatch", lambda name, args: ran.append(name) or "ran")
    monkeypatch.setattr(mcp_core, "_deny_channel_agent_dispatch", lambda name, args: None)
    for tool in sorted(se.LOCAL_ONLY_CORE_TOOLS):
        assert "remote workspace" in mcp_core._call_tool_inner(tool, {})
    # A task spec is a path on this machine; the runner gets local tools.
    assert "remote workspace" in mcp_core._call_tool_inner("task_run", {"spec": "/srv/t.md"})
    assert ran == []
    assert mcp_core._call_tool_inner("memory_recall", {}) == "ran"


@pytest.mark.parametrize("bound", [True, False])
def test_deploy_refuses_a_gateway_directory_only_in_a_remote_session(home, monkeypatch, bound):
    if bound:
        _bind(home, "dashboard:a")
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:a")
    posted: list[dict] = []
    monkeypatch.setattr(mcp_core, "_post", lambda path, body: posted.append(body) or {"ok": True})
    out = artifacts.deploy_artifact("deploy_artifact", {"site_id": "s", "local_dir": "/srv/site"})
    assert ("remote workspace" in out) is bound
    assert bool(posted) is not bound
    # An artifact lives on the gateway on purpose, so its deploy is never refused.
    posted.clear()
    artifacts.deploy_artifact("deploy_artifact", {"site_id": "s", "artifact_slug": "a"})
    assert len(posted) <= 1 and not any("local_dir" in b for b in posted)


def test_computer_use_dispatch_refuses_a_remote_session(home, monkeypatch):
    _bind(home, "dashboard:a")
    name = sorted(computer_tools.MCP_COMPUTER_SCHEMAS)[0]
    out = computer_tools._dispatch(
        name, {}, session_key="dashboard:a", agent="default", app="", approval_recorded=False
    )
    payload = json.loads(out) if out.lstrip().startswith("{") else {"text": out}
    assert "remote workspace" in json.dumps(payload)


@pytest.mark.parametrize(
    "pattern", ["artifact_*_comments", "artifact_get_comment?", "brow?er", "artifact_[gd]*"]
)
def test_core_wildcards_keep_the_complete_requested_pattern(pattern):
    grant = f"@kirocrew-core/{pattern}"
    expected = {
        f"@kirocrew-core/{tool}"
        for tool in set(_table()["kirocrew-core"]) - se.LOCAL_ONLY_CORE_TOOLS
        if _mcp_pattern(grant) is not None and fnmatch.fnmatchcase(tool, pattern)
    }
    assert set(_without_local_only_grants([grant])) == expected
    assert "@kirocrew-core/artifact_delete" not in expected


@pytest.mark.parametrize(
    "excluded,expected",
    [
        ("execute_bash", ["run_command"]),
        ("shell", ["run_command"]),
        ("@builtin/execute_bash", ["run_command"]),
        ("fs_read", ["read_file", "list_directory"]),
        ("read", ["read_file", "list_directory"]),
        ("@builtin/fs_write", ["write_file", "edit_file"]),
        ("write", ["write_file", "edit_file"]),
        ("str_replace", ["write_file", "edit_file"]),
        ("glob", ["glob"]),
        ("grep", ["grep"]),
        ("@builtin", [""]),
    ],
)
def test_remote_builtin_exclusions_cover_workspace_replacements(excluded, expected):
    spec = {"tools": "*", "excludedTools": [excluded, "@other/tool"]}
    [agent] = _runtime()._remote_workspace_agents([spec], _servers("workspace"))
    translated = [f"@workspace/{tool}" if tool else "@workspace" for tool in expected]
    assert agent["excludedTools"] == [excluded, "@other/tool", *translated]
    assert spec["excludedTools"] == [excluded, "@other/tool"]


@pytest.mark.parametrize("ref", ["@workspace", "@workspace/run_command", "@workspace/r*"])
def test_workspace_auto_approval_cannot_skip_the_shell_floor(ref):
    assert not may_skip_gate(ref, None)
    agent = to_client_custom_agent("a", {"tools": ["@workspace"], "allowedTools": [ref]}, "p")
    assert "permissions" not in agent


def _workspace_call(tool, session_key="chat-remote", kind="read", **params):
    se.remember_remote_workspace("chat-remote", "dev", "/workspace/app")
    try:
        return hooks.HookManager().on_tool_call(
            f"@workspace/{tool}",
            session_key=session_key,
            mcp_server_name="workspace",
            mcp_tool_name=tool,
            mcp_identity_trusted=True,
            tool_kind=kind,
            raw_params=params,
        )
    finally:
        se.forget_remote_workspace("chat-remote")


def test_a_local_server_named_workspace_is_not_rekinded_into_a_shell(home):
    def ls(key):
        return _workspace_call("run_command", key, "other", command="ls").action

    # ``ls`` passes the read-only shell tier; a local MCP server gets no such pass.
    assert ls("chat-remote") == hooks.TOOL_AUTO_APPROVE
    assert ls("chat-local") != hooks.TOOL_AUTO_APPROVE
    _bind(home, "chat-bound")  # recorded but not attached in this process: still remote
    assert ls("chat-bound") == hooks.TOOL_AUTO_APPROVE
    (home / "coder" / "session-choices.json").write_text("{", encoding="utf-8")
    assert ls("chat-bound") != hooks.TOOL_AUTO_APPROVE  # unreadable never widens


def _workspace_policy(monkeypatch, **policy):
    ceiling = parse_policy({"version": 1, "boot": {"fail_closed": True}, **policy})
    context = replace(hooks.current_context(), governance=ceiling)
    monkeypatch.setattr(hooks, "current_context", lambda: context)
    monkeypatch.setattr(gp, "resolve_active_scope", lambda *args, **kw: None)


@pytest.mark.parametrize("tool", ["run_command", "write_file", "edit_file", "read_file"])
def test_workspace_scopes_reach_the_gateway_policy(monkeypatch, tmp_path, tool):
    path = str(tmp_path / "target")
    _workspace_policy(
        monkeypatch,
        commands={"mode": "deny", "deny": ["rm -rf *"]},
        filesystem={"write": {"mode": "deny", "deny": [path]}},
    )
    allowed = _workspace_call("run_command", command="printf hello")
    assert allowed.action != hooks.TOOL_DENY
    result = _workspace_call(tool, command="rm -rf build", path=path)
    assert (result.action == hooks.TOOL_DENY) == (tool != "read_file")
    if tool != "read_file":
        assert result.tier == "governance"


@pytest.mark.parametrize(
    "builtin, tool",
    [("execute_bash", "run_command"), ("fs_write", "write_file"), ("fs_read", "read_file")],
)
def test_a_deny_on_the_replaced_builtin_binds_the_workspace_tool(
    monkeypatch, tmp_path, builtin, tool
):
    _workspace_policy(monkeypatch, tools={"mode": "deny", "deny": [builtin]})
    result = _workspace_call(tool, command="printf hello", path=str(tmp_path / "f"))
    assert result.action == hooks.TOOL_DENY and result.tier == "governance"
    assert _workspace_call("grep", pattern="x", path=".").action != hooks.TOOL_DENY

    inverse = {t: (b,) for b, tools in _WORKSPACE_TOOLS_BY_BUILTIN.items() for t in tools}
    assert se.WORKSPACE_DENY_ALIASES == inverse


def test_workspace_shell_meets_the_gateway_deny_floor():
    assert _workspace_call("run_command", command="printf hello").action != hooks.TOOL_DENY
    assert _workspace_call("run_command", command=None).tier == "unverifiable-shell"
    result = _workspace_call("run_command", command="rm -rf /")
    assert result.action == hooks.TOOL_DENY
    assert result.tier and result.tier != "governance"


#: Core tools reviewed as safe for a remote session: they act on gateway state on
#: purpose (artifacts, memory, crons, chats) or take content, never a gateway path.
REMOTE_SAFE_CORE_TOOLS = frozenset("""
artifact_delete artifact_delete_comment artifact_folder_create artifact_folder_delete
artifact_folder_list artifact_folder_move artifact_folder_rename artifact_get
artifact_get_comments artifact_list artifact_mark_review artifact_move artifact_post_comment
artifact_reply_comment artifact_revert artifact_save artifact_update artifact_versions
ask_question autonudge_stop chat_tag delete_message deploy_artifact
design_tweak_update_thread get_chat_session issue_radar_crew_read issue_radar_crew_record
issue_radar_record_investigation kiro_cli_logs knowledge_add_document knowledge_dedup
knowledge_list_sources learn_add learn_list learn_remove list_sessions
local_knowledge_search memory_recall monitor_inspect monitor_start monitor_stop
monitor_update monitor_watch nothing_to_do ops_mission_control_api pod_ls pod_status
read_slack_profile register_hook reset_conversation resource_status route_crew
search_chat_history select_crew send_message send_notification session_ledger_read
session_ledger_record skill_discover skill_fetch skill_search spawn_continue spawn_list
spawn_release spawn_run spawn_status spawn_steer spawn_sub_agents suggest_followup
update_message wait workflow_author workflow_cancel workflow_library_list workflow_list
workflow_result workflow_status
""".split())


def test_every_core_tool_is_classified_for_remote_sessions():
    # A new core tool must be judged local-only or remote-safe, never default in.
    core = set(_table()["kirocrew-core"])
    assert not REMOTE_SAFE_CORE_TOOLS & se.LOCAL_ONLY_CORE_TOOLS
    assert core - se.LOCAL_ONLY_CORE_TOOLS - REMOTE_SAFE_CORE_TOOLS == set()
    assert {"task_run", "workflow_run", "workflow_rerun_subtree"} <= se.LOCAL_ONLY_CORE_TOOLS
