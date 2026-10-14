import { useId, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Trans, useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { Check, ChevronRight, CircleDashed, ExternalLink, Lock, Minus, Plus, Trash2 } from 'lucide-react'
import { AnimatePresence, motion } from 'framer-motion'
import type { ReactNode } from 'react'

import { api, type SessionEnvironmentsCheck } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { AGENT_SANDBOX_PATH } from '../overview/agentSandboxLink'
import { fmtDateTime, fmtNumber } from '../../i18n/format'
import { reportForError } from '../../utils/errorReport'
import { safeHttpUrl } from '../../lib/safeUrl'
import { BoxdIcon, CoderIcon, FlyIcon } from '../../components/BrandIcon'
import Clickable from '../../components/Clickable'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsField, SettingsInput, SettingsSelect, SettingsToggle } from '../../components/settings'
import { Btn, Input, PanelSectionHeader } from '../../components/ui'

interface EnvironmentProfile {
  id: string
  name: string
  template: string
  workdir: string
  /** False for a template whose stop destroys its files. Absent means true:
   * the card writes it only once toggled, so an older gateway never sees it. */
  compute_survives_stop?: boolean
}
/** How often the card re-reads the workspace list. */
const WORKSPACES_POLL_MS = 15_000
type ProfileTextKey = 'id' | 'name' | 'template' | 'workdir'
interface SpritesProfile {
  id: string
  name: string
  /** Name of the gateway vault secret holding the Sprites API token. */
  token_secret: string
  workdir: string
}
interface BoxdProfile {
  id: string
  name: string
  /** Snapshot (name or id) new machines start from; "" starts a fresh one. */
  snapshot: string
  /** 0 is the org's default size. A snapshot keeps its own size. */
  vcpu: number
  workdir: string
}
/** Mirrors BOXD_VCPU_CHOICES in config/session_environments.py. */
const BOXD_VCPU_CHOICES = [0, 1, 2, 4, 8]
/** Mirrors MAX_PROFILES in config/session_environments.py. */
const MAX_PROFILES = 20
interface EnvironmentSettings {
  coder_url: string
  profiles: EnvironmentProfile[]
  /** Fly.io Sprites profiles. Older gateways omit it. */
  sprites_profiles?: SpritesProfile[]
  /** boxd machine profiles. Older gateways omit it. */
  boxd_profiles?: BoxdProfile[]
  /** Profile id new chats start on; "" is this machine. Older gateways omit it. */
  default_profile?: string
  /** Stop every remote environment when the gateway shuts down. Older gateways omit it. */
  stop_on_shutdown?: boolean
}
// Symbols stay local to the editor: JSON configuration writes cannot include them.
const PROFILE_ROW_ID = Symbol('profile-row-id')
type ProfileDraft<T> = T & { readonly [PROFILE_ROW_ID]: string }
type EnvironmentDraft = Omit<EnvironmentSettings, 'profiles' | 'sprites_profiles' | 'boxd_profiles'> & {
  profiles: ProfileDraft<EnvironmentProfile>[]
  sprites_profiles?: ProfileDraft<SpritesProfile>[]
  boxd_profiles?: ProfileDraft<BoxdProfile>[]
}
function newProfileDraft<T>(profile: T): ProfileDraft<T> {
  return { ...profile, [PROFILE_ROW_ID]: crypto.randomUUID() }
}
/** Configuration responses carry fields, not editor identity. Match against
 * the submitted draft on save (ids are editable), or the last saved rows on
 * refetch, to keep each surviving row's local state out of the query cache. */
function environmentDraft(section: EnvironmentSettings | undefined, previous?: EnvironmentDraft): EnvironmentDraft | undefined {
  if (!section) return undefined
  const reconcile = <T extends { id: string }>(profiles: T[], rows?: ProfileDraft<T>[]): ProfileDraft<T>[] => profiles.map(profile => {
    const row = rows?.find(candidate => candidate.id === profile.id)
    return row ? { ...profile, [PROFILE_ROW_ID]: row[PROFILE_ROW_ID] } : newProfileDraft(profile)
  })
  return {
    ...section,
    profiles: reconcile(section.profiles, previous?.profiles),
    sprites_profiles: section.sprites_profiles && reconcile(section.sprites_profiles, previous?.sprites_profiles),
    boxd_profiles: section.boxd_profiles && reconcile(section.boxd_profiles, previous?.boxd_profiles),
  }
}
/** A removed profile cannot stay the default (the gateway refuses that save),
 * so removing the default falls back to this machine. */
function withoutDefault<T extends EnvironmentSettings>(next: T, removedId: string): T {
  return next.default_profile === removedId ? { ...next, default_profile: '' } : next
}
interface ConfigResponse {
  session_environments?: EnvironmentSettings
}

type ProviderId = 'coder' | 'sprites' | 'boxd'
const PROVIDERS: readonly ProviderId[] = ['coder', 'sprites', 'boxd']
const SETUP_DOCS: Record<ProviderId, string> = {
  coder: 'https://coder.com/docs/admin/setup',
  sprites: 'https://docs.sprites.dev',
  boxd: 'https://docs.boxd.sh/quickstart',
}
const emptyRows = (): Record<string, boolean> => ({})

/** One saved profile as a disclosure. The body stays mounted (inert while
 * collapsed, hidden once the collapse has finished, so it still animates
 * shut), so field-local drafts, including tokens, survive. */
function ProfileDisclosure({ title, subtitle, open, onToggle, children }: {
  title: string
  subtitle?: string
  open: boolean
  onToggle: () => void
  children: ReactNode
}) {
  const bodyId = useId()
  // Hidden only after the height animation lands; until then it is inert.
  const [settled, setSettled] = useState(!open)
  if (open && settled) setSettled(false)
  return (
    <div className="min-w-0 rounded-lg border border-border">
      <Clickable onClick={onToggle} aria-expanded={open} aria-controls={bodyId}
        className="group flex min-w-0 items-center gap-2.5 rounded-lg px-3 py-2.5 cursor-pointer hover:bg-bg-hover/40 transition-colors">
        <motion.span className="flex shrink-0 items-center text-muted group-hover:text-text"
          animate={{ rotate: open ? 90 : 0 }} transition={{ type: 'spring', damping: 22, stiffness: 300 }}>
          <ChevronRight className="lucide-inline" aria-hidden="true" />
        </motion.span>
        <span className="truncate text-[13px] font-semibold text-text">{title}</span>
        {subtitle && <span className="truncate text-[12px] text-muted">{subtitle}</span>}
      </Clickable>
      <AnimatePresence initial={false}>
        <motion.div key="body" id={bodyId} hidden={!open && settled} {...(!open ? { inert: '' } : {})}
          initial={false} animate={{ height: open ? 'auto' : 0, opacity: open ? 1 : 0 }}
          onAnimationComplete={() => { if (!open) setSettled(true) }}
          transition={{ type: 'spring', damping: 26, stiffness: 280, mass: 0.7 }} style={{ overflow: 'hidden' }}>
          <fieldset aria-label={title} className="min-w-0 space-y-3 px-3 pb-3">{children}</fieldset>
        </motion.div>
      </AnimatePresence>
    </div>
  )
}

