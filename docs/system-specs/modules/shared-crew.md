# Shared Crew

Owners: `kiro_crew.shared_crew` (contracts in `contracts`, `admission`, `collab`, the
record codec in `codec`) and the browser mirror `website/src/api/sharedCrew.ts`.
Behaviour modules for each area below live beside the contracts in the same package
and import them; none redefines a field.

## 1. Purpose and boundaries

Shared Crew lets several people who can already reach one Crew work in it together.
It attributes what each person does, charges execution to the person who asked for
it, keeps one person from exhausting the host, and shows collaborators to each other.

It is an attribution and fairness layer, not an access layer. The network (Tailscale)
and the existing dashboard auth in `dashboard.token_auth` decide who may connect;
Shared Crew never admits a request auth refused, and never refuses one auth admitted
because an identity lookup failed. Every authorized collaborator has the same
application rights. There are no roles, no per-session ACLs, no invitations and no
in-Crew member management.

No permission scoping in this release: every collaborator a Crew admits has the same
rights, and no code path reads a role or an ACL. The enterprise `GrantGate` decides
only whether a person may use the Crew at all, never what they may do in it.

A Crew is a machine identity, not a group of people. Its synthetic principal is the
Crew acting as itself; the people who may work in it are individual grants, and
several Crews may have overlapping member lists.

Status: proof of concept. Every section below names the module that implements it.
`shared_crew.wiring` connects them at gateway start (`dashboard.server`): it registers
the identity, presence and admission capability sources and makes the admission
scheduler the People usage reader. The routes register through the `shared_crew`
slice of `dashboard.routes`, after every existing slice. With
`dashboard.shared_crew.admission_enabled` off (the default) the gateway behaves as a
single-operator Crew, plus attribution, presence and shared decisions.

Admission coverage. `POST /api/chat`, subagent spawn, continue and retry, the task
runner (start, plan, retry, execute), workflow run, run-intent, saved-definition run
and rerun, and cron admit at the handler. Every other chat turn passes
`shared_crew.turn_gate.gate_turn`, which `chat_runner._run_chat`'s exit guard calls:
the queue drain, continue, regenerate, edit-resend, rewind, app agents, send-message,
the OpenAI shim, task-runner hand-offs, nudges and linked channel threads. Model work
that starts its own runtime without `_run_chat` runs through
`turn_gate.run_admitted` (or `run_admitted_or_raise`): thread replies and side chat
(charged to the asker), the hooks agent, the heartbeat and the meetings,
auto-improvement and code-review apps (Crew automation), a remote crew turn (the
caller, after the route's own owner gate), each dashboard channel agent turn (Unknown
for a person, automation between agents) and the issue-radar one-shot calls (the
clicker). An app's background spawn (`apps.spawn_sdk`) is a child charged to the
app's system principal. A turn already inside admitted work, or nested in one
(`_prompt_depth`), is not admitted twice. Channel transports admit in
`messaging.dispatch.ChannelTurns._run`, the Telegram dispatcher and
`slack.handler.handle_message`. With admission on, the speculative eager spawn is
skipped, so a runtime only starts inside an admitted turn. A WebSocket never starts a
turn, and resume starts no runtime: its `session/load` runs inside the next admitted
turn.

| Entry points (88 inventoried) | Count | Admission |
|---|---|---|
| Handler or turn-gate admission | 68 | fair admission, charged as below |
| Reached through a recorded hop (cron manual run, `kirocrew cron trigger`) | 2 | assumed via the cron dispatch |
| Starts no new model work (interrupt, resume, steer, timers; runtime restarts after compaction, lifecycle respawns, warm connections, provider re-attach) | 9 | none needed; the prompt they serve runs in an admitted turn, and `hard_limits` refuses the spawn |
| Internal one-shot helpers (titles, the optimizer, task refine, workflow authoring, decision and knowledge calls) | 7 | `hard_limits` backstop only: a short tool-less call, not queued work |
| The CLI's own `kirocrew chat` and `kirocrew run` | 2 | `hard_limits` backstop only: a separate process with no gateway, and a process-local service would reconcile the gateway's records |

A turn that returned after its runtime failed (a fault stop reason, or no terminal
event and an error row) settles `failed` with `runtime_error` and a bounded
`extra.failure_detail` (`turn_gate.turn_failure`); a raise settles the same way.

Charging. A turn is charged to its initiator: the request's actor at a handler, the
server-stamped `author` of a queued entry at the drain (entries restored from disk or
merged from different people name no one), the bounded automation principal for an
automation actor (`app`, `cron`, `gateway`, `crew`, `subagent`) or a synthetic turn,
and the bounded Unknown bucket otherwise. A channel sender has no Shared Crew principal,
so a person's channel message is Unknown and a monitor wake is automation. A refused
turn never runs: the dashboard shows an error row, a channel gets a one-line reply, and
a wake answers busy so it retries its claim.

## 2. Identity

### Principal

`contracts.Principal` is the identity work and records are attributed to. Its `id`
is `principal_id_for(provider, issuer, subject)`: a deterministic, opaque digest, so
one person maps to one id on every device and tab without a registry, and the id does
not reveal the login. Email and display name are never inputs; two providers asserting
the same email are two principals. `DisplaySnapshot` is a label captured when the
principal was seen. A rename changes it and leaves `id` alone. It carries no avatar
URL, because a remote URL in a shared record is an outbound fetch someone else chose.

`PrincipalKind` separates a person (`human`), a device with no personal identity
(`machine`, such as an ACL-tagged tailnet node, keyed by node because every tagged
node reports the same login), Crew automation (`system`, one bounded principal per
named component from `Principal.system`), and `unknown`.

### Derivation

`identity.publish_actor` derives the principal once per request, after
`token_auth_middleware` admitted it and before the handler, and publishes an
`ActorContext` under `request[ACTOR_REQUEST_KEY]`. Handlers read it with
`identity.actor_of` (`viewer_principal` for the principal); a request with no
published actor reads as Unknown, never as the operator. The WebSocket upgrade
passes through the same middleware, so a socket's actor is the upgrade request's.
The browser never names itself: a principal is never read from a body, query,
header or display name, and `from_dict` exists only to read records this server
wrote.

Evidence, strongest first (`identity.derive_principal`):

