/**
 * The send revision a send is based on, read before the send when this tab has
 * not learned one yet.
 *
 * The host compares a send only when it carries `base_revision`, so a send
 * leaving before the session's send-state read lands would be accepted even
 * after a collaborator's send. A send therefore waits for that read, bounded:
 * when it fails, answers nothing, or outlasts the bound, the send goes without a
 * base, as a gateway without the route expects. The wait joins the read the
 * open session already started (the same query the collaboration strip runs),
 * so its answer is the state from when the session opened, not one taken after
 * the press. A late answer still records its revision.
 */
import type { QueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { queryClient as appQueryClient } from '../../api/queryClient'
import { baseRevisionFor, noteSendRevision } from '../../api/sendRace'
import { sharedCrewKeys } from './sharedCrewSocket'

/** How long a send waits for the session's send-state read (ms). */
export const SEND_REVISION_SEED_WAIT_MS = 1500

async function readSendRevision(client: QueryClient, sessionKey: string): Promise<void> {
  try {
    // staleTime 0: an answer already cached is re-read, while a read in flight
    // is joined rather than repeated.
    const state = await client.fetchQuery({
      queryKey: sharedCrewKeys.sendState(sessionKey),
      queryFn: () => api.sharedCrewSendState(sessionKey),
      staleTime: 0,
      retry: false,
    })
    if (state) noteSendRevision(sessionKey, state.revision)
  } catch {
    // No revision learned: the send goes without a base.
  }
}

/** Resolve once *sessionKey*'s send revision is known, or the read failed, or
 *  *waitMs* passed. Never rejects. */
export async function ensureSendRevision(
  sessionKey: string | null | undefined,
  { waitMs = SEND_REVISION_SEED_WAIT_MS, client = appQueryClient }: { waitMs?: number; client?: QueryClient } = {},
): Promise<void> {
  if (!sessionKey || baseRevisionFor(sessionKey) !== undefined) return
  let timer: ReturnType<typeof setTimeout> | undefined
  const bound = new Promise<void>(resolve => { timer = setTimeout(resolve, waitMs) })
  try {
    await Promise.race([readSendRevision(client, sessionKey), bound])
  } finally {
    clearTimeout(timer)
  }
}

/** The revision a send pressed now is based on: what this tab knew at the
 *  press, or, when it knew none, what the session's read answers (see
 *  `ensureSendRevision`). Read the press-time value synchronously at the press
 *  and hand it in, so a frame for a send this tab's person has not seen yet
 *  cannot raise the base while the send is on its way. */
export async function baseRevisionForPress(
  sessionKey: string | null | undefined,
  atPress: number | undefined,
  options?: { waitMs?: number; client?: QueryClient },
): Promise<number | undefined> {
  if (atPress !== undefined) return atPress
  await ensureSendRevision(sessionKey, options)
  return baseRevisionFor(sessionKey)
}

/** Test seam: forget every send-state read on the app's query client. */
export function resetSendRevisionSeedForTests(): void {
  appQueryClient.removeQueries({ queryKey: ['shared-crew', 'send-state'] })
}
