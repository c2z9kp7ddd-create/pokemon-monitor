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

def is_tcg(title):
    t = norm(title)
    return (has_any(t, ["pokemon"]) and has_any(t, CONFIG["tcg_words"])
            and not has_any(t, CONFIG["exclude_words"])
            and not SINGLE_CARD.search(t))

# "Cosmog 013/025", "SWSH123", "Mimikyu 075 ... Promo Cards": individual cards, not sealed product
SINGLE_CARD = re.compile(r"\b\d{1,3}/\d{1,3}\b|\b(swsh|svp|smp|xyp)\s?\d+|\b\d{3}\b.*promo cards?\b", re.I)

def is_hot(title):
    return has_any(norm(title), CONFIG["hot_words"])

# ---------------------------------------------------------------- fetching

def get_json(url, data=None, timeout=25):
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA, "Accept": "application/json",
        **({"Content-Type": "application/json"} if data else {})})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def item(store, pid, title, url, price, in_stock):
    return {"key": f"{store}|{pid}", "store": store, "title": norm(title), "url": url,
            "price": round(float(price), 2) if price not in (None, "") else None,
            "in_stock": bool(in_stock)}

def scan_shopify(store, base, max_pages=40):
    out = []
    for page in range(1, max_pages + 1):
        d = get_json(f"{base}/products.json?limit=250&page={page}")
        prods = d.get("products", [])
        for p in prods:
            if not is_tcg(p["title"]):
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

def notify(title, message, priority=3, click=None):
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
        text = f"<b>{html.escape(title)}</b>\n{html.escape(message)}"
        if click:
            text += f'\n\n<a href="{html.escape(click)}">Open product</a>'
        sends.append(("telegram", f"https://api.telegram.org/bot{tg['token']}/sendMessage",
                      {"chat_id": tg["chat_id"], "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": False}))
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
    return (f"{d['title']}\nBuy: R{d['price']:,.0f} at {d['store']}\n"
            f"Resale estimate: R{d['value']:,.0f} (US sales R{d['us_zar']:,.0f}, "
            f"SA median R{d['sa_median']:,.0f} across {d['sa_n']} stores)\n"
            f"Est. profit R{d['profit']:,.0f} ({d['roi']:.0%}) after fees and postage")

def send_deals(deals, state):
    """Alert once per deal; again only if the price drops 5%+ or it sold out and came back."""
    sent = state.setdefault("alerted", {})
    live = {d["key"] for d in deals if d["deal"]}
    for d in deals:
        if not d["deal"]:
            continue
        prev = sent.get(d["key"])
        if prev and prev["active"] and d["price"] > prev["price"] * 0.95:
            continue
        notify(f"Profit R{d['profit']:,.0f}: {d['store']}", deal_msg(d), priority=5, click=d["url"])
        sent[d["key"]] = {"price": d["price"], "active": True, "at": d.get("at")}
    for k, v in sent.items():
        if k not in live:
            v["active"] = False

# ---------------------------------------------------------------- dashboard

def write_dashboard(state, deals):
    drows = "".join(
        f'<tr class="{"hot" if d["deal"] else ""}"><td><a href="{html.escape(d["url"])}">{html.escape(d["title"])}</a>'
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

def check_health(state, results):
    """One message if most stores have failed for about an hour, one when it recovers,
    so silence on Telegram always means 'no deals', never 'monitor broken'."""
    bad = len(results) < len(CONFIG["stores"]) / 2
    state["bad_runs"] = state.get("bad_runs", 0) + 1 if bad else 0
    if state["bad_runs"] == 6:
        notify("Pokemon monitor problem",
               f"Only {len(results)} of {len(CONFIG['stores'])} stores answered for the last 6 scans. "
               "Deal alerts may be missed until this clears.")
        state["warned"] = True
    elif not bad and state.get("warned"):
        notify("Pokemon monitor back to normal", f"{len(results)} of {len(CONFIG['stores'])} stores answering again.")
        state["warned"] = False

# ---------------------------------------------------------------- main

def main():
    args = set(sys.argv[1:])
    if "--test" in args:
        notify("Pokemon monitor connected", "Test alert. You will only get messages when a box looks profitable.", 5)
        print("test sent")
        return

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"items": {}, "stores": {}}
    now = dt.datetime.now().isoformat(timespec="seconds")

    results = {}
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(SCANNERS[s["type"]], s["name"], s["url"]): s["name"] for s in CONFIG["stores"]}
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

    events = []
    for name, found in results.items():
        for it in found:
            old = state["items"].get(it["key"])
            if old is None:
                it["first_seen"] = now
                events.append(("NEW", it))
            else:
                it["first_seen"] = old["first_seen"]
                if it["in_stock"] and not old["in_stock"]:
                    events.append(("BACK IN STOCK", it))
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
    STATE_FILE.write_text(json.dumps(state, indent=1))
    write_dashboard(state, deals)

if __name__ == "__main__":
    main()
