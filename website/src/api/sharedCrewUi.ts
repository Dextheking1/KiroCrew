/**
 * Shared Crew browser-side shapes that are not frozen wire contracts, plus the
 * pure helpers the UI reads them through. The frozen contracts live in
 * `./sharedCrew` and are only imported here, never redefined.
 *
 * Every principal shown is one the server issued. Nothing here decides access:
 * a filter, a byline or a presence chip is discovery and display only.
 */
import {
  UNKNOWN_PRINCIPAL_ID,
  TERMINAL_STATES,
  type AcceptedWork,
  type CapabilityState,
  type CapacityResponse,
  type MembershipLink,
  type OperationState,
  type PresenceEntry,
  type PublicPrincipal,
  type ReasonCode,
  type SessionAttribution,
  type SessionPage,
  type SharedCrewCapabilities,
  type SharedDecision,
} from './sharedCrew'

/** One row of `GET /api/shared-crew/sessions`: a catalog row plus its attribution. */
export interface SharedCrewSessionItem {
  key: string
  title?: string
  agent?: string
  messages?: number
  modified?: string | number
  created?: string | number
  attribution?: SessionAttribution
}

/** `GET /api/shared-crew/sessions` answer: the frozen page plus the echoed viewer. */
export interface SharedCrewSessionPage extends SessionPage<SharedCrewSessionItem> {
  viewer?: PublicPrincipal
  creator?: string
}

/** Where a People row's evidence came from. Unknown values read as observed. */
export type PeopleSource = 'observed' | 'session_creator' | 'directory'

export interface PeopleRow {
  principal: PublicPrincipal
  sources: PeopleSource[]
  provenance?: string
  first_seen_at?: number | null
  last_seen_at?: number | null
  devices?: number
  sessions_created?: number
  is_viewer?: boolean
  /** `null` when this Crew cannot report usage for the person. */
  usage?: { active: number; queued: number } | null
}

/** `GET /api/shared-crew/people`. The directory is a separate, authoritative list. */
export interface PeopleResponse {
  schema_version: number
  viewer?: PublicPrincipal
  directory: { state: CapabilityState; people?: PeopleRow[] }
  observed_since?: number | null
  usage_available?: boolean
  people: PeopleRow[]
}

export interface PresenceResponse {
  type: 'shared_crew.presence'
  schema_version: number
  session_key: string
  entries: PresenceEntry[]
}

export interface DecisionsResponse {
  schema_version: number
  items: SharedDecision[]
}

export interface WorkListResponse {
  schema_version?: number
  items: AcceptedWork[]
}

/** A Shared Crew read that the gateway does not serve answers `null`, not an error. */
export type Maybe<T> = T | null

export const ALL_UNAVAILABLE: SharedCrewCapabilities = {
  schema_version: 1,
  attribution: 'unavailable',
  presence: 'unavailable',
  queueing: 'unavailable',
  containment: 'unavailable',
  containment_level: 'none',
  authoritative_directory: 'unavailable',
  membership_link: '',
  remote_actions: 'unavailable',
  protocols: [],
}

/** A capability the UI may lean on: `available` or `degraded`. */
export function capabilityUsable(state: CapabilityState | undefined): boolean {
  return state === 'available' || state === 'degraded'
}

export function isUnknownPrincipal(p: PublicPrincipal | null | undefined): boolean {
  return !p || p.kind === 'unknown' || p.id === UNKNOWN_PRINCIPAL_ID
}

/** The label to show for a principal, or `''` when the caller must say Unknown. */
export function principalName(p: PublicPrincipal | null | undefined): string {
  if (isUnknownPrincipal(p) || !p) return ''
  return (p.display.name || p.display.login || '').trim()
}

/** One or two letters for a principal's chip; never fetched, never an avatar URL. */
export function principalInitials(p: PublicPrincipal | null | undefined): string {
  const name = principalName(p)
  if (!name) return '?'
  const parts = name.split(/[\s@._-]+/).filter(Boolean)
  const first = Array.from(parts[0] ?? '')[0] ?? '?'
  const second = parts.length > 1 ? Array.from(parts[1])[0] ?? '' : ''
  return (first + second).toUpperCase()
}

