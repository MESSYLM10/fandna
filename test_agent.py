"""Offline tests for tools.py and agent.py. No keys or network."""
import json
from types import SimpleNamespace

import groq
import pytest

import agent
import brief
import qloo
import tools
from test_brief import err
from test_qloo import FakeQloo, Resp, entities


class FakeQloo2(FakeQloo):
    """Adds Orlando Pirates, Bathu and Qloo's demographics response."""

    def get(self, url, params=None, timeout=None):
        if url.endswith("/search") and params["query"] in ("Orlando Pirates", "Bathu"):
            self.calls.append((url, dict(params)))
            q = params["query"]
            return Resp(200, {"results": [{"name": "Orlando Pirates F.C." if q == "Orlando Pirates" else "Bathu",
                                           "entity_id": "op" if q == "Orlando Pirates" else "id-Bathu",
                                           "types": ["urn:entity:brand"]}]})
        if url.endswith("/v2/insights") and params.get("filter.type") == "urn:demographics":
            self.calls.append((url, dict(params)))
            return Resp(200, {"results": {"demographics": [{"entity_id": "id-Bathu", "query": {
                "age": {"24_and_younger": 0.31, "25_to_29": 0.12, "55_and_older": -0.4},
                "gender": {"male": 0.22, "female": -0.22}}}]}})
        if params and params.get("signal.interests.entities") == "op" and params["filter.type"] == "urn:entity:brand":
            self.calls.append((url, dict(params)))
            return Resp(200, entities(["Hunter's Cider", "Carling Black Label", "TelkomSA", "Vodacom", "Bathu"]))
        return super().get(url, params, timeout)


@pytest.fixture
def fq(tmp_path, monkeypatch):
    monkeypatch.setattr(qloo, "CACHE_PATH", str(tmp_path / "c.sqlite"))
    monkeypatch.setenv("FANDNA_CACHE", str(tmp_path / "c.sqlite"))
    f = FakeQloo2()
    monkeypatch.setattr(qloo, "_SESSION", f)
    return f


# ---------------------------------------------------------------- tools

def test_audience_tastes_marks_club_specific(fq):
    r = tools.audience_tastes("brands", club="Kaizer Chiefs")
    assert r["club"] == "Kaizer Chiefs F.C."
    assert {x["name"]: x["club_specific"] for x in r["club_fans"]}["Bathu"] is True
    assert "Grindr" not in r["whole_audience"]


def test_audience_tastes_flags_unverified_city_and_wrong_club(fq):
    r = tools.audience_tastes("artists", club="Mbabane Swallows", city="Durban")
    assert "club_fans" not in r
    assert any("unverified" in n for n in r["notes"]) and any("isn't the same thing" in n for n in r["notes"])


def test_compare_clubs(fq):
    r = tools.compare_clubs("brands", "Kaizer Chiefs", "Orlando Pirates")
    assert r["club_b"] == "Orlando Pirates F.C."
    assert "Hunter's Cider" in r["shared"] and "Power Play" in r["only_a"] and "Vodacom" in r["only_b"]


def test_entity_fans_reads_demographics(fq):
    r = tools.entity_fans("Bathu")
    assert next(iter(r["age_affinity"])) == "24_and_younger" and r["gender_affinity"]["male"] == 0.22


def test_bad_kind_is_a_clear_error(fq):
    with pytest.raises(ValueError):
        tools.audience_tastes("cars")
    assert tools.audience_tastes("music")["kind"] == "artists"


def test_names_in_collects_citable_names(fq):
    names = tools.names_in(tools.compare_clubs("brands", "Kaizer Chiefs", "Orlando Pirates"))
    assert {"Vodacom", "Power Play", "Kaizer Chiefs F.C."} <= names


# ---------------------------------------------------------------- agent

def call(name, args, cid="c1"):
    return SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def reply(content=None, calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))],
                           usage=SimpleNamespace(total_tokens=900))


