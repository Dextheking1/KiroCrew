/**
 * "Update your Slack app": what the Slack Code switches change in the Slack app
 * manifest, whether the installed app already carries it, and how to apply it.
 *
 * Slack has no safe way for us to push a manifest into an existing app (see the
 * RFC: `apps.manifest.update` replaces the WHOLE app configuration and needs a
 * workspace-wide configuration token), so the flow is copy → paste into the
 * app's App Manifest page → reinstall → Check again.
 */
import { useCallback, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, CheckCircle2, ExternalLink, RefreshCw } from 'lucide-react'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { api, type SlackConfigData, type SlackManifestChange, type SlackManifestStatus } from '../../api/client'
import { copyToClipboard } from '../../utils/clipboard'
import { i18nT } from '../../i18n/t'
import type { SlackCodeDraft } from './SlackCodePanel'

const SLACK_APPS = 'https://api.slack.com/apps'
const FEATURES = ['agent_sessions', 'code_channels'] as const

const STATUS_KEY: Record<string, [string, string]> = {
  up_to_date: ['pages.settings.slackPanel.slackCode.status_up_to_date', 'text-ok'],
  needs_update: ['pages.settings.slackPanel.slackCode.status_needs_update', 'text-warn'],
  feature_disabled: ['pages.settings.slackPanel.slackCode.status_feature_disabled', 'text-warn'],
  unknown: ['pages.settings.slackPanel.slackCode.status_unknown', 'text-muted'],
  not_configured: ['pages.settings.slackPanel.slackCode.status_not_configured', 'text-muted'],
  token_rejected: ['pages.settings.slackPanel.slackCode.status_token_rejected', 'text-danger'],
}

const STATUS_DETAIL_KEY: Record<string, string> = {
  up_to_date: 'pages.settings.slackPanel.slackCode.detail_up_to_date',
  needs_update: 'pages.settings.slackPanel.slackCode.detail_needs_update',
  feature_disabled: 'pages.settings.slackPanel.slackCode.detail_feature_disabled',
  unknown: 'pages.settings.slackPanel.slackCode.detail_unknown',
  not_configured: 'pages.settings.slackPanel.slackCode.detail_not_configured',
  token_rejected: 'pages.settings.slackPanel.slackCode.detail_token_rejected',
}

const FEATURE_LABEL_KEY: Record<string, string> = {
  agent_sessions: 'pages.settings.slackPanel.slackCode.agent_sessions',
  code_channels: 'pages.settings.slackPanel.slackCode.code_channels',
}

function StatusPill({ status }: { status: string }) {
  const [key, cls] = STATUS_KEY[status] ?? STATUS_KEY.unknown
  return (
    <span data-testid="slack-manifest-status" data-status={status}
      className={`inline-flex items-center gap-1.5 text-[12px] font-medium ${cls}`}>
      <span className="w-1.5 h-1.5 rounded-full bg-current" />
      {i18nT(key)}
    </span>
  )
}

/** A monospace chip; a scope the install lacks is marked. */
function Chip({ text, kind, missing }: { text: string; kind: 'add' | 'remove'; missing?: boolean }) {
  const tone = kind === 'remove'
    ? 'border-border text-muted line-through'
    : missing ? 'border-warn text-warn' : 'border-border text-text'
  return (
    <span className={`inline-flex items-center gap-1 rounded border bg-bg px-1.5 py-0.5 text-[11.5px] font-mono ${tone}`}>
      {kind === 'add' ? '+' : '−'} {text}
      {missing && <span className="font-sans text-[10.5px]">· {i18nT('pages.settings.slackPanel.slackCode.missing')}</span>}
    </span>
  )
}

