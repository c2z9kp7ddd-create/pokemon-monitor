"""Telegram delivery, config validation, ownership and stale-shop detection."""
import io
import json
import os
import time
import unittest
import unittest.mock as mock
import urllib.error

from helpers import fresh_state, isolate, monitor


class FakeResp:
    def read(self):
        return b"{}"


def tg_error(code, body=b"{}"):
    return urllib.error.HTTPError("https://api.telegram.org", code, "err", {}, io.BytesIO(body))


class Telegram(unittest.TestCase):
    def setUp(self):
        isolate(self)
        p = mock.patch.object(monitor.time, "sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)
        monitor.TG_STATS.update(sent=0, retried=0, failed=0)

    def post(self, outcomes):
        calls = iter(outcomes)

        def fake(*a, **k):
            o = next(calls)
            if isinstance(o, Exception):
                raise o
            return FakeResp()
        with mock.patch.object(monitor.urllib.request, "urlopen", fake):
            return monitor._post_with_retry("https://api.telegram.org/botX/sendMessage", {}, "telegram")

    def test_rate_limit_then_success(self):
        self.assertIsNone(self.post([tg_error(429, b'{"parameters":{"retry_after":3}}'), "ok"]))
        self.assertEqual(monitor.TG_STATS["retried"], 1)
        self.assertEqual(monitor.TG_STATS["sent"], 1)

    def test_network_errors_retry_then_give_up(self):
        err = self.post([TimeoutError("x"), TimeoutError("x"), TimeoutError("x")])
        self.assertIn("TimeoutError", err)
        self.assertEqual(monitor.TG_STATS["retried"], 2)

    def test_permanent_errors_are_not_retried(self):
        self.assertEqual(self.post([tg_error(400)]), "HTTP 400")
        self.assertEqual(monitor.TG_STATS["retried"], 0)

    def test_failed_message_waits_in_the_outbox_and_is_resent_later(self):
        monitor.OUTBOX.clear()
        with mock.patch.object(monitor, "_post_with_retry", return_value="HTTP 502"), \
             mock.patch.dict(os.environ, {"TELEGRAM_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}):
            monitor.notify("30TH ANNIVERSARY IN STOCK: X", "body")
        self.assertEqual(len(monitor.OUTBOX), 1)
        state = {"outbox": list(monitor.OUTBOX)}
        monitor.OUTBOX.clear()
        sent = []
        with mock.patch.object(monitor, "notify", lambda title, *a, **k: sent.append(title)):
            monitor.flush_outbox(state)
        self.assertEqual(sent, ["30TH ANNIVERSARY IN STOCK: X (delayed)"])

    def test_day_old_messages_are_dropped(self):
        state = {"outbox": [{"title": "old", "message": "", "click": None, "tg_html": None, "t": time.time() - 90000}]}
        with mock.patch.object(monitor, "notify", side_effect=AssertionError("must not resend")):
            monitor.flush_outbox(state)


class Config(unittest.TestCase):
    def base(self):
        return json.loads(json.dumps(monitor.CONFIG))

    def test_real_config_is_valid(self):
        self.assertEqual(monitor.validate_config(monitor.CONFIG, env={}, github=False), [])

    def test_missing_secrets_in_the_cloud(self):
        errs = monitor.validate_config(monitor.CONFIG, env={}, github=True)
        self.assertTrue(any("TELEGRAM_TOKEN" in e for e in errs))

    def test_invalid_entries(self):
        c = self.base()
        c["stores"].append(dict(c["stores"][0]))                         # duplicate
        c["stores"].append({"name": "Bad", "type": "ftp", "url": "http://x"})
        c["local_only_stores"].append("Nowhere")
        c["retail_refs_rand"].append(["(unclosed", 100])
        c["fast_30th"]["seconds"] = 1
        c["international"] = {"import_duty_pct": 20}
        errs = "\n".join(monitor.validate_config(c, env={}, github=False))
        for part in ("listed twice", "unknown type", "https://", "Nowhere", "bad retail reference", "below 5", "fraction"):
            self.assertIn(part, errs)


class Ownership(unittest.TestCase):
    def test_mac_owns_only_local_only_shops(self):
        with mock.patch.object(monitor, "IS_GITHUB", False), mock.patch.object(monitor, "IS_SERVER", False):
            self.assertTrue(monitor.mine("Checkers"))
            self.assertFalse(monitor.mine("Takealot"))
            self.assertFalse(monitor.mine("Geek Zone"))                  # moved to the server (server_stores)

    def test_server_owns_server_stores_and_all_local_only_when_config_says_so(self):
        with mock.patch.object(monitor, "IS_GITHUB", False), mock.patch.object(monitor, "IS_SERVER", True):
            self.assertTrue(monitor.mine("Geek Zone"))                    # server_stores
            self.assertFalse(monitor.mine("Checkers"))                    # refuses data-centre IPs: stays on the Mac
            self.assertEqual({s["name"] for s in monitor.active_stores()}, set(monitor.CONFIG["server_stores"]))
            with mock.patch.dict(monitor.CONFIG, {"local_owner": "server"}):
                self.assertTrue(monitor.mine("Checkers"))
                self.assertFalse(monitor.mine("Takealot"))               # never duplicates the cloud

    def test_no_shop_has_two_owners(self):
        owners = {}
        for role in ("github", "server", "mac"):
            with mock.patch.object(monitor, "IS_GITHUB", role == "github"), \
                 mock.patch.object(monitor, "IS_SERVER", role == "server"):
                for st in monitor.active_stores():
                    if monitor.mine(st["name"]):
                        owners.setdefault(st["name"], []).append(role)
        self.assertEqual({n: r for n, r in owners.items() if len(r) > 1}, {})

    def test_cloud_never_scans_mac_only_shops(self):
        with mock.patch.object(monitor, "IS_GITHUB", True):
            names = {s["name"] for s in monitor.active_stores()}
        self.assertFalse(names & set(monitor.CONFIG["local_only_stores"]))


class StaleShops(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_one_message_when_stale_and_one_when_back(self):
        sent = []
        s = fresh_state()
        name = next(n["name"] for n in monitor.CONFIG["stores"] if n["name"] not in monitor.CONFIG["local_only_stores"]
                    and n["name"] not in monitor.CONFIG.get("known_blocked", []))
        s["last_ok"] = {n["name"]: time.time() for n in monitor.CONFIG["stores"]}
        s["last_ok"][name] = time.time() - 46 * 60
        with mock.patch.object(monitor, "IS_GITHUB", True), mock.patch.object(monitor, "IS_CLOUD", True), \
             mock.patch.object(monitor, "notify", lambda t, m, **k: sent.append((t, m))):
            monitor.cloud_store_watch(s)
            monitor.cloud_store_watch(s)          # still stale: no second message
            monitor.record_check(s, name, True, "fast")
            monitor.cloud_store_watch(s)
        self.assertEqual([t for t, _ in sent], ["Store watchdog: shops not being checked", "Store watchdog: shops back"])
        self.assertIn(name, sent[0][1])

    def test_effective_interval(self):
        s = fresh_state()
        with mock.patch.object(monitor.time, "time", side_effect=[100.0, 105.0, 111.0, 116.0]):
            for _ in range(4):
                monitor.record_check(s, "Shop", True, "fast")
        self.assertEqual(monitor._median_gap(s["shop_stats"]["Shop"]["beats_fast"]), 5.0)

    def test_status_report_is_written(self):
        s = fresh_state()
        with mock.patch.object(monitor, "HERE", monitor.Path(self._tmp())):
            monitor.write_status(s)
            text = (monitor.HERE / "health" / "status.md").read_text()
        self.assertIn("Pokemon monitor status", text)
        self.assertIn("| Geek Zone |", text)       # the Mac's own shops

    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d


if __name__ == "__main__":
    unittest.main()


class ScanReport(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_silent_without_a_status_group(self):
        with mock.patch.object(monitor, "status_chat", return_value=None), \
             mock.patch.object(monitor, "notify", side_effect=AssertionError("must not send")), \
             mock.patch.dict(monitor.CONFIG["updates"], {"status_report_minutes": 60}):
            monitor.scan_report(fresh_state())

    def test_periodic_to_the_status_group_only(self):
        sent = []
        s = fresh_state()
        with mock.patch.object(monitor, "IS_CLOUD", True), \
             mock.patch.object(monitor, "status_chat", return_value="-100123"), \
             mock.patch.object(monitor, "notify", lambda t, m, **k: sent.append((t, m, k))), \
             mock.patch.dict(monitor.CONFIG["updates"], {"status_report_minutes": 4}):
            monitor.scan_report(s)
            monitor.scan_report(s)          # within 4 minutes: nothing
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][2]["channel"], "status")
        self.assertTrue(sent[0][2]["silent"])
        self.assertIn("shops checked", sent[0][1])

    def test_mac_sends_no_routine_update(self):
        with mock.patch.object(monitor, "IS_CLOUD", False), \
             mock.patch.object(monitor, "status_chat", return_value="-100123"), \
             mock.patch.object(monitor, "notify", side_effect=AssertionError("must not send")), \
             mock.patch.dict(monitor.CONFIG["updates"], {"status_report_minutes": 4}):
            monitor.scan_report(fresh_state())


class ChatSplit(unittest.TestCase):
    """Deal alerts -> the private chat; scanning updates -> the status group."""
    def setUp(self):
        isolate(self)

    def send(self, channel):
        bodies = []
        with mock.patch.object(monitor, "_post_with_retry", lambda url, body, name: bodies.append(body)), \
             mock.patch.dict(os.environ, {"TELEGRAM_TOKEN": "t", "TELEGRAM_CHAT_ID": "111"}), \
             mock.patch.object(monitor, "status_chat", return_value="-222"):
            monitor.notify("x", "y", channel=channel)
        return [b["chat_id"] for b in bodies if "chat_id" in b]

    def test_alerts_go_to_the_private_chat(self):
        self.assertEqual(self.send("alerts"), ["111"])

    def test_scan_updates_go_to_the_group(self):
        self.assertEqual(self.send("status"), ["-222"])

    def test_pitch_black_messages_are_not_resent(self):
        state = {"outbox": [{"title": "ETB BELOW COLLECTOR VALUE: X", "message": "Pokemon Pitch Black Elite Trainer Box",
                             "click": None, "tg_html": None, "t": time.time()}]}
        with mock.patch.object(monitor, "notify", side_effect=AssertionError("must not resend")):
            monitor.flush_outbox(state)


class CartLinks(unittest.TestCase):
    """One-tap add-to-cart links in alerts (Gideon 2026-10-21); nothing is bought automatically."""
    def shop(self, kind):
        return next(s for s in monitor.CONFIG["stores"] if s["type"] == kind)

    def test_shopify_permalink_uses_the_verified_variant(self):
        st = self.shop("shopify")
        it = {"store": st["name"], "key": f"{st['name']}|123", "url": "https://example-shop.co.za/products/etb",
              "_variant": 456}
        self.assertEqual(monitor.cart_link(it), "https://example-shop.co.za/cart/456:1")

    def test_woo_simple_product(self):
        st = self.shop("woo")
        it = {"store": st["name"], "key": f"{st['name']}|902", "url": st["url"] + "/product/x", "_woo_type": "simple"}
        self.assertEqual(monitor.cart_link(it), st["url"].rstrip("/") + "/?add-to-cart=902&quantity=1")

    def test_woo_variable_product_gets_no_cart_link(self):
        st = self.shop("woo")
        it = {"store": st["name"], "key": f"{st['name']}|902", "url": st["url"] + "/product/x", "_woo_type": "variable"}
        self.assertIsNone(monitor.cart_link(it))

    def test_other_shops_get_no_cart_link(self):
        st = self.shop("takealot")
        it = {"store": st["name"], "key": f"{st['name']}|1", "url": "https://www.takealot.com/x/PLID1"}
        self.assertIsNone(monitor.cart_link(it))

    def test_alert_message_shows_the_cart_link_first(self):
        text = []
        with mock.patch.dict(monitor.os.environ, {"TELEGRAM_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}), \
             mock.patch.object(monitor.urllib.request, "urlopen",
                               lambda req, **k: text.append(monitor.json.loads(req.data)["text"]) or io.BytesIO(b"{}")):
            monitor.notify("T", "m", click="https://shop/p", cart="https://shop/cart/1:1")
        self.assertLess(text[0].index("ADD TO CART"), text[0].index("Open product"))


class SharedDeals(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def bodies(self, channel, cfg_ids, fail=()):
        sent = []

        def post(url, body, name):
            sent.append(body["chat_id"])
            return "HTTP 502" if body["chat_id"] in fail else None
        with mock.patch.object(monitor, "_post_with_retry", post), \
             mock.patch.dict(os.environ, {"TELEGRAM_TOKEN": "t", "TELEGRAM_CHAT_ID": "111"}), \
             mock.patch.object(monitor, "status_chat", return_value="-222"), \
             mock.patch.dict(monitor.CONFIG, {"alert_chat_ids": cfg_ids}):
            monitor.OUTBOX.clear()
            monitor.notify("deal", "x", channel=channel)
        return sent

    def test_deals_go_to_me_and_the_shared_channel(self):
        self.assertEqual(self.bodies("alerts", ["-333"]), ["111", "-333"])

    def test_scan_updates_never_go_to_the_shared_channel(self):
        self.assertEqual(self.bodies("status", ["-333"]), ["-222"])

    def test_a_failed_send_is_resent_only_where_it_failed(self):
        self.bodies("alerts", ["-333"], fail={"-333"})
        self.assertEqual([m["chat"] for m in monitor.OUTBOX], ["-333"])
        state, monitor.OUTBOX[:] = {"outbox": list(monitor.OUTBOX)}, []
        got = []
        with mock.patch.object(monitor, "notify", lambda *a, **k: got.append(k.get("to"))):
            monitor.flush_outbox(state)
        self.assertEqual(got, [["-333"]])


class EdenToys(unittest.TestCase):
    """Eden Toys category cards: enabled 'Add to Cart' = buyable, disabled 'View Item' = sold out."""
    CARDS = '''
    <a href="/products/delta-reign-elite-trainer-box/7866" class="product-link">
      <input type="submit" name="ctl00$cph_vShop2$ctl05$btn_AddCart" value="View Item" id="cph_vShop2_ctl05_btn_AddCart" disabled="disabled" class="aspNetDisabled btn-rounded" />
      <div class="product-info"><h4 id="cph_vShop2_ctl05_lbl_Heading">Delta Reign - Elite Trainer Box</h4>
      <span id="cph_vShop2_ctl05_lbl_Price" class="price">R1,300.00</span></div></a>
    <a href="/products/pok&#233;mon-30th-celebration-elite-trainer-box/7900" class="product-link">
      <input type="submit" name="ctl00$cph_vShop2$ctl06$btn_AddCart" value="Add to Cart" id="cph_vShop2_ctl06_btn_AddCart" class="btn-rounded" />
      <div class="product-info"><h4 id="cph_vShop2_ctl06_lbl_Heading">Pok&#233;mon TCG 30th Celebration Elite Trainer Box</h4>
      <span id="cph_vShop2_ctl06_lbl_Price" class="price">R1,299.00</span></div></a>
    <div class="product-cols"></div>'''

    def test_cards(self):
        with mock.patch.object(monitor, "get_html", return_value=self.CARDS):
            items = monitor.scan_eden("Eden Toys", "https://www.edentoys.co.za")
        self.assertEqual([(i["title"][:20], i["price"], i["in_stock"]) for i in items],
                         [("Delta Reign - Elite ", 1300.0, False), ("Pokemon TCG 30th Cel", 1299.0, True)])
        self.assertTrue(items[1]["url"].endswith("/products/pok%C3%A9mon-30th-celebration-elite-trainer-box/7900"))


class AmazonZA(unittest.TestCase):
    """Amazon.co.za product pages: buyable only with an add-to-cart button and no 'currently unavailable'."""
    PAGE = ('<span id="productTitle" class="a-size-large"> Pok&eacute;mon TCG 30th Celebration - Elite Trainer Box </span>'
            '"priceAmount":1299.00, <div id="availability"><span class="a-color-success"> In stock </span></div>'
            "<a id='sellerProfileTriggerId' class='x'>Amazon.co.za</a>"
            '<input id="add-to-cart-button" type="submit">')

    def test_in_stock_product(self):
        with mock.patch.object(monitor, "amazon_get", return_value=self.PAGE):
            it = monitor.amazon_product("Amazon.co.za", "B0H8SV3WJ8")
        self.assertEqual((it["price"], it["in_stock"], it["seller"]), (1299.0, True, "Amazon.co.za"))
        self.assertEqual(it["url"], "https://www.amazon.co.za/dp/B0H8SV3WJ8")

    def test_unavailable_product(self):
        page = self.PAGE.replace("In stock", "Currently unavailable.").replace('id="add-to-cart-button"', "")
        with mock.patch.object(monitor, "amazon_get", return_value=page):
            self.assertFalse(monitor.amazon_product("Amazon.co.za", "B0H8SV3WJ8")["in_stock"])

    def test_cart_link_is_amazons_add_to_cart_form(self):
        it = {"store": "Amazon.co.za", "key": "Amazon.co.za|B0H8SV3WJ8", "url": "https://www.amazon.co.za/dp/B0H8SV3WJ8"}
        self.assertEqual(monitor.cart_link(it),
                         "https://www.amazon.co.za/gp/aws/cart/add.html?ASIN.1=B0H8SV3WJ8&Quantity.1=1")

    def test_owned_by_the_mac_only(self):
        """2026-10-21: Amazon bot-checks the Oracle server's data-centre IP; the Mac's home IP gets full pages."""
        with mock.patch.object(monitor, "IS_GITHUB", True):
            self.assertNotIn("Amazon.co.za", {s["name"] for s in monitor.active_stores()})
        with mock.patch.object(monitor, "IS_GITHUB", False), mock.patch.object(monitor, "IS_SERVER", True):
            self.assertFalse(monitor.mine("Amazon.co.za"))
        with mock.patch.object(monitor, "IS_GITHUB", False), mock.patch.object(monitor, "IS_SERVER", False):
            self.assertTrue(monitor.mine("Amazon.co.za"))


class ForeignShopsReadLightly(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_mac_reads_cloud_owned_shopify_shops_with_light_searches(self):
        calls = []
        with mock.patch.object(monitor, "IS_GITHUB", False), \
             mock.patch.object(monitor, "mine", lambda store: store == "Mine"), \
             mock.patch.object(monitor, "scan_shopify_light", lambda st, b: calls.append(("light", st)) or []), \
             mock.patch.object(monitor, "_scan_shopify", lambda st, b, p: calls.append(("full", st)) or []):
            monitor.scan_shopify("Cloud shop", "https://x")
            monitor.scan_shopify("Mine", "https://y")
        self.assertEqual(calls, [("light", "Cloud shop"), ("light", "Mine")])   # shopify_light_only
