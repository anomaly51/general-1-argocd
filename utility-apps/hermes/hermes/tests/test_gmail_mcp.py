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


if __name__ == "__main__":
    unittest.main()
