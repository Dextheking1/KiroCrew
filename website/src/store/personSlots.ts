import { crewModeView } from '../lib/crewMode'
import type { ChatSlot } from '../types'

/**
 * The person's own session list: every live slot except a Crew Mode root's
 * workers, which their root lists in its side panel (`crewModeView`). The one
 * rule every place that lists sessions for the person reads: the chat sidebar
 * (through its `slots` prop, filtered by the same function), the Sessions page,
 * the command palette's Recents and an artifact's Send to session. The tab strip
 * and a worker's own open view are not lists of the person's sessions and read
 * the raw rows. Re-exported from `dashboardSlice`, the path callers import.
 */
let personSlotsOf: ChatSlot[] | null = null
let personSlots: ChatSlot[] = []
export function selectPersonSlots(state: { dashboard: { slots: ChatSlot[] } }): ChatSlot[] {
  // Memoized on the slots array itself, not on the state object, so a reader
  // re-renders only when the rows change and a stale state can never answer.
  const slots = state.dashboard.slots
  if (slots !== personSlotsOf) {
    personSlotsOf = slots
    personSlots = crewModeView(slots)
  }
  return personSlots
}
