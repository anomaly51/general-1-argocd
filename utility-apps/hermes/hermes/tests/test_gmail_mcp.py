import base64
import importlib.util
import json
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("gmail_mcp", Path(__file__).parents[1] / "files/gmail_mcp.py")
mail = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mail)


class GmailTests(unittest.TestCase):
    def test_missing_credentials_is_empty(self):
        with patch.object(mail, "CREDENTIALS", Path("/nonexistent-hermes-test-file")):
            self.assertEqual(mail.list_accounts()["accounts"], [])

    def test_unknown_account_cannot_select_another_identity(self):
        with patch.object(mail, "accounts", return_value={"one@example.com": {}}):
            with self.assertRaisesRegex(ValueError, "Unknown account"):
                mail.access_token("two@example.com")

    def test_reject_arbitrary_urls_and_path_traversal(self):
        for path in ["https://example.com", "//example.com", "../users/other", "messages/%2e%2e", "messages?access_token=x"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                mail.request("one@example.com", "GET", path)

    def test_reject_authentication_in_query(self):
        with self.assertRaisesRegex(ValueError, "Authentication"):
            mail.request("one@example.com", "GET", "profile", {"access_token": "injected"})

    def test_mime_nested_body_and_attachment(self):
        body = base64.urlsafe_b64encode("Привет".encode()).decode().rstrip("=")
        result = mail.message_view({"id": "abc", "payload": {"parts": [
            {"mimeType": "multipart/alternative", "parts": [{"mimeType": "text/plain", "body": {"data": body}}]},
            {"filename": "cv.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "att", "size": 123}},
        ]}})
        self.assertEqual(result["bodies"][0]["text"], "Привет")
        self.assertEqual(result["attachments"][0]["attachment_id"], "att")

    def test_compose_defaults_to_draft_and_correct_sender(self):
        with patch.object(mail, "request", return_value={"id": "draft"}) as api:
            result = mail.gmail_compose("one@example.com", ["recipient@example.com"], "Hi", "Hello")
            self.assertFalse(result["sent"])
            self.assertEqual(api.call_args.args, ("one@example.com", "POST", "drafts"))
            encoded = api.call_args.kwargs["body"]["message"]["raw"]
            mime = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(encoded))
            self.assertEqual(mime["From"], "one@example.com")
            self.assertEqual(mime["To"], "recipient@example.com")

    def test_reply_preserves_thread_and_reference(self):
        original = {"threadId": "thread", "payload": {"headers": [{"name": "Message-ID", "value": "<orig@example.com>"}]}}
        with patch.object(mail, "request", side_effect=[original, {"id": "sent"}]) as api:
            mail.gmail_compose("one@example.com", ["recipient@example.com"], "Re: Hi", "Thanks", send=True, reply_to_message="msg")
            self.assertEqual(api.call_args.args, ("one@example.com", "POST", "messages/send"))
            payload = api.call_args.kwargs["body"]
            self.assertEqual(payload["threadId"], "thread")
            mime = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(payload["raw"]))
            self.assertEqual(mime["In-Reply-To"], "<orig@example.com>")

    def test_reject_recipient_header_injection(self):
        with self.assertRaises(ValueError):
            mail.gmail_compose("one@example.com", ["x@example.com\nBcc: other@example.com"], "Hi", "Hello")


class CalendarTests(unittest.TestCase):
    def test_create_is_idempotent_and_keeps_timezone_and_guests(self):
        with patch.object(mail, "calendar_request", return_value={}) as api:
            for _ in range(2):
                mail.calendar_create("one@example.com", "Meeting", "2026-11-20T14:00:00+02:00",
                                     "2026-11-20T15:00:00+02:00", "unique-meeting", attendees=["guest@example.com"], create_meet=True)
            a, b = api.call_args_list
            self.assertEqual(a.args[4]["id"], b.args[4]["id"])
            self.assertEqual(a.args[3]["sendUpdates"], "all")
            self.assertEqual(a.args[4]["start"]["timeZone"], "Europe/Kyiv")
            self.assertEqual(a.args[4]["attendees"], [{"email": "guest@example.com"}])
            self.assertEqual(a.args[4]["conferenceData"]["createRequest"]["requestId"], a.args[4]["id"])

    def test_reject_bad_ranges_before_api(self):
        for start, end in [("2026-11-20T15:00:00", "2026-11-20T16:00:00"),
                           ("2026-11-20T15:00:00+02:00", "2026-11-20T14:00:00+02:00"),
                           ("2026-11-20", "2026-11-20")]:
            with self.subTest(start=start), patch.object(mail, "calendar_request") as api:
                with self.assertRaises(ValueError):
                    mail.calendar_create("one@example.com", "Meeting", start, end, "test")
                api.assert_not_called()

    def test_all_day_dates_and_encoded_calendar(self):
        with patch.object(mail, "calendar_request", return_value={}) as api:
            mail.calendar_create("one@example.com", "Day", "2026-11-20", "2026-11-21", "day", calendar_id="x@y.com")
            self.assertEqual(api.call_args.args[2], "calendars/x%40y.com/events")
            self.assertEqual(api.call_args.args[4]["end"], {"date": "2026-11-21"})
        self.assertIn("%2F", mail.calendar_path("https://other.example"))
        with self.assertRaises(ValueError):
            mail.calendar_path("..")

    def test_listing_recurring_instances_and_pagination(self):
        with patch.object(mail, "calendar_request", return_value={"nextPageToken": "more"}) as api:
            result = mail.calendar_events("one@example.com", "2026-11-20T00:00:00+02:00", "2026-11-21T00:00:00+02:00", page_token="next")
            params = api.call_args.args[3]
            self.assertEqual(params["pageToken"], "next")
            self.assertEqual(params["singleEvents"], "true")
            self.assertEqual(result["result"]["nextPageToken"], "more")

    def test_update_preserves_omitted_fields_and_preconditions(self):
        existing = {"etag": '"one"', "attendees": [{"email": "guest@example.com"}], "description": "Keep me"}
        with patch.object(mail, "calendar_request", side_effect=[existing, {}]) as api:
            mail.calendar_update("one@example.com", "event", '"one"', {"summary": "Renamed"})
            self.assertEqual(api.call_args.args[1], "PATCH")
            self.assertEqual(api.call_args.args[4], {"summary": "Renamed"})
            self.assertEqual(api.call_args.kwargs["etag"], '"one"')
            self.assertEqual(api.call_args.args[3]["sendUpdates"], "all")

    def test_stale_update_and_cancellation_are_rejected(self):
        with patch.object(mail, "calendar_request", return_value={"etag": '"new"'}) as api:
            with self.assertRaisesRegex(RuntimeError, "changed"):
                mail.calendar_update("one@example.com", "event", '"old"', {"summary": "Change"})
            self.assertEqual(api.call_count, 1)
        with patch.object(mail, "calendar_request") as api:
            with self.assertRaises(ValueError):
                mail.calendar_update("one@example.com", "event", '"old"', {"status": "cancelled"})
            api.assert_not_called()

    def test_update_validates_new_end_against_existing_start(self):
        existing = {"etag": '"one"', "start": {"dateTime": "2026-11-20T15:00:00+02:00"},
                    "end": {"dateTime": "2026-11-20T16:00:00+02:00"}}
        with patch.object(mail, "calendar_request", return_value=existing) as api:
            with self.assertRaises(ValueError):
                mail.calendar_update("one@example.com", "event", '"one"', {"end": {"dateTime": "2026-11-20T14:00:00+02:00"}})
            self.assertEqual(api.call_count, 1)


if __name__ == "__main__":
    unittest.main()
