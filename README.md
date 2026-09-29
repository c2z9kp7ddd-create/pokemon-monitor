# SA Pokemon resale monitor

Scans 17 South African stores every 10 minutes. Messages you on Telegram or Discord (run `python3 setup_phone.py` once to connect;
ntfy also still works, set "ntfy_enabled": false in config.json to turn it off)
ONLY when an in-stock box looks profitable to resell. Estimates, not guarantees.

A listing alerts only if all of these hold:
- It is in stock (or open for pre-order) and English (no reliable price data for Japanese/Chinese yet)
- It matches a TCGplayer product exactly (same set, same product type: ETB vs case vs Pokemon Center etc.)
- TCGplayer live listings agree with its recent-sales price (stale prices are ignored)
- At least 2 other SA stores list the same product
- Resale value = the LOWER of US sales price (in rand) and the SA median
- After 12% selling fees, R120 postage out, R100 postage in: profit >= R300 and >= 25%

Tune all of this under "resale" in config.json.

    python3 monitor.py --dry-run   # scan + valuation, print only
    open dashboard.html            # every valued listing, best margin first
    launchctl bootout gui/$(id -u)/com.pokemon-monitor   # stop
