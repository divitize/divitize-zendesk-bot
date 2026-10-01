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

Production runs on a Render Free web service, which can sleep after inactivity.
Polling (including order-field updates and tracking) happens only while the
service is awake. Keep only one bot instance running to avoid competing writes.

Offline tests: `python3 -m unittest -v test_ai_draft.py`.
