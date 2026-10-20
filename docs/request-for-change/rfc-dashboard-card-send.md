---
title: A Dashboard card can send its reply in one click
status: draft
author: chenmingwei23
created: 2026-10-20
last-audited: 2026-10-20
audited-at: 999fcd1292
doc-pr: 22493
implementation-prs: [22379]
tracking-issues: [22367]
supersedes: []
superseded-by: []
---

# RFC: A Dashboard Card Can Send Its Reply in One Click

> **Status:** `draft`. Acceptance is requested from a maintainer; the status
> flips to `accepted` when one records it in section 7 with the date. Nothing is
> on main. Verified at `999fcd1292`: in
> `website/src/pages/members/CrewDynamicDashboard.tsx`, a page's
> `kirocrew-dashboard:act` message is validated by `actText` and handed to `onAct`,
> which `MembersPageSidePanel.tsx` implements as `suggestPaneDraft` plus
> `revealComposer`; `dashboard_frame.py` exports no page-to-host type for `act`;
> `dashboard/card_send.py` does not exist. Nothing a page can post sends a turn.
> The implementation is
> [#22379](https://github.com/kirodotdev/KiroCrew/pull/22379), which waits for this
> document. This document lands first because it adds a message to the page-to-host
> contract and lets a page cause a user-role turn, and the First Principles lane
> reads that decision from the base branch.

## 1. Summary

A button the crewmate's page draws can ask the host to send its reply into that
crewmate's own thread. Today the same button can only put the reply in the chat
box, and the person then switches to the chat and presses send. The new message,
`kirocrew-dashboard:send {text}`, is a request, not a command. The host sends
only after a recent real interaction while the page's frame holds focus, only into
the thread of the tab the page is shown in, and the gateway refuses it in every state where a person's typed
words would mean more than the page's. Every refusal falls back to today's
behaviour, so a reply is never lost.

## 2. Background

A Dashboard page is HTML the crewmate wrote. It runs in a frame with
`allow-scripts` and nothing else, on an opaque origin, with `connect-src 'none'`.
Its only channels to the host are postMessage types. Three are in use:

- `kirocrew-dashboard:ready` and `kirocrew-dashboard:refetch`, which carry no
  user content (`dashboard_frame.READY_MESSAGE_TYPE`, `REFETCH_MESSAGE_TYPE`).
- `kirocrew-dashboard:act {text}`, which the host turns into a composer draft.
- `kirocrew-dashboard:open {url}`, which opens one GitHub pull-request URL, and
  only with a live user activation (`useFrameOpenLink`).

`act` is fill-only because a page that could speak as the person would let a
crewmate answer for them. That line is correct and this RFC keeps it: `send` does
not make the page the person. It lets the person's click, which the page cannot
fake, carry the page's words into the thread, tagged as the page's.

For the cards the built-in pages draw (a needs-you row with offered answers, a
review row with two buttons), the second step adds nothing: the person chose the
reply by clicking it. Issue
[#22367](https://github.com/kirodotdev/KiroCrew/issues/22367) asked for the click
to send.

## 3. Design

### 3.1 The message

`{type: 'kirocrew-dashboard:send', text}`. The text rules are `act`'s: trimmed,
at most 600 characters, no control character except a newline. The type is
exported as `dashboard_frame.SEND_MESSAGE_TYPE` (and `ACT_MESSAGE_TYPE`, which the
frame did not export before), pinned to the tab's literals by a test. The message
names no target, no session and no agent.

### 3.2 What the host does

The host sends only when all of these hold, and otherwise behaves as `act`:

- The message comes from the frame's own window (as `act` already requires).
- The browser reports transient user activation (`navigator.userActivation.isActive`)
  AND the page's frame holds focus (`hasGesture`, the test the pull-request link
  bridge already uses). A script cannot make the activation true: the browser sets
  it from real input. A script CAN make the frame hold focus (see 4.1), so together
  the two prove a recent real interaction with the host window while the frame is
  focused, not that the person clicked inside the frame or on the button whose text
  is sent. A click on the composer or sidebar alone does not pass for a page that
  never calls `focus()`, because that click does not focus the frame; a page that
  does call it passes after such a click (section 4.1). Both survive the sandboxed child frame, which was checked in
  Chromium. A browser without the API fills the composer.
- The Dashboard tab passes `onSend`. It binds the tab's confirmed thread slot
  (`confirmedSlot`, set only once the thread open was confirmed). The session Overview passes none and so treats `send` as `act`.
- No send went out in the last 3 seconds and none is in flight. A second message in
  that window is dropped.

The send uses the composer's own transport (`sendTurn`) with `origin:
"dashboard_card"` in the body and a client send id, so the row is echoed to the tab.
A refusal or a request that never left fills the composer instead. The gateway's routine
"not now" (a `card_*` code) fills it quietly; any other error (a server error, an expired
sign-in, no connection) fills it and also shows an error notice above the page. A late
or unreadable answer does neither, because the turn may have started.

### 3.3 What the gateway does

`POST /api/chat` with `origin: "dashboard_card"` runs `card_send.card_send_refusal`
before the slot is looked up for creation, so a refused send leaves no trace. It is
refused unless:

- The caller is the owner's own dashboard. An app token, a bare internal key and a
  non-owner are refused.
- The target is an existing crewmate DM thread whose binding matches its member.
- The crewmate's `dashboard_card_send` setting is on (section 3.5).
- The text obeys the rules in 3.1, re-checked on the server.
- The thread has no open decision: no pending tool approval future, no permission
  prompt the client is waiting on, no open question card (blocking or not), no
  question parked in the gateway for that slot.
- The thread is idle: no running or reserved turn, nothing queued, no sub-agent
  hold. A card send never queues and never steers.
- No card send went through on the thread in the last 3 seconds.

Anything else in the body (agent, theme, steer, responsive, files, quote, meta) is
dropped. Only the client send id survives.

### 3.4 What the turn is

The text is `[Dashboard card] <text>`. The tag is the first word of what the agent
receives and of what the transcript shows, so it cannot be mistaken for typed
text, and a leading `/clear` or `/new` is not a command. The row carries
`meta.cardOrigin: true`, a key a caller's own meta cannot write
(`RESERVED_ROW_META_KEYS`). The row does NOT carry `meta.human`. That key means "a
row a person authored" (`history_schema.HUMAN_TURN_META_KEY`), and it feeds the
stamp for when the person last spoke in a thread and which row may name a session.
Section 4.1 says the person may not have chosen the words that went out, so a card
row claiming it would let a page advance that stamp. The cost is small: a thread
whose only turns are card sends keeps ranking by its file time, which is what every
thread did before the marker existed.

The turn does not carry a person's authority over session directives
(`_directive_user_origin` is false), and it does not mark the thread as attended by
a human.

### 3.5 The Profile switch

`dashboard_card_send` on the crewmate's record, default on, read as on when absent
or not a bool. The Profile tab has one labelled switch beside the layout lock. Off,
every card send is refused with `card_send_off` and the host fills the composer.
The setting is written through the existing `PUT /api/agents/{name}`, which refuses
a non-bool.

Default on is a deliberate choice, made by the owner, and not an oversight of
section 4.1. Opt-in would leave the feature unused: a person who never opens the
Profile tab never learns the switch exists, and the buttons it serves are the ones
the person already clicked to answer. What makes default-on acceptable is the bounds
listed in 4.1: only the owner's own thread with the crewmate that wrote the page, at
most one send per 3 seconds, a `[Dashboard card]` tag on every turn, never an
approval and no authority over session directives, a switch that turns it off per
crewmate, and a SEL record of every decision. A card that sends costs the person
one click they chose to make on the page; the worst a hostile page gains over today
is one tagged, rate-limited, idle-thread-only message per interaction, in a thread
whose crewmate it already speaks for. If that proves too much in use, the switch's
default flips to off in a later change; existing records without the key would then
need one migration, which is why the default is named here.

### 3.6 A card turn is never an approval

Approvals resolve only on their own id-keyed routes: `POST /api/approvals/{id}/{action}`,
`POST /api/chat/slots/{slot}/approve` and `POST /api/ask-question/{ask_id}/answer`.
None reads chat text. Two side effects of a user turn could still look like an
answer, and both are closed by refusing the send while a decision is open:

- A live user row retires every non-blocking question card (`_ChatSlot.append`),
  which is how a typed card answer is consumed.
- A turn start marks unresolved permission rows stale (`_sweep_stale_permissions`).

The host also never calls the composer's "retire the waiting ask after a send" path
for a card send.

## 4. Case against

A page can now cause a user-role turn without the person typing. The activation
and focus requirement means a page needs the person to have just interacted with the
window, but a page can draw a button that looks like something else, and the
person's click is then on the page's words (4.1 says what a page can do beyond that). What limits the harm is that the words arrive tagged, with less authority
than typed words, never as an approval, only into the crewmate that wrote the page
(which the person already trusts to act), at most once per 3 seconds, and with a
switch that turns it off.

### 4.1 Residual risk: a page can arm its own send

A script in the page can call `focus()` on an element in its frame, which makes the
frame hold focus. After ANY real interaction the person has with the host window
(a click on the composer, a keypress) and within the browser's activation window
(a few seconds), such a page can post `send` with text it chose, and the host will
send it. Checked in Chromium: a real click on a host button, then `focus()` from the
frame, reads activation true and frame focus true. The person did not click the
button whose text goes out, and the page can show text from outside (a pull request
body, a web page), so the words can be ones the person never read.

This is accepted for this change, and nothing in the code tries to close it. What
bounds it:

- the target is only the owner's own thread with the crewmate that wrote the page,
  an agent the person already lets act in that thread;
- at most one send per 3 seconds per thread, on the host and again on the gateway,
  and never while the thread is busy or has an open approval or question;
- the turn begins `[Dashboard card]`, so the person and the agent see where it came
  from, and a leading `/clear` is not a command;
- it never counts as an approval and has no authority over session directives;
- the text is redacted like any non-human text before it is stored;
- the Profile switch turns it off per crewmate, and every decision, allowed or
  refused, is written to the SEL.

Closing it needs either a host-drawn confirmation with a preview of the text (a
second click, so no longer one click) or a host-controlled script inside the frame
that records only trusted clicks and sends over a channel the page cannot reach (a
larger change to the frame contract). Either can follow if the bound above proves
too weak in use; this document does not take that step.

The 3 second interval, the 600 character bound and the `[Dashboard card]` tag are
chosen, not derived. The bound is `act`'s existing bound; the interval is long
enough to absorb a double click and short enough not to feel dead.

## 5. Alternatives considered

Keep fill-only. Safe and unchanged, and the one-click case stays two steps.

Let the page name the target session. Rejected: a page would then write into any
session.

Treat a card turn like a typed turn. Rejected: a page could then answer an open
question card or steer session directives as the person.

Queue or steer a card turn behind a running turn. Rejected for this change: it
adds three more delivery paths to audit, and a card is normally shown while the
crewmate waits on the person. A busy thread falls back to the composer.

A per-card confirmation dialog. Rejected: it restores the second step this change
removes.

## 6. Compatibility

Compatible. No existing page changes: the built-in pages post `act` and keep
filling the composer. The host and the gateway ship in one build (the gateway
serves the SPA), so the host never sends `origin` to a gateway that ignores it.
`dashboard_card_send` is absent on every existing record and reads as on, which
changes nothing until a page posts `send`.

## 7. Acceptance

Proposed 2026-10-20 by chenmingwei23. Not yet accepted. A maintainer accepting
this document is recorded here with their name and the date, and the status
above moves to `accepted` in the same change.
