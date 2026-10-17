"""One-off ban test: the 5-second 30th check (fast_fetch) on every given shop, from GitHub, for N minutes."""
import collections, concurrent.futures as cf, sys, time
import monitor as m

names = set(sys.argv[1].split(","))
minutes = float(sys.argv[2]) if len(sys.argv) > 2 else 5
every = float(sys.argv[3]) if len(sys.argv) > 3 else 5
shops = [s for s in m.CONFIG["stores"] if s["name"] in names]
tally = collections.defaultdict(collections.Counter)

def one(st):
    try:
        m.fast_fetch(st); return st["name"], "ok"
    except Exception as e:
        return st["name"], str(getattr(e, "code", None) or type(e).__name__)

end = time.time() + minutes * 60
while time.time() < end:
    t0 = time.time()
    with cf.ThreadPoolExecutor(8) as ex:
        for name, res in ex.map(one, shops):
            tally[name][res] += 1
    time.sleep(max(0, every - (time.time() - t0)))
print("\nRESULTS")
for st in shops:
    c = tally[st["name"]]
    print(f"{'OK     ' if set(c) == {'ok'} else 'ERRORS '} {st['name']:22s} {dict(c)}")