| Request | Principal | Provenance |
|---|---|---|
| An app identity (an app token, or an internal caller the middleware derived an app for) | `system`, component `app:<name>` | `internal` |
| The internal loopback secret (kiro-cli, MCP, doctor) | `system`, component `internal` | `internal` |
| Loopback peer forwarded by local `tailscale serve`, resolved and allowlisted by `tailnet.resolve_forwarded_peer` | `human` keyed by tailnet login; `machine` keyed by node for a tagged node | `tailscale_serve` |
| Tailnet peer connecting to the gateway's tailnet address directly, no forwarding header, resolved by daemon `whois` of the socket address (`tailnet.resolve_direct_peer`) | as above | `tailscale_direct` |
| Loopback socket with no forwarding header at all | `human`, the local operator, keyed by the token subject | `local_operator` |
| Anything else auth admitted | `Principal.unknown()` | `none` |

A tailnet principal's subject is the lowercased daemon login under provider
`tailscale` and issuer `tailnet`; a tagged node's is `node:<name>`, because every
tagged node reports the login `tagged-devices`. The display snapshot is the login
(or node name) as the daemon reported it, so a rename never changes the id.

Supported topologies: exactly the two tailnet rows, and both require the existing
identity-trust opt-in (`dashboard.tailscale.trust_identity` with a non-empty
`allowed_logins`, i.e. `TailnetTrust.enforces_identity`). Without it no daemon
lookup runs and tailnet callers are Unknown. The serve row reuses the
middleware's existing resolution, allowlist deny and pin unchanged. The direct
row is attribution only: its lookup runs after the auth decision, shares the
whois cache and executor, and is read by neither the pin (a direct peer stays
`ip:`-pinned) nor the allowlist, so it cannot admit or refuse a request.

Loopback alone is not proof of a trusted proxy: a loopback request carrying any
forwarding header (`origin.is_proxied_request`) is never the local operator, and
its forwarded address is attributed only through the daemon, and only after the kernel
proves the client end of the loopback socket belongs to `tailscaled`
(`tailnet_proxy_proof.loopback_client_is_daemon`, Linux only; elsewhere the serve row
never resolves and such a caller is Unknown). A process running as the gateway's own
user is never that proof, so a local token holder adding `X-Forwarded-For` is Unknown,
not the person it names. A
`Tailscale-User-Login` header that disagrees with the daemon drops the
attribution. A non-loopback peer that sends `X-Forwarded-For` is a proxy on
another node and is Unknown. Funnel, other reverse proxies and unverifiable
forwarding headers resolve to Unknown.

Failure never changes access. Derivation runs only on an admitted request, and
an exception anywhere in it publishes Unknown. A request auth refused publishes
nothing.

`ActorContext.auth` is a copy of what the middleware decided, kept for audit. It is
not a second decision. `correlation_id` is the client's `X-Correlation-Id` when it
is a short plain token, else the server's `request_id`.

### Observed collaborators

`people` keeps a bounded in-memory record (`MAX_OBSERVED` principals, LRU) of the
human and machine principals seen on authorized requests, with first and last
sighting and a device count (a digest per node, at most `MAX_DEVICES`). Unknown
and system principals are not recorded. `GET /api/shared-crew/people` merges that
with the creators recorded on persisted sessions. Each row names its `sources`
(`observed`, `session_creator`), so an observation is never presented as
membership; `directory.state` is `unavailable` because this build has no
authoritative assigned-user directory. Per-person `usage` is `null`, and
`usage_available` false, until the admission layer registers a reader
(`people.register_usage_source`).

## 3. Session attribution and records

`SessionAttribution` is stored under the metadata-line key
`SESSION_ATTRIBUTION_META_KEY` (`attribution`). It is not `created_by`, which already
names the creating slot key. It is write-once: `keep_creator` returns the stored value
whenever one exists, so a later message by someone else never changes the creator.

The slot holds it on `_attribution` (`attribution.slot_attribution`).
`attribution.stamp_creator` records the creating actor where a session is
created; `stamp_creator_if_fresh` records the sender of the first accepted user
message of a slot no creation path stamped, and only when the slot holds no user
turn yet. The metadata codec's `attribution` row writes it on both save forms and
reads it back on every hydration purpose. No writer lists the key as owned, so a
save that does not write it carries the stored value through
`history.carry_unowned_metadata`, and the whole line is replaced atomically by the
existing full save.

A session with no `attribution` key reads as `legacy_missing` with an Unknown creator.
It stays legacy: the codec never writes a legacy value back, no stamp replaces it,
and nothing back-fills it, least of all from the current viewer. Attribution was on
but no principal could be established reads as `unattributed`. A damaged attribution
object, or a torn metadata line, reads as legacy, because authorization never reads
it and a damaged record may only lose its label. Keys inside the attribution object
this build does not know are kept (`extra`) and written back.

The metadata line is a file an agent's own tools can write, like `created_by`.
The creator is therefore a label for discovery, never an input to an access
decision.

Browser-bound copies use `attribution.public_attribution`: availability, creation
time and `Principal.to_public_dict` (id, kind, display; no issuer or subject).
The slot payload (`slot_projection`) and every session catalog row
(`history_search.SessionCatalogProjection.list_sessions`) carry it as
`attribution`.

A message's author rides in the message `meta` under `MESSAGE_AUTHOR_META_KEY`
(`author`), in the public principal shape, stamped by the server when it accepts
the message (`attribution.stamp_message_author`). A client-supplied `author` key is
discarded at ingress and overwritten at the stamp. Every other live user row an
authorized request's handler appends (regenerate, rewind, task-runner hand-offs, the
OpenAI shim) names that request's actor (`attribution.with_request_author`, bound by
`identity.publish_actor` to the request's own task, so a turn the handler starts never
inherits it). Channel rows name the bounded Unknown principal and automation rows the
automation principal (`stamp_principal_author`). A message written before authors
reads as Unknown (`attribution.message_author`).

### Session list query

`session_query.query_sessions` runs over the whole persisted catalog: machine
transcripts are dropped, then the search (`q`, case-insensitive over title and
key) and the creator filter, then the page is cut. Order is newest `modified`
first with a key digest as the tiebreak. The cursor is a keyset position, so one
walk never returns a row twice; a session modified during a walk moves to the top
and appears on the next walk. `total_matching` counts every session passing the
search and the filter. `hidden_by_filter` counts the sessions passing the search
that the creator filter excluded. The list covers persisted sessions; a slot
that has never been saved is not in it. The route answers only the dashboard
user, and refuses an app token or an internal caller with 403, so it reaches no
session the existing list does not.

### Audit

`ActionRecord` audits a consequential shared action: session create, message,
approval, question answer, steer, cancel, retry and control actions. It names the
initiator, the responder where one exists, the runtime reference, the outcome and a
causal link. It carries ids, never prompt text or credentials.

