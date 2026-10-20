#!/bin/bash
# Installs the monitor on an always-on Linux server (Oracle Cloud Always Free, Ubuntu) as a service.
# Run from the Mac:   ssh -i ~/.ssh/pokemon_oracle ubuntu@<server-ip> 'bash -s' < server_setup.sh
#
# It does NOT take over any shops by itself. Afterwards:
#   1. copy the Telegram credentials:  scp -i ~/.ssh/pokemon_oracle secrets.json ubuntu@<ip>:pokemon-monitor/
#   2. run the reach test on the server (see README, "Moving the Mac-only shops to a server"); only if
#      Geek Zone, Marvellous Hobbies, BT Games and Checkers answer from there, set "local_owner": "server"
#      in config.json, push, and unload the Mac job. Until then the server scans but never alerts.
set -euo pipefail
sudo apt-get update -qq && sudo apt-get install -y -qq git python3 ca-certificates >/dev/null
if [ -d ~/pokemon-monitor/.git ]; then git -C ~/pokemon-monitor pull -q; else git clone -q https://github.com/c2z9kp7ddd-create/pokemon-monitor.git ~/pokemon-monitor; fi
cd ~/pokemon-monitor
python3 -m unittest discover -s tests >/dev/null 2>&1 && echo "tests: ok" || { echo "tests FAILED on the server, not installing"; exit 1; }
[ -f secrets.json ] && chmod 600 secrets.json || echo "WARNING: secrets.json missing: copy it from the Mac (step 1) or no alert can be sent"
sudo tee /etc/systemd/system/pokemon-monitor.service >/dev/null <<UNIT
[Unit]
Description=SA Pokemon stock monitor
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=/home/$USER/pokemon-monitor
Environment=MONITOR_ROLE=server
Environment=PYTHONUNBUFFERED=1
ExecStartPre=-/usr/bin/git -C /home/$USER/pokemon-monitor pull -q
ExecStart=/usr/bin/python3 monitor.py --loop
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now pokemon-monitor
sudo systemctl restart pokemon-monitor
echo "installed. It alerts only after config.json says \"local_owner\": \"server\"."
