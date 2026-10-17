/**
 * Shared Crew wire contracts. Mirrors `kiro_crew.shared_crew` (contracts.py,
 * admission.py, collab.py); the owning spec is
 * docs/system-specs/modules/shared-crew.md. Types only plus the state-machine
 * table: no request methods live here, so adding a Shared Crew endpoint still
 * goes through its domain's `create*Endpoints` factory.
 *
 * Every principal the browser holds is server-issued. The client never sends a
 * principal as "who I am"; the server stamps the authenticated actor.
 */

export const SHARED_CREW_SCHEMA_VERSION = 1 as const

export type PrincipalKind = 'human' | 'machine' | 'system' | 'unknown'

export const UNKNOWN_PRINCIPAL_ID = 'p_unknown'

export interface DisplaySnapshot {
  name: string
  login: string
  captured_at: number | null
}

/** Browser-facing principal (`Principal.to_public_dict`). */
export interface PublicPrincipal {
  id: string
  kind: PrincipalKind
  display: DisplaySnapshot
}

export type AttributionAvailability = 'recorded' | 'legacy_missing' | 'unattributed'

export type SessionTriggerKind = 'scheduled' | 'webhook' | 'system'

/** What started a session no person started (`shared_crew.trigger`). A label,
 *  never a person: `configured_by` is a verified principal only when recorded. */
export interface SessionTrigger {
  kind: SessionTriggerKind
  ref: string
  name: string
  configured_by: PublicPrincipal | null
}

export interface SessionAttribution {
  creator: PublicPrincipal
  availability: AttributionAvailability
  created_at: number | null
  trigger?: SessionTrigger
}

/** `unattributed` lists sessions with no human creator and no named trigger;
 *  `unknown` is its older spelling, which the server still accepts. */
export type CreatorFilter = 'all' | 'mine' | 'unattributed' | 'unknown' | (string & { readonly __principalId?: never })

export interface SessionPage<T> {
  schema_version: number
  items: T[]
  total_matching: number
  hidden_by_filter: number
  next_cursor: string
}

export type ActionType =
  | 'session_create'
  | 'message'
  | 'approval'
  | 'question_answer'
  | 'steer'
  | 'cancel'
  | 'retry'
  | 'control'

export type ActionOutcome = 'accepted' | 'applied' | 'rejected' | 'conflict' | 'failed'

export type CapabilityState = 'available' | 'degraded' | 'disabled' | 'unavailable'
export type EnforcementLevel = 'hard' | 'best_effort' | 'none'
export type MembershipLink = '' | 'tailscale_admin' | 'console'

export interface SharedCrewCapabilities {
  schema_version: number
  attribution: CapabilityState
  presence: CapabilityState
  queueing: CapabilityState
  containment: CapabilityState
  containment_level: EnforcementLevel
  authoritative_directory: CapabilityState
  membership_link: MembershipLink
  remote_actions: CapabilityState
  protocols: number[]
  /** Why a capability field is not what its sources would answer: `no_source`
   *  (nothing registered for it) or `source_failed` (its source raised). Only
   *  fields with such a reason appear. */
  reasons: Partial<Record<CapabilityField, CapabilityReason>>
  /** Each enterprise seam's state; the public build answers `not_configured`. */
  enterprise: Record<EnterpriseCapabilityKey, EnterpriseCapability>
  /** Each shared-access setup path's availability. An older gateway omits it. */
  setup_paths?: Record<SetupPath, SetupPathCapability>
  /** The three access modes, each with its own requirement. */
  access_modes?: AccessModes
  /** The hosted platforms workload identity supports, and this host. */
  workload_identity?: WorkloadIdentityHost
}

export type SetupPath =
  | 'tailscale'
  | 'kiro_login'
  | 'systems_manager'

export type SetupPathReason =
  | 'not_configured'
  | 'not_in_this_build'
  | 'tailnet_unreachable'
  | 'source_failed'

export interface SetupPathCapability {
  state: CapabilityState
  /** A `SetupPathReason` when not available, else empty. */
  reason: SetupPathReason | ''
}

export type IdentityClass =
  | 'organization_directory'
  | 'individual'
  | 'tailnet'

export type AccessRequirement = 'cloud_account' | 'individual_login'

export interface AccessMode {
  state: CapabilityState
  requires: AccessRequirement
  identity_classes?: IdentityClass[]
  /** Personal remote control only: whether it is on before the operator chooses. */
  default_enabled?: boolean
}

export interface AccessModes {
  shared_multi_user: AccessMode
  personal_remote_control: AccessMode
  agent_workload_identity: AccessMode
}

export interface WorkloadIdentityHost {
  supported_hosts: string[]
  host: string
  host_supported: boolean
  /** `host_not_supported` on any host outside `supported_hosts`. */
  reason: '' | 'host_not_supported'
}

/** The Crew's heartbeat health. Telemetry only: it never grants or revokes access. */
export type HeartbeatHealth =
  | 'healthy'
  | 'not_connected'
  | 'heartbeat_late'

export type CapabilityField =
  | 'attribution'
  | 'presence'
  | 'queueing'
  | 'containment'
  | 'containment_level'
  | 'authoritative_directory'
  | 'membership_link'
  | 'remote_actions'

/** `no_source` / `source_failed` for any field; the rest say why `attribution`
 *  cannot name a person reaching this Crew through `tailscale serve`. */
