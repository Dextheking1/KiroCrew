"""Persisted Session Environments settings: validator, loader and dashboard API.

One validator serves both: a hand-edited file keeps its valid profiles while
``PATCH`` refuses the same value and writes nothing.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.config import session_environments as se
from kiro_crew.config.loader import _KNOWN_CONFIG_SECTIONS, KiroCrewConfig
from kiro_crew.dashboard.handlers import api_kirocrew_config, api_kirocrew_config_patch

_BASE_CONFIG = {
    "agents": {"kirocrew": {"kiro_agent": "kirocrew"}},
    "default_agent": "kirocrew",
    "session": {"timeout_secs": 1234},
    "an_edition_section": {"keep": "me"},
}
#: What a read adds to a section that omits them.
_DEFAULTS = {
    "sprites_profiles": [],
    "boxd_profiles": [],
    "default_profile": "",
    "stop_on_shutdown": True,
}


def _profile(pid: str = "dev", **overrides: object) -> dict:
    base = {
        "id": pid,
        "name": "Dev box",
        "template": "python-dev",
        "workdir": "/workspace/repo",
        "compute_survives_stop": True,
    }
    base.update(overrides)
    return base


def _app() -> web.Application:
    app = web.Application()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    app.router.add_get("/api/config/kirocrew", api_kirocrew_config)
    return as_owner(app)


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_BASE_CONFIG), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    return path


def _stored(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


async def _patch(client, path, value):
    return await client.patch("/api/config/kirocrew", json={"path": path, "value": value})


async def _section(client) -> dict:
    return (await (await client.get("/api/config/kirocrew")).json())["session_environments"]


def _api() -> TestClient:
    return TestClient(TestServer(_app()))


def _write_section(path, section) -> None:
    path.write_text(json.dumps({**_BASE_CONFIG, "session_environments": section}), encoding="utf-8")


class TestCoderUrl:
    @pytest.mark.parametrize(
        "url",
        ["", "https://coder.example.com", "http://10.0.0.5:3000", "https://c.example.com/base/"],
    )
    def test_accepts(self, url):
        assert se.validate_coder_url(url) == url

    def test_strips_whitespace(self):
        assert se.validate_coder_url("  https://c.example.com  ") == "https://c.example.com"

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://c.example.com",
            "c.example.com",
            "https://",
            "https://user@c.example.com",
            "https://user:pw@c.example.com",
            "https://c.example.com?x=1",
            "https://c.example.com?",
            "https://c.example.com#frag",
            "https://c.example.com:99999",
            "https://c.exa mple.com",
            "https://c.exa\tmple.com",
            "javascript:alert(1)",
            123,
            None,
        ],
    )
    def test_rejects(self, url):
        with pytest.raises(se.SessionEnvironmentsError):
            se.validate_coder_url(url)


class TestProfiles:
    def test_valid_list(self):
        profiles = [_profile("a"), _profile("B-2_x")]
        assert se.validate_profiles(profiles) == profiles

    def test_id_max_length(self):
        assert se.validate_profile(_profile("a" * 64))["id"] == "a" * 64
        with pytest.raises(se.SessionEnvironmentsError):
            se.validate_profile(_profile("a" * 65))

    @pytest.mark.parametrize("pid", ["", "-dev", "_dev", "dev box", "dév", "dev.1", "dev\n", 5])
    def test_bad_id(self, pid):
        with pytest.raises(se.SessionEnvironmentsError, match=r"\.id"):
            se.validate_profile(_profile(pid))

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("name", ""),
            ("name", "   "),
            ("name", "x" * 129),
            ("name", 7),
            ("template", ""),
            ("template", "t" * 256),
            ("template", "a\x00b"),
            ("workdir", ""),
            ("workdir", "relative/path"),
            ("workdir", "C:\\repo"),
            ("workdir", "/" + "p" * 1024),
        ],
    )
    def test_bad_field(self, field, value):
        with pytest.raises(se.SessionEnvironmentsError, match=field):
            se.validate_profile(_profile(**{field: value}))

    def test_bounds_inclusive(self):
        p = _profile(name="n" * 128, template="t" * 255, workdir="/" + "p" * 1023)
        assert se.validate_profile(p) == p

    def test_unknown_key_refused_so_no_credential_can_ride_along(self):
        with pytest.raises(se.SessionEnvironmentsError, match="unknown"):
            se.validate_profile({**_profile(), "token": "secret"})

    def test_missing_key(self):
        p = _profile()
        del p["workdir"]
        with pytest.raises(se.SessionEnvironmentsError, match="missing"):
            se.validate_profile(p)

    def test_duplicate_id(self):
        with pytest.raises(se.SessionEnvironmentsError, match="duplicate"):
            se.validate_profiles([_profile("a"), _profile("a")])

    def test_cap(self):
        ok = [_profile(f"p{i}") for i in range(se.MAX_PROFILES)]
        assert len(se.validate_profiles(ok)) == se.MAX_PROFILES
        with pytest.raises(se.SessionEnvironmentsError, match="at most"):
            se.validate_profiles(ok + [_profile("extra")])

    @pytest.mark.parametrize("value", [None, {}, "a", ({"id": "a"},)])
    def test_not_a_list(self, value):
        with pytest.raises(se.SessionEnvironmentsError):
            se.validate_profiles(value)


class TestSection:
    def test_both_keys_required_and_no_others(self):
        good = {"coder_url": "", "profiles": []}
        assert se.validate_session_environments(good) == good
        with pytest.raises(se.SessionEnvironmentsError, match="missing"):
            se.validate_session_environments({"coder_url": ""})
        with pytest.raises(se.SessionEnvironmentsError, match="unknown"):
            se.validate_session_environments({**good, "enabled": True})
        with pytest.raises(se.SessionEnvironmentsError):
            se.validate_session_environments([])


class TestLoader:
    def test_absent_section_defaults(self, config_file):
        cfg = KiroCrewConfig.load()
        assert cfg.session_environments.coder_url == ""
        assert cfg.session_environments.profiles == []
        assert cfg.to_dict()["session_environments"] == {
            "coder_url": "",
            "profiles": [],
            **_DEFAULTS,
        }

    def test_valid_section_round_trips(self, config_file):
        section = {
            "coder_url": "https://c.example.com",
            "profiles": [_profile("a")],
            "sprites_profiles": [],
            "boxd_profiles": [],
            "default_profile": "",
            "stop_on_shutdown": True,
        }
        _write_section(config_file, section)
        cfg = KiroCrewConfig.load()
        assert cfg.to_dict()["session_environments"] == section
        cfg.save()
        stored = _stored(config_file)
        assert stored["session_environments"] == section
        assert stored["an_edition_section"] == {"keep": "me"}
        assert KiroCrewConfig.load().to_dict()["session_environments"] == section

    def test_lenient_load_keeps_valid_parts(self, config_file):
        section = {
            "coder_url": "https://user:pw@c.example.com",
            "profiles": [
                _profile("good"),
                _profile("bad", workdir="relative"),
                _profile("good"),
                "not-an-object",
                _profile("also-good"),
            ],
        }
        _write_section(config_file, section)
        env = KiroCrewConfig.load().session_environments
        assert env.coder_url == ""
        assert [p.id for p in env.profiles] == ["good", "also-good"]

    def test_over_cap_truncated_on_load(self):
        coder_url, profiles = se.load_session_environments(
            {"profiles": [_profile(f"p{i}") for i in range(se.MAX_PROFILES + 5)]}
        )
        assert coder_url == ""
        assert len(profiles) == se.MAX_PROFILES

    @pytest.mark.parametrize("raw", [None, "x", [], {"profiles": "x"}])
    def test_malformed_section_defaults(self, raw):
        assert se.load_session_environments(raw) == ("", [])

    def test_is_a_known_section(self):
        assert "session_environments" in _KNOWN_CONFIG_SECTIONS


class TestApi:
    pytestmark = pytest.mark.asyncio

    async def test_get_exposes_safe_defaults(self, config_file):
        async with _api() as client:
            resp = await client.get("/api/config/kirocrew")
            assert resp.status == 200
            assert (await resp.json())["session_environments"] == {
                "coder_url": "",
                "profiles": [],
                **_DEFAULTS,
            }

    async def test_whole_section_saves_and_preserves_unrelated_config(self, config_file):
        section = {
            "coder_url": " https://c.example.com ",
            "profiles": [_profile("a"), _profile("b", name="  B  ")],
        }
        async with _api() as client:
            resp = await _patch(client, "session_environments", section)
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        expected = {
            "coder_url": "https://c.example.com",
            "profiles": [_profile("a"), _profile("b", name="B")],
        }
        # The read shows the retention default; the file holds only what was sent.
        assert body["session_environments"] == {**expected, **_DEFAULTS}
        stored = _stored(config_file)
        assert stored["session_environments"] == expected
        assert stored["session"]["timeout_secs"] == 1234
        assert stored["an_edition_section"] == {"keep": "me"}
        assert stored["default_agent"] == "kirocrew"

    async def test_leaf_paths_update_one_half(self, config_file):
        async with _api() as client:
            assert (
                await _patch(client, "session_environments.profiles", [_profile()])
            ).status == 200
            resp = await _patch(client, "session_environments.coder_url", "https://c.example.com")
            assert resp.status == 200
            got = await _section(client)
        assert got == {"coder_url": "https://c.example.com", "profiles": [_profile()], **_DEFAULTS}

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            ("session_environments", {"coder_url": "https://c.example.com"}),
            ("session_environments", {"coder_url": "", "profiles": [], "token": "x"}),
            ("session_environments", "https://c.example.com"),
            ("session_environments.coder_url", "https://c.example.com?token=x"),
            ("session_environments.coder_url", "file:///etc/passwd"),
            ("session_environments.profiles", [_profile("a"), _profile("a")]),
            ("session_environments.profiles", [_profile(workdir="repo")]),
            ("session_environments.profiles", [{**_profile(), "password": "x"}]),
            ("session_environments.profiles", [_profile(f"p{i}") for i in range(21)]),
            ("session_environments.enabled", True),
        ],
    )
    async def test_invalid_values_are_refused_and_nothing_is_written(
        self, config_file, path, value
    ):
        before = config_file.read_bytes()
        async with _api() as client:
            resp = await _patch(client, path, value)
            assert resp.status == 400
            assert (await resp.json())["error"]
        assert config_file.read_bytes() == before

    async def test_get_drops_unmodelled_keys_but_save_keeps_them(self, config_file):
        """A key a newer build wrote survives the write but is not shown (the schema
        cannot vouch that it is not secret)."""
        _write_section(config_file, {"coder_url": "", "profiles": [], "future": "x"})
        async with _api() as client:
            resp = await _patch(client, "session_environments.coder_url", "https://c.example.com")
            assert resp.status == 200
            got = await _section(client)
        assert got == {"coder_url": "https://c.example.com", "profiles": [], **_DEFAULTS}
        assert _stored(config_file)["session_environments"]["future"] == "x"

    async def test_an_older_client_that_omits_an_optional_key_keeps_the_saved_value(
        self, config_file
    ):
        async with _api() as client:
            assert (
                await _patch(client, "session_environments.stop_on_shutdown", False)
            ).status == 200
            # A dashboard from before the setting saves the section without it.
            resp = await _patch(
                client,
                "session_environments",
                {"coder_url": "https://c.example.com", "profiles": []},
            )
            assert resp.status == 200, await resp.text()
            got = await _section(client)
        assert got["stop_on_shutdown"] is False

    @pytest.mark.parametrize("field", ["", ".archived_retention_days"])
    async def test_retention_is_not_a_setting(self, config_file, field):
        # Fixed at ARCHIVED_RETENTION_DAYS: refused as a field and as a section key.
        section = {"coder_url": "https://c.example.com", "profiles": []}
        value = 2 if field else {**section, "archived_retention_days": 2}
        before = config_file.read_bytes()
        async with _api() as client:
            resp = await _patch(client, "session_environments" + field, value)
            assert resp.status == 400
        assert config_file.read_bytes() == before


# Cross-provider ids address both the picker and the launch resolver.
_PROVIDER_KEYS = ("profiles", "sprites_profiles", "boxd_profiles")


def _provider_section():
    return {
        "coder_url": "https://c.example.com",
        "profiles": [_profile("coder-id")],
        "sprites_profiles": [
            {"id": "sprites-id", "name": "Sprite", "token_secret": "TOKEN", "workdir": "/w"}
        ],
        "boxd_profiles": [
            {"id": "boxd-id", "name": "Box", "snapshot": "", "vcpu": 0, "workdir": "/w"}
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed,existing", [(a, b) for a in _PROVIDER_KEYS for b in _PROVIDER_KEYS if a != b]
)
@pytest.mark.parametrize("whole", [False, True])
async def test_cross_provider_collision_is_refused_under_write_lock(
    config_file, monkeypatch, changed, existing, whole
):
    from kiro_crew.config import loader

    section = _provider_section()
    config_file.write_text(json.dumps({**_BASE_CONFIG, "session_environments": section}))
    colliding = [{**section[changed][0], "id": section[existing][0]["id"]}]
    checked = []
    original = loader.update_config_locked

    def locked(path, *, mutate, **kwargs):
        def checking(data):
            checked.append(True)
            return mutate(data)

        return original(path, mutate=checking, **kwargs)

    monkeypatch.setattr(loader, "update_config_locked", locked)
    before = config_file.read_bytes()
    # Whole-section clients may omit the other providers; the locked merge must
    # still check their preserved lists. Coder keys are required in a whole save.
    value = {"coder_url": section["coder_url"], "profiles": section["profiles"]}
    value[changed] = colliding
    async with TestClient(TestServer(_app())) as client:
        resp = await _patch(
            client,
            "session_environments" if whole else f"session_environments.{changed}",
            value if whole else colliding,
        )
        assert resp.status == 400, await resp.text()
        assert "both" in (await resp.json())["error"]
    assert config_file.read_bytes() == before
    if not whole or existing != "profiles":
        assert checked == [True]


@pytest.mark.parametrize(
    "left,right",
    [
        ("profiles", "sprites_profiles"),
        ("profiles", "boxd_profiles"),
        ("sprites_profiles", "boxd_profiles"),
    ],
)
@pytest.mark.parametrize("coder_url", ["", "https://c.example.com"])
def test_hand_edited_collision_stays_unavailable_to_picker_and_launch(
    monkeypatch, left, right, coder_url
):
    from types import SimpleNamespace

    from kiro_crew.coder.session_launch import profile_provider
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.sections import SessionEnvironmentsConfig
    from kiro_crew.dashboard.handlers import session_environments as handlers
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    raw = _provider_section()
    raw["coder_url"] = coder_url
    raw[right][0]["id"] = raw[left][0]["id"]
    section = SessionEnvironmentsConfig.from_raw(raw)
    monkeypatch.setattr(
        KiroCrewConfig, "load", lambda: SimpleNamespace(session_environments=section)
    )
    duplicate = raw[left][0]["id"]
    with pytest.raises(SessionEnvironmentUnavailable, match="ambiguous"):
        profile_provider(section, duplicate)
    offered = handlers._profiles()
    assert duplicate not in {p["id"] for p in offered}
    assert all(profile_provider(section, p["id"]) == p["provider"] for p in offered)


@pytest.mark.parametrize(
    "key,provider",
    [("profiles", "coder"), ("sprites_profiles", "sprites"), ("boxd_profiles", "boxd")],
)
def test_choice_and_inherited_rows_record_the_resolved_provider(
    tmp_path, monkeypatch, key, provider
):
    from types import SimpleNamespace

    from kiro_crew.coder.session_launch import SessionEnvironmentChoices
    from kiro_crew.config import live
    from kiro_crew.config.sections import SessionEnvironmentsConfig

    raw = _provider_section()
    config = SimpleNamespace(session_environments=SessionEnvironmentsConfig.from_raw(raw))
    monkeypatch.setattr(live, "snapshot", lambda: config)
    choices = SessionEnvironmentChoices(tmp_path / "choices.json")
    profile_id = raw[key][0]["id"]
    choices.set("parent", profile_id)
    assert choices.inherit("child", "parent") == profile_id
    rows = json.loads(choices.path.read_text())["sessions"]
    assert rows["parent"] == {"provider": provider, "configuration": profile_id}
    assert rows["child"] == {"provider": provider, "configuration": profile_id, "parent": "parent"}
    assert choices.resolve("child") == (profile_id, "parent")


@pytest.mark.asyncio
async def test_launch_derives_provider_from_id_and_refuses_collisions(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kiro_crew.coder import session_launch
    from kiro_crew.config import live, paths
    from kiro_crew.config.sections import SessionEnvironmentsConfig
    from kiro_crew.session_environment import SessionEnvironmentUnavailable
    from kiro_crew.sprites_session.launch import SpritesSessionEnvironments

    raw = _provider_section()
    config = SimpleNamespace(session_environments=SessionEnvironmentsConfig.from_raw(raw))
    monkeypatch.setattr(live, "snapshot", lambda: config)
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json")
    choices.set("session", "sprites-id")
    # The provider tag is informational: legacy rows wrote coder for every id.
    data = json.loads(choices.path.read_text())
    data["sessions"]["session"]["provider"] = "coder"
    choices.path.write_text(json.dumps(data))
    prepare = AsyncMock(return_value="prepared")
    monkeypatch.setattr(SpritesSessionEnvironments, "prepare", prepare)
    assert await session_launch.prepare_for_session("session") == "prepared"
    prepare.assert_awaited_once_with("session", "sprites-id")
    raw["profiles"][0]["id"] = "sprites-id"
    config.session_environments = SessionEnvironmentsConfig.from_raw(raw)
    with pytest.raises(SessionEnvironmentUnavailable, match="ambiguous"):
        await session_launch.prepare_for_session("session")
    assert prepare.await_count == 1


@pytest.mark.asyncio
async def test_launch_refuses_a_row_whose_profile_id_moved_to_another_provider(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kiro_crew.boxd_session.launch import BoxdSessionEnvironments
    from kiro_crew.coder import session_launch
    from kiro_crew.config import live, paths
    from kiro_crew.config.sections import SessionEnvironmentsConfig
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    raw = _provider_section()
    config = SimpleNamespace(session_environments=SessionEnvironmentsConfig.from_raw(raw))
    monkeypatch.setattr(live, "snapshot", lambda: config)
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json")
    choices.set("session", "sprites-id")
    # The Sprites profile is removed and a boxd profile reuses its id.
    raw["sprites_profiles"] = []
    raw["boxd_profiles"][0]["id"] = "sprites-id"
    config.session_environments = SessionEnvironmentsConfig.from_raw(raw)
    prepare = AsyncMock(return_value="prepared")
    monkeypatch.setattr(BoxdSessionEnvironments, "prepare", prepare)
    with pytest.raises(SessionEnvironmentUnavailable, match="another provider"):
        await session_launch.prepare_for_session("session")
    prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_subagent_inherits_its_roots_recorded_provider(tmp_path, monkeypatch):
    """A root tagged sprites whose id now names boxd: its subagent is refused too."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kiro_crew.boxd_session.launch import BoxdSessionEnvironments
    from kiro_crew.coder import session_launch
    from kiro_crew.config import live, paths
    from kiro_crew.config.sections import SessionEnvironmentsConfig
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    raw = _provider_section()
    config = SimpleNamespace(session_environments=SessionEnvironmentsConfig.from_raw(raw))
    monkeypatch.setattr(live, "snapshot", lambda: config)
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json")
    choices.set("root", "sprites-id")
    # The Sprites profile is replaced by a boxd profile with the same id.
    raw["sprites_profiles"] = []
    raw["boxd_profiles"][0]["id"] = "sprites-id"
    config.session_environments = SessionEnvironmentsConfig.from_raw(raw)
    assert choices.inherit("child", "root") == "sprites-id"
    assert choices.recorded_provider("child") == "sprites"
    prepare = AsyncMock(return_value="prepared")
    monkeypatch.setattr(BoxdSessionEnvironments, "prepare", prepare)
    for key in ("root", "child"):
        with pytest.raises(SessionEnvironmentUnavailable, match="another provider"):
            await session_launch.prepare_for_session(key)
    prepare.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [True, False])
