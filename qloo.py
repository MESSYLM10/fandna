"""
FanDNA Qloo client: fixed calls, SQLite cache, normalised evidence.

One public function does the work:

    evidence = build_evidence(Audience(), seed_query="Kaizer Chiefs")

It makes at most 9 Qloo calls (1 search + 4 audience-only "baseline" +
4 seeded), all cached. The baseline is shared by every club with the same
audience, so a second club costs 5 calls and a repeat costs 0.

Environment:
    QLOO_API_KEY       required (falls back to ~/.qloo/config api_key=...)
    QLOO_BASE_URL      default https://hackathon.api.qloo.com
    FANDNA_CACHE       SQLite path, default fandna_cache.sqlite
    FANDNA_CACHE_TTL   seconds a result counts as fresh, default 7 days
    FANDNA_EXCLUDE     comma-separated names to drop from results

Run `python qloo.py "Kaizer Chiefs"` to print a summary and write evidence.json.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

BASE = os.environ.get("QLOO_BASE_URL", "https://hackathon.api.qloo.com")
CACHE_PATH = os.environ.get("FANDNA_CACHE", "fandna_cache.sqlite")
CACHE_TTL = int(os.environ.get("FANDNA_CACHE_TTL", 7 * 86400))
TIMEOUT = 20
MAX_ATTEMPTS = 3          # 1 try + 2 retries, for timeouts and 5xx only
BACKOFF = 1.0             # seconds; doubles each retry
THIN = 5                  # fewer results than this is flagged as thin evidence
TYPES = {
    "artists": "urn:entity:artist",
    "brands": "urn:entity:brand",
    "places": "urn:entity:place",
    "tags": "urn:tag",
}
# Results that don't belong in a club activation brief (the probe returned a
# dating app for the unseeded audience). Your call: edit or set FANDNA_EXCLUDE.
EXCLUDE = {n.strip().lower() for n in os.environ.get(
    "FANDNA_EXCLUDE", "Grindr,Tinder,Bumble,Hinge").split(",") if n.strip()}
# Other sports: real signals, but off-brief for a football campaign. They are set
# aside before the brief is written and shown as "set aside" in the app.
OFF_BRIEF = tuple(w.strip().lower() for w in os.environ.get(
    "FANDNA_OFF_BRIEF", "rugby,springbok,cricket,proteas,netball").split(",") if w.strip())


class QlooError(Exception):
    """kind is one of: auth, rate_limit, timeout, network, bad_request, server."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Audience:
    location: str = "Johannesburg"
    gender: str = "male"
    age: str = "35_and_younger"   # accepted by the hackathon host (probe, 27 Oct)

    def params(self) -> dict:
        p = {"signal.location.query": self.location, "signal.demographics.age": self.age}
        if self.gender:                 # empty = everyone
            p["signal.demographics.gender"] = self.gender
        return p


# ---------------------------------------------------------------- HTTP

_SESSION: requests.Session | None = None


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        key = os.environ.get("QLOO_API_KEY", "").strip()
        cfg = Path.home() / ".qloo" / "config"
        if not key and cfg.exists():
            for line in cfg.read_text().splitlines():
                if line.startswith("api_key="):
                    key = line.split("=", 1)[1].strip()
        if not key:
            raise QlooError("auth", "QLOO_API_KEY is not set.")
        _SESSION = requests.Session()
        _SESSION.headers.update({"X-Api-Key": key, "Accept": "application/json"})
    return _SESSION


