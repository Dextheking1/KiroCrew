import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import {
  useChatReactions,
  reactionTogglePending,
  __resetReactionToggleLedgerForTests,
} from '../hooks/useChatReactions'
import { MessageReactionBar } from '../pages/chat/MessageReactionBar'
import type { ChatReaction } from '../api/reactions'

vi.mock('../api/reactions', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/reactions')>()
  return {
    REACTION_EMOJI: actual.REACTION_EMOJI,
    reactionsApi: {
      list: vi.fn(),
      create: vi.fn(),
      remove: vi.fn(),
    },
  }
})

vi.mock('../i18n/t', () => ({
  i18nT: (key: string, vars?: Record<string, unknown>) => {
    const base = key.split('.').pop() || key
    if (vars && 'emoji' in vars) return `${base} ${vars.emoji}`
    return base
  },
}))

import { reactionsApi } from '../api/reactions'

const mockReaction: ChatReaction = {
  id: 'r-1',
  slot_key: 'slot-abc',
  mid: 'm-mock-1234',
  emoji: '👍',
  reacted_at: '2026-08-01T12:00:00Z',
}

function createWrapper(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children)
}

describe('useChatReactions', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [mockReaction] })
    ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockResolvedValue(mockReaction)
    ;(reactionsApi.remove as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true })
  })

  it('fetches reactions on mount when slotKey is provided', async () => {
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(1))
    expect(reactionsApi.list).toHaveBeenCalledWith('slot-abc')
  })

  it('does not fetch when slotKey is undefined', () => {
    const { result } = renderHook(() => useChatReactions(undefined), { wrapper: createWrapper() })
    expect(result.current.reactions).toHaveLength(0)
    expect(reactionsApi.list).not.toHaveBeenCalled()
  })

  it('reactionsFor returns the set of emoji on a message', async () => {
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(1))
    expect(result.current.reactionsFor('m-mock-1234').has('👍')).toBe(true)
    expect(result.current.reactionsFor('m-mock-1234').has('⭐')).toBe(false)
    expect(result.current.reactionsFor('unknown-mid').size).toBe(0)
  })

  it('toggleReaction adds a reaction not yet present', async () => {
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(1))

    const newReaction: ChatReaction = { ...mockReaction, id: 'r-2', mid: 'm-new', emoji: '⭐' }
    ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockResolvedValue(newReaction)
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [mockReaction, newReaction] })

    await act(async () => { await result.current.toggleReaction('m-new', '⭐') })
    expect(reactionsApi.create).toHaveBeenCalledWith({
      slot_key: 'slot-abc', mid: 'm-new', emoji: '⭐',
    })
  })

  it('toggleReaction removes a reaction already present (same emoji, same message)', async () => {
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(1))

    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [] })
    await act(async () => { await result.current.toggleReaction('m-mock-1234', '👍') })
    expect(reactionsApi.remove).toHaveBeenCalledWith('slot-abc', 'm-mock-1234', '👍')
    expect(reactionsApi.create).not.toHaveBeenCalled()
  })

  it('a failed create rolls the optimistic reaction back and sets a message-scoped error', async () => {
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(1))

    ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    await act(async () => { await result.current.toggleReaction('m-new-fail', '👀').catch(() => {}) })
    await waitFor(() => expect(result.current.error).toEqual({ slot: 'slot-abc', mid: 'm-new-fail', emoji: '👀', kind: 'react' }))
    // Rolled back: the failed optimistic reaction is gone.
    expect(result.current.reactionsFor('m-new-fail').has('👀')).toBe(false)
  })

  it('a 409 on create reports the limit error kind', async () => {
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(1))

    ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockRejectedValue(Object.assign(new Error('full'), { status: 409 }))
    await act(async () => { await result.current.toggleReaction('m-limit', '👀').catch(() => {}) })
    await waitFor(() => expect(result.current.error).toEqual({ slot: 'slot-abc', mid: 'm-limit', emoji: '👀', kind: 'limit' }))
  })

  it('a failed create with NO prior cache drops the optimistic reaction (not left active)', async () => {
    // The initial GET fails, so onMutate captures no previous snapshot. A
    // failed create must still remove its own optimistic entry by id, or the
    // rejected reaction stays visibly active and a re-click would DELETE
    // instead of retrying the create (GPT useChatReactions:64).
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('list down'))
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.loadFailed).toBe(true))

    ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    await act(async () => { await result.current.toggleReaction('m-nocache', '👍').catch(() => {}) })
    await waitFor(() => expect(result.current.error).toEqual({ slot: 'slot-abc', mid: 'm-nocache', emoji: '👍', kind: 'react' }))
    // Not left active despite the absent initial cache.
    expect(result.current.reactionsFor('m-nocache').has('👍')).toBe(false)
  })

  it('a queued off-click after a failed create does NOT re-POST the reaction (intent, not cache)', async () => {
    // Click an absent reaction ON (POST fails, optimistic entry rolled out of
    // the cache), then click it OFF. The queued off-click must act on the
    // user's INTENT (off), not re-read the post-failure cache and send another
    // POST. (GPT 6.1 useChatReactions:237.)
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [] })
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(0))

    ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))

    await act(async () => {
      // Both clicks queued in the same tick: ON then OFF. Intent converges to
      // OFF on an already-absent reaction, so the chain settles to "nothing to
      // do" — crucially it never sends a POST that would persist it ON.
      const on = result.current.toggleReaction('m-x', '👀').catch(() => {})
      const off = result.current.toggleReaction('m-x', '👀').catch(() => {})
      await Promise.all([on, off])
    })
    // The reaction the user ended OFF is NOT persisted ON: no stray POST.
    expect(reactionsApi.remove).not.toHaveBeenCalled()
    expect(result.current.reactionsFor('m-x').has('👀')).toBe(false)
  })

  it('a single on-click still sends one create', async () => {
    // Guard the converge-to-intent logic does not swallow a lone click.
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [] })
    const created: ChatReaction = { ...mockReaction, id: 'r-one', mid: 'm-one', emoji: '👀' }
    ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockResolvedValue(created)
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(0))

    await act(async () => { await result.current.toggleReaction('m-one', '👀') })
    expect(reactionsApi.create).toHaveBeenCalledTimes(1)
    expect(reactionsApi.create).toHaveBeenCalledWith({ slot_key: 'slot-abc', mid: 'm-one', emoji: '👀' })
  })

  it('a reaction started in one slot settles against that slot after a switch', async () => {
    // Start a reaction in slot A, then re-render the hook for slot B before the
    // POST settles. The mutation's bookkeeping (ledger release + settled
    // refetch) must act on slot A, not the now-current slot B, or A's pending
    // count strands and A's cross-tab refreshes defer forever. (GPT 6.1
    // useChatReactions:121.)
    __resetReactionToggleLedgerForTests()
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [] })
    let resolveCreate: (r: ChatReaction) => void = () => {}
    const createStarted = new Promise<void>(start => {
      ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockImplementation(
        () => { start(); return new Promise<ChatReaction>(res => { resolveCreate = res }) })
    })
    const created: ChatReaction = { ...mockReaction, id: 'r-sw', slot_key: 'slot-A', mid: 'm-sw', emoji: '👍' }

    const { result, rerender } = renderHook(
      ({ slot }: { slot: string }) => useChatReactions(slot),
      { wrapper: createWrapper(), initialProps: { slot: 'slot-A' } },
    )
    await waitFor(() => expect(result.current.loading).toBe(false))

    let p: Promise<void> = Promise.resolve()
    await act(async () => {
      p = result.current.toggleReaction('m-sw', '👍')
      await createStarted // the POST is now in flight for slot A
    })
    expect(reactionTogglePending('slot-A')).toBe(true)
    // Switch to slot B before the create resolves.
    rerender({ slot: 'slot-B' })
    await act(async () => { resolveCreate(created); await p })
    // Slot A drained despite the hook now pointing at B.
    expect(reactionTogglePending('slot-A')).toBe(false)
    expect(reactionsApi.create).toHaveBeenCalledWith({ slot_key: 'slot-A', mid: 'm-sw', emoji: '👍' })
  })

  it('a queued second toggle on another identity is not reversed by the first settle', async () => {
    // Click 👍 on one message and ⭐ on another while both POSTs are pending.
    // The first settle's refetch must not drop the second's pending optimistic
    // entry and make its queued state read wrong (GPT useChatReactions:127).
    const r1: ChatReaction = { ...mockReaction, id: 'r-a', mid: 'm-a', emoji: '👍' }
    const r2: ChatReaction = { ...mockReaction, id: 'r-b', mid: 'm-b', emoji: '⭐' }
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [] })
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(0))

    ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockImplementation((b: { mid: string }) =>
      Promise.resolve(b.mid === 'm-a' ? r1 : r2))
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [r1, r2] })

    await act(async () => {
      await Promise.all([
        result.current.toggleReaction('m-a', '👍'),
        result.current.toggleReaction('m-b', '⭐'),
      ])
    })
    // Both reactions persisted as ADDs; neither was flipped to a delete.
    expect(reactionsApi.create).toHaveBeenCalledTimes(2)
    expect(reactionsApi.remove).not.toHaveBeenCalled()
    await waitFor(() => {
      expect(result.current.reactionsFor('m-a').has('👍')).toBe(true)
      expect(result.current.reactionsFor('m-b').has('⭐')).toBe(true)
    })
  })
})

