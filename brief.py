"""
FanDNA brief: one Groq call turns Qloo evidence into three campaign concepts.

    from brief import write_brief, generic_brief
    result = write_brief(evidence, club="Kaizer Chiefs",
                         objective="Fill the stadium for the next home match", budget_zar=250_000)

The code, not the model, does the parts a model gets wrong:
  - every cited signal is checked against the real Qloo evidence;
    anything not found is moved to "unverified" and counted
  - budget shares are normalised to 100% and turned into rand amounts here

Token budget: about 1,000 input + at most 2,400 output per brief, well under
an 8,000 tokens-per-minute limit. generic_brief() (the "without Qloo"
comparison) is capped lower and should only run when the user asks for it.

Environment:
    GROQ_API_KEY   optional locally: falls back to ~/.qloo/agent/auth.json (qloo CLI)
    GROQ_MODELS    comma-separated, tried in order on rate limit or missing model
                   default: openai/gpt-oss-120b,openai/gpt-oss-20b

CLI (uses evidence.json written by `python qloo.py "Kaizer Chiefs"`):
    python brief.py                      write a brief, print it
    python brief.py --models             list models your key can use
    python brief.py --compare M1 M2 ...  same evidence on each model: tokens, time, grounding
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
from pathlib import Path

PROMPT = (Path(__file__).parent / "prompt.md").read_text()
MODELS = [m.strip() for m in os.environ.get(
    "GROQ_MODELS", "openai/gpt-oss-120b,openai/gpt-oss-20b").split(",") if m.strip()]
MAX_TOKENS = 2400
GENERIC_MAX_TOKENS = 1200
PER_KIND = 8          # evidence items per type sent to the model
GENERIC_NOTE = ("\n\nEVIDENCE is 'none' for this request. Use your general knowledge instead: "
                "put what you believe this audience likes in each concept's \"evidence\".")


class BriefError(Exception):
    """kind is one of: auth, rate_limit, timeout, bad_output, config."""

    def __init__(self, kind: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.kind, self.retry_after = kind, retry_after


_CLIENT = None


HARNESS_AUTH = Path.home() / ".qloo" / "agent" / "auth.json"


def _groq_key() -> str:
    """GROQ_API_KEY, else the key the qloo CLI saved when you configured Groq.
    The key is read, never printed or written anywhere else."""
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key and HARNESS_AUTH.exists():
        try:
            entry = json.loads(HARNESS_AUTH.read_text()).get("groq") or {}
            key = str(entry.get("key") or "").strip()
        except (OSError, ValueError, AttributeError):
            key = ""
    return key


def _groq_module():
    """Import the Groq library, with plain errors for the two usual setup mistakes."""
    try:
        import groq
    except ImportError:
        raise BriefError("config", "The groq library is not installed here. Activate the venv "
                                   "(source .venv/bin/activate) and run: pip install -r requirements.txt")
    if not hasattr(groq, "Groq"):
        raise BriefError("config", f"{groq.__file__} is hiding the groq library. "
                                   "Rename that file (and delete __pycache__), then rerun.")
    return groq


def _client():
    global _CLIENT
    if _CLIENT is None:
        Groq = _groq_module().Groq
        key = _groq_key()
        if not key:
            raise BriefError("config", "No Groq key: set GROQ_API_KEY or configure Groq in the qloo CLI.")
        # max_retries=0: we decide what happens on 429, never the SDK in a loop.
        _CLIENT = Groq(api_key=key, max_retries=0, timeout=45)
    return _CLIENT


# ---------------------------------------------------------------- input

def compact(evidence: dict) -> dict:
    """The only evidence the model sees: names, ranks, club-specific split. ~400 tokens."""
    a = evidence["audience"]
    out = {"audience": f"{a['gender']}, age {a['age'].replace('_', ' ')}, {a['location']}",
           "club": (evidence.get("seed") or {}).get("name") if evidence.get("seed_effect") else None,
           "evidence": {}, "notes": evidence.get("warnings", [])}
    for kind, s in evidence["signals"].items():
        names = [i["name"] for i in s["items"][:PER_KIND]]
        new = set(s.get("new_vs_baseline") or [])
        if new:
            split = {"club_specific": [n for n in names if n in new],
                     "general_audience": [n for n in names if n not in new]}
            out["evidence"][kind] = {k: v for k, v in split.items() if v}   # no empty lists
        else:
            out["evidence"][kind] = {"general_audience": names}
    return out


def _known_names(evidence: dict) -> dict[str, tuple[str, str]]:
    """lower-cased name -> (type, Qloo's own spelling), across seeded signals and the baseline."""
    known = {}
    for group in ("baseline", "signals"):
        for kind, s in evidence.get(group, {}).items():
            for i in s["items"]:
                known.setdefault(i["name"].strip().lower(), (kind, i["name"]))
    return known


# ---------------------------------------------------------------- call

def _call(system: str, user: str, max_tokens: int, models: list[str]) -> tuple[dict, dict]:
    """Try each model once, in order. If all fail, the error lists why each one failed."""
    groq = _groq_module()
    failures: list[BriefError] = []
    for model in models:
        kwargs = dict(model=model, max_tokens=max_tokens, temperature=0.4,
                      response_format={"type": "json_object"},
                      messages=[{"role": "system", "content": system},
                                {"role": "user", "content": user}])
        if model.startswith("openai/gpt-oss"):
            kwargs["reasoning_effort"] = "low"   # reasoning tokens count against TPM
        t0 = time.time()
        try:
            resp = _client().chat.completions.create(**kwargs)
            data = json.loads(resp.choices[0].message.content or "")
        except groq.AuthenticationError:
            raise BriefError("auth", "Groq rejected the API key.")
        except groq.RateLimitError as e:
            m = re.search(r"try again in ([\d.]+)s", str(e))
            failures.append(BriefError("rate_limit", f"{model}: rate limit reached",
                                       float(m.group(1)) if m else None))
        except groq.NotFoundError:
            failures.append(BriefError("config", f"{model}: not available to this key"))
        except (groq.APITimeoutError, groq.APIConnectionError):
            failures.append(BriefError("timeout", f"{model}: no answer"))
        except groq.APIStatusError as e:          # 400 json_validate_failed, 413, 5xx ...
            failures.append(BriefError("bad_output", f"{model}: HTTP {e.status_code} {str(e)[:160]}"))
        except json.JSONDecodeError:
            failures.append(BriefError("bad_output", f"{model}: reply was not JSON "
                                                     "(raise MAX_TOKENS if this repeats)"))
        else:
            usage = getattr(resp, "usage", None)
            return data, {"model": model, "seconds": round(time.time() - t0, 1),
                          "tokens": getattr(usage, "total_tokens", None)}
    if not failures:
        raise BriefError("config", "No Groq models configured (GROQ_MODELS).")
    kinds = {f.kind for f in failures}
    kind = "rate_limit" if kinds == {"rate_limit"} else failures[0].kind
    waits = [f.retry_after for f in failures if f.retry_after]
    raise BriefError(kind, "All Groq models failed. " + "; ".join(str(f) for f in failures)
                     + ". Run: python doctor.py", min(waits) if waits else None)


# ---------------------------------------------------------------- checks

CONTINGENCY_PCT = 10
PLURAL = {"artists": "artists", "brands": "brands", "places": "places", "tags": "taste tags"}
ALCOHOL_WORDS = ("cider", "beer", "lager", "alcohol", "brewer", "whisky", "vodka", "liquor", "wine")
# Behaviour claims Qloo can't support ("the audience frequents this venue"). A reading that
# makes one is replaced with a neutral line; the observed line still says what Qloo showed.
BEHAVIOUR = re.compile(r"\b(frequent\w*|hang ?outs?|popular|familiar|visits?|visited|wears?|regulars?|go(es)? to)\b", re.I)
UNVERIFIED_FIGURE = " (unverified figure: use the club's own)"
ALCOHOL_CHECK = ("Alcohol: confirm age-gating (18+), responsible-drinking messaging and the "
                 "league's and broadcaster's sponsorship rules.")


def working_budget(total: int) -> int:
    return total - int(round(total * CONTINGENCY_PCT / 100, -2))


def _rand_split(budget: list, total: int) -> list:
    """Model shares -> rand amounts over the working budget, plus a fixed contingency line.
    Amounts sum exactly to the total; shares are of the total and sum to 100."""
    rows = [b for b in budget if isinstance(b, dict) and isinstance(b.get("share_pct"), (int, float))
            and b["share_pct"] > 0 and "contingen" not in str(b.get("item", "")).lower()]   # ours is added below
    if not rows or not total:
        return []
    work = working_budget(total)
    s = sum(b["share_pct"] for b in rows)
    out = [{"item": str(b.get("item", "?")), "amount_zar": int(round(work * b["share_pct"] / s, -2))}
           for b in rows]
    out[0]["amount_zar"] += work - sum(r["amount_zar"] for r in out)        # rounding remainder
    out.append({"item": "Contingency, held back", "amount_zar": total - work, "contingency": True})
    for r in out:
        r["share_pct"] = round(r["amount_zar"] * 100 / total)
    out[0]["share_pct"] += 100 - sum(r["share_pct"] for r in out)
    return out


def observed(evidence: dict, name: str, kind: str) -> str:
    """What Qloo actually returned for this item, written by code, never by the model."""
    seed = evidence.get("seed") or {}
    club = seed.get("name") if evidence.get("seed_effect") else None
    label = PLURAL.get(kind, kind)
    rank_in = lambda group: next((i["rank"] for i in evidence.get(group, {}).get(kind, {}).get("items", [])
                                  if i["name"] == name), None)
    r_club, r_base = rank_in("signals"), rank_in("baseline")
    if not club:
        return f"Qloo: #{r_club or r_base} among this audience's {label}."
    if r_club and r_base:
        return f"Qloo: #{r_club} among {club} fans' {label}; also #{r_base} for the whole audience."
    if r_club:
        base = evidence.get("baseline", {}).get(kind, {})
        n = base.get("returned") or base.get("count", 0)
        return f"Qloo: #{r_club} among {club} fans' {label}; not in the whole audience's top {n}."
    return f"Qloo: #{r_base} among the whole audience's {label}; not specific to {club} fans."


def _kpi(k) -> dict:
    k = {f: str(k.get(f, "") or "") for f in ("metric", "baseline", "method", "window")} if isinstance(k, dict) \
        else {"metric": str(k), "baseline": "", "method": "", "window": ""}
    if re.search(r"\d", k["baseline"]):          # a number the model can't know
        k["baseline"] += UNVERIFIED_FIGURE
    return k


def _reading(e: dict, name: str, kind: str) -> tuple[str, bool]:
    text = str(e.get("reading") or e.get("why") or "")
    if BEHAVIOUR.search(text):
        return (f"Qloo ranks {name} highly for this audience; this concept tests whether that interest "
                "carries over to the club."), True
    return text, False


def ground(brief: dict, evidence: dict | None, budget_zar: int) -> dict:
    """Check every cited signal against Qloo's results, write the observed lines,
    fix the budget maths, add required checks and put the lead concept first."""
    known = _known_names(evidence) if evidence else {}
    concepts = [c for c in brief.get("concepts", []) if isinstance(c, dict)][:3]
    if not concepts:
        raise BriefError("bad_output", "The model returned no campaign concepts.")
    total_cited = total_ok = 0
    for c in concepts:
        verified, unverified = [], []
        for e in c.get("evidence") or []:
            e = e if isinstance(e, dict) else {"name": str(e)}
            name = str(e.get("name", "")).strip()
            hit = known.get(name.lower())
            if hit:
                kind, qname = hit
                reading, rewritten = _reading(e, qname, kind)
                verified.append({"name": qname, "type": kind, "observed": observed(evidence, qname, kind),
                                 "reading": reading, "rewritten": rewritten})
            elif name:
                unverified.append(name)
        c["evidence"], c["unverified"] = verified, unverified
        c["grounded"] = len(verified) >= 2
        c["budget"] = _rand_split(c.get("budget") or [], budget_zar)
        c["activation"] = [str(x) for x in (c.get("activation") or [])][:4]
        c["channels"] = [str(x) for x in (c.get("channels") or [])]
        c["kpis"] = [_kpi(k) for k in (c.get("kpis") or [])]
        checks = [str(x) for x in (c.get("to_validate") or c.get("assumptions") or [])]
        text = " ".join([str(c.get("idea", ""))] + c["activation"] + c["channels"]
                        + [e["name"] for e in verified]).lower()
        for e in verified:                     # a high rank never proves a partner or a venue
            if e["type"] in ("brands", "places") and not any(e["name"].lower() in x.lower() for x in checks):
                checks.append(f"{e['name']} is willing to take part, and at what cost." if e["type"] == "brands"
                              else f"{e['name']} is available on the date, and its hire cost.")
        if any(w in text for w in ALCOHOL_WORDS) and not any("age" in x.lower() for x in checks):
            checks.append(ALCOHOL_CHECK)
        c["to_validate"] = checks
        c.pop("assumptions", None)
        total_cited += len(verified) + len(unverified)
        total_ok += len(verified)

    lead = brief.get("lead") if isinstance(brief.get("lead"), dict) else {}
    want = str(lead.get("concept", "")).strip().lower()
    idx = next((i for i, c in enumerate(concepts) if str(c.get("name", "")).strip().lower() == want), 0)
    concepts.insert(0, concepts.pop(idx))
    for i, c in enumerate(concepts):
        c["lead"] = i == 0
    return {"headline": str(brief.get("headline", "")), "concepts": concepts,
            "lead": {"name": str(concepts[0].get("name", "")), "why": str(lead.get("why", "") or "")},
            "evidence_gaps": [str(x) for x in (brief.get("evidence_gaps") or [])],
            "grounding": {"cited": total_cited, "verified": total_ok,
                          "grounded_concepts": sum(c["grounded"] for c in concepts)}}


# ---------------------------------------------------------------- public

def _request(club: str | None, objective: str, budget_zar: int, ev_text: str) -> str:
    return (f"CLUB: {club or 'not specified'}\nOBJECTIVE: {objective}\n"
            f"WORKING BUDGET: R{working_budget(budget_zar):,} (R{budget_zar:,} total; "
            f"{CONTINGENCY_PCT}% held back as contingency)\nEVIDENCE: {ev_text}")


def write_brief(evidence: dict, club: str | None, objective: str, budget_zar: int,
                models: list[str] | None = None) -> dict:
    view = compact(evidence)
    user = _request(club, objective, budget_zar, json.dumps(view, ensure_ascii=False))
    raw, meta = _call(PROMPT, user, MAX_TOKENS, models or MODELS)
    return {**ground(raw, evidence, budget_zar), "meta": meta, "evidence_sent": view}


def generic_brief(evidence: dict, club: str | None, objective: str, budget_zar: int,
                  models: list[str] | None = None) -> dict:
    """The same request without Qloo. Its claims are still checked against Qloo,
    which is the point: it shows how much of a generic answer the data supports."""
    a = evidence["audience"]
    user = _request(club, objective, budget_zar, "none") + \
        f"\nAUDIENCE: {a['gender']}, age {a['age'].replace('_', ' ')}, {a['location']}"
    raw, meta = _call(PROMPT + GENERIC_NOTE, user, GENERIC_MAX_TOKENS, models or MODELS)
    return {**ground(raw, evidence, budget_zar), "meta": meta}


def _brief_cache_path() -> str:
    return os.environ.get("FANDNA_CACHE", "fandna_cache.sqlite")


def cached_brief(evidence: dict, club: str | None, objective: str, budget_zar: int,
                 generic: bool = False, fresh: bool = False) -> dict:
    """write_brief / generic_brief with a SQLite cache, so the same inputs cost no tokens
    and a brief you reviewed is the one people see. fresh=True writes a new one."""
    key = hashlib.sha256(json.dumps(
        {"generic": generic, "evidence": compact(evidence), "club": club, "objective": objective,
         "budget": budget_zar, "prompt": PROMPT, "models": MODELS}, sort_keys=True).encode()).hexdigest()
    with closing(sqlite3.connect(_brief_cache_path())) as con, con:
        con.execute("CREATE TABLE IF NOT EXISTS briefs (key TEXT PRIMARY KEY, body TEXT, created REAL)")
        row = None if fresh else con.execute(
            "SELECT body, created FROM briefs WHERE key=?", (key,)).fetchone()
    if row:
        r = json.loads(row[0])
        r["meta"].update(cached=True, created=row[1])
        return r
    r = (generic_brief if generic else write_brief)(evidence, club, objective, budget_zar)
    r["meta"].update(cached=False, created=time.time())
    with closing(sqlite3.connect(_brief_cache_path())) as con, con:
        con.execute("INSERT OR REPLACE INTO briefs VALUES (?,?,?)", (key, json.dumps(r), r["meta"]["created"]))
    return r


def to_markdown(result: dict, club: str | None, budget_zar: int) -> str:
    """Exportable brief: what Qloo shows, what it suggests, what to validate."""
    L = [f"# FanDNA brief: {club or 'audience'}", "", result["headline"], ""]
    lead = result.get("lead") or {}
    if lead.get("name"):
        L += [f"**Recommended first:** {lead['name']}" + (f". {lead['why']}" if lead.get("why") else ""), ""]
    for n, c in enumerate(result["concepts"], 1):
        L += [f"## {n}. {c.get('name', 'Concept')}" + (" (recommended)" if c.get("lead") else ""), "",
              str(c.get("idea", "")), "", "**What Qloo shows, and what it suggests**", ""]
        for e in c["evidence"]:
            L += [f"- **{e['name']}**: {e['observed']}",
                  f"  - Suggests: {e['reading']}" + (" (rewritten by FanDNA)" if e.get("rewritten") else "")]
        if not c["evidence"]:
            L += ["- No cited signal was found in Qloo's results."]
        if c["unverified"]:
            L += ["", f"Not found in Qloo's results, so unproven: {', '.join(c['unverified'])}"]
        L += ["", "**Activation**", ""] + [f"{i}. {s}" for i, s in enumerate(c["activation"], 1)]
        L += ["", f"**Channels:** {', '.join(c['channels'])}", "",
              "**Budget** (illustrative split, not quotes)", "", "| Item | Rand | Share |", "|---|---:|---:|"]
        L += [f"| {b['item']} | R{b['amount_zar']:,} | {b['share_pct']}% |" for b in c["budget"]]
        if c["kpis"]:
            L += ["", "**KPIs**", "", "| KPI | Baseline | How to measure | Window |", "|---|---|---|---|"]
            L += [f"| {k['metric']} | {k['baseline']} | {k['method']} | {k['window']} |" for k in c["kpis"]]
        L += ["", "**To validate before spending**", ""] + [f"- [ ] {x}" for x in c["to_validate"]] + [""]
    if result["evidence_gaps"]:
        L += ["## Notes on the evidence", ""] + [f"- {g}" for g in result["evidence_gaps"]] + [""]
    L += [f"_Total budget R{budget_zar:,}, {CONTINGENCY_PCT}% held back as contingency. Signals from Qloo; "
          f"interpretation by {result['meta']['model']}, checked against Qloo's results by FanDNA._"]
    return "\n".join(L)


if __name__ == "__main__":
    if not Path("evidence.json").exists():
        sys.exit('No evidence.json here yet. Run: python qloo.py "Kaizer Chiefs"')
    ev = json.loads(Path("evidence.json").read_text())
    club = (ev.get("seed") or {}).get("name")
    args = dict(club=club, objective="Fill the stadium for the next home match", budget_zar=250_000)
    if "--models" in sys.argv:
        for m in sorted(x.id for x in _client().models.list().data):
            print(m)
    elif "--compare" in sys.argv:
        for model in sys.argv[sys.argv.index("--compare") + 1:]:
            try:
                r = write_brief(ev, models=[model], **args)
                g = r["grounding"]
                print(f"{model:<32} {r['meta']['tokens']} tokens  {r['meta']['seconds']}s  "
                      f"grounded concepts {g['grounded_concepts']}/3  citations {g['verified']}/{g['cited']}")
            except BriefError as e:
                print(f"{model:<32} failed: {e}")
            time.sleep(20)   # stay inside the per-minute token budget between models
    else:
        r = write_brief(ev, **args)
        print(to_markdown(r, club, args["budget_zar"]))
        print(f"\n[{r['meta']['model']}, {r['meta']['tokens']} tokens, {r['meta']['seconds']}s, "
              f"citations verified {r['grounding']['verified']}/{r['grounding']['cited']}]")
