import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../../api/client'
import { ALL_UNAVAILABLE, type PeopleResponse } from '../../api/sharedCrewUi'
import { PeoplePanel } from './PeoplePanel'

vi.mock('../../api/client', () => ({ api: { sharedCrewPeople: vi.fn(), sharedCrewCapabilities: vi.fn() } }))
const people = vi.mocked(api.sharedCrewPeople)
const caps = vi.mocked(api.sharedCrewCapabilities)

const row = (id: string, name: string, over: Partial<PeopleResponse['people'][number]> = {}) => ({
  principal: { id, kind: 'human' as const, display: { name, login: `${id}@example.test`, captured_at: null } },
  sources: ['observed' as const], last_seen_at: 1_700_000_000, devices: 2, sessions_created: 3,
  is_viewer: false, usage: { active: 1, queued: 2 }, ...over,
})

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><PeoplePanel /></QueryClientProvider>)
}

beforeEach(() => vi.clearAllMocks())

describe('PeoplePanel', () => {
  it('lists observed people apart from the directory, with sources and usage, read-only', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available', membership_link: 'tailscale_admin' })
    people.mockResolvedValue({
      schema_version: 1, directory: { state: 'unavailable' }, usage_available: true,
      people: [row('p_a', 'Alice', { is_viewer: true }), row('p_b', 'Bob', { sources: ['session_creator'], last_seen_at: null })],
    })
    mount()
    expect(await screen.findByTestId('people-panel')).toBeInTheDocument()
    expect(screen.getByText('Alice')).toBeInTheDocument()
    expect(screen.getByText('You')).toBeInTheDocument()
    expect(screen.getByText('Seen connecting')).toBeInTheDocument()
    expect(screen.getByText('Created sessions')).toBeInTheDocument()
    expect(screen.getAllByTestId('person-usage')[0].textContent).toBe('1 running · 2 waiting')
    expect(screen.getByText(/has no assigned-user directory/)).toBeInTheDocument()
    expect(screen.getByTestId('tailscale-admin-link')).toHaveAttribute('href', 'https://login.tailscale.com/admin/users')
    // Nothing on this surface manages membership.
    expect(screen.queryAllByRole('button')).toHaveLength(0)
    expect(screen.queryByText(/add|remove|role|admin/i, { selector: 'button' })).toBeNull()
  })

  it('shows the console pointer only when the capability names it', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available', membership_link: 'console' })
    people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, usage_available: false, people: [] })
    mount()
    expect(await screen.findByTestId('console-pointer')).toBeInTheDocument()
    expect(screen.queryByTestId('tailscale-admin-link')).toBeNull()
    expect(screen.getByText(/cannot report how much work/)).toBeInTheDocument()
  })

  it('degrades when the Crew does not attribute', async () => {
    caps.mockResolvedValue(null)
    people.mockResolvedValue(null)
    mount()
    expect(await screen.findByTestId('people-unavailable')).toBeInTheDocument()
  })
})
