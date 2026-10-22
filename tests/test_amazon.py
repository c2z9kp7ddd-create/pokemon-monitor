"""Amazon: product-page parsing (snippets of real pages, 2026-10-22), the rotation and partial scans."""
import unittest
import unittest.mock as mock

from helpers import fresh_state, isolate, monitor

IN_STOCK = ('<span id="productTitle"> Pokemon TCG 30th Celebration - Elite Trainer Box </span>'
            '<input type="hidden" id="twister-plus-price-data-price" value="1299" />'
            '<div id="availability"><span>In stock</span></div><input id="add-to-cart-button" />'
            '<a id="sellerProfileTriggerId">Amazon.co.za</a>'
            '<script>{"priceAmount":76.00}</script>')          # a carousel product's price further down
OUT = ('<span id="productTitle">Pokemon TCG 30th Celebration - Elite Trainer Box</span>'
       '<div id="outOfStock" class="a-box"> Currently unavailable. We don\'t know when or if this item will be back '
       'in stock.</div><div id="availability"><span>Currently unavailable.</span></div>')
PRE = ('<span id="productTitle">Pokemon TCG Delta Reign Elite Trainer Box (Pre-order)</span>'
       '<input type="hidden" id="twister-plus-price-data-price" value="1250" />'
       '<div id="availability"><span>This item will be released on 14 November 2026.</span></div>'
       '<input id="buy-now-button" />')
BOT = '<html><form action="/errors/validateCaptcha">Type the characters you see</form></html>'


class ProductPage(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def read(self, page):
        with mock.patch.object(monitor, "amazon_get", lambda url: page):
            return monitor.amazon_product("Amazon.co.za", "B0H8SV3WJ8")

    def test_in_stock_uses_the_main_price_not_a_carousel_price(self):
        it = self.read(IN_STOCK)
        self.assertEqual((it["in_stock"], it["price"], it["seller"]), (True, 1299.0, "Amazon.co.za"))

    def test_currently_unavailable(self):
        it = self.read(OUT)
        self.assertFalse(it["in_stock"])
        self.assertIsNone(it["price"])

    def test_preorder_with_only_buy_now(self):
        self.assertTrue(self.read(PRE)["in_stock"])

    def test_bot_check_is_an_error_not_out_of_stock(self):
        with self.assertRaises(ValueError):
            self.read(BOT)


class Rotation(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_one_product_per_beat_in_turn(self):
        st = dict(monitor.STORE_BY_NAME["Amazon.co.za"], asins=["A1", "A2", "A3"])
        read = []

        def product(store, asin, base):
            read.append(asin)
            return dict(monitor.item(store, asin, "Pokemon 30th Celebration Elite Trainer Box", "u", 1299, True))
        with mock.patch.object(monitor, "amazon_product", product), \
             mock.patch.object(monitor, "amazon_wanted", lambda s: s["asins"]):
            monitor.AMAZON_TURN[0] = -1
            for _ in range(4):
                found = monitor.fast_fetch(st)
        self.assertEqual(read, ["A1", "A2", "A3", "A1"])
        self.assertTrue(found[0]["page_checked"])

    def test_rotation_alert_does_not_reread_the_page(self):
        sent = []
        st = monitor.STORE_BY_NAME["Amazon.co.za"]
        it = dict(monitor.item("Amazon.co.za", "A1", "Pokemon 30th Celebration Elite Trainer Box", "u", 1299, True),
                  page_checked=True)
        s = fresh_state()
        s["items"][it["key"]] = dict(it, in_stock=False)          # was sold out: now back in stock
        with mock.patch.object(monitor, "verify_listing", side_effect=AssertionError("second request")), \
             mock.patch.object(monitor, "notify", lambda *a, **k: sent.append(a)):
            monitor.fast_handle(s, st, [it], known={"Amazon.co.za"})
        self.assertEqual(len(sent), 1)


class PartialScan(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_overrunning_pass_keeps_what_it_read(self):
        calls = []

        def product(store, asin, base):
            calls.append(asin)
            if len(calls) == 3:
                raise monitor.OutOfTime("scan time budget used up")
            return monitor.item(store, asin, "Pokemon 30th Celebration Elite Trainer Box", "u", None, False)
        with mock.patch.object(monitor, "amazon_product", product), \
             mock.patch.object(monitor, "amazon_wanted", lambda s: ["A1", "A2", "A3", "A4"]):
            out = monitor.scan_amazon("Amazon.co.za", "https://www.amazon.co.za")
        self.assertEqual([i["key"] for i in out], ["Amazon.co.za|A1", "Amazon.co.za|A2"])


if __name__ == "__main__":
    unittest.main()
