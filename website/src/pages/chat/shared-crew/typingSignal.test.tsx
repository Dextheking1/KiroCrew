import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useEffect } from 'react'
import { createComposerDraftStore } from '../../../chat-core/composer/draftStore'
import { setSharedCrewSender } from '../../../hooks/sharedCrew/sharedCrewSocket'
import { MIN_SPACING_MS, TYPING_HOLD_MS, usePresenceReporter } from '../../../hooks/sharedCrew/usePresenceReporter'
import { composerTypingSignal, noteComposerSent, useComposerEditWithTyping } from './typingSignal'

let sent: Array<Record<string, unknown>>

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-16T00:00:00Z'))
  sent = []
  setSharedCrewSender(p => sent.push(p as Record<string, unknown>))
})

afterEach(() => {
  setSharedCrewSender(null)
  vi.useRealTimers()
})

/** The page's composer edit handler wired to the collab strip's reporter. */
function useComposerWithPresence(edit: (v: string) => void) {
  const { noteTyping, noteSent } = usePresenceReporter('s1', true)
  useEffect(() => composerTypingSignal.subscribe(noteTyping), [noteTyping])
  useEffect(() => composerTypingSignal.subscribeSent(noteSent), [noteSent])
  return useComposerEditWithTyping(edit)
}

describe('composer typing signal', () => {
  it('a user edit reaches the socket as typing, with no text', () => {
    const edit = vi.fn()
    const { result } = renderHook(() => useComposerWithPresence(edit))
    act(() => { vi.advanceTimersByTime(MIN_SPACING_MS) })
    act(() => { result.current('secret draft') })
    expect(edit).toHaveBeenCalledWith('secret draft')
    expect(sent.map(f => f.activity)).toEqual(['viewing', 'typing'])
    expect(JSON.stringify(sent)).not.toContain('secret draft')
  })

  it('a draft written straight to the store is not typing', () => {
    const store = createComposerDraftStore('')
    renderHook(() => useComposerWithPresence(store.set))
    act(() => { vi.advanceTimersByTime(MIN_SPACING_MS) })
    act(() => { store.set('restored by a session switch') })
    act(() => { vi.advanceTimersByTime(MIN_SPACING_MS) })
    expect(sent.map(f => f.activity)).toEqual(['viewing'])
  })

  it('a send ends typing at once instead of after the typing hold', () => {
    const edit = vi.fn()
    const { result } = renderHook(() => useComposerWithPresence(edit))
    act(() => { vi.advanceTimersByTime(MIN_SPACING_MS) })
    act(() => { result.current('hello') })
    expect(sent.map(f => f.activity)).toEqual(['viewing', 'typing'])
    act(() => { noteComposerSent() })
    // Only the server's rate limit spacing stands between the send and viewing.
    act(() => { vi.advanceTimersByTime(MIN_SPACING_MS) })
    expect(MIN_SPACING_MS).toBeLessThan(TYPING_HOLD_MS)
    expect(sent.map(f => f.activity)).toEqual(['viewing', 'typing', 'viewing'])
    // Past the hold and a keep-alive, the tab still reads viewing, never typing.
    act(() => { vi.advanceTimersByTime(TYPING_HOLD_MS) })
    expect(sent.slice(2).map(f => f.activity)).not.toContain('typing')
  })

  it('a send while not typing sends nothing', () => {
    renderHook(() => useComposerWithPresence(vi.fn()))
    act(() => { vi.advanceTimersByTime(MIN_SPACING_MS) })
    act(() => { noteComposerSent() })
    act(() => { vi.advanceTimersByTime(MIN_SPACING_MS) })
    expect(sent.map(f => f.activity)).toEqual(['viewing'])
  })

  it('keeps one handler identity while the edit handler is stable', () => {
    const edit = vi.fn()
    const { result, rerender } = renderHook(() => useComposerEditWithTyping(edit))
    const first = result.current
    rerender()
    expect(result.current).toBe(first)
  })
})
