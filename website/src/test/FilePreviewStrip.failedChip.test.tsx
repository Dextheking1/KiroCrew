/**
 * The gateway's refusal of a staged picture ends with "Remove it and try
 * again", so the control it points at must be there to find: on a chip in the
 * failed state the remove button is visible at rest (not hover-gated), names
 * the picture it removes, and works from the keyboard. A chip the gateway did
 * not refuse keeps the hover-gated control it always had.
 */
import React from 'react'
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { FilePreviewStrip } from '../components/chat-input/FilePreviewStrip'

afterEach(cleanup)

const FIRST = '/tmp/uploads/first.png'
const SECOND = '/tmp/uploads/second.png'

describe('FilePreviewStrip: a refused picture keeps its remove control in reach', () => {
  it('shows the refused chip\'s remove control at rest and names the picture', () => {
    render(<FilePreviewStrip files={[FIRST, SECOND]} failed={[SECOND]} onRemove={() => {}} />)
    const failedRemove = screen.getByRole('button', { name: /^Remove second\.png/ })
    expect(failedRemove.className).not.toMatch(/opacity-0/)
    expect(failedRemove.getAttribute('aria-label')).toMatch(/couldn't be attached/)
  })

  it('leaves an unrefused chip\'s remove control hover-gated', () => {
    render(<FilePreviewStrip files={[FIRST, SECOND]} failed={[SECOND]} onRemove={() => {}} />)
    const plainRemove = screen.getAllByRole('button', { name: 'Remove' })
    expect(plainRemove).toHaveLength(1)
    expect(plainRemove[0].className).toMatch(/opacity-0/)
  })

  it('removes the refused picture from the keyboard', () => {
    const onRemove = vi.fn()
    render(<FilePreviewStrip files={[FIRST, SECOND]} failed={[SECOND]} onRemove={onRemove} />)
    const failedRemove = screen.getByRole('button', { name: /^Remove second\.png/ })
    failedRemove.focus()
    expect(document.activeElement).toBe(failedRemove)
    fireEvent.click(failedRemove)
    expect(onRemove).toHaveBeenCalledWith(SECOND)
  })

  it('marks nothing when no picture was refused', () => {
    render(<FilePreviewStrip files={[FIRST, SECOND]} onRemove={() => {}} />)
    expect(screen.getAllByRole('button', { name: 'Remove' })).toHaveLength(2)
    expect(screen.queryByRole('button', { name: /couldn't be attached/ })).toBeNull()
  })
})
