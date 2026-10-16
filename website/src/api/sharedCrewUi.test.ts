import { describe, it, expect } from 'vitest'
import { OPERATION_TRANSITIONS, type AcceptedWork, type OperationState, type SharedDecision } from './sharedCrew'
import {
  isAcceptedWork,
  isUnknownPrincipal,
  limitScope,
  membershipPointer,
  mergeDecision,
  mergeWork,
  messageAuthor,
  principalInitials,
  principalName,
  workCancellable,
  workView,
} from './sharedCrewUi'

const work = (over: Partial<AcceptedWork> = {}): AcceptedWork => ({
  work_id: 'w1', session_key: 's1', operation: 'chat_turn', state: 'queued', revision: 1,
  accepted_at: 100, expires_at: 200, deadline_at: null, reason: null, ...over,
})

const person = (name: string, login = '') => ({ id: 'p_' + 'a'.repeat(32), kind: 'human' as const, display: { name, login, captured_at: null } })

describe('workView', () => {
  it('maps every operation state to one distinct on-screen state (spec section 7)', () => {
    const expected: Record<OperationState, string> = {
      submitted: 'sending', queued: 'queued', admitted: 'running', running: 'running',
      cancelling: 'cancelling', reconciling: 'checking', completed: 'done', rejected: 'refused',
      failed: 'ended', expired: 'ended', cancelled: 'ended', unknown_outcome: 'ended',
    }
    for (const state of Object.keys(OPERATION_TRANSITIONS) as OperationState[]) {
      expect(workView(state), state).toBe(expected[state])
    }
  })

  it('offers cancel only while work can still be stopped', () => {
    expect(workCancellable(work({ state: 'queued' }))).toBe(true)
    expect(workCancellable(work({ state: 'running' }))).toBe(true)
    expect(workCancellable(work({ state: 'cancelling' }))).toBe(false)
    expect(workCancellable(work({ state: 'completed' }))).toBe(false)
  })
})

describe('mergeWork', () => {
  it('keeps one row per work id and never moves backwards', () => {
    let list = mergeWork([], work({ revision: 1 }))
    list = mergeWork(list, work({ revision: 3, state: 'running' }))
    // A replayed older frame after a reconnect changes nothing.
    list = mergeWork(list, work({ revision: 2, state: 'queued' }))
    list = mergeWork(list, work({ revision: 3, state: 'running' }))
    expect(list).toHaveLength(1)
    expect(list[0].state).toBe('running')
    expect(list[0].revision).toBe(3)
  })

  it('appends a different work id', () => {
    const list = mergeWork([work()], work({ work_id: 'w2' }))
    expect(list.map(w => w.work_id)).toEqual(['w1', 'w2'])
  })
})

describe('mergeDecision', () => {
  const d = (revision: number, state: SharedDecision['state']): SharedDecision => ({
    decision_id: 'd1', kind: 'approval', session_key: 's1', state, revision,
    initiator: person('Alice'), responder: state === 'resolved' ? person('Bob') : null,
    outcome: state === 'resolved' ? 'approved' : '', resolved_at: null,
  })
  it('lets a resolved decision replace pending, not the reverse', () => {
    let list = mergeDecision([], d(1, 'pending'))
    list = mergeDecision(list, d(2, 'resolved'))
    list = mergeDecision(list, d(1, 'pending'))
    expect(list).toHaveLength(1)
    expect(list[0].state).toBe('resolved')
  })
})

describe('principals', () => {
  it('reads Unknown for the unknown principal and missing data', () => {
    expect(isUnknownPrincipal(null)).toBe(true)
    expect(isUnknownPrincipal({ id: 'p_unknown', kind: 'unknown', display: { name: '', login: '', captured_at: null } })).toBe(true)
    expect(principalName({ id: 'p_unknown', kind: 'unknown', display: { name: 'Mallory', login: '', captured_at: null } })).toBe('')
  })

  it('falls back to the login and builds initials without fetching anything', () => {
    expect(principalName(person('', 'alice@example.test'))).toBe('alice@example.test')
    expect(principalInitials(person('Ada Lovelace'))).toBe('AL')
    expect(principalInitials(person('', 'bob@example.test'))).toBe('BE')
    expect(principalInitials(null)).toBe('?')
  })

  it('reads a server-stamped message author and ignores garbage', () => {
    expect(messageAuthor({ author: { id: 'p_x', kind: 'human', display: { name: 'Ann' } } })?.display.name).toBe('Ann')
    expect(messageAuthor({ author: 'Ann' })).toBeNull()
    expect(messageAuthor({ author: { kind: 'human' } })).toBeNull()
    expect(messageAuthor(undefined)).toBeNull()
    expect(messageAuthor({ author: { id: 'p_x', kind: 'admin', display: {} } })?.kind).toBe('unknown')
  })
})

describe('limits', () => {
  it('names whose limit holds a refusal', () => {
    expect(limitScope('principal_active_limit')).toBe('principal')
    expect(limitScope('crew_queue_full')).toBe('crew')
    expect(limitScope(null)).toBeNull()
  })

  it('prefers the capacity answer over the reason', () => {
    const cap = { limiting_scope: 'crew' } as Parameters<typeof limitScope>[1]
    expect(limitScope('principal_active_limit', cap)).toBe('crew')
  })

  it('recognises an accepted work body', () => {
    expect(isAcceptedWork(work())).toBe(true)
    expect(isAcceptedWork({ work_id: 'w' })).toBe(false)
    expect(isAcceptedWork(null)).toBe(false)
  })

  it('never turns a membership link into anything but a closed id', () => {
    expect(membershipPointer('tailscale_admin')).toBe('tailscale_admin')
    expect(membershipPointer('console')).toBe('console')
    expect(membershipPointer('')).toBeNull()
    expect(membershipPointer('https://evil.example/' as never)).toBeNull()
  })
})
