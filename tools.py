"""
FanDNA's Qloo tools, shared by the Ask FanDNA agent (agent.py) and the MCP server (mcp_server.py).

Each tool returns small JSON (names and Qloo ranks, no raw payloads), so an agent stays
inside tight token limits. All calls go through qloo.py: cached, retried safely, other
sports set aside, and a club is used only when Qloo's match is really that club.
"""
from __future__ import annotations

import qloo

KINDS = tuple(qloo.TYPES)                       # artists, brands, places, tags
AGES = ("35_and_younger", "36_to_55", "55_and_older")
GENDERS = {"men": "male", "male": "male", "women": "female", "female": "female",
           "everyone": "", "all": "", "": ""}
VERIFIED_CITIES = ("Johannesburg",)             # see live_check.py --cities
TOP = 8


def _audience(age: str, gender: str, city: str) -> tuple[qloo.Audience, list[str]]:
    notes = []
    city = (city or "Johannesburg").strip()
    if city not in VERIFIED_CITIES:
        notes.append(f"Qloo's location data for '{city}' is unverified; results may not be local. "
                     "Johannesburg is the verified city.")
    if age not in AGES:
        notes.append(f"Age '{age}' is not a Qloo band; used 35_and_younger.")
        age = "35_and_younger"
    return qloo.Audience(city, GENDERS.get((gender or "").strip().lower(), ""), age), notes


def _describe(a: qloo.Audience) -> str:
    who = {"male": "men", "female": "women"}.get(a.gender, "people")
    return f"{who}, {a.age.replace('_', ' ')}, {a.location}"


def _club(name: str) -> tuple[dict | None, str | None]:
    seed = qloo.resolve_seed(name)
    if not seed:
        return None, f"Qloo has no match for '{name}'."
    if not seed["confident"]:
        return None, f"Qloo's closest match for '{name}' is '{seed['name']}', which isn't the same thing."
    return seed, None


def _kind(kind: str) -> str:
    k = (kind or "").strip().lower()
    k = {"artist": "artists", "brand": "brands", "place": "places", "tag": "tags", "music": "artists",
         "venues": "places", "taste tags": "tags"}.get(k, k)
    if k not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    return k


def find_entity(name: str) -> dict:
    """Search Qloo for an artist, brand, place, team or other entity by name."""
    body, _ = qloo.cached_get("/search", {"query": name, "take": 5})
    return {"query": name, "matches": [
        {"name": h.get("name"), "id": h.get("entity_id"),
         "type": ((h.get("types") or ["?"])[0]).split(":")[-1]} for h in qloo._items(body)[:5]]}


def audience_tastes(kind: str, club: str = "", age: str = "35_and_younger",
                    gender: str = "men", city: str = "Johannesburg") -> dict:
    """What an audience ranks highest in Qloo, optionally narrowed to a club's fans."""
    kind = _kind(kind)
    aud, notes = _audience(age, gender, city)
    base = qloo.signal_set(kind, aud)
    out = {"kind": kind, "audience": _describe(aud),
           "whole_audience": [i["name"] for i in base["items"][:TOP]]}
    if club:
        seed, err = _club(club)
        if err:
            notes.append(err + " Showing the whole audience only.")
        else:
            seeded = qloo.signal_set(kind, aud, seed["id"])
            names = {i["name"] for i in base["items"]}
            out["club"] = seed["name"]
            out["club_fans"] = [{"rank": i["rank"], "name": i["name"], "club_specific": i["name"] not in names}
                                for i in seeded["items"][:TOP]]
            if not any(x["club_specific"] for x in out["club_fans"]):
                notes.append(f"Adding {seed['name']} did not change Qloo's results for {kind}.")
    set_aside = base.get("set_aside", [])
    if set_aside:
        notes.append("Set aside as another sport: " + ", ".join(set_aside))
    if notes:
        out["notes"] = notes
    return out


def compare_clubs(kind: str, club_a: str, club_b: str, age: str = "35_and_younger",
                  gender: str = "men", city: str = "Johannesburg") -> dict:
    """How two clubs' fans differ in Qloo for one kind of signal (same audience for both)."""
    kind = _kind(kind)
    aud, notes = _audience(age, gender, city)
    lists, names = {}, {}
    for label, club in (("a", club_a), ("b", club_b)):
        seed, err = _club(club)
        if err:
            return {"kind": kind, "error": err}
        names[label] = seed["name"]
        lists[label] = [i["name"] for i in qloo.signal_set(kind, aud, seed["id"])["items"][:10]]
    a, b = lists["a"], lists["b"]
    out = {"kind": kind, "audience": _describe(aud), "club_a": names["a"], "club_b": names["b"],
           "shared": [n for n in a if n in b],
           "only_a": [n for n in a if n not in b],
           "only_b": [n for n in b if n not in a]}
    if notes:
        out["notes"] = notes
    return out


def _distribution(obj, key: str) -> dict | None:
    """Find the first {band: number} mapping under `key` anywhere in a Qloo payload."""
    if isinstance(obj, dict):
        v = obj.get(key)
        if isinstance(v, dict) and v and all(isinstance(x, (int, float)) for x in v.values()):
            return v
        for x in obj.values():
            found = _distribution(x, key)
            if found:
                return found
    elif isinstance(obj, list):
        for x in obj:
            found = _distribution(x, key)
            if found:
                return found
    return None


def entity_fans(name: str) -> dict:
    """Who over-indexes on an entity in Qloo: age and gender affinity of its audience."""
    hit = qloo.resolve_seed(name)
    if not hit:
        return {"entity": name, "note": f"Qloo has no match for '{name}'."}
    if not hit["confident"]:
        return {"entity": name, "note": f"Qloo's closest match is '{hit['name']}', which isn't the same thing."}
    try:
        body, _ = qloo.cached_get("/v2/insights", {"filter.type": "urn:demographics",
                                                   "signal.interests.entities": hit["id"]})
    except qloo.QlooError as e:
        if e.kind == "auth":
            raise
        return {"entity": hit["name"], "note": f"Qloo returned no demographic breakdown ({e.kind})."}
    age, gender = _distribution(body, "age"), _distribution(body, "gender")
    if not age and not gender:
        return {"entity": hit["name"], "note": "Qloo returned no demographic breakdown for this entity."}
    rnd = lambda d: {k: round(v, 2) for k, v in sorted(d.items(), key=lambda kv: -kv[1])} if d else {}
    return {"entity": hit["name"], "age_affinity": rnd(age), "gender_affinity": rnd(gender),
            "how_to_read": "Positive means this entity's audience over-indexes on that group; negative, under-indexes."}


TOOLS = {"find_entity": find_entity, "audience_tastes": audience_tastes,
         "compare_clubs": compare_clubs, "entity_fans": entity_fans}


def names_in(result) -> set[str]:
    """Every entity or tag name a tool result contains: what an answer may cite."""
    found = set()
    if isinstance(result, dict):
        for k, v in result.items():
            if k in ("name", "club", "club_a", "club_b", "entity") and isinstance(v, str):
                found.add(v)
            elif k in ("whole_audience", "shared", "only_a", "only_b") and isinstance(v, list):
                found.update(x for x in v if isinstance(x, str))
            else:
                found |= names_in(v)
    elif isinstance(result, list):
        for x in result:
            found |= names_in(x)
    return found
