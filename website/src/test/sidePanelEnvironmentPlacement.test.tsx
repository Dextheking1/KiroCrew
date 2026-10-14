import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { api, type SlotEnvironment } from '../api/client'
import { slotEnvironmentQueryKey } from '../components/sessionEnvironmentHooks'
import { __resetPanelTabs, usePanelTabs } from '../hooks/usePanelTabs'
import { createTestStore } from './helpers'

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/FilesHomePanel', () => ({ default: () => <div data-testid="gateway-files" /> }))
vi.mock('../pages/chat/RemoteFilesPanel', () => ({ default: () => <div data-testid="remote-files" /> }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../components/CliPanel', () => ({
  default: () => <div data-testid="terminal-body" />,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => true,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../hooks/useDevMode', () => ({ useDevMode: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

import SidePanel from '../pages/chat/SidePanel'

const SLOT = 'placement-slot'
const openTerminal = vi.fn()
const clients: QueryClient[] = []
const remote: SlotEnvironment = {
  profile: 'dev',
  profiles: [{ id: 'dev', name: 'Verified workspace', provider: 'coder', workdir: '/workspace/app' }],
}
const local: SlotEnvironment = { profile: null, profiles: [] }

function Harness() {
  const controller = usePanelTabs(SLOT)
  const openView = controller.openView
  useEffect(() => { openView('files') }, [openView])
  return <SidePanel slot={SLOT} projectDir="/gateway/project"
    tabsCtl={{ ...controller, openTerminal }} onFileSave={async () => {}} />
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  clients.push(client)
  render(<QueryClientProvider client={client}><Provider store={createTestStore()}>
    <Harness />
  </Provider></QueryClientProvider>)
  return client
}

function filesReady(files: string) {
  // Placement query (including retries) -> React.lazy file panel -> rendered file view.
  return screen.findByTestId(files, {}, { timeout: 3000 })
}

function openMenu() {
  fireEvent.pointerDown(screen.getByRole('button', { name: 'Open side panel tab' }),
    { button: 0, ctrlKey: false, pointerType: 'mouse' })
}

/** The menu offers no Terminal, and none was opened. */
function expectNoTerminal() {
  openMenu()
  expect(screen.queryByRole('menuitem', { name: 'Terminal' })).toBeNull()
  expect(openTerminal).not.toHaveBeenCalled()
}
const envRead = () => vi.spyOn(api, 'chatSlotEnvironment')
const placementAlert = () => screen.findByRole('alert', {}, { timeout: 3000 })

beforeEach(() => {
  localStorage.clear(); __resetPanelTabs(); openTerminal.mockClear()
  vi.spyOn(api, 'listApps').mockResolvedValue([])
})
afterEach(() => { cleanup(); clients.splice(0).forEach(client => client.clear()); vi.restoreAllMocks() })

describe('placement gates gateway file and terminal access', () => {
  it('withholds actions while unread and after an initial read failure', async () => {
    let reject!: (error: Error) => void
    envRead().mockReturnValue(new Promise((_resolve, fail) => { reject = fail }))
    const client = mount()
    await waitFor(() => expect(client.getQueryState(slotEnvironmentQueryKey(SLOT))?.fetchStatus).toBe('fetching'))
    expect(screen.queryByTestId('gateway-files')).toBeNull()
    openMenu()
    expect(screen.queryByRole('menuitem', { name: 'Terminal' })).toBeNull()
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })
    await act(async () => { reject(new Error('choices_unreadable')) })
    // A fixed explanation, never the raw backend error.
    const alert = await placementAlert()
    expect(alert).toHaveTextContent("Couldn't read where this chat runs. Retry to load its files.")
    expect(alert).not.toHaveTextContent('choices_unreadable')
    expect(screen.queryByTestId('gateway-files')).toBeNull()
    expect(screen.queryByTestId('terminal-body')).toBeNull()
    expectNoTerminal()
  })

  it('re-reads the placement on Retry and opens Files only once it succeeds', async () => {
    const get = envRead().mockRejectedValue(new Error('choices_unreadable'))
    mount()
    await placementAlert()
    const calls = get.mock.calls.length
    get.mockResolvedValue(local)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await filesReady('gateway-files')
    expect(get.mock.calls.length).toBeGreaterThan(calls)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it.each([
    ['local', local, 'gateway-files', { cwd: '/gateway/project' }],
    ['remote', remote, 'remote-files', { remoteSlot: SLOT, title: 'Verified workspace' }],
  ] as const)('opens only the confirmed %s destination', async (_kind, response, files, destination) => {
    envRead().mockResolvedValue(response)
    mount()
    await filesReady(files)
    openMenu()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Terminal' }))
    expect(openTerminal).toHaveBeenCalledExactlyOnceWith(destination)
  })

  it('retries one failed read before replacing a local chat\'s Files', async () => {
    envRead().mockRejectedValueOnce(new Error('blip')).mockResolvedValue(local)
    mount()
    await filesReady('gateway-files')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('does not trust cached local placement after a failed refresh', async () => {
    const get = envRead().mockResolvedValue(local)
    const client = mount()
    await filesReady('gateway-files')
    get.mockRejectedValue(new Error('refresh refused'))
    await act(async () => { await client.invalidateQueries({ queryKey: slotEnvironmentQueryKey(SLOT) }) })
    expect(await placementAlert()).toHaveTextContent('read where this chat runs')
    expect(screen.queryByTestId('gateway-files')).toBeNull()
    expectNoTerminal()
  })

  it('never infers local from a successful response without a placement', async () => {
    envRead().mockResolvedValue({ profiles: [] } as unknown as SlotEnvironment)
    const client = mount()
    await waitFor(() => expect(client.getQueryState(slotEnvironmentQueryKey(SLOT))?.status).toBe('success'))
    expect(screen.queryByTestId('gateway-files')).toBeNull()
    expectNoTerminal()
  })

  // The read stays on with the preview flag off: the flag is per browser, while a
  // chat's binding (and the gateway's default profile) live on the gateway.
  it('resolves an unconfigured gateway to local Files with no error, preview off', async () => {
    expect(localStorage.length).toBe(0)
    // What the gateway answers with no Coder URL and no choices file.
    const get = envRead().mockResolvedValue({ profile: null, profiles: [], starting: null, locked: false, attached: false } as SlotEnvironment)
    mount()
    await filesReady('gateway-files')
    expect(screen.queryByRole('alert')).toBeNull()
    expect(get).toHaveBeenCalledTimes(1)
    openMenu()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Terminal' }))
    expect(openTerminal).toHaveBeenCalledExactlyOnceWith({ cwd: '/gateway/project' })
  })
})
