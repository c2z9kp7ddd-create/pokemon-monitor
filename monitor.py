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

import matching
import pricing
import valuation

import faulthandler, signal
faulthandler.register(signal.SIGUSR1, all_threads=True)  # `kill -USR1 <pid>`: dump every thread's stack to the log
sys.stdout.reconfigure(line_buffering=True)  # keep stdout and stderr in order in monitor.log

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

norm = matching.norm

def has_any(text, words):
    t = " " + text.lower() + " "
    return any(w.lower() in t for w in words)

def is_tcg(title, context=""):
    """context: store metadata (Shopify vendor/product type) that can say 'Pokemon' when the title doesn't."""
    t = norm(title)
    # some shops drop "Pokemon" from titles ("30th Celebration Elite Trainer Box", "Mega Evolution: ...")
    return (has_any(t + " " + norm(context), ["pokemon", "30th celebration", "mega evolution", "delta reign"])
            and has_any(t, CONFIG["tcg_words"])
            and not has_any(t, CONFIG["exclude_words"])
            and not SINGLE_CARD.search(t))

# "Cosmog 013/025", "SWSH123", "Mimikyu 075 ... Promo Cards": individual cards, not sealed product
SINGLE_CARD = re.compile(r"\[mega evolution[^\]]*\]|\b\d{1,3}/\d{1,3}|\bbasic \w+ energy\b|\b(swsh|svp|smp|xyp)\s?\d+|\b\d{3}\b.*promo cards?\b|\b[rgb]/rgb\b"
                         r"|\b(reverse )?holo(foil)?\b(?!.*\b(box|tin|collection box|pack)\b)|^[^-]*\b\d{3} - |\b(common|uncommon|(double |illustration |ultra |secret |hyper )?rare)\s*$", re.I)

THIRTIETH = matching.TARGETS[0][1]  # also used to repair Mithril Vault titles

def anniversary(title):
    """Wanted-product label (matching.TARGETS: 30TH ANNIVERSARY, DELTA REIGN, BOOSTER BOX, ETB), else None."""
    return matching.label(title)

def is_30th(title):
    """Wanted product (English 30th Anniversary, Delta Reign, any English booster box or ETB)."""
    return anniversary(title) is not None

# ---------------------------------------------------------------- stock status
# A failed request, a captcha or an unreadable page is UNKNOWN: never proof of stock, never proof of none.
IN_STOCK, PREORDER, OUT_OF_STOCK, GONE, UNKNOWN = "in stock", "pre-order", "out of stock", "listing gone", "unknown"
BUYABLE = (IN_STOCK, PREORDER)
PREORDER_WORDS = re.compile(r"\bpre-?\s?orders?\b", re.I)
BLOCK_PAGE = re.compile(r"captcha|cf-chl|challenge-platform|access denied|are you a robot", re.I)

# ---------------------------------------------------------------- state, stats
STATE_LOCK = threading.RLock()  # quick scan, listing checks and fast 30th checks run in parallel threads
ALERT_LOCK = STATE_LOCK         # one lock: state is saved while threads record alerts and stats

def sast_now():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=2)))

def stat(state, name, n=1, reason=None):
    """Daily counters (state['stats']): discovered, verified, alerts, duplicates, rejections by reason..."""
    with STATE_LOCK:
        day = sast_now().strftime("%Y-%m-%d")
        st = state.setdefault("stats", {"day": day, "counts": {}, "rejected": {}})
        if st.get("day") != day:  # new day: keep yesterday's numbers in the 14-day history
            hist = state.setdefault("daily", {})
            hist[st["day"]] = {"counts": st["counts"], "rejected": st["rejected"]}
            for d in sorted(hist)[:-14]:
                del hist[d]
            print(f"daily summary {st['day']}: {json.dumps(hist[st['day']], sort_keys=True)}")
            st.clear(); st.update(day=day, counts={}, rejected={})
        if reason:
            st["rejected"][reason] = st["rejected"].get(reason, 0) + n
        else:
            st["counts"][name] = st["counts"].get(name, 0) + n

def record_check(state, store, ok, kind, error=None):
    """Per-shop health: last success, failures and the real interval between successful checks."""
    now = time.time()
    with STATE_LOCK:
        sh = state.setdefault("shop_stats", {}).setdefault(store, {})
        if ok:
            sh["last_ok"] = now
            beats = sh.setdefault(f"beats_{kind}", [])
            beats.append(round(now, 1))
            del beats[:-30]
        else:
            sh["last_fail"], sh["last_error"] = now, str(error)[:160]
            sh["fails"] = sh.get("fails", 0) + 1

def save_state(state):
    """Atomic: a run killed mid-write must not leave a half-written state.json (alert history lives there)."""
    with STATE_LOCK:
        data = json.dumps(dict(state, outbox=state.get("outbox", []) + OUTBOX), indent=1)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(data)
    os.replace(tmp, STATE_FILE)

# ---------------------------------------------------------------- alert policy
# One alert per listing. Another only when the price is at least 5% below the last alerted price, or when
# the listing sold out and came back at least 24 hours after its last alert. The same product listed twice
# by a shop (or moved to a new URL) shares a fingerprint, so it alerts once.
REPEAT_SECONDS = 86400
REPEAT_DROP = 0.05

def _repeat_ok(prev, price, now):
    if not prev:
        return True
    if price and prev.get("price") and price <= prev["price"] * (1 - REPEAT_DROP) + 1e-6:
        return True
    return bool(prev.get("gone")) and now - prev["t"] >= REPEAT_SECONDS

def would_alert(state, key, fp, price, now=None):
    now = time.time() if now is None else now
    with ALERT_LOCK:
        sent = state.get("alerted_wanted", {})
        return all(_repeat_ok(sent.get(k), price, now) for k in (key, fp) if k)

def first_alert(state, key, price, fp=None, now=None):
    """Check and record in one locked step, so two threads finding the same deal send one alert."""
    now = time.time() if now is None else now
    with ALERT_LOCK:
        sent = state.setdefault("alerted_wanted", {})
        if not all(_repeat_ok(sent.get(k), price, now) for k in (key, fp) if k):
            return False
        for k in (key, fp):
            if k:
                sent[k] = {"t": now, "price": price}
        for k in [k for k, v in sent.items() if now - v["t"] > 14 * 86400]:
            del sent[k]
        return True

def note_unavailable(state, it):
    """A listing seen sold out after its alert may alert again once it returns (after 24 h)."""
    if it.get("in_stock"):
        return
    with ALERT_LOCK:
        sent = state.get("alerted_wanted", {})
        for k in (it["key"], matching.fingerprint(it["store"], it["title"])):
            if k in sent:
                sent[k]["gone"] = True

def merge_item(state, it, now, known, events=None):
    """Store a freshly read listing; return the change it represents (NEW, BACK IN STOCK, PRICE DROP or None)."""
    with STATE_LOCK:
        old = state["items"].get(it["key"])
        change = None
        if old is None:
            it["first_seen"] = now
            if it["store"] in known:
                change = "NEW"
        else:
            it["first_seen"] = old.get("first_seen", now)
            if it["in_stock"] and not old.get("in_stock"):
                change = "BACK IN STOCK"
            elif (it["in_stock"] and it.get("price") and old.get("price")
                  and it["price"] <= old["price"] * (1 - REPEAT_DROP) + 1e-6):
                it["was"], change = old["price"], "PRICE DROP"
        it["last_seen"] = now
        state["items"][it["key"]] = dict(old or {}, **it)
        if old is None:
            stat(state, "discovered")
    note_unavailable(state, it)
    if events is not None and change:
        events.append((change, it))
    return change

# ---------------------------------------------------------------- product-page verification

def verify_listing(it):
    """Ask the product's own page or product API (not a search index or cached listing) whether it can be
    bought right now: (status, rand price or None, detail). Never raises."""
    st = STORE_BY_NAME.get(it["store"]) or {}
    kind, pid, url = st.get("type"), it["key"].split("|", 1)[1], it["url"]
    try:
        status, price, detail = _verify(kind, st, pid, url, it)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return GONE, None, f"product page returned {e.code}"
        return UNKNOWN, None, f"check failed: HTTP {e.code}"
    except Exception as e:  # timeouts, DNS, bad JSON, layout changes
        return UNKNOWN, None, f"check failed: {type(e).__name__}: {str(e)[:80]}"
    if price is not None and st.get("currency", "ZAR") != "ZAR":
        price = round(price * zar_per(st["currency"]), 2)
    if status in BUYABLE and PREORDER_WORDS.search(it["title"]):
        status = PREORDER
    return status, price, detail

_LAST_SHOPIFY_VERIFY = [0.0]

def _pace_shopify_verify():
    """Cloud: Shopify throttles GitHub's IPs, and a burst of product-page checks (first run: ~20 in a row next
    to the 5-second checks) got 429s. Space them out; a check that still fails is retried next full scan."""
    gap = CONFIG.get("cloud_shopify_verify_gap_seconds", 3) if IS_GITHUB else 0
    wait = _LAST_SHOPIFY_VERIFY[0] + gap - time.time()
    if wait > 0:
        time.sleep(wait)
    _LAST_SHOPIFY_VERIFY[0] = time.time()

