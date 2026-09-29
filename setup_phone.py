#!/usr/bin/env python3
"""Connect the Pokemon monitor to Telegram or Discord.

    python3 setup_phone.py
"""
import getpass
import json
import os
import sys
import time
import urllib.request

import monitor

SECRETS = monitor.SECRETS_FILE


def call(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json", "User-Agent": monitor.UA})
    with urllib.request.urlopen(req, timeout=20, context=monitor.SSL_CTX) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def save(sec):
    SECRETS.write_text(json.dumps(sec, indent=2))
    os.chmod(SECRETS, 0o600)


def setup_telegram(sec):
    print("\nTelegram setup")
    print(" 1. In Telegram, open a chat with @BotFather and send: /newbot")
    print(" 2. Pick any name, then a username ending in 'bot'.")
    print(" 3. BotFather replies with a token like 123456:ABC-DEF...\n")
    token = getpass.getpass("Paste the token here (hidden) and press Enter: ").strip()
    try:
        me = call(f"https://api.telegram.org/bot{token}/getMe")["result"]
    except Exception:
        sys.exit("That token did not work. Copy it again from BotFather and rerun.")
    print(f"\nNow open https://t.me/{me['username']} on your phone and tap START.")
    print("Waiting up to 3 minutes...")
    chat_id = None
    for _ in range(60):
        ups = call(f"https://api.telegram.org/bot{token}/getUpdates").get("result", [])
        for u in reversed(ups):
            msg = u.get("message") or u.get("my_chat_member") or {}
            if msg.get("chat", {}).get("id"):
                chat_id = msg["chat"]["id"]
                break
        if chat_id:
            break
        time.sleep(3)
    if not chat_id:
        sys.exit("Did not see you tap START. Rerun and try again.")
    sec["telegram"] = {"token": token, "chat_id": chat_id}
    save(sec)
    print("Connected to Telegram.")


def setup_discord(sec):
    print("\nDiscord setup")
    print(" 1. In Discord, make a server (or use yours) and pick a channel.")
    print(" 2. Channel settings > Integrations > Webhooks > New Webhook > Copy Webhook URL.")
    print(" 3. Turn on notifications for that channel on your phone.\n")
    url = getpass.getpass("Paste the webhook URL here (hidden) and press Enter: ").strip()
    if not url.startswith("https://discord.com/api/webhooks/") and not url.startswith("https://discordapp.com/api/webhooks/"):
        sys.exit("That does not look like a Discord webhook URL.")
    sec["discord_webhook"] = url
    save(sec)
    print("Connected to Discord.")


def main():
    sec = json.loads(SECRETS.read_text()) if SECRETS.exists() else {}
    choice = input("Send alerts to (1) Telegram or (2) Discord? ").strip()
    if choice == "1":
        setup_telegram(sec)
    elif choice == "2":
        setup_discord(sec)
    else:
        sys.exit("Type 1 or 2.")
    monitor.notify("Pokemon monitor connected",
                   "You will get a message here only when an in-stock box looks profitable to resell.")
    print("Test message sent. Check your phone.")


if __name__ == "__main__":
    main()