/** How a check line's glyph reads. Only `ok` is coloured: a git host is
 * optional, so a signed-out or withheld one is never shown as a failure (the
 * blocking Coder and template failures render through ErrorNotice instead).
 * Each non-ok state has a glyph of its own; the text carries the meaning.
 * Signed-out is a dashed ring, a state rather than an arrow or button shape,
 * since the line itself is not something to click. */
type CheckLineStatus = 'ok' | 'signIn' | 'withheld' | 'unknown'
const CHECK_LINE_ICONS = { ok: Check, signIn: CircleDashed, withheld: Lock, unknown: Minus } as const

/** One check line: a status glyph and its text, announced as a list item. */
function CheckLine({ status, children }: { status: CheckLineStatus; children: ReactNode }) {
  const Icon = CHECK_LINE_ICONS[status]
  const tone = status === 'ok' ? 'text-ok' : 'text-muted'
  return (
    <li className="flex min-w-0 items-start gap-2 text-[13px] text-text">
      <Icon size={14} className={`mt-0.5 shrink-0 ${tone}`} aria-hidden="true" />
      <span className="min-w-0 break-words">{children}</span>
    </li>
  )
}

/** Display names for git hosts whose brand is a do-not-translate term; any
 * other host is shown as its host name. */
const GIT_HOST_NAMES: Record<string, string> = {
  'github.com': 'GitHub',
  'gitlab.com': 'GitLab',
  'bitbucket.org': 'Bitbucket',
  'dev.azure.com': 'Azure DevOps',
}

/** The gateway-machine command that signs each git host in. A host with no
 * CLI here (Bitbucket) falls back to git's credential helper and is reported
 * unchecked, never signed out. Commands, not prose: never translated. */
const GIT_SIGN_IN_COMMANDS: Record<string, string> = {
  'github.com': 'gh auth login',
  'gitlab.com': 'glab auth login',
  'dev.azure.com': 'az login',
}

/** One provider row of the readiness check: ok, or the failure as a notice. */
function ProviderCheckLine({ row, name, okText }: {
  row: { id: string; ok: boolean; error?: string }
  name: string
  okText: string
}) {
  const { t } = useTranslation()
  return row.ok ? (
    <CheckLine status="ok">{okText}</CheckLine>
  ) : (
    <li><ErrorNotice variant="inline" message={t('sessionEnvironments.checkTemplateFailed', { name, error: row.error ?? '' })} askAgent /></li>
  )
}

/** What the readiness check found, in the order a person fixes things.
 *
 * Follows the provider tab, like the Manage step: a Sprites setup never reports
 * Coder rows even when a Coder URL is still saved. Git rows apply to every
 * provider. */
function CheckResults({ result, provider, profiles, spritesProfiles, boxdProfiles }: {
  result: SessionEnvironmentsCheck
  provider: ProviderId
  profiles: EnvironmentProfile[]
  spritesProfiles: SpritesProfile[]
  boxdProfiles: BoxdProfile[]
}) {
  const { t } = useTranslation()
  const names = new Map(profiles.map(p => [p.id, p]))
  // A gateway that predates the git rows sends no `git` at all.
  const git = Object.entries(result.git ?? {})
  // The sandbox tier lives on Developer → Config; link there only when it is
  // what withheld a login, since otherwise nothing here is the sandbox's doing.
  const withheld = git.some(([, status]) => status === 'withheld')
  // 'strict' withholds every host alike, so one line says it once instead of
  // repeating the same sentence per host.
  const allWithheld = withheld && git.every(([, status]) => status === 'withheld')
  const spritesNames = new Map(spritesProfiles.map(p => [p.id, p.name || p.id]))
  const boxdNames = new Map(boxdProfiles.map(p => [p.id, p.name || p.id]))
  return (
    <>
    <ul aria-label={t('sessionEnvironments.checkResults')} className="space-y-1.5">
      {provider === 'coder' && (result.coder.ok ? (
        <CheckLine status="ok">{t('sessionEnvironments.checkCoderOk', { owner: result.coder.owner ?? '' })}</CheckLine>
      ) : (
        <li>
          <ErrorNotice variant="inline" message={t('sessionEnvironments.checkCoderFailed', { error: result.coder.error ?? '' })} askAgent />
          <p className="pl-5 text-[13px] text-text">
            <Trans i18nKey="sessionEnvironments.checkCoderLogin" components={{ code: <code className="font-mono select-all" /> }} />
          </p>
        </li>
      ))}
      {/* No sign-in, no template check, whatever a row says. */}
      {provider === 'coder' && !result.coder.ok && profiles.map(p => (
        <li key={p.id} className="flex min-w-0 items-start gap-2 text-[13px] text-muted">
          <Minus size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
          {t('sessionEnvironments.checkTemplateNotChecked', { name: p.name || p.id })}
        </li>
      ))}
      {provider === 'coder' && result.coder.ok && result.profiles.map(row => {
        const profile = names.get(row.id)
        const name = profile?.name || row.id
        return row.ok ? (
          <CheckLine key={row.id} status="ok">
            {t('sessionEnvironments.checkTemplateOk', { name, template: profile?.template ?? '' })}
          </CheckLine>
        ) : (
          <li key={row.id}><ErrorNotice variant="inline" message={t('sessionEnvironments.checkTemplateFailed', { name, error: row.error ?? '' })} askAgent /></li>
        )
      })}
      {provider === 'sprites' && (result.sprites ?? []).map(row => {
        const name = spritesNames.get(row.id) ?? row.id
        return <ProviderCheckLine key={`sprites:${row.id}`} row={row} name={name}
          okText={t('sessionEnvironments.checkSpritesTokenOk', { name })} />
      })}
      {provider === 'boxd' && (result.boxd ?? []).map(row => {
        const name = boxdNames.get(row.id) ?? row.id
        return <ProviderCheckLine key={`boxd:${row.id}`} row={row} name={name}
          okText={t('sessionEnvironments.checkBoxdOk', { name })} />
      })}
      {allWithheld && <CheckLine status="withheld">{t('sessionEnvironments.checkGitWithheldAll')}</CheckLine>}
      {!allWithheld && git.map(([host, status]) => {
        const name = GIT_HOST_NAMES[host] ?? host
        // Signed out names a sign-in command; a host with none reads as unchecked.
        const signedOut = status === 'not_signed_in' && !!GIT_SIGN_IN_COMMANDS[host]
        return (
          <CheckLine key={host} status={status === 'signed_in' ? 'ok' : signedOut ? 'signIn' : status === 'withheld' ? 'withheld' : 'unknown'}>
            {status === 'signed_in'
              ? t('sessionEnvironments.checkGitSignedIn', { name })
              : signedOut
                ? (
                  <Trans
                    i18nKey="sessionEnvironments.checkGitSignedOut"
                    values={{ name, command: GIT_SIGN_IN_COMMANDS[host] }}
                    components={{ code: <code className="rounded bg-bg-elevated px-1 font-mono text-[12px] text-text" translate="no" /> }}
                  />
                )
                : status === 'withheld'
                  ? t('sessionEnvironments.checkGitWithheld', { name })
                  : t('sessionEnvironments.checkGitUnchecked', { name })}
          </CheckLine>
        )
      })}
    </ul>
    {withheld && (
      <p className="text-[13px]">
        <Link to={AGENT_SANDBOX_PATH} className="inline-flex min-w-0 items-center gap-0.5 rounded text-accent hover:underline focus-ring">
          <span className="min-w-0">{t('sessionEnvironments.checkGitSandboxLink')}</span>
          <ChevronRight size={13} aria-hidden="true" className="shrink-0" />
        </Link>
      </p>
    )}
    </>
  )
}

