import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import type { ReactNode } from 'react'
import { api } from '../../../api/client'
import { MessageByline } from './MessageByline'

vi.mock('../../../api/client', () => ({ api: { sharedCrewViewer: vi.fn() } }))
const viewer = vi.mocked(api.sharedCrewViewer)

const person = (id: string, name: string, kind: 'human' | 'unknown' = 'human') => ({ id, kind, display: { name, login: '', captured_at: null } })
const authored = (id: string, name: string, kind: 'human' | 'unknown' = 'human') => ({ author: person(id, name, kind) })

function storeWith(slots: Array<Record<string, unknown>>) {
  const state = { dashboard: { slots } }
  return { getState: () => state, subscribe: () => () => {}, dispatch: (a: unknown) => a }
}

function mount(node: ReactNode, slots: Array<Record<string, unknown>> | null) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tree = <QueryClientProvider client={qc}>{node}</QueryClientProvider>
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return render(slots ? <Provider store={storeWith(slots) as any}>{tree}</Provider> : tree)
}

const SLOT = { key: 's1', attribution: { creator: person('p_ann', 'Ann'), availability: 'recorded', created_at: 1 } }

beforeEach(() => {
  vi.clearAllMocks()
  viewer.mockResolvedValue(person('p_bob', 'Bob'))
})

describe('MessageByline', () => {
  it('shows no label on a message from the session creator', async () => {
    mount(<MessageByline meta={authored('p_ann', 'Ann')} slotKey="s1" />, [SLOT])
    await waitFor(() => expect(viewer).toHaveBeenCalled())
    await act(() => new Promise(r => setTimeout(r, 20)))
    expect(screen.queryByTestId('message-byline')).toBeNull()
  })

  it('names a message from any other participant, the viewer included', async () => {
    mount(<MessageByline meta={authored('p_bob', 'Bob')} slotKey="s1" />, [SLOT])
    expect((await screen.findByTestId('message-byline')).textContent).toBe('Bob')
  })

  it('labels an author with no recorded person as Unattributed', async () => {
    mount(<MessageByline meta={authored('unknown', '', 'unknown')} slotKey="s1" />, [SLOT])
    expect((await screen.findByTestId('message-byline')).textContent).toBe('Unattributed')
  })

  it('treats the viewer as owner in a session with no recorded creator', async () => {
    mount(<MessageByline meta={authored('p_bob', 'Bob')} slotKey="s2" />, [{ key: 's2', attribution: null }])
    await act(() => new Promise(r => setTimeout(r, 20)))
    expect(screen.queryByTestId('message-byline')).toBeNull()
    mount(<MessageByline meta={authored('p_cy', 'Cy')} slotKey="s2" />, [{ key: 's2', attribution: null }])
    expect((await screen.findByTestId('message-byline')).textContent).toBe('Cy')
  })

  it('renders without a store and renders nothing for an unstamped message', async () => {
    mount(<MessageByline meta={authored('p_cy', 'Cy')} />, null)
    expect((await screen.findByTestId('message-byline')).textContent).toBe('Cy')
    const { container } = mount(<MessageByline meta={{}} />, null)
    expect(container.innerHTML).toBe('')
  })
})
