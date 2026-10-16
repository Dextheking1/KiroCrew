"""The two settings this package reads: the feature toggle and the defaults.

Both are owned by the Slack Code settings work (``slack.code_channels`` and the
code-channel defaults block); this module is the ONE place the package reads
them, so every seam answers the same question the same way. When the settings
module exports ``code_channels_enabled`` / ``code_channel_defaults`` they are
used as-is; until then the raw config fields are read with the same meaning.

The toggle is read LIVE (the gateway's reloaded config snapshot), so turning it
off takes effect on the next Slack event without a restart -- and with it off
every seam in this package is a pass-through, which is what keeps the gateway's
behaviour identical to a build without code channels.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CodeChannelDefaults:
    """What a new code channel's dashboard session starts with.

    Empty means "not set": the binding then falls back down the chain
    documented on :func:`kiro_crew.slack.code_channels.binding.resolve_session_spec`.
    """

    folder_id: str = ""
    agent: str = ""
    model: str = ""
    reasoning_effort: str = ""


def live_config(orch: Any | None = None) -> Any | None:
    """The gateway's live config (reload-aware), or a fresh load as a fallback."""
    try:
        from kiro_crew.slack.handler import slack_cfg

        return slack_cfg(orch)
    except Exception:
        logger.debug("code channels: live config unavailable", exc_info=True)
        return None


def code_channels_on(cfg: Any | None) -> bool:
    """True only when ``slack.code_channels`` is explicitly on.

    Anything else -- a missing section, a non-bool value, a config that failed
    to load -- is OFF, so a broken config can never switch the feature on.
    """
    if cfg is None:
        return False
    try:
        from kiro_crew.config import sections_slack

        fn = getattr(sections_slack, "code_channels_enabled", None)
        if callable(fn):
            return fn(cfg) is True
    except Exception:
        logger.debug("code_channels_enabled unavailable", exc_info=True)
    return getattr(getattr(cfg, "slack", None), "code_channels", False) is True


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def code_channel_defaults(cfg: Any | None) -> CodeChannelDefaults:
    """The configured defaults (all empty when unset or unreadable)."""
    if cfg is None:
        return CodeChannelDefaults()
    raw: Any = None
    try:
        from kiro_crew.config import sections_slack

        fn = getattr(sections_slack, "code_channel_defaults", None)
        if callable(fn):
            raw = fn(cfg)
    except Exception:
        logger.debug("code_channel_defaults unavailable", exc_info=True)
    if raw is None:
        slack = getattr(cfg, "slack", None)
        raw = {
            "folder_id": getattr(slack, "code_channel_folder_id", ""),
            "agent": getattr(slack, "code_channel_agent", ""),
            "model": getattr(slack, "code_channel_model", ""),
            "reasoning_effort": getattr(slack, "code_channel_reasoning_effort", ""),
        }

    def _get(name: str) -> str:
        if isinstance(raw, dict):
            return _text(raw.get(name))
        return _text(getattr(raw, name, ""))

    return CodeChannelDefaults(
        folder_id=_get("folder_id"),
        agent=_get("agent"),
        model=_get("model"),
        reasoning_effort=_get("reasoning_effort"),
    )
