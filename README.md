# Divitize Zendesk bot — working copy, not deployed

This copy starts from the exact `bot_zendesk.py` deployed from GitHub commit
`13c10a77807f1f3b5e8892448295e0ef974cb1da`. The new OpenAI path is in
`openai_draft.py` and is called by `compose_openai_draft` in `bot_zendesk.py`.

The wording of the existing first/corrected public tracking messages is unchanged.
Tracking publication now puts the public message, guard tag, and solved status
in one Zendesk safe update. It skips duplicate announcements and routes
creator-gift tracking for human handling.
Ordinary AI replies are private Zendesk notes for an agent to review and send.
The old keyword-template function remains in the file as `compose_legacy_draft`
for comparison, but is not called.

New settings:

- `OPENAI_DRAFTS_ENABLED=true` enables the AI draft path. It defaults to `false`.
- Draft generation is locked to `gpt-6.1-sol`; `DRAFT_OPENAI_MODEL` environment overrides are ignored. `gpt-6-astra` is blocked by the draft module as an additional safeguard.
- `DRAFT_REASONING_EFFORT` defaults to `medium`.
- `OPENAI_API_KEY` is still required by the existing service configuration.

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

Before enabling this on Render: verify model access and spending limits, test
draft quality on historical tickets, and make sure only one bot instance is
running. The safeguards have offline tests, but they have not been tested
against live Zendesk behavior. This review branch has not been deployed or
used to write to Zendesk.

Offline tests: `python3 -m unittest -v test_ai_draft.py`.
