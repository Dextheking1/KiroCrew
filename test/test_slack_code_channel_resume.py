"""Resuming Slack code channels a gateway restart interrupted.

The pass continues an interrupted channel session and ITS OWN interrupted
workers once per interrupted turn, posts one notice, and falls back to an
owner-only Resume button when a turn was already resumed once (its resumed run
was interrupted again, or the gateway went down during it).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_session_control_owner_dm import _state
from test_slack_code_channel_dispatch_exemption import ANCHOR, CHANNEL, OWNER, TEAM, _channel

from kiro_crew.slack import handler as slack_handler
from kiro_crew.slack.code_channels import resume, routing, status
from kiro_crew.slack.code_channels import surface as cc_surface
from kiro_crew.slack.code_channels.store import Binding, set_store

OTHER = "U0OTHER0001"
_REAL_CONTINUE = resume._continue


class _Slack:
    def __init__(self) -> None:
        self.posts: list[tuple[str, list[dict], str]] = []
        self.updates: list[tuple[str, str, str, list[dict] | None]] = []
        self.ephemerals: list[tuple[str, str, str]] = []

    async def post_blocks(self, channel: str, blocks: list[dict], text: str, thread_ts=None) -> str:
        self.posts.append((channel, blocks, text))
        return f"1700000000.00000{len(self.posts)}"

    async def update_message(self, channel: str, ts: str, text: str = "", blocks=None) -> None:
        self.updates.append((channel, ts, text, blocks))

    async def post_ephemeral(self, channel: str, user: str, text: str) -> None:
        self.ephemerals.append((channel, user, text))


@pytest.fixture
def world(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, store = _channel(state, tmp_path)
    slack = _Slack()
    audits: list[tuple] = []
    continued: list[str] = []

    async def _fake_continue(state_: Any, target: Any, still_ok: Any = None) -> str:
        if still_ok is not None and still_ok():
            return "code_channel_resume_changed"
        continued.append(target.key)
        return ""

    monkeypatch.setattr(resume, "_continue", _fake_continue)
    monkeypatch.setattr(resume, "code_channels_on", lambda cfg: True)
    monkeypatch.setattr(resume, "live_config", lambda orch=None: object())
    monkeypatch.setattr(cc_surface, "slack", lambda: slack)
    monkeypatch.setattr(cc_surface, "audit", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(cc_surface, "outbound_permitted", AsyncMock(return_value=True))
    monkeypatch.setattr(cc_surface, "is_archived", AsyncMock(return_value=False))
    monkeypatch.setattr(cc_surface, "workspace_admitted", lambda team: team == TEAM)
    monkeypatch.setattr(cc_surface, "dashboard_state", lambda: state)
    monkeypatch.setattr(status, "settle_after_stop", AsyncMock())
    monkeypatch.setattr(routing, "_origin_of", lambda channel, binding, x: "C0ORIGIN01")
    monkeypatch.setattr(routing, "channel_and_origin_permit", lambda cfg, channel, origin: True)
    monkeypatch.setattr(slack_handler, "is_owner", lambda user: user == OWNER)
    resume.unsubscribe_all()
    yield SimpleNamespace(
        state=state, slot=slot, store=store, slack=slack, audits=audits, continued=continued
    )
    resume.unsubscribe_all()
    set_store(None)


def _interrupt(slot: Any, ts: str = "2026-10-19T10:00:00+00:00") -> None:
    """The transcript a restart leaves mid-turn: a request with no reply."""
    slot.messages.append({"role": "user", "content": "fix it", "ts": ts})


def _restart(world) -> list[resume.ResumeReport]:
    return asyncio.run(resume.resume_after_restart(world.state))


def _texts(world) -> list[str]:
    return [blocks[0]["text"]["text"] for _, blocks, _ in world.slack.posts]


def test_an_interrupted_channel_auto_resumes_once(world):
    _interrupt(world.slot)
    (report,) = _restart(world)
    assert world.continued == [world.slot.key]
    assert report.resumed == [world.slot.key] and not report.held
    assert _texts(world) == [resume.AUTO_TEXT]
    assert not any(b.get("type") == "actions" for b in world.slack.posts[0][1])
    status.settle_after_stop.assert_awaited_with(CHANNEL)
    assert any(a[1] == resume.OPERATION and a[2] == "allowed" for a in world.audits)
    record = world.store.resume_record(world.slot.key)
    assert record and record["turn"] == "2026-10-19T10:00:00+00:00"


def test_the_attempt_is_recorded_before_the_continue(world, monkeypatch):
    seen: list[Any] = []

    async def _check(state_: Any, target: Any, still_ok: Any = None) -> str:
        seen.append(world.store.resume_record(target.key))
        return ""

    monkeypatch.setattr(resume, "_continue", _check)
    _interrupt(world.slot)
    _restart(world)
    assert seen and seen[0] is not None, "the loop guard exists before the turn runs"


def test_a_second_interruption_of_the_same_turn_posts_the_button(world):
    _interrupt(world.slot)
    _restart(world)
    # The resumed run is interrupted too (its recovery row continues the same turn).
    world.slot.messages.append(
        {
            "role": "inject",
            "content": "resume",
            "ts": "2026-10-19T10:05:00+00:00",
            "meta": {"injectKind": "recovery"},
        }
    )
    (report,) = _restart(world)
    assert world.continued == [world.slot.key], "never resumed twice"
    assert report.held == [world.slot.key]
    assert _texts(world) == [resume.AUTO_TEXT, resume.HELD_TEXT]
    button = world.slack.posts[-1][1][-1]["elements"][0]
    assert button["action_id"].startswith(resume.RESUME_ACTION_PREFIX)
    assert button["text"]["text"] == "Resume"


def test_no_duplicate_notices_after_repeated_restarts(world):
    _interrupt(world.slot)
    for _ in range(4):
        _restart(world)
    assert _texts(world) == [resume.AUTO_TEXT, resume.HELD_TEXT]
    assert world.continued == [world.slot.key]


def test_a_refused_continue_falls_back_to_the_button(world, monkeypatch):
    monkeypatch.setattr(resume, "_continue", AsyncMock(return_value="slot_running"))
    _interrupt(world.slot)
    (report,) = _restart(world)
    assert report.held == [world.slot.key]
    assert _texts(world) == [resume.HELD_TEXT]


def test_a_finished_turn_is_left_alone(world):
    world.slot.messages.append({"role": "user", "content": "q", "ts": "2026-10-19T09:00:00+00:00"})
    world.slot.messages.append(
        {"role": "assistant", "content": "a", "ts": "2026-10-19T09:00:01+00:00"}
    )
    assert _restart(world) == [resume.ResumeReport(channel_id=CHANNEL)]
    assert world.continued == [] and world.slack.posts == []


def test_only_its_own_workers_resume(world):
    state = world.state
    own = state.get_or_create_slot("chat-own-worker")
    own._created_by = world.slot.key
    world.store.record_worker(own.key, world.slot.key)
    stranger = state.get_or_create_slot("chat-somebody-else")
    stranger._created_by = "chat-another-conductor"
    world.store.record_worker(stranger.key, "chat-another-conductor")
    person = state.get_or_create_slot("chat-the-persons-own-tab")
    for slot in (own, stranger, person):
        _interrupt(slot)
    world.store.record_worker("chat-gone-worker", world.slot.key)
    reports: list[resume.ResumeReport] = []
    resume.subscribe(reports.append)
    _restart(world)
    assert world.continued == [own.key]
    (report,) = reports
    assert report.resumed == [own.key] and report.lost == ["chat-gone-worker"]


def test_a_held_back_worker_shows_as_lost_on_the_board(world):
    own = world.state.get_or_create_slot("chat-own-worker")
    own._created_by = world.slot.key
    world.store.record_worker(own.key, world.slot.key)
    _interrupt(own)
    _restart(world)
    reports: list[resume.ResumeReport] = []
    resume.subscribe(reports.append)
    _restart(world)  # interrupted again: held back
    assert reports[-1].held == [own.key] and own.key in reports[-1].lost


def test_archived_or_wrapped_up_channels_are_skipped(world):
    _interrupt(world.slot)
    binding = world.store.get_binding(CHANNEL)
    world.store.update_binding(CHANNEL, archived_at=1.0)
    assert _restart(world) == []
    world.store.update_binding(CHANNEL, archived_at=0.0, wrapped_up_at=1.0)
    assert _restart(world) == []
    assert world.continued == [] and world.slack.posts == []
    assert binding is not None


def test_a_trust_channel_says_the_work_runs_without_prompts(world):
    world.slot._trust = True
    _interrupt(world.slot)
    _restart(world)
    assert resume.TRUST_NOTE in _texts(world)[0]


# ── the Resume button ──────────────────────────────────────────────────────


def _held_notice(world) -> tuple[str, dict]:
    _interrupt(world.slot)
    _restart(world)
    world.slot.messages.append(
        {
            "role": "inject",
            "content": "resume",
            "ts": "2026-10-19T10:05:00+00:00",
            "meta": {"injectKind": "recovery"},
        }
    )
    _restart(world)
    ts = f"1700000000.00000{len(world.slack.posts)}"
    button = world.slack.posts[-1][1][-1]["elements"][0]
    return ts, button


def _click(user: str, ts: str, button: dict) -> dict[str, Any]:
    return {
        "team": {"id": TEAM},
        "user": {"id": user},
        "channel": {"id": CHANNEL},
        "message": {"ts": ts},
        "actions": [{"action_id": button["action_id"], "value": button["value"]}],
    }


def test_the_resume_button_is_owner_only(world):
    ts, button = _held_notice(world)
    assert asyncio.run(resume.handle_resume_click(None, _click(OTHER, ts, button))) is True
    assert world.slack.ephemerals[-1] == (CHANNEL, OTHER, resume.NOT_OWNER_TEXT)
    assert world.continued == [world.slot.key], "nothing more was continued"


def test_the_owners_resume_continues_once(world):
    ts, button = _held_notice(world)
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    assert world.continued == [world.slot.key, world.slot.key]
    assert world.slack.updates and world.slack.updates[-1][1] == ts
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    assert world.continued == [world.slot.key, world.slot.key], "single use"
    assert world.slack.ephemerals[-1] == (CHANNEL, OWNER, resume.STALE_TEXT)
    assert any(a[1] == resume.OPERATION and "via=button" in a[3] for a in world.audits)


def test_a_forged_resume_button_is_refused(world):
    ts, button = _held_notice(world)
    other = dict(button, value=json.dumps({"channel": "C0SOMEWHERE"}))
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, other)))
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, "1700000099.000001", button)))
    assert world.continued == [world.slot.key]
    assert world.slack.ephemerals[-2:] == [
        (CHANNEL, OWNER, resume.NOT_ALLOWED_TEXT),
        (CHANNEL, OWNER, resume.STALE_TEXT),
    ]


def test_the_router_dispatches_the_resume_prefix(world, monkeypatch):
    from kiro_crew.slack import interactions

    monkeypatch.setattr(interactions, "is_allowed_user", lambda user: True)
    monkeypatch.setattr(interactions, "channel_inbound_permitted", AsyncMock(return_value=True))
    ts, button = _held_notice(world)
    asyncio.run(interactions.dispatch({"type": "block_actions", **_click(OWNER, ts, button)}))
    assert world.continued == [world.slot.key, world.slot.key]


def test_the_real_continue_uses_the_resume_path(world, monkeypatch):
    """``_continue`` is the Resume button's own path, refusing a finished turn."""
    import kiro_crew.dashboard.chat_handlers as chat_handlers

    calls: list[dict[str, Any]] = []

    async def _fake(state_: Any, slot: Any, **kw: Any) -> str:
        calls.append(kw)
        return "resumed"

    monkeypatch.setattr(resume, "_continue", _REAL_CONTINUE)
    monkeypatch.setattr(chat_handlers, "continue_slot_turn", _fake)
    assert asyncio.run(resume._continue(world.state, world.slot)) == ""
    assert calls[-1]["require_interrupted"] is True
    assert calls[-1]["directive_user_origin"] is False
    assert calls[-1]["dispatch_guard"] is resume._session_map_untaken


