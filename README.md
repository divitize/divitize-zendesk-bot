# Divitize Zendesk bot — working copy, not deployed

This copy starts from the exact `bot_zendesk.py` deployed from GitHub commit
`13c10a77807f1f3b5e8892448295e0ef974cb1da`. The new OpenAI path is in
`openai_draft.py` and is called by `compose_openai_draft` in `bot_zendesk.py`.

The existing first/corrected public tracking messages have not been changed.
Ordinary AI replies are private Zendesk notes for an agent to review and send.
The old keyword-template function remains in the file as `compose_legacy_draft`
for comparison, but is not called.

New settings:

- `OPENAI_DRAFTS_ENABLED=true` enables the AI draft path. It defaults to `false`.
- Draft generation is locked to `gpt-6.1-sol`; model environment overrides are ignored.
- `DRAFT_REASONING_EFFORT` defaults to `medium`.
- `OPENAI_API_KEY` is still required by the existing service configuration.

Before enabling this on Render: verify model access and spending limits, test
draft quality on historical tickets, and make sure only one bot instance is
running. The existing tracking sender still needs its separate concurrency and
creator-gift fixes before a broader production rollout. This working copy has
not called OpenAI, written to Zendesk, or been deployed.

Offline tests: `python3 -m unittest -v test_ai_draft.py`.
