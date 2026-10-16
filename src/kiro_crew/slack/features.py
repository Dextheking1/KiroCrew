"""Slack Code feature switches and code-channel defaults: the one read path.

Three switches live in ``config.json`` under ``slack`` (see
``config/sections_slack.py``):

* ``agent_sessions`` -- the app is a Slack agent (``features.agent_view``):
  thread status/title and Slack's native Stop button;
* ``code_channels`` -- the agent may open Slack code channels;
* ``code_channel_defaults`` -- the folder / agent / model / effort a new
  code-channel session starts with.

Every runtime caller (manifest rendering, the settings page, the agent-session
and code-channel runtimes) reads them through this module instead of poking the
dataclass, so "is it on?" has one answer and fails closed the same way
everywhere: anything that is not a real ``True`` is off.

Pure and synchronous: no I/O, no Slack calls, no dashboard state. Callers that
need the folder tree pass it in (:func:`find_folder`), which keeps this module
importable from the manifest renderer and the security scanner alike.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from kiro_crew.effort import is_valid_effort

#: Sidebar folder ids are ``uuid4().hex[:12]`` (``chat_folders``); an id-shaped
#: reference is matched by id only, never as a folder name.
_FOLDER_ID_LEN = 12
_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class CodeChannelDefaults:
    """What a new code-channel session starts with; ``None`` = not set here."""

    folder: str | None = None
    agent: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None

    @property
    def is_empty(self) -> bool:
        """True when no default is set at all."""
        return not (self.folder or self.agent or self.model or self.reasoning_effort)


@dataclass(frozen=True)
class ResolvedCodeChannelSession:
    """The effective start state of a new code-channel session.

    ``*_source`` names where each value came from (``"default"``, ``"folder"``,
    ``"slack"``, ``"origin"`` or ``""`` for nothing), so the settings page can
    explain the outcome and a runtime can log it without re-deriving it.
    """

    folder_id: str | None
    agent: str | None
    agent_source: str
    model: str | None
    reasoning_effort: str | None
    project_dir: str | None
    project_source: str
    #: True when a folder default is configured but no longer exists.
    folder_missing: bool = False


def _slack_section(cfg: Any) -> Any:
    """``cfg.slack`` for a full config, or *cfg* itself for a ``SlackConfig``."""
    return getattr(cfg, "slack", cfg)


def agent_sessions_enabled(cfg: Any) -> bool:
    """Whether Slack threads run as Slack agent sessions (``slack.agent_sessions``)."""
    return getattr(_slack_section(cfg), "agent_sessions", False) is True


def code_channels_enabled(cfg: Any) -> bool:
    """Whether the agent may open Slack code channels (``slack.code_channels``)."""
    return getattr(_slack_section(cfg), "code_channels", False) is True


def _opt_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def code_channel_defaults(cfg: Any) -> CodeChannelDefaults:
    """``slack.code_channel_defaults`` as a frozen value, invalid fields unset."""
    raw = getattr(_slack_section(cfg), "code_channel_defaults", None)
    effort = _opt_str(getattr(raw, "reasoning_effort", None))
    return CodeChannelDefaults(
        folder=_opt_str(getattr(raw, "folder", None)),
        agent=_opt_str(getattr(raw, "agent", None)),
        model=_opt_str(getattr(raw, "model", None)),
        reasoning_effort=effort if is_valid_effort(effort) else None,
    )


def looks_like_folder_id(ref: str) -> bool:
    """True when *ref* has the shape of a sidebar folder id."""
    return len(ref) == _FOLDER_ID_LEN and set(ref) <= _HEX


def folder_paths(folders: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Map each sidebar folder id to its ``Parent/Child`` path (cycle-safe)."""
    by_id = {str(f.get("id") or ""): f for f in folders if isinstance(f, Mapping) and f.get("id")}
    paths: dict[str, str] = {}
    for fid in by_id:
        segments: list[str] = []
        seen: set[str] = set()
        cur = fid
        while cur and cur in by_id and cur not in seen:
            seen.add(cur)
            segments.append(str(by_id[cur].get("name") or "?"))
            cur = str(by_id[cur].get("parent_id") or "")
        paths[fid] = "/".join(reversed(segments))
    return paths


def find_folder(folders: Iterable[Mapping[str, Any]], ref: str | None) -> Mapping[str, Any] | None:
    """The sidebar folder *ref* names -- by id, else by unique ``Parent/Child`` path.

    Path matching is case-insensitive and ignores spaces around ``/``. A path two
    folders share is ambiguous and matches nothing: filing into an arbitrary one of
    them would be a silent guess. Returns ``None`` for an unset or dangling ref.
    """
    if not ref:
        return None
    listed = [f for f in folders if isinstance(f, Mapping) and f.get("id")]
    for folder in listed:
        if str(folder.get("id")) == ref:
            return folder
    if looks_like_folder_id(ref):
        return None
    wanted = "/".join(seg.strip().lower() for seg in ref.split("/") if seg.strip())
    paths = folder_paths(listed)
    hits = [f for f in listed if paths.get(str(f.get("id")), "").lower() == wanted]
    return hits[0] if len(hits) == 1 else None


def folder_project_dir(folders: Iterable[Mapping[str, Any]], folder_id: str | None) -> str | None:
    """Nearest declared ``project_dir`` on *folder_id*'s ancestor chain (unvalidated)."""
    by_id = {str(f.get("id") or ""): f for f in folders if isinstance(f, Mapping)}
    seen: set[str] = set()
    cur = folder_id or ""
    while cur and cur not in seen:
        seen.add(cur)
        folder = by_id.get(cur)
        if folder is None:
            break
        project = _opt_str(folder.get("project_dir"))
        if project:
            return project
        cur = str(folder.get("parent_id") or "")
    return None


def resolve_code_channel_session(
    defaults: CodeChannelDefaults,
    folders: Iterable[Mapping[str, Any]],
    *,
    slack_agent: str | None = None,
    origin_project_dir: str | None = None,
) -> ResolvedCodeChannelSession:
    """Apply the code-channel resolution order to one new session.

    * agent: ``defaults.agent`` > the folder's ``default_agent`` > *slack_agent*
      (the agent Slack already uses for the originating conversation);
    * model / effort: ``defaults`` only -- unset means the agent's own;
    * project: the folder's ``project_dir`` (nearest ancestor that declares one)
      > *origin_project_dir* (the project the origin thread is bound to) > none.

    Pure: the caller supplies the folder list. Validation of the project path
    (sensitive paths, existence) stays with the session layer that opens it.
    """
    listed = [f for f in folders if isinstance(f, Mapping)]
    folder = find_folder(listed, defaults.folder)
    folder_id = str(folder.get("id")) if folder is not None else None
    folder_agent = _opt_str(folder.get("default_agent")) if folder is not None else None

    if defaults.agent:
        agent, agent_source = defaults.agent, "default"
    elif folder_agent:
        agent, agent_source = folder_agent, "folder"
    elif _opt_str(slack_agent):
        agent, agent_source = _opt_str(slack_agent), "slack"
    else:
        agent, agent_source = None, ""

    project = folder_project_dir(listed, folder_id)
    if project:
        project_source = "folder"
    elif _opt_str(origin_project_dir):
        project, project_source = _opt_str(origin_project_dir), "origin"
    else:
        project, project_source = None, ""

    return ResolvedCodeChannelSession(
        folder_id=folder_id,
        agent=agent,
        agent_source=agent_source,
        model=defaults.model,
        reasoning_effort=defaults.reasoning_effort,
        project_dir=project,
        project_source=project_source,
        folder_missing=bool(defaults.folder) and folder is None,
    )
