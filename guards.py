#!/usr/bin/env python3
"""Guards for the alert channel. Two roles, each its own launchd job, and each watches the other:

    python3 guards.py telegram     # every 2 min (com.pokemon-telegram-check): is Telegram working?
    python3 guards.py supervisor   # every 5 min (com.pokemon-guard-supervisor): are the checkers running?
    python3 guards.py telegram --cloud   # GitHub Actions (telegram-check.yml), from a different network
    add --dry-run to print findings and change / send nothing

Telegram check: the bot token is accepted (getMe), the bot can reach Gideon's chat (getChat), and the
monitor's own Telegram sends are not piling up undelivered (state.json outbox, read only).
Supervisor: the Telegram checker, the watchdog and the monitor jobs are loaded and recently active,
and the cloud Telegram check is running and passing. Restarts what it can.

Silent while everything works. A problem must show on two runs in a row; then ONE alert when it starts
and ONE when it clears. Alerts go to Telegram and, because Telegram may be the thing that is down,
also to a Mac desktop notification. In the cloud a failed check fails the job, so GitHub emails the
repo owner (at most once every 6 hours per problem).

Stdlib only, on purpose: a bug in monitor.py must not take the guards down with it.
"""
import datetime as dt
import html
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
HEALTH = HERE / "health"
AGENTS = Path.home() / "Library/LaunchAgents"
GH = "/opt/homebrew/bin/gh" if os.path.exists("/opt/homebrew/bin/gh") else "gh"  # Mac / GitHub runner
DRY = "--dry-run" in sys.argv
CLOUD = "--cloud" in sys.argv

TG_JOB = "com.pokemon-telegram-check"
SUP_JOB = "com.pokemon-guard-supervisor"
# job -> (file whose age shows it is alive, max age in minutes)
JOBS = {
    TG_JOB: (HEALTH / "telegram.json", 8),
    "com.pokemon-watchdog": (HERE / "watchdog.log", 25),
    "com.pokemon-monitor": (HERE / "state.json", 30),  # the watchdog acts after 12; this is the backstop
}
OUTBOX_STUCK_MIN = 15

try:
    import certifi
    CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem" if os.path.exists("/etc/ssl/cert.pem") else None)


def now_s():
    return dt.datetime.now().strftime("%H:%M")


def creds():
    """Token and the chat for guard messages: the status group when it exists, else the alert chat."""
    status = os.environ.get("TELEGRAM_STATUS_CHAT_ID") or json.loads((HERE / "config.json").read_text()).get("telegram_status_chat_id")
    if os.environ.get("TELEGRAM_TOKEN"):
        return os.environ["TELEGRAM_TOKEN"], status or os.environ.get("TELEGRAM_CHAT_ID")
    tg = json.loads((HERE / "secrets.json").read_text()).get("telegram", {})
    return tg.get("token"), status or tg.get("chat_id")


