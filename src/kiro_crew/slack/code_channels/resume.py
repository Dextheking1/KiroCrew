"""Resume code channels that a gateway restart interrupted.

A code channel's binding survives a restart (``slack-code-channels.json``), so the
next message already reaches the same session. What did not survive is a turn
that was running: nothing picks it up, and the channel still shows Slack's
"Working..." loader. :func:`resume_after_restart` runs once per gateway start,
after the memory barrier and once Slack is connected.

**Whose work.** Only a channel whose binding, live Slack-link index and slot all
agree it is the channel's session (the dispatch exemption's strict identity), in
a channel that is admitted right now: not archived, fenced, stopped or wrapped
up, in an admitted workspace, let in by its channel and origin activation and by
channels governance. Then that session, and each of ITS OWN workers -- recorded
by the store at birth or listed by the crew log's session tree, whose live
``created_by`` still names it and which is linked to no channel. Nothing else.

**Automatic, once.** Each interrupted turn (``is_turn_interrupted``, the signal
``session_continue`` uses for "resumed") is continued through the Resume button's
own path (``continue_slot_turn(require_interrupted=True)``: the fixed "resumed"
body, no prompt text). Before that, the turn -- the ts of the row that opened it;
a Resume's recovery row continues it, so a second interruption names the same
turn -- is recorded in the write-protected code-channel store. A recorded turn is
never resumed automatically again, so a turn that takes the gateway down cannot
loop; a turn whose opening row is no longer in the window has no provable
identity and is never resumed automatically; a store that cannot be written
means no automatic resume. Identity and admission are re-checked under the slot
lock immediately before the continuation is queued.

**One notice**, top level, after the stale status is settled: "picking up where I
left off" when everything resumed, else "The gateway restarted while I was
working." with an owner-only **Resume** button. The button follows the
session-resume button pattern of ``slack/interactions.py`` (an action-id prefix
plus a short hash, a JSON value, prefix-dispatched) under the gateway's reserved
``kc_code_channel_`` prefix. It is the owner's explicit "resume what is
interrupted here now", as the dashboard's Resume control is: it counts only on the
notice ts the store recorded, is claimed by compare-and-set, re-runs every
identity and admission check, and is given back (still pressable) when nothing
could be resumed. A notice whose set of held sessions already has a button is not
posted again; when a notice cannot be posted or recorded, the owner is told in
the dashboard, with a link to each held session.

**Work board.** :func:`subscribe` receives each channel's :class:`ResumeReport`
(resumed / held / lost). Every decision is SEL-audited with the channel id.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.slack.code_channels import surface
from kiro_crew.slack.code_channels.binding import code_channels_on, live_config
from kiro_crew.slack.code_channels.store import Binding, store

logger = logging.getLogger(__name__)

OPERATION = "slack.code_channel.resume"
RESUME_ACTION_PREFIX = "kc_code_channel_resume_"

AUTO_TEXT = "The gateway restarted while I was working; picking up where I left off."
HELD_TEXT = "The gateway restarted while I was working."
TRUST_NOTE = "This channel runs in Trust, so the resumed work runs without prompts."
BUTTON_TEXT = "Resume"
PRESSED_TEXT = "Resumed by <@{user}>."
NOTHING_TEXT = "Nothing could be resumed right now ({reason}). Try Resume again in a moment."
PARTIAL_TEXT = (
    "Resumed {count}; the rest could not be resumed right now ({reason}). "
    "Press Resume again in a moment for the rest."
)
NOT_OWNER_TEXT = "⛔ Only the owner can resume this channel's work."
STALE_TEXT = "That Resume button is out of date."
NOT_ALLOWED_TEXT = "⛔ You can't resume work from this channel."

#: How long the start-up pass waits for Slack to connect before giving up.
CONNECT_WAIT_S = 120.0
#: What :func:`_interrupted_turn` answers for an interrupted turn whose opening row
#: is no longer in the window. Never resumed automatically: missing identity is not
#: a new turn.
UNKNOWN_TURN = "?"

_NOTICE_PREFIX = "notice:"


@dataclass
class ResumeReport:
    """What one channel's restart pass did, for the work board."""

    channel_id: str
    resumed: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    lost: list[str] = field(default_factory=list)
    #: Interrupted-turn label per resumed or held key (``?`` when unknown).
    turns: dict[str, str] = field(default_factory=dict)


