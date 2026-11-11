import unittest

from helpers import monitor


class ShopifyTitle(unittest.TestCase):
    """Shops that tag a product "Pokemon" but leave it out of the title (The Jokers House)."""

    def test_tagged_pokemon_title_is_repaired(self):
        p = {"title": "Perfect Order - Elite Trainer Box", "vendor": "The Jokers House",
             "tags": ["Mega Evolution", "Perfect Order", "Pokemon"]}
        t = monitor.shopify_title(p)
        self.assertEqual(t, "Pokemon Perfect Order - Elite Trainer Box")
        self.assertTrue(monitor.is_tcg(t))

    def test_tags_as_comma_string(self):
        p = {"title": "Perfect Order - Elite Trainer Box", "tags": "Pokemon, Sealed"}
        self.assertTrue(monitor.is_tcg(monitor.shopify_title(p)))

    def test_no_repair_without_pokemon_or_for_excluded(self):
        self.assertEqual(monitor.shopify_title({"title": "Magic Elite Trainer Box", "tags": ["MTG"]}),
                         "Magic Elite Trainer Box")
        self.assertEqual(monitor.shopify_title({"title": "Pikachu Plush", "tags": ["Pokemon"]}), "Pikachu Plush")
        self.assertEqual(monitor.shopify_title({"title": "Pokemon TCG 30th Celebration ETB", "tags": ["Pokemon"]}),
                         "Pokemon TCG 30th Celebration ETB")


if __name__ == "__main__":
    unittest.main()
