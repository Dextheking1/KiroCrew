"""App resource paths are screened by ``hooks.validate_file_path`` before they are followed.

Install-time containment (``AppManifest.validate``) and agent registration
(``bridges._register_agents`` and the agent prune) ask the shared link screen for a
manifest-declared path before canonicalising it. A path the screen refuses is
rejected, and it is never handed to ``Path.resolve``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kiro_crew import hooks
from kiro_crew.apps import bridges as bridges_mod
from kiro_crew.apps import execution as execution_mod
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_crew.apps.manifest import AppManifest, app_root_containment, path_within_app_root

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENT_FILE = "my-agent.json"


def _make_app_source(tmp_path: Path, name: str = "test-app") -> Path:
    src = tmp_path / "source" / name
    (src / "agents").mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Test App",
        "description": "A test app",
        "author": "tester",
        "agents": [f"agents/{_AGENT_FILE}"],
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    (src / "agents" / _AGENT_FILE).write_text(
        json.dumps({"name": "my-agent", "model": "auto"}), encoding="utf-8"
    )
    return src


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    monkeypatch.setattr(execution_mod, "third_party_execution_allowed", lambda: True)
    mcp_path = tmp_path / "mcp.json"
    monkeypatch.setattr(bridges_mod, "_mcp_json_path", lambda: mcp_path)
    return {"home": home, "kiro_agents": kiro_agents}


def _refuse_agent_file(monkeypatch) -> list[str]:
    """Make the link screen refuse the agent file, and record what is resolved.

    Returns the names of the paths ``Path.resolve`` is called on, so a test can show
    the refused path was never followed.
    """
    resolved: list[str] = []
    real_resolve = Path.resolve

    def recording_resolve(self, *args, **kwargs):
        resolved.append(self.name)
        return real_resolve(self, *args, **kwargs)

    def screen(raw: str) -> str | None:
        if Path(raw).name == _AGENT_FILE:
            return None
        return os.path.realpath(raw)

    monkeypatch.setattr(hooks, "validate_file_path", screen)
    monkeypatch.setattr(Path, "resolve", recording_resolve)
    return resolved


def test_install_rejects_an_agent_path_the_link_screen_refuses(tmp_path, app_env, monkeypatch):
    src = _make_app_source(tmp_path)
    resolved = _refuse_agent_file(monkeypatch)

    result = install_app(src)

    assert not result.ok
    assert "agents path" in result.error
    assert _AGENT_FILE not in resolved
    assert not (app_env["home"] / "apps" / "test-app").exists()


def test_register_agents_skips_an_agent_the_link_screen_refuses(tmp_path, app_env, monkeypatch):
    assert install_app(_make_app_source(tmp_path)).ok
    app_root = app_env["home"] / "apps" / "test-app"
    manifest = AppManifest.from_json_file(app_root / APP_MANIFEST_FILENAME)
    resolved = _refuse_agent_file(monkeypatch)

    assert bridges_mod._register_agents("test-app", manifest, app_root) == []
    assert _AGENT_FILE not in resolved
    assert not (app_env["kiro_agents"] / "test-app--my-agent.json").exists()


def test_prune_keeps_agent_configs_when_the_link_screen_refuses(tmp_path, app_env, monkeypatch):
    assert install_app(_make_app_source(tmp_path)).ok
    app_root = app_env["home"] / "apps" / "test-app"
    manifest = AppManifest.from_json_file(app_root / APP_MANIFEST_FILENAME)
    assert bridges_mod._register_agents("test-app", manifest, app_root) == ["test-app/my-agent"]
    config = app_env["kiro_agents"] / "test-app--my-agent.json"
    assert config.is_file()
    resolved = _refuse_agent_file(monkeypatch)

    bridges_mod._prune_stale_app_resources("test-app", manifest, app_root)

    assert _AGENT_FILE not in resolved
    assert config.is_file()


def test_register_agents_registers_an_agent_the_link_screen_passes(tmp_path, app_env):
    assert install_app(_make_app_source(tmp_path)).ok
    app_root = app_env["home"] / "apps" / "test-app"
    manifest = AppManifest.from_json_file(app_root / APP_MANIFEST_FILENAME)

    assert bridges_mod._register_agents("test-app", manifest, app_root) == ["test-app/my-agent"]


def test_path_within_app_root_refuses_when_the_root_is_refused(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "ok.json").write_text("{}", encoding="utf-8")
    assert path_within_app_root(app_root / "ok.json", app_root)
    assert app_root_containment(app_root / "ok.json", app_root) is True

    monkeypatch.setattr(
        hooks,
        "validate_file_path",
        lambda raw: None if Path(raw) == app_root else os.path.realpath(raw),
    )
    assert not path_within_app_root(app_root / "ok.json", app_root)
    assert app_root_containment(app_root / "ok.json", app_root) is None


def test_every_builtin_manifest_passes_containment_against_its_own_root():
    manifests = sorted((_REPO_ROOT / "src/kiro_crew/apps/builtins").glob("*/app.json"))
    assert manifests, "no built-in manifests found"
    offenders = {}
    for app_json in manifests:
        errors = AppManifest.from_json_file(app_json).validate(app_root=app_json.parent)
        containment = [e for e in errors if "path traversal" in e]
        if containment:
            offenders[app_json.parent.name] = containment
    assert offenders == {}
