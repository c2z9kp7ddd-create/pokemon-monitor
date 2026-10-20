import threading
import unittest
import unittest.mock as mock

from helpers import fresh_state, isolate, item, monitor

DAY = 86400


class RepeatRule(unittest.TestCase):
    def test_once_then_blocked(self):
        s = fresh_state()
        self.assertTrue(monitor.first_alert(s, "k", 1000, now=0))
        self.assertFalse(monitor.first_alert(s, "k", 1000, now=10))
        self.assertFalse(monitor.first_alert(s, "k", 1000, now=2 * DAY))   # still in stock: no daily repeats

    def test_five_percent_drop_boundary(self):
        s = fresh_state()
        monitor.first_alert(s, "k", 1000, now=0)
        self.assertFalse(monitor.first_alert(s, "k", 951, now=1))          # 4.9% lower
        self.assertTrue(monitor.first_alert(s, "k", 950, now=2))           # exactly 5% lower
        self.assertFalse(monitor.first_alert(s, "k", 940, now=3))          # vs the new 950: only 1% lower

    def test_price_bouncing_around_the_threshold_does_not_spam(self):
        s = fresh_state()
        monitor.first_alert(s, "k", 1000, now=0)
        sent = sum(monitor.first_alert(s, "k", p, now=i + 1) for i, p in enumerate([990, 1010, 980, 1005, 960]))
        self.assertEqual(sent, 0)

    def test_back_in_stock_only_after_24_hours(self):
        s = fresh_state()
        it = item()
        monitor.first_alert(s, it["key"], 1299, now=0)
        monitor.note_unavailable(s, dict(it, in_stock=False))
        self.assertFalse(monitor.first_alert(s, it["key"], 1299, now=DAY - 1))
        self.assertTrue(monitor.first_alert(s, it["key"], 1299, now=DAY))

    def test_same_product_listed_twice_alerts_once(self):
        s = fresh_state()
        fp = monitor.matching.fingerprint("Shop", "30th Celebration Elite Trainer Box")
        self.assertTrue(monitor.first_alert(s, "Shop|1", 1299, fp, now=0))
        self.assertFalse(monitor.first_alert(s, "Shop|2", 1299, fp, now=5))   # new URL / second listing

    def test_two_workers_find_the_same_deal(self):
        s = fresh_state()
        results, gate = [], threading.Barrier(8)

        def worker():
            gate.wait()
            results.append(monitor.first_alert(s, "k", 1299, "fp"))
        threads = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(results.count(True), 1)

    def test_history_survives_a_restart(self):
        import json
        s = fresh_state()
        monitor.first_alert(s, "k", 1000, now=0)
        s2 = json.loads(json.dumps(s))   # what state.json holds after a restart or redeploy
        self.assertFalse(monitor.first_alert(s2, "k", 1000, now=60))