_subscribers: list[Callable[[ResumeReport], Awaitable[None] | None]] = []


def subscribe(callback: Callable[[ResumeReport], Awaitable[None] | None]) -> None:
    """Call *callback* with each channel's :class:`ResumeReport` (the work board)."""
    _subscribers.append(callback)


def unsubscribe_all() -> None:
    """Drop every subscriber (tests)."""
    _subscribers.clear()


# ── reading the transcript ─────────────────────────────────────────────────


def opening_turn_ts(messages: list[dict[str, Any]]) -> str:
    """The ts of the row that opened the newest turn, ``""`` when none.

    A Resume's recovery row CONTINUES the turn above it (it is not an opener), so
    a resumed turn that is interrupted again names the same opening row.
    """
    from kiro_crew.dashboard.state import TURN_OPENING_INJECT_KINDS

    for row in reversed(messages or []):
        role = row.get("role")
        meta = row.get("meta") or {}
        if role in ("user", "nudge"):
            return str(row.get("ts") or "")
        if role == "inject" and isinstance(meta, dict):
            if meta.get("injectKind") in TURN_OPENING_INJECT_KINDS:
                return str(row.get("ts") or "")
    return ""


async def _interrupted_turn(slot: Any) -> str:
    """The opening ts of *slot*'s interrupted last turn; ``""`` when it finished."""
    from kiro_crew.dashboard.state import is_turn_interrupted, load_window_off_loop

    await load_window_off_loop(slot)
    messages = list(getattr(slot, "messages", None) or [])
    if not is_turn_interrupted(messages):
        return ""
    return opening_turn_ts(messages) or UNKNOWN_TURN


def _same_opening(slot: Any, turn: str) -> str:
    """``""`` while *slot*'s newest opening row is still *turn* (no disk access)."""
    try:
        current = opening_turn_ts(list(getattr(slot, "messages", None) or []))
    except Exception:
        return "transcript unavailable"
    want = "" if turn == UNKNOWN_TURN else turn
    return "" if current == want else "a newer turn started"


# ── whose work, and may the channel be touched now ─────────────────────────


def _is_the_channels_session(state: Any, binding: Binding, slot: Any) -> bool:
    """*slot* IS *binding*'s session by the strict identity the exemption uses."""
    from kiro_crew.dashboard.session_control_parts.caller_access import code_channel_binding_of

    if slot is None or str(getattr(slot, "key", "")) != binding.session_key:
        return False
    if state is None or state.get_slot(binding.session_key) is not slot:
        return False
    found = code_channel_binding_of(state, slot)
    return found is not None and found.channel_id == binding.channel_id


def _is_own_worker(state: Any, conductor_key: str, worker: Any) -> bool:
    """*worker* is an ordinary dashboard session this conductor created."""
    from kiro_crew.dashboard.session_control_parts.caller_access import (
        _channel_link_of,
        code_channel_binding_of,
    )

    if worker is None or not conductor_key:
        return False
    if str(getattr(worker, "_created_by", "") or "") != conductor_key:
        return False
    if _channel_link_of(worker) or getattr(worker, "_slack_thread_ts", ""):
        return False
    return code_channel_binding_of(state, worker) is None


MIRRORED = "mirrored"
UNVERIFIED = "unverified"


