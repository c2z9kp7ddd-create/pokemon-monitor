import json
import unittest
import unittest.mock as mock
import urllib.error

from helpers import isolate, item, monitor

SHOP = {"name": "Testshop", "type": "shopify", "url": "https://shop.test"}
WOO = {"name": "Woo", "type": "woo", "url": "https://woo.test"}


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "err", {}, None)


class Verify(unittest.TestCase):
    def setUp(self):
        isolate(self)
        p = mock.patch.dict(monitor.STORE_BY_NAME, {"Testshop": SHOP, "Woo": WOO,
                                                     "UKshop": dict(SHOP, name="UKshop", currency="GBP")})
        p.start()
        self.addCleanup(p.stop)

    def verify(self, it, json_reply=None, html_reply=None, raises=None):
        def fake(*a, **k):
            if raises:
                raise raises
            return json_reply
        with mock.patch.object(monitor, "get_json", fake), \
             mock.patch.object(monitor, "get_html", lambda *a, **k: html_reply), \
             mock.patch.object(monitor, "zar_per", lambda cur: 20.0):
            return monitor.verify_listing(it)

    def test_shopify_uses_the_cheapest_available_variant(self):
        reply = {"available": True, "price": 99900, "variants": [
            {"available": False, "price": 99900}, {"available": True, "price": 129900}]}
        status, price, _ = self.verify(item(store="Testshop"), reply)
        self.assertEqual((status, price), (monitor.IN_STOCK, 1299.0))

    def test_shopify_sold_out(self):
        reply = {"available": False, "price": 129900, "variants": [{"available": False, "price": 129900}]}
        self.assertEqual(self.verify(item(store="Testshop"), reply)[0], monitor.OUT_OF_STOCK)

    def test_preorder_in_title(self):
        reply = {"available": True, "price": 129900, "variants": [{"available": True, "price": 129900}]}
        it = item(store="Testshop", title="Pokemon 30th Celebration ETB (Pre-Order)")
        self.assertEqual(self.verify(it, reply)[0], monitor.PREORDER)

    def test_404_means_the_listing_is_gone(self):
        self.assertEqual(self.verify(item(store="Testshop"), raises=http_error(404))[0], monitor.GONE)

    def test_failures_are_unknown_not_out_of_stock(self):
        for err in (http_error(503), http_error(429), http_error(403), TimeoutError("slow"), ValueError("bad json")):
            self.assertEqual(self.verify(item(store="Testshop"), raises=err)[0], monitor.UNKNOWN, err)

    def test_woo_backorder_is_preorder(self):
        reply = {"is_purchasable": True, "is_in_stock": True, "is_on_backorder": True,
                 "prices": {"price": "129900", "currency_minor_unit": 2}}
        self.assertEqual(self.verify(item(store="Woo"), reply)[:2], (monitor.PREORDER, 1299.0))

    def test_woo_not_purchasable(self):
        reply = {"is_purchasable": False, "is_in_stock": True, "prices": {}}
        self.assertEqual(self.verify(item(store="Woo"), reply)[0], monitor.OUT_OF_STOCK)

    def test_overseas_price_converted_to_rand(self):
        reply = {"available": True, "price": 5000, "variants": [{"available": True, "price": 5000}]}
        self.assertEqual(self.verify(item(store="UKshop"), reply)[1], 1000.0)   # GBP 50 x 20

    def test_takealot(self):
        mk = lambda **kw: {"buybox": {"items": [dict(is_selected=True, price=1299, **kw)]}}
        with mock.patch.dict(monitor.STORE_BY_NAME, {"Takealot": {"name": "Takealot", "type": "takealot", "url": "https://t"}}):
            self.assertEqual(self.verify(item(), mk(is_preorder=True))[0], monitor.PREORDER)
            self.assertEqual(self.verify(item(), mk(is_add_to_cart_available=True))[0], monitor.IN_STOCK)
            self.assertEqual(self.verify(item(), mk())[0], monitor.OUT_OF_STOCK)
            self.assertEqual(self.verify(item(), {"buybox": {"items": []}})[0], monitor.OUT_OF_STOCK)

    def test_mithril_page_text(self):
        with mock.patch.dict(monitor.STORE_BY_NAME, {"MV": {"name": "MV", "type": "mithril", "url": "https://m"}}):
            it = item(store="MV", pid="x")
            self.assertEqual(self.verify(it, html_reply="<button>Add to cart</button>")[0], monitor.IN_STOCK)
            self.assertEqual(self.verify(it, html_reply="Sold out")[0], monitor.OUT_OF_STOCK)
            self.assertEqual(self.verify(it, html_reply="<div>loading</div>")[0], monitor.UNKNOWN)
            self.assertEqual(self.verify(it, html_reply="Please complete the captcha")[0], monitor.UNKNOWN)


if __name__ == "__main__":
    unittest.main()


class Pacing(unittest.TestCase):
    def test_cloud_spaces_shopify_product_checks(self):
        slept = []
        with mock.patch.object(monitor, "IS_GITHUB", True), \
             mock.patch.object(monitor.time, "sleep", slept.append), \
             mock.patch.object(monitor.time, "time", side_effect=[100.0, 100.0, 101.0, 103.0]):
            monitor._LAST_SHOPIFY_VERIFY[0] = 0.0
            monitor._pace_shopify_verify()      # first check: no wait
            monitor._pace_shopify_verify()      # 1 s later: waits the remaining 2 s
        self.assertEqual(slept, [2.0])

    def test_mac_does_not_wait(self):
        with mock.patch.object(monitor, "IS_GITHUB", False), mock.patch.object(monitor.time, "sleep", side_effect=AssertionError):
            monitor._pace_shopify_verify()
            monitor._pace_shopify_verify()