`audit.record_action` writes it to the existing tamper-evident security event log
(`kiro_crew.sel`) as one `shared_crew_action` event: `caller_identity` is the
initiator's principal id, `operation` is `shared_crew.<action_type>`, `resources`
is the target, `request_id` the correlation id, and `metadata` holds the action
ids and a reduced principal (id, kind, provenance, login). That log's HMAC chain,
size rotation and age retention bound it; there is no second audit store.

The write is the log's fail-closed `critical` mode, off the event loop, bounded by
`AUDIT_TIMEOUT_SECONDS`. `record_action` returns whether the record is confirmed.
When it is not, the action still applies and its response says it was not
audited (`audited: false`). A refused audit must not wedge a turn, and it must not
be reported as recorded. An action whose own durable row already names its actor
(a message row carries its author) is audited with `schedule_action_audit`,
which does not make the caller wait and makes no audit claim in the response; a
failed write is logged.

Audited sites, all through `schedule_action_audit`:

| Action | Site | Initiator | Responder | Outcome |
|---|---|---|---|---|
| `session_create` | `chat_api.slot_create` | request actor | | `accepted` |
| `message` | `chat_api.chat_send` | request actor | | `accepted` |
| `cancel` | `routes_admission.api_work_cancel`, target the work id | request actor | | `applied`, or `conflict` on a stale or illegal revision |
| `approval`, `question_answer` | `DecisionRegistry.resolve` (`decisions.audit_decision_response`), target the decision id | the decision's initiator | the verified responder | `applied` for the winner, `conflict` for a different response that lost; a replay of the winner and a response to nothing pending add no record |

## 4. Admission and accepted work

### Operation state machine

`admission.TRANSITIONS` is the one state machine every entry point reports through,
and `website/src/api/sharedCrew.ts` `OPERATION_TRANSITIONS` mirrors it
(`test_typescript_transition_table_matches_python` pins the two equal).

```
submitted -> queued | admitted | rejected
queued -> admitted | cancelled | expired | reconciling
admitted -> running | queued | cancelled | expired | reconciling | failed
running -> cancelling | completed | failed | reconciling
cancelling -> cancelled | completed | failed | reconciling
reconciling -> queued | running | completed | failed | cancelled | unknown_outcome
```

`submitted` exists only in the client. `rejected` is reachable only from it, and
`AcceptedWork` refuses both: a rejected request was never accepted. `admitted ->
queued` is a start-time revalidation that found capacity gone; `admitted ->
cancelled` is one that found the grant or policy revoked, so a queue record is never
standing permission. `reconciling -> queued` requires proof the work never started.
Ambiguous execution after a crash ends in `unknown_outcome` and is surfaced, never
replayed.

`TASKQ_STATE_MAP` maps every `kiro_crew.taskq.model` state onto this machine; the
durable queue stays the store of record and this is its projection.

### Capacity accounting

Work counts against active capacity in `CAPACITY_HOLDING` and against queue depth in
`QUEUE_HOLDING`. `AcceptedWork.release_allowed` returns capacity only for terminal
work whose `RuntimeOwnershipRef` shows the runtime and every descendant proven stopped.
Restart, disconnect or lease expiry therefore never frees a slot while a child lives.

`RuntimeOwnershipRef` points into the existing ownership registry
(`runtime_ownership` lease, `RuntimeTenancy` epoch, taskq generation, systemd scope
unit). It is not a second registry. Its `liveness` is what the reconciler proved,
never what a client reported.

### Requests and idempotency

`AdmissionRequest.budget_key` is the initiator's principal id. It is never the session
creator and never a per-device token, so tabs, devices, reconnects and child tasks of
one person share one budget. Work in a shared session is charged to whoever started
it.

The client sends an `Idempotency-Key` header per logical send and reuses it on retry.
The deduplication scope is `idempotency_scope(budget_key, session_key, key)`, retained
for `IDEMPOTENCY_TTL_SECONDS`. A replay with the same `request_digest` returns the
existing `AcceptedWork`; a different digest is `idempotency_conflict`. A retry never
runs a prompt twice.

A chat send asks `admission_entry.chat_replay` before it writes anything, so a retry
under a key already accepted, whether the first send started a turn, queued behind a
busy session or already finished, answers JSON with `replayed: true`, the recorded
`admission` and, for a queued message, its `queue_id`. It appends no row, queues
nothing, starts no turn and opens no stream. Only a client-supplied key can match;
every chat path records the same digest (`admission_entry.chat_digest`), so a retry
matches whichever path the first send took. A replayed `Admission` handle closes its
coroutine unrun.

### Responses

A refusal or conflict answers `admission.error_body`: the dashboard's
`{"error", "code"}` envelope, plus `capacity` on a limit and `current` (the public
`AcceptedWork`) on a conflict, so a stale UI action reconciles against server truth.
The status comes from `REASON_HTTP_STATUS`, and a refusal with a retry hint sends
`Retry-After` (`retry_after_header`). `CapacityResponse` reports both scopes' limits
and usage and the effective `EnforcementLevel`. It has no wait-time field, because the
scheduler cannot back one.

### Fairness

Owner: `shared_crew.scheduler` (pure; one lock, so a race at the limit admits exactly
as many as there are slots) behind `shared_crew.admission_gate.AdmissionService`.

Two work classes. A ROOT (chat turn, task run, workflow run, cron run) is admitted
while its principal holds fewer than `principal_active` and the Crew fewer than
`crew_active` root slots; otherwise it waits in its principal's FIFO, bounded by
`principal_queue` and `crew_queue`, and a submission past either is refused
`principal_queue_full` / `crew_queue_full`. A CHILD (subagent) never queues here:
the durable task queue is its queue, so the gate bounds what a principal and the
Crew have outstanding (`principal_children`, `crew_children`) and refuses past it.
Background runs (task runner, workflows) cannot wait in a request, so they start
now or are refused with the active-limit reason.

A message sent to a session whose turn is running (or held behind its running
subagents) waits for that turn, not for capacity. It is accepted as parked work
(`admit(park=True)`, `FairScheduler.park`): durably `queued`, charged to its sender,
bounded by `principal_queue` and `crew_queue` and `max_request_bytes` like any root,
and counted in `queued` capacity, but never dispatched. The queue entry carries its
work id (`admission_entry.QUEUED_WORK_META_KEY`); the session's drain runs the turn
as that work (`turn_gate.queued_work`, `AdmissionService.start_parked`), which takes
its place in the fair queue then. Messages a drain merges into one turn end with that
turn's outcome (`extra.merged_into`). A parked message's expiry is a check-in: while
its queue card exists it waits on; once the card is gone it ends `cancelled`
(`queue_entry_gone`). A card cancelled through the work API does not run.

