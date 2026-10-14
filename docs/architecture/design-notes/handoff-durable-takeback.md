# A take-back that cannot record itself

`session_handoff` makes a handoff durable by appending one `session/manager_set`
entry and waiting for it to be on disk. That append is the commit point, and the
wait answers three values: settled, refused, and **undecided**. The undecided
answer is the subject of this note.

An undecided append is still queued. It may land at any moment, including after
the verb has refused the handoff — so the verb appends a correction, which is the
only way an append-only log can be contradicted. When that correction *also*
cannot be written, this process has decided to revoke a grant and failed to
record the decision. The log still names the recipient, and the log is the
authority.

## What is exposed

The slot is published UNRESOLVED, which is default-deny for every verb that
authorizes against the manager — *in this process*. Nothing durable says the
handoff was refused. At the next start the log is read, it names the recipient,
and the recipient is granted exactly the reach the refusal withheld.

Reaching it takes two write failures and a restart: the entry's settlement must
come back undecided, the correction's write must then fail, and the process must
restart before anything repairs the log. It is not a rarity argument, though —
`main` has no handoff verb, so this exposure does not exist there. The feature
creates it.

## Why a process-local marker cannot carry it

Driven rather than argued. Build the end state — the log names the recipient, the
handoff index names the slot, the correction never landed, and this process has
published the slot unresolved and refuses it with `manager_unresolved` — then
restart with nothing carried over but the store on disk:

    rebuild moved:                 1
    manager after the restart:     'chat-conductor'
    unresolved after the restart:  False
    who manages it:                'chat-conductor'
    the recipient is AUTHORIZED:   the refusal did not survive

`test/test_handoff_durable_takeback_exposure.py` is that run.

Four reasons the in-memory state cannot be the carrier:

1. `_manager_unresolved` is written only by `publish_manager` and appears
   nowhere in `slot_metadata_codec.py` or on the slot's metadata line. It is
   process-local by design.
2. The in-flight mark (`manager_commit_in_flight`) has the wrong LIFETIME. Its
   contract is "a manager change is in flight right now" and it is cleared in a
   `finally`; the debt lasts until the log is repaired, which outlives the
   process. A carrier shorter-lived than the fact it carries cannot carry it.
   Holding it past the commit also contradicts the test that pins the discard.
3. The handed-over index is membership-only (`names(key) -> bool`). Per-slot
   state there is a new durable shape, not a use of the existing one.
4. The log is append-only, so "this handoff did not happen" has exactly one
   expression: another entry. That is the write that failed.

## Why ordering cannot remove THIS one

The obvious shape — make the log append the single commit point, with every
earlier step idempotent and discardable — is the right one, and the handoff verb
has it: the index write goes first and the append is last. Nothing forced the
other order (the index writer takes only the slot key, needs no sequence number,
and is idempotent), and that change removed the correction debt from four of the
five refusal paths that owed one.

It cannot remove this one. The debt here does not come from a step ordered after
the commit point; it comes from a commit point whose **outcome is unknown**. No
reordering removes an unknown, and the three-valued settlement exists because
that unknown is irreducible.

Two paths owe a correction under that order, and no other order removes them:

| path | why it survives |
|---|---|
| undecided settlement | an append of unknown outcome cannot be un-appended |
| recipient replaced inside the append's own suspension | the in-flight mark refuses sends, not recipient mutations, so that re-check must run after the append |

## Why the obvious fix is two things that do not work

A review pass proposed: hold manager-fenced queued sends while ownership is
unresolved, then revalidate each entry's stamped sender against the resolved
manager before dispatch.

The first half is the process-local flag the run above disproves — it holds
only while this process holds the flag, and the flag does not survive the
restart that is the last step of the exposure.

The second half is a per-entry sender re-check, which needs a sender stamp on
every queued entry plus a hold for the entries whose manager read has not
landed. That is the queued-send delivery surface, parked separately on
`feat/19103-handoff-queued-sends`.

## What would close it

A durable statement that a take-back is owed for a slot, written where the
entries are, and read by the manager rebuild before it publishes what the log
says. That is a new durable shape, which is why it is parked rather than folded
into the verb: the choice between a second index, a per-slot state in the
existing one, and a distinct entry type is a persistence-layer decision, and the
rebuild has to read it on a path that currently reads one set and one log.

