#!/usr/bin/env python3
"""South African Pokemon TCG stock monitor.

Scans SA retailers for Pokemon TCG sealed product, remembers what it has seen,
and pushes a phone notification (via ntfy.sh) when a new product is listed or
a watched product comes back in stock. Standard library only.

    python3 monitor.py            # normal run (first run seeds silently)
    python3 monitor.py --test     # send a test notification and exit
    python3 monitor.py --dry-run  # scan and print, no notifications, no state saved
"""
import concurrent.futures as cf
import datetime as dt
import html
import json
import os
import re
import threading
import time
import urllib.error
import ssl
import sys
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

import valuation

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text())
STATE_FILE = HERE / "state.json"
DASHBOARD = HERE / "dashboard.html"
try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # python.org builds ship without CA certs; fall back to macOS bundle
    SSL_CTX = ssl.create_default_context(
        cafile="/etc/ssl/cert.pem" if os.path.exists("/etc/ssl/cert.pem") else None)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128 Safari/537.36")

# ---------------------------------------------------------------- matching

def norm(s):
    s = html.unescape(s or "")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip()

def has_any(text, words):
    t = " " + text.lower() + " "
    return any(w.lower() in t for w in words)

def is_tcg(title, context=""):
    """context: store metadata (Shopify vendor/product type) that can say 'Pokemon' when the title doesn't."""
    t = norm(title)
    return (has_any(t + " " + norm(context), ["pokemon"]) and has_any(t, CONFIG["tcg_words"])
            and not has_any(t, CONFIG["exclude_words"])
            and not SINGLE_CARD.search(t))

# "Cosmog 013/025", "SWSH123", "Mimikyu 075 ... Promo Cards": individual cards, not sealed product
SINGLE_CARD = re.compile(r"\b\d{1,3}/\d{1,3}\b|\b(swsh|svp|smp|xyp)\s?\d+|\b\d{3}\b.*promo cards?\b", re.I)

THIRTIETH = re.compile(r"\b30\s?th\b|\b30 years\b|\b30-year", re.I)

def is_30th(title):
    return bool(THIRTIETH.search(norm(title)))

def msg_30th(it, deals_by_key):
    """Short release message: cost, plus resale and profit when the listing could be valued."""
    d = deals_by_key.get(it["key"])
    line = f"Cost R{it['price']:,.0f}" if it.get("price") else "Price not shown"
    if d:
        line += f" | Resell ~R{d['value']:,.0f} | Profit ~R{d['profit']:,.0f} ({d['roi']:.0%})"
    return f"{it['title']}\n{line}"

def send_30th(events, deals, state):
    """Every 30th Anniversary listing that becomes buyable gets its own message, any price."""
    by_key = {d["key"]: d for d in deals}
    sent = []
    for kind, it in events:
        if kind in ("NEW", "BACK IN STOCK") and it["in_stock"] and is_30th(it["title"]) and mine(it["store"]):
            notify(f"30TH ANNIVERSARY IN STOCK: {it['store']}", msg_30th(it, by_key), priority=5, click=it["url"])
            sent.append(it["key"])
    if sent:
        state["last_msg"] = time.time()
    return set(sent)

def current(i):
    """Still listed (seen in the last day) and still passes today's product filter."""
    seen = i.get("last_seen", "")
    fresh = seen >= (dt.datetime.now() - dt.timedelta(days=1)).isoformat(timespec="seconds")
    return fresh and is_tcg(i["title"])

def snapshot_30th(state, deals):
    """Once, after this feature first runs: what 30th Anniversary stock is buyable right now."""
    if state.get("snapshot_30th_sent") or not IS_CLOUD:
        return
    by_key = {d["key"]: d for d in deals}
    live = [i for i in state["items"].values()
            if i["in_stock"] and is_30th(i["title"]) and mine(i["store"]) and current(i)]
    live.sort(key=lambda i: (-(by_key.get(i["key"], {}).get("profit") or -1e9), i["price"] or 0))
    lines = []
    for i in live[:15]:
        d = by_key.get(i["key"])
        extra = f", profit ~R{d['profit']:,.0f}" if d else ""
        lines.append(f'{html.escape(i["store"])} | R{i["price"]:,.0f}{extra}\n<a href="{html.escape(i["url"])}">{html.escape(i["title"])}</a>')
    if len(live) > 15:
        lines.append(f"...and {len(live) - 15} more")
    notify(f"30th Anniversary: {len(live)} listings buyable now",
           "\n".join(f"{i['store']}: {i['title']}" for i in live[:15]) or "None in stock right now.",
           tg_html="\n\n".join(lines) or "None in stock right now. You'll get a message the moment one is.")
    state["snapshot_30th_sent"] = True
    state["last_msg"] = time.time()

