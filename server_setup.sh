#!/bin/bash
# Installs the monitor on the Oracle server as an always-on service (run by Claude over SSH).
set -e
sudo apt-get update -qq && sudo apt-get install -y -qq git python3 ca-certificates >/dev/null
if [ -d ~/pokemon-monitor/.git ]; then git -C ~/pokemon-monitor pull -q; else git clone -q https://github.com/c2z9kp7ddd-create/pokemon-monitor.git ~/pokemon-monitor; fi
chmod 600 ~/pokemon-monitor/secrets.json 2>/dev/null || true
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
echo "installed"
