import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../../api/client'
import { CreatorFilteredSessions } from './CreatorFilter'
import { SessionCreatorByline } from './SessionCreatorByline'

vi.mock('../../api/client', () => ({ api: { sharedCrewSessions: vi.fn(), sharedCrewPeople: vi.fn(), sharedCrewCapabilities: vi.fn() } }))
const sessions = vi.mocked(api.sharedCrewSessions)

const attribution = (name: string) => ({ creator: { id: 'p_b', kind: 'human' as const, display: { name, login: 'b@example.test', captured_at: null } }, availability: 'recorded' as const, created_at: 1 })

function mount(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

beforeEach(() => vi.clearAllMocks())

describe('CreatorFilteredSessions', () => {
  it('asks the server to filter and page, and shows the server counts', async () => {
    sessions.mockResolvedValueOnce({ schema_version: 1, items: [{ key: 'k1', title: 'One', attribution: attribution('Bob') }], total_matching: 120, hidden_by_filter: 30, next_cursor: 'c2' })
    sessions.mockResolvedValueOnce({ schema_version: 1, items: [{ key: 'k2', title: 'Two' }], total_matching: 120, hidden_by_filter: 30, next_cursor: '' })
    const onOpen = vi.fn()
    mount(<CreatorFilteredSessions creator="mine" q=" deploy " connected onOpen={onOpen} onClear={() => {}} />)
    expect(await screen.findByText('One')).toBeInTheDocument()
    expect(sessions).toHaveBeenLastCalledWith({ creator: 'mine', q: 'deploy', cursor: undefined, limit: 50 })
    expect(screen.getByTestId('creator-filter-counts').textContent).toMatch(/120 matching · 30 hidden by this filter/)
    fireEvent.click(screen.getByRole('button', { name: 'More' }))
    expect(await screen.findByText('Two')).toBeInTheDocument()
    expect(sessions).toHaveBeenLastCalledWith({ creator: 'mine', q: 'deploy', cursor: 'c2', limit: 50 })
    // The second page renders what the server sent: no client-side filtering.
    expect(screen.getByText(/Unknown/)).toBeInTheDocument()
    fireEvent.click(screen.getByText('Two'))
    expect(onOpen).toHaveBeenCalledWith('k2', 'Two')
  })

  it('degrades to a plain note when the route is not served', async () => {
    sessions.mockResolvedValue(null)
    const onClear = vi.fn()
    mount(<CreatorFilteredSessions creator="unknown" q="" connected onOpen={() => {}} onClear={onClear} />)
    expect(await screen.findByTestId('creator-filter-unavailable')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Show all sessions' }))
    expect(onClear).toHaveBeenCalled()
  })

  it('shows an error through the shared notice', async () => {
    sessions.mockRejectedValue(new Error('boom'))
    mount(<CreatorFilteredSessions creator="all" q="" connected onOpen={() => {}} onClear={() => {}} />)
    await waitFor(() => expect(screen.getByTestId('creator-filter-error')).toBeInTheDocument())
  })
})

describe('SessionCreatorByline', () => {
  it('renders inline, names the creator, and says Unknown for a legacy session', () => {
    const { rerender } = render(<SessionCreatorByline attribution={attribution('Bob')} />)
    expect(screen.getByTestId('session-creator-byline').tagName).toBe('SPAN')
    expect(screen.getByTitle('Created by Bob (b@example.test)')).toBeInTheDocument()
    rerender(<SessionCreatorByline attribution={{ creator: { id: 'p_unknown', kind: 'unknown', display: { name: '', login: '', captured_at: null } }, availability: 'legacy_missing', created_at: null }} />)
    expect(screen.getByTitle('Created by Unknown')).toBeInTheDocument()
  })

  it('renders nothing without attribution', () => {
    const { container } = render(<SessionCreatorByline attribution={undefined} />)
    expect(container).toBeEmptyDOMElement()
  })
})