def _worker_mirror(state: Any, worker: Any) -> str:
    """``""`` when *worker* has no outbound channel mirror; never waits.

    A dashboard-born worker given an outbound mirror reaches a channel as surely
    as a channel-born one, so it is never resumed (:data:`MIRRORED`). The link
    lives in the session map, whose lock an off-loop writer can hold through a
    disk write; this runs on the event loop (under the slot lock, right before a
    continue is queued), so it only TRIES the lock and answers
    :data:`UNVERIFIED` when it is taken, as for an unreadable link. Both refuse.
    """
    from kiro_crew.dashboard.session_control import _has_channel_mirror
    from kiro_crew.session_map import _MAP_LOCK

    if not _MAP_LOCK.acquire(blocking=False):
        return UNVERIFIED
    try:
        # Re-entrant: the probe's own guarded reads take the lock held here.
        probed = _has_channel_mirror(state, worker, on_probe_failure=True)
    except Exception:
        return UNVERIFIED
    finally:
        _MAP_LOCK.release()
    return MIRRORED if probed else ""


def _own_workers(conductor_key: str) -> list[str]:
    """The conductor's recorded workers: server-written records plus the crew-log tree."""
    keys = set(store().workers_of(conductor_key))
    try:
        from kiro_crew.dashboard.session_control_parts.session_status import (
            _created_tree_roster,
        )

        children, _quality = _created_tree_roster(conductor_key)
        keys.update(children)
    except Exception:  # pragma: no cover - the store records still stand
        logger.debug("code channel resume: crew-log roster unavailable", exc_info=True)
    keys.discard(conductor_key)
    return sorted(keys)


def _admitted_now(state: Any, channel_id: str, conductor: Any) -> str:
    """Why *channel_id* may not be resumed now; ``""`` when it may. No disk access.

    The live binding (archived, wrapped up), this process's archive fence and Stop,
    the workspace, the channel and origin activation, and the conductor's strict
    identity. Run before work starts and again under the slot lock right before a
    continuation is queued.
    """
    from kiro_crew.slack.code_channels.routing import _origin_of, channel_and_origin_permit

    if not code_channels_on(live_config()):
        return "code channels are off"
    binding = store().get_binding(channel_id)
    if binding is None:
        return "no longer a bound code channel"
    if binding.archived_at or binding.wrapped_up_at:
        return "archived or wrapped up"
    if channel_id in getattr(surface, "_archived_here", ()) or surface.channel_stopped(channel_id):
        return "archived or stopped"
    if not surface.workspace_admitted(binding.team_id):
        return "workspace not admitted"
    origin = _origin_of(channel_id, binding, None)
    if not channel_and_origin_permit(live_config(), channel_id, origin):
        return "channel or origin does not admit the bot"
    if not _is_the_channels_session(state, binding, conductor):
        return "not the channel's session"
    return ""


async def _channel_admitted(state: Any, channel_id: str, conductor: Any) -> str:
    """:func:`_admitted_now` plus channels governance (an await)."""
    why = _admitted_now(state, channel_id, conductor)
    if why:
        return why
    if not await surface.outbound_permitted():
        return "channels governance policy"
    return ""


# ── continuing ─────────────────────────────────────────────────────────────


@contextmanager
def _session_map_untaken() -> Iterator[None]:
    """Hold the session map's lock for the continue's final synchronous stretch.

    The re-check, the containment snapshot and the queue insert all read the
    session map, whose lock an off-loop writer can hold through a disk write; on
    the event loop that read would stall every task. So the lock is only TRIED:
    taken, every reader inside re-enters it without waiting; contended, the
    continue is refused (and held for the Resume button). Never held across an
    await: ``continue_slot_turn`` runs no await inside the guard.
    """
    from kiro_crew.dashboard.chat_handlers import SlotContinueRefusal
    from kiro_crew.session_map import _MAP_LOCK

    if not _MAP_LOCK.acquire(blocking=False):
        raise SlotContinueRefusal("session map busy", "code_channel_resume_busy")
    try:
        yield
    finally:
        _MAP_LOCK.release()