def _verify(kind, st, pid, url, it):
    if kind == "shopify":
        with SHOPIFY_GATE:
            _pace_shopify_verify()
            p = get_json(url.split("?")[0] + ".js", tries=1, timeout=15)
        variants = p.get("variants") or []
        avail = [v for v in variants if v.get("available")]
        if not p.get("available") or (variants and not avail):
            return OUT_OF_STOCK, (p.get("price") or 0) / 100 or None, "product page: sold out"
        cents = min(v.get("price") or 0 for v in avail) if avail else p.get("price") or 0
        if avail:  # the variant the price belongs to, for the one-tap cart link
            it["_variant"] = min(avail, key=lambda v: v.get("price") or 0).get("id")
        return IN_STOCK, cents / 100 or None, "product page: available"
    if kind == "woo":
        p = get_json(f"{st['url']}/wp-json/wc/store/v1/products/{pid}", tries=1, timeout=st.get("timeout", 20))
        pr = p.get("prices") or {}
        price = int(pr["price"]) / 10 ** pr.get("currency_minor_unit", 2) if pr.get("price") else None
        it["_woo_type"] = p.get("type")  # only simple products can go straight into the cart by link
        if not p.get("is_purchasable", True):
            return OUT_OF_STOCK, price, "product: not purchasable"
        if p.get("is_on_backorder"):
            return PREORDER, price, "product: backorder / pre-order"
        if p.get("is_in_stock"):
            return IN_STOCK, price, "product: in stock"
        return OUT_OF_STOCK, price, "product: out of stock"
    if kind == "takealot":
        d = get_json(f"https://api.takealot.com/rest/v-1-12-0/product-details/PLID{pid}?platform=desktop",
                     tries=1, timeout=15)
        sel = next((i for i in (d.get("buybox") or {}).get("items", []) if i.get("is_selected")), None)
        if not sel:
            return OUT_OF_STOCK, None, "product page: no seller offering it"
        if sel.get("is_preorder"):
            return PREORDER, sel.get("price"), "product page: pre-order"
        if sel.get("is_add_to_cart_available"):
            return IN_STOCK, sel.get("price"), "product page: add to cart"
        return OUT_OF_STOCK, sel.get("price"), "product page: not orderable"
    if kind == "checkers":
        p = checkers_product(it["store"], url)
        return (IN_STOCK if p["in_stock"] else OUT_OF_STOCK), p["price"], "product page (national delivery)"
    if kind == "magento":
        q = ('{products(filter:{url_key:{eq:"%s"}}){items{stock_status price_range{minimum_price{final_price{value}}}}}}'
             % url.rsplit("/", 1)[-1].replace('"', ""))
        items = get_json(f"{st['url']}/graphql?" + urllib.parse.urlencode({"query": q}), tries=1, timeout=15)["data"]["products"]["items"]
        if not items:
            return GONE, None, "product no longer listed"
        price = items[0]["price_range"]["minimum_price"]["final_price"]["value"]
        return (IN_STOCK if items[0]["stock_status"] == "IN_STOCK" else OUT_OF_STOCK), price, "product data"
    if kind == "amazon":
        p = amazon_product(it["store"], pid, st["url"].rstrip("/"))
        sold = f", sold by {p['seller']}" if p.get("seller") else ""
        return (IN_STOCK if p["in_stock"] else OUT_OF_STOCK), p["price"], "product page" + sold
    if kind == "eden":  # product page: "<n> In Stock" for the main item
        h = get_html(url)
        qty = re.search(r'lbl_QTYInStock">\s*(-?\d+)\s*In Stock', h)
        pr = re.search(r'lbl_OptionPrice"[^>]*>\s*R\s?([\d,.]+)', h)
        price = float(pr.group(1).replace(",", "")) if pr else it.get("price")
        if not qty:
            return UNKNOWN, None, "product page: no stock count"
        return (IN_STOCK if int(qty.group(1)) > 0 else OUT_OF_STOCK), price, f"product page: {qty.group(1)} in stock"
    if kind == "mithril":
        h = get_html(url)
        if BLOCK_PAGE.search(h):
            return UNKNOWN, None, "page shows a bot check"
        if pid.startswith("pre-"):  # pre-order set page
            return (PREORDER if "Pre-orders open" in h else OUT_OF_STOCK), it.get("price"), "pre-order page"
        if re.search(r"sold out|out of stock", h, re.I):
            return OUT_OF_STOCK, None, "product page: sold out"
        if re.search(r"add to cart|add to bag|buy now|pre-?order now", h, re.I):
            return IN_STOCK, it.get("price"), "product page: add to cart"
        return UNKNOWN, None, "product page: no stock signal"  # missing button is not proof either way
    return UNKNOWN, None, f"no product-level check for {kind} shops"

# ---------------------------------------------------------------- the one alert path

def cart_link(it):
    """One-tap link that puts the item in the shop's cart (Gideon 2026-10-21). Gideon still checks out and
    pays himself; nothing is bought automatically. Shopify: cart permalink /cart/<variant>:1 (opens checkout
    with the item). WooCommerce: ?add-to-cart=<id> (simple products only). Other shops: None."""
    st = STORE_BY_NAME.get(it["store"]) or {}
    kind, pid = st.get("type"), it["key"].split("|", 1)[1]
    try:
        if kind == "shopify":
            v = it.get("_variant")
            if not v:  # the caller verified the listing another way: one product read to find the variant
                p = get_json(it["url"].split("?")[0] + ".js", tries=1, timeout=10)
                avail = [x for x in p.get("variants") or [] if x.get("available")]
                v = min(avail, key=lambda x: x.get("price") or 0).get("id") if avail else None
            if v:
                u = urllib.parse.urlsplit(it["url"])
                return f"{u.scheme}://{u.netloc}/cart/{int(v)}:1"
        elif kind == "amazon" and re.fullmatch(r"B[0-9A-Z]{9}", pid):  # Amazon's own add-to-cart link format
            return f"{st['url'].rstrip('/')}/gp/aws/cart/add.html?ASIN.1={pid}&Quantity.1=1"
        elif kind == "woo" and it.get("_woo_type", "simple") == "simple" and pid.isdigit():
            return f"{st['url'].rstrip('/')}/?add-to-cart={pid}&quantity=1"
    except Exception as e:
        print("cart link failed:", it["store"], e, file=sys.stderr)
    return None

LAST_VALUES = {}  # listing key -> (collector value rand, source text), from the latest valuation

def fx_for(store):
    cur = (store or {}).get("currency", "ZAR")
    return 1.0 if cur == "ZAR" else zar_per(cur)

def reject(state, code, it, source, detail=""):
    stat(state, "rejected", reason=code)
    if code not in ("irrelevant", "duplicate"):
        print(f"reject [{code}] {source}: {it['store']}: {it['title'][:70]} R{it.get('price')} {detail}".rstrip())
    return False

DIGEST = []  # set to a list to collect alerts instead of sending them one by one (first sweep after deploy)

def consider(state, it, source, verified=None):
    """Discovery -> wanted? -> price pre-check -> duplicate? -> product-page check -> price on the verified
    price -> record -> Telegram. verified: (status, price, detail) when the caller just read the product page.
    Returns True when an alert was sent."""
    lab = anniversary(it["title"])
    if not lab or not is_tcg(it["title"]):
        return reject(state, "irrelevant", it, source)
    if not mine(it["store"]):
        return False
    store = STORE_BY_NAME.get(it["store"]) or {}
    value, vsrc = LAST_VALUES.get(it["key"], (None, None))
    fp = matching.fingerprint(it["store"], it["title"])
    if it.get("price"):  # cheap pre-checks before spending a request on the product page
        pre = pricing.decide(lab, norm(it["title"]).lower(), it["price"], store, CONFIG, fx_for(store), value, vsrc)
        if not pre.qualifies:
            return reject(state, pre.code, it, source, pre.reason)
        if not would_alert(state, it["key"], fp, pre.comparable):
            return reject(state, "duplicate", it, source)
    status, price, detail = verified or verify_listing(it)
    stat(state, "verified_buyable" if status in BUYABLE else "verify_not_buyable")
    if status not in BUYABLE:
        return reject(state, "verify_failed" if status == UNKNOWN else "unavailable", it, source, f"({detail})")
    price = price if price is not None else it.get("price")
    dec = pricing.decide(lab, norm(it["title"]).lower(), price, store, CONFIG, fx_for(store), value, vsrc)
    if not dec.qualifies:
        return reject(state, dec.code, it, source, dec.reason + " (on the product page)")
    if not first_alert(state, it["key"], dec.comparable, fp):
        return reject(state, "duplicate", it, source)
    title, plain, rich = alert_message(dict(it, price=price), lab, dec, status, detail)
    if DIGEST and DIGEST[0] is not None:
        DIGEST.append((it, price, lab, dec, status))
    else:
        notify(title, plain, priority=5, click=it["url"], tg_html=rich, cart=cart_link(it))
    stat(state, "alerts_sent")
    with STATE_LOCK:
        state["last_msg"] = time.time()
        state.setdefault("alert_log", []).append({
            "t": round(time.time()), "store": it["store"], "title": it["title"], "url": it["url"], "status": status,
            "price": price, "comparable": dec.comparable, "test": dec.code, "retail": dec.retail,
            "retail_kind": dec.retail_kind, "collector": dec.collector, "collector_source": dec.collector_source,
            "estimated": dec.estimated, "source": source})
        del state["alert_log"][:-200]
    print(f"ALERT {source}: {it['store']}: {it['title'][:70]} {status} R{price} [{dec.code}] {dec.reason}")
    save_state(state)  # a run killed later must not forget this alert and send it again
    return True

def send_digest(found):
    """One message listing every listing that qualified on the first sweep (cheapest first)."""
    if not found:
        return
    found.sort(key=lambda f: f[1] or 0)
    rows = [f"{html.escape(it['store'])} | R{price:,.0f} | {status} | {html.escape(dec.reason)}\n"
            f'<a href="{html.escape(it["url"])}">{html.escape(it["title"][:90])}</a>' for it, price, lab, dec, status in found[:20]]
    if len(found) > 20:
        rows.append(f"...and {len(found) - 20} more (see the status report)")
    notify(f"Buyable now at a good price: {len(found)} listings",
           "\n".join(f"{it['store']}: {it['title']} R{price}" for it, price, *_ in found[:20]),
           priority=5, tg_html="Already in stock when the new alert rules started. Each was checked on its product page. "
                               "From now on you get one message per new deal.\n\n" + "\n\n".join(rows))

def _pct(x):
    return f"{abs(x) * 100:.0f}%"

def alert_message(it, lab, dec, status, detail):
    """(title, plain text, Telegram HTML). Every number shown comes from the decision; estimates say so."""
    head = f"{lab} {'BELOW COLLECTOR VALUE' if dec.code == 'collector' and lab in ('ETB', 'BOOSTER BOX') else status.upper()}: {it['store']}"
    lines = [it["title"]]
    price = f"R{it['price']:,.2f}"
    if it.get("orig"):
        price = f"{it['orig']} (= R{it['price']:,.2f})"
    lines.append(f"{price} | {status}")
    if dec.landed and not dec.landed.parts.get("item") == dec.landed.total:
        parts = dec.landed.parts
        lines.append(f"Landed ~R{dec.landed.total:,.0f}: item R{parts['item']:,.0f} + shipping R{parts.get('shipping', 0):,.0f} "
                     f"+ duty R{parts.get('duty', 0):,.0f} + import VAT R{parts.get('vat', 0):,.0f} (estimate)")
    if dec.retail:
        rel = "below" if dec.saving_vs_retail >= 0 else "above"
        lines.append(f"Retail: SA shelf ~R{dec.retail:,.0f} ({'exact product' if dec.retail_kind == 'exact' else 'typical for this product type, estimate'})"
                     f", {_pct(dec.saving_vs_retail)} {rel}")
    if dec.collector:
        lines.append(f"Collector value ~R{dec.collector:,.0f} ({dec.collector_source}), {_pct(dec.saving_vs_collector)} below"
                     if dec.saving_vs_collector >= 0 else
                     f"Collector value ~R{dec.collector:,.0f} ({dec.collector_source})")
    lines.append(f"Why: {dec.reason}")
    lines.append(f"Checked {sast_now():%d %b %H:%M} SAST on the {detail.split(':')[0]}")
    rich = "\n".join(html.escape(l) for l in lines[1:])
    return head, "\n".join(lines), f"{html.escape(lines[0])}\n{rich}"

