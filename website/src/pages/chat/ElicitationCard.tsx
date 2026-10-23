import { memo, useState } from 'react'
import { Ban, CheckCircle, ExternalLink, Loader2, MessageSquareMore } from 'lucide-react'
import { Glass } from '../../components/Glass'
import SimpleSelect from '../../components/SimpleSelect'
import { approvalBtnClass } from '../../components/chat-input/approval'
import { apiTransport } from '../../api/apiTransport'
import { ApiError } from '../../api/apiError'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import type { ChatMessage } from '../../types'

/**
 * One field of an elicitation form, as the gateway normalised it
 * (`kiro_crew.acp.elicitation.ElicitationField.as_dict`). The kinds are the
 * only shapes MCP's elicitation schema allows a client to render, so the
 * card draws exactly these and nothing else.
 */
export interface ElicitationField {
  name: string
  kind: 'string' | 'number' | 'integer' | 'boolean' | 'enum' | 'multi_enum'
  required: boolean
  title?: string
  description?: string
  options?: { value: string; label: string }[]
  default?: unknown
  minimum?: number
  maximum?: number
  min_length?: number
  max_length?: number
  format?: string
}

/** The row meta an `elicitation` row carries (the backend's `request_id` arrives as `approval_id`). */
export interface ElicitationMeta {
  approval_id?: string
  tool_call_id?: string
  mode?: 'form' | 'url'
  message?: string
  fields?: ElicitationField[]
  url?: string
  resolved?: string
  answer?: Record<string, unknown>
}

export type ElicitationValue = string | number | boolean | string[] | undefined

/** The person's answers before validation: one entry per field they touched. */
export type ElicitationDraft = Record<string, ElicitationValue>

export function elicitationMetaOf(m: ChatMessage): ElicitationMeta {
  return (m.meta ?? {}) as ElicitationMeta
}

/** Seed the draft from the server's defaults so a field with one is pre-filled. */
export function initialDraft(fields: ElicitationField[]): ElicitationDraft {
  const draft: ElicitationDraft = {}
  for (const f of fields) {
    if (f.default !== undefined && f.default !== null) draft[f.name] = f.default as ElicitationValue
    else if (f.kind === 'boolean') draft[f.name] = false
    else if (f.kind === 'multi_enum') draft[f.name] = []
  }
  return draft
}

/**
 * Shape the draft for the wire: a number field's text becomes a number, an
 * empty optional field is left out, and a required field with no value is the
 * first thing reported. The gateway validates again against the schema, so
 * this is the client-side half that keeps an obvious miss from a round trip.
 */
export function draftToContent(fields: ElicitationField[], draft: ElicitationDraft): { content?: Record<string, unknown>; missing?: string } {
  const content: Record<string, unknown> = {}
  for (const f of fields) {
    const v = draft[f.name]
    const empty = v === undefined || v === '' || (Array.isArray(v) && v.length === 0)
    if (empty) {
      if (f.required && f.kind !== 'boolean') return { missing: f.title || f.name }
      if (f.kind === 'boolean') content[f.name] = false
      continue
    }
    if (f.kind === 'number' || f.kind === 'integer') {
      const n = typeof v === 'number' ? v : Number(v)
      if (Number.isNaN(n)) return { missing: f.title || f.name }
      content[f.name] = n
    } else {
      content[f.name] = v
    }
  }
  return { content }
}

function FieldLabel({ field }: { field: ElicitationField }) {
  return (
    <span className="flex flex-col gap-0.5 min-w-0">
      <span className="text-[12px] text-text font-medium">
        {field.title || field.name}
        {field.required && <span className="text-danger ml-0.5" aria-hidden="true">*</span>}
      </span>
      {field.description && <span className="text-[11px] text-muted">{field.description}</span>}
    </span>
  )
}

const inputClass = 'w-full rounded-md border border-border bg-bg/60 px-2 py-1 text-[12px] text-text font-body focus:outline-none focus:border-border-strong'

