"""Inbound Slack routing for code channels: the seams ``slack/events.py`` calls.

Three entry points, each a pass-through while ``slack.code_channels`` is off:

* :func:`inbound_gate` -- early in ``_route_message``, before the channel's
  activation mode is applied. For a message in one of OUR code channels it
  decides everything a code channel does differently: Slack's origin echo is
  dropped, a message that @-mentions only other people is left to them, the
  sender is authorized against the ORIGIN conversation, ``!stop`` stops the
  bound session, and anything else is DIRECTED -- answered without a mention.
* :func:`dispatch` -- at the point ``_route_message`` would start a turn. A
  DIRECTED message goes to the channel's bound dashboard session; a mention
  outside a code channel may open one (:func:`maybe_open_code_channel`).
* :func:`handle_session_stopped` -- Slack's native Stop button
  (``agent_session_stopped``) for a code channel stops the bound session.

Every allow/deny decision here writes an SEL event.
"""

from __future__ import annotations

import enum
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from kiro_crew.sel import sel
from kiro_crew.slack.code_channels import recognition
from kiro_crew.slack.code_channels.binding import (
    BindingUnavailable,
    bound_slot,
    create_bound_session,
    resolve_session_spec,
)
from kiro_crew.slack.code_channels.classifier import MAX_CONTEXT_MESSAGES, classify
from kiro_crew.slack.code_channels.client import CodeChannelsClient
from kiro_crew.slack.code_channels.creation import (
    channel_name_for,
    is_directed_human_prompt,
    is_origin_echo,
    session_id_for,
)
from kiro_crew.slack.code_channels.models import ChannelInfo, CodeChannelApiError, CreateRequest
from kiro_crew.slack.code_channels.recognition import ChannelKind
from kiro_crew.slack.code_channels.settings import (
    code_channel_defaults,
    code_channels_on,
    live_config,
)
from kiro_crew.slack.code_channels.store import (
    ROUTE_CODE_CHANNEL,
    Binding,
    Route,
    store,
)

logger = logging.getLogger(__name__)

_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")

NOT_AUTHORIZED_TEXT = (
    "⛔ You are not authorized to use this bot. Ask the owner to add you to the allowlist."
)


class Gate(enum.Enum):
    PASS = "pass"  # not a code-channel matter: the normal path runs unchanged
    DROP = "drop"  # handled (or deliberately ignored) here: the caller returns
    DIRECTED = "directed"  # ours to answer, no mention needed


@dataclass(frozen=True)
class InboundResult:
    gate: Gate
    binding: Binding | None = None
    info: ChannelInfo | None = None


PASS = InboundResult(Gate.PASS)
DROP = InboundResult(Gate.DROP)


def _self_user_id() -> str:
    from kiro_crew.slack.enterprise import validated_self_user_id

    return validated_self_user_id()


def _self_bot_id() -> str:
    from kiro_crew.slack.enterprise import validated_self_bot_id

    return validated_self_bot_id()


def _client(orch: Any) -> CodeChannelsClient | None:
    return CodeChannelsClient.from_slack(getattr(orch, "slack", None))


def _dashboard_state() -> Any | None:
    from kiro_crew.slack.handler_runtime.access import get_dashboard_state

    return get_dashboard_state()


def addressed_to_us(text: str, self_user_id: str) -> bool:
    """No mention at all, or a mention of us: directed. Others only: not."""
    mentioned = set(_MENTION.findall(text or ""))
    if not mentioned:
        return True
    return bool(self_user_id) and self_user_id in mentioned


def strip_leading_mention(text: str) -> str:
    return re.sub(r"^\s*<@[A-Z0-9]+(?:\|[^>]*)?>\s*", "", text or "")


def _origin_of(channel: str, binding: Binding | None, info: ChannelInfo | None) -> str:
    if binding is not None and binding.origin_channel_id:
        return binding.origin_channel_id
    if info is not None and info.origin_channel_id:
        return info.origin_channel_id
    return channel


def origin_permits(cfg: Any, origin_channel: str) -> bool:
    """The origin conversation still admits the bot (activation is not off)."""
    from kiro_crew.config.loader import ACTIVATION_OFF

    try:
        return cfg.channel_config(origin_channel).activation != ACTIVATION_OFF
    except Exception:
        return False


def _audit(sender: str, operation: str, outcome: str, resources: str, error: str = "") -> None:
    sel().log_api_access(
        caller=sender or "unknown",
        operation=operation,
        outcome=outcome,
        source="slack",
        resources=resources,
        error=error,
    )


