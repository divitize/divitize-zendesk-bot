# Divitize Zendesk bot

The OpenAI path is in
`openai_draft.py` and is called by `compose_openai_draft` in `bot_zendesk.py`.

The wording of the existing first/corrected public tracking messages is unchanged.
Tracking publication now puts the public message, guard tag, and solved status
in one Zendesk safe update. It skips duplicate announcements and routes
creator-gift tracking for human handling.
Ordinary AI replies are private Zendesk notes for an agent to review and send.
The old keyword-template function remains in the file as `compose_legacy_draft`
for comparison, but is not called.

The bot fills Zendesk's **Order Number** ticket field when it is empty and a
public requester comment contains one recognizable order number. It supports
Amazon's `123-1234567-1234567` format (including the contact form's
`Your123-...` placeholder) and explicit `order #1234`, `order number: 1234`,
or `order ID is 1234` wording. Ambiguous messages are skipped; an existing
field is never overwritten. The update contains no comment, status, or tag,
uses Zendesk's safe update, and rechecks the ticket before writing.

Shopify's 4-6 digit storefront references are saved in ERP format with the fixed
`3325573-` prefix: `Order #1893` becomes `3325573-1893`, and `Order 10000`
becomes `3325573-10000`. An already-prefixed reference is never prefixed twice.
Recognize explicit order wording, a standalone `#1893`, or a bare number in
direct reply to a public agent request for the order number. Arbitrary years,
measurements, bag models, phone and tracking numbers are not treated as orders.
Only public requester messages count; ambiguous references are left for review.
A short number explicitly described as Amazon is not converted to Shopify.
Existing field values, including manually entered short references, remain intact.

Recognized Shopify-order tickets receive `source_shopify_order` instead of
`source_generic_email`; the obsolete generic tag is removed in the same safe
tag update. Other tags, including draft/tracking guards and human tags, are
preserved. Shopify form inquiries without an order still use `source_shopify_form`.
Tagging rechecks the latest ticket/comments, skips unchanged tags and closed
tickets, and uses `updated_stamp` so concurrent changes are not overwritten.
These metadata-only updates never post a reply, change status, or make an AI call.

Order-number format checks are shared between field extraction and AI case data
in `order_numbers.py`. Amazon-shaped identifiers with malformed digit groups
(for example a six-digit final group) are not saved, guessed, or padded. The
model receives an explicit `order_number_validation` state and is instructed to
ask the customer to copy/resend the full number when identification is needed,
rather than thanking them as if a usable number was received. Format-valid is
still not order verification. A later complete correction supersedes the typo;
a later malformed identifier does not silently fall back to an earlier number.
Existing human-entered field values are never overwritten.

Policy `divitize-2026-10-18-v5` also makes one private correction to an outdated
unsent v4 bot draft affected by a malformed Amazon number (such as ticket752).
This runs only on recent non-solved/non-closed tickets whose latest comment is
the affected bot draft and whose latest public message is the requester’s.
New human replies/notes, changed order data and saved repair markers prevent
replacement/repetition. The corrected note explicitly supersedes the old unsent
draft; it never sends publicly or changes fields/status. Normal draft guards
and tracking behavior are otherwise unchanged.

New settings:

### Optional 17TRACK shipment-status lookups (private notes only)

`TRACK17_ENABLED=true` and secret `TRACK17_API_KEY` enable standard V2.4 API
lookups when a customer asks about shipping and a public conversation contains
an explicit tracking reference. Defaults off. Configure the key only in Render's
secret environment settings, never in code, chat, notes, model input or logs.
Only the tracking number (and the API-returned carrier when applicable) is sent;
no customer email/name/address or order number is transmitted to 17TRACK.

The adapter reads registered tracking first. A number is registered only if the
API explicitly reports it unregistered and a quota check shows usable credit.
It does not purchase quota or call paid real-time refresh/translation/notification
routes. Standard registrations consume account quota; this is not an unlimited
free service. Stop new registrations at zero quota; previously registered numbers
can still be read. Carrier autodetection is not infallible: multiple matches and
errors go to human review instead of an invented status.

Results are passed to the existing single Sol medium call as separate carrier
evidence, then appended deterministically under AGENT NOTES in the private draft.
No original/customer tracking is ever written to a custom field. The existing
replacement tracking field, public tracking announcement, solved status and
duplicate guards are untouched. A recent public replacement announcement is
used instead of an older shipment reference when relevant. Preview/pilot runs
cannot register or query shipments even when production environment is copied.

For returned-to-sender shipments, explain the reported return, apologise, and
move to replacement resolution. Do not invent the return cause or confuse it
with delivery to the customer. Distinguish returning from returned. The owner
has authorized immediate replacement processing when approving/sending a private
draft for a verified returned original parcel with a usable order reference
(or an order already located by a public agent). In that case, or if a replacement
is already arranged, the approved illustrative wording is:
"We've now arranged a replacement for you, and we'll send its tracking details
in a separate update." A replacement tracking number is not required to draft
this approved resolution. The agent note reminds the human to process it
immediately when approving/sending, verify the original order and prevent a
duplicate replacement; revise before sending if processing cannot proceed.
This is a replacement decision, not proof of ERP creation, production or dispatch.
Do not promise a second replacement or invent an ETA. Request an order ID if
missing, malformed or ambiguous. The standing authorization does not cover
unverified returns, parcels still returning, other exceptions, creator gifts,
or a customer asking for another resolution. Those cases retain contextual
human review. All support replies remain private drafts.

Initial carrier results may take time. For pending first results, defer drafting
and retry through normal polling for up to five minutes; after that, provide an
unverified context for manual review, not a false checked-status claim. In-memory
cache limits repeated API reads (30 seconds pending / 10 minutes other outcomes),
while 17TRACK retains registration idempotence across restarts. No continuous
webhook/monitor service or historical draft backfill is enabled. Render must be
awake for normal polling; existing drafts, including manually corrected #787,
are not automatically regenerated.

