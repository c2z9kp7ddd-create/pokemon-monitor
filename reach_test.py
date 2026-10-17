"""One-off: can each Mac-only shop be checked from GitHub's servers at the monitor's normal pace?
Runs the light 30th check (fast_fetch) and the 1-minute check (quick_fetch) once per round."""
import collections, sys, time
import monitor as m

rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 6
shops = [s for s in m.CONFIG["stores"] if s["name"] in set(m.CONFIG["local_only_stores"])]
tally = collections.defaultdict(collections.Counter)
for r in range(rounds):
    t0 = time.time()
    for st in shops:
        for label, fn in (("fast", m.fast_fetch), ("quick", m.quick_fetch)):
            try:
                fn(st)
                tally[st["name"]][f"{label}:ok"] += 1
            except Exception as e:
                code = getattr(e, "code", None) or type(e).__name__
                tally[st["name"]][f"{label}:{code}"] += 1
    print(f"round {r + 1} done in {time.time() - t0:.0f}s", flush=True)
    time.sleep(max(0, 60 - (time.time() - t0)))
print("\nRESULTS")
for st in shops:
    c = tally[st["name"]]
    ok = c["fast:ok"] + c["quick:ok"]
    print(f"{'CLOUD-OK' if ok == 2 * rounds else 'BLOCKED ' if ok == 0 else 'PARTIAL '} {st['name']:22s} {dict(c)}")
