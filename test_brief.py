"""Offline tests for brief.py. No key or network needed: python -m pytest -q"""
import json
from types import SimpleNamespace

import groq
import httpx
import pytest

import brief
import qloo
from test_qloo import FakeQloo

GOOD = {
    "headline": "This audience pairs club loyalty with local streetwear and Soweto nightlife.",
    "concepts": [
        {"name": "Bathu Matchday Drop", "idea": "A limited sneaker drop. Sold at the stadium.",
         "evidence": [{"name": "Bathu", "type": "brands", "why": "Local sneaker brand."},
                      {"name": "local heritage", "type": "tags", "why": "Pride in local."},
                      {"name": "Adidas", "type": "brands", "why": "Kit sponsor."}],
         "activation": ["Design", "Announce", "Sell"], "channels": ["Instagram"],
         "budget": [{"item": "Stock", "share_pct": 50}, {"item": "Ads", "share_pct": 30},
                    {"item": "Stand", "share_pct": 30}],
         "kpis": ["Units sold"], "assumptions": ["Bathu would collaborate"]},
        {"name": "Arena Afterparty", "idea": "x",
         "evidence": [{"name": "Orlando Amstel Arena", "type": "places", "why": "Venue."},
                      {"name": "Khuli Chana", "type": "artists", "why": "Artist."}],
         "activation": [], "channels": [], "budget": [{"item": "Venue", "share_pct": 100}],
         "kpis": [], "assumptions": []},
        {"name": "Thin", "idea": "x", "evidence": [{"name": "Made Up Brand"}],
         "budget": [], "kpis": [], "assumptions": []},
    ],
    "evidence_gaps": [],
}


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(qloo, "CACHE_PATH", str(tmp_path / "c.sqlite"))
    monkeypatch.setattr(qloo, "_SESSION", FakeQloo())
    return qloo.build_evidence(seed_query="Kaizer Chiefs")


def err(cls, status, msg):
    r = httpx.Response(status, request=httpx.Request("POST", "https://api.groq.com"))
    return cls(msg, response=r, body=None)


class FakeGroq:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=nxt))],
                               usage=SimpleNamespace(total_tokens=2100))


def use(monkeypatch, *script):
    fake = FakeGroq(script)
    monkeypatch.setattr(brief, "_CLIENT", fake)
    return fake


ARGS = dict(club="Kaizer Chiefs F.C.", objective="Fill the stadium", budget_zar=250_000)


def test_citations_are_checked_against_qloo(evidence, monkeypatch):
    use(monkeypatch, json.dumps(GOOD))
    r = brief.write_brief(evidence, **ARGS)
    c1, c2, c3 = r["concepts"]
    assert [e["name"] for e in c1["evidence"]] == ["Bathu", "Local Heritage"]   # Qloo spelling
    assert c1["evidence"][1]["type"] == "tags"          # type taken from Qloo, not the model
    assert c1["unverified"] == ["Adidas"] and c1["grounded"]
    assert c3["unverified"] == ["Made Up Brand"] and not c3["grounded"]
    assert r["grounding"] == {"cited": 6, "verified": 4, "grounded_concepts": 2}


def test_budget_is_normalised_and_sums_exactly(evidence, monkeypatch):
    use(monkeypatch, json.dumps(GOOD))
    b = brief.write_brief(evidence, **ARGS)["concepts"][0]["budget"]   # shares summed to 110
    assert sum(x["amount_zar"] for x in b) == 250_000
    assert sum(x["share_pct"] for x in b) == 100


def test_rate_limit_falls_through_to_next_model_once(evidence, monkeypatch):
    fake = use(monkeypatch,
               err(groq.RateLimitError, 429, "Rate limit reached ... Please try again in 22.23s."),
               json.dumps(GOOD))
    r = brief.write_brief(evidence, models=["openai/gpt-oss-120b", "openai/gpt-oss-20b"], **ARGS)
    assert r["meta"]["model"] == "openai/gpt-oss-20b" and len(fake.calls) == 2
    assert fake.calls[0]["reasoning_effort"] == "low"


def test_all_models_rate_limited_reports_wait(evidence, monkeypatch):
    use(monkeypatch, err(groq.RateLimitError, 429, "Please try again in 22.23s."))
    with pytest.raises(brief.BriefError) as e:
        brief.write_brief(evidence, models=["m1"], **ARGS)
    assert e.value.kind == "rate_limit" and e.value.retry_after == 22.23


def test_auth_error_stops_immediately(evidence, monkeypatch):
    fake = use(monkeypatch, err(groq.AuthenticationError, 401, "bad key"))
    with pytest.raises(brief.BriefError) as e:
        brief.write_brief(evidence, models=["m1", "m2"], **ARGS)
    assert e.value.kind == "auth" and len(fake.calls) == 1


def test_non_json_tries_next_model(evidence, monkeypatch):
    use(monkeypatch, "Sure! Here are some ideas...", json.dumps(GOOD))
    r = brief.write_brief(evidence, models=["m1", "m2"], **ARGS)
    assert r["meta"]["model"] == "m2"