def current(i):
    """Still listed (seen in the last day; Takealot 30 days, as sold-out items leave its search)
    and still passes today's product filter."""
    seen = i.get("last_seen", "")
    days = 30 if STORE_BY_NAME.get(i.get("store"), {}).get("type") == "takealot" else 1
    fresh = seen >= (dt.datetime.now() - dt.timedelta(days=days)).isoformat(timespec="seconds")
    return fresh and is_tcg(i["title"])

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
            if e.code == 429 and IS_CLOUD:  # Shopify throttles GitHub's IPs; waiting it out blew the time limit
                raise
            wait = e.headers.get("Retry-After")
            time.sleep(min(float(wait), 30) if wait and wait.replace(".", "").isdigit() else 5 * (attempt + 1))

# Cloud runs (GitHub Actions) share IPs that some stores block; those stores are scanned from the Mac instead
IS_GITHUB = bool(os.environ.get("GITHUB_ACTIONS"))
IS_SERVER = os.environ.get("MONITOR_ROLE") == "server"  # the always-on Oracle server: owns all stores
IS_CLOUD = IS_GITHUB or IS_SERVER                        # reports every store it scans
SCAN_DEADLINE = [float("inf")]  # set per run; scanners stop paging once it passes

class OutOfTime(Exception):
    pass

def check_deadline():
    if time.time() > SCAN_DEADLINE[0]:
        raise OutOfTime("scan time budget used up")

# Shopify rate-limits per IP across all its stores: one at a time in the cloud; the Mac isn't throttled
SHOPIFY_GATE = threading.Semaphore(1 if IS_GITHUB else 3 if IS_SERVER else 8)

def item(store, pid, title, url, price, in_stock):
    return {"key": f"{store}|{pid}", "store": store, "title": norm(title), "url": url,
            "price": round(float(price), 2) if price not in (None, "") else None,
            "in_stock": bool(in_stock)}

def shopify_item(store, base, p):
    """A Shopify product -> item. Price is that of the cheapest variant that can be bought (falling back to the
    cheapest variant), so the price shown always belongs to a variant whose stock it reports."""
    vs = p.get("variants") or [{}]
    avail = [v for v in vs if v.get("available")]
    pick = min(avail or vs, key=lambda v: float(v.get("price") or 0) or float("inf"))
    return item(store, p["id"], p["title"], f"{base}/products/{p['handle']}", pick.get("price"), bool(avail))

def scan_shopify(store, base, max_pages=40):
    if IS_GITHUB:  # Shopify throttles GitHub's shared IPs on whole-catalogue paging; targeted searches pass
        return scan_shopify_light(store, base)
    with SHOPIFY_GATE:
        return _scan_shopify(store, base, max_pages)

LIGHT_TERMS = ("30th", "delta reign", "elite trainer", "booster box")  # one search per wanted target

def scan_shopify_light(store, base):
    """Every wanted listing via Shopify's product search, in-stock first: one small request per term."""
    root = base.split("/collections/")[0]
    out, seen = [], set()
    for term in LIGHT_TERMS:
        check_deadline()
        url = f"{root}/search/suggest.json?" + urllib.parse.urlencode({
            "q": term, "resources[type]": "product", "resources[limit]": 10,
            "resources[options][unavailable_products]": "last"})
        with SHOPIFY_GATE:
            d = get_json(url, tries=1, timeout=15)
        for p in d["resources"]["results"]["products"]:
            if p["id"] in seen or not is_tcg(p["title"], p.get("vendor", "")):
                continue
            seen.add(p["id"])
            out.append(item(store, p["id"], p["title"], f"{root}/products/{p['handle']}",
                            p.get("price"), p.get("available")))
        time.sleep(0.5)
    return out

def _scan_shopify(store, base, max_pages):
    out = []
    for page in range(1, max_pages + 1):
        check_deadline()
        if page > 1:
            time.sleep(1)
        d = get_json(f"{base}/products.json?limit=250&page={page}")
        prods = d.get("products", [])
        for p in prods:
            if not is_tcg(p["title"], f'{p.get("vendor", "")} {p.get("product_type", "")}'):
                continue
            out.append(shopify_item(store, base, p))
        if len(prods) < 250:
            break
    return out

def scan_woo(store, base, max_pages=30):
    out, seen = [], set()
    timeout = STORE_BY_NAME.get(store, {}).get("timeout", 25)  # some shops are just slow
    for term in STORE_BY_NAME.get(store, {}).get("search", ["pokemon"]):
        for page in range(1, max_pages + 1):
            check_deadline()
            prods = get_json(f"{base}/wp-json/wc/store/v1/products?search={urllib.parse.quote(term)}&per_page=100&page={page}",
                             timeout=timeout)
            out += _woo_items(store, [p for p in prods if p["id"] not in seen])
            seen.update(p["id"] for p in prods)
            if len(prods) < 100:
                break
    return out

def _woo_items(store, prods):
    out = []
    for p in prods:
        if not is_tcg(p["name"]):
            continue
        pr = p.get("prices") or {}
        price = int(pr["price"]) / 10 ** pr.get("currency_minor_unit", 2) if pr.get("price") else None
        out.append(item(store, p["id"], p["name"], p["permalink"], price,
                        p.get("is_in_stock") or p.get("is_on_backorder")))
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

_FX = {}

def zar_per(cur):
    """Rand per 1 unit of `cur`, refreshed every 6 hours."""
    if cur == "ZAR":
        return 1.0
    if not _FX or time.time() - _FX.get("_t", 0) > 6 * 3600:
        rates = get_json("https://open.er-api.com/v6/latest/ZAR")["rates"]
        _FX.clear(); _FX.update(rates); _FX["_t"] = time.time()
    return 1 / _FX[cur]

SYMBOL = {"GBP": "£", "USD": "$", "EUR": "€", "JPY": "¥"}

def localize(store, items):
    """International shops: convert prices to rand, keep the original for the message."""
    cur = STORE_BY_NAME.get(store, {}).get("currency", "ZAR")
    if cur == "ZAR":
        return items
    rate = zar_per(cur)
    for it in items:
        if it["price"] is not None:
            it["orig"] = f"{SYMBOL.get(cur, cur + ' ')}{it['price']:,.0f}" if cur == "JPY" else f"{SYMBOL.get(cur, cur + ' ')}{it['price']:,.2f}"
            it["price"] = round(it["price"] * rate, 2)
        it["intl"] = True
    return items

# Checkers (Gideon, 2026-10-09): its search and catalogue need a login, but robots.txt allows crawling and
# each public product page carries price and stock for national one-day delivery. Product URLs come from
# its sitemap (cached 6 h); only wanted Pokemon listings are read, one page at a time.
CHECKERS_CACHE = HERE / ".checkers_urls.json"
CHECKERS_GATE = threading.Semaphore(1)

