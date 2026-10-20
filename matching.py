"""Which listings are wanted products. One ordered table, so a new target is one line here plus a test
in tests/test_matching.py (genuine matches and misleading near-matches).

Authoritative rules (Gideon): English only.
  1. Anything 30th Anniversary / 30th Celebration.      price test: retail OR collector
  2. Anything Delta Reign.                               price test: retail OR collector
  3. Any booster box or Elite Trainer Box, any set.      price test: collector only
The old 20th Anniversary targets (XY Evolutions, Generations, CP6) were dropped on 2026-10-07 and must
not come back: an XY Evolutions ETB is now just an ETB (rule 3), nothing more.
"""
import html
import re
import unicodedata

# (label, pattern, price test). First match wins, so the specific targets come before the generic ones.
TARGETS = [
    ("30TH ANNIVERSARY", re.compile(r"\b30\s?th\b|\b30 years\b|\b30-year", re.I), "retail_or_collector"),
    ("DELTA REIGN", re.compile(r"\bdelta reign\b", re.I), "retail_or_collector"),
    ("BOOSTER BOX", re.compile(r"\bbooster box\b|\bbooster display\b|\b36[- ]?packs?\b", re.I), "collector"),
    ("ETB", re.compile(r"\belite trainer\b|\betb\b", re.I), "collector"),
]
PRICE_TEST = {label: test for label, _, test in TARGETS}

NON_ENGLISH = re.compile(r"japanese|chinese|korean|thai|indonesian|simplified|traditional|"
                         r"\bjp\b|\bjpn\b|\bcn\b|\bchs\b|\bcht\b|\bm6a\b|\bkr\b|\bgerman\b|\bfrench\b|"
                         r"\bitalian\b|\bspanish\b|\bportuguese\b", re.I)
# accessories that merely mention a product ("Booster Box Protector", "ETB sleeves")
ACCESSORY = re.compile(r"\b(protector|acrylic|magnetic|storage box|storage case|empty|sleeves?|toploaders?|"
                       r"deck box|playmat|dividers?|card case)\b", re.I)
LONE_BINDER = re.compile(r"\bbinder\b", re.I)


def norm(s):
    s = html.unescape(s or "")
    s = re.sub(r"[‐-―−]", " - ", s)  # "Mega Evolution—Delta Reign": a dropped dash glued the words together
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip()


def is_english(title):
    return not NON_ENGLISH.search(norm(title))


def label(title):
    """Target label for a listing title, or None when it is not a wanted product."""
    t = norm(title)
    if NON_ENGLISH.search(t) or ACCESSORY.search(t):
        return None
    if LONE_BINDER.search(t) and not re.search(r"collection", t, re.I):
        return None  # a binder on its own is an accessory, not sealed product
    for name, pattern, _ in TARGETS:
        if pattern.search(t):
            return name
    return None


def fingerprint(store, title):
    """Same product listed twice by one shop (or moved to a new URL) shares this key."""
    words = re.findall(r"[a-z0-9]+", norm(title).lower())
    drop = {"pokemon", "tcg", "the", "and", "pre", "order", "preorder", "new", "english", "sealed"}
    return f"{store}|{' '.join(w for w in words if w not in drop)}"
