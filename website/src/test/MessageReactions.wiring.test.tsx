/**
 * Reaction-row wiring on the two message components: the row renders only when
 * `onToggleReaction` is given, the bar shows the fixed four emoji as inline
 * one-click toggle buttons (the single-click reaction the issue requires; the
 * four-button row rides a recorded product-owner override of
 * `max-two-buttons-per-row`), each toggles in one click, an already-active
 * emoji is marked `aria-pressed`, and a failed reaction surfaces an inline
 * notice. `MessageReactionBar`'s own unit tests (chatReactions.test.tsx) cover
 * the control in isolation; this file covers the host components' opt-in gating
 * and prop plumbing.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import UserMessage from '../pages/chat/UserMessage'
import AssistantMessage from '../pages/chat/AssistantMessage'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(undefined) }))
vi.mock('../utils/shareUrl', () => ({ copySessionLink: vi.fn().mockResolvedValue(undefined) }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../hooks/useSmoothStream', () => ({ useSmoothStream: (content: string) => content }))

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { act(() => { vi.runAllTimers() }); vi.useRealTimers() })

const renderContent = (content: string) => <span data-testid="content">{content}</span>

const triggerFor = (emoji: string) =>
  screen.getAllByTestId('reaction-trigger').find(b => b.getAttribute('data-emoji') === emoji)!

describe('UserMessage reaction row', () => {
  it('does not render when onToggleReaction is absent', () => {
    render(<UserMessage content="hi" renderContent={renderContent} messageTs="ts-1" />)
    expect(screen.queryByTestId('message-reaction-bar')).not.toBeInTheDocument()
  })

  it('renders the four inline toggle buttons when onToggleReaction is given', () => {
    render(<UserMessage content="hi" renderContent={renderContent} messageTs="ts-1" onToggleReaction={() => {}} />)
    expect(screen.getAllByTestId('reaction-trigger')).toHaveLength(4)
  })

  it('calls onToggleReaction with the clicked emoji in one click', () => {
    const onToggleReaction = vi.fn()
    render(<UserMessage content="hi" renderContent={renderContent} messageTs="ts-1" onToggleReaction={onToggleReaction} />)
    fireEvent.click(triggerFor('👍'))
    expect(onToggleReaction).toHaveBeenCalledWith('👍')
  })

  it('marks an already-active emoji as pressed', () => {
    render(
      <UserMessage
        content="hi"
        renderContent={renderContent}
        messageTs="ts-1"
        onToggleReaction={() => {}}
        activeReactions={new Set(['✅'])}
      />,
    )
    expect(triggerFor('✅')).toHaveAttribute('aria-pressed', 'true')
    expect(triggerFor('👍')).toHaveAttribute('aria-pressed', 'false')
  })

  it('surfaces a reaction error inline', () => {
    render(
      <UserMessage
        content="hi"
        renderContent={renderContent}
        messageTs="ts-1"
        onToggleReaction={() => {}}
        reactionError={{ mid: 'm-1', kind: 'react' }}
      />,
    )
    expect(screen.getByText('Could not add the reaction. Try again.')).toBeInTheDocument()
  })

  it('does not render without a messageTs even with a handler', () => {
    render(<UserMessage content="hi" renderContent={renderContent} onToggleReaction={() => {}} />)
    expect(screen.queryByTestId('message-reaction-bar')).not.toBeInTheDocument()
  })
})

describe('AssistantMessage reaction row', () => {
  it('does not render when onToggleReaction is absent', () => {
    render(<AssistantMessage content="Hi" isStreaming={false} slotRunning={false} messageTs="ts-2" />)
    expect(screen.queryByTestId('message-reaction-bar')).not.toBeInTheDocument()
  })

  it('renders the four inline toggle buttons when onToggleReaction is given', () => {
    render(<AssistantMessage content="Hi" isStreaming={false} slotRunning={false} messageTs="ts-2" onToggleReaction={() => {}} />)
    expect(screen.getAllByTestId('reaction-trigger')).toHaveLength(4)
  })

  it('calls onToggleReaction with the clicked emoji in one click', () => {
    const onToggleReaction = vi.fn()
    render(<AssistantMessage content="Hi" isStreaming={false} slotRunning={false} messageTs="ts-2" onToggleReaction={onToggleReaction} />)
    fireEvent.click(triggerFor('👀'))
    expect(onToggleReaction).toHaveBeenCalledWith('👀')
  })

  it('marks an already-active emoji as pressed', () => {
    render(
      <AssistantMessage
        content="Hi"
        isStreaming={false}
        slotRunning={false}
        messageTs="ts-2"
        onToggleReaction={() => {}}
        activeReactions={new Set(['⭐'])}
      />,
    )
    expect(triggerFor('⭐')).toHaveAttribute('aria-pressed', 'true')
  })
})
