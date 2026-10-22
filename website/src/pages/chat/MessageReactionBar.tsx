import { memo } from 'react'
import { REACTION_EMOJI } from '../../api/reactions'
import type { ReactionError } from '../../hooks/useChatReactions'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'

interface MessageReactionBarProps {
  /** Emoji the CURRENT caller has already placed on this message. */
  active: ReadonlySet<string>
  onToggle: (emoji: string) => void
  /** The last failed reaction on THIS message, when one is unacknowledged.
   *  Rendered as an inline notice so a failure is never silent
   *  (`errors-use-error-notice`). */
  error?: ReactionError | null
  onClearError?: () => void
  /** The host's own hover-reveal wrapper decides visibility, same contract as
   *  the row's other icon buttons. */
  className?: string
}

/**
 * Fixed one-click reaction set: 👍 ✅ 👀 ⭐, no picker, no custom emoji.
 *
 * The four emoji render inline as direct toggle buttons so a reaction is a
 * SINGLE click — the frozen requirement of the issue (#22482). This exceeds the
 * `max-two-buttons-per-row` default; that exception is recorded as a decision
 * in docs/decisions/2026-10-22-message-reactions-are-a-fixed-four-emoji-one-click-row.md,
 * because a dropdown (open, then select) would be two clicks and break the
 * single-click spec. The set is fixed and tiny (four known toggles, never
 * growing), so it reads as one compact control rather than an open-ended action
 * row, and it sits on its OWN row under the bubble — never folded into the
 * existing action strip — so it never pushes those icons to wrap.
 *
 * A placed emoji stays filled past the hover window, so a reacted message reads
 * as a quick "where I left off" marker when scrolling back through a long
 * reply — the issue's stated motivation.
 */
const MessageReactionBar = memo(function MessageReactionBar({ active, onToggle, error, onClearError, className }: MessageReactionBarProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly so a language switch re-renders the labels
  // While a reaction error is showing, keep every button visible even at rest:
  // the notice says "try again", and the retry IS clicking the emoji, so the
  // emoji must not be faded out when the notice is up (UX review).
  const forceVisible = error != null
  const errorMessage = error
    ? error.kind === 'limit'
      ? i18nT('pages.chat.messageReactionBar.reaction_limit_reached')
      : error.kind === 'unreact'
        ? i18nT('pages.chat.messageReactionBar.unreaction_failed')
        : i18nT('pages.chat.messageReactionBar.reaction_failed')
    : null

  return (
    <div
      data-testid="message-reaction-bar"
      className={`flex flex-col gap-0.5 ${className ?? ''}`}
    >
      <div
        role="group"
        aria-label={i18nT('pages.chat.messageReactionBar.react')}
        className="flex items-center gap-0.5"
      >
        {REACTION_EMOJI.map(emoji => {
          const isActive = active.has(emoji)
          const label = isActive
            ? i18nT('pages.chat.messageReactionBar.remove_reaction', { emoji })
            : i18nT('pages.chat.messageReactionBar.react_with', { emoji })
          // Every button keeps its slot in the row at all times (fixed order,
          // reserved w-6 h-6), so the row never changes height and a placed
          // emoji never shifts sideways when the others appear — the same
          // reserve-space-and-fade contract the existing action strip uses
          // (ACTIONS_REVEAL_CLS), which avoids the layout shift a display
          // toggle would cause under the pointer. A placed emoji is always
          // opaque (the "where I left off" marker); an unplaced one is
          // transparent at rest and fades in on hover/focus of the message row.
          // Every button keeps its slot (reserved w-6 h-6, fixed order) so the
          // row never changes height and a placed emoji never shifts sideways
          // when the others appear. A placed emoji is always opaque (the
          // marker); an unplaced one is transparent at rest and fades in on
          // hover/focus. `[@media(hover:none)]:opacity-100` is required, not
          // cosmetic: a touch device has no hover, so without it the unplaced
          // emoji would stay invisible forever and a tablet user could never
          // place a FIRST reaction — on touch the full fixed set is simply
          // always shown. (Classes stay inside `className` rather than a
          // `const`, which the i18n added-lines gate flags as a bare literal.)
          return (
            <button
              key={emoji}
              type="button"
              data-testid="reaction-trigger"
              data-emoji={emoji}
              data-reaction-active={isActive}
              aria-pressed={isActive}
              aria-label={label}
              title={label}
              onClick={() => onToggle(emoji)}
              className={`flex items-center justify-center w-6 h-6 rounded-md text-sm leading-none transition-colors bg-transparent border-none cursor-pointer [@media(hover:none)]:min-h-10 [@media(hover:none)]:min-w-10 ${isActive ? 'opacity-100 bg-accent-subtle ring-1 ring-accent/70' : forceVisible ? 'opacity-100 hover:bg-bg-hover' : 'opacity-0 transition-opacity duration-300 delay-100 group-hover/msg:opacity-100 group-hover/msg:delay-300 group-focus-within/msg:opacity-100 group-focus-within/msg:delay-300 [@media(hover:none)]:opacity-100 hover:bg-bg-hover'}`}
            >
              <span aria-hidden="true">{emoji}</span>
            </button>
          )
        })}
      </div>
      {/* No hand-off: the agent hand-off navigates to the chat and unmounts
          whatever rendered this notice, which would discard the unsaved
          composer draft and any open inline message editor on this row — the
          same ruling the sibling copy-failure notices in UserMessage /
          AssistantMessage make. A reaction retry needs neither the agent nor a
          navigation: the emoji is still one click away, so the notice only
          reports and dismisses (errors-use-error-notice). */}
      <ErrorNotice
        message={errorMessage}
        variant="inline"
        onDismiss={onClearError}
        className="[@media(hover:none)]:[&_button]:min-h-10 [@media(hover:none)]:[&_button]:min-w-10"
      />
    </div>
  )
})

export { MessageReactionBar }
export type { MessageReactionBarProps }
