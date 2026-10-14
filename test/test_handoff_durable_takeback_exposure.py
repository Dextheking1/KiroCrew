"""The reproduction for `docs/architecture/design-notes/handoff-durable-takeback.md`.

A take-back that cannot record itself leaves the log naming the recipient while only
this process's memory says the handoff was refused. This file drives that end state
across a restart and asserts what actually happens, so the exposure is a run rather
than a paragraph and whoever picks the work up does not re-derive it.

These tests PASS. They assert the behaviour as it stands, including the part that is
wrong, because a parked exposure whose test fails is a broken suite rather than a
record. The assertion messages say which lines become wrong once it is fixed.
"""

from __future__ import annotations

import asyncio

import pytest
from chat_test_helpers import _make_state
from test_session_manager_rehydrate import (  # noqa: F401 -- the autouse isolation applies
    _TAB,
    _hand_over,
    _isolated_home,
    _manager,
    _rebuild,
    _slot,
    _unit,
)

from kiro_crew.dashboard import session_control as sc


def _the_end_state_of_the_chain() -> None:
    """The log names the recipient, and no correction follows it.

    What an undecided append that later landed leaves behind: the grant's own entry is
    on disk, the correction the verb tried to write is not, and nothing else records
    that the handoff was refused.
    """
    _hand_over(
        _unit("sid-worker", "chat-worker"),
        "chat-worker",
        "chat-conductor",
        tab=_TAB,
        manager_tab=_TAB,
    )


def _team_for(state):
    """A worker, its creator, and the recipient the log names -- all admissible."""
    worker = _slot(state, "chat-worker", "chat-lead")
    conductor = _manager(state, "chat-conductor")
    conductor._tab_id = _TAB
    conductor._created_by = "chat-lead"
    lead = _manager(state, "chat-lead")
    lead._tab_id = _TAB
    return worker, conductor, lead


def test_this_process_refuses_the_slot_after_a_correction_it_could_not_write():
    """The half that works. Default-deny, in this process, with no durable trace."""
    _the_end_state_of_the_chain()
    state = _make_state_for()
    worker, _conductor, _lead = _team_for(state)

    # What _take_back does when _revoke_manager_record answers False.
    sc.publish_manager(worker, manager="", manager_tab="", unresolved=True)

    assert worker._manager_unresolved is True
    with pytest.raises(sc.SessionControlError) as refused:
        sc.require_manager_of(state, worker)
    assert (
        refused.value.code == "manager_unresolved"
    ), "the slot has to be default-deny while this process cannot say who manages it"


def test_the_refusal_does_not_survive_a_restart_and_the_recipient_is_granted():
    """THE EXPOSURE. Every assertion here is a line that must change when it is fixed.

    The restart carries nothing but the store on disk. The log names the recipient, the
    index says the log is worth reading, and the only statement that the handoff was
    refused was in memory -- so the rebuild publishes the recipient and the reach the
    refusal withheld is granted.
    """
    _the_end_state_of_the_chain()

    # The process that refused, and could not record it.
    before = _make_state_for()
    worker, _conductor, _lead = _team_for(before)
    sc.publish_manager(worker, manager="", manager_tab="", unresolved=True)
    assert worker._manager_unresolved is True

    # THE RESTART.
    after = _make_state_for(second=True)
    fresh, conductor, _lead2 = _team_for(after)
    moved = asyncio.run(_rebuild(after))

    assert moved == 1, "the rebuild published what the log says"
    assert fresh._manager == conductor.key, (
        "WRONG, and recorded on purpose: the recipient the handoff refused is the "
        "manager again. A fix makes this the creator or leaves the slot unresolved."
    )
    assert fresh._manager_unresolved is False, (
        "WRONG, and recorded on purpose: the unresolved state did not survive, because "
        "nothing durable carried it"
    )
    assert sc.require_manager_of(after, fresh) == conductor.key
    # And the reach itself, which is what the exposure is about.
    assert (
        sc.authorize_target(
            after,
            caller_session_key=conductor.key,
            target=fresh.key,
            operation="send",
        )
        is not None
    ), (
        "WRONG, and recorded on purpose: the refused recipient can drive the session "
        "after a restart. This is the assertion a durable take-back has to invert."
    )


def test_the_marker_is_not_the_carrier_because_its_lifetime_is_the_commit():
    """Why the in-flight mark cannot be reused for this.

    Its contract is "a manager change is in flight RIGHT NOW" and it is cleared in a
    ``finally``, so it is down the moment the commit returns -- which is before the
    restart that matters, and before the operator could repair anything.
    """
    assert sc.manager_commit_pending("chat-worker") is False
    with sc.manager_commit_in_flight("chat-worker"):
        assert sc.manager_commit_pending("chat-worker") is True
    assert sc.manager_commit_pending("chat-worker") is False, (
        "the mark is scoped to the commit, so it cannot carry a debt that outlives the " "process"
    )
    # And the flag the refusal actually sets is not persisted either: the metadata line
    # the slot is rebuilt from has no row for it, which is the same observation.
    import inspect

    from kiro_crew.dashboard.slot_persistence import metadata_line

    assert "manager_unresolved" not in inspect.getsource(metadata_line), (
        "the unresolved flag has no place on the metadata line, so a restart cannot "
        "learn from the line that a take-back was owed"
    )


#: Distinct data homes for "the process that refused" and "the process after the
#: restart". Both read the same store on disk, which is the whole point.
def _make_state_for(*, second: bool = False):
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix="restarted-" if second else "live-"))
    return _make_state(root)