def test_generic_brief_claims_are_checked_too(evidence, monkeypatch):
    generic = {"headline": "h", "concepts": [{"name": "Kwaito night", "idea": "x",
               "evidence": [{"name": "Kwaito"}, {"name": "Castle Lager"}, {"name": "Khuli Chana"}],
               "budget": [{"item": "a", "share_pct": 1}]}]}
    fake = use(monkeypatch, json.dumps(generic))
    r = brief.generic_brief(evidence, **ARGS)
    assert r["grounding"]["verified"] == 1                # only Khuli Chana is in Qloo's results
    assert "EVIDENCE: none" in fake.calls[0]["messages"][1]["content"]
    assert fake.calls[0]["max_tokens"] == brief.GENERIC_MAX_TOKENS


def test_request_stays_small(evidence, monkeypatch):
    fake = use(monkeypatch, json.dumps(GOOD))
    brief.write_brief(evidence, **ARGS)
    chars = sum(len(m["content"]) for m in fake.calls[0]["messages"])
    sent = chars / 3.5            # conservative: English + JSON averages ~4 chars per token
    print(f"\nrequest ~{sent:.0f} tokens + {brief.MAX_TOKENS} max output")
    assert sent + brief.MAX_TOKENS < 4000, sent


def test_markdown_export(evidence, monkeypatch):
    use(monkeypatch, json.dumps(GOOD))
    r = brief.write_brief(evidence, **ARGS)
    md = brief.to_markdown(r, ARGS["club"], ARGS["budget_zar"])
    assert "so unproven: Adidas" in md and "R250,000" in md and "To validate before spending" in md


def test_key_falls_back_to_qloo_cli_auth_file(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"google": {"type": "api_key", "key": "g"},
                                "groq": {"type": "api_key", "key": " gsk_test "}}))
    monkeypatch.setattr(brief, "HARNESS_AUTH", auth)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert brief._groq_key() == "gsk_test"
    monkeypatch.setenv("GROQ_API_KEY", "from_env")
    assert brief._groq_key() == "from_env"              # env wins (deployment)
    auth.write_text("not json")
    monkeypatch.delenv("GROQ_API_KEY")
    assert brief._groq_key() == ""


def test_every_model_failure_is_reported(evidence, monkeypatch):
    use(monkeypatch,
        err(groq.RateLimitError, 429, "Please try again in 5s."),
        err(groq.BadRequestError, 400, "json_validate_failed"),
        err(groq.NotFoundError, 404, "model not found"))
    with pytest.raises(brief.BriefError) as e:
        brief.write_brief(evidence, models=["a", "b", "c"], **ARGS)
    msg = str(e.value)
    assert "a: rate limit" in msg and "b: HTTP 400" in msg and "c: not available" in msg
    assert "doctor.py" in msg and e.value.retry_after == 5.0


def test_compact_omits_empty_lists(evidence):
    view = brief.compact(evidence)
    assert view["evidence"]["brands"] == {"club_specific": [
        "Hunter's Cider", "Power Play", "TelkomSA", "Cell C", "Bathu"]}   # all club-specific
    assert all(v for lists in view["evidence"].values() for v in lists.values())


def test_brief_cache_reuses_and_regenerates(evidence, monkeypatch, tmp_path):
    monkeypatch.setenv("FANDNA_CACHE", str(tmp_path / "b.sqlite"))
    fake = use(monkeypatch, json.dumps(GOOD), json.dumps(GOOD))
    a = brief.cached_brief(evidence, **ARGS)
    b = brief.cached_brief(evidence, **ARGS)
    assert len(fake.calls) == 1 and not a["meta"]["cached"] and b["meta"]["cached"]
    c = brief.cached_brief(evidence, fresh=True, **ARGS)
    assert len(fake.calls) == 2 and not c["meta"]["cached"]
    fake.script.append(json.dumps(GOOD))
    brief.cached_brief(evidence, club=ARGS["club"], objective="Sell shirts", budget_zar=100_000)
    assert len(fake.calls) == 3                          # different inputs: new brief


def test_audience_gender_optional():
    assert "signal.demographics.gender" not in qloo.Audience(gender="").params()
    assert qloo.Audience().params()["signal.demographics.gender"] == "male"


NEW = {
    "headline": "h",
    "lead": {"concept": "Arena Warm-up", "why": "Cheapest to test."},
    "concepts": [
        {"name": "Cider Fan Zone", "idea": "A Hunter's Cider zone.",
         "evidence": [{"name": "Hunter's Cider", "reading": "A sponsor fit."},
                      {"name": "Khuli Chana", "reading": "A headline act."}],
         "activation": ["a", "b", "c", "d", "e"], "channels": ["x"],
         "budget": [{"item": "Zone", "share_pct": 70}, {"item": "Ads", "share_pct": 30}],
         "kpis": [{"metric": "Entries", "baseline": "Last match", "method": "Gate scans", "window": "Match day"},
                  "Plain string KPI"],
         "to_validate": ["Hunter's Cider agrees to sponsor"]},
        {"name": "Arena Warm-up", "idea": "x",
         "evidence": [{"name": "Orlando Amstel Arena", "reading": "r"}, {"name": "Topman", "reading": "r"}],
         "activation": ["a"], "channels": [], "budget": [{"item": "Venue", "share_pct": 100}],
         "kpis": [], "to_validate": []},
    ],
    "evidence_gaps": []}


