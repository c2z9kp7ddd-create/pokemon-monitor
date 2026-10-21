#!/usr/bin/env python3
"""Share the deal alerts with friends: a Telegram channel (or group) that gets every deal alert too.

1. In Telegram: New Channel (e.g. "Pokemon deals"), make it private or public as you like.
2. Channel info > Administrators > Add Admin > @Kingpokemon_bot (it only needs "Post messages").
3. Post anything in the channel (e.g. "hi") so the bot sees it.
4. Run:  python3 setup_deals_channel.py   then commit and push config.json.
5. Invite friends with the channel's invite link. They just read; no /start needed.
(A group works too: add the bot, send /start@Kingpokemon_bot in it, then run this.)
"""
import json
import sys
import time

import monitor


def main():
    token = json.loads(monitor.SECRETS_FILE.read_text())["telegram"]["token"]
    found = {}
    for _ in range(20):  # up to ~60 s for the post to arrive
        for u in monitor.get_json(f"https://api.telegram.org/bot{token}/getUpdates")["result"]:
            chat = (u.get("channel_post") or u.get("message") or u.get("my_chat_member") or {}).get("chat", {})
            if chat.get("type") in ("channel", "group", "supergroup"):
                found[str(chat["id"])] = (chat.get("title", "?"), chat["type"])
        cfg = json.loads((monitor.HERE / "config.json").read_text())
        skip = {str(cfg.get("telegram_status_chat_id"))}  # never the scan-updates group
        found = {k: v for k, v in found.items() if k not in skip}
        if found:
            break
        time.sleep(3)
    if not found:
        sys.exit("No channel or group found. Make the bot an admin of the channel, post a message there, then rerun.")
    cid, (title, kind) = list(found.items())[-1]
    ids = [str(c) for c in cfg.get("alert_chat_ids", [])]
    if cid not in ids:
        ids.append(cid)
    cfg["alert_chat_ids"] = ids
    (monitor.HERE / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    monitor.CONFIG["alert_chat_ids"] = ids
    monitor.notify("Deal alerts connected", "This channel now gets the same Pokemon deal alerts as Gideon: English 30th "
                   "Anniversary and Delta Reign ETBs / booster boxes at retail or below collector value, and any ETB or "
                   "booster box below collector value. Each was checked on the shop's product page first.",
                   to=[cid])
    print(f"Connected {kind} '{title}'. Commit and push config.json so the cloud sends there too.")


if __name__ == "__main__":
    try:
        main()
    except OSError as e:
        why = ("this network intercepts secure connections, so it blocks Telegram"
               if "CERTIFICATE_VERIFY_FAILED" in str(e) else f"Telegram could not be reached ({e})")
        sys.exit(f"Could not connect: {why}.\nSwitch the Mac to your phone's hotspot or home Wi-Fi and run it again.")
