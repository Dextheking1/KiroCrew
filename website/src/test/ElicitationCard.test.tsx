import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { vi } from 'vitest'
import ElicitationCard, {
  draftToContent,
  initialDraft,
  type ElicitationField,
} from '../pages/chat/ElicitationCard'
import { defaultMessageRenderers, type MessageRenderContext } from '../app-sdk/messageRenderers'
import type { ChatMessage } from '../types'

const post = vi.fn()
vi.mock(import('../api/apiTransport'), async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    apiTransport: { ...actual.apiTransport, post: (...args: unknown[]) => post(...args) },
  }
})

const FIELDS: ElicitationField[] = [
  { name: 'confirm', kind: 'boolean', required: true, title: 'Confirm', description: 'Delete it' },
  { name: 'count', kind: 'integer', required: false, minimum: 1, maximum: 5, default: 2 },
  { name: 'reason', kind: 'string', required: false },
  { name: 'tags', kind: 'multi_enum', required: false, options: [{ value: 'a', label: 'A' }, { value: 'b', label: 'B' }] },
]

function row(meta: Record<string, unknown>): ChatMessage {
  return { role: 'elicitation', content: 'Really delete probe.txt?', cls: JSON.stringify(meta), meta: { approval_id: 'rid-1', mode: 'form', message: 'Really delete probe.txt?', fields: FIELDS, ...meta } }
}

describe('draft helpers', () => {
  it('seeds the draft from server defaults, false for a boolean and [] for a multi-select', () => {
    expect(initialDraft(FIELDS)).toEqual({ confirm: false, count: 2, tags: [] })
  })

  it('reports the first required field left empty, by its title', () => {
    expect(draftToContent([{ name: 'who', kind: 'string', required: true, title: 'Who' }], {})).toEqual({ missing: 'Who' })
  })

  it('shapes numbers from their text and leaves optional empties out', () => {
    expect(draftToContent(FIELDS, { confirm: true, count: '3', reason: '', tags: [] })).toEqual({
      content: { confirm: true, count: 3 },
    })
    expect(draftToContent(FIELDS, { confirm: true, count: 'x' })).toEqual({ missing: 'count' })
  })
})

describe('ElicitationCard', () => {
  beforeEach(() => { post.mockReset(); post.mockResolvedValue(new Response('{"ok":true}')) })

  it('posts a validated form answer to the slot elicitation route', async () => {
    render(<ElicitationCard message={row({})} slot="dashboard:main" />)
    expect(screen.getByText('Really delete probe.txt?')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Confirm'))
    fireEvent.click(screen.getByRole('button', { name: /submit/i }))
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][0]).toBe('/api/chat/slots/dashboard%3Amain/elicitation')
    expect(post.mock.calls[0][1]).toEqual({
      request_id: 'rid-1',
      action: 'accept',
      content: { confirm: true, count: 2 },
    })
  })

  it('refuses to submit with a required field empty and says which, without a request', () => {
    // An unchecked required boolean is a valid `false`; a required STRING left
    // blank is the miss the card must catch before any round trip.
    const fields: ElicitationField[] = [{ name: 'who', kind: 'string', required: true, title: 'Who' }]
    render(<ElicitationCard message={row({ fields })} slot="dashboard:main" />)
    fireEvent.click(screen.getByRole('button', { name: /submit/i }))
    expect(screen.getByRole('alert')).toHaveTextContent('Who')
    expect(post).not.toHaveBeenCalled()
  })

  it('declines with no content', async () => {
    render(<ElicitationCard message={row({})} slot="dashboard:main" />)
    fireEvent.click(screen.getByRole('button', { name: /decline/i }))
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][1]).toEqual({ request_id: 'rid-1', action: 'decline' })
  })

  it('shows the gateway reason when the answer is refused and keeps the form open', async () => {
    const { ApiError } = await import('../api/apiError')
    const body = JSON.stringify({ error: "'count' is above its maximum", code: 'invalid_content' })
    post.mockRejectedValueOnce(new ApiError(400, "'count' is above its maximum", body))
    render(<ElicitationCard message={row({})} slot="dashboard:main" />)
    fireEvent.click(screen.getByLabelText('Confirm'))
    fireEvent.click(screen.getByRole('button', { name: /submit/i }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('above its maximum'))
    expect(screen.getByRole('button', { name: /submit/i })).toBeInTheDocument()
  })

  it('renders a URL ask as a link plus done/decline, and a resolved row read-only', () => {
    const url = row({ mode: 'url', url: 'https://example.com/confirm', fields: undefined })
    render(<ElicitationCard message={url} slot="dashboard:main" />)
    expect(screen.getByRole('link')).toHaveAttribute('href', 'https://example.com/confirm')
    expect(screen.getByRole('button', { name: /done/i })).toBeInTheDocument()
    const resolved = row({ resolved: 'decline' })
    const { container } = render(<ElicitationCard message={resolved} slot="dashboard:main" />)
    expect(container.querySelectorAll('button')).toHaveLength(0)
    expect(container.textContent).toMatch(/Declined/)
  })

  it('is claimed by the default renderer registry so an embed draws it instead of a bubble', () => {
    const entry = defaultMessageRenderers.find(r => r.roles.includes('elicitation'))
    expect(entry?.id).toBe('elicitation')
    const ctx = { row: (c: React.ReactNode) => <div data-testid="row">{c}</div>, wrapper: (c: React.ReactNode) => c } as unknown as MessageRenderContext
    render(<>{entry!.render(row({}), ctx)}</>)
    // Slot-less: the ask is drawn but cannot be answered from here.
    expect(screen.getByRole('button', { name: /submit/i })).toBeDisabled()
  })
})
