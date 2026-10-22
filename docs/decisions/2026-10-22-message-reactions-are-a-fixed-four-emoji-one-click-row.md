# Message reactions are a fixed four-emoji one-click row, not a dropdown

Decided by: Rohan Khanderia (author, @RohanK6), on the message-reactions PR

## Decision

`MessageReactionBar` renders the fixed reaction set (👍 ✅ 👀 ⭐) as four inline
one-click toggle buttons in a single `role="group"` under the message bubble.
Placing or removing any reaction is a single click on its button.

This is an accepted exception to `website/AUTOSDE.yaml`'s
`max-two-buttons-per-row` default for this one control. It is not a general
relaxation of that rule: it applies only to the fixed reaction row in
`MessageReactionBar.tsx`.

## Why

- Issue [#22482](https://github.com/kirodotdev/KiroCrew/issues/22482) freezes
  the behaviour as reacting to a message **in a single click** — the point of
  the feature is to acknowledge a reply without typing one. A dropdown
  (open, then select) is two clicks and defeats that. The First Principles and
  UX review lanes flagged exactly this contradiction on an earlier dropdown
  revision, so collapsing the row to satisfy `max-two-buttons-per-row` would
  trade one blocking rule for another and break the frozen spec.
- The set is fixed and bounded — four known toggles that never grow — so it
  reads as one compact "how do I mark this message" control rather than the
  open-ended, rank-free button sprawl `max-two-buttons-per-row` exists to
  prevent. It never wraps and never folds into the existing action strip: it
  sits on its own row under the bubble, so it does not push the Copy / Pin /
  More icons onto a second line, which is the failure mode the rule guards
  against.

## Scope

- Applies only to the fixed four-emoji reaction control in
  `website/src/pages/chat/MessageReactionBar.tsx`.
- Does not change `max-two-buttons-per-row` or exempt any other row. A new
  action row elsewhere still obeys the cap, and growing this set past four
  would re-open this decision.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/22482 — the issue that freezes the single-click requirement.
- `website/AUTOSDE.yaml` — the `max-two-buttons-per-row` rule this entry records an exception to.
- `website/src/pages/chat/MessageReactionBar.tsx` — the control the decision governs.
