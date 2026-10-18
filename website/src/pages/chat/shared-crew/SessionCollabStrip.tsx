/** The open session's collaboration state, above the composer: who else is
 *  here, this session's accepted work and its shared decisions. Each part reads
 *  only when the Crew reports the capability behind it, and the whole strip
 *  renders nothing when there is nothing to say, so a single-operator Crew's
 *  chat is unchanged. It also reports this tab's presence for the session. */
import { useEffect } from 'react'
import { capabilityUsable } from '../../../api/sharedCrewUi'
import {
  useSessionDecisions,
  useSessionPresence,
  useSessionSendState,
  useSessionWork,
  useSharedCrewCapabilities,
  useSharedCrewCapacity,
  useSharedCrewViewer,
} from '../../../hooks/sharedCrew/useSharedCrew'
import { usePresenceReporter } from '../../../hooks/sharedCrew/usePresenceReporter'
import { i18nT } from '../../../i18n/t'
import { DecisionAttributionList } from './DecisionAttributionList'
import { PresenceChips } from './PresenceChips'
import { WorkStatusList } from './WorkStatusList'

/** A composer `input` listener target: the strip tells the reporter about
 *  keystrokes without ever reading what was typed. */
export interface TypingSignal {
  subscribe(listener: () => void): () => void
}

export function SessionCollabStrip({ sessionKey, typing }: { sessionKey: string | null | undefined; typing?: TypingSignal }) {
  const { capabilities } = useSharedCrewCapabilities()
  const presenceOn = capabilityUsable(capabilities.presence)
  const queueOn = capabilityUsable(capabilities.queueing)
  const attributionOn = capabilityUsable(capabilities.attribution)
  const viewer = useSharedCrewViewer(presenceOn || attributionOn)
  const presence = useSessionPresence(sessionKey, presenceOn)
  const work = useSessionWork(sessionKey, queueOn)
  const capacity = useSharedCrewCapacity(queueOn)
  const decisions = useSessionDecisions(sessionKey, attributionOn)
  // The revision this tab's next send is based on (first submit wins). Read for
  // every open session: the host settles every send against it, whichever
  // capabilities the Crew reports, and app or automation sends count too.
  useSessionSendState(sessionKey)
  const { noteTyping } = usePresenceReporter(sessionKey, presenceOn)
  useEffect(() => typing?.subscribe(noteTyping), [typing, noteTyping])

  const entries = presence.data?.entries ?? []
  const workItems = work.data?.items ?? []
  const decisionItems = decisions.data?.items ?? []
  if (!sessionKey || (entries.length === 0 && workItems.length === 0 && decisionItems.length === 0)) return null
  return (
    <section
      className="w-full px-3 py-1 flex flex-col gap-1 min-w-0"
      aria-label={i18nT('sharedCrew.collab_region')}
      data-testid="session-collab-strip"
    >
      <PresenceChips entries={entries} viewer={viewer.data} />
      {sessionKey && <WorkStatusList sessionKey={sessionKey} items={workItems} capacity={capacity.data} />}
      <DecisionAttributionList items={decisionItems} />
    </section>
  )
}
