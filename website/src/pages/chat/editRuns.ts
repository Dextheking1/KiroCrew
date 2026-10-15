/**
 * editRuns — collapse an unbroken run of edits to ONE file into one row.
 *
 * An agent that edits a file three times in a row writes three edit tool rows,
 * each with its own pill and diff chip, all naming the same file. The run reads
 * as one change, so the transcript draws it as one: the FIRST row of the run
 * (the lead) carries a chip with the combined counts and every member's diff,
 * and the later rows (followers) draw nothing.
 *
 * Adjacency is judged on what the reader sees. A row that draws nothing in the
 * transcript — the ✅/🚫 completion sibling, an approval record (the approval
 * bar owns those), a reasoning burst (TurnBlock hoists every burst to the turn
 * top) — does not break a run. Anything else does: a read, a command, a
 * message, an edit to a different file. The timeline therefore stays exact;
 * only edits the reader would already see stacked together merge.
 *
 * Only a settled, applied, card-sized diff joins a run. A call awaiting
 * approval keeps its own pill (the inline panel is where the user reads what
 * they approve), a rejected or auto-denied call never applied its change, and
 * a summary-mode diff (over the card cap, or truncated) cannot be shown whole
 * inside a combined card. Each of those stays a row of its own, which is how
 * every edit rendered before runs existed. So does a call the tool log marks
 * rejected (a Stop rejects in-flight calls there and nowhere else) and a row
 * carrying a tool-risk badge, which only its own row draws.
 *
 * Pure over (messages, toolLog), so the lead and its followers read one answer
 * from the same store slices. A follower's row renders nothing, and the
 * transcript's row wrappers collapse when empty (`empty:hidden`), so it leaves
 * no spacer. Built once per (messages, toolLog) identity (weakMapMemoize),
 * like selectToolRowIndex.
 */
import { createSelector } from '@reduxjs/toolkit'
import type { ChatMessage, ToolActivity } from '../../types'
import { readToolRiskRecord, toolRiskFieldOf } from './toolRiskRecord'
import { extractFilePath } from '../../components/DiffBlock'
import { isRejectedDecision } from '../../utils/approvalDecision'
import { presentToolDiff } from './toolDiff'
import { isReasoningRole } from './groupDisplayItems'

export interface EditRunIndex {
  /** Lead row → every member of its run, lead first, one per tool call.
   *  Only runs of two or more calls are recorded. */
  membersByLead: Map<ChatMessage, ChatMessage[]>
  /** Rows folded into an earlier lead; they draw nothing. */
  followers: Set<ChatMessage>
}

const EMPTY_INDEX: EditRunIndex = { membersByLead: new Map(), followers: new Set() }

/** Draws nothing in the transcript, so it neither joins nor breaks a run. */
function isTransparent(m: ChatMessage): boolean {
  if (m.role === 'permission' || m.role === 'queued' || isReasoningRole(m)) return true
  return m.role === 'tool' && !m.content?.startsWith('🔧')
}

interface CallState { denied: boolean; lastPerm: ChatMessage | undefined }

/** The file a mergeable edit row changes, or null when the row cannot merge. */
function mergeablePath(m: ChatMessage, calls: Map<string, CallState>, logRejected: Set<string>): { id: string; path: string } | null {
  if (m.role !== 'tool' || !m.content?.startsWith('🔧')) return null
  const meta = m.meta as Record<string, unknown> | undefined
  const id = meta?.tool_call_id
  const kind = meta?.kind
  const input = meta?.input
  if (typeof id !== 'string' || !id || typeof kind !== 'string' || typeof input !== 'string') return null
  if (meta?.mcp_app === true) return null
  if (presentToolDiff(kind, input)?.mode !== 'card') return null
  // A risk badge belongs to its own row; a merged lead draws only its own.
  if (readToolRiskRecord(toolRiskFieldOf(m))) return null
  const call = calls.get(id)
  // A Stop marks an in-flight call rejected in the tool log only
  // (clearPendingPermissions), with no message of its own.
  if (call?.denied || logRejected.has(id)) return null
  const perm = call?.lastPerm
  if (perm && (!perm.meta?.resolved || isRejectedDecision(perm.meta.resolved))) return null
  const path = extractFilePath(input)?.path
  return path ? { id, path } : null
}

function buildEditRuns(messages: ChatMessage[], toolLog: ToolActivity[]): EditRunIndex {
  // The NEWEST tool entry per call decides, as selectToolRowIndex does for the
  // row's own status: a replayed entry can follow an older rejected one.
  const newestById = new Map<string, ToolActivity>()
  for (const e of toolLog) if (e.type === 'tool' && e.tool_call_id) newestById.set(e.tool_call_id, e)
  const logRejected = new Set<string>()
  for (const [id, e] of newestById) if (e.rejected) logRejected.add(id)
  // Pass 1: per-call facts that can arrive AFTER the edit row (its 🚫 deny
  // sibling, its approval record), so pass 2 can judge each row whole.
  const calls = new Map<string, CallState>()
  for (const m of messages) {
    const id = m.meta?.tool_call_id
    if (typeof id !== 'string' || !id) continue
    if (m.role === 'permission') {
      const c = calls.get(id) ?? { denied: false, lastPerm: undefined }
      c.lastPerm = m
      calls.set(id, c)
    } else if (m.role === 'tool' && m.content?.startsWith('🚫')) {
      const c = calls.get(id) ?? { denied: false, lastPerm: undefined }
      c.denied = true
      calls.set(id, c)
    }
  }

  let membersByLead: Map<ChatMessage, ChatMessage[]> | null = null
  let followers: Set<ChatMessage> | null = null
  let run: { lead: ChatMessage; path: string; members: ChatMessage[]; ids: Set<string> } | null = null
  const close = () => {
    if (run && run.members.length > 1) (membersByLead ??= new Map()).set(run.lead, run.members)
    run = null
  }
  for (const m of messages) {
    if (isTransparent(m)) continue
    const edit = mergeablePath(m, calls, logRejected)
    if (!edit) { close(); continue }
    if (run && run.path === edit.path) {
      ;(followers ??= new Set()).add(m)
      // A second 🔧 row for the same call is the same change written twice;
      // it hides with the run but adds no diff of its own.
      if (!run.ids.has(edit.id)) { run.ids.add(edit.id); run.members.push(m) }
      continue
    }
    close()
    run = { lead: m, path: edit.path, members: [m], ids: new Set([edit.id]) }
  }
  close()
  if (!membersByLead && !followers) return EMPTY_INDEX
  return { membersByLead: membersByLead ?? new Map(), followers: followers ?? new Set() }
}

/** Memoized on the (messages, toolLog) identities (weakMapMemoize — see module
 *  doc). Keyed by message REFERENCE: a host drawing copies of the store's rows
 *  finds no run and draws every edit on its own, as before runs existed. */
export const selectEditRuns = createSelector(
  [(messages: ChatMessage[]) => messages, (_messages: ChatMessage[], toolLog: ToolActivity[]) => toolLog],
  buildEditRuns,
)
