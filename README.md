# SA Pokemon stock monitor

Watches South African (and two UK) shops for English Pokemon TCG sealed product and sends a Telegram
message (bot @Kingpokemon_bot) when a wanted product can be bought at a good price. Python standard
library only. Runs on GitHub Actions around the clock, plus a Mac job for the few shops that refuse
cloud servers.

Labels used below: **[code]** = read from the source code, **[run]** = seen in real run logs (date given),
**[unverified]** = not yet confirmed in a live run.

## 1. What triggers an alert

A listing alerts only when ALL of these hold **[code: matching.py, pricing.py, monitor.consider]**:

1. **Wanted product** (`matching.TARGETS`, first match wins), English only:

   | Target | Matches | Price test |
   |---|---|---|
   | 30TH ANNIVERSARY | "30th", "30 years" (30th Celebration ETBs, tins, bundles, collections...) | retail OR collector |
   | DELTA REIGN | "Delta Reign" ETBs and booster boxes / displays / cases only (no blisters, packs, checklanes, bundles; 2026-10-20) | retail OR collector |
   | BOOSTER BOX | "booster box", "booster display", "36 packs", any set | collector only |
   | ETB | "elite trainer", "ETB", any set | collector only |

   Never alerts at any price: Pitch Black ETBs (`matching.NEVER`, 2026-10-20).
   Never matched: Japanese / Chinese / Korean / other-language product, accessories (protectors,
   sleeves, acrylic cases, empty boxes), lone binders, single cards, plush and merch.
   The old 20th Anniversary targets (XY Evolutions, Generations, CP6) were removed on 2026-10-07; an
   XY Evolutions ETB is now just an ETB (collector test). A test guards against them coming back.
2. **Buyable**, confirmed on the product's own page or product API just before sending (not a search
   index): `in stock` or an open `pre-order`. A failed request, a captcha or a page with no clear stock
   signal is `unknown` and never alerts; a 404 is `listing gone`.
3. **Passes its price test** on the verified price (section 2).
4. **Not a repeat** (section 4).

## 2. Price tests

*Comparable price* = what you would actually pay:
* SA shops: the listed price. Domestic delivery is not added (it varies, is often free over a threshold).
* Overseas shops (Total Cards, Zatu): landed cost = item + international shipping + import duty + 15%
  import VAT, where VAT is charged on (item value x 1.10 + duty), as SARS does. Neither shop publishes
  shipping to SA (only shown at checkout) and the duty rate for trading cards is not confirmed, so both
  are `null` in config. Then only the certain part (item + VAT) is known: a listing is rejected if even
  that fails the test, and held back ("landed cost unconfirmed") if it would only pass on that minimum.
  To enable UK alerts, put the real checkout shipping in the shop's `intl_shipping` (in GBP) and the duty
  in `international.import_duty_pct` (e.g. 0.2). **In practice UK listings do not alert today.**

*Retail test* (30th and Delta Reign only): comparable price <= normal SA shelf price x 1.15
(`retail_tolerance`). Shelf prices come from `retail_refs_rand`: one typical price per product type
(ETB R1,300 so the limit is R1,495, Pokemon Center ETB R2,000, booster box R3,999, booster bundle R699,
mini tin R299 ...). These are estimates per product type, and alerts say so. `retail_exact` can hold
a confirmed price for one specific product (pattern, rand, source); it wins over everything.
Next comes the *official price*: what Nintendo Store SA (`official_retail_stores`) charges for that exact
product, learned from its listings seen in the last 60 days (30th Mini Tin R299, Tech Sticker Collection
R499, 30th ETB R1,299, Chaos Rising ETB R1,299 ...). It uses the same matching rule as the collector test,
so a Mini Tin Display never gets the single tin's price. Alerts name the source. Only then the type estimate.

