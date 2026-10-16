/**
 * Report this tab's presence in the open session: viewing while the tab is
 * visible, typing for a few seconds after a keystroke, idle while hidden.
 *
 * The report is an activity enum and a session key, nothing else: no draft text,
 * no keystroke and no principal ever leave the tab. Sends are spaced at least
 * `MIN_SPACING_MS` apart to stay under the server's per-connection rate limit,
 * and a keep-alive repeats the current activity before the server's presence
 * expiry. Typing never locks the composer for anyone.
 */
import { useCallback, useEffect, useRef } from 'react'
import type { Activity } from '../../api/sharedCrew'
import { sendActivity } from './sharedCrewSocket'

/** Server accepts one activity frame per 2s per connection; stay above it. */
export const MIN_SPACING_MS = 2_500
/** Repeat the current activity well inside the 30s server expiry. */
export const KEEPALIVE_MS = 10_000
/** How long after the last keystroke the tab still reads as typing. */
export const TYPING_HOLD_MS = 5_000

export function usePresenceReporter(sessionKey: string | null | undefined, enabled: boolean) {
  const current = useRef<Activity>('viewing')
  const lastSent = useRef(0)
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null)
  const typingTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const keyRef = useRef(sessionKey ?? '')
  keyRef.current = sessionKey ?? ''

  const flush = useCallback(() => {
    pending.current = null
    if (!keyRef.current) return
    if (sendActivity(keyRef.current, current.current)) lastSent.current = Date.now()
  }, [])

  const report = useCallback((activity: Activity, force = false) => {
    if (!enabled) return
    const changed = activity !== current.current
    current.current = activity
    if (!changed && !force) return
    const wait = MIN_SPACING_MS - (Date.now() - lastSent.current)
    if (wait <= 0) {
      if (pending.current) { clearTimeout(pending.current); pending.current = null }
      flush()
    } else if (!pending.current) {
      pending.current = setTimeout(flush, wait)
    }
  }, [enabled, flush])

  useEffect(() => {
    if (!enabled || !sessionKey) return
    const visible = () => typeof document === 'undefined' || document.visibilityState !== 'hidden'
    report(visible() ? 'viewing' : 'idle', true)
    const keepAlive = setInterval(() => report(current.current, true), KEEPALIVE_MS)
    const onVisibility = () => report(visible() ? 'viewing' : 'idle')
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      clearInterval(keepAlive)
      document.removeEventListener('visibilitychange', onVisibility)
      if (pending.current) { clearTimeout(pending.current); pending.current = null }
      if (typingTimer.current) { clearTimeout(typingTimer.current); typingTimer.current = null }
    }
  }, [enabled, sessionKey, report])

  /** Call on composer input. Carries no text by design. */
  const noteTyping = useCallback(() => {
    if (!enabled || !keyRef.current) return
    report('typing')
    if (typingTimer.current) clearTimeout(typingTimer.current)
    typingTimer.current = setTimeout(() => {
      typingTimer.current = null
      report('viewing')
    }, TYPING_HOLD_MS)
  }, [enabled, report])

  return { noteTyping }
}