def test_observed_lines_are_written_by_code(evidence, monkeypatch):
    use(monkeypatch, json.dumps(NEW))
    r = brief.write_brief(evidence, **ARGS)
    ev = {e["name"]: e for c in r["concepts"] for e in c["evidence"]}
    assert ev["Hunter's Cider"]["observed"] == ("Qloo: #1 among Kaizer Chiefs F.C. fans' brands; "
                                                "not in the whole audience's top 6.")  # 6 returned, 1 excluded
    assert ev["Khuli Chana"]["observed"].startswith("Qloo: #2 among Kaizer Chiefs F.C. fans' artists; also #1")
    assert ev["Topman"]["observed"].endswith("not specific to Kaizer Chiefs F.C. fans.")
    assert ev["Hunter's Cider"]["reading"] == "A sponsor fit."


def test_lead_concept_goes_first(evidence, monkeypatch):
    use(monkeypatch, json.dumps(NEW))
    r = brief.write_brief(evidence, **ARGS)
    assert [c["name"] for c in r["concepts"]] == ["Arena Warm-up", "Cider Fan Zone"]
    assert r["concepts"][0]["lead"] and r["lead"] == {"name": "Arena Warm-up", "why": "Cheapest to test."}


def test_alcohol_check_kpis_steps_and_contingency(evidence, monkeypatch):
    use(monkeypatch, json.dumps(NEW))
    c = {c["name"]: c for c in brief.write_brief(evidence, **ARGS)["concepts"]}["Cider Fan Zone"]
    assert c["to_validate"][-1] == brief.ALCOHOL_CHECK
    assert len(c["activation"]) == 4
    assert c["kpis"][1] == {"metric": "Plain string KPI", "baseline": "", "method": "", "window": ""}
    assert c["budget"][-1] == {"item": "Contingency, held back", "amount_zar": 25_000,
                               "contingency": True, "share_pct": 10}
    assert sum(b["amount_zar"] for b in c["budget"]) == 250_000
    assert sum(b["share_pct"] for b in c["budget"]) == 100
    assert c["budget"][0]["amount_zar"] == 157_500                      # 70% of the R225,000 working budget


def test_model_is_told_the_working_budget(evidence, monkeypatch):
    fake = use(monkeypatch, json.dumps(NEW))
    brief.write_brief(evidence, **ARGS)
    assert "WORKING BUDGET: R225,000 (R250,000 total; 10% held back" in fake.calls[0]["messages"][1]["content"]


def test_partner_and_venue_checks_added_when_missing(evidence, monkeypatch):
    use(monkeypatch, json.dumps(NEW))
    c = {c["name"]: c for c in brief.write_brief(evidence, **ARGS)["concepts"]}
    arena = c["Arena Warm-up"]["to_validate"]
    assert "Orlando Amstel Arena is available on the date, and its hire cost." in arena
    assert "Topman is willing to take part, and at what cost." in arena
    cider = c["Cider Fan Zone"]["to_validate"]
    assert not any(x.startswith("Hunter's Cider is willing") for x in cider)   # already covered by the model


BAD = {
    "headline": "h", "lead": {"concept": "Rooftop", "why": "w"},
    "concepts": [{"name": "Rooftop", "idea": "x",
                  "evidence": [{"name": "Jojo Rooftop Lounge", "reading": "The audience frequents this venue."},
                               {"name": "Local Heritage", "reading": "Celebrates local culture."}],
                  "activation": ["a"], "channels": [],
                  "budget": [{"item": "Venue", "share_pct": 90}, {"item": "Contingency reserve", "share_pct": 10}],
                  "kpis": [{"metric": "Attendance", "baseline": "average attendance of 20,000", "method": "m", "window": "w"},
                           {"metric": "Sales", "baseline": "last comparable home match", "method": "m", "window": "w"}],
                  "to_validate": ["Jojo Rooftop Lounge availability"]}],
    "evidence_gaps": []}


def test_model_rule_breaks_are_corrected_in_code(evidence, monkeypatch):
    use(monkeypatch, json.dumps(BAD))
    c = brief.write_brief(evidence, **ARGS)["concepts"][0]
    jojo, heritage = c["evidence"]
    assert jojo["rewritten"] and "frequents" not in jojo["reading"]
    assert not heritage["rewritten"] and heritage["reading"] == "Celebrates local culture."
    items = [b["item"] for b in c["budget"]]
    assert items == ["Venue", "Contingency, held back"]          # the model's own reserve is dropped
    assert c["budget"][0]["amount_zar"] == 225_000
    assert c["kpis"][0]["baseline"].endswith(brief.UNVERIFIED_FIGURE)
    assert c["kpis"][1]["baseline"] == "last comparable home match"
    assert not brief.BEHAVIOUR.search("Streetwear and the stadium")   # no false alarm on 'streetwear'
