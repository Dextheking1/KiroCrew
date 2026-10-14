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