async def test_a_coder_session_with_a_workspace_never_moves_to_a_reused_id(
    tmp_path, monkeypatch, bound
):
    """A "coder" tag pins the provider once the session owns a Coder workspace.

    Moving such a session (an archived one, with its deletion due) onto a VM
    would skip Coder's resume check and let the sweep delete its files. A row
    with no workspace is the pre-provider legacy shape and still follows its id.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kiro_crew.boxd_session.launch import BoxdSessionEnvironments
    from kiro_crew.coder import session_launch
    from kiro_crew.coder.registry import WorkspaceBindingRegistry
    from kiro_crew.config import live, paths
    from kiro_crew.config.sections import SessionEnvironmentsConfig
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    raw = _provider_section()
    config = SimpleNamespace(session_environments=SessionEnvironmentsConfig.from_raw(raw))
    monkeypatch.setattr(live, "snapshot", lambda: config)
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json")
    choices.set("session", "coder-id")
    assert choices.recorded_provider("session") == "coder"
    if bound:
        registry = WorkspaceBindingRegistry(tmp_path / "coder" / "session-envs.json")
        registry.allocate(
            "session",
            template="python-dev",
            preset="",
            prefix="kc",
            owner_name="me",
            deployment_id="https://c.example.com",
        )
        registry.schedule_deletion("session", "2099-01-01T00:00:00Z")
    # The Coder profile is removed and a boxd profile reuses its id.
    raw["profiles"] = []
    raw["boxd_profiles"][0]["id"] = "coder-id"
    config.session_environments = SessionEnvironmentsConfig.from_raw(raw)
    prepare = AsyncMock(return_value="prepared")
    monkeypatch.setattr(BoxdSessionEnvironments, "prepare", prepare)
    if bound:
        with pytest.raises(SessionEnvironmentUnavailable, match="another provider"):
            await session_launch.prepare_for_session("session")
        prepare.assert_not_awaited()
        # The archived workspace stays scheduled, so nothing was decided for it.
        assert registry.get_by_session("session").deletion_due_at
    else:
        assert await session_launch.prepare_for_session("session") == "prepared"
        assert not (tmp_path / "coder" / "session-envs.lock").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owned,now",
    [("sprites", "boxd"), ("boxd", "sprites"), ("sprites", "coder"), ("boxd", "coder")],
)
async def test_a_legacy_tagged_session_holding_a_vm_never_moves_to_a_reused_id(
    tmp_path, monkeypatch, owned, now
):
    """A choice tagged "coder" (legacy, or made before the id moved) whose session
    already holds a sprite or boxd machine is pinned to it: moving it would
    leave the archived VM's deletion stamp standing and start an empty one."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kiro_crew.boxd_session.launch import BoxdSessionEnvironments
    from kiro_crew.coder import session_launch
    from kiro_crew.coder.persistent_vm import SessionResource, SessionResourceRegistry
    from kiro_crew.config import live, paths
    from kiro_crew.config.sections import SessionEnvironmentsConfig
    from kiro_crew.session_environment import SessionEnvironmentUnavailable
    from kiro_crew.sprites_session.launch import SpritesSessionEnvironments

    classes = {"sprites": SpritesSessionEnvironments, "boxd": BoxdSessionEnvironments}
    raw = _provider_section()
    config = SimpleNamespace(session_environments=SessionEnvironmentsConfig.from_raw(raw))
    monkeypatch.setattr(live, "snapshot", lambda: config)
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json")
    choices.set("session", "coder-id")
    assert choices.recorded_provider("session") == "coder"
    # The session ran on the other provider under the reused id, and was archived.
    cls = classes[owned]
    registry = SessionResourceRegistry(tmp_path / cls.registry_file, cls.label)
    registry.put(SessionResource("session", "coder-id", "kc-0123456789abcdef", "vm-id"))
    registry.set_deletion("session", "2099-01-01T00:00:00Z")
    # Now the id names a third provider (or Coder again).
    raw["profiles"] = [_profile("coder-id")] if now == "coder" else []
    if now != "coder":
        raw[f"{now}_profiles"][0]["id"] = "coder-id"
    config.session_environments = SessionEnvironmentsConfig.from_raw(raw)
    prepares = {}
    for name, provider_cls in classes.items():
        prepares[name] = AsyncMock(return_value="prepared")
        monkeypatch.setattr(provider_cls, "prepare", prepares[name])
    coder_prepare = AsyncMock(return_value="prepared")
    monkeypatch.setattr(session_launch.CoderSessionEnvironments, "prepare", coder_prepare)
    with pytest.raises(SessionEnvironmentUnavailable, match="another provider"):
        await session_launch.prepare_for_session("session")
    for mock in (*prepares.values(), coder_prepare):
        mock.assert_not_awaited()
    assert registry.get("session").deletion_due_at == "2099-01-01T00:00:00Z"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", _PROVIDER_KEYS)
