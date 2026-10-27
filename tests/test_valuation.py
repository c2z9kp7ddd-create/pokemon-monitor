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


def c(name, market=50.0, pid=0):
    return {"name": name, "market": market, "lowest": market, "id": pid, "url": "t"}


class Matching(unittest.TestCase):
    """Real TCGplayer names and SA shop titles, 2026-10-27."""
    def test_set_numbers_and_box_pack_counts_do_not_block(self):
        self.assertEqual(v.match("Pokemon TCG : Scarlet & Violet 10 - Destined Rivals Elite Trainer Box",
                                 [c("Destined Rivals Elite Trainer Box")])["name"], "Destined Rivals Elite Trainer Box")
        self.assertIsNotNone(v.match("Pokemon: Lost Origin Booster Box (36 packs)", [c("Lost Origin Booster Box")]))
        self.assertIsNotNone(v.match("Pokemon TCG 30th Celebration - Elite Trainer Box - (max 2 per customer)",
                                     [c("30th Celebration Elite Trainer Box")]))
        self.assertIsNotNone(v.match("Pokemon 30th anniversary Elite Trainer Box", [c("30th Celebration Elite Trainer Box")]))

    def test_pack_counts_still_matter(self):
        self.assertIsNone(v.match("30th Celebration 2-Pack Blister", [c("30th Celebration 3 Pack Blister")]))
        self.assertIsNone(v.match("Lost Origin Elite Trainer Box", [c("Lost Origin Pokemon Center Elite Trainer Box (Exclusive)")]))

    def test_named_artwork_gets_that_artwork(self):
        m = v.match("Pokemon TCG 30th Celebration - ex Tin Greninja",
                    [c("30th Celebration ex Tin [Sylveon ex] (Retail Version)", 62.61),
                     c("30th Celebration ex Tin [Greninja ex] (Retail Version)", 65.48),
                     c("30th Celebration ex Tin [Greninja ex] (International Version)", 90)])
        self.assertEqual(m["market"], 65.48)

    def test_no_artwork_named_takes_the_cheapest_artwork(self):
        m = v.match("Pokemon: 30th Celebration Mini Tin",
                    [c("30th Celebration Mini Tin [Mew]", 28.61), c("30th Celebration Mini Tin [Moltres]", 22.63),
                     c("30th Celebration Mini Tin Display", 234.15), c("30th Celebration Mini Tin [Set of 2]", 40)])
        self.assertEqual(m["market"], 22.63)
        self.assertIn("any artwork", m["name"])

    def test_artwork_family_not_used_when_listing_names_an_unseen_artwork(self):
        self.assertIsNone(v.match("30th Celebration Mini Tin Pikachu", [c("30th Celebration Mini Tin [Mew]")]))

    def test_a_name_with_no_set_words_never_matches(self):
        # 'Mega Evolution Booster Box' is just 'booster box' after stop words: it used to fit any booster box
        self.assertIsNone(v.match("Pokemon TCG: Lost Origin Booster Box", [c("Mega Evolution Booster Box")]))
        self.assertIsNone(v.match("Pokemon: Mega Evolution Booster Box", [c("Mega Evolution Booster Box")]))