class Model:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def test_agent_calls_tool_answers_and_checks_names(fq, monkeypatch):
    m = Model([reply(calls=[call("compare_clubs", {"kind": "brands", "club_a": "Kaizer Chiefs",
                                                   "club_b": "Orlando Pirates"})]),
               reply("Both rank **Hunter's Cider**. Only Chiefs fans rank **Power Play**; Pirates fans rank "
                     "**Vodacom** and **Adidas**.")])
    monkeypatch.setattr(brief, "_CLIENT", m)
    r = agent.ask("How do Chiefs and Pirates fans differ in brands?")
    assert [s["tool"] for s in r["steps"]] == ["compare_clubs"]
    assert "shared" in r["steps"][0]["summary"]
    assert r["verified"] == ["Hunter's Cider", "Power Play", "Vodacom"] and r["unverified"] == ["Adidas"]
    assert m.calls[0]["tools"] == agent.SCHEMAS and m.calls[0]["reasoning_effort"] == "low"
    tool_msg = m.calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and "Vodacom" in tool_msg["content"]


def test_agent_tool_budget_forces_an_answer(fq, monkeypatch):
    loop = [reply(calls=[call("audience_tastes", {"kind": "brands"}, f"c{i}")]) for i in range(3)]
    m = Model(loop + [reply("Done with **Topman**.")])
    monkeypatch.setattr(brief, "_CLIENT", m)
    r = agent.ask("Keep looking things up")
    assert len(r["steps"]) == agent.MAX_TOOL_CALLS
    assert "tools" not in m.calls[-1]                     # final call can't call tools
    assert r["verified"] == ["Topman"]


def test_agent_bad_arguments_become_tool_errors(fq, monkeypatch):
    m = Model([reply(calls=[call("audience_tastes", {"kind": "cars"})]), reply("Qloo has no such kind.")])
    monkeypatch.setattr(brief, "_CLIENT", m)
    r = agent.ask("cars?")
    assert r["steps"][0]["summary"].startswith("stopped: kind must be one of")


def test_agent_falls_back_on_rate_limit_and_caches(fq, monkeypatch):
    m = Model([err(groq.RateLimitError, 429, "Please try again in 9s."), reply("No tools needed.")])
    monkeypatch.setattr(brief, "_CLIENT", m)
    a = agent.ask("Hello?")
    assert a["model"] == "openai/gpt-oss-20b" and not a["cached"]
    b = agent.ask("hello?")                               # same question, different case: cached
    assert b["cached"] and len(m.calls) == 2


def test_agent_all_models_limited_reports_wait(fq, monkeypatch):
    m = Model([err(groq.RateLimitError, 429, "try again in 12s")] * 2)
    monkeypatch.setattr(brief, "_CLIENT", m)
    with pytest.raises(brief.BriefError) as e:
        agent.ask("Anything", fresh=True)
    assert e.value.kind == "rate_limit" and e.value.retry_after == 12.0


def test_transcribe_falls_back_and_reports_missing_whisper(fq, monkeypatch):
    calls = []

    def create(**kw):
        calls.append(kw["model"])
        if kw["model"] == "whisper-large-v3-turbo":
            raise err(groq.NotFoundError, 404, "model not found")
        return SimpleNamespace(text=" Who are Bathu's fans? ")

    monkeypatch.setattr(brief, "_CLIENT", SimpleNamespace(audio=SimpleNamespace(
        transcriptions=SimpleNamespace(create=create))))
    assert agent.transcribe(b"RIFF....") == "Who are Bathu's fans?"
    assert calls == ["whisper-large-v3-turbo", "whisper-large-v3"]

    def none(**kw):
        raise err(groq.NotFoundError, 404, "model not found")

    monkeypatch.setattr(brief, "_CLIENT", SimpleNamespace(audio=SimpleNamespace(
        transcriptions=SimpleNamespace(create=none))))
    with pytest.raises(brief.BriefError) as e:
        agent.transcribe(b"x")
    assert "Type the question instead" in str(e.value)
