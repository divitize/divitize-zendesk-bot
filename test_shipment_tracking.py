"""Offline only: no 17TRACK, OpenAI, Zendesk or real credentials are used."""

import json
import unittest
from unittest.mock import patch

from test_ai_draft import bot, FakeClient, RESULT, TICKET, CUSTOMER
import shipment_tracking as tracking
from openai_draft import generate_draft


NUMBER = "VR976103818YP"
NEW_NUMBER = "VR991950890YP"
MESSAGE = {**CUSTOMER, "body": "Where is my package?\nYanwen tracking number: " + NUMBER}


def details(sub="Exception_Returned", status="Exception", description="Package return, Received by shipper"):
    return {"number": NUMBER, "carrier": 190012,
            "track_info": {"latest_status": {"status": status, "sub_status": sub},
                           "latest_event": {"time_utc": "2026-10-13T13:09:00Z", "description": description},
                           "time_metrics": {"estimated_delivery_date": {"source": None, "from": None, "to": None}}}}


def response(accepted=None, rejected=None, **extra):
    return {"code": 0, "data": {"accepted": accepted or [], "rejected": rejected or [], **extra}}


def rejected(code):
    return response(rejected=[{"number": NUMBER, "error": {"code": code}}])


class HTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        class R:
            status_code = 200
            def json(self):
                return result
        return R()