/** The manifest lines one switch adds, grouped by kind. */
function ChangeLines({ change, missing }: { change: SlackManifestChange; missing: Set<string> }) {
  const rows: [string, string[]][] = [
    ['pages.settings.slackPanel.slackCode.lines_features', change.added_features.map(f => `features.${f}`)],
    ['pages.settings.slackPanel.slackCode.lines_scopes', change.added_scopes],
    ['pages.settings.slackPanel.slackCode.lines_events', change.added_events],
  ]
  return (
    <div className="flex flex-col gap-1.5">
      {rows.filter(([, items]) => items.length > 0).map(([labelKey, items]) => (
        <div key={labelKey} className="flex items-start gap-2">
          <span className="text-[11px] text-muted w-[64px] flex-none pt-0.5">{i18nT(labelKey)}</span>
          <div className="flex flex-wrap gap-1">
            {items.map(item => <Chip key={item} text={item} kind="add" missing={missing.has(item)} />)}
          </div>
        </div>
      ))}
    </div>
  )
}

export function SlackManifestUpdate({ data, draft, manifestText }: {
  data: SlackConfigData
  draft: SlackCodeDraft
  manifestText: string | undefined
}) {
  const statusQ = useQuery<SlackManifestStatus>({
    queryKey: ['slack-manifest-status'],
    queryFn: () => api.getSlackManifestStatus(false),
    retry: false,
    refetchOnWindowFocus: false,
  })
  const [checking, setChecking] = useState(false)
  const [copied, setCopied] = useState(false)
  const [copyFailed, setCopyFailed] = useState(false)

  const qc = useQueryClient()
  const [recheckFailed, setRecheckFailed] = useState(false)
  // "Check again" bypasses the server's short cache: it is what a user presses
  // right after reinstalling, when a cached "Needs update" would be wrong.
  const recheck = useCallback(async () => {
    setChecking(true)
    setRecheckFailed(false)
    try {
      qc.setQueryData(['slack-manifest-status'], await api.getSlackManifestStatus(true))
    } catch {
      setRecheckFailed(true)
    } finally {
      setChecking(false)
    }
  }, [qc])

  const copy = useCallback(async () => {
    if (!manifestText) return
    setCopyFailed(false)
    let ok = false
    try { ok = await copyToClipboard(manifestText) } catch { ok = false }
    if (!ok) { setCopyFailed(true); return }
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }, [manifestText])

  // Only a body that carries an install reading counts; anything else reads as
  // "could not check" rather than throwing.
  const status = statusQ.data?.install ? statusQ.data : undefined
  const savedOn = FEATURES.filter(f => (f === 'agent_sessions' ? data.agent_sessions : data.code_channels))
  const draftOn = FEATURES.filter(f => draft[f])
  const pendingOn = draftOn.filter(f => !savedOn.includes(f))
  const pendingOff = savedOn.filter(f => !draftOn.includes(f))
  const extra = status?.install.extra_scopes ?? []
  if (!status && !statusQ.isError) return null
  if (savedOn.length === 0 && pendingOn.length === 0 && extra.length === 0) return null

  const installStatus = status?.install.status ?? 'unknown'
  const missing = new Set(status?.install.missing_scopes ?? [])
  const manifestUrl = status?.app_manifest_url || SLACK_APPS
  const detailKey = STATUS_DETAIL_KEY[installStatus] ?? STATUS_DETAIL_KEY.unknown

  return (
    <div className="border-t border-border mt-4 pt-4 flex flex-col gap-3" data-testid="slack-manifest-update">
      <div className="flex items-center gap-3 flex-wrap">
        <span className="text-[13px] font-semibold text-text">{i18nT('pages.settings.slackPanel.slackCode.update_title')}</span>
        {savedOn.length > 0 || extra.length > 0 ? <StatusPill status={installStatus} /> : null}
      </div>
      {savedOn.length > 0 && (
        <p className="text-[12px] text-muted m-0">
          {i18nT(detailKey, { features: status?.install.disabled_features.join(', ') ?? '' })}
        </p>
      )}
      {(statusQ.isError || recheckFailed) && (
        <ErrorNotice variant="inline" message={i18nT('pages.settings.slackPanel.slackCode.status_load_failed')} />
      )}

      {(pendingOn.length > 0 || pendingOff.length > 0) && (
        <div className="rounded-md border border-warn/40 bg-bg px-3 py-2 text-[12px] text-warn" data-testid="slack-manifest-pending">
          {i18nT('pages.settings.slackPanel.slackCode.pending_save')}
        </div>
      )}

      {status && [...savedOn, ...pendingOn].filter(f => !!status.feature_changes?.[f]).map(f => (
        <div key={f} className="rounded-md border border-border bg-bg-elevated px-3 py-2.5 flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <span className="text-[12px] font-semibold text-text">{i18nT(FEATURE_LABEL_KEY[f])}</span>
            {pendingOn.includes(f) && <span className="text-[11px] text-muted">{i18nT('pages.settings.slackPanel.slackCode.after_save')}</span>}
          </div>
          <ChangeLines change={status.feature_changes[f]} missing={missing} />
        </div>
      ))}

      {extra.length > 0 && (
        <div className="rounded-md border border-border bg-bg-elevated px-3 py-2.5 flex flex-col gap-2">
          <span className="text-[12px] text-muted">{i18nT('pages.settings.slackPanel.slackCode.extra_scopes')}</span>
          <div className="flex flex-wrap gap-1">{extra.map(s => <Chip key={s} text={s} kind="remove" />)}</div>
        </div>
      )}

      {(installStatus !== 'up_to_date' || pendingOn.length > 0) && savedOn.length + pendingOn.length > 0 && (
        <ol className="m-0 pl-5 text-[12px] text-text flex flex-col gap-1">
          <li>{i18nT('pages.settings.slackPanel.slackCode.step_copy')}</li>
          <li>{i18nT('pages.settings.slackPanel.slackCode.step_paste')}</li>
          <li>{i18nT('pages.settings.slackPanel.slackCode.step_reinstall')}</li>
          <li>{i18nT('pages.settings.slackPanel.slackCode.step_restart')}</li>
          <li>{i18nT('pages.settings.slackPanel.slackCode.step_check')}</li>
        </ol>
      )}
      {installStatus === 'up_to_date' && savedOn.length > 0 && pendingOn.length === 0 && (
        <div className="flex items-center gap-1.5 text-[12px] text-ok">
          <CheckCircle2 size={13} /> {i18nT('pages.settings.slackPanel.slackCode.nothing_to_do')}
        </div>
      )}

      <div className="flex items-center gap-2 flex-wrap">
        <Btn onClick={() => { void copy() }} disabled={!manifestText}>
          {copied ? <><Check size={13} /> {i18nT('pages.settings.slackPanel.copied')}</> : i18nT('pages.settings.slackPanel.copy_manifest_yaml')}
        </Btn>
        <a href={manifestUrl} target="_blank" rel="noopener noreferrer"
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-medium border border-border text-text hover:bg-bg-hover transition-colors">
          {status?.app_manifest_url
            ? i18nT('pages.settings.slackPanel.slackCode.open_app_manifest')
            : i18nT('pages.settings.slackPanel.slackCode.open_slack_apps')}
          <ExternalLink size={13} />
        </a>
        <Btn onClick={() => { void recheck() }} disabled={checking} aria-busy={checking}>
          <RefreshCw size={13} className={checking ? 'animate-spin' : ''} /> {i18nT('pages.settings.slackPanel.slackCode.check_again')}
        </Btn>
      </div>
      {copyFailed && (
        <ErrorNotice variant="inline" message={i18nT('pages.settings.slackPanel.copy_manifest_failed')} onDismiss={() => setCopyFailed(false)} />
      )}
      <p className="text-[11.5px] text-muted m-0">{i18nT('pages.settings.slackPanel.slackCode.events_unverifiable')}</p>
    </div>
  )
}
