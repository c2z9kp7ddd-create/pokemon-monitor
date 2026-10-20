#!/bin/bash
# On the server: can it check the shops that block GitHub? Prints CLOUD-OK / PARTIAL / BLOCKED per shop.
#   ssh -i ~/.ssh/pokemon_oracle ubuntu@<server-ip> 'cd pokemon-monitor && bash server_reach_test.sh'
set -euo pipefail
cd "$(dirname "$0")"
python3 reach_test.py 3
