# Queued sends after a session handoff

The handoff verb refuses while its target still has queued work
(`queue_not_drained`) rather than re-checking queued entries when they drain. That
re-check is one piece of work and it ships as one change, from this branch: a
provenance stamp recording whether the manager fence applied to the admission, a
per-entry re-compare of the stamped sender against the current manager, a hold for
the entries whose manager read has not landed, the restore path's handling of a
stamp that names a write target, and the eviction and dispatch sites that must read
that hold on the entry they take.

## What the manager line adds (#19142)

With `agent.session_manager_chain` on, a caller reaches a session through every
manager above it, not only the direct one. That widens this surface in one place the
target check does not cover: handing over a MIDDLE hop moves the line of every
session below it, and their queues are not read. A lead that queued a message on a
busy worker through its conductor, and then sees that conductor handed to someone
else, leaves the message to run after its own reach over the worker ended. With the
switch off nothing below the target moves, because reach is one level, so #19142
ships this as a declared limit and leaves the fix here.

What the re-check has to do to close it:

- Re-assert the predicate that ADMITTED the entry, not a narrower one. With the line
  on, that is the line walk the fence uses, so a lead that reached its grandchild
  through its conductor is not dropped at the drain for not being the direct
  manager. A re-check may refuse what the admission refused; it may never refuse what
  the admission allowed.
- Record which policy admitted the entry, beside the fence verdict in the stamp, so
  the drain reads no config on the loop and an operator flipping the switch while a
  message waits does not change what admitted it. An entry stamped before that field
  existed reads as the one-level answer, which is what admitted it.
- Hold, not drop, on every refusal that clears by itself, and treat those as a SET:
  an unread manager, and a hop of the line inside its own close or mid-commit
  (`manager_line_pending`). A drain that compares against one code deletes the
  entries the other code promised to hold.
- Answer a record the retention cap dropped (`manager_line_forgotten`) one level
  rather than holding on it. Nothing re-arms that read, so a hold on it would stop
  the session's queue for the life of the process.
