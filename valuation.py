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
MATCH_VERSION = 2  # bump when match() changes so cached misses are retried

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
           (r"\btwo\b", "2"), (r"\bthree\b", "3"),
           (r"(\d)-pack", r"\1 pack"), (r"\btins\b", "tin"),
           # set numbers shops put before the set name ("Scarlet & Violet 10 - Destined Rivals", "SV10.5:")
           # read as product-type numbers and blocked every match (2026-10-27)
           (r"\b(?:scarlet\W*(?:&|and)\W*violet|sword\W*(?:&|and)\W*shield|sv|swsh|sm|me)\W*\d{1,2}(?:\.\d)?\b(?!\W*packs?\b)", " "),
           (r"booster display", "booster box"),
           (r"(booster box)\W*\(?36\W*(?:booster\W*)?packs?\)?", r"\1"),
           (r"\bjourneying\b", "journey"), (r"30th anniversary", "30th celebration"),
           (r"\bmega evolution(?=[a-rt-z])", "mega evolution "),  # "Mega EvolutionChaos Rising"
           (r"elite trainers?(?! box)", "elite trainer box"),
           # purchase limits are not pack counts: "(max 2 per customer)", "Limited 1 per customer"
           (r"\b(?:max(?:imum)?|limit(?:ed)?(?: to)?)\W*\d+\W*(?:per\W*(?:customer|order|person|household))?", " "),
           (r"\b\d+\W*per\W*(?:customer|order|person|household)\b", " "),
           (r"\b1\W*random\W*(?:tin|pack|box|artwork)?\b", " ")]
# TCGplayer name decorations that do not change the product
DECOR = {"exclusive", "retail", "version", "ex"}  # 'Battle Deck (Umbreon)' is TCGplayer's '[Umbreon ex]'


def tokens(title):
    t = title.lower()
    for pat, rep in ALIASES:
        t = re.sub(pat, rep, t)
    return [w for w in re.findall(r"[a-z0-9]+", t) if w not in STOP]


def is_foreign(title):
    t = title.lower()
    return any(f in t for f in FOREIGN)


def _variant(name):
    """(name without its artwork bracket, artwork tokens). '30th Celebration Mini Tin [Mew]' ->
    ('30th Celebration Mini Tin', {'mew'}). '[Set of 2]' is a different product, not an artwork."""
    m = re.search(r"\[([^\]]+)\]", name)
    if not m or re.search(r"\bset of\b|\d", m.group(1), re.I):
        return name, set()
    return re.sub(r"\s+", " ", name[:m.start()] + name[m.end():]).strip(), set(tokens(m.group(1))) - DECOR


def _identity(toks):
    """A name must keep at least one word that is not a product-type word; 'Mega Evolution Booster Box'
    tokenizes to just 'booster box' and would otherwise match every booster box listing."""
    return bool(set(toks) - KIND)


def match(title, candidates):
    """Best TCGplayer candidate whose meaningful words all appear in the listing title
    and whose product-type words are identical. None if nothing is safe.
    TCGplayer lists each artwork separately ('Paradox Rift Elite Trainer Box [Iron Valiant]'); a listing that
    names no artwork gets the cheapest artwork of that product, so the value stays conservative."""
    lt = set(tokens(title)) - DECOR
    best, best_len = None, -1
    families = {}
    for c in candidates:
        ct = set(tokens(c["name"])) - DECOR
        if not ct or not _identity(ct) or (lt & KIND) != (ct & KIND):
            continue
        if ct <= lt:
            if len(ct) > best_len:
                best, best_len = c, len(ct)
            continue
        base, art = _variant(c["name"])
        bt = set(tokens(base)) - DECOR
        if art and bt and bt <= lt and not (art & lt) and c.get("market"):
            families.setdefault(frozenset(bt), []).append(c)
    if best is not None:
        return best
    # artwork families: only when the listing has nothing beyond the family's own words (no artwork it
    # names that TCGplayer's results did not show)
    for bt, group in sorted(families.items(), key=lambda kv: -len(kv[0])):
        if lt - bt:
            continue
        low = min(group, key=lambda c: c["market"])
        return dict(low, name=f"{_variant(low['name'])[0]} (any artwork: cheapest, {low['name']})")
    return None