async def inbound_gate(
    orch: Any,
    event: dict[str, Any],
    *,
    text: str,
    sender_id: str,
    authorized: bool,
    from_trusted_bot: bool,
) -> InboundResult:
    """Decide what a code channel does with *event* (see the module docstring)."""
    cfg = live_config(orch)
    channel = str(event.get("channel") or "")
    if not code_channels_on(cfg) or not recognition.could_be_code_channel(channel):
        return PASS
    if from_trusted_bot or not is_directed_human_prompt(event):
        return PASS
    binding = store().get_binding(channel)
    kind, info = await recognition.classify_channel(_client(orch), channel, _self_user_id())
    if kind is ChannelKind.UNAVAILABLE:
        return PASS
    if binding is not None and kind in (ChannelKind.ORDINARY, ChannelKind.OTHER_AGENT):
        # Slack no longer lists us as this channel's agent: stop answering here.
        logger.info("code channel %s no longer assigned to us; unbinding", channel)
        store().drop_binding(channel)
        return PASS
    if binding is None and kind is not ChannelKind.OURS:
        return PASS
    if is_origin_echo(event, binding):
        return DROP
    if not addressed_to_us(text, _self_user_id()):
        return DROP
    origin = _origin_of(channel, binding, info)
    resources = f"channel={channel} origin={origin}"
    if not authorized:
        _audit(sender_id, "slack.code_channel.message", "denied", resources, "unauthorized sender")
        try:
            await orch.slack.post_ephemeral(channel, sender_id, NOT_AUTHORIZED_TEXT)
        except Exception:
            logger.debug("code channel: ephemeral rejection failed", exc_info=True)
        return DROP
    if not origin_permits(cfg, origin):
        _audit(
            sender_id, "slack.code_channel.message", "denied", resources, "origin activation=off"
        )
        return DROP
    _audit(sender_id, "slack.code_channel.message", "allowed", resources)
    if strip_leading_mention(text).strip().lower() == "!stop":
        await _stop_bound(
            orch, binding, sender_id, channel, source="slack.code_channel.stop_command"
        )
        return DROP
    return InboundResult(Gate.DIRECTED, binding=binding, info=info)


# ── Dispatch ──────────────────────────────────────────────────────────────


async def dispatch(
    orch: Any,
    result: InboundResult | None,
    *,
    event: dict[str, Any],
    clean_text: str,
    sender_id: str,
    was_mention: bool,
) -> bool:
    """Start the turn for a code-channel message; True when handled here."""
    if result is not None and result.gate is Gate.DIRECTED:
        return await _dispatch_directed(orch, result, event, clean_text, sender_id)
    if was_mention and result is not None and result.gate is Gate.PASS:
        return await maybe_open_code_channel(orch, event, clean_text, sender_id)
    return False


async def _post(orch: Any, channel: str, text: str, thread_ts: str | None) -> str:
    try:
        ts = await orch.slack.post_message(channel, text, thread_ts)
    except Exception:
        logger.debug("code channel post failed", exc_info=True)
        return ""
    return str(ts or "")


def _folder_name(state: Any, folder_id: str) -> str:
    for folder in getattr(state, "_folders", None) or ():
        if isinstance(folder, dict) and folder.get("id") == folder_id:
            return str(folder.get("name") or "")
    return ""


def _kickoff_text(title: str, state: Any, folder_id: str) -> str:
    where = _folder_name(state, folder_id) if folder_id else ""
    dashboard = f"the *{where}* folder of your dashboard" if where else "your dashboard"
    heading = f"🛠️ *{title}*\n" if title else ""
    return (
        f"{heading}This channel is one Kiro Crew session: reply here, no @mention "
        f"needed, and use Stop to interrupt me. It is also open in {dashboard}."
    )


async def _bind_channel(
    orch: Any,
    *,
    channel: str,
    team_id: str,
    title: str,
    origin_channel_id: str,
    origin_ts: str,
    origin_thread_ts: str,
    created_by_us: bool,
) -> Binding:
    """Post the kickoff, create the dashboard session and record the binding."""
    state = _dashboard_state()
    if state is None:
        raise BindingUnavailable("no dashboard")
    cfg = live_config(orch)
    spec = await resolve_session_spec(
        state, cfg, code_channel_defaults(cfg), origin_thread_ts=origin_thread_ts
    )
    anchor = await _post(orch, channel, _kickoff_text(title, state, spec.folder_id), None)
    if not anchor:
        raise BindingUnavailable("kickoff post failed")
    slot = await create_bound_session(
        state, spec, title=title or "Slack code channel", channel_id=channel, anchor_ts=anchor
    )
    binding = Binding(
        channel_id=channel,
        team_id=team_id,
        session_key=slot.key,
        anchor_ts=anchor,
        project_dir=spec.project_dir,
        origin_channel_id=origin_channel_id,
        origin_ts=origin_ts,
        created_by_us=created_by_us,
        title=title,
    )
    existing = store().get_binding(channel)
    return (
        store().replace_binding(binding) if existing is not None else store().put_binding(binding)
    )


