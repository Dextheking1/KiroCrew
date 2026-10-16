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

The scheduling algorithm, its cost assumptions and its starvation bound are owned by
the admission module and documented in this section when it lands. Its constraints are
fixed now: limits apply at principal and Crew scope on every entry point; queues are
bounded per principal; the scheduler is fair across eligible principals and keeps
order within a session; a queued prompt never becomes a steering command; unknown
callers share one bounded bucket; automation charges its `Principal.system` owner. The
existing lane scheduler in `subagent_manager.admission` is the mechanism to extend,
not replace.

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