def _fetch(path: str, params: dict) -> dict:
    err = QlooError("network", "Qloo could not be reached.")
    for attempt in range(MAX_ATTEMPTS):
        try:
            r = _session().get(f"{BASE}{path}", params=params, timeout=TIMEOUT)
        except requests.Timeout:
            err = QlooError("timeout", f"Qloo did not answer within {TIMEOUT}s.")
        except requests.RequestException as e:
            err = QlooError("network", f"Qloo could not be reached ({type(e).__name__}).")
        else:
            if r.status_code == 200:
                return r.json()
            if r.status_code in (401, 403):
                raise QlooError("auth", "Qloo rejected the API key. Check the key and "
                                        "that the host is hackathon.api.qloo.com.")
            if r.status_code == 429:
                # Never loop on 429: honour a short Retry-After once, else stop.
                wait = r.headers.get("Retry-After", "")
                if wait.isdigit() and int(wait) <= 10 and attempt < MAX_ATTEMPTS - 1:
                    time.sleep(int(wait))
                    continue
                raise QlooError("rate_limit", "Qloo rate limit reached. Try again in a minute.")
            if 400 <= r.status_code < 500:
                raise QlooError("bad_request", f"Qloo refused the query: {r.text[:300]}")
            err = QlooError("server", f"Qloo returned HTTP {r.status_code}.")
        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(BACKOFF * 2 ** attempt)
    raise err


# ---------------------------------------------------------------- cache

def _db() -> sqlite3.Connection:
    con = sqlite3.connect(CACHE_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, path TEXT, "
                "params TEXT, body TEXT, fetched_at REAL)")
    return con


def cached_get(path: str, params: dict) -> tuple[dict, dict]:
    """Return (body, meta). meta says whether it came from cache and if it is stale."""
    canon = json.dumps(params, sort_keys=True)
    key = hashlib.sha256(f"{path}?{canon}".encode()).hexdigest()
    with closing(_db()) as con:
        row = con.execute("SELECT body, fetched_at FROM cache WHERE key=?", (key,)).fetchone()
    now = time.time()
    if row and now - row[1] < CACHE_TTL:
        return json.loads(row[0]), {"cached": True, "stale": False, "fetched_at": row[1]}
    try:
        body = _fetch(path, params)
    except QlooError as e:
        if row and e.kind not in ("bad_request", "auth"):
            # Serve the old answer rather than nothing, and say so.
            return json.loads(row[0]), {"cached": True, "stale": True,
                                        "fetched_at": row[1], "warning": str(e)}
        raise
    with closing(_db()) as con, con:
        con.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?)",
                    (key, path, canon, json.dumps(body), now))
    return body, {"cached": False, "stale": False, "fetched_at": now}


# ---------------------------------------------------------------- normalise

def _items(body) -> list:
    results = body.get("results", body) if isinstance(body, dict) else body
    if isinstance(results, dict):
        results = results.get("entities") or results.get("tags") or []
    return results or []


def _normalise(item: dict, rank: int) -> dict:
    q = item.get("query") or {}
    aff = q.get("affinity", item.get("affinity"))
    return {
        "rank": rank,
        "name": item.get("name", "?"),
        "id": item.get("entity_id") or item.get("tag_id") or item.get("id"),
        "affinity": round(aff, 3) if isinstance(aff, (int, float)) else None,
    }


_NOISE = {"fc", "f", "c", "the", "club", "football", "afc", "sc"}


def _words(text: str) -> set[str]:
    return set(re.sub(r"[^a-z0-9 ]", " ", text.lower().replace("'", "")).split()) - _NOISE


def resolve_seed(query: str) -> dict | None:
    """Find the club. A match counts only if every word of the query is in its name
    ("Mbabane Swallows" must not become "Swallow's Nest"). Prefer a brand entity: the
    probe showed place entities (e.g. Mamelodi Sundowns) are silently ignored as a signal."""
    body, _ = cached_get("/search", {"query": query, "take": 10})
    hits = _items(body)
    if not hits:
        return None
    want = _words(query)
    good = [h for h in hits if want and want <= _words(h.get("name", ""))]
    best = next((h for h in good if "urn:entity:brand" in (h.get("types") or [])), good[0] if good else hits[0])
    return {
        "query": query,
        "id": best.get("entity_id"),
        "name": best.get("name"),
        "types": best.get("types") or [],
        "confident": bool(good),
        "alternatives": [h.get("name") for h in hits[:5] if h is not best],
    }


