import { memo } from 'react'
import { Plus, Check } from 'lucide-react'
import { REACTION_EMOJI } from '../../api/reactions'
import type { ReactionError } from '../../hooks/useChatReactions'
import ErrorNotice from '../../components/ErrorNotice'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from '../../components/ui/dropdown-menu'
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

// The two reactions kept inline, and the rest behind one overflow trigger.
// `max-two-buttons-per-row` (website/AUTOSDE.yaml) caps a button group at two
// action controls, and a DropdownMenu trigger counts as one, so the row is two
// inline toggles plus the single `+` trigger — exactly the rule's own "Good"
// shape. The two inline emoji stay one-click (the issue's #22482 motivation);
// the rest are one extra click in the overflow, which is the sanctioned cost of
// staying under the cap rather than a maintainer-only exception.
const PRIMARY_EMOJI = REACTION_EMOJI.slice(0, 2)
const OVERFLOW_EMOJI = REACTION_EMOJI.slice(2)

/**
 * Fixed reaction set (👍 ✅ 👀 ⭐), no picker, no custom emoji.
 *
 * Two emoji render inline as direct toggle buttons so the common reactions are
 * a SINGLE click; the rest live behind a `+` overflow menu so the group stays
 * within `max-two-buttons-per-row` (two inline toggles + one trigger). The row
 * sits on its OWN row under the bubble — never folded into the existing action
 * strip — so it never pushes those icons to wrap.
 *
 * A placed emoji stays filled past the hover window, so a reacted message reads
 * as a quick "where I left off" marker when scrolling back through a long reply
 * — the issue's stated motivation. A reaction placed from the overflow menu
 * highlights the `+` trigger so that marker survives even when it is not one of
 * the two inline emoji.
 */
const MessageReactionBar = memo(function MessageReactionBar({ active, onToggle, error, onClearError, className }: MessageReactionBarProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly so a language switch re-renders the labels
  // While a reaction error is showing, keep the controls visible even at rest:
  // the notice says "click the emoji to retry", so the emoji must not be faded
  // out when the notice is up (UX review).
  const forceVisible = error != null
  const overflowActive = OVERFLOW_EMOJI.some(e => active.has(e))
  const errorMessage = error
    ? error.kind === 'limit'
      ? i18nT('pages.chat.messageReactionBar.reaction_limit_reached')
      : error.kind === 'unreact'
        ? i18nT('pages.chat.messageReactionBar.unreaction_failed')
        : i18nT('pages.chat.messageReactionBar.reaction_failed')
    : null

  // A placed emoji is always opaque (the "where I left off" marker); an
  // unplaced control is transparent at rest and fades in on hover/focus of the
  // message row. `[@media(hover:none)]:opacity-100` is required, not cosmetic:
  // a touch device has no hover, so without it an unplaced control would stay
  // invisible forever and a tablet user could never place a FIRST reaction — on
  // touch the controls are simply always shown. (Classes stay inside
  // `className` rather than a `const`, which the i18n added-lines gate flags as
  // a bare literal.)
  const restClass = forceVisible
    ? 'opacity-100 hover:bg-bg-hover'
    : 'opacity-0 transition-opacity duration-300 delay-100 group-hover/msg:opacity-100 group-hover/msg:delay-300 group-focus-within/msg:opacity-100 group-focus-within/msg:delay-300 [@media(hover:none)]:opacity-100 hover:bg-bg-hover'

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
        {PRIMARY_EMOJI.map(emoji => {
          const isActive = active.has(emoji)
          const label = isActive
            ? i18nT('pages.chat.messageReactionBar.remove_reaction', { emoji })
            : i18nT('pages.chat.messageReactionBar.react_with', { emoji })
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
              className={`flex items-center justify-center w-6 h-6 rounded-md text-sm leading-none transition-colors bg-transparent border-none cursor-pointer [@media(hover:none)]:min-h-10 [@media(hover:none)]:min-w-10 ${isActive ? 'opacity-100 bg-accent-subtle ring-1 ring-accent/70' : restClass}`}
            >
              <span aria-hidden="true">{emoji}</span>
            </button>
          )
        })}
        {/* Overflow: the rest of the fixed set, one extra click. Counts as ONE
            control toward the cap no matter how many emoji it holds. Highlighted
            when a reaction placed from here is active, so the marker survives. */}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              data-testid="reaction-overflow-trigger"
              aria-label={i18nT('pages.chat.messageReactionBar.more_reactions')}
              title={i18nT('pages.chat.messageReactionBar.more_reactions')}
              className={`flex items-center justify-center w-6 h-6 rounded-md text-sm leading-none transition-colors bg-transparent border-none cursor-pointer [@media(hover:none)]:min-h-10 [@media(hover:none)]:min-w-10 ${overflowActive ? 'opacity-100 bg-accent-subtle ring-1 ring-accent/70' : restClass}`}
            >
              <Plus size={14} aria-hidden="true" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="min-w-[160px]">
            {OVERFLOW_EMOJI.map(emoji => {
              const isActive = active.has(emoji)
              const label = isActive
                ? i18nT('pages.chat.messageReactionBar.remove_reaction', { emoji })
                : i18nT('pages.chat.messageReactionBar.react_with', { emoji })
              return (
                <DropdownMenuItem
                  key={emoji}
                  data-testid="reaction-overflow-item"
                  data-emoji={emoji}
                  aria-label={label}
                  onSelect={() => onToggle(emoji)}
                >
                  <span aria-hidden="true" className="text-sm leading-none">{emoji}</span>
                  <span>{label}</span>
                  {isActive && <Check size={13} className="ml-auto shrink-0 text-accent" />}
                </DropdownMenuItem>
              )
            })}
          </DropdownMenuContent>
        </DropdownMenu>
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
