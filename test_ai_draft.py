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
from collaboration_draft import generate_collaboration_draft, HANDLED_PREFIX


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
    def test_order_number_extraction_is_conservative(self):
        cases = [
            ("Amazon Order Number: [Your112-5428578-9415428]", ["112-5428578-9415428"]),
            ("Amazon order number: 113-4771136-5412247", ["113-4771136-5412247"]),
            ("The Amazon order number is 114-7748319-\n9746607. Thanks very much.",
             ["114-7748319-9746607"]),
            ("Amazon order: 114-7748319-\r\n 9746607", ["114-7748319-9746607"]),
            ("Amazon order: 114-7748319-\n\n9746607", ["114-7748319-9746607"]),
            ("Amazon order: 114-7748319-\n\n\n9746607", []),
            ("My order #1234 arrived yesterday", ["#1234"]),
            ("Order number: 23456", ["23456"]),
            ("Order ID is 34567", ["34567"]),
            ("The insert is 2 cm short; tracking 123456789", []),
            ("Order #1234 was wrong; it is order #5678", ["#1234", "#5678"]),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(bot.order_numbers_in_message(message), expected)

    def test_wrapped_amazon_order_is_normalized_for_draft_and_origin(self):
        message = "The Amazon order number is 114-7748319-\n\n9746607."
        self.assertEqual(bot.extract_order_number(message), "114-7748319-9746607")
        customer = {**CUSTOMER, "body": message}
        self.assertEqual(bot.classify_origin(TICKET, [customer]), "amazon_qr")

    def test_order_number_only_from_customer_public_comments(self):
        agent = {"id": 72, "author_id": 99, "public": True, "body": "Order #9999"}
        private = {"id": 73, "author_id": 5, "public": False, "body": "Order #8888"}
        customer = {**CUSTOMER, "body": "My order #1234"}
        self.assertEqual(bot.customer_order_number([customer, agent, private], 5), "#1234")
        self.assertIsNone(bot.customer_order_number([agent, private], 5))

    def test_ambiguous_latest_customer_order_does_not_use_old_number(self):
        old = {**CUSTOMER, "body": "Order #1234"}
        new = {**CUSTOMER, "id": 74, "body": "Order #1234 or order #5678?"}
        self.assertIsNone(bot.customer_order_number([old, new], 5))

    def test_order_field_safe_update_has_no_comment_or_status_change(self):
        ticket = {**TICKET, "custom_fields": [{"id": "29113177850258", "value": None},
                                                  {"id": "tracking", "value": "TRACK123"}]}
        customer = {**CUSTOMER, "body": "My Amazon order is 113-4771136-5412247"}
        with (patch.object(bot, "fetch_ticket", return_value=ticket),
              patch.object(bot, "get_ticket_comments", return_value=[customer]),
              patch.object(bot, "z_put") as put):
            self.assertTrue(bot.populate_order_number_if_missing(ticket, [customer]))
        put.assert_called_once()
        payload = put.call_args.args[1]["ticket"]
        self.assertEqual(payload["custom_fields"], [{"id": 29113177850258,
                                                      "value": "113-4771136-5412247"}])
        self.assertTrue(payload["safe_update"])
        self.assertEqual(payload["updated_stamp"], ticket["updated_at"])
        self.assertNotIn("comment", payload)
        self.assertNotIn("status", payload)
        self.assertNotIn("tags", payload)

    def test_wrapped_amazon_order_fills_only_order_field(self):
        ticket = {**TICKET, "custom_fields": [{"id": "29113177850258", "value": None}]}
        customer = {**CUSTOMER, "body": "The Amazon order number is 114-7748319-\n\n9746607."}
        with (patch.object(bot, "fetch_ticket", return_value=ticket),
              patch.object(bot, "get_ticket_comments", return_value=[customer]),
              patch.object(bot, "z_put") as put):
            self.assertTrue(bot.populate_order_number_if_missing(ticket, [customer]))
        payload = put.call_args.args[1]["ticket"]
        self.assertEqual(payload["custom_fields"], [{"id": 29113177850258,
                                                     "value": "114-7748319-9746607"}])
        self.assertNotIn("comment", payload)
        self.assertNotIn("status", payload)
        self.assertNotIn("tags", payload)

    def test_existing_order_number_is_never_overwritten(self):
        ticket = {**TICKET, "custom_fields": [{"id": "29113177850258",
                                                  "value": "113-4771136-5412247"}]}
        with (patch.object(bot, "fetch_ticket") as fetch,
              patch.object(bot, "z_put") as put):
            self.assertFalse(bot.populate_order_number_if_missing(ticket, [CUSTOMER]))
        fetch.assert_not_called()
        put.assert_not_called()

    def test_order_number_rechecks_field_and_conversation_before_write(self):
        candidate = {**CUSTOMER, "body": "Order #1234"}
        filled = {**TICKET, "custom_fields": [{"id": "29113177850258", "value": "#9999"}]}
        with (patch.object(bot, "fetch_ticket", return_value=filled),
              patch.object(bot, "z_put") as put):
            self.assertFalse(bot.populate_order_number_if_missing(TICKET, [candidate]))
        put.assert_not_called()

        changed = {**CUSTOMER, "body": "Order #5678"}
        with (patch.object(bot, "fetch_ticket", return_value=TICKET),
              patch.object(bot, "get_ticket_comments", return_value=[changed]),
              patch.object(bot, "z_put") as put):
            self.assertFalse(bot.populate_order_number_if_missing(TICKET, [candidate]))
        put.assert_not_called()

    def test_order_field_recheck_prevents_repeat_after_timeout(self):
        customer = {**CUSTOMER, "body": "Order #1234"}
        filled = {**TICKET, "custom_fields": [{"id": "29113177850258", "value": "#1234"}]}
        with (patch.object(bot, "fetch_ticket", side_effect=[TICKET, filled]),
              patch.object(bot, "get_ticket_comments", return_value=[customer]),
              patch.object(bot, "z_put", side_effect=TimeoutError) as put):
            with self.assertRaises(TimeoutError):
                bot.populate_order_number_if_missing(TICKET, [customer])
            self.assertFalse(bot.populate_order_number_if_missing(TICKET, [customer]))
        put.assert_called_once()

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
        self.assertIsNone(case["order_number_provided_unverified"])
        self.assertIsNone(case["verified_order_facts"])
        self.assertEqual(len(case["conversation"]), 1)
        self.assertEqual(case["conversation"][0]["comment_id"], 71)

    def test_prompt_asks_customer_for_missing_order_id_but_keeps_verification_internal(self):
        client = FakeClient(RESULT)
        generate_draft(client, "gpt-6.1-sol", "medium", TICKET,
                       [CUSTOMER], "Maya", "amazon_qr")
        instructions = client.calls[0]["instructions"]
        self.assertIn("put order/channel verification in facts_to_verify", instructions)
        self.assertIn("Keep this internal check out of the customer-facing reply", instructions)
        self.assertIn("ask the customer for their Amazon order number in the proposed reply", instructions)
        self.assertIn("do not ask for it again", instructions)
        self.assertIn("tracking details will follow when the replacement ships", instructions)
        self.assertIn("Do not claim that a tracking number already exists", instructions)

    def test_order_number_field_is_passed_to_draft_without_claiming_verification(self):
        ticket = {**TICKET, "custom_fields": [{"id": "29113177850258",
                                                "value": "113-4771136-5412247"}]}
        with (patch.object(bot, "client", FakeClient(RESULT)),
              patch.object(bot, "get_user_first_name", return_value="Stella")):
            bot.compose_openai_draft(ticket, [CUSTOMER])
            request = bot.client.calls[0]
        case = json.loads(request["input"])
        self.assertEqual(case["order_number_provided_unverified"], "113-4771136-5412247")
        self.assertIsNone(case["verified_order_facts"])

    def test_customer_order_number_is_available_before_field_is_filled(self):
        customer = {**CUSTOMER, "body": "My Amazon order is 113-4771136-5412247."}
        with (patch.object(bot, "client", FakeClient(RESULT)),
              patch.object(bot, "get_user_first_name", return_value="Stella")):
            bot.compose_openai_draft(TICKET, [customer])
            request = bot.client.calls[0]
        self.assertEqual(json.loads(request["input"])["order_number_provided_unverified"],
                         "113-4771136-5412247")

    def test_ticket_707_style_request_exposes_missing_order_id_to_model(self):
        ticket = {**TICKET, "subject": "Free replacement", "custom_fields": []}
        customer = {**CUSTOMER, "body": (
            "Hi...the organizer I purchased from Amazon is nice, but I need the height "
            "to be 1.5 inches shorter. Thank you so much. Stella Fowlkes")}
        thanks = {**CUSTOMER, "id": 72, "body": "Ok...thank you"}
        with (patch.object(bot, "client", FakeClient(RESULT)),
              patch.object(bot, "get_user_first_name", return_value="Stella")):
            bot.compose_openai_draft(ticket, [customer, thanks])
            request = bot.client.calls[0]
        case = json.loads(request["input"])
        self.assertIsNone(case["order_number_provided_unverified"])
        self.assertEqual(len(case["conversation"]), 2)
        self.assertIn("1.5 inches shorter", case["conversation"][0]["body"])
        self.assertIn("ask the customer for their Amazon order number", request["instructions"])

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


class CollaborationDecisionTests(unittest.TestCase):
    def setUp(self):
        self.real_authorize = bot.collaboration_decline_authorization
        patcher = patch.object(bot, "collaboration_decline_authorization", return_value=None)
        self.authorize = patcher.start()
        self.addCleanup(patcher.stop)

    def ticket(self, decision="collab_declined", **overrides):
        return {**TICKET, "custom_fields": [
            {"id": bot.Z_INQUIRY_FIELD, "value": "collaborations"},
            {"id": bot.Z_COLLAB_STATUS_FIELD, "value": decision},
        ], **overrides}

    def test_only_saved_collaboration_fields_authorize_a_decision(self):
        self.assertIsNone(bot.collaboration_decision(TICKET))
        self.assertIsNone(bot.collaboration_decision(self.ticket(None)))
        ticket = self.ticket()
        ticket["custom_fields"][0]["value"] = "returns"
        self.assertIsNone(bot.collaboration_decision(ticket))
        self.assertEqual(bot.collaboration_decision(self.ticket()), "collab_declined")

    def audit(self, decision="collab_declined", **overrides):
        return {"id": 900, "author_id": 99, "created_at": "2026-10-18T15:00:00Z",
                "via": {"channel": "web"}, "events": [
                    {"type": "Change", "field_name": bot.Z_COLLAB_STATUS_FIELD,
                     "value": decision, "previous_value": None}], **overrides}

    def test_public_decline_requires_new_web_selection_by_staff(self):
        ticket = self.ticket(via={"channel": "email"})
        for decision in ("collab_declined", "collab_declined_audience_reach"):
            audit = self.audit(decision)
            with patch.object(bot, "z_get", side_effect=[{"audits": [audit]},
                                                         {"user": {"role": "agent"}}]):
                self.assertEqual(self.real_authorize(ticket, decision), 900)

    def test_old_or_nonhuman_declines_never_authorize_public_send(self):
        ticket = self.ticket(via={"channel": "web"})
        cases = [
            self.audit(created_at="2026-10-18T14:23:59Z"),
            self.audit(via={"channel": "api"}),
            self.audit(via={"channel": "rule"}),
            self.audit("collab_approved"),
            self.audit(events=[{"type": "Change", "field_name": bot.Z_COLLAB_STATUS_FIELD,
                                "value": "collab_declined", "previous_value": "collab_declined"}]),
            self.audit(events=self.audit()["events"] + [{"type": "Comment", "public": True}]),
        ]
        for audit in cases:
            with self.subTest(audit=audit), patch.object(bot, "z_get", return_value={"audits": [audit]}):
                self.assertIsNone(self.real_authorize(ticket, "collab_declined"))
        for role in ("end-user", None):
            with patch.object(bot, "z_get", side_effect=[{"audits": [self.audit()]},
                                                         {"user": {"role": role}}]):
                self.assertIsNone(self.real_authorize(ticket, "collab_declined"))

    def test_public_reply_after_selection_blocks_automated_double_response(self):
        audit = {"id": 901, "created_at": "2026-10-18T15:01:00Z",
                 "events": [{"type": "Comment", "public": True}]}
        with patch.object(bot, "z_get", return_value={"audits": [audit, self.audit()]}):
            self.assertIsNone(self.real_authorize(self.ticket(via={"channel": "web"}), "collab_declined"))

    def test_oldest_first_audits_still_use_latest_selection(self):
        initial = {"id": 800, "created_at": "2026-10-18T14:00:00Z",
                   "events": [{"type": "Comment", "public": True}]}
        old = self.audit("collab_approved", id=850, created_at="2026-10-18T14:23:00Z")
        for audits in ([initial, old, self.audit()], [self.audit(), initial, old]):
            with patch.object(bot, "z_get", side_effect=[{"audits": audits},
                                                         {"user": {"role": "agent"}}]):
                self.assertEqual(self.real_authorize(self.ticket(via={"channel": "web"}), "collab_declined"), 900)

    def test_protected_channels_and_sent_guard_never_authorize_public_send(self):
        cases = [self.ticket(via={"channel": "api"}), self.ticket(),
                 self.ticket(via={"channel": "email"}, tags=["channelreply"]),
                 self.ticket(via={"channel": "email", "source": {
                     "from": {"address": "buyer@marketplace.amazon.com"}}}),
                 self.ticket(via={"channel": "web"}, tags=[bot.COLLAB_DECLINE_SENT_TAG])]
        for ticket in cases:
            with self.subTest(ticket=ticket), patch.object(bot, "z_get") as get:
                self.assertIsNone(self.real_authorize(ticket, "collab_declined"))
            get.assert_not_called()

    def test_audit_pagination_checks_latest_actual_field_change(self):
        with patch.object(bot, "z_get", side_effect=[
                {"audits": [], "meta": {"has_more": True, "after_cursor": "next"}},
                {"audits": [self.audit()]}, {"user": {"role": "admin"}}]) as get:
            self.assertEqual(self.real_authorize(self.ticket(via={"channel": "web"}), "collab_declined"), 900)
        self.assertEqual(get.call_args_list[1].args[1]["page[after]"], "next")

    def test_both_declines_send_only_reply_and_atomic_permanent_guard(self):
        self.authorize.return_value = 900
        result = {**RESULT, "route": "creator", "facts_to_verify": [],
                  "reply": "Hi Maya, unfortunately we won't be moving forward with this collaboration. Noe"}
        for decision in ("collab_declined", "collab_declined_audience_reach"):
            ticket = self.ticket(decision, via={"channel": "web"}, tags=["keep_me"])
            with (patch.object(bot, "generate_collaboration_draft", return_value=result),
                  patch.object(bot, "get_user_first_name", return_value="Maya"),
                  patch.object(bot, "fetch_ticket", return_value=ticket),
                  patch.object(bot, "get_ticket_comments", return_value=[CUSTOMER]),
                  patch.object(bot, "z_put") as put):
                self.assertTrue(bot.handle_collaboration_decision(ticket, [CUSTOMER]))
            update = put.call_args.args[1]["ticket"]
            self.assertEqual(update["comment"], {"public": True, "body": result["reply"]})
            self.assertIn(bot.COLLAB_DECLINE_SENT_TAG, update["tags"])
            self.assertIn("ai_collab_sent_audit_900", update["tags"])
            self.assertIn("keep_me", update["tags"])
            self.assertTrue(update["safe_update"])
            self.assertNotIn("status", update)
            self.assertNotIn("custom_fields", update)

    def test_changed_authorization_during_generation_does_not_send(self):
        self.authorize.side_effect = [900, 901]
        ticket = self.ticket()
        result = {**RESULT, "route": "creator", "facts_to_verify": [],
                  "reply": "Hi Maya, unfortunately we won't be moving forward with this collaboration. Noe"}
        with (patch.object(bot, "generate_collaboration_draft", return_value=result),
              patch.object(bot, "get_user_first_name", return_value="Maya"),
              patch.object(bot, "fetch_ticket", return_value=ticket),
              patch.object(bot, "get_ticket_comments", return_value=[CUSTOMER]),
              patch.object(bot, "z_put") as put):
            self.assertFalse(bot.handle_collaboration_decision(ticket, [CUSTOMER]))
        put.assert_not_called()

    def test_public_send_that_succeeds_but_times_out_is_not_repeated(self):
        self.authorize.return_value = 900
        ticket = self.ticket()
        comments = [CUSTOMER]
        result = {**RESULT, "route": "creator", "facts_to_verify": [],
                  "reply": "Hi Maya, unfortunately we won't be moving forward with this collaboration. Noe"}

        def save_then_timeout(_path, data):
            ticket["tags"] = data["ticket"]["tags"]
            comments.append({"id": 72, "author_id": 99, **data["ticket"]["comment"]})
            raise TimeoutError("The write succeeded but its response was lost")

        with (patch.object(bot, "generate_collaboration_draft", return_value=result) as generate,
              patch.object(bot, "get_user_first_name", return_value="Maya"),
              patch.object(bot, "fetch_ticket", return_value=ticket),
              patch.object(bot, "get_ticket_comments", return_value=comments),
              patch.object(bot, "z_put", side_effect=save_then_timeout) as put):
            with self.assertRaises(TimeoutError):
                bot.handle_collaboration_decision(ticket, comments)
            self.assertFalse(bot.handle_collaboration_decision(ticket, comments))
        self.assertTrue(comments[-1]["public"])
        put.assert_called_once()
        generate.assert_called_once()

    def test_uncertain_or_internal_output_stays_private(self):
        self.authorize.return_value = 900
        for result in (RESULT, {**RESULT, "route": "creator", "facts_to_verify": [],
                               "reply": "AGENT NOTES: this is private information, do not send this note."}):
            ticket = self.ticket()
            with (patch.object(bot, "generate_collaboration_draft", return_value=result),
                  patch.object(bot, "get_user_first_name", return_value="Maya"),
                  patch.object(bot, "fetch_ticket", return_value=ticket),
                  patch.object(bot, "get_ticket_comments", return_value=[CUSTOMER]),
                  patch.object(bot, "z_put") as put):
                self.assertTrue(bot.handle_collaboration_decision(ticket, [CUSTOMER]))
            update = put.call_args.args[1]["ticket"]
            self.assertFalse(update["comment"]["public"])
            self.assertIn("AUTO-SEND WITHHELD", update["comment"]["body"])
            self.assertNotIn(bot.COLLAB_DECLINE_SENT_TAG, update["tags"])

    def test_approval_remains_private(self):
        ticket = self.ticket("collab_approved")
        with (patch.object(bot, "generate_collaboration_draft", return_value=RESULT),
              patch.object(bot, "get_user_first_name", return_value="Maya"),
              patch.object(bot, "fetch_ticket", return_value=ticket),
              patch.object(bot, "get_ticket_comments", return_value=[CUSTOMER]),
              patch.object(bot, "z_put") as put):
            self.assertTrue(bot.handle_collaboration_decision(ticket, [CUSTOMER]))
        self.assertFalse(put.call_args.args[1]["ticket"]["comment"]["public"])

    def test_creator_classification_is_saved_with_the_private_intake_note(self):
        result = {**RESULT, "route": "creator", "needs_human_decision": True}
        for ticket, expected in ((TICKET, True), (self.ticket(None), False)):
            with (patch.object(bot, "fetch_ticket", return_value=ticket),
                  patch.object(bot, "get_ticket_comments", return_value=[CUSTOMER]),
                  patch.object(bot, "z_put") as put):
                self.assertTrue(bot.post_internal_draft_once(42, 71, result))
            update = put.call_args.args[1]["ticket"]
            self.assertEqual("custom_fields" in update, expected)
            if expected:
                self.assertEqual(update["custom_fields"], [
                    {"id": int(bot.Z_INQUIRY_FIELD), "value": "collaborations"}])
            self.assertFalse(update["comment"]["public"])

    def test_creator_backfill_uses_latest_model_draft_and_only_empty_field(self):
        creator = {"id": 72, "public": False, "body": bot.format_internal_draft(
            71, {**RESULT, "route": "creator"})}
        public_spoof = {**creator, "public": True, "author_id": 5}
        newer = {**CUSTOMER, "id": 73, "body": "A different request"}
        self.assertFalse(bot.latest_customer_has_creator_draft(TICKET, [CUSTOMER, public_spoof]))
        self.assertFalse(bot.latest_customer_has_creator_draft(TICKET, [CUSTOMER, creator, newer]))
        with (patch.object(bot, "fetch_ticket", return_value=TICKET),
              patch.object(bot, "get_ticket_comments", return_value=[CUSTOMER, creator]),
              patch.object(bot, "z_put") as put):
            self.assertTrue(bot.populate_collaboration_type_if_missing(TICKET, [CUSTOMER, creator]))
        self.assertNotIn("comment", put.call_args.args[1]["ticket"])
        self.assertNotIn("status", put.call_args.args[1]["ticket"])
        with patch.object(bot, "z_put") as put:
            self.assertFalse(bot.populate_collaboration_type_if_missing(self.ticket(None), [CUSTOMER, creator]))
        put.assert_not_called()

    def test_private_pilot_never_sets_the_inquiry_field(self):
        with (patch.object(bot, "fetch_ticket", return_value=TICKET),
              patch.object(bot, "get_ticket_comments", return_value=[CUSTOMER]),
              patch.object(bot, "z_put") as put):
            self.assertTrue(bot.post_internal_draft_once(42, 71, {**RESULT, "route": "creator"}, pilot=True))
        self.assertNotIn("custom_fields", put.call_args.args[1]["ticket"])

    def test_decision_after_existing_intake_draft_is_private_and_preserves_fields(self):
        ticket = self.ticket(tags=["collaborations", "source_shopify_form", "tracking_sent_abc"])
        intake = {"id": 72, "public": False, "author_id": 99,
                  "body": bot.format_internal_draft(71, RESULT)}
        comments = [CUSTOMER, intake]
        with (patch.object(bot, "generate_collaboration_draft", return_value=RESULT) as generate,
              patch.object(bot, "get_user_first_name", return_value="Maya"),
              patch.object(bot, "fetch_ticket", return_value=ticket),
              patch.object(bot, "get_ticket_comments", return_value=comments),
              patch.object(bot, "z_put") as put):
            self.assertTrue(bot.handle_collaboration_decision(ticket, comments))
        generate.assert_called_once()
        payload = put.call_args.args[1]["ticket"]
        self.assertFalse(payload["comment"]["public"])
        self.assertIn("supersedes earlier UNSENT", payload["comment"]["body"])
        self.assertTrue(payload["safe_update"])
        self.assertEqual(payload["updated_stamp"], ticket["updated_at"])
        self.assertEqual(set(payload), {"comment", "tags", "safe_update", "updated_stamp"})
        self.assertIn("tracking_sent_abc", payload["tags"])
        self.assertIn(HANDLED_PREFIX + "collab_declined", payload["tags"])
        self.assertIn(bot.collaboration_comment_guard(71), payload["tags"])

    def test_repeat_poll_or_timeout_does_not_repeat_the_decision(self):
        ticket = self.ticket()
        saved = {**ticket, "tags": [HANDLED_PREFIX + "collab_declined"]}
        with (patch.object(bot, "generate_collaboration_draft", return_value=RESULT) as generate,
              patch.object(bot, "get_user_first_name", return_value="Maya"),
              patch.object(bot, "fetch_ticket", return_value=ticket),
              patch.object(bot, "get_ticket_comments", return_value=[CUSTOMER]),
              patch.object(bot, "z_put", side_effect=TimeoutError) as put):
            with self.assertRaises(TimeoutError):
                bot.handle_collaboration_decision(ticket, [CUSTOMER])
            self.assertFalse(bot.handle_collaboration_decision(saved, [CUSTOMER]))
        put.assert_called_once()
        generate.assert_called_once()

    def test_private_note_also_prevents_repeat_if_state_tag_missing(self):
        saved = {"id": 72, "public": False,
                 "body": "AI_COLLAB_DECISION=collab_declined\nSaved decision"}
        with patch.object(bot, "generate_collaboration_draft") as generate:
            self.assertFalse(bot.handle_collaboration_decision(self.ticket(), [CUSTOMER, saved]))
        generate.assert_not_called()

    def test_changed_decision_or_public_reply_during_generation_is_not_written(self):
        ticket = self.ticket()
        newer = {**CUSTOMER, "id": 73, "body": "Here is an updated proposal."}
        for fresh, comments in ((self.ticket("collab_approved"), [CUSTOMER]),
                                (ticket, [CUSTOMER, newer])):
            with (self.subTest(fresh=fresh, comments=comments),
                  patch.object(bot, "generate_collaboration_draft", return_value=RESULT),
                  patch.object(bot, "get_user_first_name", return_value="Maya"),
                  patch.object(bot, "fetch_ticket", return_value=fresh),
                  patch.object(bot, "get_ticket_comments", return_value=comments),
                  patch.object(bot, "z_put") as put):
                self.assertFalse(bot.handle_collaboration_decision(ticket, [CUSTOMER]))
            put.assert_not_called()

    def test_competing_worker_state_prevents_second_write(self):
        ticket = self.ticket()
        fresh = {**ticket, "tags": [HANDLED_PREFIX + "collab_declined"]}
        with (patch.object(bot, "generate_collaboration_draft", return_value=RESULT),
              patch.object(bot, "get_user_first_name", return_value="Maya"),
              patch.object(bot, "fetch_ticket", return_value=fresh),
              patch.object(bot, "get_ticket_comments", return_value=[CUSTOMER]),
              patch.object(bot, "z_put") as put):
            self.assertFalse(bot.handle_collaboration_decision(ticket, [CUSTOMER]))
        put.assert_not_called()

    def test_decision_changes_a_to_b_to_a_each_create_one_private_draft(self):
        ticket = self.ticket(tags=["keep_me"])
        comments = [CUSTOMER]

        def save(_path, data):
            update = data["ticket"]
            ticket["tags"] = update["tags"]
            comments.append({"id": len(comments) + 72, **update["comment"]})

        with (patch.object(bot, "generate_collaboration_draft", return_value=RESULT),
              patch.object(bot, "get_user_first_name", return_value="Maya"),
              patch.object(bot, "fetch_ticket", side_effect=lambda _id: ticket),
              patch.object(bot, "get_ticket_comments", side_effect=lambda _id: comments),
              patch.object(bot, "z_put", side_effect=save) as put):
            for value in ("collab_declined", "collab_approved", "collab_declined"):
                ticket["custom_fields"][1]["value"] = value
                self.assertTrue(bot.handle_collaboration_decision(ticket, comments))
                self.assertFalse(bot.handle_collaboration_decision(ticket, comments))
        self.assertEqual(put.call_count, 3)
        self.assertIn("keep_me", ticket["tags"])
        self.assertEqual([t for t in ticket["tags"] if t.startswith(HANDLED_PREFIX)],
                         [HANDLED_PREFIX + "collab_declined"])

    def test_solved_ticket_gets_private_decision_without_reopening(self):
        ticket = self.ticket(status="solved")
        with (patch.object(bot, "generate_collaboration_draft", return_value=RESULT),
              patch.object(bot, "get_user_first_name", return_value="Maya"),
              patch.object(bot, "fetch_ticket", return_value=ticket),
              patch.object(bot, "get_ticket_comments", return_value=[CUSTOMER]),
              patch.object(bot, "z_put") as put):
            self.assertTrue(bot.handle_collaboration_decision(ticket, [CUSTOMER]))
        self.assertNotIn("status", put.call_args.args[1]["ticket"])

    def test_inflight_intake_draft_cannot_overwrite_selected_decision(self):
        for ticket in (self.ticket(), self.ticket(tags=[HANDLED_PREFIX + "collab_declined",
                                                       bot.collaboration_comment_guard(71)])):
            with (patch.object(bot, "fetch_ticket", return_value=ticket),
                  patch.object(bot, "get_ticket_comments", return_value=[CUSTOMER]),
                  patch.object(bot, "z_put") as put):
                self.assertFalse(bot.post_internal_draft_once(42, 71, RESULT))
            put.assert_not_called()

    def test_decision_generator_only_uses_public_messages_and_selected_reason(self):
        customer = {**CUSTOMER, "body": "I'd like to collaborate on a bag organizer video."}
        old_draft = {"id": 72, "public": False, "body": "Unsent: ask for social profiles."}
        client = FakeClient(RESULT)
        result = generate_collaboration_draft(
            client, "gpt-6.1-sol", "medium", self.ticket(), [customer, old_draft],
            "Maya", "collab_declined_audience_reach", {"instagram": "https://instagram.com/example"})
        case = json.loads(client.calls[0]["input"])
        self.assertEqual(case["human_selected_collaboration_decision"], "Declined — Audience Reach")
        self.assertEqual(len(case["conversation"]), 1)
        self.assertEqual(case["task"], "communicate_selected_decision")
        self.assertIs(client.calls[0]["store"], False)
        self.assertTrue(result["needs_human_decision"])
        self.assertEqual(result["route"], "creator")

    def test_follow_up_after_decision_uses_decision_context(self):
        ticket = self.ticket("collab_approved")
        with (patch.object(bot, "generate_collaboration_draft", return_value=RESULT) as generate,
              patch.object(bot, "get_user_first_name", return_value="Maya")):
            bot.compose_openai_draft(ticket, [CUSTOMER])
        self.assertTrue(generate.call_args.kwargs["follow_up"])
        self.assertEqual(generate.call_args.args[6], "collab_approved")

    def test_creator_field_alone_prevents_customer_replacement_tracking(self):
        ticket = self.ticket()
        ticket["custom_fields"].append({"id": "tracking_field", "value": "ABC123"})
        with (patch.object(bot, "Z_TRACKING_FIELD", "tracking_field"),
              patch.object(bot, "fetch_ticket", return_value=ticket),
              patch.object(bot, "get_ticket_comments", return_value=[CUSTOMER]),
              patch.object(bot, "z_put") as put):
            self.assertFalse(bot.handle_tracking_if_any(ticket))
        put.assert_not_called()


if __name__ == "__main__":
    unittest.main()