def is_hot(title):
    return has_any(norm(title), CONFIG["hot_words"])

# ---------------------------------------------------------------- fetching

def get_json(url, data=None, timeout=25, tries=4):
    """Fetch JSON; on 429/5xx wait (honouring Retry-After) and try again."""
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, headers={
            "User-Agent": UA, "Accept": "application/json",
            **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                raise
            wait = e.headers.get("Retry-After")
            time.sleep(min(float(wait), 30) if wait and wait.replace(".", "").isdigit() else 5 * (attempt + 1))

# Cloud runs (GitHub Actions) share IPs that some stores block; those stores are scanned from the Mac instead
IS_CLOUD = bool(os.environ.get("GITHUB_ACTIONS"))
SHOPIFY_GATE = threading.Semaphore(1)  # Shopify rate-limits per IP across all its stores: one at a time

def item(store, pid, title, url, price, in_stock):
    return {"key": f"{store}|{pid}", "store": store, "title": norm(title), "url": url,
            "price": round(float(price), 2) if price not in (None, "") else None,
            "in_stock": bool(in_stock)}

def scan_shopify(store, base, max_pages=40):
    with SHOPIFY_GATE:
        return _scan_shopify(store, base, max_pages)

def _scan_shopify(store, base, max_pages):
    out = []
    for page in range(1, max_pages + 1):
        if page > 1:
            time.sleep(1)
        d = get_json(f"{base}/products.json?limit=250&page={page}")
        prods = d.get("products", [])
        for p in prods:
            if not is_tcg(p["title"], f'{p.get("vendor", "")} {p.get("product_type", "")}'):
                continue
            vs = p.get("variants") or [{}]
            out.append(item(store, p["id"], p["title"], f"{base}/products/{p['handle']}",
                            vs[0].get("price"), any(v.get("available") for v in vs)))
        if len(prods) < 250:
            break
    return out

def scan_woo(store, base, max_pages=30):
    out = []
    for page in range(1, max_pages + 1):
        prods = get_json(f"{base}/wp-json/wc/store/v1/products?search=pokemon&per_page=100&page={page}")
        for p in prods:
            if not is_tcg(p["name"]):
                continue
            pr = p.get("prices") or {}
            price = int(pr["price"]) / 10 ** pr.get("currency_minor_unit", 2) if pr.get("price") else None
            out.append(item(store, p["id"], p["name"], p["permalink"], price,
                            p.get("is_in_stock") or p.get("is_on_backorder")))
        if len(prods) < 100:
            break
    return out

def scan_takealot(store, _base, max_pages=40):
    out, after = [], ""
    for _ in range(max_pages):
        q = urllib.parse.urlencode({"qsearch": "pokemon", **({"after": after} if after else {})})
        d = get_json(f"https://api.takealot.com/rest/v-1-12-0/searches/products?{q}")
        sec = d["sections"]["products"]
        for r in sec.get("results", []):
            pv = r["product_views"]
            core = pv["core"]
            if not is_tcg(core["title"]):
                continue
            prices = (pv.get("buybox_summary") or {}).get("prices") or [None]
            out.append(item(store, core["id"], core["title"],
                            f"https://www.takealot.com/{core['slug']}/PLID{core['id']}",
                            prices[0], (pv.get("buybox_summary") or {}).get("is_add_to_cart_available")))
        after = (sec.get("paging") or {}).get("next_is_after")
        if not after:
            break
    return out

def scan_magento(store, base, max_pages=10):
    out = []
    for page in range(1, max_pages + 1):
        q = ('{products(search:"pokemon",pageSize:100,currentPage:%d){total_count items{'
             'sku name url_key url_suffix stock_status '
             'price_range{minimum_price{final_price{value}}}}}}' % page)
        d = get_json(f"{base}/graphql?" + urllib.parse.urlencode({"query": q}))
        prods = d["data"]["products"]["items"]
        for p in prods:
            if not is_tcg(p["name"]):
                continue
            out.append(item(store, p["sku"], p["name"],
                            f"{base}/{p['url_key']}{p.get('url_suffix') or ''}",
                            p["price_range"]["minimum_price"]["final_price"]["value"],
                            p["stock_status"] == "IN_STOCK"))
        if len(prods) < 100:
            break
    return out

SCANNERS = {"shopify": scan_shopify, "woo": scan_woo,
            "takealot": scan_takealot, "magento": scan_magento}

# ---------------------------------------------------------------- notify

SECRETS_FILE = HERE / "secrets.json"  # telegram / discord credentials, written by setup_phone.py

def notify(title, message, priority=3, click=None, tg_html=None):
    """Send to every channel that is set up: Telegram, Discord, and ntfy if enabled."""
    sec = json.loads(SECRETS_FILE.read_text()) if SECRETS_FILE.exists() else {}
    if os.environ.get("TELEGRAM_TOKEN"):  # cloud run: credentials come from GitHub secrets
        sec["telegram"] = {"token": os.environ["TELEGRAM_TOKEN"], "chat_id": os.environ.get("TELEGRAM_CHAT_ID")}
    sends = []
    tg = sec.get("telegram")
    if tg and not tg.get("chat_id"):  # token saved but chat not known yet: look for a /start
        try:
            ups = get_json(f"https://api.telegram.org/bot{tg['token']}/getUpdates").get("result", [])
            chats = [(u.get("message") or u.get("my_chat_member") or {}).get("chat", {}).get("id") for u in ups]
            tg["chat_id"] = next((c for c in reversed(chats) if c), None)
            if tg["chat_id"]:
                SECRETS_FILE.write_text(json.dumps(sec, indent=2))
        except Exception as e:
            print("telegram chat lookup failed:", e, file=sys.stderr)
    if tg and tg.get("chat_id"):
        text = f"<b>{html.escape(title)}</b>\n" + (tg_html or html.escape(message))
        if click:
            text += f'\n\n<a href="{html.escape(click)}">Open product</a>'
        sends.append(("telegram", f"https://api.telegram.org/bot{tg['token']}/sendMessage",
                      {"chat_id": tg["chat_id"], "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": bool(tg_html)}))
    if sec.get("discord_webhook"):
        content = f"**{title}**\n{message}" + (f"\n{click}" if click else "")
        sends.append(("discord", sec["discord_webhook"], {"content": content[:1990]}))
    if CONFIG.get("ntfy_enabled", True) and CONFIG.get("ntfy_topic"):
        body = {"topic": CONFIG["ntfy_topic"], "title": title, "message": message, "priority": priority}
        if click:
            body["click"] = click
        sends.append(("ntfy", CONFIG.get("ntfy_server", "https://ntfy.sh"), body))
    for name, url, body in sends:
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
                "User-Agent": UA, "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20, context=SSL_CTX).read()
        except Exception as e:  # never let a failed push kill the run
            print(f"notify via {name} failed:", e, file=sys.stderr)

def fmt(it):
    price = f"R{it['price']:,.2f}" if it["price"] is not None else "price n/a"
    stock = "in stock / pre-order" if it["in_stock"] else "out of stock"
    return f"{it['title']}\n{it['store']} | {price} | {stock}"

def deal_msg(d):
    """Short: cost, resale, profit. Details stay on the dashboard."""
    return (f"{d['title']}\n"
            f"Cost R{d['price']:,.0f} | Resell ~R{d['value']:,.0f} | Profit ~R{d['profit']:,.0f} ({d['roi']:.0%})")

def send_deals(deals, state):
    """Alert once per deal; again only if the price drops 5%+ or it sold out and came back."""
    sent = state.setdefault("alerted", {})
    live = {d["key"] for d in deals if d["deal"]}
    for d in deals:
        # the Mac copy alerts only for stores the cloud can't reach, so nothing arrives twice
        if not d["deal"] or not mine(d["store"]):
            continue
        prev = sent.get(d["key"])
        if prev and prev["active"] and d["price"] > prev["price"] * 0.95:
            continue
        notify(f"BUY NOW: {d['store']}", deal_msg(d), priority=5, click=d["url"])
        state["last_msg"] = time.time()
        sent[d["key"]] = {"price": d["price"], "active": True}
    for k, v in sent.items():
        if k not in live:
            v["active"] = False

# ---------------------------------------------------------------- restock watch

STORE_BY_NAME = {s["name"]: s for s in CONFIG["stores"]}

def update_watchlist(deals, state):
    """Sold-out listings that would be profitable at their listed price. Watched silently;
    the only message is the one sent when a listing becomes buyable."""
    watch = state.setdefault("watch", {})
    fresh = {d["key"]: d for d in deals if d["profitable"] and not d["in_stock"] and mine(d["store"])
             and STORE_BY_NAME.get(d["store"], {}).get("type") in ("shopify", "woo")}
    for k in list(watch):
        if k not in fresh and not (k in state["items"] and is_30th(state["items"][k]["title"])
                                   and not state["items"][k]["in_stock"]):
            del watch[k]
    picked = sorted(fresh.items(), key=lambda kv: -kv[1]["profit"])[:CONFIG["updates"]["max_watch"]]
    by_key = {d["key"]: d for d in deals}
    thirtieth = [i for i in state["items"].values()
                 if not i["in_stock"] and is_30th(i["title"]) and mine(i["store"]) and i["key"] not in fresh
                 and current(i)
                 and STORE_BY_NAME.get(i["store"], {}).get("type") in ("shopify", "woo")]
    for k, d in picked:
        watch[k] = {f: d[f] for f in ("store", "title", "url", "price", "value", "profit", "roi")}
        watch[k]["is30"] = is_30th(d["title"])
    for i in thirtieth[:CONFIG["updates"]["max_watch_30th"]]:
        d = by_key.get(i["key"], {})
        watch[i["key"]] = {"store": i["store"], "title": i["title"], "url": i["url"], "price": i["price"],
                           "value": d.get("value"), "is30": True}

def poll_listing(w):
    """Live stock + price for one watched listing: (in_stock, price) or None if unknown."""
    st = STORE_BY_NAME[w["store"]]
    pid = w.get("pid")
    if st["type"] == "shopify":
        with SHOPIFY_GATE:
            p = get_json(w["url"].split("?")[0] + ".js", tries=2)
        return bool(p.get("available")), (p.get("price") or 0) / 100
    if st["type"] == "woo":
        p = get_json(f"{st['url']}/wp-json/wc/store/v1/products/{pid}", tries=2)
        pr = p.get("prices") or {}
        price = int(pr["price"]) / 10 ** pr.get("currency_minor_unit", 2) if pr.get("price") else None
        return bool(p.get("is_in_stock") or p.get("is_on_backorder")), price
    return None

def watch_loop(state, until):
    """Between full scans, check every watched listing about once a minute until `until` (epoch)."""
    watch = state.get("watch", {})
    if not watch:
        return
    cfg = CONFIG["resale"]
    print(f"watching {len(watch)} sold-out listings (profitable or 30th) until {dt.datetime.fromtimestamp(until):%H:%M}")
    while time.time() < until - 20 and watch:
        t0 = time.time()
        for key, w in list(watch.items()):
            w["pid"] = key.split("|", 1)[1]
            try:
                res = poll_listing(w)
            except Exception as e:
                print("watch poll failed:", w["store"], e, file=sys.stderr)
                continue
            if not res or not res[0]:
                continue
            price = res[1] or w["price"]
            profit = roi = None
            if w.get("value"):
                profit = w["value"] * (1 - cfg["sell_fee_pct"]) - cfg["sell_shipping_rand"] - price - cfg["buy_shipping_rand"]
                roi = profit / (price + cfg["buy_shipping_rand"])
            if w.get("is30"):
                d = {key: dict(w, price=price, profit=round(profit), roi=roi)} if profit is not None else {}
                notify(f"30TH ANNIVERSARY IN STOCK: {w['store']}", msg_30th(dict(w, key=key, price=price), d),
                       priority=5, click=w["url"])
                state["last_msg"] = time.time()
            elif profit is not None and profit >= cfg["min_profit_rand"] and roi >= cfg["min_roi"]:
                notify(f"RESTOCKED, BUY NOW: {w['store']}",
                       deal_msg(dict(w, price=price, profit=round(profit), roi=roi)), priority=5, click=w["url"])
                state.setdefault("alerted", {})[key] = {"price": price, "active": True}
                state["last_msg"] = time.time()
            if key in state.get("items", {}):
                state["items"][key].update(in_stock=True, price=price)
            del watch[key]
        time.sleep(max(5, 60 - (time.time() - t0)))

# ---------------------------------------------------------------- dashboard

def write_dashboard(state, deals):
    drows = "".join(
        f'<tr class="{"hot" if d["deal"] else ""} {"" if d["in_stock"] else "out"}"><td><a href="{html.escape(d["url"])}">{html.escape(d["title"])}</a>'
        f'<div class="s">{html.escape(d["store"])} | <a href="{d["tcg_url"]}">TCGplayer</a></div></td>'
        f'<td class="n">R{d["price"]:,.0f}</td><td class="n">R{d["us_zar"]:,.0f}</td>'
        f'<td class="n">R{d["sa_median"]:,.0f}</td><td class="n">R{d["profit"]:,.0f}</td></tr>'
        for d in deals[:60])
    items = list(state["items"].values())
    items.sort(key=lambda i: i["first_seen"], reverse=True)
    items.sort(key=lambda i: (not is_hot(i["title"]), not i["in_stock"]))
    rows = []
    for i in items:
        price = f"R{i['price']:,.0f}" if i["price"] is not None else "-"
        rows.append(
            f'<tr class="{"hot" if is_hot(i["title"]) else ""} {"in" if i["in_stock"] else "out"}">'
            f'<td><a href="{html.escape(i["url"])}">{html.escape(i["title"])}</a></td>'
            f'<td>{html.escape(i["store"])}</td><td class="n">{price}</td>'
            f'<td>{"In stock" if i["in_stock"] else "Sold out"}</td>'
            f'<td class="n">{i["first_seen"][:10]}</td></tr>')
    stores = "".join(
        f'<li><span class="{"ok" if s["ok"] else "bad"}">{"OK" if s["ok"] else "Failed"}</span> '
        f'{html.escape(name)} ({s["count"]})</li>' for name, s in sorted(state["stores"].items()))
    DASHBOARD.write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Pokemon Resale Monitor</title>
<style>
:root{{--bg:#fafaf8;--fg:#1c1c1a;--mut:#6b6b66;--line:#e2e1dc;--hot:#b3261e;--ok:#2e7d32}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe6;--mut:#9a9992;--line:#2c2c2a;--hot:#ff8a80;--ok:#81c784}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,system-ui,sans-serif}}
main{{max-width:1000px;margin:0 auto;padding:16px}}h1{{font-size:20px;margin:8px 0 2px}}
p.m{{color:var(--mut);margin:0 0 16px}}input{{width:100%;box-sizing:border-box;padding:8px;border:1px solid var(--line);
border-radius:6px;background:transparent;color:var(--fg);font:inherit;margin-bottom:10px}}
table{{width:100%;border-collapse:collapse}}td{{padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}}
h2{{font-size:16px;margin:24px 0 4px}}th{{text-align:left;font-weight:600;padding:6px;border-bottom:1px solid var(--line)}}
th.n{{text-align:right}}.s{{color:var(--mut);font-size:13px}}td.n{{white-space:nowrap;text-align:right}}a{{color:inherit}}tr.hot td:first-child a{{color:var(--hot);font-weight:600}}
tr.out{{opacity:.55}}details{{margin-top:20px;color:var(--mut)}}ul{{padding-left:18px}}.ok{{color:var(--ok)}}.bad{{color:var(--hot)}}
@media (max-width:640px){{.all td:nth-child(4),.all td:nth-child(5),.d td:nth-child(3),.d th:nth-child(3),.d td:nth-child(4),.d th:nth-child(4){{display:none}}}}
</style></head><body><main>
<h1>Pokemon Resale Monitor</h1>
<p class="m">Last scan {state["last_run"][:16].replace("T", " ")} | {len(items)} products across {len(state["stores"])} stores. </p>
<h2>Resale check</h2>
<p class="m">In-stock listings valued by both sources, best first. Red = meets your alert rule. Profit is after selling fees and postage both ways.</p>
<table class="d"><tr><th>Listing</th><th class="n">Buy</th><th class="n">US sales</th><th class="n">SA median</th><th class="n">Profit</th></tr>{drows}</table>
<h2>All tracked listings</h2>
<input id="q" placeholder="Filter, e.g. 30th, etb, takealot">
<table id="t" class="all">{''.join(rows)}</table>
<details><summary>Store status</summary><ul>{stores}</ul></details>
</main><script>
q.oninput=()=>{{const v=q.value.toLowerCase();for(const r of t.rows)r.style.display=r.textContent.toLowerCase().includes(v)?'':'none'}}
</script></body></html>""")

def active_stores():
    local_only = set(CONFIG.get("local_only_stores", []))
    return [s for s in CONFIG["stores"] if not (IS_CLOUD and s["name"] in local_only)]

def mine(store):
    """Cloud reports every store it scans; the Mac only reports the stores the cloud can't reach."""
    return IS_CLOUD or store in set(CONFIG.get("local_only_stores", []))

def send_updates(events, state):
    """One Telegram digest per scan listing new products, restocks and price drops."""
    cfg = CONFIG["updates"]
    wanted = {"NEW": cfg["new_listings"], "BACK IN STOCK": cfg["restocks"], "PRICE DROP": True}
    events = [(k, it) for k, it in events if wanted[k] and it["in_stock"] and mine(it["store"])]
    if not events:
        return
    events.sort(key=lambda e: (not is_hot(e[1]["title"]), e[0]))
    counts = {k: sum(1 for kk, _ in events if kk == k) for k in wanted}
    head = ", ".join(f"{n} {k.lower()}" for k, n in counts.items() if n)
    lines = []
    for kind, it in events[:20]:
        price = f"R{it['price']:,.0f}" if it["price"] is not None else "price n/a"
        if kind == "PRICE DROP":
            price = f"R{it['was']:,.0f} to {price}"
        tag = "LIMITED " if is_hot(it["title"]) else ""
        lines.append(f"<b>{tag}{kind}</b> | {html.escape(it['store'])} | {price}\n"
                     f'<a href="{html.escape(it["url"])}">{html.escape(it["title"])}</a>')
    if len(events) > 20:
        lines.append(f"...and {len(events) - 20} more")
    plain = "\n".join(f"{k}: {it['title']} ({it['store']})" for k, it in events[:20])
    notify(f"Pokemon update: {head}", plain, priority=3, tg_html="\n\n".join(lines))
    state["last_msg"] = time.time()

def heartbeat(state, results, deals):
    """If nothing has been sent for a while, send a short 'still watching' check-in (cloud only)."""
    hours = CONFIG["updates"]["heartbeat_hours"]
    if not IS_CLOUD or not hours or time.time() - state.get("last_msg", 0) < hours * 3600:
        return
    best = [d for d in deals if d["in_stock"] and not d["deal"]][:3]
    near = "\n".join(f"- {d['title']} at {d['store']}: R{d['price']:,.0f}, est. {'+' if d['profit'] >= 0 else '-'}R{abs(d['profit']):,.0f}"
                     for d in best)
    notify("Pokemon monitor: still watching",
           f"{len(results)} stores scanned just now, {len(state['items'])} products tracked. No profitable boxes yet.\n"
           + (f"Closest to profitable:\n{near}" if near else ""))
    state["last_msg"] = time.time()

def cloud_watchdog():
    """On the Mac: if the cloud chain has stalled (no run in 30 min), start it again."""
    import subprocess
    gh = "/opt/homebrew/bin/gh"
    if IS_CLOUD or not os.path.exists(gh):
        return
    try:  # changes committed while GitHub was unreachable (e.g. a firewalled network) go up once it's back
        ahead = subprocess.run(["git", "rev-list", "--count", "@{u}..HEAD"], cwd=HERE,
                               capture_output=True, text=True, timeout=15).stdout.strip()
        if ahead not in ("", "0"):
            subprocess.run(["git", "push", "-q"], cwd=HERE, capture_output=True, timeout=60)
    except Exception as e:
        print("auto push failed:", e, file=sys.stderr)
    try:
        out = subprocess.run([gh, "run", "list", "--workflow", "monitor.yml", "--limit", "5",
                              "--json", "status,createdAt"], cwd=HERE, capture_output=True, text=True, timeout=30)
        runs = json.loads(out.stdout or "[]")
        active = any(r["status"] != "completed" for r in runs)
        last = max((dt.datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) for r in runs), default=None)
        stale = last is None or dt.datetime.now(dt.timezone.utc) - last > dt.timedelta(minutes=30)
        if stale and not active:
            subprocess.run([gh, "workflow", "run", "monitor.yml", "--ref", "main"], cwd=HERE, timeout=30)
            print("cloud watchdog: restarted cloud monitor")
    except Exception as e:
        print("cloud watchdog failed:", e, file=sys.stderr)

def check_health(state, results):
    """One message if most stores have failed for about an hour, one when it recovers,
    so silence on Telegram always means 'no deals', never 'monitor broken'."""
    n = len(active_stores())
    bad = len(results) < n / 2
    state["bad_runs"] = state.get("bad_runs", 0) + 1 if bad else 0
    if state["bad_runs"] == 6:
        notify("Pokemon monitor problem",
               f"Only {len(results)} of {n} stores answered for the last 6 scans. "
               "Deal alerts may be missed until this clears.")
        state["warned"] = True
    elif not bad and state.get("warned"):
        notify("Pokemon monitor back to normal", f"{len(results)} of {n} stores answering again.")
        state["warned"] = False

# ---------------------------------------------------------------- main

def main():
    args = set(sys.argv[1:])
    if "--dry-run" not in args:
        cloud_watchdog()
    if "--test" in args:
        notify("Pokemon monitor connected", "Test alert. You will only get messages when a box looks profitable.", 5)
        print("test sent")
        return

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"items": {}, "stores": {}}
    now = dt.datetime.now().isoformat(timespec="seconds")

    # a store's first successful scan only records what it has; announcing all of it would be spam
    known = {n for n, st in state["stores"].items() if st.get("ok") or st.get("count")}
    results = {}
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(SCANNERS[s["type"]], s["name"], s["url"]): s["name"] for s in active_stores()}
        for f in cf.as_completed(futs):
            name = futs[f]
            try:
                results[name] = f.result()
                state["stores"][name] = {"ok": True, "count": len(results[name]), "error": None, "at": now}
            except Exception as e:
                prev = state["stores"].get(name, {})
                state["stores"][name] = {"ok": False, "count": prev.get("count", 0),
                                         "error": str(e)[:200], "at": now}
                print(f"[{name}] FAILED: {e}", file=sys.stderr)

    check_health(state, results)

    seeding = not state["items"]
    events = []
    for name, found in results.items():
        for it in found:
            old = state["items"].get(it["key"])
            if old is None:
                it["first_seen"] = now
                if name in known:
                    events.append(("NEW", it))
            else:
                it["first_seen"] = old["first_seen"]
                if it["in_stock"] and not old["in_stock"]:
                    events.append(("BACK IN STOCK", it))
                elif (it["in_stock"] and it["price"] and old.get("price")
                      and it["price"] <= old["price"] * (1 - CONFIG["updates"]["price_drop_pct"] / 100)):
                    it["was"] = old["price"]
                    events.append(("PRICE DROP", it))
            it["last_seen"] = now
            state["items"][it["key"]] = it

    state["last_run"] = now
    for name, found in results.items():
        print(f"{name:28s} {len(found):4d} TCG products")
    print(f"new / restocked listings: {len(events)}")

    valuer = valuation.Valuer(get_json, state.setdefault("prices", {}), CONFIG["resale"])
    try:
        deals = valuation.find_deals(state["items"], valuer, CONFIG["resale"])
    except Exception as e:
        print("valuation failed:", e, file=sys.stderr)
        deals = []
    print(f"price lookups: {valuer.lookups}  valued: {len(deals)}  deals: {sum(d['deal'] for d in deals)}")
    for d in deals[:10]:
        print(f"  {'DEAL' if d['deal'] else '    '} R{d['profit']:>6,.0f} {d['roi']:>5.0%}  {d['store']}: {d['title']}")

    if "--dry-run" in args:
        return
    send_deals(deals, state)
    if not seeding:
        done = send_30th(events, deals, state)
        snapshot_30th(state, deals)
        send_updates([e for e in events if e[1]["key"] not in done], state)
    update_watchlist(deals, state)
    heartbeat(state, results, deals)
    until = next((float(a.split("=", 1)[1]) for a in args if a.startswith("--watch-until=")), 0)
    if until:
        STATE_FILE.write_text(json.dumps(state, indent=1))  # save once before the long watch
        watch_loop(state, until)
    STATE_FILE.write_text(json.dumps(state, indent=1))
    write_dashboard(state, deals)

if __name__ == "__main__":
    main()
