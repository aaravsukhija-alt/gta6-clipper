"""Phone notifications: Discord webhook and/or Telegram bot (whichever secrets are set)."""
import json
import os
import urllib.request


def send(text: str) -> bool:
    sent = False
    hook = os.getenv("DISCORD_WEBHOOK_URL")
    if hook:
        try:
            req = urllib.request.Request(hook, data=json.dumps({"content": text[:1900]}).encode(),
                                         headers={"Content-Type": "application/json", "User-Agent": "gta6-clipper"})
            urllib.request.urlopen(req, timeout=20).read()
            sent = True
        except Exception as e:  # noqa: BLE001 - a failed ping must never stop posting
            print(f"[notify] discord failed: {e}")
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            data = json.dumps({"chat_id": chat, "text": text[:4000], "disable_web_page_preview": False}).encode()
            req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20).read()
            sent = True
        except Exception as e:  # noqa: BLE001
            print(f"[notify] telegram failed: {e}")
    if not sent:
        print(f"[notify] (no Discord/Telegram configured) {text}")
    return sent