class Valuer:
    def __init__(self, get_json, cache, cfg):
        self.get_json, self.cache, self.cfg = get_json, cache, cfg
        self.lookups = 0
        self._fx = None
        self.asof = {}  # listing title -> when its TCGplayer price was fetched (epoch)
        # a slow TCGplayer once stalled the Mac run before it saved, so the cache never grew and every run
        # redid every lookup until the watchdog killed it (2026-10-12): stop looking up after this long
        self.deadline = time.time() + cfg.get("lookup_budget_seconds", 90)

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
        d = self.get_json(url, json.dumps(body).encode(), timeout=15, tries=2)
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
        if c and not c["m"] and c.get("v") != MATCH_VERSION:
            c = None  # a 'no match' from an older matcher: look again
        if c and time.time() - c["t"] < self.cfg["price_refresh_hours"] * 3600:
            self.asof[title] = c["t"]
            return c["m"]
        if self.lookups >= self.cfg["max_lookups_per_run"] or time.time() > self.deadline:
            if c:
                self.asof[title] = c["t"]  # older than the refresh window: the caller decides if it is too stale
            return c["m"] if c else None
        self.lookups += 1
        try:
            m = match(title, self.search(q))
            q2 = " ".join(w for w in q.split() if w not in DECOR)
            if m is None and q2 != q and self.lookups < self.cfg["max_lookups_per_run"]:
                # TCGplayer's search drops '30th Celebration Battle Deck [Umbreon ex]' for '... umbreon ex'
                self.lookups += 1
                time.sleep(0.3)
                m = match(title, self.search(q2))
        except Exception:
            if c:
                self.asof[title] = c["t"]
            return c["m"] if c else None
        time.sleep(0.3)
        self.cache[q] = {"t": time.time(), "m": m, "v": MATCH_VERSION}
        self.asof[title] = self.cache[q]["t"]
        return m


def us_sales_value(market, lowest):
    """US collector value in dollars from TCGplayer: its market price (recent completed sales), made
    conservative when live listings disagree. A cheap or junk listing (a $1 placeholder, one undercutting
    seller) used to throw the whole value away; now the value is capped at 80% of market instead.
    Returns (dollars, basis text)."""
    if lowest and lowest < 0.8 * market:
        return 0.8 * market, "TCGplayer market capped at 80% (cheaper live listings exist)"
    if not lowest:
        return 0.8 * market, "TCGplayer market capped at 80% (no live listings)"
    return market, "TCGplayer market (recent sales)"

def find_deals(items, valuer, cfg, want=None):
    """items: dict key -> listing. Returns every listing that both sources could value, best first.
    profitable = clears the thresholds at the listed price; deal = profitable AND buyable now.
    Profitable-but-sold-out listings feed the fast restock watch."""
    # attach TCGplayer matches; in-stock listings first so the lookup budget goes where it matters
    # want: optional title filter; only those listings are looked up and valued
    ordered = sorted((i for i in items.values() if want is None or want(i["title"])), key=lambda i: not i["in_stock"])
    for it in ordered:
        it["tcg"] = valuer.tcg_for(it["title"])

    by_product = {}
    for it in items.values():
        if it.get("tcg") and it["price"]:
            by_product.setdefault(it["tcg"]["id"], []).append(it)

    fx = valuer.fx()
    deals = []
    for it in ordered:
        m = it.get("tcg")
        if not (m and it["price"] and m.get("market")):
            continue
        market, lowest = m["market"], m.get("lowest")
        if market < cfg["min_market_usd"]:
            continue
        us_usd, basis = us_sales_value(market, lowest)
        us_zar = us_usd * fx
        others = [o["price"] for o in by_product.get(m["id"], []) if o["store"] != it["store"]]
        if len(others) >= cfg["min_other_sa_stores"]:
            sa_med = statistics.median(others)
            value = min(us_zar, sa_med)  # the more pessimistic source wins
        else:  # not enough SA shops stock it: US sales alone, with a margin for the SA market
            sa_med = None
            value = us_zar * cfg.get("us_only_factor", 0.85)
            basis += f", US sales only x{cfg.get('us_only_factor', 0.85)} (fewer than {cfg['min_other_sa_stores']} other SA shops)"
        net = value * (1 - cfg["sell_fee_pct"]) - cfg["sell_shipping_rand"]
        profit = net - it["price"] - cfg["buy_shipping_rand"]
        roi = profit / (it["price"] + cfg["buy_shipping_rand"])
        profitable = profit >= cfg["min_profit_rand"] and roi >= cfg["min_roi"]
        deals.append({"deal": profitable and it["in_stock"], "profitable": profitable,
                      "in_stock": it["in_stock"], "key": it["key"], "store": it["store"], "title": it["title"], "url": it["url"],
                          "price": it["price"], "us_zar": round(us_zar), "sa_median": round(sa_med) if sa_med else None,
                          "value_basis": basis,
                          "sa_n": len(others), "value": round(value), "profit": round(profit),
                          "roi": round(roi, 3), "tcg_name": m["name"], "tcg_url": m["url"],
                          "value_asof": valuer.asof.get(it["title"])})
    deals.sort(key=lambda d: -d["profit"])
    return deals
