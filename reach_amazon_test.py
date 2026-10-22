"""One-off: does Amazon.co.za serve real product pages to this machine at the monitor's pace?
Reads each wanted product page once, rotation_seconds apart. A bot-check page counts as BLOCKED."""
import sys
import time

import monitor as m

n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
st = m.STORE_BY_NAME["Amazon.co.za"]
ok = bot = other = 0
for asin in (st.get("asins") or [])[:n]:
    t = time.time()
    try:
        it = m.amazon_product("Amazon.co.za", asin)
        ok += 1
        print(f"ok      {asin} {'in stock' if it['in_stock'] else 'sold out'} | {it['title'][:50]}", flush=True)
    except Exception as e:
        bad = "bot check" in str(e).lower() or "202" in str(e)
        bot += bad
        other += not bad
        print(f"{'BOT    ' if bad else 'ERROR  '} {asin} {str(e)[:70]}", flush=True)
    time.sleep(max(0, st.get("rotation_seconds", 15) - (time.time() - t)))
print(f"\nRESULT: {ok} real pages, {bot} bot checks, {other} other errors -> "
      f"{'CLOUD-OK' if ok and not bot else 'BLOCKED' if not ok else 'PARTIAL'}")
