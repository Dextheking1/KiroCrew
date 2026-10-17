import { useEffect, useSyncExternalStore } from 'react'
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import {
  PREVIEW_FLAGS, PREVIEW_INSTANCE_SESSIONS, PREVIEW_REMOTE_CREW_CHAT, cachePreviewFlag,
  previewFlagConfigPath, previewFlagName, readPreviewFlag, setPreviewFlagPersister,
} from '../utils/previewFlags'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'

/** The slice of the `['kirocrewConfig']` body this sync reads. */
export type PreviewFlagsConfigBody = { dashboard?: { feature_previews?: unknown } }

const CONFIG_KEY = ['kirocrewConfig'] as const

/**
 * Toggles whose PATCH has not settled yet, counted per flag. While a flag has
 * one in flight, a config body read before the write landed still carries the
 * old value, and applying it would flip the switch back under the user's hand.
 */
const pending = new Map<string, number>()

/**
 * The last queued PATCH per flag. Each write for a flag starts only after the
 * one before it settled, so the gateway applies a flag's writes in click order
 * and the last click is the value it keeps.
 */
const tails = new Map<string, Promise<void>>()

/**
 * Flags this document has already tried to carry up from the cache. A failed
 * carry is not retried on every config refetch; the next document load tries
 * again.
 */
const carried = new Set<string>()

/** Carries this document started that have not settled, and whether any failed. */
let carriesInFlight = 0
let carryFailed = false

/**
 * Set in this browser once its cache has been brought in line with a server
 * copy, so every cache-only ON from then on is a value the server gave and
 * later dropped, not one set before the server kept the switches. Outside the
 * `mc-preview-` prefix, so it is never read as a flag.
 */
export const PREVIEW_FLAGS_SYNCED_KEY = 'kc-feature-previews-synced'

/**
 * The last toggle the gateway refused, for the Feature previews section to say
 * so. Without it a refused write would only flip the switch back, which reads
 * as the switch not working. `denied` is a 403: the dashboard session is not
 * the gateway owner, who alone may write config.
 */
export type PreviewFlagWriteError = { flag: string; denied: boolean }

let writeError: PreviewFlagWriteError | null = null
const writeErrorListeners = new Set<() => void>()

function setWriteError(next: PreviewFlagWriteError | null): void {
  if (writeError === next) return
  writeError = next
  for (const listener of writeErrorListeners) listener()
}

/** Forget the last refused toggle (the notice's dismiss). */
export function clearPreviewFlagWriteError(): void {
  setWriteError(null)
}

function subscribeWriteError(listener: () => void): () => void {
  writeErrorListeners.add(listener)
  return () => { writeErrorListeners.delete(listener) }
}

/** The last toggle the gateway refused, or `null`; cleared by the next accepted toggle. */
export function usePreviewFlagWriteError(): PreviewFlagWriteError | null {
  return useSyncExternalStore(subscribeWriteError, () => writeError, () => null)
}

/** Reset the module state between tests. */
export function __resetPreviewFlagServerSyncForTests(): void {
  setWriteError(null)
  pending.clear()
  tails.clear()
  carried.clear()
  carriesInFlight = 0
  carryFailed = false
}

function markSynced(): void {
  safeSetItem(PREVIEW_FLAGS_SYNCED_KEY, '1')
}

/** The server's `{name: bool}` map, or `null` when the body has none to trust. */
function serverFlags(body: PreviewFlagsConfigBody | undefined): Record<string, unknown> | null {
  const fp = body?.dashboard?.feature_previews
  return fp !== null && typeof fp === 'object' && !Array.isArray(fp) ? fp as Record<string, unknown> : null
}

/** Store *on* for *flag* in the cached config body, so readers of it agree with the write. */
function writeIntoCachedBody(qc: QueryClient, flag: string, on: boolean): void {
  qc.setQueryData<PreviewFlagsConfigBody>(CONFIG_KEY, prev => {
    if (!prev) return prev
    const fp = serverFlags(prev) ?? {}
    return { ...prev, dashboard: { ...prev.dashboard, feature_previews: { ...fp, [previewFlagName(flag)]: on } } }
  })
}

function settle(qc: QueryClient, flag: string): void {
  const left = (pending.get(flag) ?? 1) - 1
  if (left > 0) pending.set(flag, left)
  else pending.delete(flag)
  // Reconcile against the refetched body directly: one equal to the body already
  // cached keeps its identity (structural sharing), so the hook's effect would not
  // run again, and a refused write would leave the cache disagreeing with it.
  void qc.invalidateQueries({ queryKey: CONFIG_KEY })
    .then(() => reconcilePreviewFlags(qc, qc.getQueryData<PreviewFlagsConfigBody>(CONFIG_KEY)))
}

