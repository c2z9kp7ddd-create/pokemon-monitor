"""Shared test setup: import the monitor without touching the network, the real state or Telegram."""
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("GITHUB_ACTIONS", None)
os.environ.pop("MONITOR_ROLE", None)

import monitor  # noqa: E402


def no_network(*a, **k):
    raise AssertionError("test tried to use the network")


def fresh_state():
    return {"items": {}, "stores": {}, "alerted_wanted": {}}


def item(store="Takealot", pid="1", title="Pokemon TCG 30th Celebration Elite Trainer Box", price=1299.0,
         in_stock=True, url="https://example.test/p/1"):
    return {"key": f"{store}|{pid}", "store": store, "title": title, "url": url, "price": price, "in_stock": in_stock}


def isolate(test):
    """Patch out every outgoing request and Telegram send for one test."""
    patches = [mock.patch.object(monitor, "get_json", no_network), mock.patch.object(monitor, "get_html", no_network),
               mock.patch.object(monitor, "save_state", lambda s: None),
               mock.patch.object(monitor.urllib.request, "urlopen", no_network),
               mock.patch.object(monitor, "mine", lambda store: True)]  # act as the copy that owns every shop
    for p in patches:
        p.start()
        test.addCleanup(p.stop)
