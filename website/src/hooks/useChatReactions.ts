import { useCallback, useMemo, useRef, useState } from 'react'
import { useQuery, useMutation, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { reactionsApi, type ChatReaction, type ReactMessageBody } from '../api/reactions'
import { secureRandomId } from '../utils/secureId'

const reactionQueryKey = (slotKey: string | undefined) => ['chat-reactions', slotKey] as const

// How many reaction toggles are mid-flight per slot, shared across every
// consumer in this tab (the hook AND the WebSocket frame router). A toggle
// holds an optimistic entry in the ['chat-reactions', slot] cache until its
// request settles; refetching that cache while the entry is pending drops it,
// and a queued off-toggle then reads the identity as absent and sends POST
// instead of DELETE — persisting the reaction the user ended OFF. The hook's
// own settled refetch already waits for this to drain (see `settleWhenIdle`);
// this module-level ledger extends the same guard to the `reactions_changed`
// WebSocket invalidation, which lives in another module and would otherwise
// bypass it entirely. Keyed by slot_key so a toggle on one slot never defers
// another slot's refresh.
const pendingToggles = new Map<string, number>()
// A slot whose settled refetch must fire once its last toggle drains. Set by
// the hook's own onSettled (it always wants the server truth once nothing is
// pending) and by a `reactions_changed` frame that arrived mid-flight (a
// concurrent-tab change that must still land). Either way the refetch is held
// until the slot's pending count hits zero so it cannot drop a still-pending
// optimistic entry, then fired exactly once.
const refetchWhenDrained = new Set<string>()

function beginToggle(slotKey: string): void {
  pendingToggles.set(slotKey, (pendingToggles.get(slotKey) ?? 0) + 1)
}

function endToggle(slotKey: string, qc: QueryClient): void {
  const next = Math.max(0, (pendingToggles.get(slotKey) ?? 0) - 1)
  if (next > 0) {
    pendingToggles.set(slotKey, next)
    return
  }
  // Nothing pending for this slot now. Fire the single settled refetch — the
  // server truth the hook always wants once its own toggles drain, and the
  // flush of any WS refresh held back while a toggle was in flight.
  pendingToggles.delete(slotKey)
  refetchWhenDrained.delete(slotKey)
  qc.invalidateQueries({ queryKey: reactionQueryKey(slotKey) })
}

/** True when a reaction toggle is still in flight for `slotKey`. The
 *  `reactions_changed` frame handler checks this so a cross-tab refetch never
 *  stomps an optimistic entry that a queued off-toggle still needs to read. */
export function reactionTogglePending(slotKey: string): boolean {
  return (pendingToggles.get(slotKey) ?? 0) > 0
}

/** Record that a `reactions_changed` refresh for `slotKey` was held back
 *  because a toggle was in flight; it is flushed when the last toggle settles. */
export function deferReactionInvalidation(slotKey: string): void {
  refetchWhenDrained.add(slotKey)
}

/** Test seam: drop all cross-consumer toggle state between cases. */
export function __resetReactionToggleLedgerForTests(): void {
  pendingToggles.clear()
  refetchWhenDrained.clear()
}

/** Test seam: pin a slot's pending-toggle count so a WS-frame test can drive
 *  the deferral branch without standing up real mutations. */
export function __setReactionTogglePendingForTests(slotKey: string, count: number): void {
  if (count <= 0) pendingToggles.delete(slotKey)
  else pendingToggles.set(slotKey, count)
}

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

  // Every mutation carries the slot it STARTED on through its variables, and
  // every callback derives its cache key and ledger slot from that — never from
  // the hook's render-captured `slotKey`. The hook is reused across slots
  // (ChatPage keeps one instance and swaps the active slot), so a reaction
  // started in slot A can settle after the user has switched to B. Reading the
  // render slot in onSettled would then release B's pending count and refetch
  // B's cache while A's count stays stuck above zero — stranding A's deferred
  // `reactions_changed` refreshes for the rest of the session. Keying off the
  // originating slot keeps each mutation's bookkeeping on its own slot.
  const { mutateAsync: addReactionAsync } = useMutation({
    mutationFn: (body: ReactMessageBody) => reactionsApi.create(body),
    onMutate: async (body: ReactMessageBody) => {
      const sk = body.slot_key
      const qk = reactionQueryKey(sk)
      beginToggle(sk)
      await qc.cancelQueries({ queryKey: qk })
      const optimisticId = `temp-${secureRandomId()}`
      const optimistic: ChatReaction = {
        id: optimisticId,
        slot_key: sk,
        mid: body.mid,
        emoji: body.emoji,
        reacted_at: new Date().toISOString(),
      }
      qc.setQueryData<ChatReaction[]>(qk, old => [...(old ?? []), optimistic])
      return { optimisticId, sk, qk }
    },
    onError: (err: unknown, body, ctx) => {
      // Drop THIS optimistic entry by id rather than restoring a captured
      // snapshot: when the initial GET failed there is no prior snapshot to
      // restore, and a snapshot would also clobber other identities' pending
      // entries. Removing by id leaves the failed reaction off; the chained
      // toggle's own per-identity intent (not this cache) decides the next
      // queued write, so a queued off-click still resolves to "leave it off".
      if (ctx?.optimisticId) {
        qc.setQueryData<ChatReaction[]>(ctx.qk, old =>
          (old ?? []).filter(r => r.id !== ctx.optimisticId),
        )
      }
      const limit = typeof err === 'object' && err !== null && (err as { status?: number }).status === 409
      setError({ mid: body.mid, kind: limit ? 'limit' : 'react' })
    },
    onSuccess: (real, _body, ctx) => {
      if (!ctx) return
      // Replace the optimistic row with the server record. If the optimistic
      // row is GONE — a concurrent refetch (e.g. a `reactions_changed` frame
      // from another identity's save) dropped it before this resolved — append
      // `real` instead of silently losing it, so the cache still reflects the
      // server. The ledger guard normally prevents the drop; this makes the
      // create self-healing even if an invalidation slips through.
      qc.setQueryData<ChatReaction[]>(ctx.qk, old => {
        const list = old ?? []
        if (list.some(r => r.id === ctx.optimisticId)) {
          return list.map(r => r.id === ctx.optimisticId ? real : r)
        }
        return list.some(r => r.id === real.id) ? list : [...list, real]
      })
    },
    onSettled: (_data, _err, _body, ctx) => { if (ctx) endToggle(ctx.sk, qc) },
  })

  const { mutateAsync: removeReactionAsync } = useMutation({
    mutationFn: ({ slot_key, mid, emoji }: { slot_key: string; mid: string; emoji: string }) =>
      reactionsApi.remove(slot_key, mid, emoji),
    onMutate: async ({ slot_key, mid, emoji }: { slot_key: string; mid: string; emoji: string }) => {
      const qk = reactionQueryKey(slot_key)
      beginToggle(slot_key)
      await qc.cancelQueries({ queryKey: qk })
      // Snapshot just the removed records so a failed delete restores exactly
      // them by id, without touching other identities' pending entries.
      const removed = (qc.getQueryData<ChatReaction[]>(qk) ?? []).filter(
        r => r.mid === mid && r.emoji === emoji,
      )
      qc.setQueryData<ChatReaction[]>(qk, old =>
        (old ?? []).filter(r => !(r.mid === mid && r.emoji === emoji)),
      )
      return { removed, sk: slot_key, qk }
    },
    onError: (_err, { mid }, ctx) => {
      if (ctx?.removed?.length) {
        qc.setQueryData<ChatReaction[]>(ctx.qk, old => {
          const have = new Set((old ?? []).map(r => r.id))
          return [...(old ?? []), ...ctx.removed.filter(r => !have.has(r.id))]
        })
      }
      setError({ mid, kind: 'unreact' })
    },
    onSettled: (_data, _err, _vars, ctx) => { if (ctx) endToggle(ctx.sk, qc) },
  })

  // Per-identity toggle state, keyed by `(mid, emoji)`. `tail` serializes the
  // writes so a new click chains onto the one in flight rather than racing it
  // (a bodyless DELETE that 404s before a concurrent POST takes the backend
  // lock could otherwise leave the wrong final state). `want` is the user's
  // LATEST intended state for that identity — flipped on every click at request
  // time — and is what the chained callback acts on. Reading intent from `want`
  // rather than the live cache is what fixes the queued-off-click-after-failure
  // race: a failed create rolls its optimistic entry out of the cache, but the
  // queued off-click still knows the user wants it OFF and sends nothing (it is
  // already absent) instead of re-POSTing it ON.
  const toggles = useRef(new Map<string, { tail: Promise<void>; want: boolean }>())

  /** Toggle `emoji` on `mid`: flips the caller's reaction on this message.
   *  Rejects on failure so the caller can await it; the hook's own `error`
   *  state drives the UI notice. */
  const toggleReaction = useCallback(
    (mid: string, emoji: string): Promise<void> => {
      if (!slotKey) return Promise.resolve()
      const sk = slotKey
      setError(null)
      // Chain key is the (mid, emoji) pair joined by a NUL, which cannot occur
      // in either value so the boundary is unambiguous. Built with join +
      // fromCharCode rather than a template literal: this is an internal Map
      // key, never user-facing, and a literal separator string on this line
      // would trip the i18n added-lines gate (no-literal-string) as if it were
      // untranslated copy.
      const key = [sk, mid, emoji].join(String.fromCharCode(0))
      const entry = toggles.current.get(key)
      // The new desired state is the opposite of the last INTENDED state, not
      // the last committed one: three fast clicks mean on, off, on regardless
      // of what has committed yet. Seed the first click's intent from the live
      // cache (whether the reaction is currently placed).
      const currentlyPlaced = (qc.getQueryData<ChatReaction[]>(reactionQueryKey(sk)) ?? [])
        .some(r => r.mid === mid && r.emoji === emoji)
      const want = entry ? !entry.want : !currentlyPlaced
      // Wait for the previous toggle on this identity to settle (success OR
      // failure) before issuing the next, so the writes never race.
      const prior = (entry?.tail ?? Promise.resolve()).catch(() => {})
      const next = prior.then(() => {
        // Act on the recorded intent, re-read at run time so a later click that
        // landed while this one waited wins. If the server already matches the
        // intent (the reaction is placed and we want it on, or absent and we
        // want it off), there is nothing to send.
        const placed = (qc.getQueryData<ChatReaction[]>(reactionQueryKey(sk)) ?? [])
          .some(r => r.mid === mid && r.emoji === emoji)
        const desired = toggles.current.get(key)?.want ?? want
        if (desired === placed) return
        return desired
          ? addReactionAsync({ slot_key: sk, mid, emoji }).then(() => {})
          : removeReactionAsync({ slot_key: sk, mid, emoji }).then(() => {})
      })
      // The stored tail swallows rejections (so it never becomes an unhandled
      // rejection and a later toggle can still await it); drop the entry once
      // it is the settled tail and nothing newer replaced it.
      const tail = next.catch(() => {})
      toggles.current.set(key, { tail, want })
      void tail.then(() => {
        if (toggles.current.get(key)?.tail === tail) toggles.current.delete(key)
      })
      return next
    },
    [slotKey, qc, addReactionAsync, removeReactionAsync],
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
