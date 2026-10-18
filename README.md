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

New settings:

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
  submit/save the ticket. While the bot is running, it adds one private decision
  draft, even when an introductory private draft already exists. Nothing is sent
  to the creator automatically. Review the newest decision draft and send manually.
- Choices: Approved (`collab_approved`), Declined (`collab_declined`), and
  Declined — Audience Reach (`collab_declined_audience_reach`). Approval asks for
  a missing delivery address; the audience decline refers to expected visibility,
  not prioritising larger creators. Replies adapt to the public conversation.
- A decision draft explicitly supersedes earlier **unsent** drafts. Public replies
  remain authoritative conversation context. Subsequent creator messages use the
  selected decision as context rather than restarting intake.
- The note and last-handled decision tag are saved in one safe update. Repeated
  saves, polling and successful-but-timed-out writes do not repeat the draft.
  Changing the decision creates a new private draft for human review. Clearing
  and reselecting the same already-handled decision does not resend it.
- Decisions can be drafted on solved tickets without reopening them; closed
  tickets cannot be changed. Customer-replacement tracking is skipped for tickets
  explicitly classified as Collaborations.
- The existing single ticket form has five optional, agent-only fields. On this
  account's Support Team plan they are visible on every ticket, as approved by
  the account owner; conditional visibility is unavailable. Content Status is
  manual tracking only and does not trigger messages.

Configured field IDs (Divitize):

| Field | ID |
| --- | --- |
| Inquiry type (existing) | 28789187446674 |
| Collaboration Status | 39921183865618 |
| Content Status | 39921238350354 |
| Instagram | 39921224964114 |
| TikTok | 39921242636818 |
| Other Social Profiles | 39921229607826 |

Production runs on a Render Free web service, which can sleep after inactivity.
Polling (including order-field updates and tracking) happens only while the
service is awake. Keep only one bot instance running to avoid competing writes.

Offline tests: `python3 -m unittest -v test_ai_draft.py`.