def tg_call(token, method, params=None):
    """(ok, kind, detail). kind: ok | rejected | network. Never puts the token in detail."""
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(params or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        json.loads(urllib.request.urlopen(req, timeout=20, context=CTX).read())
        return True, "ok", ""
    except urllib.error.HTTPError as e:
        raw = e.read()[:2000]
        try:
            desc = json.loads(raw).get("description", "")
        except Exception:  # not Telegram's JSON: a firewall or proxy answered instead
            return False, "network", f"HTTP {e.code} from something in between, not Telegram"
        return False, "rejected", f"HTTP {e.code}: {desc}"[:160]
    except Exception as e:
        msg = str(e).replace(token, "<token>")
        if "CERTIFICATE_VERIFY_FAILED" in msg:
            msg = "the network intercepts HTTPS (certificate not trusted)"
        return False, "network", msg[:160]


def internet_ok():
    for url in ("https://www.google.com/generate_204", "https://www.cloudflare.com/cdn-cgi/trace"):
        try:
            urllib.request.urlopen(url, timeout=10, context=CTX).read(100)
            return True
        except Exception:
            continue
    return False


def mins_old(path):
    try:
        return (time.time() - path.stat().st_mtime) / 60
    except FileNotFoundError:
        return 1e9


def sh(*cmd, timeout=60):
    try:
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 1, str(e)


# ---------------------------------------------------------------- alerting

def mac_notify(title, text):
    if CLOUD or DRY:
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    sh("osascript", "-e", f'display notification "{esc(text[:230])}" with title "{esc(title)}" sound name "Basso"')


def send_tg(title, text):
    token, chat = creds()
    if not (token and chat):
        return False
    ok, _, _ = tg_call(token, "sendMessage", {"chat_id": chat, "parse_mode": "HTML",
                                              "text": f"<b>{html.escape(title)}</b>\n{html.escape(text)}"})
    return ok


def report(role, problems):
    """Open/clear bookkeeping in health/<role>_alerts.json; returns the problems now confirmed open."""
    f = HEALTH / f"{role}_alerts.json"
    st = json.loads(f.read_text()) if f.exists() else {}
    st.setdefault("open", {}); st.setdefault("pending", {}); st.setdefault("unsent", [])
    t = time.time()
    print(f"{now_s()} {role}: {problems or 'all fine'}")
    if DRY:
        return problems
    for k in list(st["pending"]):
        if k not in problems:
            del st["pending"][k]
    new = {}
    for k, v in problems.items():
        if k in st["open"]:
            continue
        if k in st["pending"]:
            new[k] = v
            del st["pending"][k]
        else:
            st["pending"][k] = t  # first sighting: confirm on the next run
    cleared = [k for k in st["open"] if k not in problems]
    msgs = []
    if new:
        msgs.append(("Pokemon alerts problem", "\n".join(new.values())))
        for k, v in new.items():
            st["open"][k] = {"since": t, "text": v}
    if cleared:
        lines = [f"{st['open'][k]['text']} (since {dt.datetime.fromtimestamp(st['open'][k]['since']):%d %b %H:%M})"
                 for k in cleared]
        msgs.append(("Pokemon alerts fixed", "Working again: " + " ".join(lines)))
        for k in cleared:
            del st["open"][k]
    # Telegram messages that could not go out earlier (Telegram was down): send once it is back,
    # before today's news so they arrive in order
    keep = []
    for m in st["unsent"]:
        if t - m["t"] > 86400:
            continue
        if keep or not send_tg(m["title"] + " (delayed)", m["text"]):
            keep.append(m)
    for title, text in msgs:
        mac_notify(title, text)
        if keep or not send_tg(title, text):
            keep.append({"title": title, "text": text, "t": t})
    st["unsent"] = keep
    HEALTH.mkdir(exist_ok=True)
    f.write_text(json.dumps(st, indent=1))
    return st["open"]


def ensure_job(label, fix):
    """Loaded and kicked if needed. Returns a problem text or None."""
    code, out = sh("launchctl", "list", label)
    if code != 0:
        plist = AGENTS / f"{label}.plist"
        if not plist.exists() and (HERE / f"{label}.plist").exists():
            if fix:
                plist.write_bytes((HERE / f"{label}.plist").read_bytes())
        if fix and plist.exists():
            c, o = sh("launchctl", "load", str(plist))
            if sh("launchctl", "list", label)[0] == 0:
                return None  # quietly fixed
            return f"{label} is not loaded and could not be loaded ({o.strip()[:80]})."
        return f"{label} is not loaded."
    return None


def kick(label):
    sh("launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}")


# ---------------------------------------------------------------- roles

def check_telegram():
    problems = {}
    token, chat = creds()
    if not token:
        problems["tg_token"] = "No Telegram bot token is saved, so no alerts can be sent. Run setup_phone.py."
        return problems
    ok, kind, detail = tg_call(token, "getMe")
    if not ok and kind == "network":
        if not internet_ok():
            print(f"{now_s()} telegram: no internet, skipped")
            return None
        problems["tg_unreachable"] = (f"Telegram can't be reached from {'GitHub' if CLOUD else 'the Mac'} "
                                      f"({detail}). Alerts from there are not arriving.")
        return problems
    if not ok:
        problems["tg_token"] = f"Telegram rejected the bot token ({detail}). No alerts can be sent. Run setup_phone.py."
        return problems
    if not chat:
        problems["tg_chat"] = "Telegram has no chat saved for alerts. Send /start to the bot, then run setup_phone.py."
        return problems
    alert_chat = (os.environ.get("TELEGRAM_CHAT_ID") if os.environ.get("TELEGRAM_TOKEN")
                  else json.loads((HERE / "secrets.json").read_text()).get("telegram", {}).get("chat_id"))
    ok, kind, detail = tg_call(token, "getChat", {"chat_id": alert_chat})
    if not ok:
        problems["tg_chat"] = f"The bot can't reach your Telegram chat ({detail}). Unblock the bot or send it /start."
    if chat != alert_chat:  # the status group (watchdog and status messages) is checked too
        ok, kind, detail = tg_call(token, "getChat", {"chat_id": chat})
        if not ok:
            problems["tg_status_chat"] = (f"The bot can't post in the 'Polymarket bot security updates' group ({detail}). "
                                          "Add the bot back to the group.")
    if not CLOUD:
        try:
            box = json.loads((HERE / "state.json").read_text()).get("outbox", [])
        except Exception:
            box = []
        old = [m for m in box if time.time() - m.get("t", time.time()) > OUTBOX_STUCK_MIN * 60]
        if old:
            problems["tg_outbox"] = (f"{len(old)} stock alert(s) from the Mac have not been delivered for over "
                                     f"{OUTBOX_STUCK_MIN} minutes.")
    return problems


def telegram_role():
    problems = check_telegram()
    if problems is None:
        return 0
    if CLOUD:
        problems.update(check_cloud_chain())
        return cloud_verdict(problems)
    # watch the watcher: the supervisor must be alive too
    sup_age = mins_old(HEALTH / "supervisor.json")
    fix = not DRY
    p = ensure_job(SUP_JOB, fix)
    if p:
        problems["sup_job"] = "Guard supervisor: " + p
    elif sup_age > 15:
        if fix:
            kick(SUP_JOB)
        problems["sup_stale"] = f"The guard supervisor has not run for {sup_age:.0f} minutes. Restarted it."
    if not DRY:
        HEALTH.mkdir(exist_ok=True)
        (HEALTH / "telegram.json").write_text(json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"),
                                                          "problems": problems}, indent=1))
    report("telegram", problems)
    return 0


