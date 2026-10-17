/**
 * The Shared Crew side of the dashboard socket: the server frames folded into the
 * React Query cache, the reconnect rule, and the one client frame (activity).
 *
 * Frames are whole values, so a frame replaces what it names, keyed by id and
 * revision: a replay after a reconnect, or an HTTP read that lands after a newer
 * frame, can never move an item backwards or show it twice. On every (re)open the
 * HTTP reads are invalidated and refetched, and the next frames are authoritative.
 * Nothing here ever resubmits a send.
 */
import type { QueryClient } from '@tanstack/react-query'
import { noteSendRevision } from '../../api/sendRace'
import type { Activity, SendState, SharedCrewActivityFrame, SharedCrewServerFrame } from '../../api/sharedCrew'
import { mergeDecision, mergeWork, type DecisionsResponse, type PresenceResponse, type WorkListResponse } from '../../api/sharedCrewUi'

export const sharedCrewKeys = {
  all: ['shared-crew'] as const,
  capabilities: ['shared-crew', 'capabilities'] as const,
  capacity: ['shared-crew', 'capacity'] as const,
  people: ['shared-crew', 'people'] as const,
  mode: ['shared-crew', 'mode'] as const,
  viewer: ['shared-crew', 'viewer'] as const,
  sessions: (creator: string, q: string) => ['shared-crew', 'sessions', creator, q] as const,
  work: (sessionKey: string) => ['shared-crew', 'work', sessionKey] as const,
  presence: (sessionKey: string) => ['shared-crew', 'presence', sessionKey] as const,
  decisions: (sessionKey: string) => ['shared-crew', 'decisions', sessionKey] as const,
  sendState: (sessionKey: string) => ['shared-crew', 'send-state', sessionKey] as const,
}

/** Fold one server frame into the cache. `data` is the frame the gateway sent. */
export function applySharedCrewFrame(queryClient: QueryClient, data: unknown): void {
  if (!data || typeof data !== 'object') return
  const frame = data as SharedCrewServerFrame
  switch (frame.type) {
    case 'shared_crew.presence': {
      const value: PresenceResponse = { type: frame.type, schema_version: frame.schema_version, session_key: frame.session_key, entries: frame.entries ?? [] }
      queryClient.setQueryData(sharedCrewKeys.presence(frame.session_key), value)
      return
    }
    case 'shared_crew.work': {
      const work = frame.work
      if (!work?.work_id) return
      // The chat reads its work list by tab key; the record names its session.
      const keys = new Set([work.session_key, frame.slot_key].filter((k): k is string => !!k))
      for (const key of keys) {
        queryClient.setQueryData<WorkListResponse | null>(sharedCrewKeys.work(key), prev => ({
          ...(prev ?? {}),
          items: mergeWork(prev?.items ?? [], work),
        }))
      }
      return
    }
    case 'shared_crew.decision': {
      const decision = frame.decision
      if (!decision?.decision_id) return
      queryClient.setQueryData<DecisionsResponse | null>(sharedCrewKeys.decisions(decision.session_key), prev => ({
        schema_version: prev?.schema_version ?? frame.schema_version,
        items: mergeDecision(prev?.items ?? [], decision),
      }))
      return
    }
    case 'shared_crew.capacity':
      queryClient.setQueryData(sharedCrewKeys.capacity, frame.capacity)
      return
    case 'shared_crew.send_state': {
      if (!frame.session_key) return
      noteSendRevision(frame.session_key, frame.revision)
      const value: SendState = { schema_version: frame.schema_version, session_key: frame.session_key, revision: frame.revision, last: frame.last ?? null }
      queryClient.setQueryData<SendState | null>(sharedCrewKeys.sendState(frame.session_key), prev =>
        prev && prev.revision > value.revision ? prev : value)
      return
    }
  }
}

/** A (re)opened socket: read server truth again; frames after this are current. */
export function onSharedCrewSocketOpen(queryClient: QueryClient): void {
  void queryClient.invalidateQueries({ queryKey: sharedCrewKeys.all }, { cancelRefetch: false })
}

type Sender = (payload: object) => void
let sender: Sender | null = null

/** Installed by the socket owner; `null` while there is no socket. */
export function setSharedCrewSender(next: Sender | null): void {
  sender = next
}

/**
 * Report this tab's activity in one session. The frame names no principal and
 * carries no text: the server stamps the connection's verified actor.
 */
export function sendActivity(sessionKey: string, activity: Activity): boolean {
  if (!sender || !sessionKey) return false
  const frame: SharedCrewActivityFrame = { type: 'shared_crew.activity', session_key: sessionKey, activity }
  sender(frame)
  return true
}
