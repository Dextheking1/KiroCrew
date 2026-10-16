/** A content-free "the main composer changed" signal. The composer owner calls
 *  `noteComposerInput()` on a draft change; the presence reporter subscribes.
 *  Nothing typed is passed along: only the fact that a change happened. */
type Listener = () => void
const listeners = new Set<Listener>()

export const composerTypingSignal = {
  subscribe(listener: Listener): () => void {
    listeners.add(listener)
    return () => { listeners.delete(listener) }
  },
}

export function noteComposerInput(): void {
  for (const listener of listeners) listener()
}
