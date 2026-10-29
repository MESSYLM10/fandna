"""Offline tests for qloo.py. No key or network needed: python -m pytest -q"""
import os
import tempfile

import pytest
import requests

os.environ["FANDNA_CACHE"] = os.path.join(tempfile.mkdtemp(), "t.sqlite")
import qloo  # noqa: E402

qloo.BACKOFF = 0

BASE_ARTISTS = ["Khuli Chana", "Tekno", "Runtown", "Riky Rick", "Oskido"]
BASE_BRANDS = ["Grindr", "Topman", "BMW", "ORO", "Tag Heuer", "Nike"]
CHIEFS_BRANDS = ["Hunter's Cider", "Power Play", "TelkomSA", "Cell C", "Bathu"]


class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = str(body)

    def json(self):
        return self._body


def entities(names):
    return {"results": {"entities": [
        {"name": n, "entity_id": f"id-{n}", "query": {"affinity": 0.9}} for n in names]}}


class FakeQloo:
    """Answers like the hackathon host did in the 27 Oct probe."""

    def __init__(self):
        self.calls = []
        self.script = []  # optional queued responses / exceptions, used first

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params)))
        if self.script:
            nxt = self.script.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        if url.endswith("/search"):
            q = params["query"]
            if q == "Mamelodi Sundowns":
                return Resp(200, {"results": [
                    {"name": "Mamelodi Sundowns FC", "entity_id": "sun-place",
                     "types": ["urn:entity:place"]}]})
            if q == "Nobody FC":
                return Resp(200, {"results": []})
            return Resp(200, {"results": [
                {"name": "Kaizer Chiefs Village", "entity_id": "kc-place", "types": ["urn:entity:place"]},
                {"name": "Kaizer Chiefs F.C.", "entity_id": "kc-brand", "types": ["urn:entity:brand"]}]})
        seed = params.get("signal.interests.entities")
        ftype = params["filter.type"]
        if ftype == "urn:tag":
            if seed == "sun-place":
                return Resp(200, {"results": {"tags": []}})
            return Resp(200, {"results": {"tags": [
                {"name": n, "tag_id": f"t-{n}", "query": {"affinity": 0.83}}
                for n in (["Local Heritage", "Youth-Centric", "Rugby", "Dramatic", "Cultural Pride"]
                          if seed == "kc-brand" else ["Motorcycle", "Momo", "Rugby", "Commercial", "Car & SUV"])]}})
        if ftype == "urn:entity:brand":
            return Resp(200, entities(CHIEFS_BRANDS if seed == "kc-brand" else BASE_BRANDS))
        if ftype == "urn:entity:place":
            return Resp(200, entities(
                ["Jojo Rooftop Lounge", "Orlando Amstel Arena", "SUD Restaurant", "Views at Twenty5", "Patong Guest Lodge"]
                if seed == "kc-brand" else ["Sotano", "Views at Twenty5", "Colony Arms", "Radisson Blu Sandton", "Cradle Moon"]))
        return Resp(200, entities(BASE_ARTISTS if seed != "kc-brand" else ["D'Banj", "Khuli Chana", "Wande Coal", "Ryan Leslie", "Lalah Hathaway"]))


@pytest.fixture(autouse=True)
def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(qloo, "CACHE_PATH", str(tmp_path / "c.sqlite"))
    f = FakeQloo()
    monkeypatch.setattr(qloo, "_SESSION", f)
    monkeypatch.setattr(qloo.time, "sleep", lambda s: None)
    return f


def test_club_seed_prefers_brand_and_finds_club_specific_signals(fake):
    ev = qloo.build_evidence(seed_query="Kaizer Chiefs")
    assert ev["seed"]["id"] == "kc-brand"
    assert ev["seed_effect"] is True
    assert ev["signals"]["brands"]["new_vs_baseline"] == CHIEFS_BRANDS
    assert "Local Heritage" in ev["signals"]["tags"]["new_vs_baseline"]
    assert len(fake.calls) == 9  # 1 search + 4 baseline + 4 seeded


def test_repeat_run_makes_zero_calls_and_says_cached(fake):
    qloo.build_evidence(seed_query="Kaizer Chiefs")
    n = len(fake.calls)
    ev = qloo.build_evidence(seed_query="Kaizer Chiefs")
    assert len(fake.calls) == n
    assert all(s["cached"] for s in ev["signals"].values())


