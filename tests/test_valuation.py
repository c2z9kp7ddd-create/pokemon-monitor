"""Collector value: conservative, but no longer thrown away by one cheap or junk listing."""
import unittest

import valuation as v


class USValue(unittest.TestCase):
    def test_normal_market(self):
        self.assertEqual(v.us_sales_value(100, 95)[0], 100)

    def test_cheap_or_junk_listing_caps_at_80_percent(self):
        self.assertEqual(v.us_sales_value(68.09, 47.5)[0], 0.8 * 68.09)   # Chaos Rising ETB, 2026-10-23
        self.assertEqual(v.us_sales_value(71.42, 1.0)[0], 0.8 * 71.42)    # Perfect Order ETB: a $1 listing
        self.assertEqual(v.us_sales_value(100, None)[0], 80)

    def test_listings_above_market_keep_the_lower_market(self):
        self.assertEqual(v.us_sales_value(100, 180)[0], 100)


class FakeValuer:
    def __init__(self, matches):
        self.matches, self.asof = matches, {}

    def tcg_for(self, title):
        return self.matches.get(title)

    def fx(self):
        return 20.0


CFG = {"min_market_usd": 10, "min_other_sa_stores": 2, "us_only_factor": 0.85, "sell_fee_pct": 0.12,
       "sell_shipping_rand": 120, "buy_shipping_rand": 100, "min_profit_rand": 300, "min_roi": 0.25}


def listing(store, price, title="Chaos Rising Elite Trainer Box"):
    return {"key": f"{store}|1", "store": store, "title": title, "url": "u", "price": price, "in_stock": True}


class FindDeals(unittest.TestCase):
    match = {"Chaos Rising Elite Trainer Box": {"id": 1, "name": "Chaos Rising Elite Trainer Box", "url": "t",
                                                "market": 68.09, "lowest": 47.5}}

    def test_value_kept_despite_a_cheap_tcgplayer_listing(self):
        items = {f"S{i}": listing(f"S{i}", p) for i, p in enumerate([999, 1299, 1299])}
        deals = {d["store"]: d for d in v.find_deals(items, FakeValuer(self.match), CFG)}
        self.assertIn("S0", deals)
        self.assertAlmostEqual(deals["S0"]["value"], min(0.8 * 68.09 * 20, 1299), places=0)  # R1,089

    def test_single_shop_uses_us_sales_with_a_margin(self):
        items = {"S0": listing("S0", 900)}
        d = v.find_deals(items, FakeValuer(self.match), CFG)[0]
        self.assertIsNone(d["sa_median"])
        self.assertAlmostEqual(d["value"], 0.8 * 68.09 * 20 * 0.85, places=0)
        self.assertIn("US sales only", d["value_basis"])


if __name__ == "__main__":
    unittest.main()
