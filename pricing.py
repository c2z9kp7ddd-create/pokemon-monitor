"""Price decisions: is a wanted, buyable listing a good enough deal to alert on?

Pure functions (no network, no globals), so every threshold is unit-tested in tests/test_pricing.py.

Business rules (Gideon, 2026-10-07 / 2026-10-08, authoritative):
  * 30th Anniversary and Delta Reign listings pass when the comparable price is at most `retail_tolerance`
    (15%) above the normal SA shelf price for that product, OR below its collector value.
  * Any other English booster box or ETB passes only when the comparable price is below collector value.
  * No price, or nothing reliable to compare against, never passes: an alert must not claim a bargain
    the data cannot support.

Comparable price = what is realistically payable for the item. For South African shops that is the
listed price (domestic delivery is excluded: it varies by order size and is often free over a threshold).
For overseas shops it is the landed cost: item + international shipping + SA import duty + 15% import
VAT, where VAT is charged on (customs value x 1.10 + duty) as SARS does. Overseas shipping and duty are
not published for these shops, so unless config supplies them the landed cost is only a lower bound and
the listing cannot pass (it is still rejected outright when even the lower bound fails the test).
"""
from dataclasses import dataclass, field
import re

import valuation

SA_IMPORT_VAT = 0.15      # SARS import VAT rate
SA_VAT_UPLIFT = 0.10      # SARS adds 10% to the customs value before VAT (freight/insurance proxy)


@dataclass
class Landed:
    total: float | None   # full landed cost in rand, None when shipping or duty is unknown
    lower_bound: float    # cost with only the charges that are certain (item + VAT on item)
    confirmed: bool       # True when every part is known
    parts: dict = field(default_factory=dict)
    note: str = ""


def landed_cost(price_zar, store, intl_cfg, fx):
    """price_zar: item price already converted to rand. store: the shop's config entry.
    intl_cfg: config["international"] (import_duty_pct may be None = unknown).
    fx: rand per 1 unit of the shop's currency (for its shipping estimate)."""
    cur = store.get("currency", "ZAR")
    if cur == "ZAR":
        return Landed(price_zar, price_zar, True, {"item": price_zar}, "")
    ship_native = store.get("intl_shipping")          # in the shop's currency; None = not published
    duty_pct = (intl_cfg or {}).get("import_duty_pct")  # None = not confirmed
    vat_on_item = SA_IMPORT_VAT * price_zar * (1 + SA_VAT_UPLIFT)
    lower = price_zar + vat_on_item
    if ship_native is None or duty_pct is None:
        missing = [n for n, v in (("shipping", ship_native), ("import duty", duty_pct)) if v is None]
        return Landed(None, round(lower, 2), False, {"item": price_zar, "vat_min": round(vat_on_item, 2)},
                      f"{' and '.join(missing)} not known; at least R{lower:,.0f} incl. 15% import VAT")
    ship = ship_native * fx
    duty = duty_pct * price_zar                       # SARS charges duty on the FOB (item) value
    vat = SA_IMPORT_VAT * (price_zar * (1 + SA_VAT_UPLIFT) + duty)
    handling = (intl_cfg or {}).get("clearance_fee_zar", 0) or 0
    total = price_zar + ship + duty + vat + handling
    return Landed(round(total, 2), round(lower, 2), True,
                  {"item": price_zar, "shipping": round(ship, 2), "duty": round(duty, 2),
                   "vat": round(vat, 2), "clearance": handling},
                  "estimated landed cost (shipping and duty from config)")


def official_reference(title, official):
    """(rand, source) from the official SA store's own listing of this exact product, or (None, None).
    official: [(title, rand, store)] of listings at the stores in config official_retail_stores.
    Same safety rule as the collector match: every word of the official name is in the listing title and
    the product-type words are identical; the most specific official name wins."""
    lt = set(valuation.tokens(title)) - valuation.DECOR
    best = None
    for name, rand, store in official or []:
        ot = set(valuation.tokens(name)) - valuation.DECOR
        if not ot or not (ot - valuation.KIND) or not ot <= lt or (lt & valuation.KIND) != (ot & valuation.KIND):
            continue
        if best is None or len(ot) > best[0]:
            best = (len(ot), rand, f"official price at {store} (R{rand:,.0f}, {name})")
    return (best[1], best[2]) if best else (None, None)


