import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { useChatReactions } from '../hooks/useChatReactions'
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
    await waitFor(() => expect(result.current.error).toEqual({ mid: 'm-new-fail', kind: 'react' }))
    // Rolled back: the failed optimistic reaction is gone.
    expect(result.current.reactionsFor('m-new-fail').has('👀')).toBe(false)
  })

  it('a 409 on create reports the limit error kind', async () => {
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(1))

    ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockRejectedValue(Object.assign(new Error('full'), { status: 409 }))
    await act(async () => { await result.current.toggleReaction('m-limit', '👀').catch(() => {}) })
    await waitFor(() => expect(result.current.error).toEqual({ mid: 'm-limit', kind: 'limit' }))
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
    await waitFor(() => expect(result.current.error).toEqual({ mid: 'm-nocache', kind: 'react' }))
    // Not left active despite the absent initial cache.
    expect(result.current.reactionsFor('m-nocache').has('👍')).toBe(false)
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
  it('renders one inline toggle button per fixed emoji (single click to react)', () => {
    render(<MessageReactionBar active={new Set()} onToggle={() => {}} />)
    expect(screen.getAllByTestId('reaction-trigger')).toHaveLength(4)
  })

  it('marks an active emoji as pressed', () => {
    render(<MessageReactionBar active={new Set(['👍'])} onToggle={() => {}} />)
    const buttons = screen.getAllByTestId('reaction-trigger')
    const thumbsUp = buttons.find(b => b.getAttribute('data-emoji') === '👍')
    const star = buttons.find(b => b.getAttribute('data-emoji') === '⭐')
    expect(thumbsUp).toHaveAttribute('aria-pressed', 'true')
    expect(star).toHaveAttribute('aria-pressed', 'false')
  })

  it('calls onToggle with the clicked emoji in a single click', () => {
    const onToggle = vi.fn()
    render(<MessageReactionBar active={new Set()} onToggle={onToggle} />)
    const star = screen.getAllByTestId('reaction-trigger').find(b => b.getAttribute('data-emoji') === '⭐')!
    fireEvent.click(star)
    expect(onToggle).toHaveBeenCalledWith('⭐')
  })

  it('surfaces a message-scoped error through an inline notice', () => {
    render(<MessageReactionBar active={new Set()} onToggle={() => {}} error={{ mid: 'm-1', kind: 'react' }} />)
    expect(screen.getByText('reaction_failed')).toBeInTheDocument()
  })

  it('renders no notice when there is no error', () => {
    render(<MessageReactionBar active={new Set()} onToggle={() => {}} error={null} />)
    expect(screen.queryByText('reaction_failed')).not.toBeInTheDocument()
  })
})