def test_resume_waits_for_slack_then_runs(world, monkeypatch):
    ran: list[Any] = []
    monkeypatch.setattr(
        resume, "resume_after_restart", AsyncMock(side_effect=lambda s: ran.append(s))
    )
    flips = iter([False, True])
    asyncio.run(resume.resume_when_ready(world.state, lambda: next(flips, True), sleep=AsyncMock()))
    assert ran == [world.state]


def test_binding_is_unchanged_by_a_resume(world):
    before = world.store.get_binding(CHANNEL)
    _interrupt(world.slot)
    _restart(world)
    assert world.store.get_binding(CHANNEL) == before
    assert before is not None and before.session_key == world.slot.key
    assert before.anchor_ts == ANCHOR and isinstance(before, Binding)


def test_a_malformed_resume_table_reads_as_unavailable(tmp_path):
    """The loop guard lives in the write-protected store; a damaged table must not
    read as "never resumed" (which would let a crashing turn loop)."""
    from kiro_crew.slack.code_channels.store import CodeChannelStore

    path = tmp_path / "slack-code-channels.json"
    path.write_text(json.dumps({"resume": {"chat-x": {"turn": 3}}}), encoding="utf-8")
    loaded = CodeChannelStore(path=path)
    loaded.load()
    assert loaded.unreadable is True
    path.write_text(json.dumps({"resume": {"chat-x": {"turn": "t"}}}), encoding="utf-8")
    good = CodeChannelStore(path=path)
    good.load()
    assert good.resume_record("chat-x") == {"turn": "t"}