class Pipeline(unittest.TestCase):
    def setUp(self):
        isolate(self)
        self.sent = []
        p = mock.patch.object(monitor, "notify", lambda *a, **k: self.sent.append((a, k)))
        p.start()
        self.addCleanup(p.stop)
        monitor.LAST_VALUES.clear()

    def run_with(self, it, verified):
        with mock.patch.object(monitor, "verify_listing", return_value=verified):
            return monitor.consider(fresh_state() if not hasattr(self, "state") else self.state, it, "test")

    def test_in_stock_at_retail_alerts_with_a_useful_message(self):
        ok = self.run_with(item(price=1299), (monitor.IN_STOCK, 1299.0, "product page: available"))
        self.assertTrue(ok)
        (title, plain), kw = self.sent[0][0][:2], self.sent[0][1]
        self.assertIn("30TH ANNIVERSARY IN STOCK: Takealot", title)
        self.assertIn("R1,299.00 | in stock", plain)
        self.assertIn("SA shelf ~R1,300", plain)
        self.assertIn("estimate", plain)
        self.assertIn("SAST", plain)
        self.assertEqual(kw["click"], "https://example.test/p/1")

    def test_preorder_is_buyable_and_labelled(self):
        self.assertTrue(self.run_with(item(), (monitor.PREORDER, 1299.0, "product: backorder / pre-order")))
        self.assertIn("PRE-ORDER", self.sent[0][0][0])

    def test_failed_check_never_alerts(self):
        self.assertFalse(self.run_with(item(), (monitor.UNKNOWN, None, "check failed: HTTP 503")))
        self.assertFalse(self.sent)

    def test_sold_out_on_the_product_page_never_alerts(self):
        self.assertFalse(self.run_with(item(), (monitor.OUT_OF_STOCK, 1299.0, "product page: sold out")))
        self.assertFalse(self.run_with(item(), (monitor.GONE, None, "product page returned 404")))
        self.assertFalse(self.sent)

    def test_price_is_rechecked_on_the_product_page(self):
        # search said R1,299, the product page says R1,899: above retail +15%, so no alert
        self.assertFalse(self.run_with(item(price=1299), (monitor.IN_STOCK, 1899.0, "product page: available")))
        self.assertFalse(self.sent)

    def test_overpriced_listing_is_rejected_before_any_request(self):
        with mock.patch.object(monitor, "verify_listing", side_effect=AssertionError("should not verify")):
            self.assertFalse(monitor.consider(fresh_state(), item(price=2500), "test"))

    def test_irrelevant_and_non_english_listings(self):
        with mock.patch.object(monitor, "verify_listing", side_effect=AssertionError("should not verify")):
            s = fresh_state()
            self.assertFalse(monitor.consider(s, item(title="Pokemon Surging Sparks Booster Bundle"), "test"))
            self.assertFalse(monitor.consider(s, item(title="Japanese 30th Celebration Booster Box"), "test"))
        self.assertEqual(s["stats"]["rejected"]["irrelevant"], 2)

    def test_second_discovery_is_a_suppressed_duplicate(self):
        self.state = fresh_state()
        v = (monitor.IN_STOCK, 1299.0, "product page: available")
        self.assertTrue(self.run_with(item(), v))
        self.assertFalse(self.run_with(item(), v))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.state["stats"]["rejected"]["duplicate"], 1)

    def test_etb_needs_collector_value(self):
        etb = item(title="Pokemon Prismatic Evolutions Elite Trainer Box", price=1200)
        self.assertFalse(self.run_with(etb, (monitor.IN_STOCK, 1200.0, "x")))
        monitor.LAST_VALUES[etb["key"]] = (1800, "lower of TCGplayer recent sales and SA median, 20 Oct")
        self.assertTrue(self.run_with(etb, (monitor.IN_STOCK, 1200.0, "product page: available")))
        self.assertIn("BELOW COLLECTOR VALUE", self.sent[0][0][0])
        self.assertIn("33% below", self.sent[0][0][1])


if __name__ == "__main__":
    unittest.main()


class FirstSweepDigest(unittest.TestCase):
    def setUp(self):
        isolate(self)
        self.sent = []
        p = mock.patch.object(monitor, "notify", lambda *a, **k: self.sent.append((a, k)))
        p.start()
        self.addCleanup(p.stop)

    def test_already_buyable_deals_arrive_as_one_message(self):
        s = fresh_state()
        v = (monitor.IN_STOCK, 1299.0, "product page: available")
        monitor.DIGEST[:] = [True]
        with mock.patch.object(monitor, "verify_listing", return_value=v):
            n = sum(monitor.consider(s, item(pid=str(i), title=f"Pokemon 30th Celebration Elite Trainer Box {i}"), "scan")
                    for i in range(5))
        found, monitor.DIGEST[:] = monitor.DIGEST[1:], []
        self.assertEqual((n, len(found), len(self.sent)), (5, 5, 0))   # recorded, nothing sent yet
        monitor.send_digest(found)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("5 listings", self.sent[0][0][0])
        with mock.patch.object(monitor, "verify_listing", return_value=v):   # later runs: already alerted
            self.assertFalse(monitor.consider(s, item(pid="0", title="Pokemon 30th Celebration Elite Trainer Box 0"), "scan"))
