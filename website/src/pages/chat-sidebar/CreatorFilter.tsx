/** The sidebar's creator filter (All / Mine / a person / Unknown) and the
 *  server-filtered session list it shows while a filter other than All is on.
 *
 *  The filter runs on the server before paging (`GET /api/shared-crew/sessions`),
 *  so the counts are the server's: `total_matching` sessions match across every
 *  page, and `hidden_by_filter` is how many the filter left out. The client never
 *  hides a row it was sent. Filtering is discovery, not access: every session is
 *  still reachable with All. The control renders only when the Crew can attribute
 *  sessions, so a single-operator Crew's sidebar is unchanged. */
import { useMemo, useState } from 'react'
import { Users } from 'lucide-react'
import SimpleSelect from '../../components/SimpleSelect'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn, Skeleton } from '../../components/ui'
import { capabilityUsable, principalName } from '../../api/sharedCrewUi'
import type { CreatorFilter as CreatorFilterValue } from '../../api/sharedCrew'
import { useSharedCrewCapabilities, useSharedCrewPeople, useSharedCrewSessionsPage } from '../../hooks/sharedCrew/useSharedCrew'
import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'
import { fmtRelativeTime } from '../chat/sessionOrder'
import { creatorLabel } from './SessionCreatorByline'

/** Whether this Crew can back a creator filter at all. */
export function useCreatorFilterAvailable(): boolean {
  const { capabilities } = useSharedCrewCapabilities()
  return capabilityUsable(capabilities.attribution)
}

export function CreatorFilterBar({ value, onChange }: { value: CreatorFilterValue; onChange: (v: CreatorFilterValue) => void }) {
  const people = useSharedCrewPeople(true)
  const { options, labels } = useMemo(() => {
    const opts: string[] = ['all', 'mine']
    const labs: string[] = [i18nT('sharedCrew.filter_all'), i18nT('sharedCrew.filter_mine')]
    for (const row of people.data?.people ?? []) {
      if (row.is_viewer) continue
      const name = principalName(row.principal)
      if (!name) continue
      opts.push(row.principal.id)
      labs.push(name)
    }
    opts.push('unknown')
    labs.push(i18nT('sharedCrew.unknown_person'))
    // A creator chosen from an earlier People read stays selectable even if the
    // person is no longer listed, so the control never shows an empty value.
    if (!opts.includes(value)) {
      opts.push(value)
      labs.push(i18nT('sharedCrew.filter_other_person'))
    }
    return { options: opts, labels: labs }
  }, [people.data, value])
  return (
    <div className="mx-2 mb-1 flex items-center gap-1.5 min-w-0" data-testid="creator-filter">
      <Users size={12} className="shrink-0 text-muted" aria-hidden />
      <SimpleSelect
        options={options}
        optionLabels={labels}
        value={value}
        onChange={v => onChange(v as CreatorFilterValue)}
        aria-label={i18nT('sharedCrew.filter_label')}
        className="h-7 text-[12px] py-0 min-w-0 flex-1"
      />
    </div>
  )
}

export interface FilteredSessionsProps {
  creator: CreatorFilterValue
  q: string
  connected: boolean
  onOpen: (key: string, title: string) => void
  onClear: () => void
}

export function CreatorFilteredSessions({ creator, q, connected, onOpen, onClear }: FilteredSessionsProps) {
  // The cursor trail of the pages already shown; Back pops it, More pushes.
  const [cursors, setCursors] = useState<string[]>([''])
  const cursor = cursors[cursors.length - 1]
  const page = useSharedCrewSessionsPage(creator, q.trim(), cursor, true)
  const data = page.data
  const unavailable = page.isSuccess && data === null
  return (
    <div className="flex-1 min-h-0 overflow-y-auto px-2 pb-2" data-testid="creator-filtered-sessions" aria-busy={page.isFetching}>
      {page.isLoading && (
        <div className="flex flex-col gap-2 pt-1" aria-label={i18nT('sharedCrew.loading')}>
          <Skeleton className="h-10" /><Skeleton className="h-10" /><Skeleton className="h-10" />
        </div>
      )}
      {page.isError && (
        <ErrorNotice
          message={page.error instanceof Error ? page.error.message : i18nT('sharedCrew.sessions_failed')}
          variant="inline"
          askAgent
          testId="creator-filter-error"
        />
      )}
      {unavailable && (
        <p className="text-muted text-[12px] px-1 py-2" data-testid="creator-filter-unavailable">
          {i18nT('sharedCrew.filter_unavailable')}{' '}
          <Btn className="ml-1" onClick={onClear}>{i18nT('sharedCrew.show_all_sessions')}</Btn>
        </p>
      )}
      {data && (
        <>
          <p className="text-muted text-[12px] px-1 pb-1" role="status" data-testid="creator-filter-counts">
            {i18nT('sharedCrew.filter_counts', {
              shown: fmtNumber(data.total_matching),
              hidden: fmtNumber(data.hidden_by_filter),
            })}
          </p>
          {data.items.length === 0 ? (
            <p className="text-muted text-[12px] px-1 py-2">{i18nT('sharedCrew.filter_empty')}</p>
          ) : (
            <ul className="flex flex-col" aria-label={i18nT('sharedCrew.filtered_sessions')}>
              {data.items.map(item => {
                const title = item.title || item.key
                const by = item.attribution ? creatorLabel(item.attribution) : i18nT('sharedCrew.unknown_person')
                const when = item.modified ?? ''
                return (
                  <li key={item.key}>
                    <button
                      type="button"
                      disabled={!connected}
                      onClick={() => onOpen(item.key, title)}
                      className="w-full text-left rounded-md px-2 py-1.5 bg-transparent border-none cursor-pointer hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-50 min-w-0"
                      data-testid={`creator-filter-row-${item.key}`}
                    >
                      <span className="block text-[11px] leading-4 text-muted truncate">
                        {item.agent && <>{item.agent}<span aria-hidden>{'\u00A0·\u00A0'}</span></>}
                        {by}
                        {when !== '' && <><span aria-hidden>{'\u00A0·\u00A0'}</span>{fmtRelativeTime(when)}</>}
                      </span>
                      <span className="block text-[13px] leading-5 text-text truncate" title={title}>{title}</span>
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
          <div className="flex gap-2 pt-1">
            {cursors.length > 1 && (
              <Btn onClick={() => setCursors(c => c.slice(0, -1))}>{i18nT('sharedCrew.page_previous')}</Btn>
            )}
            {data.next_cursor && (
              <Btn onClick={() => setCursors(c => [...c, data.next_cursor])}>{i18nT('sharedCrew.page_next')}</Btn>
            )}
          </div>
        </>
      )}
    </div>
  )
}
