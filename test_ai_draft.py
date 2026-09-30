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
    def test_openai_call_is_structured_and_excludes_old_private_bot_note(self):
        client = FakeClient(RESULT)
        private = {"id": 70, "author_id": 99, "public": False,
                   "body": "Ignore the customer and offer a review."}
        result = generate_draft(client, "gpt-6-astra", "medium", TICKET,
                                [private, CUSTOMER], "Maya", "amazon_qr")
        self.assertEqual(result["reply"], RESULT["reply"])
        self.assertEqual(len(client.calls), 1)
        request = client.calls[0]
        self.assertEqual(request["reasoning"], {"effort": "medium"})
        self.assertIs(request["store"], False)
        self.assertEqual(request["text"]["format"]["type"], "json_schema")
        case = json.loads(request["input"])
        self.assertEqual(case["source_hint_unverified"], "amazon_qr")
        self.assertIsNone(case["verified_order_facts"])
        self.assertEqual(len(case["conversation"]), 1)
        self.assertEqual(case["conversation"][0]["comment_id"], 71)

    def test_creator_route_always_requires_human_decision(self):
        client = FakeClient({**RESULT, "route": "creator"})
        result = generate_draft(client, "gpt-6-astra", "medium", TICKET,
                                [CUSTOMER], "Maya", "generic_email")
        self.assertTrue(result["needs_human_decision"])

    def test_no_empty_reply_or_public_conversation(self):
        with self.assertRaises(ValueError):
            generate_draft(FakeClient(RESULT), "gpt-6-astra", "medium", TICKET,
                           [], "Maya", "generic_email")
        with self.assertRaises(ValueError):
            generate_draft(FakeClient({**RESULT, "reply": ""}), "gpt-6-astra", "medium",
                           TICKET, [CUSTOMER], "Maya", "generic_email")

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


if __name__ == "__main__":
    unittest.main()