# ── round-15 review: identity, admission, stale buttons, races ─────────────


def test_an_unknown_turn_identity_is_held_not_resumed(world):
    """The opener pushed out of the window: missing identity is never a new turn."""
    world.slot.messages.append(
        {
            "role": "inject",
            "content": "resume",
            "ts": "2026-10-19T10:05:00+00:00",
            "meta": {"injectKind": "recovery"},
        }
    )
    world.slot.messages.append(
        {"role": "error", "content": "boom", "ts": "2026-10-19T10:06:00+00:00"}
    )
    (report,) = _restart(world)
    assert world.continued == [] and report.held == [world.slot.key]
    assert _texts(world) == [resume.HELD_TEXT]


def test_a_tab_handed_the_anchor_is_not_resumed(world):
    other = world.state.get_or_create_slot("chat-the-persons-own-tab")
    _interrupt(other)
    world.state.link_slack(other.key, ANCHOR, CHANNEL)  # the anchor now maps to B
    assert _restart(world) == []
    assert world.continued == [] and world.slack.posts == []


def test_a_worker_relinked_elsewhere_is_not_resumed(world):
    worker = world.state.get_or_create_slot("chat-own-worker")
    worker._created_by = world.slot.key
    world.store.record_worker(worker.key, world.slot.key)
    _interrupt(worker)
    worker.linked_session_key = "slack:C0ELSEWHERE:1.0"
    _restart(world)
    assert world.continued == []


