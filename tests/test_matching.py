import unittest

from helpers import monitor
import matching


class Targets(unittest.TestCase):
    def check(self, title, expected):
        self.assertEqual(matching.label(title), expected, title)

    def test_30th_anniversary_products(self):
        for t in ["Pokemon TCG: 30th Celebration Elite Trainer Box",
                  "Pokemon 30th Celebration Booster Bundle",
                  "Pokémon TCG 30th Celebration Mini Tin",
                  "Pokemon 30th Anniversary Ultra-Premium Collection",
                  "30th Celebration Pokemon Center Elite Trainer Box",
                  "Pokemon TCG: 30th Celebration - 2-Pack Blister"]:
            self.check(t, "30TH ANNIVERSARY")

    def test_delta_reign_products(self):
        # 2026-10-20: Delta Reign ETBs and booster boxes only
        for t in ["Pokemon TCG Mega Evolution - Delta Reign Elite Trainer Box",
                  "Pokemon: Mega Evolution—Delta Reign Booster Box",     # em dash glued the words
                  "Delta Reign Booster Display (36 packs)",
                  "Pokemon TCG: Mega Evolution - Delta Reign Pokemon Center Elite Trainer Box",
                  "Pokemon: Delta Reign Booster Box Case"]:
            self.check(t, "DELTA REIGN")
        for t in ["Pokemon TCG: Delta Reign - 3-Pack blister (Pre-Order)",
                  "Pokemon TCG: Delta Reign - Premium Blister",
                  "Pokemon: Mega Evolution—Delta Reign Booster Pack",
                  "Pokemon-TCG Mega Evolution Delta Reign Booster *Pre-Order*",
                  "Pre-Order: Pokemon TCG: Mega Evolution - Delta Reign Premium Checklane",
                  "Pokemon TCG: Mega Evolution - Delta Reign Booster Bundle",
                  "Pokemon TCG: Mega Evolution - Delta Reign Build & Battle Box"]:
            self.check(t, None)

    def test_generic_boxes_and_etbs(self):
        self.check("Pokemon Scarlet & Violet Prismatic Evolutions Elite Trainer Box", "ETB")
        self.check("Pokemon Mega Evolution Perfect Order ETB", "ETB")
        self.check("Pokemon Surging Sparks Booster Box", "BOOSTER BOX")
        self.check("Pokemon Chaos Rising Booster Display 36 Packs", "BOOSTER BOX")

    def test_20th_anniversary_targets_are_gone(self):
        # 2026-10-07: no 20th Anniversary targets. Non-box 2016 products never match; an XY Evolutions
        # ETB is only an ordinary ETB (collector-value test), not a special target.
        self.check("Pokemon Generations Radiant Collection", None)
        self.check("Pokemon XY Evolutions Booster Pack", None)
        self.check("Pokemon 20th Anniversary Mythical Collection", None)
        self.check("Pokemon XY Evolutions Elite Trainer Box", "ETB")
        self.assertFalse(hasattr(monitor, "TWENTIETH"))
        self.assertNotIn("evolutions", monitor.LIGHT_TERMS)
        self.assertNotIn("generations", monitor.LIGHT_TERMS)

    def test_non_english_never_matches(self):
        for t in ["Pokemon TCG Japan: Terastal Festival Booster Box",          # PokePlugZA spelling
                  "Pokemon Japanese 30th Celebration Booster Box (M6a)",
                  "Pokemon Chinese | 30th Anniversary Celebration | Booster Box",
                  "Pokemon TCG Korean Delta Reign Booster Box",
                  "Pokemon 30th Celebration Simplified Chinese Gift Box",
                  "Pokemon Mega Dream ex Booster Box JP"]:
            self.check(t, None)
        self.assertTrue(matching.is_english("Pokemon 30th Celebration ETB"))

    def test_near_misses(self):
        self.check("Pokemon Booster Bundle Surging Sparks", None)             # bundle is not a box
        self.check("Pokemon Surging Sparks Booster Pack", None)               # single pack, ordinary set
        self.check("Magnetic Booster Box Protector", None)                    # accessory
        self.check("Pokemon ETB Sleeves 65 pack", None)                       # accessory
        self.check("Acrylic Booster Box Display Case", None)                  # accessory
        self.check("Pokemon 30th Celebration Binder", None)                   # lone binder is an accessory
        self.check("Pokemon 30th Celebration Binder Collection", "30TH ANNIVERSARY")
        self.check("Pokemon Celebrations Elite Trainer Box", "ETB")           # 25th, not 30th
        self.check("Pokemon Celebrations Ultra Premium Collection", None)     # 25th Anniversary product
        self.check("Pokemon 25th Anniversary Collection", None)
        self.check("Empty Elite Trainer Box", None)

    def test_nothing_pitch_black_ever_alerts(self):
        for t in ["Pokemon TCG Mega Evolution Pitch Black Elite Trainer Box",
                  "Pokemon: Mega Evolution—Pitch Black: Pokemon Center Elite Trainer Box",
                  "Pokemon Pitch Black ETB",
                  "Elite Trainer Box - Pitch Black (Type May Vary)"]:
            self.check(t, None)
        self.check("Pokemon Mega Evolution Pitch Black Booster Box", None)          # nothing Pitch Black at all
        self.check("Pokemon: Mega Evolution—Pitch Black Booster Display", None)
        self.check("Pokemon: Mega EvolutionPitch Black: Booster Box", None)          # Toys R Us spelling
        self.check("Pokemon: Mega EvolutionPitch Black: Elite Trainer Box", None)
        self.check("Pokemon PitchBlack ETB", None)
        self.check("Pokemon Pitch-Black Booster Box", None)
        self.check("Pokemon Mega Evolution Chaos Rising Elite Trainer Box", "ETB")

    def test_is_tcg_drops_singles_and_merch(self):
        self.assertFalse(monitor.is_tcg("Pikachu ex 030/193 Delta Reign"))
        self.assertFalse(monitor.is_tcg("Pokemon 30th Celebration Plush Pikachu"))
        self.assertTrue(monitor.is_tcg("Pokemon TCG 30th Celebration Elite Trainer Box"))

    def test_fingerprint_joins_duplicate_listings(self):
        a = matching.fingerprint("Shop", "Pokemon TCG: 30th Celebration Elite Trainer Box (Pre-Order)")
        b = matching.fingerprint("Shop", "30th Celebration Elite Trainer Box")
        self.assertEqual(a, b)
        self.assertNotEqual(a, matching.fingerprint("Other shop", "30th Celebration Elite Trainer Box"))
        self.assertNotEqual(a, matching.fingerprint("Shop", "30th Celebration Pokemon Center Elite Trainer Box"))


if __name__ == "__main__":
    unittest.main()
