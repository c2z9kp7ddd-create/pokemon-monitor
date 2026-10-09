#!/usr/bin/env python3
"""Watchdog for the Pokemon stock monitor. Runs every 10 minutes on the Mac (launchd com.pokemon-watchdog).

Checks both copies, fixes what it can, and sends ONE Telegram message per problem when it starts
and one when it clears. Silent while everything is fine.

    python3 watchdog.py            # normal run
    python3 watchdog.py --dry-run  # print findings, change nothing, send nothing
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import monitor  # noqa: E402  (reuses its config, Telegram sender and HTTP helper)

STATE = HERE / "state.json"
LOG = HERE / "monitor.log"
WD_STATE = HERE / "watchdog_state.json"
PLIST = Path.home() / "Library/LaunchAgents/com.pokemon-monitor.plist"
GH = "/opt/homebrew/bin/gh"
DRY = "--dry-run" in sys.argv


def sh(*cmd, timeout=60):
    r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def online():
    try:
        monitor.get_json("https://api.takealot.com/rest/v-1-12-0/searches/products?qsearch=pokemon", timeout=15, tries=1)
        return True
    except Exception:
        return False


def mins_since(iso):
    try:
        return (dt.datetime.now() - dt.datetime.fromisoformat(iso)).total_seconds() / 60
    except Exception:
        return 1e9


def check_mac(fix):
    """Problems with the Mac copy as {id: text}. Fixes: load / kick the launchd job."""
    problems = {}
    code, out = sh("launchctl", "list")
    if "com.pokemon-monitor" not in out:
        problems["mac_job"] = "The Mac monitor job was not loaded."
        if fix and PLIST.exists():
            sh("launchctl", "load", str(PLIST))
            problems["mac_job"] += " Loaded it again."
        return problems
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    age = mins_since(state.get("last_run", ""))
    if age > 12:
        problems["mac_stale"] = f"No full scan on the Mac for {age:.0f} minutes."
        if fix:
            sh("launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.pokemon-monitor")
            problems["mac_stale"] += " Restarted it."
    stores = state.get("stores", {})
    mine = set(monitor.CONFIG.get("local_only_stores", []))
    bad = [n for n, s in stores.items() if n in mine and not s.get("ok")
           and "404" not in (s.get("error") or "")]  # 404: shop closed or page gone, not a monitor fault
    if mine and len(bad) > len(mine) / 2:
        problems["mac_stores"] = f"{len(bad)} of {len(mine)} Mac shops failing in the last scan: {', '.join(sorted(bad)[:6])}."
    # the every-minute checks: there should be several quick scans in the last 10 minutes
    if LOG.exists() and age <= 12:
        tail = LOG.read_text(errors="ignore")[-60000:]
        beats = re.findall(r"^(?:quick scan|fast30) (\d\d:\d\d):", tail, re.M)
        cutoff = (dt.datetime.now() - dt.timedelta(minutes=10)).strftime("%H:%M")
        recent = [b for b in beats if b >= cutoff]
        if len(recent) < 5:
            problems["mac_beat"] = f"Only {len(recent)} every-minute checks on the Mac in the last 10 minutes (expected about 10)."
        # a real crash: a traceback through monitor.py with no checks after it (ignore Python's
        # harmless "I/O operation on closed file" noise from connections abandoned at shutdown)
        tb = tail.rfind("Traceback")
        if tb != -1:
            block = tail[tb:tb + 3000]
            end = block.find("\n", block.find("Error:")) if "Error:" in block else len(block)
            if ('monitor.py", line' in block[:end] and "closed file" not in block[:end]
                    and len(re.findall(r"^(?:quick scan|fast30) ", tail[tb:], re.M)) < 2):
                problems["mac_crash"] = "The Mac monitor crashed: " + block[:end].strip().splitlines()[-1][:150]
    return problems


def check_cloud(fix):
    """Problems with the GitHub copy. Fix: start a run if the chain has stopped."""
    problems = {}
    if not os.path.exists(GH):
        return problems
    code, out = sh(GH, "run", "list", "--workflow", "monitor.yml", "--limit", "6",
                   "--json", "status,conclusion,createdAt")
    if code != 0:
        if "403" in out or "resolve" in out.lower():
            return problems  # GitHub unreachable from this network: can't judge the cloud from here
        problems["cloud_api"] = "Could not read the cloud runs: " + out.strip()[:120]
        return problems
    runs = json.loads(out or "[]")
    if not runs:
        problems["cloud_none"] = "The cloud has no runs."
        return problems
    last = max(dt.datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) for r in runs)
    idle = (dt.datetime.now(dt.timezone.utc) - last).total_seconds() / 60
    active = any(r["status"] != "completed" for r in runs)
    if idle > 15 and not active:
        problems["cloud_stalled"] = f"The cloud has not started a run for {idle:.0f} minutes."
        if fix:
            c, o = sh(GH, "workflow", "run", "monitor.yml", "--ref", "main")
            problems["cloud_stalled"] += " Started it again." if c == 0 else f" Restart failed: {o.strip()[:80]}"
    done = [r for r in runs if r["status"] == "completed"][:3]
    if len(done) == 3 and all(r["conclusion"] not in ("success", "cancelled") for r in done):
        problems["cloud_failing"] = "The last 3 cloud runs failed."
    return problems


def check_telegram():
    """The alert channel itself: the saved bot token must still be accepted."""
    try:
        sec = json.loads((HERE / "secrets.json").read_text())
        token = sec["telegram"]["token"]
        monitor.get_json(f"https://api.telegram.org/bot{token}/getMe", timeout=15, tries=2)
        return {}
    except Exception as e:
        if "401" in str(e) or "404" in str(e):
            return {"telegram": "Telegram rejected the bot token, so no alerts can be sent. Run setup_phone.py."}
        return {}


def main():
    wd = json.loads(WD_STATE.read_text()) if WD_STATE.exists() else {"open": {}}
    if not online():
        print(f"{dt.datetime.now():%H:%M} watchdog: no internet, skipped")
        return
    problems = {}
    problems.update(check_mac(fix=not DRY))
    problems.update(check_cloud(fix=not DRY))
    problems.update(check_telegram())
    now = time.time()
    opened = {k: v for k, v in problems.items() if k not in wd["open"]}
    cleared = [k for k in wd["open"] if k not in problems]
    print(f"{dt.datetime.now():%H:%M} watchdog: {len(problems)} problem(s): {problems or 'all fine'}")
    if DRY:
        return
    # a problem must be seen on two runs in a row before it is reported, so a blip after
    # waking from sleep or a fix that worked straight away stays quiet
    pending = wd.setdefault("pending", {})
    report = {k: v for k, v in opened.items() if k in pending}
    for k in list(pending):
        if k not in problems:
            del pending[k]
    for k, v in opened.items():
        pending.setdefault(k, now)
    if report:
        monitor.notify("Pokemon monitor problem", "\n".join(report.values()))
        for k, v in report.items():
            wd["open"][k] = {"since": now, "text": v}
            pending.pop(k, None)
    if cleared:
        texts = [wd["open"][k]["text"] for k in cleared]
        monitor.notify("Pokemon monitor fixed", "Working again: " + " ".join(texts)[:600])
        for k in cleared:
            del wd["open"][k]
    WD_STATE.write_text(json.dumps(wd, indent=1))


if __name__ == "__main__":
    main()