def get_html(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read().decode("utf-8", "replace")

def checkers_urls(base):
    """Wanted Pokemon product URLs from the Checkers sitemap: [(url, title-from-slug)]."""
    try:
        c = json.loads(CHECKERS_CACHE.read_text())
        if time.time() - c["t"] < 6 * 3600:
            return c["urls"]
    except Exception:
        pass
    urls = []
    for sm in re.findall(r"<loc>([^<]+)", get_html(f"{base}/sitemap.xml")):
        check_deadline()
        urls += [u for u in re.findall(r"<loc>([^<]+)", get_html(sm, timeout=60)) if re.search(r"pokemon", u, re.I)]
    out = []
    for u in urls:
        title = re.sub(r"-\w+$", "", u.rsplit("/", 1)[-1]).replace("-", " ")
        if is_tcg(title) and anniversary(title):
            out.append([u, title])
    CHECKERS_CACHE.write_text(json.dumps({"t": time.time(), "urls": out}))
    return out

def checkers_product(store, url):
    """One Checkers product page -> item (in stock only when it can be ordered for delivery)."""
    with CHECKERS_GATE:
        h = get_html(url)
    name = re.search(r'"@type":"Product","name":"([^"]+)"', h)
    m = re.search(r'"price":([\d.]+),"outOfStock":(true|false),"discountedPrice":([\d.]+|null)', h)
    if not (name and m):
        raise ValueError("Checkers page has no product data (layout changed or blocked)")
    price = float(m.group(3)) if m.group(3) != "null" else float(m.group(1))
    return item(store, url.rsplit("-", 1)[-1], json.loads(f'"{name.group(1)}"'), url,
                price or None, m.group(2) == "false" and price > 0)

def scan_checkers(store, base):
    out = []
    for u, _ in checkers_urls(base):
        check_deadline()
        out.append(checkers_product(store, u))
        time.sleep(1)
    return out

# Mithril Vault (2026-10-12): left Shopify for its own Next.js site. Its search pages carry each product's
# handle, title, price and stock in the page data; pre-orders are taken on preorder.<domain>, one page per set.
MITHRIL_SEARCHES = ("30th", "delta reign", "elite trainer", "booster box")
MITHRIL_PRODUCT = re.compile(r'"product":\{"handle":"([^"]+)","title":"((?:[^"\\]|\\.)*)","price":"([^"]*)",'
                             r'"available":(true|false)(?:[^{}]*?"kind":"(\w+)")?')
MITHRIL_LANG = re.compile(r"\b(japanese|chinese|korean|thai|indonesian)\b", re.I)

def _mithril_title(handle, title):
    """Its titles often drop the set and 'Pokemon' ("Elite Trainer Box"); the handle has both."""
    h = handle.replace("-", " ")
    lang = MITHRIL_LANG.search(h)
    if lang and not MITHRIL_LANG.search(title):
        title = f"{lang.group(1).title()} {title}"
    if "30th celebration" in h and not THIRTIETH.search(title):
        title = f"30th Celebration {title}"
    if h.startswith("pokemon") and "pokemon" not in norm(title).lower():
        title = f"Pokemon TCG {title}"
    return title

def scan_mithril(store, base):
    base = base.rstrip("/")
    out, seen = [], set()
    for q in MITHRIL_SEARCHES:
        check_deadline()
        page = get_html(f"{base}/search?" + urllib.parse.urlencode({"q": q})).replace('\\"', '"')
        for handle, title, price, avail, kind in MITHRIL_PRODUCT.findall(page):
            if handle in seen or (kind and kind != "sealed"):
                continue
            seen.add(handle)
            title = _mithril_title(handle, json.loads(f'"{title}"'))
            price = float(price or 0)
            if not is_tcg(title) or price < 20:  # R0 / R10 are placeholder listings, not real prices
                continue
            out.append(item(store, handle, title, f"{base}/products/{handle}", price, avail == "true"))
    pre = base.replace("://", "://preorder.", 1)
    for s in sorted(set(re.findall(r'href="/sets/([\w-]+)"', get_html(pre + "/")))):
        if not s.startswith("pokemon"):
            continue
        check_deadline()
        h = get_html(f"{pre}/sets/{s}")
        is_open = "Pre-orders open" in h
        d = h.replace('\\"', '"')
        for pid, name, price in re.findall(r'\{"id":"(\w+)","name":"((?:[^"\\]|\\.)*)","sellingPrice":([\d.]+)', d):
            name = json.loads(f'"{name}"')
            if is_tcg(name):
                out.append(item(store, f"pre-{pid}", name, f"{pre}/sets/{s}", price, is_open))
    return out

# Eden Toys (George; Gideon 2026-10-21): an ASP.NET shop with no product API. Its Pokemon category page
# (newest first) lists each product as a card: link /products/<slug>/<id>, title, price, and an enabled
# "Add to Cart" button only when it can be bought ("View Item", disabled, when sold out). Small shop:
# read once a minute (quick scan), not in the 5-second loop.
EDEN_CARD = re.compile(r'href="(/products/[^"]+/(\d+))".*?btn_AddCart" value="([^"]+)"([^>]*)>.*?'
                       r'lbl_Heading">([^<]+)</h4>.*?lbl_Price" class="price">\s*R\s?([\d,.]+)', re.S)

def scan_eden(store, base):
    st = STORE_BY_NAME.get(store) or {}
    root = base.rstrip("/")
    h = get_html(root + st.get("category", "/shop/pok%C3%A9mon/205"))
    out = []
    for href, pid, button, attrs, title, price in EDEN_CARD.findall(h):
        title = html.unescape(title).strip()
        if is_tcg(title):
            out.append(item(store, pid, title, root + urllib.parse.quote(html.unescape(href), safe="/%"),
                            float(price.replace(",", "")),
                            button.strip().lower() == "add to cart" and "disabled" not in attrs))
    if not out and "product-cols" not in h:
        raise ValueError("Eden Toys category page has no product cards (layout changed or blocked)")
    return out

# Amazon.co.za (Gideon 2026-10-21). Its search page answers scripts with a bot check, which is not
# worked around. Its category / best-seller / new-release pages and product pages load normally and
# robots.txt does not disallow them, so: discover products on those listing pages, then read each
# wanted product's own page (price, availability, seller), politely spaced. No 1-minute or 5-second checks.
AMAZON_ASIN_TITLE = re.compile(r'title="([^"]{6,200})"[^>]*?href="[^"]*?/dp/(B[0-9A-Z]{9})'
                               r'|href="[^"]*?/dp/(B[0-9A-Z]{9})[^"]*"[^>]*>\s*(?:<[^>]+>\s*)*?<img[^>]+alt="([^"]{6,200})"')
AMAZON_GAP = [0.0]

def amazon_get(url):
    gap = (STORE_BY_NAME.get("Amazon.co.za") or {}).get("request_gap_seconds", 3)
    wait = AMAZON_GAP[0] + gap - time.time()
    if wait > 0:
        time.sleep(wait)
    AMAZON_GAP[0] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html",
                                               "Accept-Language": "en-ZA,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=25, context=SSL_CTX) as r:
        if r.status == 202:  # Amazon's bot check: treat as a failed request, never work around it
            raise ValueError("Amazon answered with its bot check (202)")
        return r.read().decode("utf-8", "replace")

def amazon_product(store, asin, base="https://www.amazon.co.za"):
    """One product page -> item, plus the seller. Buyable only with an add-to-cart button and no
    'currently unavailable' notice."""
    url = f"{base}/dp/{asin}"
    h = amazon_get(url)
    t = re.search(r'id="productTitle"[^>]*>\s*([^<]+)', h)
    if not t:
        raise ValueError("Amazon product page without a title (layout changed or blocked)")
    price = re.search(r'"priceAmount":([\d.]+)', h)
    avail = re.search(r'id="availability"[^>]*>(.*?)</div>', h, re.S)
    avail_txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", avail.group(1))).strip()[:80] if avail else ""
    seller = (re.search(r'id=["\']sellerProfileTriggerId["\'][^>]*>\s*([^<]{2,60})', h)
              or re.search(r"Sold by:?\s*(?:<[^>]+>\s*)*([^<]{2,60})", h))
    buyable = 'id="add-to-cart-button"' in h and not re.search(r"currently unavailable|out of stock", avail_txt, re.I)
    it = item(store, asin, html.unescape(t.group(1)).strip(), url, float(price.group(1)) if price else None, buyable)
    it["seller"] = seller.group(1).strip() if seller else ""
    return it

AMAZON_SEEN = HERE / ".amazon_asins.json"  # wanted products found on earlier listing pages (they rotate)

def scan_amazon(store, base):
    st = STORE_BY_NAME.get(store) or {}
    base = base.rstrip("/")
    found, pages_ok = {}, 0
    for path in st.get("pages", []):
        check_deadline()
        try:
            h = amazon_get(base + path)
        except Exception as e:
            print(f"amazon page {path} failed: {e}", file=sys.stderr)
            continue
        pages_ok += 1
        for t1, a1, a2, t2 in AMAZON_ASIN_TITLE.findall(h):
            asin, title = (a1, t1) if a1 else (a2, t2)
            found.setdefault(asin, html.unescape(title))
    try:
        seen = json.loads(AMAZON_SEEN.read_text())
    except Exception:
        seen = {}
    seen.update({a: t for a, t in found.items() if is_tcg(t) and anniversary(t)})
    try:
        AMAZON_SEEN.write_text(json.dumps(seen))
    except Exception:
        pass
    # every wanted product ever seen, plus the configured ones, gets its own product-page read
    wanted = list(dict.fromkeys(list(st.get("asins", [])) + list(seen)))[:st.get("max_product_pages", 20)]
    out = []
    for asin in wanted:
        check_deadline()
        try:
            out.append(amazon_product(store, asin, base))
        except Exception as e:
            print(f"amazon product {asin} failed: {e}", file=sys.stderr)
    if not out and not pages_ok:
        raise ValueError("Amazon: no listing page and no product page could be read")
    return out

SCANNERS = {"shopify": scan_shopify, "woo": scan_woo, "checkers": scan_checkers,
            "takealot": scan_takealot, "magento": scan_magento, "mithril": scan_mithril, "eden": scan_eden,
            "amazon": scan_amazon}

# ---------------------------------------------------------------- notify

SECRETS_FILE = HERE / "secrets.json"  # telegram / discord credentials, written by setup_phone.py

def status_chat():
    """Telegram group for monitor status and watchdog messages (Gideon, 2026-10-20: keep them out of the
    product-alert chat). None until the group exists; status then falls back to the alert chat."""
    return os.environ.get("TELEGRAM_STATUS_CHAT_ID") or CONFIG.get("telegram_status_chat_id")

def alert_chats(main_chat):
    """Deal alerts go to the private chat plus any shared deals channel/group (config alert_chat_ids,
    added with setup_deals_channel.py) so friends can follow without the bot needing subscriptions."""
    out = []
    for c in [main_chat, *CONFIG.get("alert_chat_ids", []), *os.environ.get("TELEGRAM_ALERT_CHAT_IDS", "").split(",")]:
        c = str(c).strip()
        if c and c != "None" and c not in out:
            out.append(c)
    return out

def notify(title, message, priority=3, click=None, tg_html=None, silent=False, channel="alerts", cart=None, to=None):
    """Send to every channel that is set up: Telegram, Discord, and ntfy if enabled.
    silent: Telegram delivers without sound (used for the every-minute status).
    channel: "alerts" (product news: the main chat and any shared deals channel) or "status" (the monitor's
    own health and checks, the status group). to: only these chat ids (used when resending a failed message)."""
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
        chats = [status_chat() or tg["chat_id"]] if channel == "status" else alert_chats(tg["chat_id"])
        if to:
            chats = [c for c in map(str, to)]
        text = f"<b>{html.escape(title)}</b>\n" + (tg_html or html.escape(message))
        if cart:
            text += f'\n\n<a href="{html.escape(cart)}">ADD TO CART (1 tap)</a>'
        if click:
            text += ("\n" if cart else "\n\n") + f'<a href="{html.escape(click)}">Open product</a>'
        for chat in chats:
            sends.append(("telegram", f"https://api.telegram.org/bot{tg['token']}/sendMessage",
                          {"chat_id": chat, "text": text, "parse_mode": "HTML",
                           "disable_web_page_preview": bool(tg_html), "disable_notification": silent}))
    if sec.get("discord_webhook"):
        content = f"**{title}**\n{message}" + (f"\nAdd to cart: {cart}" if cart else "") + (f"\n{click}" if click else "")
        sends.append(("discord", sec["discord_webhook"], {"content": content[:1990]}))
    if CONFIG.get("ntfy_enabled", True) and CONFIG.get("ntfy_topic"):
        body = {"topic": CONFIG["ntfy_topic"], "title": title, "message": message, "priority": priority}
        if click:
            body["click"] = click
        sends.append(("ntfy", CONFIG.get("ntfy_server", "https://ntfy.sh"), body))
    for name, url, body in sends:
        err = _post_with_retry(url, body, name)
        if err is None:
            print(f"sent via {name}: {title}")
            continue
        print(f"notify via {name} failed:", err, file=sys.stderr)  # never let a failed push kill the run
        if name == "telegram":  # kept and retried next run (see flush_outbox), for this chat only
            TG_STATS["failed"] += 1
            OUTBOX.append({"title": title, "message": message, "click": click, "chat": body.get("chat_id"),
                           "tg_html": tg_html, "t": time.time(), "channel": channel, "cart": cart})

TG_STATS = {"sent": 0, "retried": 0, "failed": 0}

def _post_with_retry(url, body, name, attempts=3):
    """POST JSON; retry network errors, 5xx and Telegram's 429 (honouring retry_after, capped at 10 s).
    Other 4xx (bad token, chat gone, bad HTML) are permanent: retrying would not help. Returns None or an error."""
    err = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
                "User-Agent": UA, "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20, context=SSL_CTX).read()
            if name == "telegram":
                TG_STATS["sent"] += 1
            return None
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
            if e.code == 429:
                try:
                    wait = json.loads(e.read().decode()).get("parameters", {}).get("retry_after", 2)
                except Exception:
                    wait = 2
            elif e.code >= 500:
                wait = 2 * (i + 1)
            else:
                return err
        except Exception as e:
            err, wait = f"{type(e).__name__}: {str(e)[:120]}", 2 * (i + 1)
        if i < attempts - 1:
            if name == "telegram":
                TG_STATS["retried"] += 1
            time.sleep(min(float(wait), 10))
    return err

OUTBOX = []  # Telegram messages that failed this run

def flush_outbox(state):
    """Resend Telegram messages that failed on an earlier run (e.g. on a network that blocks Telegram).
    Anything older than a day is dropped: stock news that old is no use."""
    pending = [m for m in state.pop("outbox", []) if time.time() - m["t"] < 86400
               # a product Gideon has since ruled out (e.g. Pitch Black) is not resent either
               and not any(p.search(f"{m.get('title', '')} {m.get('message', '')}") for p, _ in matching.NEVER)]
    for m in pending:
        title = m["title"] if m["title"].endswith("(delayed)") else m["title"] + " (delayed)"
        notify(title, m["message"], click=m["click"], tg_html=m["tg_html"], channel=m.get("channel", "alerts"),
               cart=m.get("cart"), to=[m["chat"]] if m.get("chat") else None)

def fmt(it):
    price = f"R{it['price']:,.2f}" if it["price"] is not None else "price n/a"
    stock = "in stock / pre-order" if it["in_stock"] else "out of stock"
    return f"{it['title']}\n{it['store']} | {price} | {stock}"

def deal_msg(d):
    """Short: cost, resale, profit. Details stay on the dashboard."""
    return (f"{d['title']}\n"
            f"Cost R{d['price']:,.0f} | Resell ~R{d['value']:,.0f} | Profit ~R{d['profit']:,.0f} ({d['roi']:.0%})")

# ---------------------------------------------------------------- restock watch

STORE_BY_NAME = {s["name"]: s for s in CONFIG["stores"]}

def update_watchlist(deals, state):
    """Sold-out listings that would be profitable at their listed price. Watched silently;
    the only message is the one sent when a listing becomes buyable."""
    watch = state.setdefault("watch", {})
    fresh = {d["key"]: d for d in deals if d["profitable"] and not d["in_stock"] and mine(d["store"])
             and is_30th(d["title"])
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
                 and STORE_BY_NAME.get(i["store"], {}).get("type") in ("shopify", "takealot")
                 # older ETBs abroad are never valued (no shipping/import in their price), so can't alert
                 and not (anniversary(i["title"]) == "ETB" and STORE_BY_NAME[i["store"]].get("currency"))]
    for k, d in picked:
        watch[k] = {f: d[f] for f in ("store", "title", "url", "price", "value", "profit", "roi")}
        watch[k]["is30"] = is_30th(d["title"])
    thirtieth.sort(key=lambda i: anniversary(i["title"]) == "ETB")
    for i in thirtieth[:CONFIG["updates"]["max_watch_30th"]]:
        d = by_key.get(i["key"], {})
        watch[i["key"]] = {"store": i["store"], "title": i["title"], "url": i["url"], "price": i["price"],
                           "value": d.get("value"), "is30": True}

def poll_listing(w):
    """(buyable, rand price) from the product page, or None when the check failed (status unknown)."""
    status, price, _ = verify_listing({"store": w["store"], "url": w["url"], "title": w.get("title", ""),
                                       "key": f"{w['store']}|{w['pid']}", "price": w.get("price")})
    return None if status == UNKNOWN else (status in BUYABLE, price)

LAST_DEALS = []  # valuations from the latest full scan, reused by quick scans
WANTED_TERMS = ("30th", "delta reign", "elite trainer", "booster box")  # WooCommerce quick searches covering every wanted listing

def known_stores(state):
    """Shops whose listings this copy already knows, so a listing it hasn't seen is genuinely new.
    A shop this copy hasn't scanned successfully in a day (e.g. just moved here from the Mac) starts
    over: its first scan is recorded silently instead of announcing its whole stock."""
    last_ok = state.get("last_ok", {})
    return {n for n, st in state["stores"].items()
            if (st.get("ok") or st.get("count")) and time.time() - last_ok.get(n, 0) < 86400}

def quick_fetch(st):
    """Cheap 'what changed' request per store: newest listings only."""
    name, base, kind = st["name"], st["url"], st["type"]
    if kind == "amazon":
        return []  # full scans only (heavy pages); sold-out wanted products are rechecked by the watch
    if kind == "shopify":
        # a collection URL is already narrowed to Pokemon, so read all of it (it isn't sorted newest first)
        prods = get_json(f"{base}/products.json?limit={250 if '/collections/' in base else 50}", tries=1).get("products", [])
        out = []
        for p in prods:
            if is_tcg(p["title"], f'{p.get("vendor", "")} {p.get("product_type", "")}'):
                out.append(shopify_item(name, base, p))
        return out
    if kind == "woo":
        prods, seen = [], set()
        urls = [f"{base}/wp-json/wc/store/v1/products?search={urllib.parse.quote(t)}&orderby=date&order=desc&per_page=30"
                for t in st.get("search", ["pokemon"])]
        # every wanted listing's stock in one request per term, so restocks show up within a minute
        urls += [f"{base}/wp-json/wc/store/v1/products?search={urllib.parse.quote(t)}&per_page=100" for t in WANTED_TERMS]
        for u in urls:
            for p in get_json(u, tries=1, timeout=st.get("timeout", 25)):
                if p["id"] not in seen:
                    seen.add(p["id"])
                    prods.append(p)
        out = []
        for p in prods:
            if is_tcg(p["name"]):
                pr = p.get("prices") or {}
                price = int(pr["price"]) / 10 ** pr.get("currency_minor_unit", 2) if pr.get("price") else None
                out.append(item(name, p["id"], p["name"], p["permalink"], price,
                                p.get("is_in_stock") or p.get("is_on_backorder")))
        return out
    if kind == "takealot":  # targeted searches instead of the whole Pokemon catalogue
        out = []
        for q in ("pokemon 30th", "pokemon 30th celebration", "pokemon delta reign", "pokemon booster box",
                  "pokemon elite trainer box"):
            d = get_json("https://api.takealot.com/rest/v-1-12-0/searches/products?" + urllib.parse.urlencode({"qsearch": q}), tries=1)
            for r in d["sections"]["products"].get("results", []):
                pv, core = r["product_views"], r["product_views"]["core"]
                if is_tcg(core["title"]):
                    bb = pv.get("buybox_summary") or {}
                    out.append(item(name, core["id"], core["title"],
                                    f"https://www.takealot.com/{core['slug']}/PLID{core['id']}",
                                    (bb.get("prices") or [None])[0], bb.get("is_add_to_cart_available")))
        return out
    if kind == "checkers":
        return []  # covered by the 5-second 30th checks; the full scan reads every page every 5 minutes
    return SCANNERS[kind](name, base)  # magento store is small: full scan is cheap

def quick_scan(state):
    """Every minute: newest / wanted listings at every store; changes go through the alert path."""
    cool = state.setdefault("cooldown", {})
    stores = [st for st in active_stores() if cool.get(st["name"], 0) <= time.time()
              and not (IS_GITHUB and st["type"] == "shopify")]  # cloud: Shopify shops get the 5-second checks instead
    known = known_stores(state)
    events, now = [], dt.datetime.now().isoformat(timespec="seconds")
    ex = cf.ThreadPoolExecutor(max_workers=10)
    futs = {ex.submit(quick_fetch, st): st["name"] for st in stores}
    done = []
    try:  # a slow shop (ThunderBolt takes ~1 min) must not delay the every-minute check of the others
        for f in cf.as_completed(futs, timeout=CONFIG["quick_scan_seconds"] * 0.6):
            done.append(f)
    except cf.TimeoutError:
        pass
    ex.shutdown(wait=False, cancel_futures=True)
    for f in done:
        if True:
            name = futs[f]
            try:
                found = localize(name, f.result())
                record_check(state, name, True, "quick")
            except Exception as e:
                record_check(state, name, False, "quick", e)
                if "429" in str(e):
                    cool[name] = time.time() + CONFIG["cooldown_minutes"] * 60
                continue
            for it in found:
                merge_item(state, it, now, known, events)
    sent = [it["key"] for change, it in events if it["in_stock"] and consider(state, it, "quick")]
    with STATE_LOCK:
        for k in sent:
            state.get("watch", {}).pop(k, None)
    print(f"quick scan {now[11:16]}: {len(stores)} stores, {len(events)} changes, {len(sent)} alerts")
    return len(done)

def minute_status(state, stores_checked, polled):
    """Cloud only (Gideon, 2026-10-07): a silent Telegram status every minute; in-stock alerts stay loud."""
    if not (IS_CLOUD and CONFIG["updates"].get("minute_status")):
        return
    live = sorted((i for i in state["items"].values()
                   if i["in_stock"] and is_30th(i["title"]) and current(i) and i["store"] in STORE_BY_NAME),
                  key=lambda i: i["price"] or 0)
    lines = [f"{dt.datetime.now(dt.timezone(dt.timedelta(hours=2))):%H:%M}: {stores_checked} stores and "
             f"{polled} sold-out listings checked. English 30th buyable now: {len(live)}"]
    rows = [(f"{i['store']} | R{i['price']:,.0f}" if i.get("price") else i["store"], i) for i in live[:10]]
    lines += [f"{head} | {i['title'][:60]} {i['url']}" for head, i in rows]
    links = [html.escape(lines[0])] + [f'{html.escape(head)}\n<a href="{html.escape(i["url"])}">{html.escape(i["title"][:80])}</a>'
                                       for head, i in rows]
    if len(live) > 10:
        lines.append(f"...and {len(live) - 10} more")
        links.append(f"...and {len(live) - 10} more")
    notify("Pokemon check", "\n".join(lines), priority=1, silent=True, tg_html="\n\n".join(links), channel="status")

def _safe_poll(w, cool):
    """(status, rand price, detail) for one watched listing; failures come back as UNKNOWN."""
    res = verify_listing({"store": w["store"], "url": w["url"], "title": w.get("title", ""),
                          "key": f"{w['store']}|{w['pid']}", "price": w.get("price")})
    if res[0] == UNKNOWN:
        if "429" in res[2]:
            cool[w["store"]] = time.time() + CONFIG["cooldown_minutes"] * 60
        elif any(c in res[2] for c in ("403", "500", "502", "503")):  # blocked or site down: pause its listing polls
            cool[w["store"] + "|watch"] = time.time() + 600
        print("watch poll failed:", w["store"], res[2], file=sys.stderr)
    return res

def watch_loop(state, until):
    """Between full scans, on a true 1-minute beat: a quick scan of every store (in its own thread, so
    slow shops can't delay it) and stock checks of the watched sold-out listings, least recently checked
    first, cut off after ~50 s so the next minute starts on time."""
    watch = state.setdefault("watch", {})
    cfg = CONFIG["resale"]
    print(f"watching {len(watch)} sold-out listings (profitable or 30th) until {dt.datetime.fromtimestamp(until):%H:%M}")
    qs = cf.ThreadPoolExecutor(max_workers=1)
    quick = None
    while time.time() < until - 20:
        t0 = time.time()
        if CONFIG["quick_scan_seconds"] and (quick is None or quick.done()):
            quick = qs.submit(quick_scan, state)  # never two at once; a slow one just skips a beat
        try:
            scan_report(state)  # self-throttled (status_report_minutes); here so it isn't tied to the scan cycle
        except Exception as e:
            print("scan report failed:", e, file=sys.stderr)
        cool = state.setdefault("cooldown", {})
        names = {st["name"] for st in active_stores()}  # cloud leaves Mac-only shops to the Mac
        # cloud: Shopify restocks show up in the 5-second checks (in-stock first), so their product pages
        # aren't polled as well; ~325 page reads a minute from GitHub's shared IPs got throttled
        due = [(k, w) for k, w in watch.items() if w["store"] in names
               and not (IS_GITHUB and STORE_BY_NAME.get(w["store"], {}).get("type") == "shopify")
               and max(cool.get(w["store"], 0), cool.get(w["store"] + "|watch", 0)) <= time.time()
               and time.time() - w.get("polled", 0) >= STORE_BY_NAME.get(w["store"], {}).get("watch_every_seconds", 0)]
        due.sort(key=lambda kw: kw[1].get("polled", 0))
        for k, w in due:
            w["pid"] = k.split("|", 1)[1]
        polled = {}
        ex = cf.ThreadPoolExecutor(max_workers=8)  # one slow shop must not hold up the rest
        futs = {ex.submit(_safe_poll, w, cool): k for k, w in due}
        try:
            for f in cf.as_completed(futs, timeout=max(5, 50 - (time.time() - t0))):
                polled[futs[f]] = f.result()
        except cf.TimeoutError:
            pass  # the rest go first next minute
        ex.shutdown(wait=False, cancel_futures=True)
        for key, w in due:
            if key not in polled:
                continue
            w["polled"] = time.time()
            status, price, detail = polled[key]
            if status == UNKNOWN:
                record_check(state, w["store"], False, "watch", detail)
                continue  # a failed check says nothing about stock: keep watching
            record_check(state, w["store"], True, "watch")
            if status not in BUYABLE:
                continue
            it = dict(state.get("items", {}).get(key) or {}, key=key, store=w["store"], title=w["title"],
                      url=w["url"], price=price or w.get("price"), in_stock=True)
            consider(state, it, "watch", verified=(status, price, detail))
            with STATE_LOCK:
                if key in state.get("items", {}):
                    state["items"][key].update(in_stock=True, price=price or w.get("price"))
                watch.pop(key, None)
        print(f"watch {dt.datetime.now():%H:%M}: {len(polled)} of {len(due)} sold-out listings checked")
        time.sleep(max(1, 60 - (time.time() - t0)))
    qs.shutdown(wait=False, cancel_futures=True)

# ---------------------------------------------------------------- fast 30th checks
# Gideon, 2026-10-09: English 30th listings checked every 5-10 s. One request per shop returns all of
# its 30th listings with live stock, so 16 shops cost ~3 requests a second, not one per listing.
# A hit is re-checked on the product's own page before any alert, so a stale search can't alert.
CHECKERS_TURN = [-1]

def fast_fetch(st):
    """The shop's 30th listings, available ones first: [item]."""
    name, kind = st["name"], st["type"]
    base = st["url"].split("/collections/")[0]
    q = CONFIG["fast_30th"].get("query", "30th")
    out = []
    if kind == "shopify" and st.get("fast_collection"):
        # shops whose search is flooded by singles, tokens or books: read a small 30th / sealed collection instead
        fc = st["fast_collection"]
        prods = []
        for h in ([fc] if isinstance(fc, str) else fc):
            with SHOPIFY_GATE:
                prods += get_json(f"{base}/collections/{h}/products.json?limit=250", tries=1, timeout=15)["products"]
        for p in prods:
            out.append(shopify_item(name, base, p))
    elif kind == "shopify":
        url = f"{base}/search/suggest.json?" + urllib.parse.urlencode({
            "q": q, "resources[type]": "product", "resources[limit]": 10,
            "resources[options][unavailable_products]": "last"})  # in-stock first: a restock is always in the top 10
        with SHOPIFY_GATE:
            d = get_json(url, tries=1, timeout=10)
        for p in d["resources"]["results"]["products"]:
            out.append(item(name, p["id"], p["title"], f"{base}/products/{p['handle']}", p.get("price"), p.get("available")))
    elif kind == "woo":
        for p in get_json(f"{base}/wp-json/wc/store/v1/products?search={urllib.parse.quote(q)}&per_page=100",
                          tries=1, timeout=st.get("timeout", 15)):
            pr = p.get("prices") or {}
            price = int(pr["price"]) / 10 ** pr.get("currency_minor_unit", 2) if pr.get("price") else None
            out.append(item(name, p["id"], p["name"], p["permalink"], price, p.get("is_in_stock") or p.get("is_on_backorder")))
    elif kind == "checkers":
        # no search without a login: one 30th product page per beat, in turn
        pages = [u for u, t in checkers_urls(base) if anniversary(t) == "30TH ANNIVERSARY"]
        if pages:
            CHECKERS_TURN[0] = (CHECKERS_TURN[0] + 1) % len(pages)
            out.append(checkers_product(name, pages[CHECKERS_TURN[0]]))
        return out  # rand prices already
    elif kind == "takealot":
        d = get_json("https://api.takealot.com/rest/v-1-12-0/searches/products?" + urllib.parse.urlencode({"qsearch": f"pokemon {q}"}),
                     tries=1, timeout=10)
        for r in d["sections"]["products"].get("results", []):
            pv, core = r["product_views"], r["product_views"]["core"]
            bb = pv.get("buybox_summary") or {}
            out.append(item(name, core["id"], core["title"], f"https://www.takealot.com/{core['slug']}/PLID{core['id']}",
                            (bb.get("prices") or [None])[0], bb.get("is_add_to_cart_available")))
    return [i for i in localize(name, out) if anniversary(i["title"]) == "30TH ANNIVERSARY" and is_tcg(i["title"])]

def fast_handle(state, st, found, known):
    """Record stock; a 30th listing that just became buyable goes through the alert path (which re-checks it
    on the product page before anything is sent)."""
    now = dt.datetime.now().isoformat(timespec="seconds")
    for it in found:
        change = merge_item(state, dict(it), now, known)
        if change and it["in_stock"] and st["name"] in known:
            consider(state, it, "fast30")

def fast_loop(state, stop):
    """Each of my shops every fast_30th.seconds; a shop that errors slows down (doubling, up to
    max_backoff_seconds) and speeds back up as it answers; a 429 also pauses it for cooldown_minutes."""
    cfg = CONFIG["fast_30th"]
    base_wait = cfg["seconds"]
    stores = [st for st in active_stores() if mine(st["name"]) and st["type"] in ("shopify", "woo", "takealot", "checkers")
              and st["name"] not in cfg.get("skip", [])]
    known = known_stores(state)
    # cloud: reading a whole 30th collection is a much bigger request than a search; every 5 s from
    # GitHub's shared IPs it got throttled, so those shops are read every cloud_collection_seconds there
    floor = {st["name"]: (cfg.get("cloud_collection_seconds", 30) if IS_GITHUB and st.get("fast_collection")
                          else base_wait) for st in stores}
    wait = dict(floor)
    due = {st["name"]: time.time() + i * base_wait / max(1, len(stores)) for i, st in enumerate(stores)}  # spread out
    cool = state.setdefault("cooldown", {})
    stats = {"ok": 0, "err": {}}
    last_log = time.time()
    ex = cf.ThreadPoolExecutor(max_workers=6)
    running = {}
    while not stop.is_set():
        t = time.time()
        for st in stores:
            n = st["name"]
            if n in running or due[n] > t or max(cool.get(n, 0), cool.get(n + "|fast", 0)) > t:
                continue
            running[n] = (ex.submit(fast_fetch, st), t)
        for n, (f, started) in list(running.items()):
            if not f.done():
                continue
            del running[n]
            try:
                found = f.result()
                record_check(state, n, True, "fast")
                fast_handle(state, STORE_BY_NAME[n], found, known)
                stats["ok"] += 1
                wait[n] = max(floor[n], wait[n] / 2)
            except Exception as e:
                record_check(state, n, False, "fast", e)
                code = re.search(r"HTTP Error (\d+)", str(e))
                k = f"{n} {code.group(1) if code else type(e).__name__}"
                stats["err"][k] = stats["err"].get(k, 0) + 1
                wait[n] = min(cfg["max_backoff_seconds"], wait[n] * 2)
                if "429" in str(e):  # pauses only these fast checks; the shop's scans carry on
                    cool[n + "|fast"] = time.time() + CONFIG["cooldown_minutes"] * 60
            due[n] = max(started + wait[n], time.time() + 1)  # a steady beat from each check's start
        if time.time() - last_log >= 60:
            slow = {n: round(w) for n, w in wait.items() if w > floor[n]}
            print(f"fast30 {dt.datetime.now():%H:%M}: {len(stores)} shops, {stats['ok']} checks ok, "
                  f"errors {stats['err'] or 'none'}{', slowed ' + str(slow) if slow else ''}", flush=True)
            stats, last_log = {"ok": 0, "err": {}}, time.time()
        stop.wait(0.5)
    ex.shutdown(wait=False, cancel_futures=True)

_FAST = [None, None]  # the running fast thread, so a crashed cycle in --loop mode never leaves two

def start_fast(state):
    stop_fast(*_FAST)
    if not CONFIG.get("fast_30th", {}).get("enabled"):
        return None, None
    stop = threading.Event()
    th = threading.Thread(target=fast_loop, args=(state, stop), daemon=True)
    th.start()
    _FAST[:] = [th, stop]
    return th, stop

def stop_fast(th, stop):
    if th:
        stop.set()
        th.join(timeout=30)

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

def server_owned():
    """Shops the Oracle server checks and alerts for: config server_stores (those proven to answer the
    server; 2026-10-20 only Geek Zone, the other Mac-only shops refuse data-centre IPs), or every
    Mac-only shop once local_owner is "server"."""
    local_only = set(CONFIG.get("local_only_stores", []))
    return set(CONFIG.get("server_stores", [])) | (local_only if CONFIG.get("local_owner") == "server" else set())

def active_stores():
    local_only = set(CONFIG.get("local_only_stores", []))
    if IS_SERVER:  # the small free server only scans the shops it owns
        return [s for s in CONFIG["stores"] if s["name"] in server_owned()]
    return [s for s in CONFIG["stores"] if not (IS_GITHUB and s["name"] in local_only)]

def probe_local_only():
    """Log which Mac-only stores the cloud can reach now, so they can move to the cloud."""
    def one(st):
        try:
            return st["name"], f"ok ({len(quick_fetch(st))} TCG items)"
        except Exception as e:
            return st["name"], f"fail: {str(e)[:60]}"
    stores = [s for s in CONFIG["stores"] if s["name"] in set(CONFIG.get("local_only_stores", []))]
    for st in stores:  # one at a time, so a burst of requests doesn't trigger rate limits by itself
        name, res = one(st)
        print(f"probe {name}: {res}")
        time.sleep(3)

def mine(store):
    """Which copy alerts for a shop, so no listing alerts twice: GitHub owns every shop it scans (all but
    local_only_stores); the shops GitHub can't reach belong to whichever of the Mac or the Oracle server
    config local_owner names ("mac" until a server is set up and verified)."""
    if IS_GITHUB:
        return True
    if IS_SERVER:
        return store in server_owned()
    return store in set(CONFIG.get("local_only_stores", [])) - server_owned()

def heartbeat(state, results, deals):
    """If nothing has been sent for a while, send a short 'still watching' check-in (cloud only)."""
    hours = CONFIG["updates"]["heartbeat_hours"]
    if not IS_CLOUD or not hours or time.time() - state.get("last_msg", 0) < hours * 3600:
        return
    tracked = [i for i in state["items"].values() if is_30th(i["title"]) and current(i)]
    buyable = sum(i["in_stock"] for i in tracked)
    notify("Pokemon monitor: still watching",
           f"{len(results)} stores checked. Tracking {len(tracked)} English 30th Anniversary listings, "
           f"{buyable} buyable. You'll get a message the moment another one is.", channel="status")
    state["last_msg"] = time.time()

def scan_report(state):
    """Scanning update every status_report_minutes (4, Gideon 2026-10-20) to the separate status group (never to the deal chat): shops checked,
    failing shops, alerts and why listings were turned down. Sends nothing until telegram_status_chat_id is set."""
    every = CONFIG["updates"].get("status_report_minutes", 0)
    if not IS_CLOUD and not CONFIG["updates"].get("mac_status_report"):
        return  # the Mac's problems still reach the group through the watchdog; routine updates come from the cloud copies
    if not every or not status_chat() or time.time() - state.get("last_status_report", 0) < every * 60:
        return
    role = "Cloud" if IS_GITHUB else "Server" if IS_SERVER else "Mac"
    names = [st["name"] for st in active_stores() if mine(st["name"]) and st["name"] not in CONFIG.get("known_blocked", [])]
    with STATE_LOCK:
        shops = state.get("shop_stats", {})
        stats = json.loads(json.dumps(state.get("stats", {})))
        limit = CONFIG["updates"].get("store_stale_minutes", 45) * 60
        bad = [n for n in names if time.time() - (shops.get(n) or {}).get("last_ok", 0) > limit]
        fast = [_median_gap((shops.get(n) or {}).get("beats_fast", [])) for n in names]
    fast = sorted(f for f in fast if f)
    c, r = stats.get("counts", {}), stats.get("rejected", {})
    lines = [f"{role}: {len(names) - len(bad)} of {len(names)} shops checked in the last {limit // 60} min"
             + (f"; 30th checks every ~{fast[len(fast) // 2]:.0f} s (median)" if fast else ""),
             "Not being checked: " + (", ".join(bad) if bad else "none"),
             f"Today: {c.get('discovered', 0)} new listings, {c.get('verified_buyable', 0)} confirmed buyable, "
             f"{c.get('alerts_sent', 0)} deal alerts",
             "Turned down today: " + (", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(r.items(), key=lambda kv: -kv[1])
                                              if k not in ("irrelevant", "duplicate")) or "nothing"),
             f"Telegram: {TG_STATS['failed']} failed sends this run, {len(state.get('outbox', [])) + len(OUTBOX)} waiting"]
    notify(f"Scan update ({role}, {sast_now():%H:%M})", "\n".join(lines), priority=1, silent=True, channel="status")
    state["last_status_report"] = time.time()

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
                              "--json", "status,conclusion,createdAt"], cwd=HERE, capture_output=True, text=True, timeout=30)
        runs = json.loads(out.stdout or "[]")
        active = any(r["status"] != "completed" for r in runs)
        last = max((dt.datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) for r in runs), default=None)
        stale = last is None or dt.datetime.now(dt.timezone.utc) - last > dt.timedelta(minutes=30)
        done = [r for r in runs if r["status"] == "completed"][:3]
        flag = HERE / ".cloud_warned"
        if len(done) == 3 and all(r.get("conclusion") != "success" for r in done) and CONFIG["updates"].get("status_messages"):
            if not flag.exists():
                notify("Pokemon monitor problem", "The last 3 cloud runs did not finish. Alerts may be missed until this is fixed.", channel="status")
                flag.write_text("1")
        elif done and done[0].get("conclusion") == "success" and flag.exists():
            flag.unlink()
        if stale and not active:
            subprocess.run([gh, "workflow", "run", "monitor.yml", "--ref", "main"], cwd=HERE, timeout=30)
            print("cloud watchdog: restarted cloud monitor")
    except Exception as e:
        print("cloud watchdog failed:", e, file=sys.stderr)