Across principals the scheduler is deficit round robin. Principals with queued roots
sit in a ring; a visit adds the quantum (1) to the principal's deficit and admits
while the deficit covers the head ticket's weight (1) and both scopes have room. A
principal blocked by its own limit is skipped with its deficit reset, so no credit
is banked while ineligible. Within one principal the queue is FIFO, so order within
a session is kept. The subagent pump's existing lane round-robin becomes per person
too: `taskq.lanes.set_lane_resolver` maps a root session to `principal:<id>` while
admission is on.

Starvation bound: with `P` principals holding eligible queued work, a continuously
eligible principal is admitted within `(P - 1) * (quantum + max_weight - 1)`
admissions of others, which is `P - 1` with the defaults. Every admitted root ends
within its run deadline, so the wall-clock wait is at most
`ceil(P / crew_active)` root deadlines; work that would wait past `queue_expiry`
expires `queue_expired`, reported, never hidden.

Who is charged. The initiator, read from `request[ACTOR_REQUEST_KEY]`
(`admission_gate.actor_budget`); never the session creator. Every caller without a
principal is `p_unknown`, one shared bucket. A child inherits the budget and root of
the work running in its parent session (`budget_for_session`), so a collaborator
steering or spawning inside someone else's turn charges that turn's initiator. Cron
and loopback automation with no recorded initiator charge `Principal.system("automation")`,
which has the same limits as anyone. Work an agent starts from inside a turn reaches
the gateway on the internal transport naming its calling session (`X-Session-Key`,
set by the in-process MCP broker); it is charged to the principal whose work runs
in that session (`admission_gate.internal_initiator`), so a person cannot gain a
second budget through automation.

### Accepted work, revalidation and recovery

`shared_crew.work_store` keeps each `AcceptedWork` as one `accepted_work` row in
`tasks.db` (`taskq.store_work`, created by `taskq.migrate`; additive, so the schema
version stays and an older build leaves the table in place). The row is committed
before the caller hears `admitted` or `queued`. Idempotency is the unique `scope`
column; a replay returns the stored record and a replayed handle never runs twice.
Terminal rows are purged after `IDEMPOTENCY_TTL_SECONDS`.

`Admission.run` waits for a slot, then revalidates right before starting: queue
expiry, `policy_version` (a change to `admission_enabled`, `hard_limits` or
`containment_enabled` since acceptance is `policy_changed`), the hard-limit probe,
and the entry point's own check (a chat slot must still be live). `running` is
written before the work launches, so `admitted` proves it never started. A queued
chat turn holds the slot reservation; while it waits a later send on that slot
cannot steer into it (`admission_entry.admission_pending`) and queues instead, so a
queued prompt is never delivered as a steer.

The gateway's own stop (a signal or an in-app restart, read through
`host_hooks.shutdown_probe`) never records `completed` for work it cut: a running
turn that returns once the stop began ends `cancelled` with
`extra.reconcile_detail = host_stopped_while_running`, and admitted or queued work
that would start then ends `cancelled` with `host_stopped_before_start`, unrun.

Deadlines: `root_deadline_seconds` for roots, `child_deadline_seconds` for children
and background runs, enforced by `AdmissionService.tick` (once a second): it cancels
the work and records `cancelled` with `run_deadline`. A root's slot is released only
after its own task ended and every child it started settled; a child's slot only
after its owner (subagent manager, task runner, workflow registry) reports it
terminal.

At gateway start (`startup_check`), and in any case before the first admission or
capacity read of a process, `ensure_ready` reconciles every non-terminal row:

| Found | Becomes | Capacity |
|---|---|---|
| `queued`, `admitted` | `reconciling`, then `queued` and relaunched when a relauncher is registered for its operation and payload, else `cancelled` with `extra.reconcile_detail = host_restarted_before_start` | held only if requeued |
| `running`, `cancelling` with no unit of its own | `reconciling` -> `unknown_outcome` (`cancelled` if a cancel was asked) | released once proven stopped |
| `running` with a live scope unit, or a principal slice holding a populated `kirocrew-rt-*.scope` | `reconciling`; the scopes found are stopped (`extra.orphans_reaped`), and the row ends `unknown_outcome` once `systemctl is-active` says each stopped | held |
| child or probed background run | `reconciling`; its owner reconciles the run and the probe settles the row | held |

Nothing that may have run is replayed. No relauncher is registered for chat turns in
this build, so a chat turn queued across a restart ends `cancelled`, and the session
says so in an error row (`host_hooks.reconcile_note`, also for `unknown_outcome`). A
parked message is the exception: its queue entry is saved with the session and drains
again after the restart. A session that has never been saved gets its metadata line
before its first work is accepted (`host_hooks.ensure_slot_recorded`), so a crash
right after the accept cannot leave a work record for a session that is gone.

A chat turn's work carries its accepted prompt and author as its payload
(`admission_entry.CHAT_PROMPT_PAYLOAD_KEY`, `CHAT_AUTHOR_PAYLOAD_KEY`) until it ends;
reconciliation reads it before the terminal write clears it and hands it to the note.
Notes wait until the startup restore has built its slots
(`host_hooks.mark_sessions_restored`). A session no slot holds is opened from disk,
so a session the crash left without an open-tab record can be opened; when it never
saved the accepted prompt, the prompt is put back as its user row ahead of the note.
The prompt is only shown, never run again.

### Containment

Owner: `shared_crew.containment`, reusing `sandbox_cgroup` and `sandbox_slice`.
Each principal's runtimes start in `kirocrew-agents-<instance>-p<sha256(id)[:12]>.slice`,
a child of the instance slice, sized by `systemctl --user set-property --runtime`
(`principal_cpu_quota_percent`, `principal_memory_max_mb`, `principal_tasks_max`); the
instance slice gets `crew_cpu_quota_percent`. `sandbox_slice.cgroup_scope_argv` picks
the principal slice from the `principal_scope` context the gate sets around admitted
work and around subagent run tasks. A principal slice nothing of its principal has
used for `SLICE_IDLE_SECONDS` (600) is stopped and its runtime drop-ins removed
(`systemctl --user stop` then `revert`), only while no runtime scope in it holds a
process; the gateway's stop releases every idle one at once and its start releases
those an earlier process left (`AdmissionService.sweep_slices`,
`release_stale_slices`). The slice is forgotten before it is stopped, so a spawn
racing the release lands in the instance slice. A shared session's runtime is one process, so it
stays in the slice of the initiator whose turn started it; later turns by others are
bounded by admission counts, not that slice.