async def _continue(state: Any, slot: Any, still_ok: Callable[[], str] | None = None) -> str:
    """Continue *slot*'s interrupted turn; ``""`` when queued, else the refusal code.

    *still_ok* runs under the slot lock immediately before the queue insert
    (``before_dispatch``) and refuses on any answer but ``""``.
    """
    from kiro_crew.dashboard.chat_handlers import SlotContinueRefusal, continue_slot_turn

    def _recheck() -> None:
        why = still_ok() if still_ok is not None else ""
        if why:
            raise SlotContinueRefusal(why, "code_channel_resume_changed")

    async def _governance() -> None:
        # After the continue's last await: a policy revoked while it waited stops
        # the continuation here (the evaluation itself runs off the loop).
        if not await surface.outbound_permitted():
            raise SlotContinueRefusal("channels governance policy", "code_channel_resume_changed")

    try:
        body = await continue_slot_turn(
            state,
            slot,
            directive_user_origin=False,
            via="code_channel_resume",
            require_interrupted=True,
            prepare_dispatch=_governance,
            dispatch_guard=_session_map_untaken,
            before_dispatch=_recheck,
        )
    except SlotContinueRefusal as exc:
        return exc.code or "refused"
    except Exception:  # pragma: no cover - a failed continue is held back, not raised
        logger.warning("code channel resume: continue failed", exc_info=True)
        return "error"
    return "" if body == "resumed" else f"unexpected_{body}"


def _recheck_for(
    state: Any, channel_id: str, conductor: Any, slot: Any, turn: str
) -> Callable[[], str]:
    """The under-lock re-check for one continuation of *slot* (conductor or worker)."""
    key = str(getattr(slot, "key", "") or "")
    conductor_key = str(getattr(conductor, "key", "") or "")

    def _check() -> str:
        why = _admitted_now(state, channel_id, conductor)
        if why:
            return why
        if state.get_slot(key) is not slot:
            return "the session changed"
        if slot is not conductor:
            if not _is_own_worker(state, conductor_key, slot):
                return "the worker changed"
            mirror = _worker_mirror(state, slot)
            if mirror:
                return f"worker mirror {mirror}"
        return _same_opening(slot, turn)

    return _check


async def _resume_slot(
    state: Any, conductor: Any, slot: Any, channel_id: str, report: ResumeReport
) -> None:
    """Resume one interrupted slot automatically, at most once per interrupted turn."""
    key = str(slot.key)
    turn = await _interrupted_turn(slot)
    if not turn:
        return
    report.turns[key] = turn
    resources = f"channel={channel_id} session={key} turn={turn}"
    if turn == UNKNOWN_TURN:
        surface.audit("system", OPERATION, "denied", resources, "turn identity unknown")
        report.held.append(key)
        return
    record = await asyncio.to_thread(store().resume_record, key)
    if record is not None and record.get("turn") == turn:
        # Already tried for this very turn: its resumed run was interrupted again,
        # or the gateway went down during it. Never twice.
        surface.audit("system", OPERATION, "denied", resources, "already resumed once")
        report.held.append(key)
        return
    try:
        await asyncio.to_thread(
            store().record_resume, key, {"turn": turn, "at": str(int(time.time()))}
        )
    except Exception:
        # No durable record, no automatic resume: the loop guard must exist first.
        surface.audit("system", OPERATION, "error", resources, "attempt not recorded")
        report.held.append(key)
        return
    refused = await _continue(state, slot, _recheck_for(state, channel_id, conductor, slot, turn))
    if refused:
        surface.audit("system", OPERATION, "denied", resources, f"continue refused: {refused}")
        report.held.append(key)
        return
    surface.audit("system", OPERATION, "allowed", f"{resources} result=resumed")
    report.resumed.append(key)


