"""The reviewed demo survives a move to a fresh host."""
import json

import brief
import demo_seed
import qloo
from test_brief import FakeGroq, NEW
from test_qloo import FakeQloo


def test_seed_roundtrip_serves_reviewed_brief_without_api_calls(tmp_path, monkeypatch):
    laptop, host = str(tmp_path / "laptop.sqlite"), str(tmp_path / "host.sqlite")
    monkeypatch.setattr(demo_seed, "SEED", tmp_path / "demo_seed.json")

    # On the laptop: build and review the demo.
    monkeypatch.setenv("FANDNA_CACHE", laptop)
    monkeypatch.setattr(qloo, "CACHE_PATH", laptop)
    monkeypatch.setattr(qloo, "_SESSION", FakeQloo())
    monkeypatch.setattr(brief, "_CLIENT", FakeGroq([json.dumps(NEW)]))
    ev = qloo.build_evidence(seed_query="Kaizer Chiefs")
    reviewed = brief.cached_brief(ev, "Kaizer Chiefs F.C.", "Fill the stadium", 250_000)
    assert demo_seed.export(laptop) == {"qloo_results": 9, "briefs": 1, "answers": 0}

    # On a fresh host: empty cache, APIs that must not be called.
    monkeypatch.setenv("FANDNA_CACHE", host)
    monkeypatch.setattr(qloo, "CACHE_PATH", host)
    dead_qloo, dead_groq = FakeQloo(), FakeGroq([])
    monkeypatch.setattr(qloo, "_SESSION", dead_qloo)
    monkeypatch.setattr(brief, "_CLIENT", dead_groq)
    assert demo_seed.load_if_empty(host) == 10
    assert demo_seed.load_if_empty(host) == 0                    # only once
    ev2 = qloo.build_evidence(seed_query="Kaizer Chiefs")
    served = brief.cached_brief(ev2, "Kaizer Chiefs F.C.", "Fill the stadium", 250_000)
    assert dead_qloo.calls == [] and dead_groq.calls == []
    assert served["meta"]["cached"] and served["concepts"] == reviewed["concepts"]
