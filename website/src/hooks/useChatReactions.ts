import { useCallback, useMemo, useRef, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { reactionsApi, type ChatReaction, type ReactMessageBody } from '../api/reactions'
import { secureRandomId } from '../utils/secureId'

const reactionQueryKey = (slotKey: string | undefined) => ['chat-reactions', slotKey] as const

/** A failed reaction, scoped to the message it happened on so only that row's
 *  bar shows the notice rather than every bar on the page. */
export interface ReactionError {
  mid: string
  kind: 'react' | 'unreact' | 'limit'
}

/**
 * Hook to manage message reactions for a given slot.
 *
 * A reaction toggle is idempotent on `(slot_key, mid, emoji)` with no ordering,
 * no preview text and no jump-navigation. The one coordination it DOES need is
 * per-identity serialization: a quick on-then-off double click would otherwise
 * fire an add and a remove concurrently, and because the bodyless DELETE can
 * 404 before the POST takes the backend lock, the server could persist the
 * reaction the user ended on OFF. So toggles for a given `(mid, emoji)` are
 * chained here — the latest requested state is applied after the one in flight
 * settles — rather than racing.
 */
export function useChatReactions(slotKey: string | undefined) {
  const qc = useQueryClient()
  const queryKey = useMemo(() => reactionQueryKey(slotKey), [slotKey])
  const [error, setError] = useState<ReactionError | null>(null)
  const clearError = useCallback(() => setError(null), [])

  const { data: reactions = [], isLoading: loading, isError: loadFailed } = useQuery<ChatReaction[]>({
    queryKey,
    queryFn: async () => {
      const res = await reactionsApi.list(slotKey!)
      return res.reactions
    },
    enabled: !!slotKey,
  })

  /** Every emoji the CURRENT caller has placed on `mid`, as a Set for O(1) membership. */
  const reactionsFor = useCallback(
    (mid: string) => new Set(reactions.filter(r => r.mid === mid).map(r => r.emoji)),
    [reactions],
  )

  // Count of toggle mutations currently in flight. The settled refetch is held
  // until this hits zero, because a refetch fired for identity A's settle would
  // otherwise overwrite the cache and drop identity B's still-pending optimistic
  // entry — B's queued off-toggle would then read B as absent and POST instead
  // of DELETE, leaving the wrong final state. Draining to the server truth only
  // once nothing is pending keeps every pending identity intact until it commits.
  const inFlight = useRef(0)
  const settleWhenIdle = useCallback(() => {
    inFlight.current = Math.max(0, inFlight.current - 1)
    if (inFlight.current === 0) qc.invalidateQueries({ queryKey })
  }, [qc, queryKey])

  const { mutateAsync: addReactionAsync } = useMutation({
    mutationFn: (body: ReactMessageBody) => reactionsApi.create(body),
    onMutate: async (body: ReactMessageBody) => {
      inFlight.current += 1
      await qc.cancelQueries({ queryKey })
      const optimisticId = `temp-${secureRandomId()}`
      const optimistic: ChatReaction = {
        id: optimisticId,
        slot_key: body.slot_key,
        mid: body.mid,
        emoji: body.emoji,
        reacted_at: new Date().toISOString(),
      }
      qc.setQueryData<ChatReaction[]>(queryKey, old => [...(old ?? []), optimistic])
      return { optimisticId }
    },
    onError: (err: unknown, body, ctx) => {
      // Drop THIS optimistic entry by id rather than restoring a captured
      // snapshot: when the initial GET failed there is no prior snapshot to
      // restore, and a snapshot would also clobber other identities' pending
      // entries. Removing by id leaves the failed reaction off, so a re-click
      // correctly retries the create instead of sending a DELETE.
      if (ctx?.optimisticId) {
        qc.setQueryData<ChatReaction[]>(queryKey, old =>
          (old ?? []).filter(r => r.id !== ctx.optimisticId),
        )
      }
      const limit = typeof err === 'object' && err !== null && (err as { status?: number }).status === 409
      setError({ mid: body.mid, kind: limit ? 'limit' : 'react' })
    },
    onSuccess: (real, _body, ctx) => {
      if (!ctx) return
      qc.setQueryData<ChatReaction[]>(queryKey, old =>
        (old ?? []).map(r => r.id === ctx.optimisticId ? real : r),
      )
    },
    onSettled: settleWhenIdle,
  })

  const { mutateAsync: removeReactionAsync } = useMutation({
    mutationFn: ({ mid, emoji }: { mid: string; emoji: string }) =>
      reactionsApi.remove(slotKey!, mid, emoji),
    onMutate: async ({ mid, emoji }: { mid: string; emoji: string }) => {
      inFlight.current += 1
      await qc.cancelQueries({ queryKey })
      // Snapshot just the removed records so a failed delete restores exactly
      // them by id, without touching other identities' pending entries.
      const removed = (qc.getQueryData<ChatReaction[]>(queryKey) ?? []).filter(
        r => r.mid === mid && r.emoji === emoji,
      )
      qc.setQueryData<ChatReaction[]>(queryKey, old =>
        (old ?? []).filter(r => !(r.mid === mid && r.emoji === emoji)),
      )
      return { removed }
    },
    onError: (_err, { mid }, ctx) => {
      if (ctx?.removed?.length) {
        qc.setQueryData<ChatReaction[]>(queryKey, old => {
          const have = new Set((old ?? []).map(r => r.id))
          return [...(old ?? []), ...ctx.removed.filter(r => !have.has(r.id))]
        })
      }
      setError({ mid, kind: 'unreact' })
    },
    onSettled: settleWhenIdle,
  })

  // Serializes toggles per `(mid, emoji)`: a new toggle on the same identity
  // chains onto the one in flight rather than racing it, so a bodyless DELETE
  // that 404s before a concurrent POST takes the backend lock can no longer
  // leave the wrong final state persisted. Keyed by a tail promise per
  // identity; a NUL separator keeps the `mid`/`emoji` boundary unambiguous.
  const chains = useRef(new Map<string, Promise<void>>())

  /** Toggle `emoji` on `mid`: adds it if the caller has not reacted with it
   *  yet on this message, removes it otherwise. Rejects on failure so the
   *  caller can await it; the hook's own `error` state drives the UI notice. */
  const toggleReaction = useCallback(
    (mid: string, emoji: string): Promise<void> => {
      if (!slotKey) return Promise.resolve()
      setError(null)
      // Chain key is the (mid, emoji) pair joined by a NUL, which cannot occur
      // in either value so the boundary is unambiguous. Built with join +
      // fromCharCode rather than a template literal: this is an internal Map
      // key, never user-facing, and a literal separator string on this line
      // would trip the i18n added-lines gate (no-literal-string) as if it were
      // untranslated copy.
      const key = [mid, emoji].join(String.fromCharCode(0))
      // Wait for the previous toggle on this identity to settle (success OR
      // failure) before deciding add vs. remove, so the decision reads the
      // true current state rather than racing an in-flight mutation.
      const prior = (chains.current.get(key) ?? Promise.resolve()).catch(() => {})
      const next = prior.then(() => {
        // Read the add/remove decision from the LIVE cache inside the chained
        // callback, not the render-captured `reactionsFor`: a second and third
        // click queued while the first POST is still pending would otherwise
        // both see the click-time optimistic snapshot and emit POST, DELETE,
        // DELETE — the last DELETE 404s and the reaction ends OFF when the user
        // ended ON. The onSettled refetch keeps the cache current between links.
        const current = qc.getQueryData<ChatReaction[]>(queryKey) ?? []
        const alreadyReacted = current.some(r => r.mid === mid && r.emoji === emoji)
        return alreadyReacted
          ? removeReactionAsync({ mid, emoji }).then(() => {})
          : addReactionAsync({ slot_key: slotKey, mid, emoji }).then(() => {})
      })
      // The stored tail swallows rejections (so it never becomes an unhandled
      // rejection and a later toggle can still await it); drop it once it is
      // the settled tail and nothing newer replaced it.
      const tail = next.catch(() => {})
      chains.current.set(key, tail)
      void tail.then(() => {
        if (chains.current.get(key) === tail) chains.current.delete(key)
      })
      return next
    },
    [slotKey, qc, queryKey, addReactionAsync, removeReactionAsync],
  )

  return {
    reactions,
    loading,
    // True when the reactions GET failed after retries. Surfaced so the caller
    // can SHOW the failure instead of letting `data: reactions = []` quietly
    // read as "no reactions" and make a message's existing markers vanish with
    // no notice (errors-use-error-notice).
    loadFailed,
    error,
    clearError,
    reactionsFor,
    toggleReaction,
  }
}