export type CapabilityReason =
  | 'no_source'
  | 'source_failed'
  | 'platform_unprovable'
  | 'daemon_same_user'
  | 'daemon_not_found'

export type EnterpriseCapabilityKey =
  | 'human_credentials'
  | 'individual_grants'
  | 'workload_identity'
  | 'crew_identity'
  | 'heartbeat'
  | 'inference_budget'
  | 'deployment_context'

export type EnterpriseUnavailableReason =
  | 'not_configured'
  | 'provider_refused'
  | 'test_provider_refused'
  | 'provider_failed'
  | 'requires_workload_identity'
  | 'host_not_supported'

export interface EnterpriseCapability {
  state: CapabilityState
  /** An `EnterpriseUnavailableReason` when unavailable, else empty. */
  reason: EnterpriseUnavailableReason | ''
}

export type OperationKind =
  | 'chat_turn'
  | 'session_start'
  | 'resume'
  | 'subagent'
  | 'workflow'
  | 'task'
  | 'cron'
  | 'channel_turn'
  | 'remote'
  | 'background'

export type OperationState =
  | 'submitted'
  | 'queued'
  | 'admitted'
  | 'running'
  | 'cancelling'
  | 'reconciling'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'expired'
  | 'rejected'
  | 'unknown_outcome'

export const TERMINAL_STATES: ReadonlySet<OperationState> = new Set<OperationState>([
  'completed',
  'failed',
  'cancelled',
  'expired',
  'rejected',
  'unknown_outcome',
])

/** Mirrors `admission.TRANSITIONS`; a test pins the two equal. */
export const OPERATION_TRANSITIONS: Readonly<Record<OperationState, readonly OperationState[]>> = {
  submitted: ['queued', 'admitted', 'rejected'],
  queued: ['admitted', 'cancelled', 'expired', 'reconciling'],
  admitted: ['running', 'queued', 'cancelled', 'expired', 'reconciling', 'failed'],
  running: ['cancelling', 'completed', 'failed', 'reconciling'],
  cancelling: ['cancelled', 'completed', 'failed', 'reconciling'],
  reconciling: ['queued', 'running', 'completed', 'failed', 'cancelled', 'unknown_outcome'],
  completed: [],
  failed: [],
  cancelled: [],
  expired: [],
  rejected: [],
  unknown_outcome: [],
}

export function isLegalTransition(from: OperationState, to: OperationState): boolean {
  return OPERATION_TRANSITIONS[from].includes(to)
}

export type ReasonCode =
  | 'principal_active_limit'
  | 'principal_queue_full'
  | 'crew_active_limit'
  | 'crew_queue_full'
  | 'session_create_rate'
  | 'request_too_large'
  | 'storage_limit'
  | 'containment_unavailable'
  | 'queue_expired'
  | 'run_deadline'
  | 'runtime_error'
  | 'grant_revoked'
  | 'policy_changed'
  | 'cancelled_by_user'
  | 'stale_revision'
  | 'already_resolved'
  | 'idempotency_conflict'
  | 'invalid_transition'
  | 'not_found'

/** `AcceptedWork.to_public_dict`. */
export interface AcceptedWork {
  work_id: string
  session_key: string
  operation: OperationKind
  state: OperationState
  revision: number
  accepted_at: number
  expires_at: number
  deadline_at: number | null
  reason: ReasonCode | null
}

export interface ScopeUsage {
  active: number
  queued: number
  /** `null` means the scope is not limited on this host. */
  active_limit: number | null
  queue_limit: number | null
}

/** No wait-time estimate exists by design; `retry_after_seconds` is a polling hint. */
export interface CapacityResponse {
  schema_version: number
  principal: ScopeUsage
  crew: ScopeUsage
  enforcement: EnforcementLevel
  limiting_scope: 'principal' | 'crew' | null
  reason: ReasonCode | null
  retry_after_seconds: number | null
  queue_expiry_seconds: number | null
  run_deadline_seconds: number | null
}

/** Refusal / conflict body: the dashboard's `{error, code}` envelope plus context. */
export interface SharedCrewErrorBody {
  error: string
  code: ReasonCode
  capacity?: CapacityResponse
  current?: AcceptedWork
}

export type Activity = 'viewing' | 'typing' | 'idle'

export interface PresenceEntry {
  principal: PublicPrincipal
  session_key: string
  activity: Activity
  connections: number
  expires_at: number
}

export type DecisionKind = 'approval' | 'question'
export type DecisionState = 'pending' | 'resolved' | 'expired' | 'withdrawn'

/** `SharedDecision.to_public_dict`. */
export interface SharedDecision {
  decision_id: string
  kind: DecisionKind
  session_key: string
  state: DecisionState
  revision: number
  initiator: PublicPrincipal
  responder: PublicPrincipal | null
  outcome: string
  resolved_at: number | null
}

export type SharedCrewServerFrame =
  | { type: 'shared_crew.presence'; schema_version: number; session_key: string; entries: PresenceEntry[] }
  /** `slot_key`: the dashboard tab showing the work's session, when one does. */
  | { type: 'shared_crew.work'; schema_version: number; work: AcceptedWork; slot_key?: string }
  | { type: 'shared_crew.decision'; schema_version: number; decision: SharedDecision }
  | { type: 'shared_crew.capacity'; schema_version: number; capacity: CapacityResponse }

/** Client activity report. Carries no principal: the server stamps the connection's actor. */
export interface SharedCrewActivityFrame {
  type: 'shared_crew.activity'
  session_key: string
  activity: Activity
}
