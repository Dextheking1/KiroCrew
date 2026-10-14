import { describe, it, expect } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { useRef } from 'react'
import { renderWithProviders, createTestStore } from './helpers'
import { setActiveId } from '../store/instancesSlice'
import SelectionToolbar, { useSelectionActions } from '../components/SelectionToolbar'

// The toolbar portals to document.body, outside the local dashboard wrapper a
// remote Crew hides with display:none, so it must hide itself while one shows.
function Harness() {
  const ref = useRef<HTMLDivElement>(null)
  const actions = useSelectionActions(() => {})
  return (
    <div ref={ref}>
      <SelectionToolbar containerRef={ref} actions={actions} externalSelection={{ text: 'hi', x: 10, y: 10 }} />
    </div>
  )
}

describe('SelectionToolbar across Crew switches', () => {
  it('hides while a remote Crew is showing and returns with the local one', async () => {
    const store = createTestStore()
    renderWithProviders(<Harness />, { store })
    expect(screen.getByRole('button', { name: 'Quote' })).toBeInTheDocument()

    act(() => { store.dispatch(setActiveId('remote-crew')) })
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Quote' })).not.toBeInTheDocument())

    act(() => { store.dispatch(setActiveId(null)) })
    expect(await screen.findByRole('button', { name: 'Quote' })).toBeInTheDocument()
  })

  it('renders without a Redux provider, treating the local dashboard as showing', () => {
    render(<Harness />)
    expect(screen.getByRole('button', { name: 'Quote' })).toBeInTheDocument()
  })
})