/** Due today or earlier (an unreadable date is not). */
function dueByToday(iso: string): boolean {
  const endOfToday = new Date()
  endOfToday.setHours(23, 59, 59, 999)
  return new Date(iso).getTime() <= endOfToday.getTime()
}

/** A Sprites API token field. The token is typed here and stored by the
 * section's own Save settings, in the gateway vault under the profile's
 * secret name, before the configuration is written: one Save for the whole
 * profile. The token never enters config or the draft, and is never read back:
 * the vault is write-only to the dashboard, so the field can only say whether
 * a token is saved, never show one. */
function SpritesTokenField({ secretName, profileSaved, value, onChange, error }: {
  secretName: string
  profileSaved: boolean
  value: string
  onChange: (value: string) => void
  /** Why the last Save could not store this token, or null. */
  error: string | null
}) {
  const { t } = useTranslation()
  const inputId = useId()
  const secrets = useQuery({ queryKey: ['secrets'], queryFn: () => api.secretsList(), retry: false })
  const stored = !!secretName && (secrets.data?.names ?? []).includes(secretName)
  return (
    <div className="space-y-1.5">
      <label htmlFor={inputId} className="block text-[13px] font-medium text-text">{t('sessionEnvironments.apiToken')}</label>
      <Input id={inputId} type="password" autoComplete="new-password" spellCheck={false}
        className="min-w-0 w-full font-mono placeholder:font-sans" value={value} onChange={e => onChange(e.target.value)}
        placeholder={stored ? t('sessionEnvironments.apiTokenSavedPlaceholder')
          : t('sessionEnvironments.apiTokenPlaceholder')} />
      {/* Hidden while a save failure shows, so the two never contradict each other. */}
      {secrets.isSuccess && error === null && <p role="status" className="text-[13px] text-muted">
        {stored ? t('sessionEnvironments.apiTokenStored', { name: secretName }) : t('sessionEnvironments.apiTokenHint', { name: secretName })}
      </p>}
      {/* No hand-off: a retry preserves both the typed token and profile draft. */}
      <ErrorNotice message={secrets.isError ? t('sessionEnvironments.apiTokenListError') : null}
        footer={<Btn type="button" onClick={() => void secrets.refetch()}>{t('sessionEnvironments.retry')}</Btn>} />
      {/* An unsaved profile is not in the composer yet: say so. The card's own
        * Save bar stays in view and stores the profile and its token together. */}
      {!profileSaved && (
        <p data-testid="sprites-profile-unsaved" className="text-[13px] text-warn">{t('sessionEnvironments.spritesProfileUnsaved')}</p>
      )}
      {/* No hand-off: navigating would discard the typed token. When the
        * server gave a reason (retrying would only meet it again), the fix on
        * this card comes first, a different secret name, then that reason;
        * else the retry step. */}
      <ErrorNotice message={error === null ? null
        : error ? t('sessionEnvironments.apiTokenRename')
          : t('sessionEnvironments.apiTokenRetry', { name: secretName })}
        title={error === null ? undefined : t('sessionEnvironments.apiTokenError', { name: secretName })}
        footer={error ? <span data-testid="sprites-token-reason">{error}</span> : undefined}
        messagePlacement="below" />
    </div>
  )
}

/** A token the vault refused during Save, named by its editor row. Only the
 * server's own words are kept as the reason: a transport failure ("Failed to
 * fetch") is no use to a reader, who gets the next step instead. */
class TokenSaveError extends Error {
  /** `movedTo`: the profile's secret name changed after the token was typed
   * (another window, or the agent), so nothing was written; the next Save,
   * after this notice, stores it under the name now shown. */
  constructor(readonly rowId: string, cause: unknown, readonly movedTo?: string) {
    super(cause instanceof ApiError ? cause.message : typeof cause === 'string' ? cause : '')
  }
}

/** A typed token and the secret name it was typed for. */
type PendingToken = { token: string; secret: string }

/** The workspaces Kiro Crew owns for one provider, from the gateway's registries
 * (no remote call). */