class ShipmentTrackingTests(unittest.TestCase):
    def setUp(self):
        tracking._cache.clear()
        tracking._pending.clear()
        self.sleep = patch.object(tracking.time, "sleep").start()
        self.addCleanup(patch.stopall)

    def test_ticket_787_number_is_read_from_customer_quoted_shipping_email(self):
        body = ("Good day, I am trying to track my package. Can you provide current details?\n"
                "On Sunday, October 4, 2026, Divitize wrote:\nORDER #1865\n"
                "yanwen tracking number: VR976103818YP")
        candidate = tracking.tracking_request([{**MESSAGE, "body": body}], 5)
        self.assertEqual(candidate["numbers"], [NUMBER])
        self.assertFalse(candidate["ambiguous"])

    def test_direct_link_and_duplicate_label_are_one_number(self):
        body = f"Please track this package. Tracking number: {NUMBER}\nhttps://t.17track.net/en#nums={NUMBER}"
        self.assertEqual(tracking.tracking_request([{**MESSAGE, "body": body}], 5)["numbers"], [NUMBER])

    def test_multiple_numbers_are_ambiguous_not_arbitrarily_selected(self):
        body = f"Please check tracking number: {NUMBER}; tracking number: {NEW_NUMBER}"
        candidate = tracking.tracking_request([{**MESSAGE, "body": body}], 5)
        self.assertTrue(candidate["ambiguous"])
        with patch.object(tracking, "lookup_tracking") as lookup:
            result = tracking.tracking_context([{**MESSAGE, "body": body}], 5, True, "fake")
        lookup.assert_not_called()
        self.assertEqual(result["outcome"], "ambiguous")

    def test_numeric_tracking_is_accepted_but_order_and_phone_without_tracking_label_are_not(self):
        body = "Where is my package? Tracking number: 123456789012"
        self.assertEqual(tracking.tracking_request([{**MESSAGE, "body": body}], 5)["numbers"], ["123456789012"])
        for body in ("Where is order #1865?", "Track order 113-4771136-5412247",
                     "Where is my package? Phone 123456789012", "Where is order 3325573-1865?"):
            with self.subTest(body=body):
                self.assertIsNone(tracking.tracking_request([{**MESSAGE, "body": body}], 5))

    def test_bare_reply_after_agent_tracking_request(self):
        agent = {"id": 70, "public": True, "author_id": 99, "body": "Please send your tracking number."}
        self.assertEqual(tracking.tracking_request([agent, {**MESSAGE, "body": NUMBER}], 5)["numbers"], [NUMBER])
        self.assertIsNone(tracking.tracking_request([{**MESSAGE, "body": NUMBER}], 5))

    def test_private_notes_do_not_supply_tracking_and_latest_public_replacement_is_used(self):
        private = {"public": False, "author_id": 99, "body": "Tracking number: " + NEW_NUMBER}
        self.assertEqual(tracking.tracking_request([MESSAGE, private], 5)["numbers"], [NUMBER])
        new = {"id": 72, "public": True, "author_id": 99,
               "body": "Here is the tracking number for your replacement: " + NEW_NUMBER}
        follow = {**MESSAGE, "id": 73, "body": "Where is my package now?"}
        self.assertEqual(tracking.tracking_request([MESSAGE, new, follow], 5)["numbers"], [NEW_NUMBER])

    def test_thanks_or_fit_request_with_quoted_notification_does_not_spend_quota(self):
        for prefix in ("Thanks!", "The insert is 2 cm too short; please make a replacement."):
            body = prefix + "\nOn Sunday, Divitize wrote:\nTrack your shipment. Tracking number: " + NUMBER
            self.assertIsNone(tracking.tracking_request([{**MESSAGE, "body": body}], 5))

    def test_missing_key_or_disabled_feature_never_contacts_provider(self):
        with patch.object(tracking, "lookup_tracking", wraps=tracking.lookup_tracking) as lookup:
            context = tracking.tracking_context([MESSAGE], 5, False, "fake-secret")
        self.assertEqual(context["outcome"], "unavailable")
        self.assertEqual(lookup.call_args.args[1], "")
        self.assertNotIn("fake-secret", json.dumps(context))

    def test_existing_shipment_is_read_without_registration_or_quota_charge(self):
        http = HTTP([response([details()])])
        result = tracking.lookup_tracking(NUMBER, "fake-key", http)
        self.assertTrue(result["verified"])
        self.assertEqual(result["outcome"], "returned_to_sender")
        self.assertEqual(len(http.calls), 1)
        self.assertTrue(http.calls[0][0].endswith("/gettrackinfo"))
        request = http.calls[0][1]
        self.assertEqual(request["json"], [{"number": NUMBER}])
        self.assertFalse(request["allow_redirects"])
        self.assertEqual(request["headers"]["17token"], "fake-key")
        self.assertNotIn("fake-key", json.dumps(result))

    def test_registers_absent_number_once_after_quota_check_then_reads_standard_result(self):
        http = HTTP([rejected(-18019902), response(quota_remain=200),
                     response([{"number": NUMBER, "carrier": 190012}]), response([details()])])
        result = tracking.lookup_tracking(NUMBER, "fake-key", http)
        self.assertEqual(result["outcome"], "returned_to_sender")
        self.assertEqual([u.rsplit("/", 1)[-1] for u, _ in http.calls],
                         ["gettrackinfo", "getquota", "register", "gettrackinfo"])
        self.assertEqual(http.calls[2][1]["json"], [{"number": NUMBER}])
        self.assertEqual(http.calls[3][1]["json"], [{"number": NUMBER, "carrier": 190012}])

    def test_exhausted_or_invalid_quota_cannot_register_new_shipments(self):
        for quota in (0, -1, None, "200", True):
            tracking._cache.clear()
            http = HTTP([rejected(-18019902), response(quota_remain=quota)])
            result = tracking.lookup_tracking(NUMBER, "fake-key", http)
            self.assertFalse(result["verified"])
            self.assertEqual(len(http.calls), 2)

    def test_no_additional_charges_for_cached_result_or_duplicate_registration_error(self):
        http = HTTP([rejected(-18019902), response(quota_remain=200),
                     rejected(-18019901), response([details()])])
        first = tracking.lookup_tracking(NUMBER, "fake-key", http, now=lambda: 1000)
        second = tracking.lookup_tracking(NUMBER, "fake-key", http, now=lambda: 1005)
        self.assertEqual(first, second)
        self.assertEqual(len(http.calls), 4)
        first["latest_event"]["description"] = "changed locally"
        self.assertNotEqual(tracking.lookup_tracking(NUMBER, "fake-key", http, now=lambda: 1006), first)

    def test_timeout_after_registration_is_followed_by_read_not_blind_registration(self):
        http = HTTP([rejected(-18019902), response(quota_remain=200), TimeoutError("fake-key"),
                     response([details()])])
        first = tracking.lookup_tracking(NUMBER, "fake-key", http, now=lambda: 1000)
        self.assertFalse(first["verified"])
        self.assertNotIn("fake-key", json.dumps(first))
        second = tracking.lookup_tracking(NUMBER, "fake-key", http, now=lambda: 1700)
        self.assertTrue(second["verified"])
        self.assertEqual(sum(u.endswith("/register") for u, _ in http.calls), 1)

    def test_initial_pending_results_defer_draft_for_up_to_five_minutes(self):
        pending = details(description="")
        http = HTTP([response([pending]), response([pending])])
        with self.assertRaises(tracking.TrackingPending):
            tracking.lookup_tracking(NUMBER, "fake-key", http, now=lambda: 1000)
        with self.assertRaises(tracking.TrackingPending):
            tracking.lookup_tracking(NUMBER, "fake-key", http, now=lambda: 1005)
        self.assertEqual(len(http.calls), 1)
        result = tracking.lookup_tracking(NUMBER, "fake-key", http, now=lambda: 1301)
        self.assertEqual(result["outcome"], "pending")
        self.assertFalse(result["verified"])

    def test_returning_does_not_claim_returned_and_old_return_event_is_not_used(self):
        for entry, expected in ((details(sub="Exception_Returning", description="Returned toward sender"), "returning_to_sender"),
                                (details(sub="InTransit_Other", status="InTransit", description="At sorting center"), "carrier_update")):
            tracking._cache.clear()
            entry["track_info"]["tracking"] = {"providers": [{"events": [{"description": "Package return, Received by shipper"}]}]}
            result = tracking.lookup_tracking(NUMBER, "fake-key", HTTP([response([entry])]))
            self.assertEqual(result["outcome"], expected)

    def test_plain_received_by_shipper_is_not_returned(self):
        entry = details(sub="InTransit_PickedUp", status="InTransit", description="Received by shipper")
        self.assertEqual(tracking.lookup_tracking(NUMBER, "fake-key", HTTP([response([entry])]))["outcome"],
                         "carrier_update")

    def test_conflicting_delivery_or_multiple_carrier_results_are_not_verified(self):
        for entries, expected in (([details(status="Delivered")], "conflicting"),
                                  ([details(), {**details(), "carrier": 123}], "ambiguous")):
            tracking._cache.clear()
            result = tracking.lookup_tracking(NUMBER, "fake-key", HTTP([response(entries)]))
            self.assertFalse(result["verified"])
            self.assertEqual(result["outcome"], expected)

    def test_invalid_responses_and_provider_errors_fail_closed_without_echoing_secrets(self):
        for payload in ({"code": -1, "error": "fake-key"}, {"code": 0, "data": "fake-key"},
                        TimeoutError("fake-key"), response(rejected=[{"number": NUMBER, "error": {"code": -18019903}}])):
            tracking._cache.clear()
            result = tracking.lookup_tracking(NUMBER, "fake-key", HTTP([payload]))
            self.assertFalse(result["verified"])
            self.assertNotIn("fake-key", json.dumps(result))

    def test_real_time_translation_and_email_endpoints_are_not_permitted(self):
        for endpoint in ("getRealTimeTrackInfo", "changeinfo", "push", "deletetrack", "https://evil.invalid"):
            http = HTTP([])
            with self.assertRaises(ValueError):
                tracking._post("fake", endpoint, [], http)
            self.assertFalse(http.calls)

    def test_tracking_evidence_and_approved_replacement_wording_reach_one_sol_call(self):
        shipment = tracking.lookup_tracking(NUMBER, "fake", HTTP([response([details()])]))
        client = FakeClient(RESULT)
        generate_draft(client, "gpt-6.1-sol", "medium", TICKET, [MESSAGE], "Denise", "shopify",
                       "3325573-1865", shipment_tracking=shipment, replacement_tracking_entered=True)
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        case = json.loads(call["input"])
        self.assertEqual(case["shipment_tracking"], shipment)
        self.assertTrue(case["replacement_tracking_entered"])
        self.assertEqual(case["order_number_provided_unverified"], "3325573-1865")
        self.assertIsNone(case["verified_order_facts"])
        self.assertEqual(call["reasoning"], {"effort": "medium"})
        self.assertIn("We've now arranged a replacement for you, and we'll send its tracking details in a separate update.",
                      call["instructions"])
        self.assertIn("Do not require a replacement tracking number before drafting this wording", call["instructions"])
        self.assertIn("Process the replacement immediately when approving/sending this reply", call["instructions"])
        self.assertIn("Always open ordinary support replies, including follow-ups", call["instructions"])
        self.assertIn("Do not omit the greeting", call["instructions"])
        self.assertIn("Do not guess the reason for the return", call["instructions"])

    def test_agent_notes_contain_evidence_but_customer_reply_is_not_rewritten_as_template(self):
        shipment = tracking.lookup_tracking(NUMBER, "fake", HTTP([response([details()])]))
        result = {**RESULT, "_shipment_tracking": shipment}
        note = bot.format_internal_draft(71, result)
        customer_part, agent_part = note.split("AGENT NOTES — do not send:")
        self.assertNotIn(NUMBER, customer_part)
        self.assertIn(RESULT["reply"], customer_part)
        self.assertIn(NUMBER, agent_part)
        self.assertIn("Package return, Received by shipper", agent_part)
        self.assertIn("2026-10-13", agent_part)

    def test_tracking_context_never_reads_or_populates_replacement_field(self):
        ticket = {**TICKET, "custom_fields": [{"id": "tracking", "value": NEW_NUMBER}]}
        context = {"number": NUMBER, "outcome": "unavailable", "verified": False, "reason": "manual check"}
        with (patch.object(bot, "Z_TRACKING_FIELD", "tracking"), patch.object(bot, "client", FakeClient(RESULT)),
              patch.object(bot, "get_user_first_name", return_value="Denise"),
              patch.object(bot, "tracking_context", return_value=context) as lookup,
              patch.object(bot, "z_put") as put):
            result = bot.compose_openai_draft(ticket, [MESSAGE])
            case = json.loads(bot.client.calls[0]["input"])
        put.assert_not_called()
        self.assertNotIn(NEW_NUMBER, json.dumps(lookup.call_args.args))
        self.assertTrue(case["replacement_tracking_entered"])
        self.assertIn(NUMBER, bot.format_internal_draft(71, result))

    def test_preview_cannot_spend_quota_even_when_production_env_is_copied(self):
        with (patch.object(bot, "IS_PULL_REQUEST", True), patch.object(bot, "TRACK17_ENABLED", True),
              patch.object(bot, "TRACK17_API_KEY", "fake-secret"),
              patch.object(bot, "client", FakeClient(RESULT)),
              patch.object(bot, "get_user_first_name", return_value="Denise"),
              patch.object(bot, "tracking_context", return_value=None) as lookup):
            bot.compose_openai_draft(TICKET, [MESSAGE])
        self.assertIs(lookup.call_args.kwargs["enabled"], False)

    def test_tracking_draft_rechecks_replacement_number_order_and_public_snapshot(self):
        context = {"number": NUMBER, "outcome": "unavailable", "verified": False, "reason": "manual check"}
        with (patch.object(bot, "Z_TRACKING_FIELD", "tracking"),
              patch.object(bot, "client", FakeClient(RESULT)),
              patch.object(bot, "get_user_first_name", return_value="Denise"),
              patch.object(bot, "tracking_context", return_value=context)):
            result = bot.compose_openai_draft(TICKET, [MESSAGE])
        cases = [({**TICKET, "custom_fields": [{"id": "tracking", "value": NEW_NUMBER}]}, [MESSAGE]),
                 ({**TICKET, "requester_id": 8}, [MESSAGE]),
                 ({**TICKET, "custom_fields": [{"id": bot.Z_ORDER_FIELD, "value": "3325573-1865"}]}, [MESSAGE]),
                 (TICKET, [{**MESSAGE, "body": MESSAGE["body"] + " corrected"}])]
        for ticket, comments in cases:
            with (self.subTest(ticket=ticket, comments=comments), patch.object(bot, "Z_TRACKING_FIELD", "tracking"),
                  patch.object(bot, "fetch_ticket", return_value=ticket),
                  patch.object(bot, "get_ticket_comments", return_value=comments), patch.object(bot, "z_put") as put):
                self.assertFalse(bot.post_internal_draft_once(42, 71, result))
            put.assert_not_called()

    def test_successful_tracking_draft_writes_one_private_note_no_status_or_tracking_field(self):
        context = {"number": NUMBER, "outcome": "unavailable", "verified": False, "reason": "manual check"}
        with (patch.object(bot, "client", FakeClient(RESULT)),
              patch.object(bot, "get_user_first_name", return_value="Denise"),
              patch.object(bot, "tracking_context", return_value=context)):
            result = bot.compose_openai_draft(TICKET, [MESSAGE])
        saved = {"id": 72, "public": False, "author_id": 99, "body": bot.format_internal_draft(71, result)}
        with (patch.object(bot, "fetch_ticket", return_value=TICKET),
              patch.object(bot, "get_ticket_comments", side_effect=[[MESSAGE], [MESSAGE, saved]]),
              patch.object(bot, "z_put") as put):
            self.assertTrue(bot.post_internal_draft_once(42, 71, result))
            self.assertFalse(bot.post_internal_draft_once(42, 71, result))
        put.assert_called_once()
        payload = put.call_args.args[1]["ticket"]
        self.assertFalse(payload["comment"]["public"])
        self.assertNotIn("custom_fields", payload)
        self.assertNotIn("status", payload)
        self.assertTrue(payload["safe_update"])


if __name__ == "__main__":
    unittest.main()
