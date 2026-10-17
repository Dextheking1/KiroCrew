/**
 * Shared Crew reads and the one Shared Crew write the dashboard makes (cancel).
 *
 * Every read degrades: a gateway that does not serve a route answers 404 and the
 * method resolves `null`, which the UI renders as "not available on this Crew"
 * rather than as an error. Any other failure throws through `j` as usual.
 * Filtering, paging and counting happen on the server; the client sends the
 * query and renders the page it gets back.
 */
import type { AcceptedWork, CapacityResponse, CreatorFilter, PublicPrincipal, SendState, SharedCrewCapabilities } from '../sharedCrew'
import { isAcceptedWork } from '../sharedCrewUi'
import type {
  DecisionsResponse,
  Maybe,
  PeopleResponse,
  PresenceResponse,
  SharedCrewSessionPage,
  WorkListResponse,
} from '../sharedCrewUi'
import type { CrewModeResponse } from '../sharedCrewMode'
import type { ClientTransport } from './transport'

export interface SharedCrewSessionQuery {
  creator: CreatorFilter
  q?: string
  cursor?: string
  limit?: number
}

/** A cancel answer: the next state, or the server's current state on a stale revision. */
export type CancelWorkResult =
  | { ok: true; work: AcceptedWork | null }
  | { ok: false; conflict: true; current: AcceptedWork | null; code: string }

export function createSharedCrewEndpoints({ get, post, j }: ClientTransport) {
  const read = async <T,>(url: string): Promise<Maybe<T>> => {
    const r = await get(url)
    if (r.status === 404) return null
    return await j(r) as T
  }
  const requests = {
    sharedCrewCapabilities: () => read<SharedCrewCapabilities>('/api/shared-crew/capabilities'),
    sharedCrewSessions: (query: SharedCrewSessionQuery) => {
      const params = new URLSearchParams({ creator: query.creator })
      if (query.q) params.set('q', query.q)
      if (query.cursor) params.set('cursor', query.cursor)
      if (query.limit) params.set('limit', String(query.limit))
      return read<SharedCrewSessionPage>('/api/shared-crew/sessions?' + params)
    },
    sharedCrewPeople: () => read<PeopleResponse>('/api/shared-crew/people'),
    sharedCrewMode: () => read<CrewModeResponse>('/api/shared-crew/mode'),
    /** The viewer's server-derived principal from `/api/auth/me`, or `null` when
     *  this gateway does not report one. Never sent back as "who I am". */
    sharedCrewViewer: async (): Promise<Maybe<PublicPrincipal>> => {
      const r = await get('/api/auth/me')
      if (!r.ok) return null
      const body = await r.json().catch(() => null) as { principal?: PublicPrincipal } | null
      return body?.principal?.id ? body.principal : null
    },
    sharedCrewCapacity: () => read<CapacityResponse>('/api/shared-crew/capacity'),
    sharedCrewWork: (sessionKey: string) =>
      read<WorkListResponse>('/api/shared-crew/work?' + new URLSearchParams({ session_key: sessionKey })),
    sharedCrewPresence: (sessionKey: string) =>
      read<PresenceResponse>('/api/shared-crew/presence?' + new URLSearchParams({ session_key: sessionKey })),
    sharedCrewDecisions: (sessionKey: string) =>
      read<DecisionsResponse>('/api/shared-crew/decisions?' + new URLSearchParams({ session_key: sessionKey })),
    sharedCrewSendState: (sessionKey: string) =>
      read<SendState>('/api/shared-crew/send-state?' + new URLSearchParams({ session_key: sessionKey })),
    /** Cancel exactly one work item at the revision the UI showed. A 409 is not
     *  an error: it carries the item's current state, which the UI adopts. */
    sharedCrewCancelWork: async (workId: string, expectedRevision: number): Promise<CancelWorkResult> => {
      const r = await post('/api/shared-crew/work/' + encodeURIComponent(workId) + '/cancel', { expected_revision: expectedRevision })
      if (r.status === 409) {
        const body = await r.json().catch(() => ({})) as { current?: AcceptedWork; code?: string }
        return { ok: false, conflict: true, current: body.current ?? null, code: body.code ?? 'stale_revision' }
      }
      // Accepted with no item in the body: the caller re-reads the list.
      const body = await j(r) as Partial<AcceptedWork> & { work?: AcceptedWork }
      const work = isAcceptedWork(body) ? body : isAcceptedWork(body.work) ? body.work : null
      return { ok: true, work }
    },
  }
  return { requests }
}
