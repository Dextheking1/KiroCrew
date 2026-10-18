/** A content-free "the main composer changed" signal. The composer owner calls
 *  `noteComposerInput()` on the user's own edit and `noteComposerSent()` when
 *  that draft leaves as a send; the presence reporter subscribes to both.
 *  Nothing typed is passed along: only the fact that a change happened. */
import { useCallback } from 'react'

type Listener = () => void
const listeners = new Set<Listener>()
const sentListeners = new Set<Listener>()

export const composerTypingSignal = {
  subscribe(listener: Listener): () => void {
    listeners.add(listener)
    return () => { listeners.delete(listener) }
  },
  /** Called when the composer's draft is sent: the typing it announced is over. */
  subscribeSent(listener: Listener): () => void {
    sentListeners.add(listener)
    return () => { sentListeners.delete(listener) }
  },
}

export function noteComposerInput(): void {
  for (const listener of listeners) listener()
}

/** The composer's draft left as a send, so this tab is no longer typing. */
export function noteComposerSent(): void {
  for (const listener of sentListeners) listener()
}

/** The composer's user-edit handler, also signalling that the user typed.
 *  ChatInput calls its `onChange` only for the user's own edits (typing, paste,
 *  undo, picker inserts), never for a draft a session switch restores, so that
 *  handler is the one signal that means "someone is typing". The draft store
 *  itself is not: its identity never changes, and a restore writes it too. */
export function useComposerEditWithTyping(edit: (value: string) => void): (value: string) => void {
  return useCallback((value: string) => { noteComposerInput(); edit(value) }, [edit])
}
