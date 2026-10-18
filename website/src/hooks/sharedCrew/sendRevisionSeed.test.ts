import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { api } from '../../api/client'
import { baseRevisionFor, noteSendRevision, resetSendRaceForTests } from '../../api/sendRace'
import { ensureSendRevision, resetSendRevisionSeedForTests } from './sendRevisionSeed'

vi.mock('../../api/client', () => ({ api: { sharedCrewSendState: vi.fn() } }))
const sendState = vi.mocked(api.sharedCrewSendState)

const body = (revision: number) => ({ schema_version: 1, session_key: 's', revision, last: null })

beforeEach(() => {
  sendState.mockReset()
  resetSendRaceForTests()
  resetSendRevisionSeedForTests()
})
afterEach(() => { vi.useRealTimers() })

describe('ensureSendRevision', () => {
  it('reads nothing when the revision is already known', async () => {
    noteSendRevision('s', 3)
    await ensureSendRevision('s')
    expect(sendState).not.toHaveBeenCalled()
  })

  it('records the revision the read answers', async () => {
    sendState.mockResolvedValue(body(7))
    await ensureSendRevision('s')
    expect(baseRevisionFor('s')).toBe(7)
  })

  it('shares one read between sends waiting on the same session', async () => {
    let answer: (v: ReturnType<typeof body>) => void = () => {}
    sendState.mockReturnValue(new Promise(resolve => { answer = resolve }))
    const both = Promise.all([ensureSendRevision('s'), ensureSendRevision('s')])
    answer(body(2))
    await both
    expect(sendState).toHaveBeenCalledTimes(1)
    expect(baseRevisionFor('s')).toBe(2)
  })

  it('stops waiting at the bound and still records a late answer', async () => {
    vi.useFakeTimers()
    let answer: (v: ReturnType<typeof body>) => void = () => {}
    sendState.mockReturnValue(new Promise(resolve => { answer = resolve }))
    const waiting = ensureSendRevision('s', 1000)
    await vi.advanceTimersByTimeAsync(1000)
    await waiting
    expect(baseRevisionFor('s')).toBeUndefined()
    answer(body(4))
    await vi.waitFor(() => expect(baseRevisionFor('s')).toBe(4))
  })

  it('resolves without a revision when the route answers nothing or fails', async () => {
    sendState.mockResolvedValueOnce(null)
    await ensureSendRevision('s')
    expect(baseRevisionFor('s')).toBeUndefined()
    sendState.mockRejectedValueOnce(new Error('offline'))
    await ensureSendRevision('s')
    expect(baseRevisionFor('s')).toBeUndefined()
  })
})
