---
name: ui-design-frontend-hk
description: Design or refine Handoff's web UI using an editorial, high-clarity utility aesthetic for realtime meetings and agent action traces. Use for product UI work in this repository; do not use it to recreate a referenced brand or website.
---

# Handoff UI design direction

## Intent

Create a confident, calm interface for a realtime meeting agent. The UI must
make two things immediately legible:

1. people are in a live conversation; and
2. Handoff is resolving context and taking—or asking permission to take—real
   actions.

The visual reference is the *method*, not a template. Its useful traits are a
white, highly structured canvas; a single saturated ink-blue action color;
fine rules; generous whitespace; large, plain-language headlines; and
purposeful product diagrams. Do not reproduce the reference's brand, logo,
copy, marketing-page composition, fake analytics dashboard, category rail, or
individual illustrations.

This is an application UI, not a marketing site. Reuse the methodology only
when it improves clarity, trust, and demo comprehension for Handoff.

## Product hierarchy

### Current finance demo: two-hour build

Three people join the hosted LiveKit room through existing open-source clients.
Handoff's web page is the companion activity and approval view. Reuse the visual
system below in one static HTML/CSS/JS page; avoid a new framework, participant
grid, call controls, or custom conferencing implementation for this demo.

A viewer should be able to scan the page in this order:

1. **Current action** — the fixed invoice objective, actual state, and one
   relevant primary button. Use “Approve check” for a proposed objective and
   “Create sandbox draft” only after the selected invoice passes checks.
2. **Invoice comparison** — original and selected quantity, unit price, tax,
   and total. Display the specific changed fields and their supporting records.
3. **Source evidence** — effective signed contract, accepted work, previously
   billed quantity, and purchase order. Expand raw details on demand.
4. **Activity history** — a compact, time-ordered record of proposal, approval,
   checks, repair if needed, creation, and actual readback.
5. **Service state** — compact OpenAI, LiveKit, Airwallex sandbox, and Langfuse
   readiness. Only show room counts or “Live” when the server reports them.

Put the invoice and evidence in the main column and the current action/activity
in a narrower column. On small screens, show the current action first. Keep
approval visible while work is pending; disable repeated submissions and bind
approval to the exact proposal. An altered proposal requires fresh approval.

Distinguish “draft created” from “readback verified”; show partial or uncertain
external writes with any known invoice ID. “No changes needed” is a valid result.
Do not manufacture three alternatives when the initial invoice passes. Keep
synthetic inputs and injected faults visibly labelled, and keep recorded metrics
separate from live results. A Langfuse link must refer to an actual trace; absent
configuration has an explicit empty state. Slack delivery is a stretch goal.

For this build, prioritize a readable comparison, exact approval states, keyboard
focus, safe text rendering, and an aria-live status message. Defer custom icons,
animation, avatars, charts, transcript search, and a conferencing redesign.

Do not let a generic dashboard, a large empty hero, decorative charts, or a
long transcript outrank the action trace. Handoff is not a transcription or
analytics product.

## Visual system

### Colour and contrast

Start with an almost-white neutral surface, white cards, cool blue-gray rules,
and one deep indigo/blue as the authority colour. A useful starting palette is
shown below; tune it to the existing application if it already has deliberate
tokens.

| Role | Starting value | Use |
| --- | --- | --- |
| Canvas | `#F8F9FC` | App background; avoid tinted full-screen gradients |
| Surface | `#FFFFFF` | Cards, panels, dialog bodies |
| Ink | `#151A31` | Primary text and high-emphasis icons |
| Muted ink | `#66708A` | Supporting labels and metadata |
| Rule | `#E2E6F0` | 1px dividers and quiet boundaries |
| Handoff blue | `#2026C9` | Primary actions, active states, Handoff identity |
| Blue wash | `#F0F1FF` | Selected rows, contextual callouts, quiet actions |
| Success | `#087A55` | Completed state, always paired with text/icon |
| Warning | `#A85B00` | Pending approval / needs attention |
| Danger | `#B42318` | Failed or destructive action |

Use indigo deliberately. It should identify Handoff, primary actions, selected
navigation, active controls, and key numbers—not turn every icon, border, and
heading blue. Status colours communicate state, never brand.

Maintain accessible text contrast. Do not rely on a faint blue fill or colour
alone to distinguish pending, success, and error states.

### Typography

Use a clean sans-serif that is already available in the app. Prefer a compact,
workmanlike UI face over a novelty display type. Make hierarchy come from size,
weight, line length, and spacing:

- Page/room title: 28–36px, 650–750 weight, tight but readable line-height.
- Section title: 18–22px, 600–700 weight.
- Card/action title: 14–16px, 600–650 weight.
- Body: 14–16px, regular weight, 1.45–1.6 line-height.
- Metadata, timestamps, and labels: 12–13px; never so pale that they vanish.

Use sentence case, short labels, and direct verbs: “Request approval”, “Action
completed”, “Send to #engineering”. Avoid all-caps except a small, restrained
event-type label when it helps scanning.

### Geometry and spacing

Use a 4px spacing base, with common gaps of 8, 12, 16, 24, 32, and 48px.
Prefer 1px rules and modest corner radii (4–8px). Keep shadows almost absent;
elevation should come mainly from whitespace, borders, and surface contrast.

The reference feels precise because edges align to a clear grid. Match that
discipline: align card headers, event markers, controls, and content columns.
Do not make every region a rounded floating container.

## Layout patterns

### App shell (future embedded meeting client)

The embedded meeting patterns below are a reference for later work. For the
current finance demo, use the companion-page hierarchy above and existing
LiveKit clients.