def check_health(state, results, skipped=()):
    """One message if most stores have failed for about an hour, one when it recovers,
    so silence on Telegram always means 'no deals', never 'monitor broken'."""
    if not CONFIG["updates"].get("status_messages"):
        return
    n = len(active_stores())
    bad = len(results) + len(skipped) < n / 2
    state["bad_runs"] = state.get("bad_runs", 0) + 1 if bad else 0
    if state["bad_runs"] == 6:
        notify("Pokemon monitor problem",
               f"Only {len(results)} of {n} stores answered for the last 6 scans. "
               "Deal alerts may be missed until this clears.", channel="status")
        state["warned"] = True
    elif not bad and state.get("warned"):
        notify("Pokemon monitor back to normal", f"{len(results)} of {n} stores answering again.", channel="status")
        state["warned"] = False

def cloud_store_watch(state):
    """Cloud watchdog (Gideon, 2026-10-20: it must work with the Mac closed). Every cloud run checks when each
    cloud shop was last scanned successfully; one message when a shop goes stale, one when it recovers.
    Messages go to the status group."""
    if not IS_CLOUD:
        return
    now = time.time()
    limit = CONFIG["updates"].get("store_stale_minutes", 45) * 60
    last_ok = state.setdefault("last_ok", {})
    first = state.setdefault("watch_first_seen", {})  # grace period for shops the cloud hasn't scanned yet
    open_ = state.setdefault("store_alerts", {})
    blocked = set(CONFIG.get("known_blocked", []))
    went_bad, came_back = [], []
    for st in active_stores():
        n = st["name"]
        if n in blocked or not mine(n):
            continue
        any_ok = (state.get("shop_stats", {}).get(n) or {}).get("last_ok")  # any successful check counts
        last = max(last_ok.get(n) or 0, any_ok or 0) or first.setdefault(n, now)
        if now - last > limit:
            if n not in open_:
                open_[n] = now
                err = (state["stores"].get(n) or {}).get("error") or "no successful scan"
                went_bad.append(f"{n}: no successful scan for {(now - last) / 60:.0f} min ({err[:80]})")
        elif n in open_:
            came_back.append(f"{n} (down since {dt.datetime.fromtimestamp(open_.pop(n)):%d %b %H:%M})")
    if went_bad:
        notify("Store watchdog: shops not being checked", "\n".join(went_bad), channel="status")
    if came_back:
        notify("Store watchdog: shops back", "Being checked again: " + ", ".join(came_back), channel="status")
    print(f"store watchdog: {len(open_)} shop(s) stale")

