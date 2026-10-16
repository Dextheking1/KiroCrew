/** The accepted work of this session as the host reports it: sending, queued
 *  (and whose limit holds it), running, cancelling, checking, done, refused or
 *  ended. Cancel targets exactly one item at the revision shown; a stale click
 *  gets the item's current state back and the list adopts it.
 *
 *  Server truth only: the list is the `/work` read plus `shared_crew.work`
 *  frames, folded by id and revision, so a reconnect never duplicates a row and
 *  nothing here ever resends. */
import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, CircleSlash, Clock, Loader2, Send, Square, XCircle } from 'lucide-react'
import type { AcceptedWork, CapacityResponse } from '../../../api/sharedCrew'
import { isTerminal, mergeWork, workCancellable, workView, type WorkListResponse, type WorkView } from '../../../api/sharedCrewUi'
import { api } from '../../../api/client'
import { Btn } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import { sharedCrewKeys } from '../../../hooks/sharedCrew/sharedCrewSocket'
import { fmtNumber } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import { endedReason, queuedReason } from './overloadCopy'

const ICONS: Record<WorkView, typeof Clock> = {
  sending: Send,
  queued: Clock,
  running: Loader2,
  cancelling: Square,
  checking: Loader2,
  done: CheckCircle2,
  refused: CircleSlash,
  ended: XCircle,
}

export function workLabel(view: WorkView): string {
  switch (view) {
    case 'sending': return i18nT('sharedCrew.work_sending')
    case 'queued': return i18nT('sharedCrew.work_queued')
    case 'running': return i18nT('sharedCrew.work_running')
    case 'cancelling': return i18nT('sharedCrew.work_cancelling')
    case 'checking': return i18nT('sharedCrew.work_checking')
    case 'done': return i18nT('sharedCrew.work_done')
    case 'refused': return i18nT('sharedCrew.work_refused')
    case 'ended': return i18nT('sharedCrew.work_ended')
  }
}

/** Which items to show: every open one, plus the latest settled one so its
 *  outcome stays readable until newer work replaces it. At most five. */
export function visibleWork(items: readonly AcceptedWork[]): AcceptedWork[] {
  const settled = (w: AcceptedWork) => isTerminal(w.state)
  const lastSettled = [...items].reverse().find(settled)
  return items.filter(w => !settled(w) || w === lastSettled).slice(-5)
}

export function WorkStatusList({ sessionKey, items, capacity }: {
  sessionKey: string
  items: readonly AcceptedWork[]
  capacity: CapacityResponse | null | undefined
}) {
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const shown = visibleWork(items)
  if (shown.length === 0) return null
  const queuedCount = items.filter(w => w.state === 'queued').length
  const cancel = async (w: AcceptedWork) => {
    setBusy(w.work_id)
    setError('')
    setNotice('')
    try {
      const result = await api.sharedCrewCancelWork(w.work_id, w.revision)
      const next = result.ok ? result.work : result.current
      if (next) {
        queryClient.setQueryData<WorkListResponse | null>(sharedCrewKeys.work(sessionKey), prev => ({ ...(prev ?? {}), items: mergeWork(prev?.items ?? [], next) }))
      } else {
        void queryClient.invalidateQueries({ queryKey: sharedCrewKeys.work(sessionKey) })
      }
      if (!result.ok) setNotice(i18nT('sharedCrew.cancel_conflict'))
    } catch (e) {
      setError(e instanceof Error ? e.message : i18nT('sharedCrew.cancel_failed'))
    } finally {
      setBusy(null)
    }
  }
  return (
    <div className="flex flex-col gap-0.5 min-w-0" data-testid="work-status-list">
      {queuedCount > 1 && (
        <p className="text-[12px] text-muted">{i18nT('sharedCrew.queued_count', { n: fmtNumber(queuedCount) })}</p>
      )}
      <ul className="flex flex-col gap-0.5 min-w-0" aria-label={i18nT('sharedCrew.work_list')}>
        {shown.map(w => {
          const view = workView(w.state)
          const Icon = ICONS[view]
          const detail = view === 'queued' ? queuedReason(w.reason, capacity)
            : view === 'ended' || view === 'refused' ? endedReason(w.reason)
              : ''
          const spin = view === 'running' || view === 'checking'
          return (
            <li key={w.work_id} className="flex items-center gap-1.5 min-w-0 text-[12px] leading-5" data-testid={`work-${w.work_id}`} data-state={w.state}>
              <Icon size={12} className={`shrink-0 ${view === 'ended' || view === 'refused' ? 'text-danger' : view === 'done' ? 'text-ok' : 'text-muted'} ${spin ? 'animate-spin' : ''}`} aria-hidden />
              <span role="status" className="text-text shrink-0">{workLabel(view)}</span>
              {detail && <span className="text-muted truncate min-w-0" title={detail}>{'\u00A0·\u00A0'}{detail}</span>}
              {workCancellable(w) && (
                <Btn
                  className="ml-auto shrink-0 h-6 px-2 text-[12px]"
                  disabled={busy === w.work_id}
                  onClick={() => { void cancel(w) }}
                  aria-label={i18nT('sharedCrew.cancel_work_label', { state: workLabel(view) })}
                >
                  {i18nT('sharedCrew.cancel_work')}
                </Btn>
              )}
            </li>
          )
        })}
      </ul>
      {notice && <p className="text-[12px] text-muted" role="status">{notice}</p>}
      <ErrorNotice message={error} variant="inline" askAgent testId="work-cancel-error" />
    </div>
  )
}
