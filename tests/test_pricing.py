import unittest

import pricing

CFG = {"retail_tolerance": 0.15,
       "retail_refs_rand": [["pokemon center elite trainer|pokemon center etb", 2000], ["elite trainer|\\betb\\b", 1300],
                            ["booster bundle", 699]],
       "retail_exact": [],
       "international": {"import_duty_pct": None, "clearance_fee_zar": 0}}
SA = {"name": "Takealot"}
UK = {"name": "Zatu (UK)", "currency": "GBP"}
ETB30 = "pokemon tcg 30th celebration elite trainer box"


class RetailTest(unittest.TestCase):
    def test_exact_15_percent_boundary(self):
        self.assertTrue(pricing.decide("30TH ANNIVERSARY", ETB30, 1495.00, SA, CFG).qualifies)   # 1300 x 1.15
        d = pricing.decide("30TH ANNIVERSARY", ETB30, 1495.01, SA, CFG)
        self.assertFalse(d.qualifies)
        self.assertEqual(d.code, "overpriced")

    def test_saving_and_premium(self):
        d = pricing.decide("30TH ANNIVERSARY", ETB30, 1170, SA, CFG)
        self.assertEqual(d.code, "retail")
        self.assertAlmostEqual(d.saving_vs_retail, 0.10)
        d = pricing.decide("30TH ANNIVERSARY", ETB30, 1430, SA, CFG)
        self.assertAlmostEqual(d.saving_vs_retail, -0.10)  # 10% premium, still within tolerance

    def test_product_specific_reference_wins_and_is_not_marked_estimate(self):
        cfg = dict(CFG, retail_exact=[["30th celebration elite trainer", 1199, "test"]])
        d = pricing.decide("30TH ANNIVERSARY", ETB30, 1380, SA, cfg)   # 1199 x 1.15 = 1378.85
        self.assertFalse(d.qualifies)
        d = pricing.decide("30TH ANNIVERSARY", ETB30, 1378, SA, cfg)
        self.assertTrue(d.qualifies)
        self.assertEqual(d.retail_kind, "exact")
        self.assertFalse(d.estimated)

    def test_pokemon_center_etb_uses_its_own_reference(self):
        t = "pokemon 30th celebration pokemon center elite trainer box"
        self.assertTrue(pricing.decide("30TH ANNIVERSARY", t, 2300, SA, CFG).qualifies)    # 2000 x 1.15
        self.assertFalse(pricing.decide("30TH ANNIVERSARY", t, 2301, SA, CFG).qualifies)


class CollectorTest(unittest.TestCase):
    def test_boundary_is_strictly_below(self):
        title = "pokemon surging sparks booster box"
        self.assertFalse(pricing.decide("BOOSTER BOX", title, 3000, SA, CFG, value=3000).qualifies)
        d = pricing.decide("BOOSTER BOX", title, 2999.99, SA, CFG, value=3000, value_source="test")
        self.assertTrue(d.qualifies)
        self.assertEqual(d.code, "collector")

    def test_boxes_and_etbs_never_use_the_retail_test(self):
        d = pricing.decide("ETB", "pokemon prismatic evolutions elite trainer box", 900, SA, CFG)
        self.assertFalse(d.qualifies)          # cheap, but no collector value: can't call it a deal
        self.assertEqual(d.code, "no_reference")
        self.assertIsNone(d.retail)

    def test_30th_passes_on_collector_value_when_above_retail(self):
        d = pricing.decide("30TH ANNIVERSARY", ETB30, 2000, SA, CFG, value=2500, value_source="x")
        self.assertTrue(d.qualifies)
        self.assertEqual(d.code, "collector")


class NoDataNoBargain(unittest.TestCase):
    def test_no_price(self):
        self.assertEqual(pricing.decide("30TH ANNIVERSARY", ETB30, None, SA, CFG).code, "no_price")
        self.assertEqual(pricing.decide("30TH ANNIVERSARY", ETB30, 0, SA, CFG).code, "no_price")

    def test_no_reference_at_all(self):
        d = pricing.decide("30TH ANNIVERSARY", "pokemon 30th celebration mystery thing", 100, SA, CFG)
        self.assertFalse(d.qualifies)
        self.assertEqual(d.code, "no_reference")


class Landed(unittest.TestCase):
    def test_domestic_is_the_listed_price(self):
        l = pricing.landed_cost(1299, SA, CFG["international"], 1)
        self.assertTrue(l.confirmed)
        self.assertEqual(l.total, 1299)

    def test_overseas_lower_bound_is_item_plus_vat_on_uplifted_value(self):
        l = pricing.landed_cost(1000, UK, CFG["international"], 22.0)
        self.assertFalse(l.confirmed)
        self.assertIsNone(l.total)
        self.assertAlmostEqual(l.lower_bound, 1000 + 0.15 * 1100)   # R1,165

    def test_overseas_full_landed_cost(self):
        cfg = {"import_duty_pct": 0.20, "clearance_fee_zar": 50}
        l = pricing.landed_cost(1000, dict(UK, intl_shipping=10), cfg, 22.0)
        self.assertTrue(l.confirmed)
        # item 1000 + shipping 220 + duty 200 + VAT 0.15 x (1100 + 200) = 195 + clearance 50
        self.assertAlmostEqual(l.total, 1000 + 220 + 200 + 195 + 50)

    def test_overseas_rejected_when_even_the_minimum_is_too_high(self):
        d = pricing.decide("30TH ANNIVERSARY", ETB30, 1300, UK, CFG, fx=22.0)   # min 1300 + 214.5 > 1495
        self.assertEqual(d.code, "overpriced")

    def test_overseas_held_back_when_only_the_minimum_passes(self):
        d = pricing.decide("30TH ANNIVERSARY", ETB30, 1100, UK, CFG, fx=22.0)   # min 1281.5 <= 1495
        self.assertFalse(d.qualifies)
        self.assertEqual(d.code, "landed_unknown")

    def test_overseas_passes_once_costs_are_confirmed(self):
        cfg = dict(CFG, international={"import_duty_pct": 0.0, "clearance_fee_zar": 0})
        d = pricing.decide("30TH ANNIVERSARY", ETB30, 1000, dict(UK, intl_shipping=5), cfg, fx=22.0)
        # 1000 + 110 shipping + 0.15 x 1100 = 1275 <= 1495
        self.assertTrue(d.qualifies)
        self.assertAlmostEqual(d.comparable, 1275)
        self.assertTrue(d.estimated)


if __name__ == "__main__":
    unittest.main()
