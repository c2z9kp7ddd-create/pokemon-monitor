"""Outside check of the Oracle server, run by GitHub every ~10 minutes (telegram-check.yml).

Nothing on the server can report the server itself being dead, so GitHub asks it over SSH with a key that can only
run server_probe.sh (status / restart). Results:
  scanning                     -> silent (and "back" once, if a problem was open)
  up but not scanning          -> restart it, tell the status group
  unreachable twice in a row   -> tell Gideon's private chat and the status group (no deals from its shops until
                                  it is back; fix: Oracle console > the instance > Reboot), and fail the job so
                                  GitHub emails too. Open/clear only: one message per problem.
Env: SERVER_PROBE_KEY (private key), TELEGRAM_TOKEN, TELEGRAM_CHAT_ID; host and host key from config.json."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import guards

HERE = Path(__file__).resolve().parent
STATE = HERE / "health" / "server_probe.json"
DOWN_AFTER = 2  # consecutive unreachable checks (~20 min) before telling Gideon


def ask(cmd, cfg):
    """(ok, output). ok False when the server could not be reached."""
    key = os.environ.get("SERVER_PROBE_KEY", "").strip()
    if not key:
        return None, "no SERVER_PROBE_KEY secret"
    with tempfile.TemporaryDirectory() as d:
        k, kh = Path(d) / "k", Path(d) / "known_hosts"
        k.write_text(key + "\n")
        k.chmod(0o600)
        kh.write_text(f"{cfg['host']} {cfg['host_key']}\n")
        try:
            r = subprocess.run(["ssh", "-i", str(k), "-o", f"UserKnownHostsFile={kh}", "-o", "StrictHostKeyChecking=yes",
                                "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", f"{cfg['user']}@{cfg['host']}", cmd],
                               capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return False, "ssh timed out"
    if r.returncode != 0:
        return False, (r.stderr.strip().splitlines() or ["ssh failed"])[-1][:150]
    return True, r.stdout.strip()


def send(text, private=False):
    token, status = guards.creds()
    chats = [status] + ([os.environ.get("TELEGRAM_CHAT_ID")] if private and os.environ.get("TELEGRAM_CHAT_ID") else [])
    for c in chats:
        if c:
            guards.tg_call(token, "sendMessage", {"chat_id": c, "text": text})


def main():
    cfg = json.loads((HERE / "config.json").read_text()).get("server_probe") or {}
    st = json.loads(STATE.read_text()) if STATE.exists() else {"open": None, "fails": 0}
    ok, out = ask("status", cfg)
    if ok is None:
        print(out)
        return 0
    failed = False
    if not ok:
        st["fails"] = st.get("fails", 0) + 1
        print(f"server unreachable ({st['fails']}x): {out}")
        if st["fails"] >= DOWN_AFTER and st.get("open") != "down":
            send(f"Oracle server unreachable for about {st['fails'] * 10} minutes ({out}). No deal alerts from its "
                 "shops until it is back. Fix: Oracle Cloud console, Compute, Instances, pokemon-monitor, Reboot.",
                 private=True)
            st["open"] = "down"
        failed = st["fails"] >= DOWN_AFTER
    else:
        st["fails"] = 0
        parts = out.split()
        active, n, _, age = (parts + ["", "0", "0", "0"])[:4]
        # a monitor that started under 5 minutes ago has not logged its fast-check minutes yet
        scanning = active == "active" and ((n.isdigit() and int(n) >= 1) or (age.isdigit() and int(age) < 300))
        print(f"server: {out} -> {'scanning' if scanning else 'NOT scanning'}")
        if scanning:
            if st.get("open"):
                send(f"Oracle server scanning again ({parts[2] if len(parts) > 2 else '?'} shops).",
                     private=st["open"] == "down")
                st["open"] = None
        else:
            _, r = ask("restart", cfg)
            print(f"restart: {r}")
            if st.get("open") != "stalled":
                send(f"Oracle server was up but not scanning ({out}); GitHub restarted the monitor ({r}).")
            st["open"] = "stalled"
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