def retail_reference(title, cfg, official=None):
    """(rand, kind, source) for the normal SA shelf price, or (None, None, None).
    retail_exact entries name one specific product and win; then the official SA store's own price for this
    product (learned from its listings); then the per-product-type estimates."""
    t = title.lower()
    for pattern, rand, source in cfg.get("retail_exact", []):
        if re.search(pattern, t):
            return rand, "exact", source
    rand, source = official_reference(title, official)
    if rand:
        return rand, "exact", source
    for pattern, rand in cfg.get("retail_refs_rand", []):
        if re.search(pattern, t):
            return rand, "type", "typical SA shelf price for this product type (config retail_refs_rand)"
    return None, None, None


@dataclass
class Decision:
    qualifies: bool
    reason: str                 # why it qualifies, or the rejection reason (short, logged and counted)
    code: str                   # machine-readable: retail, collector, no_price, overpriced, no_reference, landed_unknown
    comparable: float | None = None
    retail: float | None = None
    retail_kind: str | None = None
    retail_source: str | None = None
    collector: float | None = None
    collector_source: str | None = None
    landed: Landed | None = None
    saving_vs_retail: float | None = None      # fraction, negative = premium
    saving_vs_collector: float | None = None
    estimated: bool = True


def decide(label, title, price_zar, store, cfg, fx=1.0, value=None, value_source=None, official=None):
    """label: matching.label() result (30TH ANNIVERSARY, DELTA REIGN, BOOSTER BOX, ETB).
    value: collector value in rand from recent sales, or None when unknown/stale.
    official: [(title, rand, store)] official SA store listings, see official_reference."""
    if price_zar is None or price_zar <= 0:
        return Decision(False, "no price shown", "no_price")
    landed = landed_cost(price_zar, store, cfg.get("international"), fx)
    comparable = landed.total if landed.confirmed else landed.lower_bound
    tol = cfg.get("retail_tolerance", 0.15)
    ref, kind, source = (None, None, None)
    collector_only = label in ("BOOSTER BOX", "ETB")
    if not collector_only:
        ref, kind, source = retail_reference(title, cfg, official)
    d = Decision(False, "", "", comparable=round(comparable, 2), retail=ref, retail_kind=kind, retail_source=source,
                 collector=value, collector_source=value_source if value else None, landed=landed)
    if ref:
        d.saving_vs_retail = round(1 - comparable / ref, 4)
    if value:
        d.saving_vs_collector = round(1 - comparable / value, 4)
    passes_retail = bool(ref) and comparable <= ref * (1 + tol) + 1e-9
    passes_collector = bool(value) and comparable < value
    if not ref and not value:
        d.reason, d.code = ("no collector value to compare" if collector_only
                            else "no retail or collector price to compare"), "no_reference"
        return d
    if not (passes_retail or passes_collector):
        what = "collector value" if collector_only else "retail +15% and collector value"
        d.reason, d.code = f"R{comparable:,.0f}{'+' if not landed.confirmed else ''} is above {what}", "overpriced"
        return d
    if not landed.confirmed:  # would pass on the minimum cost, but the real cost is unknown
        d.reason, d.code = f"landed cost unconfirmed ({landed.note})", "landed_unknown"
        return d
    d.qualifies = True
    d.estimated = kind != "exact" or not landed.confirmed
    if passes_retail:
        d.code = "retail"
        d.reason = (f"at retail: R{comparable:,.0f} vs SA shelf ~R{ref:,.0f} "
                    f"({'exact product' if kind == 'exact' else 'estimate for this product type'})")
    else:
        d.code = "collector"
        d.reason = f"below collector value: R{comparable:,.0f} vs ~R{value:,.0f} ({value_source or 'recent sales'})"
    return d
