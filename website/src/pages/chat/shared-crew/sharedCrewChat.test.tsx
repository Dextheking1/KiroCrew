import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { AcceptedWork, CapacityResponse, PresenceEntry, SharedDecision } from '../../../api/sharedCrew'
import { api } from '../../../api/client'
import { WorkStatusList } from './WorkStatusList'
import { PresenceChips } from './PresenceChips'
import { DecisionAttributionList } from './DecisionAttributionList'
import { endedReason, refusalMessage, queuedReason } from './overloadCopy'
import { alreadyResolvedDecision, resolvedByLine } from '../../../components/chat-input/sharedDecision'
import { readSendReceipt } from '../../../utils/sendDelivery'

vi.mock('../../../api/client', () => ({ api: { sharedCrewCancelWork: vi.fn() } }))

const cancel = vi.mocked(api.sharedCrewCancelWork)

const work = (over: Partial<AcceptedWork>): AcceptedWork => ({
  work_id: 'w1', session_key: 's1', operation: 'chat_turn', state: 'queued', revision: 1,
  accepted_at: 1, expires_at: 2, deadline_at: null, reason: null, ...over,
})
const person = (id: string, name: string) => ({ id, kind: 'human' as const, display: { name, login: '', captured_at: null } })
const capacity = (scope: 'principal' | 'crew' | null): CapacityResponse => ({
  schema_version: 1,
  principal: { active: 2, queued: 1, active_limit: 2, queue_limit: 3 },
  crew: { active: 8, queued: 4, active_limit: 8, queue_limit: 4 },
  enforcement: 'hard', limiting_scope: scope, reason: null, retry_after_seconds: 5,
  queue_expiry_seconds: 600, run_deadline_seconds: null,
})

function mount(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return { qc, ...render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>) }
}

beforeEach(() => vi.clearAllMocks())

describe('WorkStatusList', () => {
  it.each([
    ['queued', 'Queued on the host'],
    ['running', 'Running'],
    ['cancelling', 'Cancelling'],
    ['reconciling', 'Checking what happened'],
    ['completed', 'Done'],
    ['failed', 'Ended'],
  ] as const)('shows %s as its own state', (state, label) => {
    mount(<WorkStatusList sessionKey="s1" items={[work({ state })]} capacity={null} />)
    expect(screen.getByText(label)).toBeInTheDocument()
  })

  it('says whose limit holds queued work and never estimates a wait', () => {
    const { unmount } = mount(<WorkStatusList sessionKey="s1" items={[work({ reason: 'principal_active_limit' })]} capacity={capacity('principal')} />)
    expect(screen.getByText(/your personal limit/)).toBeInTheDocument()
    unmount()
    mount(<WorkStatusList sessionKey="s1" items={[work({})]} capacity={capacity('crew')} />)
    const text = screen.getByTestId('work-status-list').textContent ?? ''
    expect(text).toMatch(/shared limit/)
    expect(text).not.toMatch(/second|minute|ETA/i)
  })

  it('cancels exactly the shown item at its revision and adopts the result', async () => {
    cancel.mockResolvedValue({ ok: true, work: work({ state: 'cancelled', revision: 2 }) })
    const { qc } = mount(<WorkStatusList sessionKey="s1" items={[work({ revision: 1 })]} capacity={null} />)
    fireEvent.click(screen.getByRole('button', { name: /Cancel this item/ }))
    await waitFor(() => expect(cancel).toHaveBeenCalledWith('w1', 1))
    await waitFor(() => expect((qc.getQueryData(['shared-crew', 'work', 's1']) as { items: AcceptedWork[] }).items[0].state).toBe('cancelled'))
  })

  it('a stale cancel adopts the server state and says so', async () => {
    cancel.mockResolvedValue({ ok: false, conflict: true, current: work({ state: 'running', revision: 5 }), code: 'stale_revision' })
    const { qc } = mount(<WorkStatusList sessionKey="s1" items={[work({ revision: 1 })]} capacity={null} />)
    fireEvent.click(screen.getByRole('button', { name: /Cancel this item/ }))
    expect(await screen.findByText(/changed before the cancel landed/)).toBeInTheDocument()
    expect((qc.getQueryData(['shared-crew', 'work', 's1']) as { items: AcceptedWork[] }).items[0].revision).toBe(5)
  })

  it('shows only open work plus the latest settled item', () => {
    mount(<WorkStatusList sessionKey="s1" items={[
      work({ work_id: 'a', state: 'completed' }),
      work({ work_id: 'b', state: 'completed' }),
      work({ work_id: 'c', state: 'running' }),
    ]} capacity={null} />)
    expect(screen.queryByTestId('work-a')).toBeNull()
    expect(screen.getByTestId('work-b')).toBeInTheDocument()
    expect(screen.getByTestId('work-c')).toBeInTheDocument()
  })
})

