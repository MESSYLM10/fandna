"""
Live checks against the real Qloo and Groq APIs, before a demo or a deploy:

    python live_check.py                 # all checks
    python live_check.py --cities        # which cities Qloo resolves (eyeball the places)

1. Same brief twice: the second run must cost 0 Qloo calls and 0 Groq calls.
2. Weak-data club: does Qloo know a smaller African club, and does the app say so honestly?
3. Groq limit: an oversized request must fail fast with a clear message, never loop.

Uses about one Groq brief (~3,500 tokens) the first time; repeat runs are cached.
"""
import sys
import time

import brief
import qloo

if "--cities" in sys.argv:
    for city in ["Pretoria", "Durban", "Cape Town", "Mbabane"]:
        for query in (f"{city}, South Africa" if city != "Mbabane" else "Mbabane, Eswatini",
                      f"{city}, Gauteng" if city == "Pretoria" else f"{city}, KwaZulu-Natal" if city == "Durban"
                      else f"{city}, Western Cape" if city == "Cape Town" else "Mbabane, Swaziland"):
            s = qloo.signal_set("places", qloo.Audience(location=query))
            print(f"{query:<28} {', '.join(i['name'] for i in s['items'][:5]) or '(nothing)'}")
    print("\nIf a query's places are in that city, add it to CITIES in app.py as {label: query}.")
    sys.exit(0)

WEAK_CLUB = next((a for a in sys.argv[1:] if not a.startswith("--")), "Mbabane Swallows")
ARGS = dict(objective="Fill the stadium for the next home match", budget_zar=250_000)
ok = True


def check(cond: bool, msg: str):
    global ok
    ok &= cond
    print(("  [ok]  " if cond else "  [FAIL] ") + msg)


calls = {"qloo": 0}
_fetch = qloo._fetch


def counted(path, params):
    calls["qloo"] += 1
    return _fetch(path, params)


qloo._fetch = counted

print("1. Same brief twice")
t0 = time.time()
ev = qloo.build_evidence(seed_query="Kaizer Chiefs")
club = ev["seed"]["name"] if ev.get("seed_effect") else None
first = brief.cached_brief(ev, club, **ARGS)
t1 = time.time()
q_before = calls["qloo"]
ev2 = qloo.build_evidence(seed_query="Kaizer Chiefs")
second = brief.cached_brief(ev2, club, **ARGS)
t2 = time.time()
print(f"  first run {t1 - t0:.1f}s ({q_before} Qloo calls, brief {'saved' if first['meta']['cached'] else 'new'}), "
      f"second run {t2 - t1:.2f}s")
check(calls["qloo"] == q_before, "second run made no Qloo calls")
check(second["meta"]["cached"], "second run reused the saved brief (no Groq call)")
g = first["grounding"]
check(g["verified"] == g["cited"], f"every cited signal found in Qloo ({g['verified']} of {g['cited']})")
check(all(c["to_validate"] for c in first["concepts"]), "every concept lists checks to validate")
check(all(c["kpis"] and all(k["baseline"] for k in c["kpis"]) for c in first["concepts"]),
      "every KPI has a baseline")
print(f"  recommended: {first['lead']['name']}")

print(f"\n2. Weak-data club: {WEAK_CLUB}")
weak = qloo.build_evidence(qloo.Audience(), seed_query=WEAK_CLUB)   # Johannesburg: tests the club, not the city
seed = weak.get("seed")
print(f"  Qloo match: {seed['name'] + ' ' + str(seed['types']) if seed else 'none'}"
      + (f", accepted: {seed['confident']}" if seed else ""))
print(f"  club changed the results: {weak.get('seed_effect')}")
for kind, s in weak["signals"].items():
    print(f"  {kind:<8} {s['count']:>2} results  {', '.join(i['name'] for i in s['items'][:4])}")
for w in weak["warnings"]:
    print(f"  note shown to the user: {w}")
check(not seed or seed["confident"] or any("isn't the same thing" in w for w in weak["warnings"]),
      "a wrong club match is rejected and reported, not used")
check(bool(weak["warnings"]) or bool(weak.get("seed_effect")),
      "thin or missing club data is reported, not hidden")

print("\n3. Groq limit")
t0 = time.time()
try:
    brief._call("Reply with JSON.", "x " * 40_000, 10, brief.MODELS)
    check(False, "an oversized request should have been refused")
except brief.BriefError as e:
    print(f"  message: {e}")
    check(time.time() - t0 < 15, f"failed fast ({time.time() - t0:.1f}s), one attempt per model, no loop")

print("\nAll checks passed." if ok else "\nSome checks failed: see [FAIL] lines above.")
sys.exit(0 if ok else 1)