def test_seed_that_changes_nothing_falls_back_to_audience(fake):
    ev = qloo.build_evidence(seed_query="Mamelodi Sundowns")
    assert ev["seed_effect"] is False
    assert ev["signals"] is ev["baseline"]
    assert any("did not change" in w for w in ev["warnings"])


def test_unknown_club_uses_audience_only(fake):
    ev = qloo.build_evidence(seed_query="Nobody FC")
    assert ev["seed"] is None
    assert any("no match" in w for w in ev["warnings"])
    assert ev["signals"]["artists"]["count"] == 5


def test_excluded_brand_is_dropped_and_counted(fake):
    s = qloo.signal_set("brands", qloo.Audience())
    assert "Grindr" not in [i["name"] for i in s["items"]]
    assert s["excluded"] == 1 and s["items"][0]["rank"] == 2   # Qloo rank kept


def test_thin_results_are_flagged(fake):
    fake.script = [Resp(200, entities(["Only One"]))]
    s = qloo.signal_set("artists", qloo.Audience())
    assert s["thin"] is True


def test_401_raises_auth_without_retry(fake):
    fake.script = [Resp(401, "unauthorised")]
    with pytest.raises(qloo.QlooError) as e:
        qloo.build_evidence()
    assert e.value.kind == "auth" and len(fake.calls) == 1


def test_429_without_retry_after_is_not_retried(fake):
    fake.script = [Resp(429, "slow down")]
    with pytest.raises(qloo.QlooError) as e:
        qloo.signal_set("artists", qloo.Audience())
    assert e.value.kind == "rate_limit" and len(fake.calls) == 1


def test_429_with_short_retry_after_retries_once(fake):
    fake.script = [Resp(429, "x", {"Retry-After": "2"})]
    s = qloo.signal_set("artists", qloo.Audience())
    assert s["count"] == 5 and len(fake.calls) == 2


def test_timeouts_retry_twice_then_fail(fake):
    fake.script = [requests.Timeout()] * 3
    with pytest.raises(qloo.QlooError) as e:
        qloo.signal_set("artists", qloo.Audience())
    assert e.value.kind == "timeout" and len(fake.calls) == 3


def test_stale_cache_is_served_when_qloo_is_down(fake, monkeypatch):
    qloo.signal_set("artists", qloo.Audience())
    monkeypatch.setattr(qloo, "CACHE_TTL", -1)  # everything is now stale
    fake.script = [Resp(503, "down")] * 3
    s = qloo.signal_set("artists", qloo.Audience())
    assert s["stale"] is True and s["count"] == 5


def test_one_failing_type_becomes_a_warning_not_a_crash(fake):
    fake.script = [Resp(400, "bad filter")]  # first insights call (artists baseline)
    ev = qloo.build_evidence()
    assert "artists" not in ev["signals"]
    assert any(w.startswith("artists") for w in ev["warnings"])
    assert ev["signals"]["brands"]["count"] == 5


def test_other_sports_are_set_aside_not_sent(fake):
    ev = qloo.build_evidence(seed_query="Kaizer Chiefs")
    tags = ev["signals"]["tags"]
    assert "Rugby" not in [i["name"] for i in tags["items"]]
    assert tags["set_aside"] == ["Rugby"]
    assert [i["rank"] for i in tags["items"]][:3] == [1, 2, 4]   # gap where Rugby was


def test_wrong_club_match_is_rejected(fake):
    fake.script = [Resp(200, {"results": [
        {"name": "Swallow's Nest", "entity_id": "nest", "types": ["urn:entity:place"]}]})]
    ev = qloo.build_evidence(seed_query="Mbabane Swallows")
    assert ev["seed"]["confident"] is False and ev["seed_effect"] is None
    assert not any(c[1].get("signal.interests.entities") for c in fake.calls)   # never used as a seed
    assert "isn't the same thing" in ev["warnings"][0]


def test_club_name_with_fc_suffix_still_matches(fake):
    ev = qloo.build_evidence(seed_query="Kaizer Chiefs")
    assert ev["seed"]["confident"] and ev["seed"]["id"] == "kc-brand"
