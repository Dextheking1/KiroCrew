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

/**
 * Fixed reaction set (👍 ✅ 👀 ⭐), no picker, no custom emoji.
 *
 * The row is two separated regions:
 *   1. PLACED reactions — one chip per emoji the caller has placed, drawn as
 *      its own glyph and always visible (hover or not), so a reacted message
 *      reads as a "where I left off" marker when scrolling back (issue #22482).
 *      Clicking a chip removes that reaction. This is the message's reaction
 *      STATE, the universal applied-reaction-pill pattern, not an action row.
 *   2. ADD — a single `+` trigger that opens the fixed set to place a reaction
 *      not yet on the message. It is the only add control, stays neutral, and
 *      reveals on hover/focus like the row's other icon buttons.
 *
 * Why this shape: `max-two-buttons-per-row` (website/AUTOSDE.yaml) caps a row of
 * peer ACTION controls at two, and the add affordance here is exactly one (the
 * `+` trigger counts as one however many emoji it holds). The placed chips are
 * the current-state display the marker needs — an earlier revision that only
 * ringed the `+` when an overflow emoji was placed hid the chosen emoji, so the
 * marker read as an empty "add" control (UX review). Showing each placed emoji
 * as its own chip is that review's prescribed fix.
 */
const MessageReactionBar = memo(function MessageReactionBar({ active, onToggle, error, onClearError, className }: MessageReactionBarProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly so a language switch re-renders the labels
  // While a reaction error is showing, keep the add control visible even at
  // rest: the notice says "click it to retry", so the + must not be faded out.
  const forceVisible = error != null
  // Placed emoji in the fixed set's own order, so the chips never reshuffle.
  const placed = REACTION_EMOJI.filter(e => active.has(e))
  const unplaced = REACTION_EMOJI.filter(e => !active.has(e))
  const errorMessage = error
    ? error.kind === 'limit'
      ? i18nT('pages.chat.messageReactionBar.reaction_limit_reached')
      : error.kind === 'unreact'
        ? i18nT('pages.chat.messageReactionBar.unreaction_failed')
        : error.emoji
          ? i18nT('pages.chat.messageReactionBar.reaction_failed_emoji', { emoji: error.emoji })
          : i18nT('pages.chat.messageReactionBar.reaction_failed')
    : null

  // The `+` trigger is transparent at rest and fades in on hover/focus of the
  // message row. `[@media(hover:none)]:opacity-100` is required, not cosmetic:
  // a touch device has no hover, so without it the add control would stay
  // invisible forever and a tablet user could never place a FIRST reaction — on
  // touch it is simply always shown. (Classes stay inside `className` rather
  // than a `const`, which the i18n added-lines gate flags as a bare literal.)
  const addRestClass = forceVisible
    ? 'opacity-100 hover:bg-bg-hover'
    : 'opacity-0 transition-opacity duration-300 delay-100 group-hover/msg:opacity-100 group-hover/msg:delay-300 group-focus-within/msg:opacity-100 group-focus-within/msg:delay-300 [@media(hover:none)]:opacity-100 hover:bg-bg-hover'

  return (
    <div
      data-testid="message-reaction-bar"
      className={`flex flex-col gap-0.5 ${className ?? ''}`}
    >
      <div className="flex items-center gap-0.5">
        {/* Placed reactions: the message's reaction STATE, always visible. */}
        {placed.length > 0 && (
          <div
            role="group"
            aria-label={i18nT('pages.chat.messageReactionBar.your_reactions')}
            data-testid="reaction-placed-group"
            className="flex items-center gap-0.5"
          >
            {placed.map(emoji => {
              const label = i18nT('pages.chat.messageReactionBar.remove_reaction', { emoji })
              return (
                <button
                  key={emoji}
                  type="button"
                  data-testid="reaction-chip"
                  data-emoji={emoji}
                  data-reaction-active="true"
                  aria-pressed="true"
                  aria-label={label}
                  title={label}
                  onClick={() => onToggle(emoji)}
                  className="flex items-center justify-center w-6 h-6 rounded-md text-sm leading-none transition-colors bg-accent-subtle ring-1 ring-accent/70 border-none cursor-pointer opacity-100 [@media(hover:none)]:min-h-10 [@media(hover:none)]:min-w-10"
                >
                  <span aria-hidden="true">{emoji}</span>
                </button>
              )
            })}
          </div>
        )}
        {/* Add: the single control that opens the fixed set to place a reaction
            not yet on the message. Counts as ONE toward max-two-buttons-per-row
            however many emoji it holds; hidden when every emoji is placed. */}
        {unplaced.length > 0 && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                data-testid="reaction-add-trigger"
                aria-label={i18nT('pages.chat.messageReactionBar.react')}
                title={i18nT('pages.chat.messageReactionBar.react')}
                className={`flex items-center justify-center w-6 h-6 rounded-md text-sm leading-none transition-colors bg-transparent border-none cursor-pointer [@media(hover:none)]:min-h-10 [@media(hover:none)]:min-w-10 ${addRestClass}`}
              >
                <Plus size={14} aria-hidden="true" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="min-w-[180px]">
              {REACTION_EMOJI.map(emoji => {
                const isActive = active.has(emoji)
                const label = isActive
                  ? i18nT('pages.chat.messageReactionBar.remove_reaction', { emoji })
                  : i18nT('pages.chat.messageReactionBar.react_with', { emoji })
                return (
                  <DropdownMenuItem
                    key={emoji}
                    data-testid="reaction-menu-item"
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
        )}
      </div>
      {/* No hand-off: the agent hand-off navigates to the chat and unmounts
          whatever rendered this notice, which would discard the unsaved
          composer draft and any open inline message editor on this row — the
          same ruling the sibling copy-failure notices in UserMessage /
          AssistantMessage make. A reaction retry needs neither the agent nor a
          navigation: the emoji is still one click away (the placed chip to
          remove, the `+` menu to add), so the notice only reports and dismisses
          (errors-use-error-notice). */}
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