def test_a_worker_that_edited_its_creator_away_is_not_resumed(world):
    worker = world.state.get_or_create_slot("chat-own-worker")
    world.store.record_worker(worker.key, world.slot.key)
    worker._created_by = ""
    _interrupt(worker)
    _restart(world)
    assert world.continued == []


@pytest.mark.parametrize("gate", ["workspace", "channel", "governance"])
def test_a_channel_the_gates_refuse_is_neither_resumed_nor_posted(world, monkeypatch, gate):
    if gate == "workspace":
        monkeypatch.setattr(cc_surface, "workspace_admitted", lambda team: False)
    elif gate == "channel":
        monkeypatch.setattr(routing, "channel_and_origin_permit", lambda cfg, c, o: False)
    else:
        monkeypatch.setattr(cc_surface, "outbound_permitted", AsyncMock(return_value=False))
    _interrupt(world.slot)
    assert _restart(world) == []
    assert world.continued == [] and world.slack.posts == []


def test_an_old_button_does_not_cover_a_newer_turn(world):
    ts, button = _held_notice(world)
    _interrupt(world.slot, ts="2026-10-19T11:00:00+00:00")  # a new request, also cut off
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    # The owner's press means "resume what is interrupted here now", as the
    # dashboard's Resume does; the turn it resumed is then recorded, so a later
    # restart holds it for the button instead of resuming it automatically.
    assert world.continued == [world.slot.key, world.slot.key]
    assert world.store.resume_record(world.slot.key)["turn"] == "2026-10-19T11:00:00+00:00"


def test_two_deliveries_of_one_press_continue_once(world):
    ts, button = _held_notice(world)

    async def both() -> None:
        await asyncio.gather(
            resume.handle_resume_click(None, _click(OWNER, ts, button)),
            resume.handle_resume_click(None, _click(OWNER, ts, button)),
        )

    asyncio.run(both())
    assert world.continued == [world.slot.key, world.slot.key]


