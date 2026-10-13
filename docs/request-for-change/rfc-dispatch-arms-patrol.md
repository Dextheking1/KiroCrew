---
title: Dispatch arms a patrol -- session_create arms the default patrol, and a dispatch replaces any loop the system stopped
status: accepted
author: iamwhatever, with kirocrew-worker
created: 2026-10-13
last-audited: 2026-10-13
audited-at: 18d54c98c8
doc-pr:
implementation-prs: [19742]
tracking-issues: [19741]
supersedes: []
superseded-by: []
---

# RFC: Dispatch arms a patrol

- Status: accepted. This is the product owner's decision, given 2026-10-13 at
  17:34 UTC in the conductor session that drives #19742 ("#19742 写 RFC，默认开巡查":
  write the RFC; a dispatch arms the patrol by default). The bind note in
  decision 2 is not part of that ruling: it was added at the Design review's
  request on this document's pull request. It amends
  [rfc-conductor-default-patrol.md](rfc-conductor-default-patrol.md) in this
  document, so that implemented document is not edited. Claims about today's
  code were checked at `18d54c98c8` (main).
- Author: iamwhatever
- Implementation: [#19742](https://github.com/kirodotdev/KiroCrew/pull/19742);
  tracks [#19741](https://github.com/kirodotdev/KiroCrew/issues/19741).
- Related: [monitor-architecture.md](../system-specs/modules/monitor-architecture.md)
  (the default patrol as built) and the goal-conductor skill's Patrol section.

## 1. Today

At `18d54c98c8`:

- **A bind re-arms only the gateway's own loop.** `conductor_patrol.ensure_patrol`,
  called from the `bind` branch of `api_work_ledger_record` in
  `src/kiro_crew/dashboard/handlers/work_ledger.py`, arms the default patrol when
  the conductor's slot holds no loop record. A stopped record is replaced only
  when it is the gateway's own `default_patrol` and the system stopped it. A
  conductor's own `monitor_start` loop (`watch="work-ledger"`) stops with
  `monitor_terminal` when every item closes. The next round's binds then answer
  `existing`, and nobody reads the new workers' reports.
- **The bind reply hides the outcome.** The route returns `patrol` and, when no
  `work-ledger` watch is active afterwards, `patrol_note`. The
  `work_ledger_record` tool prints only `item`, `state` and `verdict`
  (`_call_tool_inner` in `src/kiro_crew/mcp_work.py`), so neither reaches the
  agent, though the tool description promises both.
- **`session_create` arms nothing.** `api_session_control_create` in
  `src/kiro_crew/dashboard/handlers/session_control.py` opens the session and
  returns. Its tool reply (`_run_session_create` in `src/kiro_crew/mcp_dashboard.py`)
  ends "watch it with session_read_message". A session that dispatches with
  `session_create` and never binds, such as one that holds no work ledger, gets
  no patrol and no word about one.

## 2. Decision

The rule: **dispatch means monitor.** After a session dispatches work, it holds
an active loop that reads that work, or its reply says why not.

1. **`session_create` arms the default patrol.** A create that commits (not a
   dry run) runs the same `ensure_patrol` on its caller that a bind runs: the
   same bounds, gated `work-ledger` watch, fixed message, `default_patrol` tag
   and authorizer (`autonudge_authz.authorize_and_add_nudge`, create-only, an
   outside arm).
2. **The create reply says what the patrol reads.** The default patrol reads
   the caller's work ledger, and the session a create opens joins the ledger
   only when it is bound. So a create reply from a caller that holds a ledger
   always carries a bind note, whatever the patrol outcome: bind the session
   with `work_ledger_record` before you seed it, or read it yourself with
   `session_read_message`. A caller that holds no work ledger gets nothing
   armed (`unsupported (no-ledger)`), because the watch would have nothing to
   read. Unless an active loop already runs on its slot, its reply carries a
   note: arm a `monitor_start` loop that reads the session it opened with
   `session_read_message`, or watches the one PR it waits on.
3. **Crew and member callers stay refused.** On both routes, an arm on a crew-
   or member-mode slot is refused as an outside arm (`refused
   (outside-arm-refused)`), as a bind's arm is refused at `18d54c98c8`. Neither
   route can tell the member's own turn from a cron, app or sub-agent turn on the
   same session key ([rfc-conductor-default-patrol.md](rfc-conductor-default-patrol.md) §5).
4. **A loop the system stopped counts as no loop.** On a bind, or a create from
   a caller that holds a ledger, an inactive record the system stopped is
   replaced by a fresh default, whoever armed it, the conductor's own loop
   included. "The system stopped" is the `_stopped_row_is_replaceable` allowlist
   a directive re-arm uses: a spent bound, a finished subject, a lapsed approval,
   a vanished target and the other system stops it names. For a monitor record
   those are the outcomes in `REARMABLE_MONITOR_OUTCOMES` in
   `src/kiro_crew/monitoring/models.py`: `success`, `blocked`, `budget` and
   `target_unavailable`, bar a record quarantined as malformed. Replacing a
   `blocked` record is intended: it is a system stop in that same allowlist, and
   the new dispatch is work nobody reads without a patrol.
5. **A loop a person stopped stays stopped.** An inactive record whose stop a
   person or an app recorded (`user_stop`, session-close retention, a manual
   pause, a research tombstone, or a reason or outcome this version does not
   know) is kept, and the arm answers `refused (user-stopped)`. An active record
   is never displaced or stacked; it answers `existing`.
6. **Every dispatch reply says what happened.** The bind and create routes
   return `patrol` and `patrol_reason`. They add `patrol_note` when nothing
   patrols the caller's work afterwards, and a create from a ledger holder
   always adds the bind note (decision 2). The `work_ledger_record` bind reply
   and the `session_create` reply end with `patrol=<outcome> (<reason>)` and
   the note. A reply that lacks `patrol` reads `patrol=unknown` with the
   arm-yourself note, so a dispatch never looks patrolled when nothing says it
   is.
7. **The guidance says it once.** The goal-conductor skill's Patrol section and
   the member operating-mode block (`operating_mode_block` in
   `src/kiro_crew/context_assembly/member.py`) state the rule: dispatch means
   monitor, `watch="work-ledger"` by default.

| `patrol` | `patrol_reason` | When |
|---|---|---|
| `armed` | `no-loop` | the slot held no loop |
| `armed` | `replaced-system-stopped` | an inactive record the system stopped was replaced |
| `existing` | `active-work-ledger` | an active loop already watches the ledger |
| `existing` | `active-other-watch` | an active loop watches something else; `unpatrolled` on the ledger read stays the signal |
| `refused` | `user-stopped` | a person's stop is kept |
| `refused` | `outside-arm-refused` | an arm on a crew- or member-mode slot |
| `refused` | `session-cannot-host-loop` | the authorizer refused the slot with 403: an incognito or temporary session, or a channel user outside its allowlist |
| `refused` | `arm-failed` | any other refusal or error; the dispatch still commits |
| `unsupported` | `autonudge-disabled` | auto-nudge is off |
| `unsupported` | `no-loop-slot` | the caller's session key maps to no loop slot |
| `unsupported` | `no-ledger` | a create from a caller that holds no work ledger |

## 3. What this amends

[rfc-conductor-default-patrol.md](rfc-conductor-default-patrol.md) reads with
these changes. That document is not edited.

- §2 Goals, "A conductor that binds a worker and holds no loop gets a patrol":
  a session that dispatches work (a bind, or a create from a caller that holds a
  work ledger) and holds no ACTIVE loop gets a patrol.
- §2 Non-goals, "Changing the goal-conductor skill or the conductor prompt's
  Patrol section": one paragraph of the skill and one clause of the member
  operating-mode block state decision 7.
- §3 Arm on bind, "A stopped row is left alone, with one exception": every
  inactive row the system stopped is replaced, whoever armed it (decision 4),
  and a row a person stopped is kept and refused (decision 5). The reply carries
  `patrol_reason` (decision 6).
- §3 Backstop flag: the bind-time arm keys on "no active loop", not on "no loop
  at all". An active loop on another watch is left in place, and the flag stays
  the signal.
- §5 Security applies to the create route unchanged.
- §8 Rollout, "A second bind, or a stopped retained loop, arms nothing" and "A
  system-stopped default patrol is re-armed on the next bind": a loop a person
  stopped arms nothing, and any loop the system stopped is replaced.

## 4. Non-goals

- Arming a loop on a crew- or member-mode slot from either route. That needs the
  turn's provenance, which only `session_directive_apply` has.
- Changing the patrol's bounds, message, gate or cadence.
- A dry-run `session_create`, which commits nothing.
- Reviving a loop a person stopped.

## 5. Backward compatibility

Additive on the wire. The bind reply gains `patrol_reason`; the create reply
gains `patrol`, `patrol_reason` and `patrol_note`. Nothing is removed or
renamed, and every request accepted at `18d54c98c8` is still accepted. The
behaviour changes are the decided ones: a create from a ledger holder with no
active loop arms the default; a dispatch replaces a system-stopped loop that is
not the gateway's default; a person-stopped loop answers `refused
(user-stopped)` where it answered `existing`.

## 6. Risks

- **A patrol the caller did not want.** It is a gated loop that fires only when
  the ledger moves. The caller's own `monitor_start` or `monitor_watch` replaces
  it without a 409 (the `default_patrol` tag,
  [rfc-conductor-default-patrol.md](rfc-conductor-default-patrol.md) §3), and
  `autonudge_stop` ends it.
- **A replaced record.** A dispatch replaces a finished loop's retained row,
  `blocked` included. That row is a system stop, and without the replacement
  the new work would run unread (decision 4).

## 7. Security

The create route arms through the same chokepoint with the same rules:
create-only, an outside arm with no `initiator_slot_key`, fixed gateway text,
and every arm and refusal in the authorizer's SEL audit. The caller is the
verified session key every session-control route reads. The ledger check
answers yes or no and returns nothing from the record. Replacement is limited to
the directive re-arm's allowlist, so this path cannot revive a stop a directive
could not.

## 8. Alternatives considered

- **Bind only, and leave `session_create` alone.** Rejected by the ruling: a
  create is a dispatch too, and the failure this document exists for is the
  same unread dispatch.
- **Report, never replace (render the reply, keep the default-only re-arm).**
  Rejected: a conductor whose own loop finished one round is the common case,
  and a note it skips leaves the next round unread.
- **Keep a `blocked` record of a non-default loop and refuse with its own
  reason.** Rejected (decision 4): every later dispatch would stay unpatrolled
  until a person cleared that row.
- **Rely on a bind always following a create, with no bind note.** Rejected:
  nothing enforces the bind. The `work_ledger_record` description asks for it
  before the session is seeded, and the bind note (decision 2) says it in the
  reply the caller reads.
- **Self-arm a crew or member caller from the create route.** Rejected for the
  reason [rfc-conductor-default-patrol.md](rfc-conductor-default-patrol.md) §5
  gives for bind.

## 9. Open questions

None open.

## 10. Rollout

One implementation PR, [#19742](https://github.com/kirodotdev/KiroCrew/pull/19742),
after this document lands. Exit criteria, each pinned by a test there:

- A bind, or a non-dry-run create from a caller that holds a ledger, on a slot
  with no loop arms exactly one default patrol: `armed (no-loop)`.
- The caller's own loop that the system stopped, a finished `work-ledger` watch
  or a finished PR watch, is replaced: `armed (replaced-system-stopped)`.
- A person-stopped loop is kept (`refused (user-stopped)`), and an active loop
  is left alone (`existing`).
- An arm on a crew- or member-mode slot is refused on both routes, and no loop
  is written.
- A create from a caller that holds a ledger carries the bind note, whether
  the patrol was armed, already active or refused.
- A create from a caller with no ledger arms nothing and carries the note; a
  dry run arms nothing.
- A caller whose session key maps to no loop slot answers `unsupported
  (no-loop-slot)`, and a 403 from the authorizer answers `refused
  (session-cannot-host-loop)`.
- Both tool replies end with `patrol=<outcome> (<reason>)`, and a reply missing
  the field reads `patrol=unknown` with the note.