describe('PresenceChips', () => {
  const entry = (id: string, name: string, activity: PresenceEntry['activity'], connections = 1): PresenceEntry =>
    ({ principal: person(id, name), session_key: 's1', activity, connections, expires_at: 0 })

  it('shows one chip per person, leaves the viewer out, and names who types', () => {
    mount(<PresenceChips viewer={person('p_me', 'Me')} entries={[
      entry('p_me', 'Me', 'typing', 3),
      entry('p_bob', 'Bob Example', 'typing', 4),
      entry('p_ann', 'Ann', 'viewing'),
    ]} />)
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
    expect(screen.getByTestId('presence-typing').textContent).toBe('Bob Example is typing')
  })

  it('handles a long name and Unknown without breaking', () => {
    const long = 'A'.repeat(200)
    mount(<PresenceChips viewer={null} entries={[entry('p_l', long, 'viewing'), { ...entry('p_unknown', '', 'viewing'), principal: { id: 'p_unknown', kind: 'unknown', display: { name: '', login: '', captured_at: null } } }]} />)
    expect(screen.getByLabelText(`${long} is here`)).toBeInTheDocument()
    expect(screen.getByLabelText('Unknown is here')).toBeInTheDocument()
  })

  it('renders nothing when nobody else is here', () => {
    const { container } = mount(<PresenceChips viewer={person('p_me', 'Me')} entries={[entry('p_me', 'Me', 'viewing')]} />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('shared decisions', () => {
  const decision = (state: SharedDecision['state'], outcome = ''): SharedDecision => ({
    decision_id: 'approval:2:s1:7', kind: 'approval', session_key: 's1', state, revision: 2,
    initiator: person('p_a', 'Alice'), responder: state === 'resolved' ? person('p_b', 'Bob') : null,
    outcome, resolved_at: state === 'resolved' ? 10 : null,
  })

  it('names both the person whose turn asked and the person who answered', () => {
    mount(<DecisionAttributionList items={[decision('resolved', 'approved')]} />)
    expect(screen.getByText('Approved by Bob')).toBeInTheDocument()
    expect(screen.getByText(/asked during Alice's turn/)).toBeInTheDocument()
  })

  it('reads a lost race as the winning decision, not a failure', () => {
    const err = { status: 409, body: JSON.stringify({ code: 'already_resolved', decision: decision('resolved', 'rejected') }) }
    const won = alreadyResolvedDecision(err)
    expect(won?.responder?.display.name).toBe('Bob')
    expect(resolvedByLine(won!)).toBe('Rejected by Bob')
    expect(alreadyResolvedDecision({ status: 404, body: '{}' })).toBeNull()
    expect(alreadyResolvedDecision({ status: 409, body: 'not json' })).toBeNull()
  })
})

describe('overload copy', () => {
  it('separates a personal limit from a Crew-wide one and keeps the draft promise', () => {
    const mine = refusalMessage({ code: 'principal_active_limit', capacity: capacity('principal') })
    const crew = refusalMessage({ code: 'crew_active_limit', capacity: capacity('crew') })
    expect(mine).toMatch(/personal limit \(2 of 2 running\)/)
    expect(crew).toMatch(/limit for everyone \(8 of 8 running\)/)
    for (const s of [mine, crew]) {
      expect(s).toMatch(/still in the composer/)
      expect(s).not.toMatch(/second|minute/i)
    }
  })

  it('names a turn that ended in a runtime error', () => {
    expect(endedReason('runtime_error')).toBe('stopped with an error')
  })

  it('leaves non Shared Crew refusals to the caller', () => {
    expect(refusalMessage({ code: 'slot_busy' })).toBeNull()
    expect(refusalMessage({})).toBeNull()
  })

  it('falls back to the reason when no capacity is known', () => {
    expect(queuedReason('crew_queue_full', null)).toMatch(/shared limit/)
    expect(queuedReason(null, null)).toBe('waiting for its turn')
  })
})

describe('send receipt', () => {
  const response = (status: number, body: unknown) => ({
    ok: status < 300, status, headers: { get: () => 'application/json' }, json: async () => body,
  }) as unknown as Response

  it('counts a host-accepted queued work item as accepted, not refused', async () => {
    const r = await readSendReceipt(response(202, { work: work({}) }))
    expect(r.outcome).toBe('accepted')
  })

  it('still reads a limit refusal as refused, so the draft is restored', async () => {
    const r = await readSendReceipt(response(429, { error: 'limit', code: 'principal_active_limit' }))
    expect(r.outcome).toBe('refused')
  })
})
