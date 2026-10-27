#!/usr/bin/env python3
"""Watchdog for the Pokemon stock monitor. Runs in the cloud, not on the Mac (Gideon, 2026-10-27):

    python3 watchdog.py --cloud    # GitHub Actions, every ~10 min (telegram-check.yml): the cloud run chain,
                                   # the cloud's shops (moves blocked ones to the Mac), the Telegram token
    python3 watchdog.py --server   # Oracle server, every 10 min (systemd pokemon-watchdog.timer): the server's
                                   # monitor service and shops, pulls new code, and checks the cloud chain
                                   # from outside GitHub
    python3 watchdog.py            # Mac role (launchd job unloaded since 2026-10-27; kept for manual runs)
    add --dry-run to print findings, change nothing, send nothing

Each role fixes what it can and sends ONE Telegram message per problem when it starts and one when it
clears. Silent while everything is fine.
"""
import datetime as dt
import json
import os
import re
import shutil
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
GH = "/opt/homebrew/bin/gh" if os.path.exists("/opt/homebrew/bin/gh") else (shutil.which("gh") or "")
DRY = "--dry-run" in sys.argv
ROLE = "cloud" if "--cloud" in sys.argv else "server" if "--server" in sys.argv else "Mac"
REPO = "c2z9kp7ddd-create/pokemon-monitor"


def sh(*cmd, timeout=60):
    try:
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:  # slow network: gh run view --log hung and crashed the whole watchdog
        return 124, f"timed out after {timeout} s"
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def online():
    if ROLE == "cloud":
        return True  # a GitHub runner is online by definition
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
    if not GH:
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
    done = [r for r in runs if r["status"] == "completed"][:8]
    if len(done) == 3 and all(r["conclusion"] not in ("success", "cancelled") for r in done):
        problems["cloud_failing"] = "The last 3 cloud runs failed."
    return problems


def check_server(fix):
    """Problems with the Oracle server copy (run there). Fixes: restart the systemd service."""
    problems = {}
    code, out = sh("systemctl", "is-active", "pokemon-monitor")
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    age = mins_since(state.get("last_run", ""))
    if out.strip() != "active" or age > 12:
        problems["server_stale"] = ("The server monitor was not running." if out.strip() != "active"
                                    else f"No full scan on the server for {age:.0f} minutes.")
        if fix:
            c, _ = sh("sudo", "systemctl", "restart", "pokemon-monitor")
            problems["server_stale"] += " Restarted it." if c == 0 else " Restart failed."
        return problems
    code, out = sh("journalctl", "-u", "pokemon-monitor", "--since", "10 min ago", "--no-pager", "-o", "cat")
    beats = re.findall(r"^(?:quick scan|fast30|watch) \d\d:\d\d", out, re.M)
    if code == 0 and len(beats) < 5:
        problems["server_beat"] = f"Only {len(beats)} every-minute checks on the server in the last 10 minutes."
        if fix:
            sh("sudo", "systemctl", "restart", "pokemon-monitor")
            problems["server_beat"] += " Restarted it."
    return problems


def update_server_code(fix):
    """The server runs whatever GitHub has: pull, and restart the monitor when the code changed."""
    if not fix:
        return {}
    _, before = sh("git", "rev-parse", "HEAD")
    c, out = sh("git", "pull", "-q", "--ff-only", timeout=90)
    _, after = sh("git", "rev-parse", "HEAD")
    if c != 0:
        return {"server_pull": "The server could not update its code from GitHub: " + out.strip()[:100]}
    if before != after:
        print(f"updated code {before.strip()[:7]} -> {after.strip()[:7]}, restarting the monitor")
        sh("sudo", "systemctl", "restart", "pokemon-monitor")
    return {}


def check_cloud_public():
    """The cloud run chain seen from outside GitHub (public API, read only). If GitHub Actions stops,
    the cloud watchdog stops with it, so the server is the one that can tell."""
    try:
        d = monitor.get_json(f"https://api.github.com/repos/{REPO}/actions/workflows/monitor.yml/runs?per_page=5",
                             timeout=20, tries=2)
    except Exception:
        return {}  # GitHub API unreachable: can't judge
    runs = d.get("workflow_runs", [])
    if not runs:
        return {"cloud_none": "The cloud monitor has no runs."}
    last = max(dt.datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) for r in runs)
    idle = (dt.datetime.now(dt.timezone.utc) - last).total_seconds() / 60
    if idle > 20 and not any(r["status"] != "completed" for r in runs):
        return {"cloud_stalled": f"The cloud monitor has not started a run for {idle:.0f} minutes. "
                                 "Start it from GitHub (Actions, pokemon-monitor, Run workflow)."}
    return {}