*Collector test*: comparable price strictly below collector value. Collector value = the lower of
(a) TCGplayer market price (recent completed US sales) converted to rand, capped at 80% of market when
cheaper live listings exist (a junk $1 listing or one undercutting seller no longer throws the value
away; 2026-10-23), and (b) the median price of the same product at 2+ other SA shops (current listings).
When fewer than 2 other SA shops stock it, the US value alone is used, x0.85, and the alert says so.
The TCGplayer match must be exact (same set and product type) and at most 48 h old. On 2026-10-23 this
gave a value for 313 of 443 wanted English listings (was 99). On 2026-10-27, 500 of 603 (was 360): set
numbers ("Scarlet & Violet 10 -"), "(36 packs)", "(max 2 per customer)", "30th Anniversary", "Elite Trainer"
without "Box" and TCGplayer's "(Exclusive)"/"(Retail Version)" no longer block a match. TCGplayer lists each
artwork separately ("Mini Tin [Mew]"): a listing that names its artwork gets that one, a listing that names
none gets the cheapest artwork. A TCGplayer name with no set words left ("Mega Evolution Booster Box")
never matches, as it used to fit any booster box. Combo listings ("ETB + booster box") get the value of one
item at most, so they can only be undervalued.

No price, no reference, stale value: no alert. The rejection reason is counted (section 7).

## 3. Telegram message (real example from a dry run on 2026-10-20, not sent)

```
DELTA REIGN IN STOCK: Galaxy Boxx
Pokemon: Delta Reign Elite Trainer Box
R1,299.95 | in stock
Retail: SA shelf ~R1,300 (typical for this product type, estimate), 0% below
Collector value ~R1,299 (lower of TCGplayer recent sales ~R1,895 and SA median R1,299 across 14 shops, 19 Oct)
Why: at retail: R1,300 vs SA shelf ~R1,300 (estimate for this product type)
Checked 20 Oct 09:20 SAST on the product
Open product -> link
```
Overseas listings also show the original price and, when shipping and duty are configured, the landed
cost broken down. No routine "still running" messages (`heartbeat_hours` 0, `status_messages` false).

### Sharing alerts with friends
The bot does not take subscriptions (typing /start to it does nothing). To share alerts, make a Telegram
channel (or group), add @Kingpokemon_bot as an admin, post one message there, then run
`python3 setup_deals_channel.py` and push `config.json`. Every deal alert then also goes to that channel
(`alert_chat_ids`); friends join with the channel's invite link. Scan updates never go there.

## 4. Repeats and duplicates  **[code, tests/test_alerts.py]**

* Each listing alerts once. It alerts again only when its price is at least 5% below the last alerted
  price, or when it sold out and came back at least 24 hours after its last alert. It does not repeat
  daily while it simply stays in stock, and a price bouncing around a threshold does not re-alert.
* The same product listed twice by one shop, or moved to a new URL, shares a fingerprint: one alert.
* Check-and-record is one locked step, so parallel threads finding the same deal send one message.
* History lives in `state.json` (cloud: the Actions cache, restored every run), is saved atomically
  right after each alert, and survives restarts and deploys. Entries expire after 14 days.
* The Mac and the cloud never alert for the same shop (`mine()`): the cloud owns every shop except
  `local_only_stores`; those belong to the Mac (or a server, see 9).
* First full scan after this version is deployed: listings that were already buyable at a good price
  (the old code never announced them) arrive as ONE summary message, then normal alerts resume.

## 5. Shops  (config.json, counted 2026-10-20)

| | Count | Notes |
|---|---|---|
| Configured | 38 | none disabled |
| Checked by the cloud (GitHub) | 33 | all except the 4 below and Rocket Grunt |
| Checked only by the Mac, when it is on | 4 | Geek Zone, Marvellous Hobbies, BT Games, Checkers (they return 403 to GitHub) |
| Configured but cannot be checked | 1 | Rocket Grunt TCG (403 to every automated request) |
| Not configured: block automated access | - | Game, Makro, Pwned Games, Pokestop, Luckshack (bot checks, captchas) |

**Amazon.co.za** (Mac only: Amazon bot-checks data-centre IPs such as the Oracle server). Its search is
bot-checked and is not worked around; wanted products are found on category, best-seller and
new-release pages, then each wanted product's own page is read in rotation, one page every 20 s
(`rotation_seconds`), so with 8 wanted products each is checked about every 2.7 minutes. Ban test on
2026-10-22: about one page every 6 s got Amazon's bot check within a minute; 15 s ran clean but was not
proven over hours, so 20 s keeps a margin. A bot-check page
counts as a failed check (the rotation backs off), never as sold out. A full pass (known products first,
then the discovery pages) runs at most every 15 min and keeps what it read if it hits the time budget.
Not checked while the Mac is asleep or offline.

