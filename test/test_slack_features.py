"""``kiro_crew.slack.features``: the Slack Code switches and code-channel defaults.

Pins the shared contract other work codes against: the switch readers fail
closed, the defaults normalize, and the resolution order for a new code-channel
session is explicit default > folder > Slack's agent (agent) and folder > origin
thread (project).
"""

from __future__ import annotations

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.section_builders import _build_slack_config
from kiro_crew.slack import features

_FOLDERS = [
    {"id": "aaaaaaaaaaaa", "name": "Dev", "parent_id": "", "project_dir": "/repo/dev"},
    {
        "id": "bbbbbbbbbbbb",
        "name": "Slack Code",
        "parent_id": "aaaaaaaaaaaa",
        "default_agent": "builder",
    },
    {"id": "cccccccccccc", "name": "Twin", "parent_id": ""},
    {"id": "dddddddddddd", "name": "twin", "parent_id": ""},
]


class TestSwitches:
    def test_defaults_are_off(self) -> None:
        cfg = KiroCrewConfig()
        assert features.agent_sessions_enabled(cfg) is False
        assert features.code_channels_enabled(cfg) is False
        assert features.code_channel_defaults(cfg) == features.CodeChannelDefaults()
        assert features.code_channel_defaults(cfg).is_empty

    @pytest.mark.parametrize("raw", ["true", 1, "yes", None, [], {}])
    def test_only_a_real_true_turns_a_switch_on(self, raw: object) -> None:
        slack = _build_slack_config({"agent_sessions": raw, "code_channels": raw})
        assert features.agent_sessions_enabled(slack) is False
        assert features.code_channels_enabled(slack) is False

    def test_true_turns_each_switch_on_independently(self) -> None:
        slack = _build_slack_config({"code_channels": True})
        assert features.code_channels_enabled(slack) is True
        assert features.agent_sessions_enabled(slack) is False

    def test_reader_accepts_full_config_or_slack_section(self) -> None:
        cfg = KiroCrewConfig()
        cfg.slack = _build_slack_config({"agent_sessions": True})
        assert features.agent_sessions_enabled(cfg) is True
        assert features.agent_sessions_enabled(cfg.slack) is True

    def test_a_reader_handed_something_unrelated_is_off(self) -> None:
        assert features.code_channels_enabled(object()) is False
        assert features.code_channel_defaults(object()) == features.CodeChannelDefaults()


class TestDefaultsNormalization:
    def test_strings_are_trimmed_and_blank_is_unset(self) -> None:
        slack = _build_slack_config(
            {
                "code_channel_defaults": {
                    "folder": "  Dev/Slack Code ",
                    "agent": "   ",
                    "model": "claude-opus-4.8",
                    "reasoning_effort": "high",
                }
            }
        )
        assert features.code_channel_defaults(slack) == features.CodeChannelDefaults(
            folder="Dev/Slack Code", agent=None, model="claude-opus-4.8", reasoning_effort="high"
        )

    @pytest.mark.parametrize("effort", ["HIGH", "extreme", "", 3, None])
    def test_effort_outside_the_ladder_is_unset(self, effort: object) -> None:
        slack = _build_slack_config({"code_channel_defaults": {"reasoning_effort": effort}})
        assert features.code_channel_defaults(slack).reasoning_effort is None

    @pytest.mark.parametrize("raw", ["Dev", 7, ["x"], None])
    def test_non_object_defaults_read_as_unset(self, raw: object) -> None:
        slack = _build_slack_config({"code_channel_defaults": raw})
        assert features.code_channel_defaults(slack).is_empty

    def test_over_long_value_is_unset(self) -> None:
        slack = _build_slack_config({"code_channel_defaults": {"agent": "a" * 257}})
        assert features.code_channel_defaults(slack).agent is None


class TestFindFolder:
    def test_by_id(self) -> None:
        assert features.find_folder(_FOLDERS, "bbbbbbbbbbbb")["name"] == "Slack Code"

    def test_by_path_case_and_space_insensitive(self) -> None:
        hit = features.find_folder(_FOLDERS, " dev / slack code ")
        assert hit is not None and hit["id"] == "bbbbbbbbbbbb"

    def test_ambiguous_path_matches_nothing(self) -> None:
        assert features.find_folder(_FOLDERS, "Twin") is None

    def test_dangling_id_never_falls_back_to_a_name(self) -> None:
        folders = [*_FOLDERS, {"id": "eeeeeeeeeeee", "name": "ffffffffffff", "parent_id": ""}]
        assert features.find_folder(folders, "ffffffffffff") is None

    def test_parent_cycle_does_not_hang(self) -> None:
        cyc = [
            {"id": "111111111111", "name": "A", "parent_id": "222222222222"},
            {"id": "222222222222", "name": "B", "parent_id": "111111111111"},
        ]
        assert features.find_folder(cyc, "nope") is None
        assert features.folder_project_dir(cyc, "111111111111") is None


class TestResolution:
    def test_explicit_default_agent_wins(self) -> None:
        got = features.resolve_code_channel_session(
            features.CodeChannelDefaults(folder="bbbbbbbbbbbb", agent="reviewer"),
            _FOLDERS,
            slack_agent="slackbot",
        )
        assert (got.agent, got.agent_source) == ("reviewer", "default")

    def test_folder_agent_beats_slack_agent(self) -> None:
        got = features.resolve_code_channel_session(
            features.CodeChannelDefaults(folder="Dev/Slack Code"), _FOLDERS, slack_agent="slackbot"
        )
        assert (got.agent, got.agent_source) == ("builder", "folder")
        assert got.folder_id == "bbbbbbbbbbbb"

    def test_slack_agent_is_the_last_agent_fallback(self) -> None:
        got = features.resolve_code_channel_session(
            features.CodeChannelDefaults(), _FOLDERS, slack_agent="slackbot"
        )
        assert (got.agent, got.agent_source) == ("slackbot", "slack")

    def test_folder_project_is_inherited_from_ancestor_and_beats_origin(self) -> None:
        got = features.resolve_code_channel_session(
            features.CodeChannelDefaults(folder="bbbbbbbbbbbb"),
            _FOLDERS,
            origin_project_dir="/repo/origin",
        )
        assert (got.project_dir, got.project_source) == ("/repo/dev", "folder")

    def test_origin_project_when_no_folder_project(self) -> None:
        got = features.resolve_code_channel_session(
            features.CodeChannelDefaults(), _FOLDERS, origin_project_dir="/repo/origin"
        )
        assert (got.project_dir, got.project_source) == ("/repo/origin", "origin")

    def test_nothing_resolves_to_nothing(self) -> None:
        got = features.resolve_code_channel_session(features.CodeChannelDefaults(), [])
        assert (got.agent, got.project_dir, got.folder_id) == (None, None, None)
        assert got.folder_missing is False

    def test_deleted_folder_is_flagged_and_other_defaults_still_apply(self) -> None:
        got = features.resolve_code_channel_session(
            features.CodeChannelDefaults(folder="999999999999", model="m1", reasoning_effort="low"),
            _FOLDERS,
            slack_agent="slackbot",
            origin_project_dir="/repo/origin",
        )
        assert got.folder_missing is True
        assert got.folder_id is None
        assert (got.agent_source, got.project_source) == ("slack", "origin")
        assert (got.model, got.reasoning_effort) == ("m1", "low")