def check_cloud_chain():
    """Cloud only: the monitor's run chain must be alive. If it stopped, start it again and say so on
    Telegram, so the cloud watches itself even when the Mac is closed or off the internet."""
    code, out = sh(GH, "run", "list", "--workflow", "monitor.yml", "--limit", "5",
                   "--json", "status,createdAt", timeout=30)
    if code != 0:
        return {"chain_unknown": f"Could not read the monitor's cloud runs ({out.strip()[:80]})."}
    runs = json.loads(out or "[]")
    if any(r["status"] != "completed" for r in runs):
        return {}
    last = max((dt.datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) for r in runs), default=None)
    age = (dt.datetime.now(dt.timezone.utc) - last).total_seconds() / 60 if last else 1e9
    if age <= 15:
        return {}
    if not DRY:
        sh(GH, "workflow", "run", "monitor.yml", "--ref", "main", timeout=30)
    msg = f"The cloud monitor had not run for {age:.0f} minutes. Restarted it."
    if not DRY:
        send_tg("Pokemon cloud monitor restarted", msg)
    return {"chain_stalled": msg}


def cloud_verdict(problems):
    """Fail the Actions job (GitHub emails the owner) when a problem is new, then every 6 hours."""
    f = HEALTH / "cloud_alerts.json"
    st = json.loads(f.read_text()) if f.exists() else {}
    t = time.time()
    loud = [k for k in problems if t - st.get(k, 0) > 6 * 3600]
    st = {k: (t if k in loud else st[k]) for k in problems}
    HEALTH.mkdir(exist_ok=True)
    f.write_text(json.dumps(st))  # kept between runs by the Actions cache
    for k, v in problems.items():
        print(f"::error title=Telegram check::{v}")
    print(f"{now_s()} telegram (cloud): {problems or 'all fine'}")
    return 1 if loud else 0


def check_cloud_guard(fix):
    if not os.path.exists(GH):
        return {}
    code, out = sh(GH, "run", "list", "--workflow", "telegram-check.yml", "--limit", "5",
                   "--json", "status,conclusion,createdAt")
    if code != 0:
        if "not found" in out.lower() and "workflow" in out.lower():
            return {"cloud_guard_missing": "The cloud Telegram check is not on GitHub yet (commits not pushed)."}
        return {}  # GitHub unreachable from this network: can't judge the cloud from here
    runs = json.loads(out or "[]")
    if not runs:
        return {"cloud_guard_none": "The cloud Telegram check has never run."}
    last = max(dt.datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) for r in runs)
    idle = (dt.datetime.now(dt.timezone.utc) - last).total_seconds() / 60
    problems = {}
    if idle > 45 and not any(r["status"] != "completed" for r in runs):
        if fix:
            sh(GH, "workflow", "run", "telegram-check.yml", "--ref", "main")
        problems["cloud_guard_stalled"] = f"The cloud Telegram check has not run for {idle:.0f} minutes. Started it."
    done = [r for r in runs if r["status"] == "completed"][:2]
    if len(done) == 2 and all(r["conclusion"] == "failure" for r in done):
        problems["cloud_guard_failing"] = "The cloud Telegram check is failing: Telegram is not working from GitHub."
    return problems


def supervisor_role():
    fix = not DRY
    problems = {}
    for label, (beat, max_age) in JOBS.items():
        p = ensure_job(label, fix)
        if p:
            problems[f"job:{label}"] = p
            continue
        age = mins_old(beat)
        if age > max_age:
            if fix:
                kick(label)
            seen = "has never run" if age > 1e8 else f"has shown no activity for {age:.0f} minutes"
            problems[f"stale:{label}"] = f"{label} {seen}. Restarted it."
    problems.update(check_cloud_guard(fix))
    # local commits (e.g. this guard's own workflow) reach GitHub once it is reachable
    c, ahead = sh("git", "rev-list", "--count", "origin/main..main")
    if fix and c == 0 and ahead.strip() not in ("", "0"):
        sh("git", "push", "-q", timeout=60)
    if not DRY:
        HEALTH.mkdir(exist_ok=True)
        (HEALTH / "supervisor.json").write_text(json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"),
                                                            "problems": problems}, indent=1))
    report("supervisor", problems)
    return 0


if __name__ == "__main__":
    role = next((a for a in sys.argv[1:] if not a.startswith("--")), "")
    if role == "telegram":
        sys.exit(telegram_role())
    if role == "supervisor":
        sys.exit(supervisor_role())
    print(__doc__)
    sys.exit(2)