def test_a_lost_post_tells_the_owner_in_the_dashboard_with_links(world, monkeypatch):
    from kiro_crew.dashboard.session_control_parts import code_channel_exemption as cce

    notes: list[tuple] = []
    world.state.notify = lambda *a, **k: notes.append(a)
    monkeypatch.setattr(
        cce,
        "_live_config",
        lambda: SimpleNamespace(
            slack=SimpleNamespace(use_tunnel_url=False),
            dashboard=SimpleNamespace(url="http://127.0.0.1:7777"),
        ),
    )
    monkeypatch.setattr(world.slack, "post_blocks", AsyncMock(side_effect=TimeoutError()))
    _interrupt(world.slot)
    _restart(world)
    world.slot.messages.append(
        {
            "role": "inject",
            "content": "resume",
            "ts": "2026-10-19T10:05:00+00:00",
            "meta": {"injectKind": "recovery"},
        }
    )
    _restart(world)  # held: its notice could not be posted
    assert notes, "the owner is told in the dashboard"
    body = notes[-1][2]
    assert "http://127.0.0.1:7777/chat?sid=" in body and "token" not in body


def test_nothing_happens_while_code_channels_are_off(world, monkeypatch):
    monkeypatch.setattr(resume, "code_channels_on", lambda cfg: False)
    _interrupt(world.slot)
    assert _restart(world) == []
    assert world.continued == [] and world.slack.posts == []
    payload = {"channel": {"id": CHANNEL}, "user": {"id": OWNER}, "actions": [{}]}
    assert asyncio.run(resume.handle_resume_click(None, payload)) is False


# ── round-16 verifier ──────────────────────────────────────────────────────


def _real_continue(world, monkeypatch, meanwhile):
    """Undo the fixture's fake continue; run the real one, with *meanwhile*
    applied during its awaited sub-agent probe. Returns the turn-start mock."""
    import kiro_crew.dashboard.chat_handlers as ch

    feature = {"on": True}
    governance = {"ok": True}
    monkeypatch.setattr(resume, "_continue", _REAL_CONTINUE)
    monkeypatch.setattr(resume, "code_channels_on", lambda cfg: feature["on"])

    async def _governance() -> bool:
        return governance["ok"]

    monkeypatch.setattr(cc_surface, "outbound_permitted", _governance)
    started = AsyncMock(return_value=True)
    monkeypatch.setattr(ch, "_start_next_queued_turn", started)
    monkeypatch.setattr(ch, "sel", lambda: MagicMock())

    async def _probe(*_a, **_kw):
        meanwhile(world, feature, governance)
        return None

    monkeypatch.setattr(ch, "_subagents_attached_response", _probe)
    return started


@pytest.mark.parametrize(
    "meanwhile",
    [
        pytest.param(
            lambda w, f, g: w.store.update_binding(CHANNEL, wrapped_up_at=1.0),
            id="wrapped-up",
        ),
        pytest.param(lambda w, f, g: f.update(on=False), id="code-channels-off"),
        pytest.param(lambda w, f, g: g.update(ok=False), id="governance-denies"),
    ],
)
def test_a_revocation_during_the_continue_stops_it(world, monkeypatch, meanwhile):
    """Through the real continue: what changes while it awaits its child probe
    is re-checked before the queue insert."""
    started = _real_continue(world, monkeypatch, meanwhile)
    _interrupt(world.slot)
    (report,) = _restart(world)
    assert report.held == [world.slot.key] and report.resumed == []
    assert world.slot.queue_depth == 0
    started.assert_not_awaited()


def test_the_real_continue_resumes_when_nothing_changes(world, monkeypatch):
    _real_continue(world, monkeypatch, lambda w, f, g: None)
    _interrupt(world.slot)
    (report,) = _restart(world)
    assert report.resumed == [world.slot.key]


def test_a_mirrored_worker_is_not_resumed(world, monkeypatch):
    from kiro_crew.dashboard import session_control

    worker = world.state.get_or_create_slot("chat-mirrored-worker")
    worker._created_by = world.slot.key
    world.store.record_worker(worker.key, world.slot.key)
    _interrupt(worker)
    monkeypatch.setattr(
        session_control, "_has_channel_mirror", lambda state, slot, **kw: slot is worker
    )
    _restart(world)
    assert worker.key not in world.continued


