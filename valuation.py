"""Resale valuation for SA listings.

Two independent price sources per product:
  1. TCGplayer market price (recent US sales), converted to rand.
  2. Median asking price of the same product at the other SA stores we scan.
A listing only counts as a deal when BOTH say it resells for well above the buy
price after selling fees and postage. Estimates, not guarantees.
"""
import json
import re
import statistics
import time
import urllib.parse

TCG_SEARCH = "https://mp-search-api.tcgplayer.com/v1/search/request?"
FX_URL = "https://open.er-api.com/v6/latest/USD"

STOP = set("""pokemon tcg the and of a an pre order preorder trading card cards game english eng sealed new
max per customer customers only for random one at artwork assorted random bots orders using uding will be
cancelled wave item items stock sv me mega evolution scarlet violet sword shield""".split())
# words that change what the product physically is; must agree between listing and match
KIND = set("""case display center half mini jumbo box pack blister bundle tin tins collection deck decks binder
poster sticker figure premium ultra super special build battle stadium chest lunch booster trainer elite
upc etb 2 3 6 8 10 18 36""".split())
FOREIGN = ("japanese", "chinese", "korean", "thai", "indonesian", "simplified", "traditional", "jpn", "(jp)")
ALIASES = [(r"\betb\b", "elite trainer box"), (r"\bupc\b", "ultra premium collection"),
           (r"ultra-premium", "ultra premium"), (r"pok[eé]mon center", "center"),
           (r"(\d)-pack", r"\1 pack"), (r"\btins\b", "tin")]


def tokens(title):
    t = title.lower()
    for pat, rep in ALIASES:
        t = re.sub(pat, rep, t)
    return [w for w in re.findall(r"[a-z0-9]+", t) if w not in STOP]


def is_foreign(title):
    t = title.lower()
    return any(f in t for f in FOREIGN)


def match(title, candidates):
    """Best TCGplayer candidate whose meaningful words all appear in the listing title
    and whose product-type words are identical. None if nothing is safe."""
    lt = set(tokens(title))
    best = None
    for c in candidates:
        ct = set(tokens(c["name"]))
        if not ct or not ct <= lt:
            continue
        if (lt & KIND) != (ct & KIND):
            continue
        if best is None or len(ct) > len(set(tokens(best["name"]))):
            best = c
    return best


class Valuer:
    def __init__(self, get_json, cache, cfg):
        self.get_json, self.cache, self.cfg = get_json, cache, cfg
        self.lookups = 0
        self._fx = None

    def fx(self):
        if self._fx is None:
            c = self.cache.get("_fx")
            if c and time.time() - c["t"] < 6 * 3600:
                self._fx = c["rate"]
            else:
                self._fx = self.get_json(FX_URL)["rates"]["ZAR"]
                self.cache["_fx"] = {"t": time.time(), "rate": self._fx}
        return self._fx

    def search(self, query):
        body = {"algorithm": "sales_dismax", "from": 0, "size": 12,
                "filters": {"term": {"productLineName": ["pokemon"], "productTypeName": ["Sealed Products"]},
                            "range": {}, "match": {}},
                "listingSearch": {"context": {"cart": {}}, "filters": {"term": {"sellerStatus": "Live", "channelId": 0},
                                  "range": {"quantity": {"gte": 1}}, "exclude": {"channelExclusion": 0}}},
                "context": {"cart": {}, "shippingCountry": "US", "userProfile": {}},
                "settings": {"useFuzzySearch": True, "didYouMean": {}}, "sort": {}}
        url = TCG_SEARCH + urllib.parse.urlencode({"q": query, "isList": "false"})
        d = self.get_json(url, json.dumps(body).encode())
        return [{"name": r.get("productName", ""), "id": int(r.get("productId") or 0),
                 "market": r.get("marketPrice"), "lowest": r.get("lowestPrice"),
                 "url": f"https://www.tcgplayer.com/product/{int(r.get('productId') or 0)}"}
                for r in d["results"][0]["results"]]

    def tcg_for(self, title):
        """Cached TCGplayer match for a listing title, or None. Refreshes daily."""
        if is_foreign(title):
            return None
        q = " ".join(tokens(title))
        c = self.cache.get(q)
        if c and time.time() - c["t"] < self.cfg["price_refresh_hours"] * 3600:
            return c["m"]
        if self.lookups >= self.cfg["max_lookups_per_run"]:
            return c["m"] if c else None
        self.lookups += 1
        try:
            m = match(title, self.search(q))
        except Exception:
            return c["m"] if c else None
        time.sleep(0.3)
        self.cache[q] = {"t": time.time(), "m": m}
        return m


def find_deals(items, valuer, cfg):
    """items: dict key -> listing. Returns every in-stock listing that both sources could value,
    best first; the ones that clear the thresholds have deal=True."""
    # attach TCGplayer matches; in-stock listings first so the lookup budget goes where it matters
    ordered = sorted(items.values(), key=lambda i: not i["in_stock"])
    for it in ordered:
        it["tcg"] = valuer.tcg_for(it["title"])

    by_product = {}
    for it in items.values():
        if it.get("tcg") and it["price"]:
            by_product.setdefault(it["tcg"]["id"], []).append(it)

    fx = valuer.fx()
    deals = []
    for it in items.values():
        m = it.get("tcg")
        if not (it["in_stock"] and m and it["price"] and m.get("market")):
            continue
        market, lowest = m["market"], m.get("lowest")
        # stale or thin market price: live listings must be near the sales price
        if market < cfg["min_market_usd"] or not lowest or not (0.8 * market <= lowest <= 1.5 * market):
            continue
        us_zar = market * fx
        others = [o["price"] for o in by_product.get(m["id"], []) if o["store"] != it["store"]]
        if len(others) < cfg["min_other_sa_stores"]:
            continue
        sa_med = statistics.median(others)
        value = min(us_zar, sa_med)  # the more pessimistic source wins
        net = value * (1 - cfg["sell_fee_pct"]) - cfg["sell_shipping_rand"]
        profit = net - it["price"] - cfg["buy_shipping_rand"]
        roi = profit / (it["price"] + cfg["buy_shipping_rand"])
        deals.append({"deal": profit >= cfg["min_profit_rand"] and roi >= cfg["min_roi"],"key": it["key"], "store": it["store"], "title": it["title"], "url": it["url"],
                          "price": it["price"], "us_zar": round(us_zar), "sa_median": round(sa_med),
                          "sa_n": len(others), "value": round(value), "profit": round(profit),
                          "roi": round(roi, 3), "tcg_name": m["name"], "tcg_url": m["url"]})
    deals.sort(key=lambda d: -d["profit"])
    return deals
