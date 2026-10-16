/** Settings > People: who this Crew has seen, kept apart from any authoritative
 *  assigned-user directory, each row labelled with where its evidence came from
 *  and, when the Crew can tell, how much work that person has active and queued.
 *
 *  Read-only by design. Access to a Crew is decided by the network and the
 *  sign-in in front of it, not here: Tailscale users are managed in Tailscale,
 *  and an enterprise Crew points to its console only when its capabilities say
 *  one is configured. There is no add, remove, role or admin control, and no
 *  management URL is ever taken from a peer or from config.
 *
 *  While no shared-access path is configured the whole surface is one setup
 *  empty state (`SharedAccessSetup`) instead. */
import { ExternalLink, Users } from 'lucide-react'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { Badge, ContentSkeleton, EmptyState } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { capabilityUsable, membershipPointer, principalInitials, principalName, sharedAccessConfigured, type PeopleRow, type PeopleSource } from '../../api/sharedCrewUi'
import { SharedAccessSetup } from './SharedAccessSetup'
import { useSharedCrewCapabilities, useSharedCrewPeople } from '../../hooks/sharedCrew/useSharedCrew'
import { fmtDateTime, fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/** The one fixed, public page where a tailnet's users are managed. */
const TAILSCALE_USERS_URL = 'https://login.tailscale.com/admin/users'

function sourceLabel(source: PeopleSource): string {
  switch (source) {
    case 'directory': return i18nT('sharedCrew.source_directory')
    case 'session_creator': return i18nT('sharedCrew.source_session_creator')
    default: return i18nT('sharedCrew.source_observed')
  }
}

function PersonRow({ row, usageAvailable }: { row: PeopleRow; usageAvailable: boolean }) {
  const name = principalName(row.principal) || i18nT('sharedCrew.unknown_person')
  const login = row.principal.display.login
  return (
    <li className="flex items-start gap-3 py-2 min-w-0" data-testid={`person-${row.principal.id}`}>
      <span className="inline-flex items-center justify-center w-8 h-8 rounded-full bg-bg-hover text-text text-[12px] font-semibold shrink-0" aria-hidden>
        {principalInitials(row.principal)}
      </span>
      <div className="flex flex-col gap-0.5 min-w-0 flex-1">
        <div className="flex items-center gap-2 min-w-0 flex-wrap">
          <span className="text-sm font-medium text-text-strong truncate max-w-full" title={name}>{name}</span>
          {row.is_viewer && <Badge variant="muted">{i18nT('sharedCrew.you')}</Badge>}
          {row.principal.kind === 'machine' && <Badge variant="muted">{i18nT('sharedCrew.kind_machine')}</Badge>}
          {row.sources.map(s => <Badge key={s} variant="aim">{sourceLabel(s)}</Badge>)}
        </div>
        {login && login !== name && <span className="text-[12px] text-muted truncate" title={login}>{login}</span>}
        <span className="text-[12px] text-muted">
          {row.last_seen_at ? i18nT('sharedCrew.last_seen', { when: fmtDateTime(row.last_seen_at * 1000) }) : i18nT('sharedCrew.not_seen_live')}
          {(row.sessions_created ?? 0) > 0 && (
            <><span aria-hidden>{'\u00A0·\u00A0'}</span>{i18nT('sharedCrew.sessions_created', { n: fmtNumber(row.sessions_created ?? 0) })}</>
          )}
        </span>
        {usageAvailable && row.usage && (
          <span className="text-[12px] text-muted" data-testid="person-usage">
            {i18nT('sharedCrew.usage_line', { active: fmtNumber(row.usage.active), queued: fmtNumber(row.usage.queued) })}
          </span>
        )}
      </div>
    </li>
  )
}

export function PeoplePanel() {
  const { capabilities, isLoading: capsLoading } = useSharedCrewCapabilities()
  const people = useSharedCrewPeople(true)
  const pointer = membershipPointer(capabilities.membership_link)
  if (capsLoading || people.isLoading) return <ContentSkeleton rows={4} />
  if (people.isError) {
    return <ErrorNotice message={people.error instanceof Error ? people.error.message : i18nT('sharedCrew.people_failed')} askAgent testId="people-error" />
  }
  if (!sharedAccessConfigured(capabilities)) return <SharedAccessSetup capabilities={capabilities} />
  const data = people.data
  if (!data || !capabilityUsable(capabilities.attribution)) {
    return (
      <EmptyState
        icon={<Users size={20} />}
        title={i18nT('sharedCrew.people_unavailable_title')}
        subtitle={i18nT('sharedCrew.people_unavailable_body')}
        testId="people-unavailable"
      />
    )
  }
  const usageAvailable = !!data.usage_available
  const directoryOn = capabilityUsable(data.directory.state) && capabilityUsable(capabilities.authoritative_directory)
  return (
    <div className="flex flex-col gap-4" data-testid="people-panel">
      <SettingsSection title={i18nT('sharedCrew.people_seen_title')}>
        <SettingsCard>
          <p className="text-[13px] text-muted">{i18nT('sharedCrew.people_seen_note')}</p>
          {data.people.length === 0 ? (
            <p className="text-[13px] text-muted pt-2">{i18nT('sharedCrew.people_seen_empty')}</p>
          ) : (
            <ul className="flex flex-col divide-y divide-border" aria-label={i18nT('sharedCrew.people_seen_title')}>
              {data.people.map(row => <PersonRow key={row.principal.id} row={row} usageAvailable={usageAvailable} />)}
            </ul>
          )}
          {!usageAvailable && <p className="text-[12px] text-muted pt-2">{i18nT('sharedCrew.usage_unavailable')}</p>}
        </SettingsCard>
      </SettingsSection>
      <SettingsSection title={i18nT('sharedCrew.directory_title')}>
        <SettingsCard>
          {directoryOn && (data.directory.people?.length ?? 0) > 0 ? (
            <ul className="flex flex-col divide-y divide-border" aria-label={i18nT('sharedCrew.directory_title')}>
              {(data.directory.people ?? []).map(row => <PersonRow key={row.principal.id} row={row} usageAvailable={usageAvailable} />)}
            </ul>
          ) : (
            <p className="text-[13px] text-muted">{i18nT('sharedCrew.directory_unavailable')}</p>
          )}
        </SettingsCard>
      </SettingsSection>
      <SettingsSection title={i18nT('sharedCrew.access_title')}>
        <SettingsCard>
          <p className="text-[13px] text-muted">{i18nT('sharedCrew.access_tailscale')}</p>
          {pointer === 'tailscale_admin' && (
            <a className="inline-flex items-center gap-1 text-[13px] text-accent pt-1" href={TAILSCALE_USERS_URL} target="_blank" rel="noopener noreferrer" data-testid="tailscale-admin-link">
              {i18nT('sharedCrew.access_tailscale_link')}
              <ExternalLink size={12} className="lucide-inline" aria-hidden />
            </a>
          )}
          {pointer === 'console' && (
            <p className="text-[13px] text-muted pt-1" data-testid="console-pointer">{i18nT('sharedCrew.access_console')}</p>
          )}
        </SettingsCard>
      </SettingsSection>
    </div>
  )
}
