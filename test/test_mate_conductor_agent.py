# SPDX-License-Identifier: Apache-2.0
"""``kirocrew-mate-conductor``: the dispatch-only conductor a crewmate's chat runs on.

Migration step 1 of ``rfc-mate-owned-agent-config.md`` (§5.0, §6): a crewmate's
own chat moves off the full default template and onto a conductor derived the way
the goal, pipeline and security conductors are -- no file-writing tool, no shell,
no user MCP server, grants that create or read and never mutate -- with a charter
that comes straight from source rather than from a file. Every crewmate binds to
it. There is deliberately no test here for a worker, a delegation hop or
``~/.kiro/mates/agents/`` -- those are steps 2 and 3.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import agent
from kiro_crew.agent_files import (
    MATE_CONDUCTOR_AGENT_FILENAME,
    MATE_CONDUCTOR_AGENT_NAME,
    OWNED_KIRO_AGENT_FILES,
)
from kiro_crew.agent_materialization import conductor_agents


@pytest.fixture
def installed(tmp_path, monkeypatch) -> dict:
    """The spec this installer actually writes, read back off disk."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    assert conductor_agents._install_mate_conductor_agent() is True
    return json.loads((tmp_path / MATE_CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8"))


class TestTheSpecIsAConductorNotTheDefaultTemplate:
    def test_the_file_carries_this_agents_name(self, installed):
        assert installed["name"] == MATE_CONDUCTOR_AGENT_NAME == "kirocrew-mate-conductor"
        assert "Never does the work itself" in installed["description"]

    def test_the_filename_is_owned_so_the_boot_sweep_covers_it(self):
        assert MATE_CONDUCTOR_AGENT_FILENAME == "kirocrew-mate-conductor.json"
        assert MATE_CONDUCTOR_AGENT_FILENAME in OWNED_KIRO_AGENT_FILES

    def test_no_tool_that_writes_a_file_and_no_shell(self, installed):
        """What makes "never does the work itself" a property of the spec: no
        ``fs_write``, no ``code`` (governance classes it under ``filesystem.write``)
        and -- unlike its siblings, which mount an un-approved shell for a bundled
        script -- no ``execute_bash`` at all."""
        tools = set(installed["tools"])
        assert "fs_read" in tools
        assert not tools & {"fs_write", "code", "execute_bash"}

    def test_every_crew_server_is_mounted_whole_and_no_user_server_is(self, installed):
        """The conductor set plus cron. A user's own MCP server reaches the WORKER a
        mate dispatches (steps 2 and 3), never the mate's own context."""
        crew_servers = {
            "kirocrew-core",
            "kirocrew-dashboard",
            "kirocrew-work",
            "kirocrew-cron",
            "kirocrew-guide",
            "kirocrew-mate",
        }
        assert {f"@{s}" for s in crew_servers} <= set(installed["tools"])
        assert set(installed["mcpServers"]) <= crew_servers

    def test_the_default_templates_extra_servers_do_not_reach_it(self, tmp_path, monkeypatch):
        """A person with many servers configured pays for none of them on a
        conversational turn (RFC §2 problem 6)."""
        template = agent.build_agent_config()
        template.setdefault("mcpServers", {})["my-jira"] = {"command": "jira-mcp", "args": []}
        monkeypatch.setattr(agent, "build_agent_config", lambda *a, **k: dict(template))
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        conductor_agents._install_mate_conductor_agent()
        spec = json.loads((tmp_path / MATE_CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8"))
        assert "my-jira" not in spec["mcpServers"]

    def test_the_grants_are_the_dispatch_set_and_nothing_mutating(self, installed):
        granted = set(installed["allowedTools"])
        assert "@kirocrew-core/spawn_run" in granted
        assert "@kirocrew-core/memory_recall" in granted
        assert "@kirocrew-cron/cron_add" in granted
        assert "@kirocrew-guide/find_ui" in granted
        assert "@kirocrew-mate/request_capabilities" in granted
        # The conductor line: never a verb that mutates a peer's state, never the
        # shell, never a routing verb a mate has no use for.
        assert not granted & {
            "execute_bash",
            "fs_write",
            "@kirocrew-dashboard/session_send",
            "@kirocrew-dashboard/session_stop",
            "@kirocrew-core/select_crew",
            "@kirocrew-cron/cron_secret_request",
        }

    def test_the_shipped_grants_are_all_in_the_provenance_history(self):
        shipped = conductor_agents._conductor_shipped(
            agent._MATE_CONDUCTOR_CORE_GRANTS,
            agent._CONDUCTOR_DASHBOARD_GRANTS,
            agent._MATE_CONDUCTOR_CRON_GRANTS + agent._GUIDE_AUTO_GRANTS + agent._MATE_AUTO_GRANTS,
        )
        assert set(shipped) <= conductor_agents._SHIPPED_GRANT_HISTORY[MATE_CONDUCTOR_AGENT_NAME]

    def test_the_file_prompt_is_the_native_stub_not_the_charter(self, installed):
        """The charter is not in the file, so editing the file cannot change what a
        mate runs; the ``[AGENT SYSTEM PROMPT]`` block carries it from source."""
        assert installed["prompt"] == agent._NATIVE_PROMPT_STUB
        assert "You dispatch" not in installed["prompt"]


class TestTheCharterComesFromSource:
    def _build(self, tmp_path):
        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        return ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )

    def test_the_contract_block_is_the_source_charter(self, tmp_path, monkeypatch):
        msg, _ = self._build(tmp_path).build_message(
            "hello", is_new_session=True, agent=MATE_CONDUCTOR_AGENT_NAME
        )
        assert msg.count("[AGENT SYSTEM PROMPT]\n") == 1
        assert "# Kiro Crew Mate" in msg
        assert "You dispatch; you never do the work yourself" in msg

    def test_a_user_prompt_override_does_not_reach_it(self, tmp_path, monkeypatch):
        """``~/.kiro/crew/prompt.md`` overrides the default template's contract and
        is the one editable prompt path a crewmate's chat had; the mate conductor
        does not read it (RFC §5.0)."""
        from kiro_crew import context as ctx_mod

        override = tmp_path / "prompt.md"
        override.write_text("# OVERRIDDEN BY THE USER\n", encoding="utf-8")
        monkeypatch.setattr(ctx_mod, "_prompt_path", lambda: override)

        builder = self._build(tmp_path)
        plain, _ = builder.build_message("hello", is_new_session=True, agent="kirocrew")
        mate, _ = builder.build_message(
            "hello", is_new_session=True, agent=MATE_CONDUCTOR_AGENT_NAME
        )
        assert "OVERRIDDEN BY THE USER" in plain
        assert "OVERRIDDEN BY THE USER" not in mate
        assert "# Kiro Crew Mate" in mate

    def test_the_charter_names_no_model_id(self):
        """Never hardcode a model id (AGENTS.md): the charter is shipped text."""
        import re

        assert not re.search(
            r"\b(claude-|opus|sonnet|haiku|gpt-|fable)", agent._MATE_CONDUCTOR_SYSTEM_PROMPT, re.I
        )


def test_rebuild_agent_config_installs_it_alongside_the_default(tmp_path, monkeypatch):
    """A full ``rebuild_agent_config()`` writes the mate conductor too, not just a
    direct call to the installer -- this is the self-healing boot path a crewmate's
    turn actually depends on."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    agent.rebuild_agent_config(clean=True)
    assert (tmp_path / MATE_CONDUCTOR_AGENT_FILENAME).exists()


class TestIsKirocrewDefaultAgent:
    """``context_assembly``'s ``is_custom`` checks all defer to this predicate so
    the mate conductor keeps the workspace identity block, the docs pointer and the
    crew context a crewmate's chat has today -- see test_context.py's
    ``test_docs_still_injected_for_the_mate_conductor``."""

    def test_kirocrew_and_the_mate_conductor_are_both_the_default(self):
        from kiro_crew.agent_files import is_kirocrew_default_agent

        assert is_kirocrew_default_agent("kirocrew")
        assert is_kirocrew_default_agent(MATE_CONDUCTOR_AGENT_NAME)

    def test_a_real_custom_agent_is_not(self):
        from kiro_crew.agent_files import is_kirocrew_default_agent

        assert not is_kirocrew_default_agent("code-reviewer")
        assert not is_kirocrew_default_agent("")
        assert not is_kirocrew_default_agent(None)

    def test_other_derived_specs_stay_out_of_scope(self):
        """Research/worker/team-lead and the other conductors are backend loops
        this change does not touch; widening their classification is a separate
        decision."""
        from kiro_crew.agent_files import is_kirocrew_default_agent

        assert not is_kirocrew_default_agent("kirocrew-research")
        assert not is_kirocrew_default_agent("kirocrew-worker")
        assert not is_kirocrew_default_agent("kirocrew-team-lead")
        assert not is_kirocrew_default_agent("kirocrew-conductor")


def test_every_crewmate_binds_to_the_mate_conductor():
    """Both template defaults name it: the first crewmate's implicit template and
    the New Crewmate dialog's "Built from" default (whose frontend twin,
    ``NewCrewmateDialog.DEFAULT_CREWMATE_TEMPLATE``, is kept equal by hand)."""
    from kiro_crew import change_card_catalog
    from kiro_crew.agent_materialization import first_crewmate

    assert first_crewmate._ORDINARY_TEMPLATE == MATE_CONDUCTOR_AGENT_NAME
    assert first_crewmate._default_template() == MATE_CONDUCTOR_AGENT_NAME
    assert change_card_catalog.DEFAULT_CREWMATE_TEMPLATE == MATE_CONDUCTOR_AGENT_NAME


def test_the_frontend_default_matches_the_backend_default():
    """The dialog prepends its own constant to the "Built from" list (the catalog
    withholds the mate conductor), so the two constants must name one spec or the
    dialog and the change cards would create crewmates on different templates."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "website/src/pages/members/NewCrewmateDialog.tsx"
    ).read_text(encoding="utf-8")
    assert f"export const DEFAULT_CREWMATE_TEMPLATE = '{MATE_CONDUCTOR_AGENT_NAME}'" in source


class TestTheRewiredBindingDoesNotFlipDefaultAgentPathsToCustom:
    """Every reader that decides "default vs custom" by the agent's NAME must keep
    treating the mate conductor as the default, or a crewmate's chat silently
    loses the context a ``kirocrew``-bound crewmate has today. These pin the paths
    the four ``is_custom`` sites in ``context_assembly`` do not cover."""

    def test_crew_context_is_injected_for_the_mate_conductor(self):
        from kiro_crew.context import _agent_includes_crew_context

        assert _agent_includes_crew_context("kirocrew") is True
        assert _agent_includes_crew_context(MATE_CONDUCTOR_AGENT_NAME) is True

    def test_session_skill_globs_resolves_the_whole_catalog_for_the_mate_conductor(
        self, tmp_path, monkeypatch
    ):
        """With no ``skill://`` entry of its own, the mate conductor must resolve
        the GLOBAL catalog (``session_skill_globs`` returns ``None``), exactly as
        the ``kirocrew`` default does -- not an empty custom scope (``[]``)."""
        from kiro_crew import agent as agent_mod
        from kiro_crew.agent_discovery import clear_list_agents_cache, session_skill_globs

        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", tmp_path)
        monkeypatch.setattr("kiro_crew.agent_discovery._KIRO_AGENTS_DIR", tmp_path)
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        conductor_agents._install_mate_conductor_agent()
        clear_list_agents_cache()

        assert session_skill_globs("", "kirocrew") is None
        assert session_skill_globs("", MATE_CONDUCTOR_AGENT_NAME) is None
        clear_list_agents_cache()

    def test_the_mate_conductor_is_not_a_dangling_template(self):
        """``_dangling_template`` must not flag the mate conductor as dangling even
        if a stale declared-name set does not list it -- a dangling verdict would
        self-heal the binding back to the default, undoing the rewire."""
        from kiro_crew.config.loader import _dangling_template

        empty: frozenset[str] = frozenset()
        assert _dangling_template("kirocrew", empty) is None
        assert _dangling_template(MATE_CONDUCTOR_AGENT_NAME, empty) is None
        assert _dangling_template("code-reviewer", empty) == "code-reviewer"

    def test_a_missing_mate_conductor_template_degrades_rather_than_raising(self, monkeypatch):
        """``member_essential_context`` raises for an unknown template whose spec
        file is absent, but degrades for a Crew default. If the mate conductor spec
        has not materialized yet (a boot race), it must degrade like ``kirocrew``."""
        from kiro_crew import member_essential_context as mec

        monkeypatch.setattr(mec, "resolve_template_path", lambda *a, **k: None)

        with pytest.raises(mec.MemberEssentialContextError):
            mec.documents_for_member("code-reviewer", None)

        for template in ("kirocrew", MATE_CONDUCTOR_AGENT_NAME):
            mec.documents_for_member(template, None)