async def resume_channel(state: Any, binding: Binding) -> ResumeReport | None:
    """Resume one channel's interrupted work and post its notice; ``None`` if skipped."""
    from kiro_crew.slack.code_channels.binding import bound_slot

    channel = binding.channel_id
    conductor = bound_slot(state, binding)
    why = await _channel_admitted(state, channel, conductor)
    if not why and await surface.is_archived(binding):
        why = "archived"
    if why:
        surface.audit("system", OPERATION, "denied", f"channel={channel}", why)
        return None
    report = ResumeReport(channel_id=channel)
    await _resume_slot(state, conductor, conductor, channel, report)
    conductor_key = str(getattr(conductor, "key", "") or "")
    for worker_key in _own_workers(conductor_key):
        worker = state.get_slot(worker_key)
        if worker is None:
            # Gone with the process: nothing to continue, and the board says so.
            report.lost.append(worker_key)
            continue
        if not _is_own_worker(state, conductor_key, worker):
            surface.audit(
                "system",
                OPERATION,
                "denied",
                f"channel={channel} session={worker_key}",
                "not this channel's worker any more",
            )
            continue
        if _worker_mirror(state, worker) == MIRRORED:
            surface.audit(
                "system",
                OPERATION,
                "denied",
                f"channel={channel} session={worker_key}",
                "the worker mirrors to a channel",
            )
            continue
        await _resume_slot(state, conductor, worker, channel, report)
    report.lost.extend(k for k in report.held if k != conductor_key)
    if report.resumed or report.held:
        await _notify(state, binding, conductor, report)
    for callback in list(_subscribers):
        try:
            result = callback(report)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # pragma: no cover - a board failure never stops the pass
            logger.debug("code channel resume subscriber failed", exc_info=True)
    return report


async def resume_after_restart(state: Any) -> list[ResumeReport]:
    """The start-up pass over every bound code channel (see the module docstring)."""
    if not code_channels_on(live_config()):
        return []
    try:
        await asyncio.to_thread(store().load)
        bindings = await asyncio.to_thread(store().bindings)
    except Exception:
        logger.warning("code channel resume: store unavailable", exc_info=True)
        return []
    reports: list[ResumeReport] = []
    for binding in bindings:
        try:
            report = await resume_channel(state, binding)
        except Exception:
            logger.warning("code channel %s: resume failed", binding.channel_id, exc_info=True)
            continue
        if report is not None:
            reports.append(report)
    return reports


async def resume_when_ready(
    state: Any,
    connected: Callable[[], bool],
    *,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    """Wait (bounded) for Slack to connect, then run :func:`resume_after_restart`."""
    for _ in range(int(CONNECT_WAIT_S)):
        if connected():
            await resume_after_restart(state)
            return
        await sleep(1.0)
    logger.info("code channel resume: Slack did not connect; skipped")


# ── the notice ─────────────────────────────────────────────────────────────


def _short_id(channel_id: str, signature: str) -> str:
    return hashlib.sha256(f"{channel_id}|{signature}".encode()).hexdigest()[:12]


def _signature(keys: list[str], turns: dict[str, str]) -> str:
    """Which sessions, at which interrupted turns, a notice is about (dedupe key)."""
    return ",".join(f"{k}@{turns.get(k, '')}" for k in sorted(keys))


def notice_blocks(channel_id: str, signature: str, *, held: bool, trust: bool) -> list[dict]:
    """The restart notice: one line, the Trust note, and (when held) the Resume button."""
    text = HELD_TEXT if held else AUTO_TEXT
    if trust:
        text = f"{text} {TRUST_NOTE}"
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if held:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": f"{RESUME_ACTION_PREFIX}{_short_id(channel_id, signature)}",
                        "value": json.dumps({"channel": channel_id}),
                        "text": {"type": "plain_text", "text": BUTTON_TEXT},
                    }
                ],
            }
        )
    return blocks