Before saving a tracking-informed draft, recheck the public transcript,
requester, order reference and replacement-tracking value. Concurrent edits
cancel the write. A successful private note still uses the original comment
marker/tag and safe update, preventing duplicates after retries or restarts.

Offline regression suite:
`python3 -m unittest -q test_ai_draft.py test_shipment_tracking.py`

- `OPENAI_DRAFTS_ENABLED=true` enables the AI draft path. It defaults to `false`.
- Draft generation is locked to `gpt-6.1-sol`; `DRAFT_OPENAI_MODEL` environment overrides are ignored. `gpt-6-astra` is blocked by the draft module as an additional safeguard.
- `DRAFT_REASONING_EFFORT` defaults to `medium`.
- `OPENAI_API_KEY` is still required by the existing service configuration.
- `Z_ORDER_FIELD` defaults to Divitize's Order Number field ID
  `29113177850258`. No additional Render setting is required.

Private-draft trial on a Render pull-request preview:

- A preview (`IS_PULL_REQUEST=true`) never runs the normal origin-tagging or
  public tracking loops, even if production environment settings are copied.
- It does nothing until `PILOT_PRIVATE_DRAFTS_ENABLED=true` and
  `PILOT_TICKET_IDS` contains one to three distinct numeric ticket IDs.
- It can add only one private draft per selected ticket, marked with
  `ai_private_draft_pilot_done`. It never changes status or sends a public reply.
- Disable the preview after the trial so it does not continue consuming Render
  free-instance hours. Never enable the pilot variables on the production
  service as a substitute for a preview.

Creator collaboration decisions:

- The existing creator intake instructions in `openai_draft.py` are unchanged.
- The model's existing `creator` route automatically fills an empty **Inquiry
  type** with **Collaborations**, alongside the private intake note. It also
  backfills empty Inquiry type from an existing creator draft for the latest
  customer comment. A manually selected Inquiry type is never overwritten;
  approvals and declines are always selected by the human agent.
- Set **Inquiry type = Collaborations**, select **Collaboration Status**, and
  submit/save the ticket. **Approved** creates a private draft for manual review.
  A newly saved **Declined** or **Declined — Audience Reach** sends the generated
  customer-facing reply directly, even if an unsent intake draft already exists.
  The public decline, **Solved** status and duplicate-protection tags are saved
  together in one safe update. A withheld/private draft does not solve the ticket.
  The bot must be awake; batch processing may take several minutes.
- Choices: Approved (`collab_approved`), Declined (`collab_declined`), and
  Declined — Audience Reach (`collab_declined_audience_reach`). Approval asks for
  a missing delivery address; the audience decline refers to expected visibility,
  not prioritising larger creators. Replies adapt to the public conversation.
- A decision draft explicitly supersedes earlier **unsent** drafts. Public replies
  remain authoritative conversation context. Subsequent creator messages use the
  selected decision as context rather than restarting intake.
- The reply/note and last-handled decision tag are saved in one safe update.
  Repeated saves, polling, restarts, and successful-but-timed-out writes do not
  repeat the response. A permanent `ai_collab_decline_sent` guard allows only one
  automatic decline per ticket, even if statuses are toggled later; any later
  changed decision is a private draft. Approval always remains private.
- Public sending requires the latest status-change audit to prove a web selection
  by an agent/admin after `2026-10-18T14:24:00Z`. Old selections/drafts are never
  retroactively sent. API/rule/customer edits cannot authorize sending. A public
  reply in/after that selection blocks auto-send to avoid doubling a human reply.
  The selection and public conversation are rechecked after generation.
- Uncertain output requiring factual checks, protected Amazon/ChannelReply
  channels, unknown integrations, and legacy selections stay private with an
  explicit AUTO-SEND WITHHELD explanation. Internal notes are never published.
  Later incoming creator messages still produce private follow-up drafts.
- Decisions can be handled on solved tickets without reopening them; closed
  tickets cannot be changed. Customer-replacement tracking is skipped for tickets
  explicitly classified as Collaborations.
- The existing single ticket form has four optional, agent-only fields. On this
  account's Support Team plan they are visible on every ticket, as approved by
  the account owner; conditional visibility is unavailable. Content Status is
  manual tracking only and does not trigger messages.
- **Instagram** and **TikTok** automatically fill from explicit profile URLs or
  clearly labelled @handles in public requester messages, including HTML links.
  Tracking parameters are removed. Internal drafts/agent guesses, post/video URLs,
  shortlinks and lookalike domains are not used. No profile pages are visited and
  no extra OpenAI calls are needed. More than one distinct profile on a platform
  is left for human review. Existing field values are never overwritten.
  Field-only updates recheck the requester, conversation and current field values,
  use safe_update, and never send a comment, change status, or replace ticket tags.
  They run on recent non-closed tickets (including solved) while the bot is awake.
- **Other Social Profiles** has been deactivated to remove it from the left
  column without permanently deleting historical data. The bot no longer reads
  or writes that field.

Configured field IDs (Divitize):

| Field | ID |
| --- | --- |
| Inquiry type (existing) | 28789187446674 |
| Collaboration Status | 39921183865618 |
| Content Status | 39921238350354 |
| Instagram | 39921224964114 |
| TikTok | 39921242636818 |
| Other Social Profiles (inactive; historical only) | 39921229607826 |

Production runs on a Render Free web service, which can sleep after inactivity.
Polling (including order-field updates and tracking) happens only while the
service is awake. Keep only one bot instance running to avoid competing writes.

Offline tests: `python3 -m unittest -v test_ai_draft.py`.