function WorkspaceList({ provider }: { provider: ProviderId }) {
  const { t } = useTranslation()
  const query = useQuery({
    queryKey: ['sessionEnvironmentsWorkspaces'],
    queryFn: () => api.sessionEnvironmentsWorkspaces(),
    refetchInterval: WORKSPACES_POLL_MS,
    retry: false,
  })
  if (query.isPending) return null
  if (query.isError) return <ErrorNotice askAgent message={t('sessionEnvironments.workspacesError')} />
  // A row without a provider comes from an older gateway, which only had Coder.
  const rows = query.data.workspaces.filter(row => (row.provider ?? 'coder') === provider)
  if (!rows.length) return <p className="text-[13px] text-muted">{t('sessionEnvironments.workspacesNone')}</p>
  return (
    <ul aria-label={t('sessionEnvironments.workspaces')} className="divide-y divide-border rounded-lg border border-border">
      {rows.map(row => {
        // Only a live chat has a slot to open; an archived row says what to do.
        const inUse = row.state !== 'deleting' && !row.deletion_due_at
        const name = 'truncate font-mono text-[12px]'
        return (
        <li key={row.workspace} className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-0.5 px-3 py-2 text-[13px]">
          {inUse && row.slot
            ? <Link to={`/chat?sid=${encodeURIComponent(row.slot)}`} className={`${name} text-accent hover:underline`}>{row.workspace}</Link>
            : <span className={`${name} text-text`}>{row.workspace}</span>}
          {inUse ? (
            <span className="text-muted">{t('sessionEnvironments.workspaceSession')}</span>
          ) : (
            <span className="ml-auto text-[12px] text-muted">
              {row.state === 'deleting'
                ? t('sessionEnvironments.workspaceDeleting')
                : dueByToday(row.deletion_due_at)
                  ? t('sessionEnvironments.workspaceDeletesSoon')
                  : t('sessionEnvironments.workspaceDeletes', { when: fmtDateTime(row.deletion_due_at) })}
            </span>
          )}
        </li>
        )
      })}
    </ul>
  )
}

/** Saving starts no compute. The preview switch is indexed in
 * FeaturePreviewsSection; the URL setting is searchable only after opting in. */
