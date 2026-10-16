import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useEffect } from 'react'
import { createComposerDraftStore } from '../../../chat-core/composer/draftStore'
import { setSharedCrewSender } from '../../../hooks/sharedCrew/sharedCrewSocket'
import { MIN_SPACING_MS, usePresenceReporter } from '../../../hooks/sharedCrew/usePresenceReporter'
import { composerTypingSignal, useComposerEditWithTyping } from './typingSignal'

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
  const { noteTyping } = usePresenceReporter('s1', true)
  useEffect(() => composerTypingSignal.subscribe(noteTyping), [noteTyping])
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

  it('keeps one handler identity while the edit handler is stable', () => {
    const edit = vi.fn()
    const { result, rerender } = renderHook(() => useComposerEditWithTyping(edit))
    const first = result.current
    rerender()
    expect(result.current).toBe(first)
  })
})