/** Read a message row's server-stamped author, or `null` when it carries none. */
export function messageAuthor(meta: Record<string, unknown> | undefined): PublicPrincipal | null {
  const raw = meta?.author
  if (!raw || typeof raw !== 'object') return null
  const a = raw as Partial<PublicPrincipal> & { display?: Partial<PublicPrincipal['display']> }
  if (typeof a.id !== 'string' || !a.id) return null
  const kind = a.kind === 'human' || a.kind === 'machine' || a.kind === 'system' ? a.kind : 'unknown'
  return {
    id: a.id,
    kind,
    display: {
      name: typeof a.display?.name === 'string' ? a.display.name : '',
      login: typeof a.display?.login === 'string' ? a.display.login : '',
      captured_at: typeof a.display?.captured_at === 'number' ? a.display.captured_at : null,
    },
  }
}

/** What a work item looks like on screen (spec section 7). */
export type WorkView = 'sending' | 'queued' | 'running' | 'cancelling' | 'checking' | 'done' | 'refused' | 'ended'

export function workView(state: OperationState): WorkView {
  switch (state) {
    case 'submitted': return 'sending'
    case 'queued': return 'queued'
    case 'admitted':
    case 'running': return 'running'
    case 'cancelling': return 'cancelling'
    case 'reconciling': return 'checking'
    case 'completed': return 'done'
    case 'rejected': return 'refused'
    default: return 'ended'
  }
}

export function isTerminal(state: OperationState): boolean {
  return TERMINAL_STATES.has(state)
}

/** Cancel is offered while the work has not ended and is not already stopping. */
export function workCancellable(work: AcceptedWork): boolean {
  return work.state === 'queued' || work.state === 'admitted' || work.state === 'running'
}

/**
 * Fold one work item into a list by `work_id`, keeping whichever copy has the
 * higher revision. A frame replayed after a reconnect, or an HTTP read that
 * lands after a newer frame, can never move an item backwards or duplicate it.
 */
export function mergeWork(list: readonly AcceptedWork[], next: AcceptedWork): AcceptedWork[] {
  const i = list.findIndex(w => w.work_id === next.work_id)
  if (i < 0) return [...list, next]
  if (list[i].revision >= next.revision) return list as AcceptedWork[]
  const copy = list.slice()
  copy[i] = next
  return copy
}

/** Same rule as {@link mergeWork} for shared decisions. */
export function mergeDecision(list: readonly SharedDecision[], next: SharedDecision): SharedDecision[] {
  const i = list.findIndex(d => d.decision_id === next.decision_id)
  if (i < 0) return [...list, next]
  if (list[i].revision >= next.revision) return list as SharedDecision[]
  const copy = list.slice()
  copy[i] = next
  return copy
}

/** Which scope a refusal or a queue is held by. */
export type LimitScope = 'principal' | 'crew' | null

const PERSONAL_REASONS: ReadonlySet<ReasonCode> = new Set<ReasonCode>(['principal_active_limit', 'principal_queue_full', 'session_create_rate'])
const CREW_REASONS: ReadonlySet<ReasonCode> = new Set<ReasonCode>(['crew_active_limit', 'crew_queue_full', 'storage_limit', 'containment_unavailable'])

export function limitScope(reason: ReasonCode | null | undefined, capacity?: CapacityResponse | null): LimitScope {
  if (capacity?.limiting_scope) return capacity.limiting_scope
  if (reason && PERSONAL_REASONS.has(reason)) return 'principal'
  if (reason && CREW_REASONS.has(reason)) return 'crew'
  return null
}

const KNOWN_REASONS: ReadonlySet<string> = new Set<ReasonCode>([
  'principal_active_limit', 'principal_queue_full', 'crew_active_limit', 'crew_queue_full',
  'session_create_rate', 'request_too_large', 'storage_limit', 'containment_unavailable',
  'queue_expired', 'run_deadline', 'grant_revoked', 'policy_changed', 'cancelled_by_user',
  'stale_revision', 'already_resolved', 'idempotency_conflict', 'invalid_transition', 'not_found',
])

export function asReasonCode(code: unknown): ReasonCode | null {
  return typeof code === 'string' && KNOWN_REASONS.has(code) ? code as ReasonCode : null
}

/** A body is an `AcceptedWork` when it carries the fields the UI keys on. */
export function isAcceptedWork(value: unknown): value is AcceptedWork {
  if (!value || typeof value !== 'object') return false
  const w = value as Partial<AcceptedWork>
  return typeof w.work_id === 'string' && typeof w.state === 'string' && typeof w.revision === 'number'
}

/** Which access-management pointer the People surface may show. Never a URL. */
export function membershipPointer(link: MembershipLink | undefined): 'tailscale_admin' | 'console' | null {
  return link === 'tailscale_admin' || link === 'console' ? link : null
}