Cloud evidence: 21 shops passed the GitHub reach test on 2026-10-17 (6/6 at 1-minute pace, 58/58 at
5 s); on 2026-10-18, 292 of 292 cloud runs in 24 h succeeded and 30 of 30 cloud shops were scanned
**[run]**. Galaxy Boxx (added 18 Oct) appears in cloud runs since then. **Magic The Trading** and
**Otaku Kulture** were added on 20 Oct and have not been through a cloud run yet **[unverified]**; if
they refuse GitHub, the watchdog moves them to the Mac.

Shop types: Shopify 20 (incl. the 2 UK shops), WooCommerce 14, Takealot API, Toys R Us (Magento
GraphQL), Checkers (public product pages from its sitemap), Mithril Vault (own site).

`health/status.md` (and each GitHub run's summary page) lists every shop the copy owns with its last
good check, real check intervals and last error. A shop is "NOT CHECKED" there, and the cloud sends a
status message, after 45 minutes without a successful check.

## 6. How often  **[code; intervals as measured in real runs, Oct 17-20]**

| Check | Target | In practice (cloud) |
|---|---|---|
| 30th listings (search / small 30th collection, in-stock first) | every 5 s per shop | 5 s at most shops; 30 s at the 7 shops read via a 30th collection (Toy Kingdom, Level Up, Aniware, Big Bang, WZRD, Readers Warehouse, Weebforce). A shop that errors backs off, doubling up to 120 s |
| 1-minute check (WooCommerce, Takealot, Toys R Us; newest + wanted searches) | 60 s | ~60-70 s. In the cloud Shopify shops skip it (their 5-second checks cover it) |
| Sold-out wanted listings, product page | 60 s | WooCommerce / Takealot / Checkers; Shopify is not polled from the cloud (throttled), its restocks show in the in-stock-first 5 s checks |
| Full scan of every shop | 5 min | non-Shopify every run; in the cloud each Shopify shop is searched about every 20 min (5 shops per run) |

Rate limits: 429 pauses that shop's scans for 15 minutes (a 429 on the fast check pauses only the fast
check), Shopify requests go one at a time from GitHub, each scan has a 5-minute budget and every
request a timeout, so one slow shop cannot stall the rest.

## 7. Cloud operation and health  **[code]**

* `monitor.yml`: one run at a time (concurrency group), ~5 minutes each: full scan, then 1-minute and
  5-second checks until the next run. Each run queues the next one (GitHub's cron is a backup; it did
  not fire reliably for this repo). State goes through the Actions cache.
* `telegram-check.yml`: every 10 minutes (queues itself) checks the bot token and the chat, fails its
  job when Telegram is broken (GitHub then emails you), and restarts the monitor chain if no monitor
  run started in 15 minutes. The monitor chain in turn starts this check if it is overdue. Neither can
  restart anything while GitHub Actions itself is down; GitHub can also start runs late.
