import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent, within } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import { CrewModeChip } from './CrewModeChip'
import CrewModeWelcome from './CrewModeWelcome'

describe('CrewModeChip', () => {
  it('reads off by default and reports its state through aria-pressed', () => {
    renderWithProviders(<CrewModeChip on={false} onToggle={vi.fn()} />)
    const chip = screen.getByTestId('crew-mode-chip')
    expect(chip).toHaveAttribute('aria-pressed', 'false')
    expect(chip).not.toHaveTextContent('Crew Mode on')
    // Only one off label is displayed: the short name below `sm`, the
    // explained one from `sm` up.
    expect(within(chip).getByText('Crew Mode')).toHaveClass('sm:hidden')
    expect(within(chip).getByText('Crew Mode: split into tasks')).toHaveClass('hidden', 'sm:inline')
  })

  it('says what turning it off does in its hint', () => {
    renderWithProviders(<CrewModeChip on onToggle={vi.fn()} />)
    expect(screen.getByTestId('crew-mode-chip')).toHaveAttribute(
      'title',
      'Turn off Crew Mode. This chat becomes a normal chat again; chats it already opened keep running.',
    )
  })

  it('says it is on when on', () => {
    renderWithProviders(<CrewModeChip on onToggle={vi.fn()} />)
    const chip = screen.getByTestId('crew-mode-chip')
    expect(chip).toHaveAttribute('aria-pressed', 'true')
    expect(chip).toHaveTextContent('Crew Mode on')
    // A visible "off" mark beside the label.
    expect(chip.querySelector('svg.lucide-x')).not.toBeNull()
  })

  it('calls onToggle on click', () => {
    const onToggle = vi.fn()
    renderWithProviders(<CrewModeChip on={false} onToggle={onToggle} />)
    fireEvent.click(screen.getByTestId('crew-mode-chip'))
    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  it('cannot be flipped while a reply is running', () => {
    const onToggle = vi.fn()
    renderWithProviders(<CrewModeChip on onToggle={onToggle} disabled />)
    const chip = screen.getByTestId('crew-mode-chip')
    expect(chip).toBeDisabled()
    fireEvent.click(chip)
    expect(onToggle).not.toHaveBeenCalled()
  })

  it('keeps its on look while a reply runs, stays focusable, and ignores clicks', () => {
    const onToggle = vi.fn()
    renderWithProviders(<CrewModeChip on onToggle={onToggle} busy />)
    const chip = screen.getByTestId('crew-mode-chip')
    // aria-disabled, not disabled: a keyboard still reaches it and hears its hint.
    expect(chip).not.toBeDisabled()
    expect(chip).toHaveAttribute('aria-disabled', 'true')
    expect(chip).toHaveAttribute('aria-pressed', 'true')
    expect(chip.className).not.toContain('opacity-')
    expect(chip).toHaveClass('aria-disabled:cursor-not-allowed')
    expect(chip).toHaveAttribute('title', 'Available after the reply.')
    fireEvent.click(chip)
    expect(onToggle).not.toHaveBeenCalled()
  })

  it('keeps one accessible name, with aria-pressed alone saying on or off', () => {
    const { unmount } = renderWithProviders(<CrewModeChip on={false} onToggle={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Crew Mode', pressed: false })).toBeInTheDocument()
    unmount()
    renderWithProviders(<CrewModeChip on onToggle={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Crew Mode', pressed: true })).toBeInTheDocument()
  })

  it('names the visible line that describes it', () => {
    renderWithProviders(<><CrewModeChip on onToggle={vi.fn()} describedBy="note" /><span id="note">Turning off Crew Mode makes this a normal chat. Chats it opened keep running.</span></>)
    expect(screen.getByRole('button', { name: 'Crew Mode' })).toHaveAccessibleDescription("Turning off Crew Mode makes this a normal chat. Chats it opened keep running.")
  })

  it('fades when disabled for any other reason', () => {
    renderWithProviders(<CrewModeChip on onToggle={vi.fn()} disabled />)
    expect(screen.getByTestId('crew-mode-chip')).toHaveClass('disabled:opacity-60')
  })
})

describe('CrewModeWelcome', () => {
  it('says what it is in big type and what it does in four short labels', () => {
    renderWithProviders(<CrewModeWelcome />)
    const page = screen.getByTestId('crew-mode-welcome')
    expect(within(page).getByRole('heading', { name: 'Crew Mode' })).toBeInTheDocument()
    expect(page).toHaveTextContent('You set the goal.')
    expect(page).toHaveTextContent('Your crew does the work.')
    for (const label of ['Opens chats', 'Shares updates', 'Takes requests', 'Shows progress']) {
      expect(within(page).getByText(label)).toBeInTheDocument()
    }
  })

  it('draws no sample board', () => {
    renderWithProviders(<CrewModeWelcome onOpenTaskBoard={vi.fn()} />)
    const page = screen.getByTestId('crew-mode-welcome')
    expect(within(page).queryByRole('region')).toBeNull()
    expect(within(page).queryByText('Your crew')).toBeNull()
    for (const sample of ['Example', 'Which day works for you?', 'Email', 'Needs you']) {
      expect(page).not.toHaveTextContent(sample)
    }
  })

  it('says the task board is in the side panel, with a button that opens it', () => {
    const onOpenTaskBoard = vi.fn()
    renderWithProviders(<CrewModeWelcome onOpenTaskBoard={onOpenTaskBoard} />)
    expect(screen.getByTestId('crew-mode-board-hint')).toHaveTextContent('Your task board shows in the side panel')
    fireEvent.click(screen.getByRole('button', { name: 'Open task board' }))
    expect(onOpenTaskBoard).toHaveBeenCalledTimes(1)
  })

  it('leaves the side panel line out when there is no board to open', () => {
    renderWithProviders(<CrewModeWelcome />)
    expect(screen.queryByTestId('crew-mode-board-hint')).toBeNull()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })
})