CLOSED_WORDS = ("back soon", "be back", "closed", "maintenance", "opening soon", "password")
FIXABLE_CLOUD = ("429", "403")       # the cloud is rate-limited or blocked: the Mac can cover the shop
BUDGET = ("budget", "unfinished")    # catalogue too big to scan in time


def shop_closed(url):
    """True when the shop's home page says it is closed / coming back soon."""
    base = re.match(r"https?://[^/]+", url).group(0)
    try:
        r = subprocess.run(["curl", "-s", "-L", "--max-time", "20", "-A", "Mozilla/5.0", base],
                           capture_output=True, text=True, timeout=30)
        title = " ".join(re.findall(r"<title>([^<]*)", r.stdout, re.I)).lower()
        return any(w in title for w in CLOSED_WORDS)
    except Exception:
        return False


def pokemon_collection(store):
    """For a Shopify shop whose catalogue is too big: its /collections/pokemon URL if that holds
    at least as many Pokemon card products as the full scan found last time, else None."""
    base = re.match(r"https?://[^/]+", store["url"]).group(0)
    for handle in ("pokemon", "pokemon-tcg", "pokemon-sealed", "pokemon-trading-card-game"):
        try:
            ps = monitor.get_json(f"{base}/collections/{handle}/products.json?limit=250", tries=1).get("products", [])
        except Exception:
            continue
        n = sum(monitor.is_tcg(p["title"], f'{p.get("vendor", "")} {p.get("product_type", "")}') for p in ps)
        if n >= 20:
            return f"{base}/collections/{handle}"
    return None


def cloud_store_results():
    """{store: 'ok' | error text} from the last 8 completed cloud runs (newest wins)."""
    if not GH:
        return None
    # the cloud searches each Shopify shop about every 20 min (5 shops per ~5-min run), so look back 8 runs
    code, out = sh(GH, "run", "list", "--workflow", "monitor.yml", "--limit", "10", "--json", "databaseId,status")
    if code != 0:
        return None
    ids = [r["databaseId"] for r in json.loads(out or "[]") if r["status"] == "completed"][:8]
    seen = {}
    for rid in ids:
        c, log = sh(GH, "run", "view", str(rid), "--log", timeout=120)
        if c != 0:
            continue
        for line in log.splitlines():
            line = re.sub(r"^.*?Z ", "", line)
            m = re.match(r"^(.+?)\s{2,}\d+ TCG products$", line)
            if m:
                seen.setdefault(m.group(1).strip(), "ok")
            m = re.match(r"^\[(.+?)\] FAILED: (.*)$", line)
            if m:
                seen.setdefault(m.group(1), m.group(2)[:120])
            if "unfinished:" in line:
                for name in re.findall(r"'([^']+)'", line):
                    seen.setdefault(name, "scan time budget used up")
    return seen