Use a compact top bar with the Handoff mark/name at the left, room state near
the centre or title region, and quiet utility controls at the right. Keep the
bar practical, not promotional. A bottom control dock is appropriate for call
controls when it is visually separate from the activity stream.

On wide screens, use a two-column working layout:

```text
+--------------------------------------------------------------+
| Handoff | Room: Weekly engineering sync · Live | controls    |
+-------------------------------------+------------------------+
|                                     | Handoff activity       |
|  Live meeting                       | Context resolved       |
|  participant tiles / speaker focus  | “that” → GitHub #12    |
|                                     |                        |
|  call controls                      | Awaiting approval      |
|                                     | Assign #12 → Guru      |
|                                     | [Approve] [Correct]    |
+-------------------------------------+------------------------+
```

The meeting region should be roughly 60–70% of the available width. The
activity rail should remain wide enough to show a complete action sentence
without forcing a dense, chat-like feed. Start around 360–420px and adjust to
the content.

On narrow viewports, preserve the meeting first, then place the activity feed
below it or in a labeled drawer. Do not merely shrink two desktop columns until
the trace becomes unreadable. Keep the active/pending action visible near the
top before historical events.

### Meeting surface

Keep participant tiles simple: stable aspect ratios, names anchored to a
predictable edge, a clear active-speaker outline or label, and compact audio /
muted indicators. Reserve strong blue for Handoff’s own tile or speaking state;
people should not look like inactive UI chrome.

Use realistic media states. If a camera is off, show an initial/avatar plus
name and connection state, rather than a decorative placeholder. Loading,
connecting, reconnecting, and no-participant states need intentional layouts.

### Context and action cards

An agent event should read like a compact work record, not a chat bubble. Use
a consistent anatomy:

```text
CONTEXT RESOLVED                              10:42
“that issue” → GitHub #12
Authentication timeout · High priority

ACTION PROPOSED
Assign GitHub #12 to Guru
Requires verbal confirmation
```

- Put the event kind in a small label, then state the outcome in plain English.
- Use links or a secondary action for source objects such as GitHub #12, not a
  wall of raw URLs or IDs.
- Group one causal chain together: context → proposal → approval → result.
- Use a vertical rule, small status glyph, or aligned timeline marker to convey
  sequence; avoid large cards for every log line.
- Show a timestamp only when it helps auditability, and use concise local time.

For a proposed consequential action, show the target and expected side effect
unambiguously. The primary visual state is **Awaiting spoken confirmation**;
buttons are an optional accessible fallback, not a replacement for the
conversational approval model. “Cancel” or “Correct” must be visually available
without competing with the confirm action.

For completed work, show what happened and where. For failure, say what failed
and give the user a next action, such as retrying or opening the GitHub item.
Do not use celebratory green checkmarks without a human-readable result.

### Controls

Use one prominent filled primary button per local decision. Secondary actions
should be outlined or quiet text buttons. Destructive actions should require a
separate confirmation moment rather than a permanently alarming red button.

Icon-only controls must have accessible names and visible tooltips on hover /
focus. Pair unfamiliar icons with labels. Use familiar audio/video controls;
this interface gains trust by being unsurprising during a live meeting.

## Motion and realtime behaviour

Motion should communicate causality, not decorate the screen.

- Append a new activity item with a brief 150–200ms fade/slide, then stop.
- Change an in-progress item in place when the action completes; do not create
  a second duplicate success card without a reason.
- Keep the live indicator and speaking cue subtle and continuous.
- Never auto-scroll a user away from an event they are reading. If the feed is
  not at the newest item, show a “New activity” affordance.
- Respect reduced-motion preferences; status must remain understandable with no
  animation.

Optimistic UI is acceptable only when labeled as pending. A GitHub assignment,
Slack delivery, or payment-link creation cannot be visually presented as done
before its tool result arrives.

## Content rules

Write interface language like a precise teammate:

| Prefer | Avoid |
| --- | --- |
| “I found GitHub #12: Authentication timeout.” | “Data retrieval successful.” |
| “Awaiting your confirmation to assign #12 to Guru.” | “Action queued.” |
| “Assigned #12 to Guru and sent the Slack DM.” | “Your request has been processed.” |
| “Couldn’t send the message. Slack returned an access error.” | “Something went wrong.” |

Do not invent activity, names, issue details, or integrations for visual
polish. Clearly distinguish demo fixtures from live data when fixtures are in
use. Preserve meeting privacy: do not expose an unnecessary full transcript in
the default screen.

## Implementation checklist

Before considering a Handoff UI change complete, verify:

- The currently pending or running action is visible without scrolling.
- A non-technical observer can tell whether Handoff has acted, is waiting, or
  failed in under five seconds.
- Context resolution is shown as evidence, but does not overwhelm the meeting.
- Approval and correction paths are equally clear, keyboard reachable, and
  cannot be mistaken for completion.
- Important state is expressed with text and icon/structure as well as colour.
- Keyboard focus is plainly visible, controls have accessible names, and status
  changes are announced through an appropriate live region.
- The page remains coherent at desktop and narrow viewport widths.
- Empty, loading, reconnecting, and failed-integration states have deliberate
  copy and layout.
- The UI uses the project’s existing components/tokens where they are already
  intentional; this document is a direction, not permission to redesign every
  primitive.

## Explicit non-goals

- Do not replicate Atlas, its logo, exact palette values, headline language,
  navigation, ecommerce/category layout, or mock revenue chart.
- Do not add a generic analytics dashboard to make the product look more
  sophisticated.
- Do not turn Handoff into a meeting summary or transcript reader.
- Do not hide consequential actions behind autonomous-looking automation.
- Do not introduce visual complexity that compromises the fast-path demo:
  resolve context → ask/act → show result → confirm aloud.