The guarantee is `best_effort` and is reported that way: the kernel enforces the
CPU, memory and pids ceilings for cooperative or buggy work, but every runtime runs as
the gateway's uid, which can reach the user bus and the delegated subtree, so
adversarial escape is not closed (`adversarial_escape_closed: false`). IO is not
delegated on the supported profile and is `unavailable`. Disk is `app_accounting`:
`shared_crew.accounting` charges accepted request bytes per principal to the durable
`principal_storage` ledger and refuses past `principal_storage_mb` with
`storage_limit`; it is not a filesystem quota.

`hard_limits` turns any missing prerequisite (`not_linux`, `systemd_run_untrusted`,
`systemctl_untrusted`, `no_user_session`, `user_bus_unreachable`, `not_cgroup_v2`,
`controllers_not_delegated`, `controllers_unreadable`, or `containment_probe_failed`
when the probe or the startup check raised) into `containment_unavailable`
(503) at admission, and `runtime_start` refuses every runtime spawn and `session/new`
with `ContainmentUnavailable` (the backstop for entry points that do not reach the
gate). With it off the gate admits and the capacity answer says what holds.
`hard_limits` needs fair admission, which applies the limits: with
`admission_enabled` off and `hard_limits` on, nothing would apply them, so the
gateway fails closed with `admission_disabled` (every admission and every runtime
start refused, logged at ERROR at start) rather than running unlimited.

### Defaults and API

