/**
 * The send revision a send is based on, read before the send when this tab has
 * not learned one yet.
 *
 * The host compares a send only when it carries `base_revision`, so a send
 * leaving before the session's send-state read lands would be accepted even
 * after a collaborator's send. A send therefore waits for that read, bounded:
 * when it fails, answers nothing, or outlasts the bound, the send goes without a
 * base, as a gateway without the route expects. One read per session is in
 * flight at a time, and a late answer still records its revision.
 */
import { api } from '../../api/client'
import { baseRevisionFor, noteSendRevision } from '../../api/sendRace'

/** How long a send waits for the session's send-state read (ms). */
export const SEND_REVISION_SEED_WAIT_MS = 1500

const inFlight = new Map<string, Promise<void>>()

function readSendRevision(sessionKey: string): Promise<void> {
  const pending = inFlight.get(sessionKey)
  if (pending) return pending
  const read = (async () => {
    try {
      const state = await api.sharedCrewSendState(sessionKey)
      if (state) noteSendRevision(sessionKey, state.revision)
    } catch {
      // No revision learned: the send goes without a base.
    } finally {
      inFlight.delete(sessionKey)
    }
  })()
  inFlight.set(sessionKey, read)
  return read
}

/** Resolve once *sessionKey*'s send revision is known, or the read failed, or
 *  *waitMs* passed. Never rejects. */
export async function ensureSendRevision(sessionKey: string | null | undefined, waitMs = SEND_REVISION_SEED_WAIT_MS): Promise<void> {
  if (!sessionKey || baseRevisionFor(sessionKey) !== undefined) return
  let timer: ReturnType<typeof setTimeout> | undefined
  const bound = new Promise<void>(resolve => { timer = setTimeout(resolve, waitMs) })
  try {
    await Promise.race([readSendRevision(sessionKey), bound])
  } finally {
    clearTimeout(timer)
  }
}

/** Test seam: forget reads in flight. */
export function resetSendRevisionSeedForTests(): void {
  inFlight.clear()
}
