#!/usr/bin/env python3
"""Connect the separate Telegram group for scanning updates (deal alerts stay in your private chat).

1. In Telegram: New Group, add @Kingpokemon_bot, name it e.g. "Pokemon scan updates".
2. In that group send:  /start@Kingpokemon_bot
3. Run:  python3 setup_status_group.py
It finds the group, saves its id as telegram_status_chat_id in config.json and sends a test message there.
"""
import json
import sys
import time
import urllib.parse

import monitor

def main():
    sec = json.loads(monitor.SECRETS_FILE.read_text())
    token = sec["telegram"]["token"]
    groups = {}
    for _ in range(20):  # up to ~60 s for the /start to arrive
        ups = monitor.get_json(f"https://api.telegram.org/bot{token}/getUpdates")["result"]
        for u in ups:
            chat = (u.get("message") or u.get("my_chat_member") or {}).get("chat", {})
            if chat.get("type") in ("group", "supergroup"):
                groups[chat["id"]] = chat.get("title", "?")
        if groups:
            break
        time.sleep(3)
    if not groups:
        sys.exit("No group found. Add the bot to the group, send /start@Kingpokemon_bot there, then rerun.")
    gid, title = list(groups.items())[-1]
    cfg_path = monitor.HERE / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["telegram_status_chat_id"] = str(gid)
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
    monitor.CONFIG["telegram_status_chat_id"] = str(gid)
    monitor.notify("Scan updates connected", "This group gets the monitor's scanning updates (hourly) and problem "
                   "warnings. Deal alerts stay in your private chat.", silent=True, channel="status")
    print(f"Connected to group '{title}' ({gid}). Commit and push config.json so the cloud uses it too.")

if __name__ == "__main__":
    main()
