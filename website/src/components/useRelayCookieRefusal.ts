import { useEffect, useState } from 'react'

/** Deadline for the pair of probe requests. A probe that runs out says nothing. */
const RELAY_PROBE_TIMEOUT_MS = 5000

/**
 * Whether something in front of the dashboard refuses the relayed browser view's
 * files because they come without a cookie (#23180).
 *
 * The view is framed in a sandbox without `allow-same-origin`, so its document has
 * an opaque origin. Its `crossorigin` script and stylesheet fetches then send no
 * cookies. A front door that checks a cookie on every request (an SSO or OAuth
 * proxy, a corporate tunnel) lets the frame's navigation through, because the
 * dashboard starts it and it carries the cookie, but refuses every asset. The view
 * never boots and the frame stays blank. The opaque frame sends the panel no
 * signal it could wait for, and the panel cannot read it.
 *
 * So the panel asks the question directly: it GETs the relay path once with
 * cookies and once without. The relay's own auth is the capability token in the
 * path, never a cookie, so with nothing in front both answers are the relay's.
 * Refused ONLY when the request with cookies reached the relay and the one
 * without did not: that difference is exactly the frame's failure. Every other
 * outcome (both fail, a timeout, the gateway down) is `false`, so the panel never
 * blames a proxy for what the other states already explain.
 *
 * Runs once per `path` / `resetKey` while `enabled`, not on a timer: the relay
 * pays a token check and an ownership proof per request.
 */
export function useRelayCookieRefusal(path: string, enabled: boolean, resetKey = 0): boolean {
  const [refused, setRefused] = useState(false)
  useEffect(() => {
    setRefused(false)
    if (!path || !enabled) return
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), RELAY_PROBE_TIMEOUT_MS)
    // True when the answer is the relay's: a 2xx that stayed under the relay path
    // (the relay's own `/` -> `index.html` redirect does). A sign-in redirect to
    // another page is a refusal even when that page answers 200.
    const reachesRelay = async (credentials: RequestCredentials): Promise<boolean> => {
      try {
        const res = await fetch(path, { credentials, cache: 'no-store', signal: ctrl.signal })
        void res.body?.cancel().catch(() => {})
        if (!res.ok) return false
        if (!res.redirected) return true
        const landed = new URL(res.url, window.location.href)
        return landed.origin === window.location.origin && landed.pathname.startsWith(path)
      } catch {
        return false
      }
    }
    void Promise.all([reachesRelay('same-origin'), reachesRelay('omit')]).then(([withCookie, withoutCookie]) => {
      if (!ctrl.signal.aborted) setRefused(withCookie && !withoutCookie)
    }).finally(() => clearTimeout(timer))
    return () => { clearTimeout(timer); ctrl.abort() }
  }, [path, enabled, resetKey])
  return refused
}