async def _route_to_slot(orch: Any, binding: Binding, text: str, sender_id: str) -> bool:
    from kiro_crew.slack.handler_runtime.inbound import maybe_route_linked_thread

    return await maybe_route_linked_thread(
        text, binding.session_key, sender_id, binding.channel_id, orch.slack, binding.anchor_ts
    )


async def _dispatch_directed(
    orch: Any, result: InboundResult, event: dict[str, Any], clean_text: str, sender_id: str
) -> bool:
    channel = str(event.get("channel") or "")
    state = _dashboard_state()
    if state is None:
        return False
    binding = result.binding
    try:
        if binding is None:
            # A code channel Slack's UI opened with us as its agent: adopt it.
            info = result.info
            binding = await _bind_channel(
                orch,
                channel=channel,
                team_id=str(event.get("team") or ""),
                title=(info.name if info else "") or "",
                origin_channel_id=info.origin_channel_id if info else "",
                origin_ts=info.origin_ts if info else "",
                origin_thread_ts="",
                created_by_us=False,
            )
            _audit(sender_id, "slack.code_channel.adopt", "allowed", f"channel={channel}")
        elif bound_slot(state, binding) is None:
            # The session was closed in the dashboard: open a fresh one.
            binding = await _bind_channel(
                orch,
                channel=channel,
                team_id=binding.team_id,
                title=binding.title,
                origin_channel_id=binding.origin_channel_id,
                origin_ts=binding.origin_ts,
                origin_thread_ts="",
                created_by_us=binding.created_by_us,
            )
    except BindingUnavailable as exc:
        logger.info("code channel %s: no session to route to (%s)", channel, exc)
        return False
    except Exception:
        logger.exception("code channel %s: binding a session failed", channel)
        return False
    return await _route_to_slot(orch, binding, clean_text, sender_id)


# ── Creation from a mention ───────────────────────────────────────────────


async def _thread_context(
    orch: Any, channel: str, thread_ts: str, msg_ts: str
) -> list[dict[str, str]]:
    """The origin thread's last few messages before this one, newest first."""
    if not thread_ts:
        return []
    fetch = getattr(orch.slack, "fetch_thread_replies", None)
    if fetch is None:
        return []
    try:
        messages = await fetch(channel, thread_ts, limit=50, warn_on_pagination=False)
    except Exception:
        return []
    names = getattr(getattr(orch, "channel_history", None), "_user_names", {}) or {}
    out: list[dict[str, str]] = []
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("ts") == msg_ts:
            continue
        user = str(message.get("user") or message.get("bot_id") or "")
        out.append({"author": str(names.get(user) or user), "text": str(message.get("text") or "")})
        if len(out) >= MAX_CONTEXT_MESSAGES:
            break
    return out


def _seed_prompt(request: str, origin_channel: str, context: list[dict[str, str]]) -> str:
    lines = [request.strip(), "", "[Slack code channel]"]
    lines.append(
        f"This session is a Slack code channel opened from a thread in <#{origin_channel}>. "
        "Your replies are posted to that channel."
    )
    if context:
        lines.append("Earlier messages in that thread, newest first:")
        lines.extend(f"- {c['author']}: {c['text'][:500]}" for c in context)
    return "\n".join(lines)


