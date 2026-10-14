"""A ``skill://`` mapping is matched against every copy of a name, not only the first.

The catalog keeps one row per skill name, from the first loader root holding it.
When two roots hold the same name (a package manager's tree and ``~/.kiro/skills``), a mapping
that names the later root's copy must still give the agent that skill.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.skills import SkillsLoader

pytestmark = pytest.mark.xdist_group("shadowed_skill_mapping")


def _skill(root: Path, name: str, tag: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {tag}\n---\nBODY {tag}\n", encoding="utf-8")
    return path


@pytest.fixture
def two_roots(tmp_path, opened):
    """A loader whose first extra root shadows a same-named skill in the second."""
    first, second = tmp_path / "aim", tmp_path / "global"
    _skill(first, "demo-skill", "FIRST")
    _skill(second, "demo-skill", "SECOND")
    cfg = KiroCrewConfig()
    cfg.skills.extra_paths = [str(first), str(second)]
    loader = opened(
        SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False, config=cfg)
    )
    return loader, first, second


def _bodies(loader: SkillsLoader, only: list[str] | None) -> list[str]:
    rows = loader.scoped_skills(only=only)
    return [
        loader.read_scoped_skill(row["key"], only=only).strip().splitlines()[-1] for row in rows
    ]


def test_mapping_naming_the_later_copy_gets_that_copy(two_roots):
    loader, _first, second = two_roots
    assert _bodies(loader, [str(second / "*" / "SKILL.md")]) == ["BODY SECOND"]


def test_mapping_naming_the_first_copy_keeps_it_once(two_roots):
    loader, first, second = two_roots
    only = [str(first / "*" / "SKILL.md"), str(second / "*" / "SKILL.md")]
    assert _bodies(loader, only) == ["BODY FIRST"]


def test_unmapped_scope_is_unchanged(two_roots):
    loader, _first, _second = two_roots
    assert _bodies(loader, None) == ["BODY FIRST"]


def test_mapping_reaching_no_copy_gets_nothing(two_roots, tmp_path):
    loader, _first, _second = two_roots
    assert _bodies(loader, [str(tmp_path / "elsewhere" / "*" / "SKILL.md")]) == []


def test_a_disabled_apps_copy_stays_out(two_roots, monkeypatch):
    loader, _first, second = two_roots
    monkeypatch.setattr(loader, "_get_disabled_app_names", lambda: frozenset({"off"}))
    monkeypatch.setattr(
        loader,
        "_owning_app",
        lambda _name, path: "off" if Path(path).is_relative_to(second) else None,
    )
    assert _bodies(loader, [str(second / "*" / "SKILL.md")]) == []
