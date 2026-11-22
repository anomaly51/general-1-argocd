"""Relay new private messages from one connected Telegram account to its Gmail."""

import base64
from datetime import datetime
from email.message import EmailMessage
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


POLL_TIMEOUT = 35
KYIV = ZoneInfo("Europe/Kyiv")


def post(url, data, content_type="application/json", token=None, timeout=20):
    headers = {"Content-Type": content_type}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=data, headers=headers, method="POST")
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def post_json(url, data, **options):
    return post(url, json.dumps(data).encode(), **options)


class GmailSender:
    def __init__(self, address, credentials):
        self.address = address
        self.credentials = credentials
        self.token = ""
        self.expires = 0

    def send(self, message):
        if time.monotonic() >= self.expires:
            account = json.loads(self.credentials.read_text())
            form = {k: account[k] for k in ("client_id", "client_secret", "refresh_token")}
            form["grant_type"] = "refresh_token"
            reply = post("https://oauth2.googleapis.com/token", urlencode(form).encode(),
                         "application/x-www-form-urlencoded")
            self.token = reply["access_token"]
            self.expires = time.monotonic() + max(0, int(reply["expires_in"]) - 90)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        post_json("https://gmail.googleapis.com/gmail/v1/users/me/messages/send", {"raw": raw}, token=self.token)


def email_for(update, connection, owner_id, address):
    message = update["business_message"]
    owner = connection["user"]
    if not connection.get("is_enabled") or owner.get("id") != owner_id:
        return None
    sender = message.get("from", {})
    chat = message.get("chat", {})
    if (chat.get("type") != "private" or sender.get("id") in {None, owner["id"]}
            or sender["id"] != chat.get("id") or sender.get("is_bot") or message.get("has_protected_content")):
        return None

    name = " ".join(" ".join(str(sender.get(field, "")) for field in ("first_name", "last_name")).split()) or "Telegram"
    sent = datetime.fromtimestamp(message["date"], tz=KYIV)
    text = message.get("text") or message.get("caption") or "Вложение или другое сообщение в Telegram."
    receipt = f"{connection['id']}:{chat['id']}:{message['message_id']}"
    message_id = hashlib.sha256(receipt.encode()).hexdigest()
    mail = EmailMessage()
    mail["From"] = address
    mail["To"] = address
    mail["Subject"] = f"Telegram — {name}"
    mail["Date"] = sent
    mail["Message-ID"] = f"<telegram-{message_id}@{address.split('@', 1)[1]}>"
    mail.set_content(f"{name}\n{sent:%d.%m.%Y %H:%M} · Київ\n\n{text}")
    return mail


def consume(bot, sender, owner_id, cursor):
    offset = int(cursor.read_text()) if cursor.exists() else 0
    delay = 1
    while True:
        try:
            updates = bot("getUpdates", {"offset": offset, "timeout": POLL_TIMEOUT,
                                        "allowed_updates": ["business_connection", "business_message"]}, timeout=POLL_TIMEOUT + 10)
            for update in updates:
                if connection := update.get("business_connection"):
                    if connection["user"]["id"] == owner_id:
                        print("Telegram mail: account connected." if connection["is_enabled"]
                              else "Telegram mail: account disconnected.", flush=True)
                if message := update.get("business_message"):
                    if connection_id := message.get("business_connection_id"):
                        connection = bot("getBusinessConnection", {"business_connection_id": connection_id})
                        if mail := email_for(update, connection, owner_id, sender.address):
                            sender.send(mail)
                            print("Telegram mail: delivered an incoming message.", flush=True)
                next_offset = update["update_id"] + 1
                temporary = cursor.with_suffix(".tmp")
                temporary.write_text(str(next_offset))
                temporary.replace(cursor)
                offset = next_offset
            delay = 1
        except Exception as exc:
            status = getattr(exc, "code", None)
            print(f"Telegram mail: retry in {delay}s ({type(exc).__name__}, HTTP {status or '-'})", flush=True)
            time.sleep(delay)
            delay = min(60, delay * 2)


def main():
    token = os.environ["TELEGRAM_MAIL_BOT_TOKEN"]
    owner_id = int(os.environ["TELEGRAM_MAIL_OWNER_ID"])
    address = os.environ["TELEGRAM_MAIL_ADDRESS"].lower()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token) or owner_id <= 0:
        raise SystemExit("Telegram mail: invalid bot configuration.")
    if not re.fullmatch(r"[a-z0-9._+\-]+@gmail\.com", address):
        raise SystemExit("Telegram mail: invalid Gmail address.")

    def bot(method, payload, **options):
        reply = post_json(f"https://api.telegram.org/bot{token}/{method}", payload, **options)
        if not reply["ok"]:
            raise RuntimeError("Telegram refused the request.")
        return reply["result"]

    sender = GmailSender(address, Path("/opt/telegram-mail-credentials/gmail.json"))
    print("Telegram mail: polling for new messages.", flush=True)
    consume(bot, sender, owner_id, Path("/state/telegram-mail.offset"))


if __name__ == "__main__":
    main()
