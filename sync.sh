#!/bin/sh
# Mac only (launchd com.pokemon-sync, every 5 min): keeps this copy in step with GitHub. Not a watchdog:
# the watchdogs run in the cloud and on the server. Pulls their config changes (a shop moved to the Mac)
# and pushes local commits. On a network that blocks GitHub both fail quietly and retry next time.
cd "$(dirname "$0")" || exit 1
git pull -q --rebase --autostash 2>/dev/null || git rebase --abort 2>/dev/null
git push -q 2>/dev/null
exit 0
