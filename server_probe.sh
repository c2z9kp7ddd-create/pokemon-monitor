#!/bin/bash
# Forced command for the GitHub probe key (~/.ssh/authorized_keys on the Oracle server):
#   command="/usr/local/bin/pokemon_probe.sh",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 ...
# The key can do exactly two things, whatever the caller asks for:
#   status   -> one line: "<active|inactive> <fast-check minutes logged in the last 5 min> <shops in the last line>
#               <seconds since the service started>"
#   restart  -> restart the monitor, only if it is not scanning: inactive, or up for 5+ minutes with no
#               fast-check line in the last 5 (a monitor that has just started is never restarted)
# Installed as a root-owned copy (/usr/local/bin/pokemon_probe.sh), so a git pull cannot change what the key does.
set -u
health() {
  local act lines shops since age
  act=$(systemctl is-active pokemon-monitor 2>/dev/null)
  since=$(systemctl show pokemon-monitor -p ActiveEnterTimestampMonotonic --value 2>/dev/null)
  age=$(( $(cut -d. -f1 /proc/uptime) - ${since:-0} / 1000000 ))
  lines=$(journalctl -u pokemon-monitor --since "-5 min" --no-pager -o cat 2>/dev/null | grep "^fast30 ")
  n=$(printf '%s\n' "$lines" | grep -c "^fast30 ")
  shops=$(printf '%s\n' "$lines" | tail -1 | sed -nE 's/^fast30 [0-9:]+: ([0-9]+) shops.*/\1/p')
  echo "$act $n ${shops:-0} $age"
}
case "${SSH_ORIGINAL_COMMAND:-status}" in
  restart)
    read -r act n _ age <<<"$(health)"
    if [ "$act" != "active" ] || { [ "$n" -eq 0 ] && [ "$age" -ge 300 ]; }; then
      sudo systemctl reset-failed pokemon-monitor
      sudo systemctl restart pokemon-monitor
      echo "restarted"
    else
      echo "scanning, not restarted"
    fi ;;
  *) health ;;
esac