# ---------------------------------------------------------------- config checks

def validate_config(cfg, env=os.environ, github=None):
    """Problems that would make the monitor silently do the wrong thing. Empty list = fine."""
    github = IS_GITHUB if github is None else github
    errs = []
    stores = cfg.get("stores") or []
    names = [s.get("name") for s in stores]
    if not stores:
        errs.append("config has no stores")
    for dup in sorted({n for n in names if names.count(n) > 1}):
        errs.append(f"store listed twice: {dup}")
    for st in stores:
        n = st.get("name") or "?"
        if st.get("type") not in SCANNERS:
            errs.append(f"{n}: unknown type {st.get('type')!r}")
        if not str(st.get("url", "")).startswith("https://"):
            errs.append(f"{n}: url must start with https://")
        if st.get("currency", "ZAR") not in ("ZAR", *SYMBOL):
            errs.append(f"{n}: unsupported currency {st.get('currency')!r}")
        if st.get("intl_shipping") is not None and not isinstance(st["intl_shipping"], (int, float)):
            errs.append(f"{n}: intl_shipping must be a number in the shop's currency")
    for key in ("local_only_stores", "known_blocked", "cloud_verified"):
        for n in cfg.get(key, []):
            if n not in names:
                errs.append(f"{key} names an unknown store: {n}")
    for n in cfg.get("fast_30th", {}).get("skip", []):
        if n not in names:
            errs.append(f"fast_30th.skip names an unknown store: {n}")
    for row in cfg.get("retail_refs_rand", []) + cfg.get("retail_exact", []):
        try:
            re.compile(row[0])
            assert float(row[1]) > 0
        except Exception:
            errs.append(f"bad retail reference row: {row!r}")
    if not 0 <= cfg.get("retail_tolerance", 0.15) <= 1:
        errs.append("retail_tolerance must be between 0 and 1")
    if cfg.get("fast_30th", {}).get("seconds", 5) < 5:
        errs.append("fast_30th.seconds below 5 would hammer the shops")
    duty = (cfg.get("international") or {}).get("import_duty_pct")
    if duty is not None and not 0 <= duty <= 1:
        errs.append("international.import_duty_pct must be a fraction (0.2 = 20%) or null")
    if cfg.get("local_owner", "mac") not in ("mac", "server"):
        errs.append("local_owner must be 'mac' or 'server'")
    if github and not (env.get("TELEGRAM_TOKEN") and env.get("TELEGRAM_CHAT_ID")):
        errs.append("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID secrets are missing: alerts could not be sent")
    return errs

