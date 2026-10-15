"""KAS sessions get a per-session token on their kirocrew-core element.

Background: ``AcpRuntime`` on the KAS backend (``acp_backend == "kas"``) is
session-unbound -- one kiro-cli process serves many sessions and carries no
per-session ``KIROCREW_STUB_SESSION_TOKEN`` in its environment.  The per-session
token is the identity channel for managed servers like ``kirocrew-core``; without
it every call is refused ``identity_unattested``.

When ``mcp_gateway.stub_servers`` is empty (the default), no stub entry is
injected into ``session/new`` and ``kirocrew-core`` runs directly with no token.
The fix: ``_unpooled_control_planes`` on the KAS backend calls
``kiro_control_plane_servers`` so a control-plane element -- carrying the managed
invocation and ready to receive the session token from ``_own_stub_session`` --
lands in the ``session/new`` server list regardless of the stub configuration.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.acp import session_mcp
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp_backends import ACP_BACKEND_KAS

MANAGED = {"command": "test-kirocrew", "args": ["mcp-core"]}


@pytest.fixture()
def kas_tree(tmp_path, monkeypatch):
    """A minimal kiro home with a kirocrew agent spec, for KAS-path tests."""
    home = tmp_path / "kiro"
    agents = home / "agents"
    agents.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()

    spec = {
        "name": "kirocrew",
        "tools": ["*"],
        "mcpServers": {"kirocrew-core": MANAGED},
    }
    (agents / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")

    monkeypatch.setattr(session_mcp, "managed_mcp_spec_entry", lambda name, **_kw: dict(MANAGED))
    monkeypatch.setattr(session_mcp, "_registry_mode", lambda: False)
    monkeypatch.setattr(session_mcp, "_agent_spec_for", lambda agent, *a, **k: spec)
    monkeypatch.setattr(
        session_mcp,
        "native_settings_sources",
        lambda *a, **k: [],  # empty: no settings files to check
    )
    monkeypatch.setattr(
        session_mcp,
        "native_mount_withholding",
        lambda name, entry, settings: None,  # no restrictions
    )

    return SimpleNamespace(agents=agents, project=project, spec=spec)


def _make_kas_runtime() -> AcpRuntime:
    """A minimal KAS-backend AcpRuntime with a fake process."""
    rt = AcpRuntime(work_dir="/tmp", acp_backend=ACP_BACKEND_KAS)
    proc = MagicMock()
    proc.returncode = None
    # nosemgrep: kirocrew.mock-pid-allocatable -- fake process for the KAS identity test
    proc.pid = 9999
    rt._process = proc
    rt._pid = 9999
    rt._initialized = True
    return rt


@pytest.mark.asyncio
async def test_kas_unpooled_control_planes_adds_core_element(kas_tree):
    """With no existing entries, the KAS path adds a kirocrew-core element.

    This is the regression guard for the identity_unattested bug: when
    ``stub_servers`` is empty, the stub pipeline is bypassed and no token reaches
    kirocrew-core.  The KAS branch of ``_unpooled_control_planes`` calls
    ``kiro_control_plane_servers`` so the element (which ``_own_stub_session``
    then attaches the session token to) is always present.
    """
    runtime = _make_kas_runtime()
    servers = await runtime._unpooled_control_planes([], "kirocrew", kas_tree.project)
    names = [str(s.get("name")) for s in servers]
    assert "kirocrew-core" in names, (
        "KAS _unpooled_control_planes must add a kirocrew-core element when none "
        f"is in the existing entries; got {names!r}"
    )


@pytest.mark.asyncio
async def test_kas_unpooled_control_planes_skips_existing_entry(kas_tree):
    """When a stub entry for kirocrew-core is already in entries, no duplicate.

    If ``mcp_gateway.stub_servers`` includes kirocrew-core, a stub entry is
    already in the list.  ``existing_names`` prevents the control-plane element
    from being added a second time.
    """
    existing = {"name": "kirocrew-core", "command": "stub", "args": [], "type": "stdio"}
    runtime = _make_kas_runtime()
    servers = await runtime._unpooled_control_planes([existing], "kirocrew", kas_tree.project)
    core_count = sum(1 for s in servers if s.get("name") == "kirocrew-core")
    assert core_count == 1, (
        "kirocrew-core must appear exactly once when a stub entry is already present; "
        f"got {core_count} copies in {[s.get('name') for s in servers]!r}"
    )


@pytest.mark.asyncio
async def test_kas_unpooled_control_planes_returns_entries_plus_elements(kas_tree):
    """Existing non-core entries are preserved alongside the new element."""
    other = {"name": "slack-mcp", "command": "slack", "args": [], "type": "stdio"}
    runtime = _make_kas_runtime()
    servers = await runtime._unpooled_control_planes([other], "kirocrew", kas_tree.project)
    names = [str(s.get("name")) for s in servers]
    assert "slack-mcp" in names, "existing non-core entries must be preserved"
    assert "kirocrew-core" in names, "kirocrew-core element must still be added"