function FieldControl({ field, value, onChange, disabled }: {
  field: ElicitationField
  value: ElicitationValue
  onChange: (v: ElicitationValue) => void
  disabled: boolean
}) {
  const id = `elicit-${field.name}`
  if (field.kind === 'boolean') {
    return (
      <label className="flex items-start gap-2 cursor-pointer">
        <input
          id={id}
          type="checkbox"
          aria-label={field.title || field.name}
          className="mt-0.5 accent-accent"
          checked={value === true}
          disabled={disabled}
          onChange={e => onChange(e.target.checked)}
        />
        <FieldLabel field={field} />
      </label>
    )
  }
  if (field.kind === 'enum') {
    const options = field.options ?? []
    return (
      <div className="flex flex-col gap-1">
        <label htmlFor={id}><FieldLabel field={field} /></label>
        <SimpleSelect
          id={id}
          aria-label={field.title || field.name}
          options={options.map(o => o.value)}
          optionLabels={options.map(o => o.label)}
          value={typeof value === 'string' ? value : ''}
          triggerFallback={i18nT('pages.chat.elicitationCard.choose')}
          disabled={disabled}
          onChange={onChange}
          className="px-2 py-1 text-[12px]"
        />
      </div>
    )
  }
  if (field.kind === 'multi_enum') {
    const selected = Array.isArray(value) ? value : []
    return (
      <fieldset className="flex flex-col gap-1 border-none p-0 m-0 min-w-0">
        <legend className="p-0"><FieldLabel field={field} /></legend>
        <div className="flex flex-wrap gap-x-3 gap-y-1">
          {(field.options ?? []).map(o => (
            <label key={o.value} className="inline-flex items-center gap-1.5 text-[12px] text-text cursor-pointer">
              <input
                type="checkbox"
                aria-label={o.label}
                className="accent-accent"
                checked={selected.includes(o.value)}
                disabled={disabled}
                onChange={e => onChange(e.target.checked ? [...selected, o.value] : selected.filter(s => s !== o.value))}
              />
              {o.label}
            </label>
          ))}
        </div>
      </fieldset>
    )
  }
  const numeric = field.kind === 'number' || field.kind === 'integer'
  const inputType = numeric ? 'number' : field.format === 'email' ? 'email' : field.format === 'uri' ? 'url' : field.format === 'date' ? 'date' : field.format === 'date-time' ? 'datetime-local' : 'text'
  return (
    <label htmlFor={id} className="flex flex-col gap-1">
      <FieldLabel field={field} />
      <input
        id={id}
        type={inputType}
        aria-label={field.title || field.name}
        className={inputClass}
        value={value === undefined || typeof value === 'boolean' || Array.isArray(value) ? '' : String(value)}
        disabled={disabled}
        required={field.required}
        min={field.minimum}
        max={field.maximum}
        step={field.kind === 'integer' ? 1 : undefined}
        minLength={field.min_length}
        maxLength={field.max_length}
        onChange={e => onChange(e.target.value)}
      />
    </label>
  )
}

export function resolvedLabel(resolved: string): string {
  if (resolved === 'accept') return i18nT('pages.chat.elicitationCard.resolved_accept')
  if (resolved === 'decline') return i18nT('pages.chat.elicitationCard.resolved_decline')
  return i18nT('pages.chat.elicitationCard.resolved_cancel')
}

/** The sentence a failed answer shows: the gateway's own reason when it sent one. */
function answerErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 404) return i18nT('pages.chat.elicitationCard.no_longer_pending')
    // The transport already unwrapped the gateway's `error` field into the
    // message (friendlyErrText), which for a refused value names the field.
    if (err.message) return err.message
  }
  return i18nT('pages.chat.elicitationCard.answer_failed')
}

/**
 * An MCP server's own question to the person, raised in the middle of a tool
 * call: either a small form built from the schema the server sent, or a link
 * the person is asked to visit. Neither is Kiro Crew's tool approval -- the
 * server declared that a human must answer, so no trust setting answers it --
 * which is why this card has no Trust tier and its wording names the tool,
 * not the agent. Answering posts to the slot's `elicitation` endpoint; the
 * gateway validates a form against the schema and a refused value comes back
 * as the card's inline error with the form still open.
 */