# ---------------------------------------------------------------- status report

def _median_gap(beats):
    gaps = sorted(b - a for a, b in zip(beats, beats[1:]))
    return gaps[len(gaps) // 2] if gaps else None

def _ago(ts):
    if not ts:
        return "never"
    m = (time.time() - ts) / 60
    return f"{m:.0f} min ago" if m < 120 else f"{m / 60:.1f} h ago"

def _every(sec):
    return "-" if sec is None else f"{sec:.0f} s" if sec < 120 else f"{sec / 60:.0f} min"

def write_status(state):
    """health/status.md (and the GitHub run summary in the cloud): is it running, which shops are checked,
    how often in practice, what was rejected and why, and whether Telegram is getting through."""
    role = "GitHub cloud" if IS_GITHUB else "Oracle server" if IS_SERVER else "Mac"
    with STATE_LOCK:
        shops = json.loads(json.dumps(state.get("shop_stats", {})))
        stats = json.loads(json.dumps(state.get("stats", {})))
        alerts = list(state.get("alert_log", []))[-5:]
        outbox = len(state.get("outbox", [])) + len(OUTBOX)
    rows = []
    for st in CONFIG["stores"]:
        n = st["name"]
        if n in CONFIG.get("known_blocked", []):
            rows.append(f"| {n} | blocked by the shop | - | - | - | - | - |")
            continue
        if n not in {x["name"] for x in active_stores()} or not mine(n):
            continue  # another copy owns this shop
        sh = shops.get(n, {})
        stale = not sh.get("last_ok") or time.time() - sh["last_ok"] > CONFIG["updates"].get("store_stale_minutes", 45) * 60
        rows.append(f"| {n} | {'NOT CHECKED' if stale else 'ok'} | {_ago(sh.get('last_ok'))} | "
                    f"{_every(_median_gap(sh.get('beats_fast', [])))} | {_every(_median_gap(sh.get('beats_quick', [])))} | "
                    f"{_every(_median_gap(sh.get('beats_scan', [])))} | {(sh.get('last_error') or '')[:60] if stale else ''} |")
    c, r = stats.get("counts", {}), stats.get("rejected", {})
    lines = [f"# Pokemon monitor status ({role})", "",
             f"Generated {sast_now():%Y-%m-%d %H:%M} SAST. Counters are for {stats.get('day', 'today')} (SAST).", "",
             f"- Discovered (new listings): {c.get('discovered', 0)}",
             f"- Product-page checks: {c.get('verified_buyable', 0)} buyable, {c.get('verify_not_buyable', 0)} not buyable or failed",
             f"- Alerts sent: {c.get('alerts_sent', 0)}; already alerted, skipped: {r.get('duplicate', 0)}",
             f"- Rejected: " + (", ".join(f"{k} {v}" for k, v in sorted(r.items()) if k != "duplicate") or "none"),
             f"- Telegram this run: {TG_STATS['sent']} sent, {TG_STATS['retried']} retries, {TG_STATS['failed']} failed; "
             f"{outbox} waiting to resend",
             f"- Collector values in use: {len(LAST_VALUES)}", "",
             "| Shop | Status | Last good check | 30th check every | 1-min check every | Full scan every | Last error |",
             "|---|---|---|---|---|---|---|", *rows, ""]
    if alerts:
        lines += ["Recent alerts:", ""] + [
            f"- {dt.datetime.fromtimestamp(a['t']):%d %b %H:%M} {a['store']}: {a['title'][:60]} R{a['price']} "
            f"({a['test']}{', estimate' if a.get('estimated') else ''})" for a in alerts]
    text = "\n".join(lines) + "\n"
    try:
        (HERE / "health").mkdir(exist_ok=True)
        (HERE / "health" / "status.md").write_text(text)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
                f.write(text)
    except OSError as e:
        print("status report not written:", e, file=sys.stderr)

# ---------------------------------------------------------------- main

RUN_START = time.time()

def main():
    args = set(sys.argv[1:])
    problems = validate_config(CONFIG)
    if problems:  # fail loudly (GitHub emails a failed run) instead of looking operational
        for p in problems:
            print(f"::error title=Config problem::{p}" if IS_GITHUB else f"CONFIG PROBLEM: {p}", file=sys.stderr)
        sys.exit(2)
    if "--dry-run" not in args:
        cloud_watchdog()
    if "--test" in args:
        notify("Pokemon monitor connected", "Test alert. You will only get messages about English 30th Anniversary products.", 5)
        print("test sent")
        return

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"items": {}, "stores": {}}
    now = dt.datetime.now().isoformat(timespec="seconds")
    OUTBOX.clear()
    if "--dry-run" not in args:
        flush_outbox(state)
    fast = start_fast(state) if "--dry-run" not in args else (None, None)  # 30th every 5 s, from the start

    # a store's first successful scan only records what it has; announcing all of it would be spam
    known = known_stores(state)
    results, skipped = {}, []
    cool = state.setdefault("cooldown", {})
    last_ok = state.setdefault("last_ok", {})
    todo = []
    for st in active_stores():
        n = st["name"]
        if cool.get(n, 0) > time.time():
            skipped.append(n)  # throttled us recently: leave it alone for a while
        elif (IS_GITHUB and st["type"] == "shopify"
              and time.time() - last_ok.get(n, 0) < CONFIG["shopify_rescan_minutes"] * 60):
            skipped.append(n)  # full Shopify catalogues are heavy; rescan each one every N minutes
        else:
            todo.append(st)
    if IS_GITHUB:
        # Shopify limits each IP across all its shops: a few shops per run, least recently scanned first,
        # so the searches don't land in one burst on top of the 5-second checks
        shop = sorted((st for st in todo if st["type"] == "shopify"), key=lambda st: last_ok.get(st["name"], 0))
        keep = {st["name"] for st in shop[:CONFIG.get("github_shopify_per_run", 5)]}
        skipped += [st["name"] for st in shop if st["name"] not in keep]
        todo = [st for st in todo if st["type"] != "shopify" or st["name"] in keep]
    if IS_GITHUB and CONFIG.get("probe_local_only"):
        probe_local_only()
    SCAN_DEADLINE[0] = time.time() + CONFIG["scan_budget_seconds"]
    ex = cf.ThreadPoolExecutor(max_workers=8)
    futs = {ex.submit(SCANNERS[s["type"]], s["name"], s["url"]): s["name"] for s in todo}
    try:
        for f in cf.as_completed(futs, timeout=CONFIG["scan_budget_seconds"] + 30):
            name = futs[f]
            try:
                results[name] = localize(name, f.result())
                state["stores"][name] = {"ok": True, "count": len(results[name]), "error": None, "at": now}
                last_ok[name] = time.time()
                record_check(state, name, True, "scan")
            except Exception as e:
                prev = state["stores"].get(name, {})
                state["stores"][name] = {"ok": False, "count": prev.get("count", 0),
                                         "error": str(e)[:200], "at": now}
                if "429" in str(e):
                    cool[name] = time.time() + CONFIG["cooldown_minutes"] * 60
                record_check(state, name, False, "scan", e)
                print(f"[{name}] FAILED: {e}", file=sys.stderr)
    except cf.TimeoutError:
        print("scan budget exceeded; unfinished:", [n for f, n in futs.items() if not f.done()], file=sys.stderr)
    ex.shutdown(wait=False, cancel_futures=True)
    SCAN_DEADLINE[0] = float("inf")

    check_health(state, results, skipped)
    cloud_store_watch(state)

    seeding = not state["items"]
    events = []
    for name, found in results.items():
        for it in found:
            merge_item(state, it, now, known, events)

    state["last_run"] = now
    for name, found in results.items():
        print(f"{name:28s} {len(found):4d} TCG products")
    print(f"new / restocked listings: {len(events)}")

    valuer = valuation.Valuer(get_json, state.setdefault("prices", {}), CONFIG["resale"])
    try:
        # current SA listings only: delisted or overseas prices would distort the SA median
        local = {k: v for k, v in state["items"].items() if not v.get("intl") and current(v)}
        deals = valuation.find_deals(local, valuer, CONFIG["resale"], want=is_30th)
    except Exception as e:
        print("valuation failed:", e, file=sys.stderr)
        deals = []
    print(f"price lookups: {valuer.lookups}  valued: {len(deals)}  deals: {sum(d['deal'] for d in deals)}")
    for d in deals[:10]:
        print(f"  {'DEAL' if d['deal'] else '    '} R{d['profit']:>6,.0f} {d['roi']:>5.0%}  {d['store']}: {d['title']}")

    if "--dry-run" in args:
        return
    LAST_DEALS[:] = deals
    LAST_VALUES.clear()
    max_age = CONFIG.get("collector_max_age_hours", 48) * 3600
    for d in deals:  # a collector value older than collector_max_age_hours is too stale to call a bargain
        if d.get("value_asof") and time.time() - d["value_asof"] <= max_age:
            LAST_VALUES[d["key"]] = (d["value"], f"lower of TCGplayer recent sales ~R{d['us_zar']:,.0f} and SA median "
                                                 f"R{d['sa_median']:,.0f} across {d['sa_n']} shops, "
                                                 f"{dt.datetime.fromtimestamp(d['value_asof']):%d %b}")
    if not seeding:
        # every buyable wanted listing is evaluated each full scan; the repeat rule stops repeats, so this
        # also catches listings that only now qualify (new collector value, price drop, pre-order opened)
        with STATE_LOCK:
            cands = [dict(i) for i in state["items"].values()
                     if i.get("in_stock") and current(i) and mine(i["store"]) and i["store"] in known and is_30th(i["title"])]
        # The old code alerted only on changes, so listings already buyable at a good price were never
        # announced. The first sweep under this policy sends them as ONE summary, not a burst of messages.
        first_sweep = not state.get("alert_policy", 0) >= 2
        if first_sweep:
            DIGEST[:] = [True]
        try:
            sent = sum(consider(state, it, "scan") for it in cands)
        finally:
            found, DIGEST[:] = DIGEST[1:], []
        if first_sweep:
            send_digest(found)
            state["alert_policy"] = 2
        print(f"full-scan candidates: {len(cands)}, alerts {'in summary' if first_sweep else 'sent'}: {sent}")
    update_watchlist(deals, state)
    heartbeat(state, results, deals)
    until = next((float(a.split("=", 1)[1]) for a in args if a.startswith("--watch-until=")), 0)
    watch_for = next((float(a.split("=", 1)[1]) for a in args if a.startswith("--watch-for=")), 0)
    if watch_for:  # Mac: watch until this many seconds after the run started
        until = RUN_START + watch_for
    if until:
        save_state(state)  # once before the long watch
        watch_loop(state, until)
    stop_fast(*fast)
    scan_report(state)
    save_state(state)
    write_status(state)
    write_dashboard(state, deals)
    if IS_GITHUB:
        sys.stdout.flush()
        os._exit(0)

def run_forever():
    """Server mode: a full scan every full_scan_seconds (5 minutes), quick scans and stock checks in between."""
    while True:
        start = time.time()
        sys.argv = [sys.argv[0], f"--watch-until={start + CONFIG['full_scan_seconds']}"]
        try:
            main()
        except Exception as e:
            print("cycle failed:", repr(e), file=sys.stderr)
            time.sleep(60)
        sys.stdout.flush()

if __name__ == "__main__":
    if "--loop" in sys.argv:
        run_forever()
    else:
        main()
