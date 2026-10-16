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

Status: the contract types, codec, state machine and their tests are in the tree. The
behaviour each later section describes (derivation, stores, scheduler, routes, frames)
is the contract those modules implement; a section names its module once that module
exists. Until then the gateway behaves as a single-operator Crew and `Capabilities`
reports every Shared Crew capability unavailable.

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

The server derives the principal once per request, after `token_auth_middleware`
accepts it, and publishes an `ActorContext` under `request[ACTOR_REQUEST_KEY]`. The
browser never names itself: a principal is never read from a body, query, header or
display name, and `from_dict` exists only to read records this server wrote.

| Request | Principal | Provenance |
|---|---|---|
| Loopback peer, forwarded by local `tailscale serve`, resolved by `tailnet.resolve_forwarded_peer` | `human` keyed by tailnet login; `machine` keyed by node for a tagged node | `tailscale_serve` |
| Tailnet peer connecting to the gateway's tailnet address directly, resolved by daemon `whois` of the socket address | as above | `tailscale_direct` |
| Loopback peer with no forwarded chain | the local operator, keyed by the token subject | `local_operator` |
| Anything else auth admitted | `Principal.unknown()` | `none` |

Supported topologies are exactly the two tailnet rows. Loopback alone is not proof of
a trusted proxy: the forwarded address is attributed only through the daemon, and a
`Tailscale-User-Login` header that disagrees with the daemon drops the attribution.
Funnel, other reverse proxies and unverifiable forwarding headers resolve to Unknown.

`ActorContext.auth` is a copy of what the middleware decided, kept for audit. It is
not a second decision.

## 3. Session attribution and records

`SessionAttribution` is stored under the metadata-line key
`SESSION_ATTRIBUTION_META_KEY` (`attribution`). It is not `created_by`, which already
names the creating slot key. It is write-once: `keep_creator` returns the stored value
whenever one exists, so a later message by someone else never changes the creator.

A session with no `attribution` key reads as `legacy_missing` with an Unknown creator.
Nothing back-fills it, least of all from the current viewer. Attribution was on but no
principal could be established reads as `unattributed`. A damaged attribution object
reads as legacy, because authorization never reads it and a damaged record may only
lose its label.

A message's author rides in the message `meta` under `MESSAGE_AUTHOR_META_KEY`
(`author`), stamped when the server accepts the message.

`ActionRecord` audits a consequential shared action: session create, message,
approval, question answer, steer, cancel, retry and control actions. It names the
initiator, the responder where one exists, the runtime reference, the outcome and a
causal link. It carries ids, never prompt text or credentials. Audit rides the
existing crew log; there is no second audit store. When the audit append is refused,
the action still applies and its response says it was not audited. A refused audit
must not wedge a turn, and it must not be reported as recorded.

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
which has the same limits as anyone.

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

Deadlines: `root_deadline_seconds` for roots, `child_deadline_seconds` for children
and background runs, enforced by `AdmissionService.tick` (once a second): it cancels
the work and records `cancelled` with `run_deadline`. A root's slot is released only
after its own task ended and every child it started settled; a child's slot only
after its owner (subagent manager, task runner, workflow registry) reports it
terminal.

On the first admission of a process, `ensure_ready` reconciles every non-terminal row
before admitting anything:

| Found | Becomes | Capacity |
|---|---|---|
| `queued`, `admitted` | `reconciling`, then `queued` and relaunched when a relauncher is registered for its operation and payload, else `cancelled` with `extra.reconcile_detail = host_restarted_before_start` | held only if requeued |
| `running`, `cancelling` with no unit of its own | `reconciling` -> `unknown_outcome` (`cancelled` if a cancel was asked) | released once proven stopped |
| `running` with a live scope unit | `reconciling` until `systemctl is-active` says stopped | held |
| child or probed background run | `reconciling`; its owner reconciles the run and the probe settles the row | held |

Nothing that may have run is replayed. No relauncher is registered for chat turns in
this build, so a chat turn queued across a restart ends `cancelled`; its user row is
in the transcript to resend.

### Containment

Owner: `shared_crew.containment`, reusing `sandbox_cgroup` and `sandbox_slice`.
Each principal's runtimes start in `kirocrew-agents-<instance>-p<sha256(id)[:12]>.slice`,
a child of the instance slice, sized by `systemctl --user set-property --runtime`
(`principal_cpu_quota_percent`, `principal_memory_max_mb`, `principal_tasks_max`); the
instance slice gets `crew_cpu_quota_percent`. `sandbox_slice.cgroup_scope_argv` picks
the principal slice from the `principal_scope` context the gate sets around admitted
work and around subagent run tasks. A shared session's runtime is one process, so it
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
`controllers_not_delegated`, `controllers_unreadable`) into `containment_unavailable`
(503) at admission, and `runtime_start` refuses every runtime spawn and `session/new`
with `ContainmentUnavailable` (the backstop for entry points that do not reach the
gate). With it off the gate admits and the capacity answer says what holds.

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

`GET /api/shared-crew/capacity` answers `CapacityResponse` plus `effective` (every
resolved limit), `containment` (`ContainmentReport.to_dict`) and `fairness`.
`GET /api/shared-crew/work?session_key=` lists public records (the viewer's own without
`session_key`). `POST /api/shared-crew/work/{id}/cancel {expected_revision}` cancels a
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

A shared decision (`collab.SharedDecision`) is a tool approval or agent question that
every collaborator sees and any one may answer. `classify_response` decides a response
under the resolver's lock: the first response to see `pending` wins, a retry with the
winning `response_id` replays the same outcome, and any other response receives the
resolved decision with `already_resolved`. A resolved decision always names its
responder.

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
