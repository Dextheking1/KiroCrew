/**
 * First submit wins, in the single-chat composer.
 *
 * A send carries the session revision this tab last saw. When the host answers
 * `send_race` (a collaborator's send was accepted first) the draft comes back
 * to the composer untouched, no bubble or error row is left behind, and a
 * notice names who sent first with Send now. Send now is a new send at the
 * current revision that never steers. An overload refusal reads differently:
 * an error row with the limit, the draft back, and no race notice.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { store as appStore } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { baseRevisionFor, noteSendRevision, resetSendRaceForTests } from '../api/sendRace'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
const sendChat = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: (...a: unknown[]) => sendChat(...a),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    knowledgeSources: vi.fn().mockResolvedValue({ sources: [] }),
    createChatSlot: vi.fn(),
  },
  SEARCH_MIN_CHARS: 2,
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  },
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slotsLoaded: true,
        slots: [{ key: 'slot-a', messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: '',
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

async function renderPage(store: ReturnType<typeof makeStore>) {
  vi.spyOn(appStore, 'getState').mockImplementation(store.getState)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter initialEntries={['/chat']}><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
}

async function sendText(text: string) {
  const box = screen.getByLabelText('Message input')
  fireEvent.change(box, { target: { value: text } })
  await act(async () => { fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' }) })
}

const input = () => screen.getByLabelText('Message input') as HTMLTextAreaElement
const userRow = (store: ReturnType<typeof makeStore>) => store.getState().chat.messages.find(m => m.role === 'user')

const WINNER = {
  revision: 4,
  principal: { id: 'p-alice', kind: 'human', display: { name: 'alice', login: 'alice@example.test', captured_at: null } },
  message_id: 'm-alice',
  accepted_at: 1,
}
const RACE = {
  ok: false,
  status: 409,
  json: () => Promise.resolve({ error: 'someone else sent first', code: 'send_race', session_key: 'slot-a', current_revision: 4, winner: WINNER }),
}
type RaceArg = { baseRevision?: number; sendNow?: boolean; idempotencyKey?: string }

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  sendChat.mockReset()
  resetSendRaceForTests()
})
afterEach(() => vi.restoreAllMocks())

describe('a send that loses a send race', { timeout: 20_000 }, () => {
  it('keeps the draft, names who sent first, and leaves nothing sent', async () => {
    noteSendRevision('slot-a', 3)
    sendChat.mockResolvedValue(RACE)
    const store = makeStore()
    await renderPage(store)
    await sendText('my draft')
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    const race = sendChat.mock.calls[0][7] as RaceArg
    expect(race.baseRevision).toBe(3)
    expect(race.sendNow).toBe(false)
    expect(race.idempotencyKey).toMatch(/^send-s-/)
    await waitFor(() => expect(screen.getByTestId('send-race-notice')).toBeTruthy())
    expect(screen.getByTestId('send-race-notice').textContent).toContain('alice sent first')
    expect(screen.getByTestId('send-race-notice').getAttribute('data-winner-message')).toBe('m-alice')
    expect(input().value).toBe('my draft')
    expect(userRow(store)).toBeUndefined()
    expect(store.getState().chat.messages.some(m => m.role === 'error')).toBe(false)
    expect(store.getState().chat.slotRunning).toBe(false)
    expect(baseRevisionFor('slot-a')).toBe(4)
    // Nothing is sent again on its own.
    expect(sendChat).toHaveBeenCalledTimes(1)
  })

  it('Send now sends the kept draft at the current revision and never steers', async () => {
    sendChat.mockResolvedValueOnce(RACE)
    const store = makeStore()
    await renderPage(store)
    await sendText('my draft')
    await waitFor(() => expect(screen.getByTestId('send-race-send-now')).toBeTruthy())
    sendChat.mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ ok: true, queued: true, queue_id: 'q-1' }) })
    await act(async () => { fireEvent.click(screen.getByTestId('send-race-send-now')) })
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(2))
    const [message, slot, , , , steer, , race] = sendChat.mock.calls[1]
    expect(message).toBe('my draft')
    expect(slot).toBe('slot-a')
    expect(steer).toBeUndefined()
    expect(race).toMatchObject({ baseRevision: 4, sendNow: true })
    expect((race as RaceArg).idempotencyKey).not.toBe((sendChat.mock.calls[0][7] as RaceArg).idempotencyKey)
    await waitFor(() => expect(screen.queryByTestId('send-race-notice')).toBeNull())
    expect(store.getState().chat.messages.some(m => m.role === 'error')).toBe(false)
  })

  it('Dismiss hides the notice and keeps the draft', async () => {
    sendChat.mockResolvedValue(RACE)
    const store = makeStore()
    await renderPage(store)
    await sendText('keep me')
    await waitFor(() => expect(screen.getByTestId('send-race-dismiss')).toBeTruthy())
    await act(async () => { fireEvent.click(screen.getByTestId('send-race-dismiss')) })
    expect(screen.queryByTestId('send-race-notice')).toBeNull()
    expect(input().value).toBe('keep me')
    expect(sendChat).toHaveBeenCalledTimes(1)
    expect(userRow(store)).toBeUndefined()
  })

  it('an overload refusal reads as a limit, not a race', async () => {
    sendChat.mockResolvedValue({ ok: false, status: 429, json: () => Promise.resolve({ error: 'limit', code: 'principal_active_limit' }) })
    const store = makeStore()
    await renderPage(store)
    await sendText('over the limit')
    await waitFor(() => expect(store.getState().chat.messages.some(m => m.role === 'error')).toBe(true))
    expect(screen.queryByTestId('send-race-notice')).toBeNull()
    expect(input().value).toBe('over the limit')
  })
})
