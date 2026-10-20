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
            self.assertTrue(monitor.mine("Geek Zone"))
            self.assertFalse(monitor.mine("Takealot"))

    def test_server_takes_over_only_when_config_says_so(self):
        with mock.patch.object(monitor, "IS_GITHUB", False), mock.patch.object(monitor, "IS_SERVER", True):
            self.assertFalse(monitor.mine("Geek Zone"))                    # local_owner is still "mac"
            with mock.patch.dict(monitor.CONFIG, {"local_owner": "server"}):
                self.assertTrue(monitor.mine("Geek Zone"))
                self.assertFalse(monitor.mine("Takealot"))               # never duplicates the cloud

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

    def test_hourly_to_the_status_group_only(self):
        sent = []
        s = fresh_state()
        with mock.patch.object(monitor, "status_chat", return_value="-100123"), \
             mock.patch.object(monitor, "notify", lambda t, m, **k: sent.append((t, m, k))), \
             mock.patch.dict(monitor.CONFIG["updates"], {"status_report_minutes": 60}):
            monitor.scan_report(s)
            monitor.scan_report(s)          # within the hour: nothing
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][2]["channel"], "status")
        self.assertTrue(sent[0][2]["silent"])
        self.assertIn("shops checked", sent[0][1])


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
