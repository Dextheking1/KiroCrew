/**
 * React Query reads for Shared Crew. Each answers `null` when the gateway does
 * not serve the route, so a consumer renders "not available" rather than an
 * error. Live updates arrive as socket frames (./sharedCrewSocket) into these
 * same cache entries.
 */
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import type { CreatorFilter } from '../../api/sharedCrew'
import { ALL_UNAVAILABLE } from '../../api/sharedCrewUi'
import { sharedCrewKeys } from './sharedCrewSocket'

/** The Crew's capabilities; all unavailable until the gateway says otherwise. */
export function useSharedCrewCapabilities() {
  const query = useQuery({
    queryKey: sharedCrewKeys.capabilities,
    queryFn: () => api.sharedCrewCapabilities(),
    staleTime: 60_000,
    retry: false,
  })
  return { ...query, capabilities: query.data ?? ALL_UNAVAILABLE }
}

/** Who this tab is, as the server derived it; `null` when it reports no one. */
export function useSharedCrewViewer(enabled = true) {
  return useQuery({ queryKey: sharedCrewKeys.viewer, queryFn: () => api.sharedCrewViewer(), enabled, staleTime: 60_000, retry: false })
}

export function useSharedCrewPeople(enabled = true) {
  return useQuery({ queryKey: sharedCrewKeys.people, queryFn: () => api.sharedCrewPeople(), enabled, retry: false })
}

export function useSharedCrewCapacity(enabled = true) {
  return useQuery({ queryKey: sharedCrewKeys.capacity, queryFn: () => api.sharedCrewCapacity(), enabled, retry: false })
}

export function useSessionWork(sessionKey: string | null | undefined, enabled = true) {
  return useQuery({
    queryKey: sharedCrewKeys.work(sessionKey ?? ''),
    queryFn: () => api.sharedCrewWork(sessionKey ?? ''),
    enabled: enabled && !!sessionKey,
    retry: false,
  })
}

export function useSessionPresence(sessionKey: string | null | undefined, enabled = true) {
  return useQuery({
    queryKey: sharedCrewKeys.presence(sessionKey ?? ''),
    queryFn: () => api.sharedCrewPresence(sessionKey ?? ''),
    enabled: enabled && !!sessionKey,
    retry: false,
  })
}

export function useSessionDecisions(sessionKey: string | null | undefined, enabled = true) {
  return useQuery({
    queryKey: sharedCrewKeys.decisions(sessionKey ?? ''),
    queryFn: () => api.sharedCrewDecisions(sessionKey ?? ''),
    enabled: enabled && !!sessionKey,
    retry: false,
  })
}

/** One server-filtered page per cursor; the caller holds the cursor list. */
export function useSharedCrewSessionsPage(creator: CreatorFilter, q: string, cursor: string, enabled = true) {
  return useQuery({
    queryKey: [...sharedCrewKeys.sessions(creator, q), cursor],
    queryFn: () => api.sharedCrewSessions({ creator, q: q || undefined, cursor: cursor || undefined, limit: 50 }),
    enabled,
    retry: false,
    placeholderData: prev => prev,
  })
}