describe('MessageReactionBar', () => {
  it('shows only the add trigger when nothing is placed (one control, under the cap)', () => {
    render(<MessageReactionBar active={new Set()} onToggle={() => {}} />)
    expect(screen.getByTestId('reaction-add-trigger')).toBeInTheDocument()
    expect(screen.queryAllByTestId('reaction-chip')).toHaveLength(0)
  })

  it('draws each placed emoji as its own visible chip, including an overflow emoji', () => {
    render(<MessageReactionBar active={new Set(['👍', '⭐'])} onToggle={() => {}} />)
    const chips = screen.getAllByTestId('reaction-chip')
    expect(chips.map(c => c.getAttribute('data-emoji'))).toEqual(['👍', '⭐'])
    // The ⭐ (an "overflow" emoji) is drawn as its glyph, not hidden behind a
    // highlighted + — the regression the UX review caught.
    const star = chips.find(c => c.getAttribute('data-emoji') === '⭐')!
    expect(star).toHaveTextContent('⭐')
    expect(star).toHaveAttribute('aria-pressed', 'true')
  })

  it('removes a placed reaction in one click on its chip', () => {
    const onToggle = vi.fn()
    render(<MessageReactionBar active={new Set(['👍'])} onToggle={onToggle} />)
    fireEvent.click(screen.getAllByTestId('reaction-chip')[0])
    expect(onToggle).toHaveBeenCalledWith('👍')
  })

  it('opens the add menu to place any not-yet-placed emoji', async () => {
    const onToggle = vi.fn()
    render(<MessageReactionBar active={new Set()} onToggle={onToggle} />)
    // Radix opens on keyboard activation, a path jsdom handles (unlike the
    // PointerEvent-driven mouse open).
    fireEvent.keyDown(screen.getByTestId('reaction-add-trigger'), { key: 'Enter' })
    const items = await screen.findAllByTestId('reaction-menu-item')
    expect(items.map(i => i.getAttribute('data-emoji'))).toEqual(['👍', '✅', '👀', '⭐'])
    const star = items.find(i => i.getAttribute('data-emoji') === '⭐')!
    fireEvent.click(star)
    expect(onToggle).toHaveBeenCalledWith('⭐')
  })

  it('hides the add trigger once every emoji is placed', () => {
    render(<MessageReactionBar active={new Set(['👍', '✅', '👀', '⭐'])} onToggle={() => {}} />)
    expect(screen.getAllByTestId('reaction-chip')).toHaveLength(4)
    expect(screen.queryByTestId('reaction-add-trigger')).not.toBeInTheDocument()
  })

  it('surfaces a message-scoped error through an inline notice', () => {
    render(<MessageReactionBar active={new Set()} onToggle={() => {}} error={{ slot: 's', mid: 'm-1', emoji: '👍', kind: 'react' }} />)
    // The mock i18n renders a key with an {emoji} var as "<lastSegment> <emoji>".
    expect(screen.getByText('reaction_failed_emoji 👍')).toBeInTheDocument()
  })

  it('renders no notice when there is no error', () => {
    render(<MessageReactionBar active={new Set()} onToggle={() => {}} error={null} />)
    expect(screen.queryByText(/reaction_failed_emoji/)).not.toBeInTheDocument()
  })
})