## Two in-process races accepted with the handoff

A review pass on `2b08e00e23` found two more races. Both sit inside a commit that
is in flight, and neither needs a restart. A maintainer accepted both for that
commit as known residual risk, tracked here
([override](https://github.com/kirodotdev/KiroCrew/pull/19135#issuecomment-6172123548)).
Line numbers below are at `2b08e00e23`, in
`src/kiro_crew/dashboard/session_control.py` unless named otherwise.

### A steer fallback re-admits without reading the in-flight mark

`send_to_target` reads `manager_commit_pending` once, at `:10405`, right after its
first `authorize_target`. The handoff relies on that read: it reads the target's
queue once, ahead of its commit point, and does not read it again after the
append. Both queue fallbacks of a steer break that reliance.

1. A `steer=True` send reaches a running target whose client cannot steer. The
   fence check at `:10405` passes, because no commit is in flight yet.
2. The steer is scheduled (`:10606`) and the send yields on it (`:10608`). A
   handoff enters its commit, and its index write suspends.
3. The steer comes back `STEER_UNAVAILABLE`. With no steer-capable client it
   returns before it registers anything in `_pending_steers`.
4. The send falls back to the queue. Its config re-warm runs in a thread
   (`prewarm_enabled_check`), which is a real suspension every time.
5. The handoff's index write completes first. The recipient check passes, the
   queue read finds nothing, and the append suspends.
6. The re-warm completes. The send re-runs `authorize_target` (`:10743`) and
   enqueues (`:10778`). The manager is not published until after the append, so
   the old manager still passes, and nothing reads the mark.
7. The append settles, the recipient re-check passes, and the manager is
   published. The entry admitted for the old manager drains under the new one.

The window is the gap between the handoff's queue read and the fallback's
enqueue: the read lands after the steer gave up and before the text was queued.
A steer that registers and then loses its text in the RPC clears itself and
reaches the same fallback.

The orphan arm `_queue_after_orphaned_unavailable` (`:10574`) has the same shape:
it re-gates and enqueues (`:10594`) for a frame that was cancelled.

A steer that is delivered does not open the gap in the orderings traced.
`steer_into_running_turn` registers into `_pending_steers` on the task's first
step, with no await anywhere before that append. The handoff's
queue read can only run after its index write suspends, so it sees the pending
steer and refuses. That was traced, not driven.

**Fix shape.** Read the mark after every re-admission, not only the first. The
in-frame arm raises `manager_change_in_flight`, which the caller can retry. The
orphan arm has no caller left to tell, so it logs and does not enqueue, the way
it already treats a target that resolved to a different session. The fix is
local to `send_to_target` and needs no new state.

### A correction that times out releases the mark before it lands

The mark is entered by `with manager_commit_in_flight(...)` (`:7107`) and released
when `_commit` returns or raises. A correction can outlive that.

1. The commit's append comes back undecided: still queued, outcome unknown.
2. `_take_back` appends a correction and waits through `awaiting_commit`
   (`:7431`). That wait answers False on an exhausted bound as well as on a
   drop, so a correction that is only slow reads as failed.
3. The slot is published unresolved (`:6844`) and `_commit` raises
   `handoff_unresolved`. The `with` block exits and the mark comes down.
4. A caller refused `manager_unresolved` arms a re-read
   (`_arm_manager_reresolve`).
5. The first append has landed and the correction has not. The log's newest
   statement names the recipient, the mark is down, and the re-read publishes
   the recipient.
6. The correction lands on disk. Nothing in this process reads the log again,
   so the recipient stays authorized until the next start reads the revert.

This is the exposure at the top of this note, landing late instead of never. The
restart variant needs the correction never to land; this one needs it to land
after the re-read. Both come from one cause: the mark's lifetime is the commit
window, and the debt lasts until the correction settles.

**Fix shape.** Hold the mark until the correction settles, and release it from
the correction's settlement callback instead of a `finally`; then re-resolve the
slot. That changes the mark's contract from "a commit is in flight" to "a commit
or its correction is unsettled", and it still does not survive a restart. It
closes this race and leaves the durable one, so it belongs with the durable
take-back: both need a "take-back owed" state that lives as long as the
correction does, not as long as the verb.