@pytest.mark.parametrize("malformed", [None, [None], ["not-an-object"], [{}], [{"id": 7}]])
async def test_leaf_patch_ignores_malformed_stored_provider_rows(config_file, key, malformed):
    section = _provider_section()
    section[key] = malformed
    config_file.write_text(json.dumps({**_BASE_CONFIG, "session_environments": section}))
    async with TestClient(TestServer(_app())) as client:
        response = await _patch(client, "session_environments.stop_on_shutdown", False)
        assert response.status == 200, await response.text()
        assert (await response.json())["session_environments"]["stop_on_shutdown"] is False
    saved = _stored(config_file)["session_environments"]
    assert saved[key] == malformed
    assert saved["stop_on_shutdown"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["sprites_profiles", "boxd_profiles"])
@pytest.mark.parametrize("malformed", [None, [None], ["not-an-object"], [{}], [{"id": 7}]])
async def test_whole_patch_refuses_malformed_preserved_provider_rows(config_file, key, malformed):
    section = _provider_section()
    section[key] = malformed
    config_file.write_text(json.dumps({**_BASE_CONFIG, "session_environments": section}))
    before = config_file.read_bytes()
    async with TestClient(TestServer(_app())) as client:
        response = await _patch(
            client,
            "session_environments",
            {"coder_url": section["coder_url"], "profiles": section["profiles"]},
        )
        assert response.status == 400, await response.text()
        assert (await response.json())["code"] == "config_write_refused"
    assert config_file.read_bytes() == before


def test_every_vm_provider_is_wired_to_launch_and_the_move_guard():
    """One table names the VM providers; launch and the move guard read the same
    wiring, so a provider listed in one but not the other fails here."""
    from kiro_crew.coder import session_launch

    wired = session_launch._vm_environments()
    assert set(wired) == {provider for provider, _key, _label in se.VM_PROVIDERS}
    assert set(se.PROVIDER_IDS) == {session_launch.PROVIDER_ID, *wired}
    # The boot check reads VM_REGISTRY_FILES; each class uses the same path.
    assert set(se.VM_REGISTRY_FILES) == set(wired)
    for provider, cls in wired.items():
        assert cls.registry_file == se.VM_REGISTRY_FILES[provider], provider


@pytest.mark.asyncio
async def test_the_sweep_and_shutdown_cover_every_vm_provider(monkeypatch, tmp_path):
    """Retention builds its provider list from the same table, so a provider
    added there can never miss the sweep (its archived VMs would keep billing)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kiro_crew.coder import retention, session_launch
    from kiro_crew.config import paths

    monkeypatch.setattr(retention, "_section", AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    built = await retention._persistent_environments()
    assert [type(e) for e in built] == list(session_launch._vm_environments().values())
