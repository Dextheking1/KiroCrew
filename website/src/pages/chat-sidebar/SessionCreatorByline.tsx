/** The session's creator, inline on the row's agent/meta line after the agent
 *  name (AUTOSDE session-row-fixed-height): plain muted text that yields first
 *  when the line is tight, never a new stacked line. Renders nothing for a slot
 *  the gateway sent no attribution for, so a single-operator Crew looks as before. */
import type { SessionAttribution } from '../../api/sharedCrew'
import { principalName } from '../../api/sharedCrewUi'
import { i18nT } from '../../i18n/t'

export function creatorLabel(attribution: SessionAttribution): string {
  const name = principalName(attribution.creator)
  return name || i18nT('sharedCrew.unknown_person')
}

export function SessionCreatorByline({ attribution }: { attribution?: SessionAttribution | null }) {
  if (!attribution) return null
  const label = creatorLabel(attribution)
  const login = attribution.creator.display.login
  const title = login && login !== label
    ? i18nT('sharedCrew.created_by_with_login', { name: label, login })
    : i18nT('sharedCrew.created_by', { name: label })
  return (
    <span
      data-testid="session-creator-byline"
      className="min-w-0 max-w-[40%] truncate font-normal text-muted"
      title={title}
    >
      <span aria-hidden>{'\u00A0·\u00A0'}</span>
      <span className="sr-only">{i18nT('sharedCrew.created_by', { name: label })}</span>
      <span aria-hidden>{label}</span>
    </span>
  )
}
