# SA Pokemon resale monitor

Runs 24/7 on GitHub Actions (each run queues the next ~10 min later; the cron is a backup).
Scans 27 South African stores and messages Telegram (@Kingpokemon_bot).

Telegram messages: ONLY 20th Anniversary booster boxes / ETBs (XY Evolutions, Generations, Japanese CP6)
and 30th Anniversary / 30th Celebration products. Nothing else.
- 20TH / 30TH ANNIVERSARY IN STOCK: one message per listing the moment it is buyable (new, restock or pre-order),
  any price. Shows cost, plus resale and profit when they can be estimated. Sold-out ones are checked every minute.
- BUY NOW: a wanted listing that should also resell at a profit.
- Pokemon update: 10%+ price drops on wanted listings that are in stock.
- Still watching: daily check-in if nothing else was sent.
- Monitor problem / back to normal: most stores unreachable for about an hour.

How profit is estimated (settings under "resale" in config.json)
- Exact TCGplayer match (same set and product type), English products only
- Resale value = the LOWER of TCGplayer recent sales (in rand) and the median price at 2+ other SA stores
- Minus 12% selling fees, R120 postage out, R100 postage in; alert if profit >= R300 and >= 25%
Estimates, not guarantees.

Stores that block cloud servers ("local_only_stores") are scanned by the Mac (launchd, every 10 min when awake).
The Mac also restarts the cloud chain if it has stalled for 30 minutes.

    python3 monitor.py --dry-run     # scan + valuation, print only
    python3 setup_phone.py           # reconnect Telegram / Discord
    gh workflow run monitor.yml      # start the cloud run manually
    launchctl bootout gui/$(id -u)/com.pokemon-monitor   # stop the Mac copy
