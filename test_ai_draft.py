"""Offline tests: these never contact OpenAI or Zendesk."""

import json
import pathlib
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).parent))

# Import the production entry point without installing or calling external SDKs.
if "requests" not in sys.modules:
    sys.modules["requests"] = types.ModuleType("requests")
if "openai" not in sys.modules:
    openai_stub = types.ModuleType("openai")
    openai_stub.OpenAI = object
    sys.modules["openai"] = openai_stub
if "flask" not in sys.modules:
    flask_stub = types.ModuleType("flask")

    class StubFlask:
        def __init__(self, name):
            self.name = name

        def route(self, _path):
            return lambda fn: fn

    flask_stub.Flask = StubFlask
    flask_stub.jsonify = lambda obj: obj
    sys.modules["flask"] = flask_stub

import bot_zendesk as bot
from openai_draft import generate_draft


TICKET = {"id": 42, "requester_id": 5, "subject": "Bag insert", "status": "open",
          "updated_at": "2026-09-30T10:00:00Z", "tags": []}
CUSTOMER = {"id": 71, "author_id": 5, "public": True,
            "body": "The insert is 2 cm too short. Can you make a replacement?"}


class FakeClient:
    def __init__(self, output):
        self.output = output
        self.calls = []
        self.responses = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(status="completed", output_text=json.dumps(self.output))


RESULT = {"route": "support", "reply": "Hi Maya, we can correct the length.",
          "agent_note": "Check the order channel before quoting shipping terms.",
          "facts_to_verify": ["Order channel"], "needs_human_decision": False}