def check_stores(fix, wd):
    """Every shop in config.json must be scanned successfully by its owner (Mac or cloud).
    Returns ({problem_id: text}, [config changes made])."""
    cfg = json.loads((HERE / "config.json").read_text())
    mac_only = set(cfg.get("local_only_stores", []))
    server = set(cfg.get("server_stores", [])) & mac_only
    if cfg.get("local_owner") == "server":
        server = set(mac_only)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    mac = state.get("stores", {})  # this machine's own scan results (Mac or server)
    cloud = cloud_store_results() if ROLE in ("cloud", "Mac") else None
    fails = wd.setdefault("store_fails", {})
    problems, changes, rows = {}, [], []
    for st in cfg["stores"]:
        name = st["name"]
        owner = "server" if name in server else "Mac" if name in mac_only else "cloud"
        if owner != ROLE and not (ROLE == "Mac" and owner == "cloud"):
            rows.append(f"{name}: {owner} (checked by the {owner} watchdog)" if owner != "Mac"
                        else f"{name}: Mac (not checked: the Mac's own watchdog is off)")
            continue
        if owner in ("Mac", "server"):
            x = mac.get(name)
            if x is None:
                err = "never scanned"
            elif not x.get("ok"):
                err = x.get("error") or "failed"
            elif mins_since(x.get("at", "")) > 15:
                err = f"last scanned {mins_since(x['at']):.0f} min ago"
            else:
                err = None
        else:
            if cloud is None:
                rows.append(f"{name}: cloud (GitHub unreachable, not checked)")
                continue
            err = None if cloud.get(name) == "ok" else (cloud.get(name) or "not in the last 3 cloud runs")
        if not err:
            fails.pop(name, None)
            rows.append(f"{name}: {owner} ok")
            continue
        if name in cfg.get("known_blocked", []):
            rows.append(f"{name}: {owner} blocked by the shop (known, can't be fixed): {err[:40]}")
            continue
        fails[name] = fails.get(name, 0) + 1
        rows.append(f"{name}: {owner} NOT OK ({err}) x{fails[name]}")
        # one bad scan is usually a blip: act on the second in a row. Shops proven reachable from the
        # cloud (reach test) get an hour, so a short throttle doesn't send them back to the Mac.
        if fails[name] < (6 if name in cfg.get("cloud_verified", []) else 2):
            continue
        fixed = ""
        if fix:
            if owner == "cloud" and any(c in err for c in FIXABLE_CLOUD) and "Rocket Grunt" not in name:
                cfg["local_only_stores"] = sorted(mac_only | {name})
                mac_only.add(name)
                changes.append(f"moved {name} to the Mac (cloud got {err[:40]})")
                fixed = " Moved it to the Mac, which can reach it."
            elif any(b in err for b in BUDGET) and st["type"] == "shopify" and "/collections/" not in st["url"]:
                url = pokemon_collection(st)
                if url:
                    st["url"] = url
                    changes.append(f"{name} now scans {url}")
                    fixed = " Switched it to its Pokemon collection so the scan finishes."
            elif "404" in err or "401" in err:
                if shop_closed(st["url"]):
                    fixed = " The shop's site says it is closed for now; it will be scanned again when it reopens."
            elif owner == "Mac" and ("never" in err or "min ago" in err):
                sh("launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.pokemon-monitor")
                fixed = " Restarted the Mac scan."
            elif owner == "server" and "min ago" in err:
                sh("sudo", "systemctl", "restart", "pokemon-monitor")
                fixed = " Restarted the server scan."
        problems[f"store:{name}"] = f"{name} ({owner}): {err[:90]}.{fixed}"
    if changes and fix:
        (HERE / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        if ROLE == "cloud":  # the Mac picks the change up with its next git pull (sync.sh)
            sh("git", "config", "user.name", "github-actions[bot]")
            sh("git", "config", "user.email", "41898283+github-actions[bot]@users.noreply.github.com")
            sh("git", "pull", "-q", "--rebase")
        sh("git", "add", "config.json")
        sh("git", "commit", "-q", "-m", "Watchdog: " + "; ".join(changes)
           + "\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
        sh("git", "push", "-q")
    print("coverage:\n  " + "\n  ".join(rows))
    return problems, changes


def check_telegram():
    """The alert channel itself: the saved bot token must still be accepted."""
    try:
        token = os.environ.get("TELEGRAM_TOKEN") or json.loads((HERE / "secrets.json").read_text())["telegram"]["token"]
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
    if ROLE == "Mac":
        problems.update(check_mac(fix=not DRY))
    if ROLE == "server":
        problems.update(update_server_code(fix=not DRY))
        problems.update(check_server(fix=not DRY))
        problems.update(check_cloud_public())
    else:
        problems.update(check_cloud(fix=not DRY))
    problems.update(check_telegram())
    store_problems, _ = check_stores(not DRY, wd)
    problems.update(store_problems)
    now = time.time()
    opened = {k: v for k, v in problems.items() if k not in wd["open"]}
    cleared = [k for k in wd["open"] if k not in problems]
    print(f"{dt.datetime.now():%H:%M} watchdog ({ROLE}): {len(problems)} problem(s): {problems or 'all fine'}")
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
        shops = [v for k, v in report.items() if k.startswith("store:")]
        other = [v for k, v in report.items() if not k.startswith("store:")]
        text = "\n".join(other)
        if shops:
            text += ("\n\n" if other else "") + "Shops not being monitored:\n" + "\n".join(shops)
        monitor.notify(f"Pokemon monitor problem ({ROLE} watchdog)", text, channel="status")
        for k, v in report.items():
            wd["open"][k] = {"since": now, "text": v}
            pending.pop(k, None)
    if cleared:
        texts = [wd["open"][k]["text"] for k in cleared]
        monitor.notify("Pokemon monitor fixed", "Working again: " + " ".join(texts)[:600], channel="status")
        for k in cleared:
            del wd["open"][k]
    WD_STATE.write_text(json.dumps(wd, indent=1))


if __name__ == "__main__":
    main()
