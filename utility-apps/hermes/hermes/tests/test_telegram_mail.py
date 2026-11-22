import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location("telegram_mail", Path(__file__).parents[1] / "files/telegram_mail.py")
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)

ACCOUNT = {"id": "connection-1", "user": {"id": 100}, "is_enabled": True}
ADDRESS = "tracker-test@gmail.com"
INCOMING = {"business_connection_id": "connection-1", "message_id": 123, "date": 1764846000,
            "chat": {"id": 42, "type": "private"}, "from": {"id": 42, "first_name": "Олена"},
            "text": "Доброго дня!"}


class TelegramMailTests(unittest.TestCase):
    def test_incoming_private_message_is_mailed_only_to_the_configured_address(self):
        message = relay.email_for({"business_message": INCOMING}, ACCOUNT, 100, ADDRESS)
        self.assertEqual(message["To"].addresses[0].addr_spec, ADDRESS)
        self.assertEqual(str(message["Subject"]), "Telegram — Олена")
        self.assertIn("Доброго дня!", message.get_content())
        repeated = relay.email_for({"business_message": INCOMING}, ACCOUNT, 100, ADDRESS)
        self.assertEqual(message["Message-ID"], repeated["Message-ID"])

    def test_other_accounts_and_ineligible_messages_are_ignored(self):
        for update, connection, owner in [
            (INCOMING, ACCOUNT, 101),
            (INCOMING, {**ACCOUNT, "is_enabled": False}, 100),
            ({**INCOMING, "from": {"id": 100}}, ACCOUNT, 100),
            ({**INCOMING, "chat": {"id": 42, "type": "group"}}, ACCOUNT, 100),
            ({**INCOMING, "has_protected_content": True}, ACCOUNT, 100),
        ]:
            with self.subTest(update=update, owner=owner):
                self.assertIsNone(relay.email_for({"business_message": update}, connection, owner, ADDRESS))