async def _notify(state: Any, binding: Binding, conductor: Any, report: ResumeReport) -> None:
    """Settle the stale status and post the one notice, deduplicated by store record."""
    from kiro_crew.slack.code_channels import status

    channel = binding.channel_id
    resources = f"channel={channel}"
    held = bool(report.held)
    keys = report.held if held else report.resumed
    signature = _signature(keys, report.turns)
    notice_key = f"{_NOTICE_PREFIX}{channel}"
    previous = await asyncio.to_thread(store().resume_record, notice_key)
    if previous and previous.get("sig") == signature and previous.get("state") == "button":
        # This held set already has its button: a further restart posts none.
        surface.audit("system", f"{OPERATION}.notice", "denied", resources, "already posted")
        return
    slack = surface.slack()
    why = await _channel_admitted(state, channel, conductor)
    if slack is None or why:
        surface.audit("system", f"{OPERATION}.notice", "denied", resources, why or "no Slack")
        if held:
            _notify_dashboard(state, binding, report.held)
        return
    try:
        await status.settle_after_stop(channel)
    except Exception:  # pragma: no cover - decoration only
        logger.debug("code channel resume: status not settled", exc_info=True)
    trust = bool(getattr(conductor, "_trust", False))
    blocks = notice_blocks(channel, signature, held=held, trust=trust)
    try:
        ts = str(await slack.post_blocks(channel, blocks, blocks[0]["text"]["text"]) or "")
    except Exception:
        logger.warning("code channel %s: restart notice outcome unknown", channel, exc_info=True)
        ts = ""
    if not ts:
        surface.audit("system", f"{OPERATION}.notice", "error", resources, "not posted")
        if held:
            _notify_dashboard(state, binding, report.held)
        return
    try:
        await asyncio.to_thread(
            store().record_resume,
            notice_key,
            {"sig": signature, "state": "button" if held else "auto", "notice_ts": ts},
        )
    except Exception:
        # Posted but not recorded: its button will be refused as stale, so say so.
        logger.warning("code channel %s: restart notice not recorded", channel, exc_info=True)
        if held:
            _notify_dashboard(state, binding, report.held)
    surface.audit(
        "system",
        f"{OPERATION}.notice",
        "allowed",
        f"{resources} message={ts} held={len(report.held)} resumed={len(report.resumed)}",
    )


def _notify_dashboard(state: Any, binding: Binding, held: list[str]) -> None:
    """Tell the owner in the dashboard which held sessions to resume, with links."""
    notify = getattr(state, "notify", None)
    if not callable(notify) or not held:
        return
    from kiro_crew.dashboard.session_control_parts.code_channel_exemption import (
        worker_dashboard_link,
    )

    lines = []
    for key in held:
        link = worker_dashboard_link(key)
        lines.append(f"- {link or key}")
    try:
        notify(
            "slack",
            "Code channel work to resume",
            "The gateway restarted while a code channel was working, and its Resume "
            "button could not be offered in the channel. Open each session below and "
            "press Resume:\n" + "\n".join(lines),
            meta={"channel_id": binding.channel_id, "session_key": binding.session_key},
        )
    except Exception:  # pragma: no cover
        logger.debug("restart-resume dashboard notification failed", exc_info=True)


# ── the Resume button ──────────────────────────────────────────────────────


async def _ephemeral(channel: str, user: str, text: str) -> None:
    slack = surface.slack()
    if slack is None or not (channel and user):
        return
    try:
        await slack.post_ephemeral(channel, user, text)
    except Exception:
        logger.debug("code channel ephemeral failed", exc_info=True)