def signal_set(kind: str, audience: Audience, seed_id: str | None = None, take: int = 10) -> dict:
    params = {"filter.type": TYPES[kind], "take": take, **audience.params()}
    if seed_id:
        params["signal.interests.entities"] = seed_id
    body, meta = cached_get("/v2/insights", params)
    raw = _items(body)
    items, excluded, set_aside = [], 0, []
    for rank, it in enumerate(raw, 1):          # rank = Qloo's own position, kept as-is
        name = it.get("name", "").strip()
        if name.lower() in EXCLUDE:
            excluded += 1
        elif any(w in name.lower() for w in OFF_BRIEF):
            set_aside.append(name)
        else:
            items.append(_normalise(it, rank))
    return {"kind": kind, "params": params, "items": items, "count": len(items), "returned": len(raw),
            "excluded": excluded, "set_aside": set_aside, "thin": len(items) < THIN, **meta}


# ---------------------------------------------------------------- evidence

def build_evidence(audience: Audience = Audience(), seed_query: str | None = None,
                   take: int = 10) -> dict:
    """Everything the brief needs, in one JSON-able dict. Raises only on auth."""
    ev = {"audience": asdict(audience), "seed": None, "seed_effect": None,
          "signals": {}, "baseline": {}, "warnings": [], "built_at": time.time()}

    def attempt(fn, label):
        try:
            return fn()
        except QlooError as e:
            if e.kind == "auth":
                raise
            ev["warnings"].append(f"{label}: {e}")
            return None

    if seed_query:
        ev["seed"] = attempt(lambda: resolve_seed(seed_query), "club lookup")
        if ev["seed"] is None and not ev["warnings"]:
            ev["warnings"].append(f"Qloo has no match for '{seed_query}'; using the audience only.")
        elif ev["seed"] and not ev["seed"]["confident"]:
            kind = (ev["seed"]["types"] or ["entity"])[0].split(":")[-1]
            ev["warnings"].append(f"Qloo's closest match for '{seed_query}' is '{ev['seed']['name']}' (a {kind}), "
                                  "which isn't the same thing, so FanDNA used the audience only.")

    seed_id = (ev["seed"] or {}).get("id") if (ev["seed"] or {}).get("confident") else None
    for kind in TYPES:
        base = attempt(lambda: signal_set(kind, audience, None, take), f"{kind} (audience)")
        if base:
            ev["baseline"][kind] = base
        if seed_id:
            seeded = attempt(lambda: signal_set(kind, audience, seed_id, take), f"{kind} (club)")
            if seeded:
                base_names = {i["name"] for i in (base or {}).get("items", [])}
                seeded["new_vs_baseline"] = [i["name"] for i in seeded["items"]
                                             if i["name"] not in base_names]
                ev["signals"][kind] = seeded

    if seed_id:
        ev["seed_effect"] = any(s.get("new_vs_baseline") for s in ev["signals"].values())
        if not ev["seed_effect"]:
            ev["warnings"].append(f"Adding '{ev['seed']['name']}' did not change Qloo's results; "
                                  "the brief uses the audience only.")
            ev["signals"] = {}
    if not ev["signals"]:
        ev["signals"] = ev["baseline"]

    for kind, s in ev["signals"].items():
        if s["thin"]:
            ev["warnings"].append(f"Only {s['count']} {kind} returned; treat them as weak evidence.")
        if s.get("stale"):
            ev["warnings"].append(f"{kind} are from an older cached result ({s['warning']}).")
    return ev


if __name__ == "__main__":
    ev = build_evidence(seed_query=sys.argv[1] if len(sys.argv) > 1 else None)
    seed = ev["seed"]
    print(f"Seed: {seed['name']} {seed['types']}" if seed else "Seed: none")
    print(f"Seed changed results: {ev['seed_effect']}")
    for kind, s in ev["signals"].items():
        src = "stale cache" if s.get("stale") else "cache" if s["cached"] else "live"
        top = ", ".join(i["name"] for i in s["items"][:5]) or "(empty)"
        print(f"  {kind:<8} {s['count']:>2} [{src}]  {top}")
        if s.get("new_vs_baseline"):
            print(f"           club-specific: {', '.join(s['new_vs_baseline'][:5])}")
    for w in ev["warnings"]:
        print(f"  ! {w}")
    Path("evidence.json").write_text(json.dumps(ev, indent=2, ensure_ascii=False))
    print("Wrote evidence.json")