def test_a_partial_press_keeps_the_button_for_the_rest(world, monkeypatch):
    worker = world.state.get_or_create_slot("chat-held-worker")
    worker._created_by = world.slot.key
    world.store.record_worker(worker.key, world.slot.key)
    ts, button = _held_notice(world)
    _interrupt(worker)

    async def _one_refuses(state, slot, still_ok=None):
        return "slot_running" if slot is worker else ""

    monkeypatch.setattr(resume, "_continue", _one_refuses)
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    assert "the rest could not be resumed" in world.slack.ephemerals[-1][2]
    assert not world.slack.updates
    assert world.store.resume_record(f"notice:{CHANNEL}")["state"] == "button"


def test_a_same_key_slot_not_linked_to_the_channel_is_not_resumed(world):
    """Key equality alone is not identity: the slot must also be the channel's."""
    _interrupt(world.slot)
    world.slot._slack_channel = "C0ELSEWHERE"
    assert _restart(world) == []
    assert world.continued == []


def test_a_crew_log_only_worker_is_resumed(world, monkeypatch):
    from kiro_crew.dashboard.session_control_parts import session_status

    worker = world.state.get_or_create_slot("chat-tree-worker")
    worker._created_by = world.slot.key
    _interrupt(worker)
    monkeypatch.setattr(
        session_status,
        "_created_tree_roster",
        lambda key: (
            (["chat-tree-worker"], "readable") if key == world.slot.key else ([], "readable")
        ),
    )
    _restart(world)
    assert world.continued == [worker.key]


def test_a_press_that_resumes_nothing_gives_the_button_back(world, monkeypatch):
    ts, button = _held_notice(world)
    monkeypatch.setattr(resume, "_continue", AsyncMock(return_value="slot_running"))
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    assert "slot_running" in world.slack.ephemerals[-1][2]
    assert not world.slack.updates, "not reported as resumed"
    assert world.store.resume_record(f"notice:{CHANNEL}")["state"] == "button"


def test_a_contended_session_map_refuses_the_worker_without_waiting(world):
    """The final mirror re-check runs on the event loop under the slot lock: a
    session-map lock held by an off-loop writer makes it refuse, never wait."""
    import threading

    from kiro_crew.session_map import _MAP_LOCK

    worker = world.state.get_or_create_slot("chat-busy-map-worker")
    worker._created_by = world.slot.key
    world.store.record_worker(worker.key, world.slot.key)
    held, release = threading.Event(), threading.Event()

    def _writer() -> None:
        with _MAP_LOCK:
            held.set()
            release.wait(10)

    thread = threading.Thread(target=_writer)
    thread.start()
    try:
        assert held.wait(5)
        assert resume._worker_mirror(world.state, worker) == resume.UNVERIFIED
        check = resume._recheck_for(world.state, CHANNEL, world.slot, worker, resume.UNKNOWN_TURN)
        assert check() == "worker mirror unverified"
    finally:
        release.set()
        thread.join(5)
    assert resume._worker_mirror(world.state, worker) == ""


@pytest.mark.parametrize("who", ["conductor", "worker"])
def test_a_busy_session_map_refuses_the_real_continue_at_once(world, monkeypatch, who):
    """Through the real continue: the containment snapshot and insert run under a
    TRIED session-map lock, so a writer holding it costs a refusal, not a stall."""
    import threading
    import time as _time

    from kiro_crew.session_map import _MAP_LOCK

    target = world.slot
    if who == "worker":
        target = world.state.get_or_create_slot("chat-busy-real-worker")
        target._created_by = world.slot.key
        world.store.record_worker(target.key, world.slot.key)
    held, release = threading.Event(), threading.Event()

    def _writer() -> None:
        with _MAP_LOCK:
            held.set()
            release.wait(10)

    thread = threading.Thread(target=_writer)

    def _meanwhile(w, f, g) -> None:
        if not thread.is_alive():
            thread.start()
            assert held.wait(5)

    started = _real_continue(world, monkeypatch, _meanwhile)
    _interrupt(target)
    try:
        t0 = _time.monotonic()
        (report,) = _restart(world)
        elapsed = _time.monotonic() - t0
    finally:
        release.set()
        thread.join(5)
    assert elapsed < 2.0, "the loop waited on the session map"
    assert target.key in report.held and target.key not in report.resumed
    assert target.queue_depth == 0
    started.assert_not_awaited()