/**
 * Queue one write of *flag* to the gateway, after any earlier write of it. A
 * refused write is put right by the refetch it ends with: the server's own
 * value, when it has one, is what the cache then takes. A refused toggle is
 * also kept for {@link usePreviewFlagWriteError}, so the switch moving back
 * comes with a reason. `ifUnset` makes the
 * gateway refuse (409) when the key is already set. Resolves to whether the
 * gateway accepted it.
 */
function persist(qc: QueryClient, flag: string, on: boolean, ifUnset = false): Promise<boolean> {
  pending.set(flag, (pending.get(flag) ?? 0) + 1)
  const write = (tails.get(flag) ?? Promise.resolve())
    .then(() => api.patchConfig(previewFlagConfigPath(flag), on, ifUnset ? { ifUnset: true } : undefined))
    .then(() => {
      writeIntoCachedBody(qc, flag, on)
      if (!ifUnset) setWriteError(null)
      return true
    }, (e: unknown) => {
      // Only a user's toggle is reported. A refused carry is the one-time
      // migration finding the key already set (409), which is not an error.
      if (!ifUnset) setWriteError({ flag, denied: (e as { status?: unknown } | null)?.status === 403 })
      return false
    })
  const tail = write.then(() => settle(qc, flag))
  tails.set(flag, tail)
  void tail.then(() => { if (tails.get(flag) === tail) tails.delete(flag) })
  return write
}

/**
 * Bring the browser cache in line with one config body.
 *
 * - A flag the server holds as `true`/`false` is written into the cache when the
 *   cache differs, unless a toggle of that flag is still in flight.
 * - A flag the server does not hold reads as OFF, except in a browser that has
 *   never synced: there a cache-only ON is carried up once with `if_unset`, so
 *   a value another surface wrote first is never replaced. This is the one-time
 *   migration of a switch set before the server kept it.
 * - The retired "Remote crew sessions" key is never carried as itself: an ON is
 *   carried as "Chat on a crew", and the key is cleared once the server holds
 *   that switch, so it cannot keep the feature on behind a switch reading off.
 * - A body with no `feature_previews` object (a failed read has none) leaves the
 *   cache as it is.
 */
export function reconcilePreviewFlags(qc: QueryClient, body: PreviewFlagsConfigBody | undefined): void {
  const fp = serverFlags(body)
  if (fp === null) return
  const synced = safeGetItem(PREVIEW_FLAGS_SYNCED_KEY) === '1'
  const remote = fp[previewFlagName(PREVIEW_REMOTE_CREW_CHAT)]
  if (readPreviewFlag(PREVIEW_INSTANCE_SESSIONS) && (synced || typeof remote === 'boolean')) {
    cachePreviewFlag(PREVIEW_INSTANCE_SESSIONS, false)
  }
  for (const flag of PREVIEW_FLAGS) {
    if (pending.has(flag)) continue
    const server = fp[previewFlagName(flag)]
    if (typeof server === 'boolean') {
      if (readPreviewFlag(flag) !== server) cachePreviewFlag(flag, server)
      continue
    }
    const cacheOn = readPreviewFlag(flag)
      || (flag === PREVIEW_REMOTE_CREW_CHAT && readPreviewFlag(PREVIEW_INSTANCE_SESSIONS))
    if (!cacheOn) continue
    if (synced) {
      cachePreviewFlag(flag, false)
      continue
    }
    if (carried.has(flag)) continue
    carried.add(flag)
    carriesInFlight += 1
    void persist(qc, flag, true, true).then(ok => {
      carriesInFlight -= 1
      if (!ok) carryFailed = true
      if (carriesInFlight === 0 && !carryFailed) markSynced()
    })
  }
  if (!synced && carriesInFlight === 0 && !carryFailed) markSynced()
}

/**
 * Keep the Feature previews switches on the gateway, with the browser copy as a
 * cache (see `utils/previewFlags.ts`). Mount once, in the app shell.
 *
 * Reads the shared `['kirocrewConfig']` query, which a config `refresh` frame
 * already invalidates, so a switch flipped on another surface reaches this one
 * without a reload. Installs the persister `setPreviewFlag` hands each toggle to.
 */
export function usePreviewFlagServerSync(): void {
  const qc = useQueryClient()
  const { data } = useQuery<PreviewFlagsConfigBody>({
    queryKey: CONFIG_KEY,
    queryFn: () => api.kirocrewConfig(),
  })
  useEffect(() => {
    setPreviewFlagPersister((flag, on) => {
      // The retired key has no server copy; `setRemoteCrewChat` clears it in the
      // cache only.
      if (PREVIEW_FLAGS.includes(flag)) void persist(qc, flag, on)
    })
    return () => setPreviewFlagPersister(null)
  }, [qc])
  useEffect(() => {
    reconcilePreviewFlags(qc, data)
  }, [qc, data])
}