export function SessionEnvironmentsCard({ enabled }: { enabled: boolean }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const controlId = useId()
  const query = useQuery<ConfigResponse>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    enabled,
  })
  const section = query.data?.session_environments
  const [savedRows, setSavedRows] = useState<{
    section: EnvironmentSettings | undefined
    value: EnvironmentDraft | undefined
  }>(() => ({ section, value: environmentDraft(section) }))
  // Reconcile before committing children, so a settings refresh cannot remount
  // a surviving row and discard its field-local token.
  if (savedRows.section !== section) {
    setSavedRows({ section, value: environmentDraft(section, savedRows.value) })
  }
  const savedConfig = savedRows.value
  // Null follows fresh server data. Once edited, background refetches never
  // replace the draft. Saving freezes controls until the server responds.
  const [draft, setDraft] = useState<EnvironmentDraft | null>(null)
  const [saved, setSaved] = useState(false)
  // Removal has no confirm: the draft keeps it undoable until Save.
  const [removed, setRemoved] = useState(false)
  // Null until the person picks a tab; until then the tab follows what is saved
  // (see `provider` below), so a Sprites-only setup does not open on Coder.
  const [providerChoice, setProvider] = useState<ProviderId | null>(null)
  // Disclosure and token state belong to an immutable editor row, never its
  // position or editable profile id. Removing a row retires its local state.
  const [openRows, setOpenRows] = useState(emptyRows)
  // Typed Sprites tokens and the reason one was refused, by editor row. Saved
  // to the vault by Save, before the configuration; never part of the draft.
  // Each keeps the secret name it was typed for, so a refresh that rebinds the
  // profile can never send it over another key's credential.
  const [tokens, setTokens] = useState<Record<string, PendingToken>>({})
  const [tokenErrors, setTokenErrors] = useState<Record<string, string>>({})
  const toggleRow = (rowId: string) => setOpenRows(rows => ({ ...rows, [rowId]: !rows[rowId] }))
  const addRow = (rowId: string) => setOpenRows(rows => ({ ...rows, [rowId]: true }))
  const removeRow = (rowId: string) => {
    const drop = <T,>(rows: Record<string, T>) => {
      const next = { ...rows }
      delete next[rowId]
      return next
    }
    setOpenRows(drop)
    setTokens(drop)
    setTokenErrors(drop)
  }
  const mutation = useMutation({
    mutationFn: async ({ value, typed }: { value: EnvironmentDraft | null; typed: [string, PendingToken][] }) => {
      const rows = (value ?? savedRows.value)?.sprites_profiles ?? []
      for (const [rowId, { token, secret }] of typed) {
        const profile = rows.find(row => row[PROFILE_ROW_ID] === rowId)
        if (!profile?.token_secret) continue
        if (profile.token_secret !== secret) {
          throw new TokenSaveError(rowId, t('sessionEnvironments.apiTokenMoved', { name: profile.token_secret }), profile.token_secret)
        }
        try {
          await api.secretsSave(secret, token)
        } catch (error) {
          throw new TokenSaveError(rowId, error)
        }
      }
      return value ? await api.patchConfig('session_environments', value) as ConfigResponse : null
    },
    onMutate: () => qc.cancelQueries({ queryKey: ['kirocrewConfig'] }),
    onError: error => {
      if (!(error instanceof TokenSaveError)) return
      setTokenErrors({ [error.rowId]: error.message })
      // The reason lives on the row's own field: bring that field into view,
      // even when the row was collapsed or another provider's tab was open.
      setProvider('sprites')
      addRow(error.rowId)
      const movedTo = error.movedTo
      if (movedTo !== undefined) {
        setTokens(current => current[error.rowId]
          ? { ...current, [error.rowId]: { ...current[error.rowId], secret: movedTo } }
          : current)
      }
    },
    onSuccess: (response, { value: submitted, typed }) => {
      // Clear only what was saved: a field changed since then keeps its text.
      setTokens(current => {
        const next = { ...current }
        for (const [rowId, { token }] of typed) if (next[rowId]?.token === token) delete next[rowId]
        return next
      })
      setTokenErrors({})
      if (typed.length) void qc.invalidateQueries({ queryKey: ['secrets'] })
      if (!response || !submitted) {
        setSaved(true)
        return
      }
      const cached = qc.setQueryData<ConfigResponse>(['kirocrewConfig'], response)
      setSavedRows({
        section: cached?.session_environments,
        value: environmentDraft(cached?.session_environments, submitted),
      })
      setDraft(null)
      setOpenRows(emptyRows())
      setSaved(true)
      setRemoved(false)
    },
  })
  const config = draft ?? savedConfig
  // Typed tokens the next Save stores. They count as unsaved edits like the draft.
  const typedTokens = Object.entries(tokens).filter(([, pending]) => pending.token)
  const dirty = !!draft || typedTokens.length > 0
  // The check reads the SAVED configuration on the gateway, so it is offered
  // only with no unsaved draft, and its answer is dropped when one appears.
  const savedCoderUrl = safeHttpUrl(query.data?.session_environments?.coder_url ?? '')
  const savedProfiles = query.data?.session_environments?.profiles ?? []
  const savedSpritesProfiles = query.data?.session_environments?.sprites_profiles ?? []
  const savedBoxdProfiles = query.data?.session_environments?.boxd_profiles ?? []
  // The providers are separate choices, so open on the one in use. Coder wins a
  // tie because it is the only one with deployment-level settings.
  const provider: ProviderId = providerChoice
    ?? (savedCoderUrl || savedProfiles.length > 0 ? 'coder'
      : savedSpritesProfiles.length > 0 ? 'sprites'
        : savedBoxdProfiles.length > 0 ? 'boxd'
          : 'coder')
  // Whether the open tab has anything saved for the check and the Manage list.
  const providerSaved = provider === 'coder' ? !!savedCoderUrl
    : provider === 'sprites' ? savedSpritesProfiles.length > 0
      : savedBoxdProfiles.length > 0
  const checkMutation = useMutation({ mutationFn: () => api.sessionEnvironmentsCheck() })
  const change = (next: EnvironmentDraft) => {
    setDraft(next)
    setSaved(false)
    mutation.reset()
  }
  // The secret name is bound when typing begins; only the person's own edit of
  // it (rebindToken) or an acknowledged refusal moves it.
  const changeToken = (rowId: string, token: string, secret: string) => {
    setTokens(current => {
      const next = { ...current }
      if (token) next[rowId] = { token, secret: current[rowId]?.secret ?? secret }
      else delete next[rowId]
      return next
    })
    setTokenErrors(current => {
      const next = { ...current }
      delete next[rowId]
      return next
    })
    setSaved(false)
  }
  const rebindToken = (rowId: string, secret: string) => setTokens(current => current[rowId]
    ? { ...current, [rowId]: { ...current[rowId], secret } }
    : current)

  if (!enabled && !draft) return null
  if (query.isPending) return <p role="status" className="text-[13px] text-muted">{t('sessionEnvironments.loading')}</p>
  if (query.isError && !query.data) return (
    <ErrorNotice message={t('sessionEnvironments.loadError')} askAgent footer={
      <Btn onClick={() => void query.refetch()}>{t('sessionEnvironments.retry')}</Btn>
    } />
  )
  // An older gateway has no section: never show a form that cannot save.
  if (!config) return <ErrorNotice message={t('sessionEnvironments.unavailable')} askAgent />

  const profileFields: { key: ProfileTextKey; label: string; description?: string }[] = [
    { key: 'id', label: t('sessionEnvironments.id'), description: t('sessionEnvironments.idHint') },
    { key: 'name', label: t('sessionEnvironments.name'), description: t('sessionEnvironments.nameHint') },
    { key: 'template', label: t('sessionEnvironments.template'), description: t('sessionEnvironments.templateHint') },
    { key: 'workdir', label: t('sessionEnvironments.workdir'), description: t('sessionEnvironments.workdirHint') },
  ]
  // Per-profile like the fields above, so not a searchable settings row.
  const survivesStopLabel = t('sessionEnvironments.computeSurvivesStop')
  const updateProfile = (index: number, field: ProfileTextKey, value: string) => {
    const old = config.profiles[index]?.id
    const current = config.default_profile
    // Renaming the default's id carries the default along: a dangling one is
    // refused on save. A rename that passes through an empty id keeps the old
    // default until the row has an id again, so retyping it does not drop it.
    const orphaned = !!current && !config.profiles.some(profile => profile.id === current)
    const follows = field === 'id' && !!value && (old ? old === current : orphaned)
    change({
      ...config,
      ...(follows ? { default_profile: value } : {}),
      profiles: config.profiles.map((profile, i) => i === index ? { ...profile, [field]: value } : profile),
    })
  }
  const addProfile = () => {
    let n = 1
    while (config.profiles.some(profile => profile.id === `profile-${n}`)) n++
    const row = newProfileDraft({ id: `profile-${n}`, name: '', template: '', workdir: '' })
    addRow(row[PROFILE_ROW_ID])
    change({ ...config, profiles: [...config.profiles, row] })
  }

  const spritesProfiles = config.sprites_profiles ?? []
  const spritesFields: { key: 'id' | 'name' | 'workdir'; label: string }[] = [
    { key: 'id', label: t('sessionEnvironments.id') },
    { key: 'name', label: t('sessionEnvironments.name') },
    { key: 'workdir', label: t('sessionEnvironments.workdir') },
  ]
  const updateSprites = (index: number, updates: Partial<SpritesProfile>) => {
    change({ ...config, sprites_profiles: spritesProfiles.map((profile, i) => i === index ? { ...profile, ...updates } : profile) })
  }
  const addSprites = () => {
    let n = 1
    while (spritesProfiles.some(profile => profile.id === `sprites-${n}`)) n++
    // A name is required to save; start from one so a new profile saves as-is.
    const name = t('sessionEnvironments.spritesNewProfile', { n: fmtNumber(n) })
    const row = newProfileDraft({
      id: `sprites-${n}`, name, token_secret: 'SPRITES_TOKEN', workdir: '/home/sprite/workspace',
    })
    addRow(row[PROFILE_ROW_ID])
    change({ ...config, sprites_profiles: [...spritesProfiles, row] })
  }
  const boxdProfiles = config.boxd_profiles ?? []
  const boxdFields: { key: 'id' | 'name' | 'snapshot' | 'workdir'; label: string }[] = [
    { key: 'id', label: t('sessionEnvironments.id') },
    { key: 'name', label: t('sessionEnvironments.name') },
    { key: 'snapshot', label: t('sessionEnvironments.boxdSnapshot') },
    { key: 'workdir', label: t('sessionEnvironments.workdir') },
  ]
  const updateBoxd = (index: number, updates: Partial<BoxdProfile>) => {
    // A snapshot keeps its own size (the gateway refuses both), so naming one
    // returns the size to the default.
    const next = updates.snapshot ? { ...updates, vcpu: 0 } : updates
    change({ ...config, boxd_profiles: boxdProfiles.map((profile, i) => i === index ? { ...profile, ...next } : profile) })
  }
  const addBoxd = () => {
    let n = 1
    while (boxdProfiles.some(profile => profile.id === `boxd-${n}`)) n++
    const name = t('sessionEnvironments.boxdNewProfile', { n: fmtNumber(n) })
    const row = newProfileDraft({
      id: `boxd-${n}`, name, snapshot: '', vcpu: 0, workdir: '/home/boxd/workspace',
    })
    addRow(row[PROFILE_ROW_ID])
    change({ ...config, boxd_profiles: [...boxdProfiles, row] })
  }
  const providerOptions: Record<ProviderId, { label: string; icon: ReactNode }> = {
    coder: { label: t('sessionEnvironments.coder'), icon: <CoderIcon size={14} /> },
    sprites: { label: t('sessionEnvironments.sprites'), icon: <FlyIcon size={18} /> },
    boxd: { label: t('sessionEnvironments.boxd'), icon: <BoxdIcon size={16} /> },
  }

  return (
    <form className="min-w-0 space-y-3 pt-3" onSubmit={event => {
      event.preventDefault()
      if (dirty && !mutation.isPending) mutation.mutate({ value: draft, typed: typedTokens })
    }}>
      {/* No hand-off: navigating would discard the unsaved environment draft. */}
      <ErrorNotice message={query.isError ? t('sessionEnvironments.loadError') : null} footer={
        <Btn type="button" onClick={() => void query.refetch()}>{t('sessionEnvironments.retry')}</Btn>
      } />
      <fieldset disabled={mutation.isPending} className="min-w-0 pt-3">
        <legend className="mb-2 text-[13px] font-semibold text-text">{t('sessionEnvironments.provider')}</legend>
        <p className="mb-2 text-[12px] text-muted">{t('sessionEnvironments.providerHint')}</p>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
          {PROVIDERS.map(id => (
            <label key={id} className={`flex items-center gap-3 rounded-lg border p-3 cursor-pointer text-[13px] ${provider === id ? 'border-accent bg-accent-subtle text-text' : 'border-border bg-bg text-text'}`}>
              <input type="radio" name={`${controlId}-provider`} value={id} checked={provider === id}
                aria-label={providerOptions[id].label} onChange={() => setProvider(id)} className="accent-[var(--accent)]" />
              {providerOptions[id].icon}
              {providerOptions[id].label}
            </label>
          ))}
        </div>
      </fieldset>
      <section aria-label={t('sessionEnvironments.configureStep')} className="space-y-3 pt-3">
      <PanelSectionHeader label={t('sessionEnvironments.configureStep')} />
      <details className="rounded-lg border border-border p-3">
        <summary className="cursor-pointer text-[13px] font-semibold text-text">{t('sessionEnvironments.setup')}</summary>
        <ol className="list-decimal pl-5 space-y-2 pt-3 text-[13px] text-muted">
          {provider === 'coder' ? <>
            <li>{t('sessionEnvironments.coderSetup1')}</li>
            <li>{t('sessionEnvironments.coderSetup2')}</li>
            <li>{t('sessionEnvironments.coderSetup3')}</li>
          </> : provider === 'sprites' ? <>
            <li>{t('sessionEnvironments.spritesSetup1')}</li>
            <li>{t('sessionEnvironments.spritesSetup2')}</li>
            <li>{t('sessionEnvironments.spritesSetup3')}</li>
          </> : <>
            <li>{t('sessionEnvironments.boxdSetup1')}</li>
            <li>{t('sessionEnvironments.boxdSetup2')}</li>
            <li>{t('sessionEnvironments.boxdSetup3')}</li>
          </>}
        </ol>
        <a className="inline-block pt-3 text-[13px] text-accent underline" target="_blank" rel="noopener noreferrer"
          href={SETUP_DOCS[provider]}>
          {t('sessionEnvironments.setupDocs')}
        </a>
      </details>
      <fieldset disabled={mutation.isPending} className="min-w-0 space-y-3">
        <div hidden={provider !== 'coder'} className="space-y-3">
        <SettingsInput
          configKey="session_environments.coder_url"
          label={t('sessionEnvironments.url')}
          description={t('sessionEnvironments.urlHint')}
          value={config.coder_url}
          onChange={value => change({ ...config, coder_url: value })}
        />
        <SettingsField configKey="session_environments.profiles" label={t('sessionEnvironments.profiles')}
          description={t('sessionEnvironments.profilesCoderHint')}>
          <div className="min-w-0 space-y-3">
            {config.profiles.map((profile, index) => (
              <ProfileDisclosure key={profile[PROFILE_ROW_ID]} title={profile.name || profile.id} subtitle={profile.template}
                open={!!openRows[profile[PROFILE_ROW_ID]]} onToggle={() => toggleRow(profile[PROFILE_ROW_ID])}>
                {profileFields.map(field => (
                  <SettingsInput key={field.key} label={field.label} description={field.description} value={profile[field.key]} onChange={value => updateProfile(index, field.key, value)} />
                ))}
                <SettingsToggle anchor={false} label={survivesStopLabel}
                  description={t('sessionEnvironments.computeSurvivesStopHint')}
                  checked={profile.compute_survives_stop !== false}
                  onChange={on => change({ ...config, profiles: config.profiles.map((p, i) => i === index ? { ...p, compute_survives_stop: on } : p) })} />
                <Btn type="button" onClick={() => {
                  removeRow(profile[PROFILE_ROW_ID])
                  setRemoved(true)
                  change(withoutDefault({ ...config, profiles: config.profiles.filter((_, i) => i !== index) }, profile.id))
                }}>
                  <Trash2 size={14} aria-hidden="true" />{t('sessionEnvironments.remove')}
                </Btn>
              </ProfileDisclosure>
            ))}
            <Btn type="button" disabled={config.profiles.length >= MAX_PROFILES} onClick={addProfile}>
              <Plus size={14} aria-hidden="true" />{t('sessionEnvironments.add')}
            </Btn>
          </div>
        </SettingsField>
        </div>
        <div hidden={provider !== 'sprites'} className="space-y-3">
          {/* Mounted only on its own tab, so an older gateway shows one notice. */}
          {config.sprites_profiles === undefined ? (provider === 'sprites' && <ErrorNotice message={t('sessionEnvironments.unavailable')} askAgent />) : (
            <SettingsField configKey="session_environments.sprites_profiles" label={t('sessionEnvironments.profiles')}
              description={t('sessionEnvironments.profilesSpritesHint')}>
              <div className="min-w-0 space-y-3">
                {spritesProfiles.map((profile, index) => (
                  <ProfileDisclosure key={profile[PROFILE_ROW_ID]} title={profile.name || profile.id}
                    open={!!openRows[profile[PROFILE_ROW_ID]]} onToggle={() => toggleRow(profile[PROFILE_ROW_ID])}>
                    {spritesFields.map(field => <SettingsInput key={field.key} label={field.label} value={profile[field.key]} onChange={value => updateSprites(index, { [field.key]: value })} />)}
                    <SpritesTokenField secretName={profile.token_secret}
                      profileSaved={savedSpritesProfiles.some(saved => saved.id === profile.id)}
                      value={tokens[profile[PROFILE_ROW_ID]]?.token ?? ''}
                      onChange={token => changeToken(profile[PROFILE_ROW_ID], token, profile.token_secret)}
                      error={tokenErrors[profile[PROFILE_ROW_ID]] ?? null} />
                    {/* Where the token is kept: rarely changed, so closed by default. */}
                    <details className="rounded-lg border border-border px-3 py-2">
                      <summary className="cursor-pointer text-[13px] text-muted">{t('sessionEnvironments.tokenSecretAdvanced')}</summary>
                      <div className="pt-2">
                        <SettingsInput label={t('sessionEnvironments.tokenSecret')} description={t('sessionEnvironments.tokenSecretHint')} value={profile.token_secret}
                          onChange={value => {
                            rebindToken(profile[PROFILE_ROW_ID], value)
                            updateSprites(index, { token_secret: value })
                          }} />
                      </div>
                    </details>
                    <Btn type="button" onClick={() => {
                      removeRow(profile[PROFILE_ROW_ID])
                      setRemoved(true)
                      const remaining = spritesProfiles.filter((_, i) => i !== index)
                      change(withoutDefault({ ...config, sprites_profiles: remaining }, profile.id))
                    }}>
                      <Trash2 size={14} aria-hidden="true" />{t('sessionEnvironments.remove')}
                    </Btn>
                  </ProfileDisclosure>
                ))}
                <Btn type="button" disabled={spritesProfiles.length >= MAX_PROFILES} onClick={addSprites}>
                  <Plus size={14} aria-hidden="true" />{t('sessionEnvironments.add')}
                </Btn>
              </div>
            </SettingsField>
          )}
        </div>
        <div hidden={provider !== 'boxd'} className="space-y-3">
          {config.boxd_profiles === undefined ? (provider === 'boxd' && <ErrorNotice message={t('sessionEnvironments.unavailable')} askAgent />) : (
            <SettingsField configKey="session_environments.boxd_profiles" label={t('sessionEnvironments.profiles')}
              description={t('sessionEnvironments.profilesBoxdHint')}>
              <div className="min-w-0 space-y-3">
                {boxdProfiles.map((profile, index) => (
                  <ProfileDisclosure key={profile[PROFILE_ROW_ID]} title={profile.name || profile.id} subtitle={profile.snapshot ? t('sessionEnvironments.profileSubtitleSnapshot', { name: profile.snapshot }) : undefined}
                    open={!!openRows[profile[PROFILE_ROW_ID]]} onToggle={() => toggleRow(profile[PROFILE_ROW_ID])}>
                    {boxdFields.map(field => <SettingsInput key={field.key} label={field.label} value={profile[field.key]} onChange={value => updateBoxd(index, { [field.key]: value })} />)}
                    {/* A snapshot keeps its own size, so the size applies only without one. */}
                    <SettingsSelect label={t('sessionEnvironments.boxdSize')} description={t('sessionEnvironments.boxdSizeHint')}
                      value={String(profile.snapshot ? 0 : profile.vcpu)} disabled={!!profile.snapshot}
                      options={BOXD_VCPU_CHOICES.map(String)}
                      optionLabels={BOXD_VCPU_CHOICES.map(vcpu => vcpu === 0
                        ? t(profile.snapshot ? 'sessionEnvironments.boxdSizeFromSnapshot' : 'sessionEnvironments.boxdSizeDefault')
                        : t('sessionEnvironments.boxdSizeVcpu', { vcpu: fmtNumber(vcpu), memory: fmtNumber(vcpu * 4) }))}
                      onChange={value => updateBoxd(index, { vcpu: Number(value) })} />
                    <Btn type="button" onClick={() => {
                      removeRow(profile[PROFILE_ROW_ID])
                      setRemoved(true)
                      change(withoutDefault({ ...config, boxd_profiles: boxdProfiles.filter((_, i) => i !== index) }, profile.id))
                    }}>
                      <Trash2 size={14} aria-hidden="true" />{t('sessionEnvironments.remove')}
                    </Btn>
                  </ProfileDisclosure>
                ))}
                <Btn type="button" disabled={boxdProfiles.length >= MAX_PROFILES} onClick={addBoxd}>
                  <Plus size={14} aria-hidden="true" />{t('sessionEnvironments.add')}
                </Btn>
              </div>
            </SettingsField>
          )}
        </div>
      </fieldset>
      </section>
      <section aria-label={t('sessionEnvironments.verifyStep')} className="space-y-3 pt-3">
        <PanelSectionHeader label={t('sessionEnvironments.verifyStep')} />
        <p id={`${controlId}-verification`} className="text-[13px] text-muted">
          {dirty ? t('sessionEnvironments.checkSaveFirst')
            : !providerSaved ? t('sessionEnvironments.checkNothingSaved')
              : provider === 'coder' ? t('sessionEnvironments.verifyHint')
                : provider === 'sprites' ? t('sessionEnvironments.verifyHintSprites')
                  : t('sessionEnvironments.verifyHintBoxd')}
        </p>
        <Btn type="button" disabled={dirty || !providerSaved || checkMutation.isPending}
          aria-describedby={`${controlId}-verification`} onClick={() => checkMutation.mutate()}>
          {checkMutation.isPending ? t('sessionEnvironments.checkRunning')
            : provider === 'coder' ? t('sessionEnvironments.check')
              : provider === 'sprites' ? t('sessionEnvironments.checkSprites')
                : t('sessionEnvironments.checkBoxd')}
        </Btn>
        {checkMutation.data && !dirty && <CheckResults result={checkMutation.data} provider={provider}
          profiles={savedProfiles} spritesProfiles={savedSpritesProfiles} boxdProfiles={savedBoxdProfiles} />}
        <ErrorNotice askAgent message={checkMutation.isError ? t('sessionEnvironments.checkError') : null} />
      </section>
      <section aria-label={t('sessionEnvironments.manageStep')} className="space-y-3 pt-3">
        <PanelSectionHeader label={t('sessionEnvironments.manageStep')} />
        <p className="text-[13px] text-muted">
          {provider === 'coder' ? t('sessionEnvironments.managementHint')
            : provider === 'sprites' ? t('sessionEnvironments.managementHintSprites')
              : t('sessionEnvironments.managementHintBoxd')}
        </p>
        {providerSaved && <WorkspaceList provider={provider} />}
        {provider === 'coder' && savedCoderUrl && (
          <a href={savedCoderUrl} target="_blank" rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-[13px] text-accent hover:underline">
            {t('sessionEnvironments.openCoder')}<ExternalLink size={12} className="lucide-inline" aria-hidden="true" />
          </a>
        )}
      </section>
      {/* Global: these apply to every profile. They follow the numbered
        * steps, which a first setup walks in order. Older gateways omit
        * either key, and then its control stays hidden. */}
      {(config.default_profile !== undefined || config.stop_on_shutdown !== undefined) && (() => {
        const all = [...config.profiles, ...spritesProfiles, ...boxdProfiles].filter(p => p.id)
        const defaultOn = !!config.default_profile
        return (
          <section aria-label={t('sessionEnvironments.globalDefaults')} className="space-y-3 pt-3">
            <PanelSectionHeader label={t('sessionEnvironments.globalDefaults')} />
            <fieldset disabled={mutation.isPending} className="min-w-0 space-y-3">
              {config.default_profile !== undefined && <>
                <SettingsToggle configKey="session_environments.default_profile"
                  label={t('sessionEnvironments.defaultEnabled')}
                  description={all.length ? t('sessionEnvironments.defaultEnabledHint') : t('sessionEnvironments.defaultEnabledNoProfiles')}
                  checked={defaultOn} disabled={!all.length && !defaultOn}
                  onChange={on => change({ ...config, default_profile: on ? all[0]?.id ?? '' : '' })} />
                {defaultOn && (
                  <SettingsSelect label={t('sessionEnvironments.defaultProfile')}
                    description={t('sessionEnvironments.defaultProfileHint')}
                    value={config.default_profile ?? ''}
                    options={all.map(p => p.id)}
                    optionLabels={all.map(p => p.name || p.id)}
                    // Never ''. The picker reports '' when its value briefly
                    // leaves the options (a rename); that is not a choice.
                    onChange={value => { if (value) change({ ...config, default_profile: value }) }} />
                )}
              </>}
              {config.stop_on_shutdown !== undefined && (() => {
                // One global switch, shown the same on every tab: p1's label and
                // Coder line, then what it does for each other provider (boxd
                // machines are hibernated, coder/retention.py stop_for_shutdown;
                // a sprite goes cold by itself and is never stopped).
                const notesId = `${controlId}-shutdown-providers`
                return <div>
                  <SettingsToggle configKey="session_environments.stop_on_shutdown"
                    label={t('sessionEnvironments.stopOnShutdown')}
                    description={t('sessionEnvironments.stopOnShutdownHint')}
                    checked={!!all.length && config.stop_on_shutdown} disabled={!all.length}
                    describedBy={notesId}
                    onChange={on => change({ ...config, stop_on_shutdown: on })} />
                  <ul id={notesId} data-testid="shutdown-provider-notes" className="mr-14 space-y-0.5 text-[12px] text-muted">
                    <li>{t('sessionEnvironments.stopOnShutdownBoxd')}</li>
                    <li>{t('sessionEnvironments.stopOnShutdownSprites')}</li>
                  </ul>
                </div>
              })()}
            </fieldset>
          </section>
        )
      })()}
      {/* Last, after the defaults, and stuck to the bottom of the panel while
        * there are unsaved edits, so Save stays in view from every section. */}
      <div data-testid="session-environments-save-bar"
        className={`flex flex-wrap items-center gap-2 ${dirty ? 'sticky bottom-0 z-10 -mx-1 border-t border-border bg-bg px-1 py-2' : ''}`}>
        <Btn type="submit" primary={dirty} disabled={!dirty || mutation.isPending}>{t('sessionEnvironments.save')}</Btn>
        <Btn type="button" disabled={!dirty || mutation.isPending} onClick={() => {
          setDraft(null)
          setTokens({})
          setTokenErrors({})
          setSaved(false)
          setRemoved(false)
          setOpenRows(emptyRows())
          mutation.reset()
        }}>{t('sessionEnvironments.discard')}</Btn>
        {(saved || (removed && draft)) && <span role="status" className="text-[13px] text-muted">
          {saved ? t('sessionEnvironments.saved') : t('sessionEnvironments.removed')}</span>}
      </div>
      {/* No hand-off: navigating would discard the unsaved environment draft.
        * The server's detail names the field it refused. A refused token is
        * reported on its own field instead. */}
      <ErrorNotice report={reportForError(mutation.error)} message={mutation.isError && !(mutation.error instanceof TokenSaveError)
        ? t('sessionEnvironments.saveError', { detail: mutation.error.message === 'coder_url must not include a query string'
          ? t('sessionEnvironments.urlQueryError')
          : mutation.error.message || t('pages.settings.instancesPanel.unknown_error') })
        : null} />
    </form>
  )
}