async def maybe_open_code_channel(
    orch: Any, event: dict[str, Any], clean_text: str, sender_id: str
) -> bool:
    """Open a code channel for a mention that asks for multi-step work.

    True when a channel was opened (or had already been opened for this very
    message) and the work moved there; False to answer in place as usual.
    """
    cfg = live_config(orch)
    channel = str(event.get("channel") or "")
    msg_ts = str(event.get("ts") or "")
    thread_ts = str(event.get("thread_ts") or "")
    team_id = str(event.get("team") or "")
    if not code_channels_on(cfg) or not is_directed_human_prompt(event):
        return False
    if store().get_binding(channel) is not None:
        return False  # already a code channel: dispatch() owns it
    state = _dashboard_state()
    client = _client(orch)
    if state is None or client is None or not clean_text.strip():
        return False

    session_id = session_id_for(_self_bot_id(), team_id, channel, msg_ts)
    route = store().get_route(session_id)
    context: list[dict[str, str]] = []
    if route is None or not route.decision:
        context = await _thread_context(orch, channel, thread_ts, msg_ts)
        decision = await classify(getattr(orch, "sessions", None), clean_text, context)
        route = store().record_decision(
            Route(
                session_id=session_id,
                origin_channel_id=channel,
                origin_ts=msg_ts,
                team_id=team_id,
                decision=decision.route,
                title=decision.title,
                created_at=time.time(),
            )
        )
    if route.decision != ROUTE_CODE_CHANNEL:
        return False
    if route.channel_id and store().get_binding(route.channel_id) is not None:
        return True  # a re-delivered event: this message already has its channel

    resources = f"origin={channel} session_id={session_id[:12]}"
    request = CreateRequest(
        name=channel_name_for(route.title),
        session_id=session_id,
        origin_channel_id=channel,
        origin_message_ts=msg_ts,
        team_id=team_id,
    )
    try:
        try:
            new_channel = await client.create(request)
        except CodeChannelApiError as exc:
            if not exc.maybe_applied:
                raise
            # The create may have landed. Same session_id = same channel, so
            # asking once more reconciles instead of duplicating.
            new_channel = await client.create(request)
    except CodeChannelApiError as exc:
        _audit(sender_id, "slack.code_channel.create", "error", resources, exc.code)
        logger.info("code channel not opened (%s); answering in place", exc.code)
        return False
    _audit(sender_id, "slack.code_channel.create", "allowed", f"{resources} channel={new_channel}")
    store().record_destination(session_id, new_channel)

    try:
        binding = await _bind_channel(
            orch,
            channel=new_channel,
            team_id=team_id,
            title=route.title,
            origin_channel_id=channel,
            origin_ts=msg_ts,
            origin_thread_ts=thread_ts or msg_ts,
            created_by_us=True,
        )
    except Exception:
        logger.exception("code channel %s opened but its session could not be bound", new_channel)
        await _post(
            orch,
            channel,
            f"I opened <#{new_channel}> but could not start its session. Mention me there to retry.",
            thread_ts or msg_ts,
        )
        return True
    if not context:
        context = await _thread_context(orch, channel, thread_ts, msg_ts)
    if not await _route_to_slot(
        orch, binding, _seed_prompt(clean_text, channel, context), sender_id
    ):
        logger.warning("code channel %s: first turn could not be routed", new_channel)
    await _post(
        orch, channel, f"Opened <#{new_channel}> for this. I'll work there.", thread_ts or msg_ts
    )
    return True


# ── Stop ──────────────────────────────────────────────────────────────────


async def _stop_bound(
    orch: Any,
    binding: Binding | None,
    sender_id: str,
    channel: str,
    *,
    source: str,
    say_idle: bool = True,
) -> str:
    state = _dashboard_state()
    slot = bound_slot(state, binding) if binding is not None else None
    if state is None or slot is None or not getattr(slot, "running", False):
        _audit(sender_id, source, "allowed", f"channel={channel} result=idle")
        # Nothing to stop, but Slack never clears "processing" by itself.
        from kiro_crew.slack.code_channels.status import set_status

        await set_status(channel, "active")
        if say_idle:
            await _post(orch, channel, "Nothing running.", None)
        return "idle"
    from kiro_crew.dashboard.chat_api.turn_stop import stop_slot_turn

    result = await stop_slot_turn(state, slot, source="slack", escalate=False)
    info = str(result.get("info") or "stopping")
    _audit(sender_id, source, "allowed", f"channel={channel} slot={slot.key} result={info}")
    return info


async def handle_session_stopped(orch: Any, event: dict[str, Any], *, team_id: str = "") -> bool:
    """Slack's Stop button in a code channel; True when the channel is ours."""
    channel = str(event.get("channel") or "")
    if event.get("thread_ts"):
        return False  # a thread session, not a code channel
    if not code_channels_on(live_config(orch)):
        return False
    binding = store().get_binding(channel)
    if binding is None:
        return False
    user = str(event.get("user") or "")
    from kiro_crew.platform import current_context
    from kiro_crew.slack.handler_runtime.access import is_allowed_user

    if not current_context().slack_gate.check_message_origin(team_id):
        _audit(
            user,
            "slack.code_channel.stop",
            "denied",
            f"channel={channel}",
            "enterprise_origin_mismatch",
        )
        return True
    if not is_allowed_user(user):
        _audit(
            user, "slack.code_channel.stop", "denied", f"channel={channel}", "unauthorized sender"
        )
        return True
    if not origin_permits(live_config(orch), _origin_of(channel, binding, None)):
        _audit(
            user, "slack.code_channel.stop", "denied", f"channel={channel}", "origin activation=off"
        )
        return True
    await _stop_bound(
        orch, binding, user, channel, source="slack.code_channel.stop", say_idle=False
    )
    return True