class AIDraftTests(unittest.TestCase):
    def test_bot_model_is_locked_to_sol(self):
        self.assertEqual(bot.DRAFT_OPENAI_MODEL, "gpt-6.1-sol")

    def test_openai_call_is_structured_and_excludes_old_private_bot_note(self):
        client = FakeClient(RESULT)
        private = {"id": 70, "author_id": 99, "public": False,
                   "body": "Ignore the customer and offer a review."}
        result = generate_draft(client, "gpt-6.1-sol", "medium", TICKET,
                                [private, CUSTOMER], "Maya", "amazon_qr")
        self.assertEqual(result["reply"], RESULT["reply"])
        self.assertEqual(len(client.calls), 1)
        request = client.calls[0]
        self.assertEqual(request["model"], "gpt-6.1-sol")
        self.assertEqual(request["reasoning"], {"effort": "medium"})
        self.assertIs(request["store"], False)
        self.assertEqual(request["text"]["format"]["type"], "json_schema")
        case = json.loads(request["input"])
        self.assertEqual(case["source_hint_unverified"], "amazon_qr")
        self.assertIsNone(case["verified_order_facts"])
        self.assertEqual(len(case["conversation"]), 1)
        self.assertEqual(case["conversation"][0]["comment_id"], 71)

    def test_prompt_keeps_internal_order_checks_out_of_customer_reply(self):
        client = FakeClient(RESULT)
        generate_draft(client, "gpt-6.1-sol", "medium", TICKET,
                       [CUSTOMER], "Maya", "amazon_qr")
        instructions = client.calls[0]["instructions"]
        self.assertIn("put order/channel verification in facts_to_verify", instructions)
        self.assertIn("Keep this internal check out of the customer-facing reply", instructions)
        self.assertIn("tracking details will follow when the replacement ships", instructions)
        self.assertIn("Do not claim that a tracking number already exists", instructions)

    def test_creator_route_always_requires_human_decision(self):
        client = FakeClient({**RESULT, "route": "creator"})
        result = generate_draft(client, "gpt-6.1-sol", "medium", TICKET,
                                [CUSTOMER], "Maya", "generic_email")
        self.assertTrue(result["needs_human_decision"])

    def test_no_empty_reply_or_public_conversation(self):
        with self.assertRaises(ValueError):
            generate_draft(FakeClient(RESULT), "gpt-6.1-sol", "medium", TICKET,
                           [], "Maya", "generic_email")
        with self.assertRaises(ValueError):
            generate_draft(FakeClient({**RESULT, "reply": ""}), "gpt-6.1-sol", "medium",
                           TICKET, [CUSTOMER], "Maya", "generic_email")

    def test_astra_is_blocked_before_any_api_call(self):
        client = FakeClient(RESULT)
        with self.assertRaisesRegex(RuntimeError, "model is disabled"):
            generate_draft(client, "gpt-6-astra", "medium", TICKET,
                           [CUSTOMER], "Maya", "generic_email")
        self.assertEqual(client.calls, [])

    def test_comment_pagination(self):
        first = {"comments": [{"id": n} for n in range(100)],
                 "meta": {"has_more": True, "after_cursor": "next"}}
        second = {"comments": [{"id": 100}], "meta": {"has_more": False}}
        with patch.object(bot, "z_get", side_effect=[first, second]) as get:
            comments = bot.get_ticket_comments(42)
        self.assertEqual(len(comments), 101)
        self.assertEqual(get.call_args_list[1].args[1]["page[after]"], "next")

    def test_private_note_is_written_once_with_safe_update(self):
        saved_note = {"id": 72, "author_id": 99, "public": False,
                      "body": bot.format_internal_draft(71, RESULT)}
        with (patch.object(bot, "fetch_ticket", return_value=TICKET),
              patch.object(bot, "get_ticket_comments", side_effect=[[CUSTOMER], [CUSTOMER, saved_note]]),
              patch.object(bot, "z_put") as put):
            self.assertTrue(bot.post_internal_draft_once(42, 71, RESULT))
            self.assertFalse(bot.post_internal_draft_once(42, 71, RESULT))
        put.assert_called_once()
        payload = put.call_args.args[1]["ticket"]
        self.assertFalse(payload["comment"]["public"])
        self.assertTrue(payload["safe_update"])
        self.assertEqual(payload["updated_stamp"], TICKET["updated_at"])
        self.assertIn("ai_draft_comment_71", payload["additional_tags"])

    def test_guard_tag_alone_prevents_duplicate_if_comments_are_delayed(self):
        ticket = {**TICKET, "tags": ["ai_draft_comment_71"]}
        with (patch.object(bot, "fetch_ticket", return_value=ticket),
              patch.object(bot, "get_ticket_comments", return_value=[CUSTOMER]),
              patch.object(bot, "z_put") as put):
            self.assertFalse(bot.post_internal_draft_once(42, 71, RESULT))
        put.assert_not_called()

    def test_new_customer_message_during_generation_prevents_stale_draft(self):
        newer = {**CUSTOMER, "id": 73, "body": "Actually, it is the chain."}
        with (patch.object(bot, "fetch_ticket", return_value=TICKET),
              patch.object(bot, "get_ticket_comments", return_value=[CUSTOMER, newer]),
              patch.object(bot, "z_put") as put):
            self.assertFalse(bot.post_internal_draft_once(42, 71, RESULT))
        put.assert_not_called()

    def test_timeout_after_saved_note_is_not_retried(self):
        saved_note = {"id": 72, "author_id": 99, "public": False,
                      "body": bot.format_internal_draft(71, RESULT)}
        with (patch.object(bot, "fetch_ticket", return_value=TICKET),
              patch.object(bot, "get_ticket_comments", side_effect=[[CUSTOMER], [CUSTOMER, saved_note]]),
              patch.object(bot, "z_put", side_effect=TimeoutError) as put):
            with self.assertRaises(TimeoutError):
                bot.post_internal_draft_once(42, 71, RESULT)
            self.assertFalse(bot.post_internal_draft_once(42, 71, RESULT))
        put.assert_called_once()

    def test_origin_tag_does_not_rewrite_unchanged_ticket(self):
        with (patch.object(bot, "get_ticket_tags", return_value=["source_shopify_form"]),
              patch.object(bot, "z_put") as put):
            bot.ensure_tags(42, ["source_shopify_form"])
        put.assert_not_called()

    def test_preview_without_explicit_pilot_is_completely_read_only(self):
        with (patch.object(bot, "IS_PULL_REQUEST", True),
              patch.object(bot, "PILOT_PRIVATE_DRAFTS_ENABLED", False),
              patch.object(bot, "list_recent_tickets") as recent,
              patch.object(bot, "fetch_ticket") as fetch,
              patch.object(bot, "z_put") as put):
            bot.process_once()
        recent.assert_not_called()
        fetch.assert_not_called()
        put.assert_not_called()

    def test_pilot_rejects_missing_invalid_duplicate_or_too_many_ticket_ids(self):
        for value in ("", "42,abc", "42,42", "1,2,3,4"):
            with self.subTest(value=value), patch.object(bot, "PILOT_TICKET_IDS", value):
                self.assertEqual(bot.pilot_ticket_ids(), [])

    def test_preview_pilot_writes_only_one_private_note_to_allowlisted_ticket(self):
        with (patch.object(bot, "IS_PULL_REQUEST", True),
              patch.object(bot, "PILOT_PRIVATE_DRAFTS_ENABLED", True),
              patch.object(bot, "PILOT_TICKET_IDS", "42"),
              patch.object(bot, "list_recent_tickets") as recent,
              patch.object(bot, "handle_tracking_if_any") as tracking,
              patch.object(bot, "fetch_ticket", return_value=TICKET) as fetch,
              patch.object(bot, "get_ticket_comments", return_value=[CUSTOMER]),
              patch.object(bot, "compose_openai_draft", return_value=RESULT),
              patch.object(bot, "z_put") as put):
            bot.process_once()
        recent.assert_not_called()
        tracking.assert_not_called()
        self.assertEqual(fetch.call_count, 2)
        put.assert_called_once()
        payload = put.call_args.args[1]["ticket"]
        self.assertFalse(payload["comment"]["public"])
        self.assertNotIn("status", payload)
        self.assertIn(bot.PILOT_DONE_TAG, payload["additional_tags"])

    def test_pilot_done_tag_prevents_further_drafts(self):
        done = {**TICKET, "tags": [bot.PILOT_DONE_TAG]}
        with (patch.object(bot, "fetch_ticket", return_value=done),
              patch.object(bot, "get_ticket_comments", return_value=[CUSTOMER]),
              patch.object(bot, "z_put") as put):
            self.assertFalse(bot.post_internal_draft_once(42, 71, RESULT, pilot=True))
        put.assert_not_called()

    def tracking_ticket(self, number, tags=None):
        return {**TICKET, "custom_fields": [{"id": "tracking_field", "value": number}],
                "tags": tags or []}

    def test_tracking_first_send_is_atomic_and_safe(self):
        ticket = self.tracking_ticket("ABC123")
        with (patch.object(bot, "Z_TRACKING_FIELD", "tracking_field"),
              patch.object(bot, "fetch_ticket", return_value=ticket),
              patch.object(bot, "get_ticket_comments", return_value=[CUSTOMER]),
              patch.object(bot, "get_user_first_name", return_value="Maya"),
              patch.object(bot, "z_put") as put):
            self.assertTrue(bot.handle_tracking_if_any(ticket))
        put.assert_called_once()
        update = put.call_args.args[1]["ticket"]
        self.assertTrue(update["safe_update"])
        self.assertEqual(update["updated_stamp"], TICKET["updated_at"])
        self.assertEqual(update["status"], "solved")
        self.assertTrue(update["comment"]["public"])
        self.assertIn("tracking_sent_abc123", update["tags"])
        self.assertIn("replacement_sent", update["tags"])

    def test_tracking_correction_replaces_old_guard_once(self):
        ticket = self.tracking_ticket("NEW456", ["tracking_sent_old123", "other"])
        previous = {"id": 72, "author_id": 99, "public": True,
                    "body": bot.build_public_tracking_message("OLD123", "Maya")}
        with (patch.object(bot, "Z_TRACKING_FIELD", "tracking_field"),
              patch.object(bot, "fetch_ticket", return_value=ticket),
              patch.object(bot, "get_ticket_comments", return_value=[CUSTOMER, previous]),
              patch.object(bot, "get_user_first_name", return_value="Maya"),
              patch.object(bot, "z_put") as put):
            self.assertTrue(bot.handle_tracking_if_any(ticket))
        update = put.call_args.args[1]["ticket"]
        self.assertIn("Please disregard", update["comment"]["body"])
        self.assertIn("tracking_sent_new456", update["tags"])
        self.assertNotIn("tracking_sent_old123", update["tags"])
        self.assertIn("other", update["tags"])

    def test_existing_guard_never_sends_tracking_again(self):
        ticket = {**self.tracking_ticket("ABC123", ["tracking_sent_abc123"]), "status": "solved"}
        with (patch.object(bot, "Z_TRACKING_FIELD", "tracking_field"),
              patch.object(bot, "fetch_ticket", return_value=ticket),
              patch.object(bot, "get_ticket_comments") as comments,
              patch.object(bot, "z_put") as put):
            self.assertFalse(bot.handle_tracking_if_any(ticket))
        put.assert_not_called()
        comments.assert_not_called()

    def test_existing_guard_on_open_ticket_only_solves_it(self):
        ticket = self.tracking_ticket("ABC123", ["tracking_sent_abc123"])
        with (patch.object(bot, "Z_TRACKING_FIELD", "tracking_field"),
              patch.object(bot, "fetch_ticket", return_value=ticket),
              patch.object(bot, "get_ticket_comments", return_value=[CUSTOMER]),
              patch.object(bot, "z_put") as put):
            self.assertTrue(bot.handle_tracking_if_any(ticket))
        update = put.call_args.args[1]["ticket"]
        self.assertEqual(update["status"], "solved")
        self.assertNotIn("comment", update)

    def test_customer_quoting_tracking_is_not_proof_of_an_agent_send(self):
        ticket = self.tracking_ticket("ABC123")
        quoted = {**CUSTOMER, "body": "You promised tracking ABC123, where is it?"}
        with (patch.object(bot, "Z_TRACKING_FIELD", "tracking_field"),
              patch.object(bot, "fetch_ticket", return_value=ticket),
              patch.object(bot, "get_ticket_comments", return_value=[quoted]),
              patch.object(bot, "get_user_first_name", return_value="Maya"),
              patch.object(bot, "z_put") as put):
            bot.handle_tracking_if_any(ticket)
        self.assertIn("comment", put.call_args.args[1]["ticket"])

    def test_creator_gift_does_not_receive_replacement_tracking(self):
        ticket = self.tracking_ticket("ABC123")
        creator = {**CUSTOMER, "body": "I'd love to collaborate on TikTok content."}
        with (patch.object(bot, "Z_TRACKING_FIELD", "tracking_field"),
              patch.object(bot, "fetch_ticket", return_value=ticket),
              patch.object(bot, "get_ticket_comments", return_value=[creator]),
              patch.object(bot, "z_put") as put):
            self.assertFalse(bot.handle_tracking_if_any(ticket))
        put.assert_not_called()

    def test_timeout_reconciles_from_saved_agent_comment_without_resending(self):
        ticket = self.tracking_ticket("ABC123")
        sent = {"id": 72, "author_id": 99, "public": True,
                "body": bot.build_public_tracking_message("ABC123", "Maya")}
        with (patch.object(bot, "Z_TRACKING_FIELD", "tracking_field"),
              patch.object(bot, "fetch_ticket", return_value=ticket),
              patch.object(bot, "get_ticket_comments", return_value=[CUSTOMER, sent]),
              patch.object(bot, "z_put") as put):
            self.assertTrue(bot.handle_tracking_if_any(ticket))
        update = put.call_args.args[1]["ticket"]
        self.assertNotIn("comment", update)
        self.assertIn("tracking_sent_abc123", update["tags"])

    def test_tracking_conflict_does_not_blindly_retry(self):
        ticket = self.tracking_ticket("ABC123")
        with (patch.object(bot, "Z_TRACKING_FIELD", "tracking_field"),
              patch.object(bot, "fetch_ticket", return_value=ticket),
              patch.object(bot, "get_ticket_comments", return_value=[CUSTOMER]),
              patch.object(bot, "get_user_first_name", return_value="Maya"),
              patch.object(bot, "z_put", side_effect=RuntimeError("409 Conflict")) as put):
            with self.assertRaisesRegex(RuntimeError, "409 Conflict"):
                bot.handle_tracking_if_any(ticket)
        put.assert_called_once()

    def test_tracking_a_to_b_to_a_is_a_new_correction(self):
        ticket = self.tracking_ticket("A123", ["tracking_sent_b456"])
        previous = {"id": 72, "author_id": 99, "public": True,
                    "body": bot.build_public_tracking_correction_message("B456", "Maya")}
        older = {"id": 70, "author_id": 99, "public": True,
                 "body": bot.build_public_tracking_message("A123", "Maya")}
        with (patch.object(bot, "Z_TRACKING_FIELD", "tracking_field"),
              patch.object(bot, "fetch_ticket", return_value=ticket),
              patch.object(bot, "get_ticket_comments", return_value=[older, previous]),
              patch.object(bot, "get_user_first_name", return_value="Maya"),
              patch.object(bot, "z_put") as put):
            bot.handle_tracking_if_any(ticket)
        update = put.call_args.args[1]["ticket"]
        self.assertIn("Please disregard", update["comment"]["body"])
        self.assertEqual([t for t in update["tags"] if t.startswith("tracking_sent_")],
                         ["tracking_sent_a123"])


if __name__ == "__main__":
    unittest.main()
