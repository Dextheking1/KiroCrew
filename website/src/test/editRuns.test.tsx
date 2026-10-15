import { describe, it, expect, beforeEach } from 'vitest'
import { fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ToolCallLine, { resetOpenedDiffCards } from '../pages/chat/ToolCallLine'
import { selectEditRuns } from '../pages/chat/editRuns'
import { decodePathForDisplay } from '../utils/pathBasename'
import { deriveToolCallTitle } from '../utils/toolCallTitle'
import type { RootState } from '../store'
import type { ChatMessage, ToolActivity } from '../types'

type ChatState = RootState['chat']

if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

beforeEach(() => { localStorage.clear(); resetOpenedDiffCards() })

const NO_LOG: ToolActivity[] = []
const BLOG = 'drafts/The%20Power%20of%20Running%20Remote%20Blog.md'

function diff(path: string, removed: string[], added: string[]): string {
  return [`--- ${path}`, `+++ ${path}`, `@@ -1,${removed.length + 1} +1,${added.length + 1} @@`, ' keep',
    ...removed.map(l => `-${l}`), ...added.map(l => `+${l}`)].join('\n')
}

function edit(id: string, input: string): ChatMessage {
  return { role: 'tool', content: '🔧 fs_write', cls: '', meta: { tool_call_id: id, kind: 'edit', input } }
}
const done = (id: string): ChatMessage => ({ role: 'tool', content: '✅ fs_write', cls: '', meta: { tool_call_id: id } })
const read = (id: string): ChatMessage => ({ role: 'tool', content: '🔧 fs_read', cls: '', meta: { tool_call_id: id, kind: 'read', input: '{"path":"a.md"}' } })

describe('selectEditRuns', () => {
  it('folds consecutive edits to one file into the first row', () => {
    const a = edit('a', diff(BLOG, ['x', 'y', 'z', 'w'], []))
    const b = edit('b', diff(BLOG, ['p'], ['q']))
    const c = edit('c', diff(BLOG, ['r'], ['s']))
    const thinking: ChatMessage = { role: 'thinking', content: 'next tweak', cls: '' }
    const msgs = [read('r'), done('r'), a, done('a'), thinking, b, done('b'), c]
    const runs = selectEditRuns(msgs, NO_LOG)
    expect(runs.membersByLead.get(a)).toEqual([a, b, c])
    expect([...runs.followers]).toEqual([b, c])
  })

  it('breaks the run on a visible row or a different file', () => {
    const a = edit('a', diff(BLOG, ['x'], ['y']))
    const b = edit('b', diff(BLOG, ['x'], ['y']))
    const other = edit('o', diff('drafts/other.md', ['x'], ['y']))
    const say: ChatMessage = { role: 'assistant', content: 'Tightened the intro.', cls: '' }
    expect(selectEditRuns([a, read('r'), b], NO_LOG).followers.size).toBe(0)
    expect(selectEditRuns([a, say, b], NO_LOG).followers.size).toBe(0)
    expect(selectEditRuns([a, other, b], NO_LOG).followers.size).toBe(0)
  })

  it('keeps an unapproved, rejected or denied call on its own row', () => {
    const a = edit('a', diff(BLOG, ['x'], ['y']))
    const b = edit('b', diff(BLOG, ['x'], ['y']))
    const pending: ChatMessage = { role: 'permission', content: 'approve?', cls: '', meta: { tool_call_id: 'b' } }
    const rejected: ChatMessage = { ...pending, meta: { tool_call_id: 'b', resolved: 'rejected' } }
    const approved: ChatMessage = { ...pending, meta: { tool_call_id: 'b', resolved: 'approved' } }
    const denied: ChatMessage = { role: 'tool', content: '🚫 fs_write — Blocked by security policy: x', cls: '', meta: { tool_call_id: 'b' } }
    expect(selectEditRuns([a, pending, b], NO_LOG).followers.size).toBe(0)
    expect(selectEditRuns([a, rejected, b], NO_LOG).followers.size).toBe(0)
    expect(selectEditRuns([a, b, denied], NO_LOG).followers.size).toBe(0)
    expect(selectEditRuns([a, approved, b], NO_LOG).followers.has(b)).toBe(true)
  })

  it('keeps a call the tool log marks rejected on its own row', () => {
    // A Stop rejects the in-flight call in the tool log only
    // (clearPendingPermissions); no permission or 🚫 message records it.
    const a = edit('a', diff(BLOG, ['x'], ['y']))
    const b = edit('b', diff(BLOG, ['x'], ['y']))
    const log: ToolActivity[] = [
      { type: 'tool', text: 'fs_write', ts: 1, tool_call_id: 'a', output: 'ok' },
      { type: 'tool', text: 'fs_write', ts: 2, tool_call_id: 'b', rejected: true },
    ]
    expect(selectEditRuns([a, b], log).followers.size).toBe(0)
    expect(selectEditRuns([a, b], NO_LOG).followers.has(b)).toBe(true)
    // Only the newest entry for a call counts, as for the row's own status.
    const replayed: ToolActivity[] = [...log, { type: 'tool', text: 'fs_write', ts: 3, tool_call_id: 'b', output: 'ok' }]
    expect(selectEditRuns([a, b], replayed).followers.has(b)).toBe(true)
  })

  it('keeps a row with a tool-risk badge on its own row', () => {
    const a = edit('a', diff(BLOG, ['x'], ['y']))
    const b = edit('b', diff(BLOG, ['x'], ['y']))
    const risky: ChatMessage = { ...b, meta: { ...b.meta, decisions_tool_risk: { turn_id: 't1', tool: 'fs_write', tier: 'risky', p: 0.9 } } }
    expect(selectEditRuns([a, risky], NO_LOG).followers.size).toBe(0)
    expect(selectEditRuns([risky, a], NO_LOG).followers.size).toBe(0)
  })

  it('does not merge a diff too large or truncated for a card', () => {
    const a = edit('a', diff(BLOG, ['x'], ['y']))
    const cut = edit('b', `${diff(BLOG, ['x'], ['y'])}\n\\ diff truncated`)
    expect(selectEditRuns([a, cut], NO_LOG).followers.size).toBe(0)
  })

  it('a repeated row for the same call hides with the run but adds no diff', () => {
    const a = edit('a', diff(BLOG, ['x'], ['y']))
    const again = edit('a', diff(BLOG, ['x'], ['y']))
    const runs = selectEditRuns([a, again], NO_LOG)
    expect(runs.followers.has(again)).toBe(true)
    expect(runs.membersByLead.size).toBe(0)
  })
})

describe('decodePathForDisplay', () => {
  it('decodes percent-escapes per segment', () => {
    expect(decodePathForDisplay(BLOG)).toBe('drafts/The Power of Running Remote Blog.md')
    expect(decodePathForDisplay('caf%C3%A9/men%C3%BC.md')).toBe('café/menü.md')
  })

  it('leaves a segment encoded when decoding would mislead or fails', () => {
    expect(decodePathForDisplay('a%2Fb.md')).toBe('a%2Fb.md')
    expect(decodePathForDisplay('a%0Ab.md')).toBe('a%0Ab.md')
    expect(decodePathForDisplay('a%E2%80%AEb.md')).toBe('a%E2%80%AEb.md')
    expect(decodePathForDisplay('100%.md')).toBe('100%.md')
    expect(decodePathForDisplay('x/%E0%A4%A.md')).toBe('x/%E0%A4%A.md')
  })

  it('decodes the path in a derived edit title', () => {
    const t = deriveToolCallTitle({ kind: 'edit', rawInput: diff(BLOG, ['x'], ['y']), title: 'fs_write' })
    expect(t.title).toContain('The Power of Running Remote Blog.md')
    expect(t.title).not.toContain('%20')
  })
})

describe('ToolCallLine edit runs', () => {
  function setup() {
    const a = edit('a', diff(BLOG, ['x', 'y', 'z', 'w'], []))
    const b = edit('b', diff(BLOG, ['p'], ['q']))
    const c = edit('c', diff(BLOG, ['r'], ['s']))
    const messages = [a, done('a'), b, done('b'), c, done('c')]
    const store = createTestStore({
      chat: { messages, toolLog: [], slotRunning: false } as unknown as ChatState,
    })
    return { a, b, c, store }
  }

  it('the lead row carries the run: one chip, summed counts, every diff', () => {
    const { a, store } = setup()
    const { container, getByText } = renderWithProviders(<ToolCallLine message={a} running={false} />, { store })
    const chip = container.querySelector<HTMLElement>('[data-testid="tool-diff-chip"]')!
    expect(chip.textContent).toContain('3 edits')
    expect(getByText('-6')).toBeTruthy()
    expect(getByText('+2')).toBeTruthy()
    fireEvent.click(chip)
    expect(container.querySelectorAll('.diff-block')).toHaveLength(3)
    expect(container.textContent).not.toContain('%20')
  })

  it('a follower row draws nothing', () => {
    const { b, c, store } = setup()
    expect(renderWithProviders(<ToolCallLine message={b} running={false} />, { store }).container.innerHTML).toBe('')
    expect(renderWithProviders(<ToolCallLine message={c} running={false} />, { store }).container.innerHTML).toBe('')
  })

  it('after a Stop rejects the second edit, both rows draw on their own', () => {
    const a = edit('a', diff(BLOG, ['x'], ['y']))
    const b = edit('b', diff(BLOG, ['p'], ['q']))
    const store = createTestStore({
      chat: {
        messages: [a, b],
        toolLog: [
          { type: 'tool', text: 'fs_write', ts: 1, tool_call_id: 'a', kind: 'edit', input: a.meta!.input as string, output: 'ok' },
          { type: 'tool', text: 'fs_write', ts: 2, tool_call_id: 'b', kind: 'edit', input: b.meta!.input as string, rejected: true },
        ],
        slotRunning: false,
      } as unknown as ChatState,
    })
    const lead = renderWithProviders(<ToolCallLine message={a} running={false} />, { store }).container
    const chip = lead.querySelector<HTMLElement>('[data-testid="tool-diff-chip"]')
    expect(chip).not.toBeNull()
    expect(chip!.textContent).not.toContain('edits')
    expect(renderWithProviders(<ToolCallLine message={b} running={false} />, { store }).container.innerHTML).not.toBe('')
  })

  it('shows the file name decoded, once', () => {
    const { a, store } = setup()
    const { container, getByTestId } = renderWithProviders(<ToolCallLine message={a} running={false} />, { store })
    const label = getByTestId('tool-pill-label').textContent ?? ''
    expect(label).toContain('The Power of Running Remote Blog.md')
    expect(container.textContent).not.toContain('%20')
    // The label names the file, so the chip's visible text does not repeat it;
    // the name stays its accessible name.
    const chip = container.querySelector<HTMLElement>('[data-testid="tool-diff-chip"]')!
    expect(chip.querySelector('.font-mono')).toBeNull()
    expect(chip.querySelector('.sr-only')?.textContent).toBe('The Power of Running Remote Blog.md')
  })
})