async def handle_resume_click(orch: Any, payload: dict[str, Any]) -> bool:
    """A Resume click on a restart notice; False when it is not ours to answer."""
    if not code_channels_on(live_config(orch)):
        return False
    from kiro_crew.slack.code_channels.binding import bound_slot
    from kiro_crew.slack.handler import is_owner

    channel = str((payload.get("channel") or {}).get("id") or "")
    user = str((payload.get("user") or {}).get("id") or "")
    msg_ts = str((payload.get("message") or {}).get("ts") or "")
    action = (payload.get("actions") or [{}])[0] or {}
    try:
        value = json.loads(str(action.get("value") or "{}"))
    except ValueError:
        value = {}
    resources = f"channel={channel} message={msg_ts}"
    team = str(
        (payload.get("team") or {}).get("id") or (payload.get("user") or {}).get("team_id") or ""
    )
    if not surface.workspace_admitted(team):
        surface.audit(user, OPERATION, "denied", f"{resources} team_id={team}", "workspace")
        return True
    binding = await asyncio.to_thread(store().get_binding, channel) if channel else None
    if binding is None or not isinstance(value, dict) or value.get("channel") != channel:
        surface.audit(user, OPERATION, "denied", resources, "not a bound code channel")
        await _ephemeral(channel, user, NOT_ALLOWED_TEXT)
        return True
    if not is_owner(user):
        surface.audit(user, OPERATION, "denied", resources, "not the owner")
        await _ephemeral(channel, user, NOT_OWNER_TEXT)
        return True
    state = surface.dashboard_state()
    conductor = bound_slot(state, binding)
    why = await _channel_admitted(state, channel, conductor)
    if not why and await surface.is_archived(binding):
        why = "archived"
    if why:
        surface.audit(user, OPERATION, "denied", resources, why)
        await _ephemeral(channel, user, NOT_ALLOWED_TEXT)
        return True
    notice_key = f"{_NOTICE_PREFIX}{channel}"
    record = await asyncio.to_thread(store().resume_record, notice_key)
    if record is None or record.get("state") != "button" or record.get("notice_ts") != msg_ts:
        surface.audit(user, OPERATION, "denied", resources, "not the current restart notice")
        await _ephemeral(channel, user, STALE_TEXT)
        return True
    pressed = {**record, "state": "pressed"}
    # Claimed atomically: of two deliveries of the same press, one wins.
    if not await asyncio.to_thread(store().replace_resume_if, notice_key, record, pressed):
        surface.audit(user, OPERATION, "denied", resources, "already pressed")
        await _ephemeral(channel, user, STALE_TEXT)
        return True
    resumed, refusals = await _resume_on_press(state, channel, conductor, user, resources)
    slack = surface.slack()
    if not resumed or refusals:
        # Something could not run: give the button back so the owner can try
        # again (what did resume is no longer interrupted, so it is not re-run).
        await asyncio.to_thread(store().replace_resume_if, notice_key, pressed, record)
        reason = ", ".join(sorted(set(refusals))) or "nothing is interrupted"
        if resumed:
            text = PARTIAL_TEXT.format(count=len(resumed), reason=reason)
        else:
            text = NOTHING_TEXT.format(reason=reason)
        await _ephemeral(channel, user, text)
        return True
    if slack is not None:
        try:
            done = [
                {"type": "section", "text": {"type": "mrkdwn", "text": HELD_TEXT}},
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": PRESSED_TEXT.format(user=user)}],
                },
            ]
            await slack.update_message(channel, msg_ts, HELD_TEXT, done)
        except Exception:
            logger.debug("code channel resume notice not updated", exc_info=True)
    return True


async def _resume_on_press(
    state: Any, channel: str, conductor: Any, user: str, resources: str
) -> tuple[list[str], list[str]]:
    """Continue the channel's own interrupted sessions for the owner's press."""
    conductor_key = str(getattr(conductor, "key", "") or "")
    candidates: list[Any] = [conductor]
    for key in _own_workers(conductor_key):
        worker = state.get_slot(key) if state is not None else None
        if (
            worker is not None
            and _is_own_worker(state, conductor_key, worker)
            and _worker_mirror(state, worker) != MIRRORED
        ):
            candidates.append(worker)
    resumed: list[str] = []
    refusals: list[str] = []
    for slot in candidates:
        turn = await _interrupted_turn(slot)
        if not turn:
            continue
        key = str(slot.key)
        refused = await _continue(state, slot, _recheck_for(state, channel, conductor, slot, turn))
        surface.audit(
            user,
            OPERATION,
            "denied" if refused else "allowed",
            f"{resources} session={key} via=button",
            f"continue refused: {refused}" if refused else "",
        )
        if refused:
            refusals.append(refused)
            continue
        resumed.append(key)
        if turn != UNKNOWN_TURN:
            # This turn has now been resumed: a later restart holds it for the button.
            try:
                await asyncio.to_thread(
                    store().record_resume, key, {"turn": turn, "at": str(int(time.time()))}
                )
            except Exception:  # pragma: no cover
                logger.debug("code channel resume: press not recorded", exc_info=True)
    return resumed, refusals