`dashboard.shared_crew` (`config.sections_shared_crew`), bounded at parse and resolved
by `effective_limits`: `admission_enabled` false (the single-operator default; nothing
is recorded and every entry point behaves as before), `principal_concurrency` 4,
`principal_child_limit` 8, `principal_queue_depth` 16, `crew_concurrency` 0 (the
session start gate's automatic width), `crew_child_limit` 0 (twice the Crew width),
`crew_queue_depth` 64, `queue_expiry_seconds` 300, `root_deadline_seconds` 1800,
`child_deadline_seconds` 600, `max_request_bytes` 4 MiB, `session_creates_per_minute`
30, `principal_storage_mb` 2048, `containment_enabled` true, `hard_limits` false,
`principal_cpu_quota_percent` 800, `principal_memory_max_mb` 24576,
`principal_tasks_max` 8192, `crew_cpu_quota_percent` 2400. These are proof-of-concept
test targets measured on one host, not product commitments.

`GET /api/shared-crew/capacity` reconciles first (`ensure_ready`), so after a
restart it counts work whose runtime is still alive, and answers `CapacityResponse` plus `effective` (every
resolved limit), `containment` (`ContainmentReport.to_dict`) and `fairness`.
`GET /api/shared-crew/work?session_key=` lists public records (the viewer's own without
`session_key`). The key may be a dashboard tab's slot key: it is read as the session
that tab's turns run on (`routes_admission.work_session_keys`), so the chat's work list
sees its own queued and running work. Every accepted-work write is pushed as a
`shared_crew.work` frame carrying `slot_key`, the tab that shows the session. `POST /api/shared-crew/work/{id}/cancel {expected_revision}` cancels a
queued or admitted record at once and moves running work to `cancelling`; a stale
revision or a terminal record is 409 with `current`. A chat send answers 202 with
`admission` when queued (WS mode) or carries `X-Shared-Crew-Work` /
`X-Shared-Crew-Work-State` headers (SSE mode).

## 5. Collaboration

Presence (`collab.PresenceEntry`) is telemetry. It is bound to the connection's
verified actor, folds every tab of one principal into one entry, expires after
`PRESENCE_TTL_SECONDS` without a refresh, is rate-limited to one activity frame per
`ACTIVITY_MIN_INTERVAL_SECONDS` per connection, and is never persisted. It never
grants access or frees a slot. Draft text is never sent.

`presence.PresenceRegistry` implements it in memory. `dashboard.ws` reads the
connection's principal once, from `request[ACTOR_REQUEST_KEY]` on the upgrade
request (Unknown when none was published), and hands each `shared_crew.activity`
frame to `presence.handle_activity_frame`, which reads only `session_key` and
`activity`, admits dashboard-user sockets only, and only for a session the gateway
has open. A refused frame (too soon, malformed, registry full) changes nothing and
is never answered with an eviction of someone else's entry. Disconnect and expiry
push a fresh `shared_crew.presence` frame for every session they changed; the
socket's status tick sweeps stale entries.

In the browser, typing is reported from the composer's user-edit handler
(`typingSignal.useComposerEditWithTyping`, which ChatInput calls only for the
user's own edits), not from the draft store: the store keeps one identity and a
session switch writes it too. The collab strip's `usePresenceReporter` listens to
that content-free signal and sends `typing`, then `viewing` after
`TYPING_HOLD_MS` without a keystroke.

A shared decision (`collab.SharedDecision`) is a tool approval or agent question that
every collaborator sees and any one may answer. `classify_response` decides a response
under the resolver's lock: the first response to see `pending` wins, a retry with the
winning `response_id` replays the same outcome, and any other response receives the
resolved decision with `already_resolved`. A resolved decision always names its
responder.

`decisions.DecisionRegistry` is that resolver: one in-memory, bounded record per
decision, with the classification, the side effect that settles the waiting future,
and the state write under one lock. A decision id is derived from what the card
already shows (`approval:<len>:<slot>:<request id>`, `approval-state:<id>`,
`question:<ask id>`), so every tab computes it without a lookup. A decision opens
the moment its card shows: a slot permission prompt in `chat_runner` (unless it was
declined at its append), a background approval in `ApprovalCoordinator.request`, and
a blocking or agent question in `QuestionCoordinator`. The initiator is the
author of the turn that asked (the latest user message's `author` stamp) or, for a
background approval, the `Principal.system` of its source; the responder is the
request's verified actor. Every existing gate on the approve and answer routes runs
before the registry sees a response. A client that sends `response_id` gets 409
`already_resolved` with the decision on a lost race; a client that sends none keeps
the existing 404. A wait that ends through a path with no verified responder closes
the decision with an Unknown responder, and an unanswered one closes as `expired` or
`withdrawn`. Approval decision frames ride `broadcast_ws` like approval frames.

An agent question belongs to every collaborator. `collaborators.is_collaborator_request`
admits the owner (`is_owner_dashboard_request`) and a dashboard-user session whose
verified actor is a person a membership boundary admitted: provenance
`tailscale_serve` (allowlisted and pinned by the middleware) or `enterprise`. A
direct tailnet peer, a Slack-allowlist dashboard token, an app token, an internal
caller and Unknown are not collaborators and keep the owner gate's 403.
`GET /api/ask-question/pending`, `POST /api/ask-question/{id}/answer` and
`POST /api/ask-question/dismiss` accept every collaborator; opening a question
(`POST /api/ask-question`) stays owner-only. The card frames (`question_card`,
`question_card_resolved`) and the question decision frame go to the owner sockets
and to every collaborator socket (`collaborators.broadcast_to_collaborators`, which
reads the socket's `_shared_crew_actor`), the same audience the pending read has.
Nothing else the owner set receives reaches a collaborator socket. `GET /api/shared-crew/presence` and `GET /api/shared-crew/decisions` (in
`routes_collab`) answer the same state for a reconnecting tab.

Message attribution. The session's creator is its default owner, shown on the
session row in the list (`SessionCreatorByline`). In the transcript a user message
written by the creator carries no extra label; a message any other participant
wrote carries the sender's name (`MessageByline`, `sharedCrewUi.bylineFor`). A
session with no recorded creator (legacy) treats the viewer as the default owner, so
a single-operator transcript is unchanged. An Unknown author reads as Unknown.

Recorded non-goal: no input lock. Typing or presence never locks the composer or the
session for anyone else; every collaborator can send at any time, and admission
(section 4) orders the turns. Presence and typing stay telemetry only.

## 6. Wire shapes

### HTTP

| Route | Answer |
|---|---|
| `GET /api/shared-crew/capabilities` | `Capabilities` |
| `GET /api/auth/me` | gains `principal` (`Principal.to_public_dict`) |
| `GET /api/shared-crew/sessions?creator=&q=&cursor=&limit=` | `SessionPage`; each item carries `attribution` |
| `GET /api/shared-crew/people` | observed collaborators with usage, and the directory state |
| `GET /api/shared-crew/capacity` | `CapacityResponse` for the viewer |
| `GET /api/shared-crew/work?session_key=` | public `AcceptedWork` list |
| `POST /api/shared-crew/work/{work_id}/cancel` `{expected_revision}` | the next `AcceptedWork`, or 409 with `current` |
| a chat send | 200 when admitted, 202 with `work` when queued, 429 / 413 / 503 with `error_body` |
| an approval or answer, with `response_id` | the `SharedDecision`; 409 `already_resolved` with it on a lost race |
| `GET /api/shared-crew/presence?session_key=` | the session's `PresenceEntry` list (dashboard users only) |
| `GET /api/shared-crew/decisions?session_key=` | the session's `SharedDecision` list (dashboard users only) |

Browser-bound principals use `to_public_dict` (no issuer or subject); browser-bound
work uses `AcceptedWork.to_public_dict` (no idempotency scope, lease or fencing ids).

### Session list filter

`SessionQuery.from_query` parses `creator` (`all`, `mine`, `unknown` or a principal
id), `q`, an opaque `cursor` and `limit` (clamped to `SESSION_PAGE_MAX`). The filter
runs on the server before pagination. `mine` resolves against the authenticated
viewer, and never matches for an Unknown viewer. The filter is discovery, not access
control. `SessionPage.total_matching` counts every session the filter passes across
all pages; `hidden_by_filter` counts the sessions it excluded.

### WebSocket

The server pushes whole-value frames: `shared_crew.presence`, `shared_crew.work`,
`shared_crew.decision` and `shared_crew.capacity` (`collab.SERVER_FRAMES`). The
client sends `shared_crew.activity` with a session and an `Activity`. The frame
carries no principal; the server stamps the connection's actor and ignores anything
else. On reconnect the client refetches the HTTP state and treats the next frames as
authoritative. It never resubmits a send on its own.

## 7. UI states

| `OperationState` | Shown as | Draft |
|---|---|---|
| `submitted` | sending | kept |
| `queued` | queued, with personal or Crew-wide limit named | sent |
| `admitted`, `running` | running | sent |
| `cancelling` | cancelling | sent |
| `reconciling` | checking | sent |
| `completed` | done | cleared |
| `rejected` | refused, with the reason and next step | restored |
| `failed`, `expired`, `cancelled`, `unknown_outcome` | ended, with the reason | sent |

## 8. Capabilities

`Capabilities` reports what this Crew can back. Every field defaults to
`unavailable`, so a build that wires nothing claims nothing. `containment_level`
states the effective guarantee (`hard`, `best_effort`, `none`), never the configured
intent. `membership_link` is an id from the closed `MEMBERSHIP_LINKS` set, never a
URL taken from config or a peer. Enterprise providers compose through the platform
seams in `kiro_crew.platform` and stay disabled in the public build.

### Aggregation

`capabilities.register_capability_source(name, fields, source)` is how a behaviour
module speaks for a field; the gateway wires the registrations at startup.
`aggregate_capabilities` combines them conservatively:

- A source answers only for the fields it declared. A name registers once.
- A source that raises, or returns anything but a `CapabilityContribution`, makes
  each declared field `unavailable` with reason `source_failed`.
- Two sources on one field: the weaker state wins (`unavailable` < `disabled` <
  `degraded` < `available`), and the weaker `containment_level` wins.
- `containment_level` is `none` unless `containment` is `available` or `degraded`.
- Two different membership links leave the link empty.

A source answers from state it already holds; it does no I/O on the request path.

`GET /api/shared-crew/capabilities` (`routes_capabilities`) answers
`Capabilities.to_dict()` plus these additive keys, states and reasons only, sent
`no-store`:

- `reasons`: field to `no_source` or `source_failed`;
- `enterprise`: each key in `enterprise.ENTERPRISE_CAPABILITY_KEYS` to `{state, reason}`;
- `setup_paths`: each shared-access setup path (`access.SETUP_PATHS`: `tailscale`,
  `kiro_login`, `systems_manager`) to `{state, reason}`. Tailscale reports what the
  gateway resolved at start (`access.note_tailnet`): `available` once Tailscale access
  is on and the tailnet name resolved, `degraded` (`tailnet_unreachable`) when it is
  on without a name, else `not_configured`. A path no source registered for
  (`access.register_setup_path_source`) reports `not_in_this_build`;
- `access_modes`: `shared_multi_user`, `personal_remote_control` and
  `agent_workload_identity`, each with `state` and `requires` (see Access modes);
- `workload_identity`: `supported_hosts`, the detected `host`, `host_supported` and a
  `reason` (`host_not_supported` off a supported host).

The People surface (Settings > People) shows one setup empty state while no setup
path is `available`: it names the three paths, each with the availability its
capability reports and a link to existing docs only. It offers no button that does
nothing.

### Enterprise seams

`PlatformContext.shared_crew_enterprise` (`platform.shared_crew_enterprise`) is the
one slot an edition fills. The public default answers `enabled() -> False` and `None`
for every provider. `enterprise.resolve_enterprise` checks each provider against its
protocol before use and reports one capability per concern:

| Key | Contract | Disabled behaviour |
|---|---|---|
| `human_credentials` | `CredentialVerifier`; `verify_credential` checks issuer, audience, key id, key rotation window, signature, lifetime, expiry and not-before with bounded skew | every credential rejected `unavailable` |
| `individual_grants` | `GrantAuthority.lookup` per person per Crew; `GrantGate` decides new access | `not_configured`; Tailscale and dashboard auth govern alone |
| `workload_identity` | `WorkloadIdentityProvider.bind` / `renew`; `WorkloadLease` binds, renews, disables; only on a host in `access.SUPPORTED_WORKLOAD_HOSTS` | `unavailable`; `host_not_supported` on any other host; no fallback credential exists |
| `crew_identity` | `CrewBindingProvider`; `CrewBinding` pairs `CrewIdentity` with a synthetic `system` principal | none |
| `heartbeat` | `HeartbeatTransport.send(payload, workload credential)`; `HeartbeatTracker` | never sent; needs `workload_identity` too |
| `inference_budget` | `InferenceBudgetProvider.state`; `BudgetGate` | `not_enforced` |
| `deployment_context` | opaque `deployment_ref`, `tenant_ref`, `resource_policy_ref` | empty |

`available` means a checked provider is composed. It says the Crew is configured for
the concern, not that the remote service answered. A provider, or anything it
returns, that sets `TEST_ONLY = True` is refused with `test_provider_refused` unless
the caller passes `allow_test_providers=True`, which only tests do.

No production credential format is defined in core. A provider parses its own format
into `PresentedCredential` and supplies the `KeySet`. A pass yields a `human`
principal keyed by `(provider, issuer, subject)` with `enterprise` provenance.

Identity class. A human credential carries an `access.IdentityClass`:
`organization_directory` (organization-managed, directory-backed), `individual` (an
individually authenticated consumer account) or `tailnet` (a Tailscale user). The
verifier copies `CredentialClaims.identity_class` onto the principal's `extra`; a
credential that states none, or an unknown value, is `individual`. A tailnet principal
is `tailnet`. Shared multi-user access is for `organization_directory` and `tailnet`:
`GrantGate.decide` refuses `individual` with `individual_account_not_eligible` before
it asks the authority. Personal remote control of one's own machine admits every human
class (`access.remote_control_eligibility`). Tailscale attribution is unchanged.

Grants are individual only: no group field, and `GrantGate.decide` reads only the
principal, its identity class and the authority. Session attribution, authorship,
presence and heartbeat state are not inputs, so none of them grants access. Non-human
principals are refused `not_individual`.

Call sites. `enterprise_gates.enterprise_gate_middleware` runs inner to token auth on
the dashboard. A request from a person on a shared path (tailnet or enterprise
provenance) passes `GrantGate.decide`; a POST that starts model work
(`enterprise_gates.CHARGEABLE_ROUTES`: chat send, the OpenAI shim, continue, side
turn, thread reply, member thread, regenerate, edit-resend, task retry, spawn,
continue a subagent, cron run, task plan and execute) passes `BudgetGate.check`. A
refusal is 403 `{error: shared_access_refused | inference_refused, reason}`. The local
operator, automation and apps are not shared access. With the default seam neither
gate is composed, so the middleware is a no-op and both concerns report unavailable.
Model work that does not arrive over HTTP (cron schedules, channels) is not yet
budget-checked here.

Cache and outage policy (`GrantCachePolicy`): a confirmed active grant is reused for
`fresh_seconds` (default 60, max 300). After that the authority is asked; when it is
unreachable the last confirmed active grant is honoured for `outage_grace_seconds`
more (default 240, max 900), then access is refused `authority_unavailable`. A
revocation is therefore noticed within `revocation_bound_seconds` (default 300). A
pushed revocation applies at once, and an older revision never overrides a newer one.
A missing or revoked grant is never extended by an outage. A clock that moves back
forces a fresh check. The cache is bounded (LRU).

Workload disablement is terminal for the lease: after it, no bind or renew is
attempted. A failed renewal keeps the current credential until it expires and no
longer. `WorkloadCredentialRef` is a handle, never material.

The synthetic Crew principal is keyed under its own provider namespace
(`crew-workload`), so no human credential can produce its id. `CrewBinding.inference_owner`
is the Crew, so chargeable inference is never billed to the human who started it.

Heartbeat health: `not_connected -> healthy <-> heartbeat_late`, moved only by an
acknowledged, workload-authenticated send; `last_seen` is the last acknowledged send. The payload is the closed `HEARTBEAT_PAYLOAD_KEYS` set of
ids, counts and capability states; anything else is refused before sending, so no
transcript, prompt, title or principal can ride in it. Freshness is telemetry, never
authorization.

Budget: `CHARGEABLE_OPERATIONS` (new turn, steer, retry, spawn, automation run) stop
with `inference_budget_exhausted` when spent and `inference_budget_unknown` when the
budget cannot be read. Reads, listing, export and cancel always proceed.

Caps compose by intersection: `effective_limit` takes the smallest of the tenant,
Crew and principal caps and names the binding scope, so a tenant cap added later only
tightens.

Credential isolation: an edition keeps credentials under
`enterprise.enterprise_state_dir(data_home)`, inside the secret vault directory. The
sensitive-path gate refuses it to every file tool and every sandbox mode hides it. No
enterprise setting is read from `config.json`.

### Service prerequisites and open decisions

Access modes and what each needs (`access.access_modes_dict`):

| Mode | Needs | Identity classes |
|---|---|---|
| `shared_multi_user` | a cloud account setup | `organization_directory`, `tailnet` |
| `agent_workload_identity` | a cloud account setup, on a supported host | the Crew's own machine identity |
| `personal_remote_control` | an individual login only | every human class |

Workload identity runs only on EKS at launch (`SUPPORTED_WORKLOAD_HOSTS = ('eks',)`,
detected by `access.detect_workload_host`); any other host reports
`host_not_supported`, and there is no static-key fallback. Personal remote control
is off by default (opt-in, `REMOTE_CONTROL_DEFAULT_ENABLED = False`) until that is
decided.

Infrastructure cost of the hosted platform belongs to the customer's own account. The
product shows no cost UI and reports no cost.

Live enterprise verification needs, none of which exist in the public build:

- an approved human credential contract and issuer key distribution;
- an authoritative individual-assignment service with revocation push;
- a workload identity provider on the target runtime;
- a heartbeat and discovery endpoint that accepts workload-authenticated calls;
- an inference budget and Profile service.

Open, for the identity and security owners: the cache and outage bounds above; whether
running work is stopped on revocation or only new work is refused; whether an unknown
budget should refuse (current) or allow for a bounded window; how the Crew and a relay
check each other through the authority.

Open product questions:

- whether a Crew must be deployed in the same cloud account as the console that
  manages it;
- where connection, membership and heartbeat state lives, in the backend or in the
  relay, preferring the least persistent storage, possibly a heartbeat over the relay;
- whether personal remote control is opt-in (current default) or opt-out.

Acceptance probes once services exist: forged, expired, wrong-audience, wrong-issuer
and retired-key credentials are refused; a revoked person cannot start work within the
revocation bound; the authority down past the bound refuses; disabling the workload
stops renewals and heartbeats; the Console shows `heartbeat_late` after the threshold; a
heartbeat capture holds only allowlisted keys; an exhausted budget refuses a new turn
and still serves an existing result.

## 9. Records and migration

Every record carries `schema_version` (`codec.SCHEMA_VERSION`). It is the Shared Crew
record version, unrelated to the platform `CONTRACT_VERSION`. Rules:

- An added optional field keeps the version. A changed meaning bumps it, and the
  reader handles every older version.
- A reader preserves keys it does not own (`codec.split_extra` into `extra`, written
  back by `merge_extra`), so an older gateway's rewrite never drops a newer field.
  Session metadata also passes through `history.carry_unowned_metadata`.
- An enum value a newer writer added reads as the field's explicit fallback
  (`enum_or`), not as a refusal of the whole record.
- No migration rewrites history to add attribution. Legacy stays legacy.

The contracts are pinned by `test/test_shared_crew_contracts.py`. Changing a field,
an enum value or a transition changes that test and the TypeScript mirror together.

### Operations

Prerequisites. One gateway on a host the operator controls. Collaborators reach it
through `tailscale serve` (preferred) or the tailnet address directly (only when
`KIROCREW_BIND` widens the bind), with
`dashboard.tailscale.trust_identity` on and `dashboard.tailscale.allowed_logins`
listing each person (section 2). Without that opt-in every remote caller is Unknown.
Per-person containment needs Linux with cgroup v2, a `systemd --user` session and the
cpu, memory and pids controllers delegated to the user slice; anything else runs with
admission counts only and reports it.

Supported profiles.

| Profile | Admission | Containment | `hard_limits` |
|---|---|---|---|
| macOS, Windows, Linux without delegation | counts and queues | `unavailable` | refuses all work, by design |
| Linux, cgroup v2, delegated user slice | counts and queues | `best_effort` (CPU, memory, pids) | allowed |
| Unrestricted host root | counts and queues | `best_effort` | not adversarial containment |

Start, stop and configure. Start with `kirocrew gateway` (or the installed service)
and stop with `kirocrew stop`. Limits live under `dashboard.shared_crew` (section 4)
and are re-read within seconds, without a restart (every turn gate call refreshes
the gateway's service, so enabling admission applies to the next turn):

```bash
kirocrew config set dashboard.shared_crew.admission_enabled true
kirocrew config set dashboard.shared_crew.principal_concurrency 2
kirocrew config set dashboard.shared_crew.hard_limits true   # only on a delegated Linux host
```

Check what holds with `GET /api/shared-crew/capabilities` (what is enforced) and
`GET /api/shared-crew/capacity` (the effective limits, the containment report and the
viewer's usage). A `hard_limits` host that lacks a prerequisite logs
`hard-limit mode is on and containment is unavailable (<reason>)` at start and refuses
every admission and runtime start until it is fixed.

Migration. Nothing is rewritten. Sessions created before Shared Crew read as
`legacy_missing` with an Unknown creator; their messages have no author. New sessions
and messages carry `attribution` and `author` stamps. No creator is backfilled and no
access is inferred from an author.

Rollback. Drain first: list `GET /api/shared-crew/work`, cancel what should not run
(`POST /api/shared-crew/work/{id}/cancel`), and wait until `capacity` shows nothing
active or queued. Then set `admission_enabled` false; new work passes through at once.
Accepted-work records stay in the task store (`tasks.db`), and attribution stays in
session metadata, which older readers carry as unowned keys. Never delete sessions to
roll back.

Diagnosing Unknown attribution. `GET /api/auth/me` answers the caller's `principal`.
`kind: unknown` with provenance `none` means the request did not come through a
verified tailnet path: check `trust_identity`, the caller's login in `allowed_logins`,
that `tailscale serve` (not Funnel or another proxy) fronts the port, and the gateway
log for whois failures. A channel sender is always Unknown in this build.

Quota ownership. `GET /api/shared-crew/capacity` names the limiting scope (`principal`
or `crew`) and the viewer's active and queued counts. `GET /api/shared-crew/people`
shows each observed person's usage when admission is on. Work records carry
`budget_key`, the opaque principal id charged.

Cancellation and recovery. Cancel a queued or admitted item from the work list in the
chat, or by the cancel route with its `expected_revision`; a stale revision answers
409 with the current record. A restart reconciles accepted work before admitting new
work (section 4): nothing that may have run is replayed, and a chat turn queued across
a restart ends `cancelled` with its user row in the transcript to resend.

Cleanup. The admission service purges terminal work records from the task store
24 hours after they ended (`IDEMPOTENCY_TTL_SECONDS`), while admission is on.
Presence, observed people and shared decisions are in memory and reset on restart.
Principal slices are runtime-only systemd units and disappear with the user session.

Known limits. Containment is `best_effort`: runtimes share the gateway's uid. IO is not
limited, and disk is accounting of accepted request bytes, not a filesystem quota. A
shared session's runtime stays in its first initiator's slice. Channel senders are
Unknown. The internal one-shot helpers and the CLI's own processes in the table in
section 1 bypass fair admission. Enterprise grant and budget gates exist as contracts
(section 8) and nothing calls them yet.
