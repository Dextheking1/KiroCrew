/**
 * Isolated capture entry for message reactions.
 *
 * WHY ISOLATED: no gateway is needed to show the fixed emoji row — this
 * mounts the REAL `UserMessage` and `AssistantMessage` over the literal
 * ChatPage row chain (see `quote-message.tsx` for the shared `RowBox`
 * pattern), with local state standing in for `useChatReactions`.
 *
 * Query string: ?theme=dark|light&scene=idle|active|hover|error
 *   `idle`   — the row at rest with no reaction: the unplaced buttons are
 *              collapsed to zero size, so the message pays no extra height.
 *   `active` — 👍 already placed on the assistant reply (stays visible at rest,
 *              the "where I left off" marker), ⭐ on the user message.
 *   `hover`  — the full four-emoji row as a hover/focus reveals it (the harness
 *              forces the CSS reveal, see message-reactions.html), which is how
 *              a user places their first reaction.
 *   `error`  — the inline failure notice under the assistant reply.
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClientProvider, QueryClient } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import UserMessage from '../src/pages/chat/UserMessage'
import AssistantMessage from '../src/pages/chat/AssistantMessage'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const scene = params.get('scene') || 'idle'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
document.documentElement.setAttribute('data-mode', theme)

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const render = (c: string) => <MarkdownRenderer content={c} softBreaks />

const USER_TS = '2026-09-29T09:12:00Z'
const ASSISTANT_TS = '2026-09-29T09:12:40Z'
const ASSISTANT_TEXT = [
  'Three things landed in the composer:',
  '',
  '- Reactions are a fixed, one-click set — no picker.',
  '- A reaction stays visible once placed, so it doubles as a "where I left off" marker.',
  '- Pinning still works the same way it always did, independently.',
].join('\n')

function RowBox({ user, children }: { user?: boolean; children: React.ReactNode }) {
  return (
    <div className="px-4 mx-auto w-full py-1" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <div className={`group/msg flex flex-col min-w-0 ${user ? 'items-end' : ''}`}>
        <div className={`flex flex-col gap-0.5 min-w-0 overflow-hidden max-w-full ${user ? 'items-end' : ''}`}>{children}</div>
      </div>
    </div>
  )
}

function Scene() {
  const [userReactions, setUserReactions] = useState<Set<string>>(
    new Set(scene === 'active' ? ['⭐'] : []),
  )
  const [assistantReactions, setAssistantReactions] = useState<Set<string>>(
    new Set(scene === 'active' ? ['👍'] : []),
  )
  const toggle = (set: Set<string>, setter: (s: Set<string>) => void, emoji: string) => {
    const next = new Set(set)
    if (next.has(emoji)) next.delete(emoji); else next.add(emoji)
    setter(next)
  }
  // `error` scene: a failed-reaction notice on the assistant reply, so the
  // inline ErrorNotice ("Could not add the reaction. Try again.") is captured.
  const assistantError = scene === 'error' ? { mid: 'cap-assistant', kind: 'react' as const } : null
  return (
    <div className="bg-bg text-text flex flex-col" style={{ height: '100vh' }} data-capture-root data-scene={scene}>
      <div className="flex-1 overflow-y-auto py-4">
        <RowBox user>
          <UserMessage
            content="Can you show me the new reaction row on a reply?"
            messageTs={USER_TS}
            timestamp="09:12"
            timestampTitle="Today 09:12"
            slotKey="chat-1"
            slotTitle="Message reactions"
            renderContent={render}
            activeReactions={userReactions}
            onToggleReaction={(emoji) => toggle(userReactions, setUserReactions, emoji)}
          />
        </RowBox>
        <RowBox>
          <AssistantMessage
            content={ASSISTANT_TEXT}
            isStreaming={false}
            slotRunning={false}
            messageTs={ASSISTANT_TS}
            timestamp="09:12"
            timestampTitle="Today 09:12"
            slotKey="chat-1"
            slotTitle="Message reactions"
            activeReactions={assistantReactions}
            onToggleReaction={(emoji) => toggle(assistantReactions, setAssistantReactions, emoji)}
            reactionError={assistantError}
            onClearReactionError={() => {}}
          />
        </RowBox>
      </div>
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Scene />
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