* `tests.yml`: the unit tests on every push.
* Telegram: each send is retried (network errors, 5xx, Telegram's 429 with its retry_after); permanent
  errors are not retried; a message that still fails is queued and resent next run, marked
  "(delayed)", for up to 24 h.
* Config is validated at start; a broken config or missing Telegram secrets in the cloud fails the run
  loudly instead of running silently.
* Mac: launchd jobs `com.pokemon-monitor` (back to back: a ~5-minute cycle, restarted 10 s after each ends), `com.pokemon-watchdog`,
  `com.pokemon-telegram-check`, `com.pokemon-guard-supervisor` (watch each other and the cloud).
* Scanning updates go to a separate Telegram group (`telegram_status_chat_id`, set up with
  `setup_status_group.py`): an hourly silent "Scan update" (shops checked, shops not being checked,
  alerts sent, why listings were turned down, Telegram failures; `updates.status_report_minutes`) and
  problem warnings. Until the group exists, hourly updates are not sent; problem warnings (a shop
  unchecked for 45 min) fall back to the deal chat.

## 8. What gets measured (`health/status.md`, GitHub run summary)

Per day (SAST): new listings discovered, product-page checks (buyable / not), alerts sent, duplicates
suppressed, rejections by reason (`overpriced`, `no_reference`, `no_price`, `landed_unknown`,
`unavailable`, `verify_failed`, `irrelevant`), Telegram sends / retries / failures and queued messages.
Per shop: last good check, measured intervals of each check type, last error. Every rejection of a
wanted listing is logged with its reason. A 14-day history is kept in state.json (`daily`).

## 9. Covering the 4 Mac-only shops with the Mac closed

Why they fail from GitHub: Geek Zone sits behind Cloudflare, Marvellous Hobbies and BT Games behind
their hosts' firewalls, Checkers behind Amazon CloudFront/WAF. All four accept a home internet
connection (the Mac). An Oracle Cloud server is also a data-centre address, so it **may be blocked
too**; only a test from that server can tell. Options:

| Option | Cost | Likely to reach the 4 shops | Effort |
|---|---|---|---|
| Small always-on device at home (Raspberry Pi 4/5 or an old laptop) | ~R1,500 once + electricity | yes (home connection, like the Mac) | plug in, run `server_setup.sh` |
| Oracle Cloud Always Free VM, Johannesburg | R0 (card for sign-up) | unknown until tested | sign-up + `server_setup.sh` |
| Keep the Mac awake and plugged in | R0 | yes | none, but not 24/7 |

Steps (any Linux box; commands run from the Mac / VS Code terminal):
```bash
ssh -i ~/.ssh/pokemon_oracle ubuntu@<server-ip> 'bash -s' < server_setup.sh
scp -i ~/.ssh/pokemon_oracle secrets.json ubuntu@<server-ip>:pokemon-monitor/
ssh -i ~/.ssh/pokemon_oracle ubuntu@<server-ip> 'cd pokemon-monitor && bash server_reach_test.sh'
```
Only if the reach test shows CLOUD-OK for the 4 shops: set `"local_owner": "server"` in config.json,
push, and `launchctl bootout gui/$(id -u)/com.pokemon-monitor` on the Mac. Until then the server
scans but never alerts, so it cannot look operational by accident. **Not done yet: needs your sign-up
(Oracle) or a device.**

## 10. Setup, tests, deploy (Mac + VS Code)

```bash
cd ~/Desktop/pokemon-monitor
python3 -m unittest discover -s tests -v      # 63+ tests, no network, no Telegram
python3 monitor.py --dry-run                  # one real scan, prints decisions, sends nothing, saves nothing
python3 setup_phone.py                        # (re)connect Telegram
git push                                      # deploys: the next cloud run uses the new code
gh workflow run monitor.yml --ref main        # start the cloud chain by hand
gh run list --workflow monitor.yml --limit 5  # is it running?
```
Environment variables (never commit values): `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` (GitHub secrets for
the cloud; `secrets.json` on the Mac, git-ignored), optional `TELEGRAM_STATUS_CHAT_ID`, `MONITOR_ROLE=server`
(set by `server_setup.sh`). See `.env.example`.

Adding a target: one row in `matching.TARGETS` plus genuine and near-miss titles in
`tests/test_matching.py`. Adding a shop: an entry in `config.json` `stores` (`validate_config` checks it).

## 11. Troubleshooting

* No messages for days: normal when nothing qualifies. Check `health/status.md` / the latest GitHub run
  summary: shops "ok", rejections listed, Telegram failures 0.
* GitHub and Telegram unreachable from the Mac: some networks (FortiGate) block both and break HTTPS
  to some shops. The cloud is unaffected; the Mac's queued messages go out once it is back online.
* A cloud shop keeps failing with 403/429: the Mac watchdog moves it to `local_only_stores`.
* Run killed or cancelled: state is saved after every alert and at the end; the next run restores it.

## 12. Known limitations

* Retail references are per product type unless Nintendo Store SA lists the product or it is in `retail_exact`.
* UK shops cannot alert until their shipping and the import duty are confirmed and configured.
* Collector value relies on TCGplayer (US sales) and SA asking prices; no SA completed-sales source.
* Mithril Vault pages have no machine-readable stock flag: their text is read ("Add to cart" / "Sold
  out"), and anything else is treated as unknown.
* Domestic delivery costs are not included in SA prices.
* GitHub Actions is not a server: runs can start late, and nothing in it can restart itself if
  Actions is down. The 4 Mac-only shops are unchecked while the Mac is off.