export default memo(function ElicitationCard({ message, slot }: { message: ChatMessage; slot: string | null }) {
  useLanguageGeneration()
  const meta = elicitationMetaOf(message)
  const fields = meta.fields ?? []
  const isForm = meta.mode !== 'url'
  const [draft, setDraft] = useState<ElicitationDraft>(() => initialDraft(fields))
  const [busy, setBusy] = useState<'accept' | 'decline' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const resolved = meta.resolved
  const requestId = meta.approval_id

  const answer = async (action: 'accept' | 'decline') => {
    if (!slot || !requestId) return
    let content: Record<string, unknown> | undefined
    if (action === 'accept' && isForm) {
      const shaped = draftToContent(fields, draft)
      if (shaped.missing) {
        setError(i18nT('pages.chat.elicitationCard.required_missing', { field: shaped.missing }))
        return
      }
      content = shaped.content
    }
    setBusy(action)
    setError(null)
    try {
      await apiTransport.post(`/api/chat/slots/${encodeURIComponent(slot)}/elicitation`, {
        request_id: requestId,
        action,
        ...(content ? { content } : {}),
      })
    } catch (err) {
      setError(answerErrorText(err))
    } finally {
      setBusy(null)
    }
  }

  return (
    <Glass variant="chip" radius={16} className={`w-full${resolved ? '' : ' approval-glow'}`} data-testid="elicitation-card">
      <div className="flex flex-col gap-2 px-3.5 py-2.5">
        <div className="flex items-start gap-2">
          <MessageSquareMore size={14} className="text-warn shrink-0 mt-0.5" />
          <div className="flex flex-col gap-0.5 min-w-0 flex-1">
            <span className="text-[11px] text-muted uppercase tracking-wide">{i18nT('pages.chat.elicitationCard.title')}</span>
            <span className="text-[13px] text-text whitespace-pre-wrap break-words">{meta.message || message.content}</span>
          </div>
        </div>
        {!resolved && isForm && fields.length > 0 && (
          <div className="flex flex-col gap-2 pl-6">
            {fields.map(f => (
              <FieldControl
                key={f.name}
                field={f}
                value={draft[f.name]}
                disabled={busy !== null}
                onChange={v => setDraft(d => ({ ...d, [f.name]: v }))}
              />
            ))}
          </div>
        )}
        {!resolved && !isForm && meta.url && (
          <div className="pl-6">
            <a
              href={meta.url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-[12px] text-accent hover:underline break-all"
            >
              <ExternalLink size={12} className="shrink-0" />{meta.url}
            </a>
            <div className="text-[11px] text-muted mt-1">{i18nT('pages.chat.elicitationCard.url_hint')}</div>
          </div>
        )}
        {error && <div className="pl-6 text-[12px] text-danger" role="alert">{error}</div>}
        <div className="flex items-center gap-1.5 pl-6">
          {resolved ? (
            <span className="inline-flex items-center gap-1 text-[12px] text-muted">
              {resolved === 'accept' ? <CheckCircle size={12} className="shrink-0" /> : <Ban size={12} className="shrink-0" />}
              {resolvedLabel(resolved)}
            </span>
          ) : busy ? (
            <span className="inline-flex items-center gap-1 text-[12px] text-muted/60">
              <Loader2 size={12} className="animate-spin shrink-0" />{i18nT('pages.chat.elicitationCard.sending')}
            </span>
          ) : (
            <>
              <button type="button" onClick={() => void answer('accept')} className={approvalBtnClass} disabled={!slot}>
                <CheckCircle size={12} className="shrink-0" />
                {isForm ? i18nT('pages.chat.elicitationCard.submit') : i18nT('pages.chat.elicitationCard.done')}
              </button>
              <button type="button" onClick={() => void answer('decline')} className={`${approvalBtnClass} hover:!text-danger hover:!border-danger`} disabled={!slot}>
                <Ban size={12} className="shrink-0" />{i18nT('pages.chat.elicitationCard.decline')}
              </button>
            </>
          )}
        </div>
      </div>
    </Glass>
  )
})
