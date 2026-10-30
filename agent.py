"""
Ask FanDNA: a small research agent over Qloo.

    from agent import ask
    r = ask("How do Kaizer Chiefs fans differ from Orlando Pirates fans in brands?")
    r["answer"], r["steps"], r["verified"], r["unverified"]

The model decides which Qloo tools to call (tools.py), up to MAX_TOOL_CALLS per question,
then answers in a few sentences. Guardrails in code:
  - tool outputs are compact (names and ranks), so a question costs roughly 4-6k tokens;
  - the model must bold every Qloo item it names, and each bold name is checked against
    what the tools actually returned (verified / unverified);
  - on a rate limit it moves to the next model once, never loops (same models as brief.py);
  - answers are cached by question and context, so a demo question costs nothing twice.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing

import brief
import qloo
import tools

MAX_TOOL_CALLS = 3
STEP_TOKENS = 700
SYSTEM = """You are FanDNA's research agent for football marketing in South Africa. You answer only with Qloo data.
- Call the tools to get facts, at most 3 calls. Prefer one well-chosen call.
- Then answer in at most 120 words of plain language.
- Bold every Qloo item you name, exactly as the tool returned it, e.g. **Bathu**. Bold nothing else.
- Qloo shows affinity (what an audience ranks highly), not behaviour. Never say people visit, wear, use or frequent things.
- If a tool returns a note or an error, say so plainly. Never fill gaps from your own knowledge.
- Ages: 35_and_younger, 36_to_55, 55_and_older. Johannesburg is the only verified city.
Context: {context}"""

_KIND = {"type": "string", "enum": list(tools.KINDS)}
_AGE = {"type": "string", "enum": list(tools.AGES)}
_GENDER = {"type": "string", "enum": ["men", "women", "everyone"]}
SCHEMAS = [
    {"type": "function", "function": {
        "name": "audience_tastes",
        "description": "Top Qloo artists, brands, places or taste tags for an audience; with a club, shows which "
                       "items are specific to that club's fans.",
        "parameters": {"type": "object", "properties": {
            "kind": _KIND, "club": {"type": "string"}, "age": _AGE, "gender": _GENDER},
            "required": ["kind"]}}},
    {"type": "function", "function": {
        "name": "compare_clubs",
        "description": "Compare two clubs' fans for one kind of signal: shared items and items unique to each.",
        "parameters": {"type": "object", "properties": {
            "kind": _KIND, "club_a": {"type": "string"}, "club_b": {"type": "string"}, "age": _AGE, "gender": _GENDER},
            "required": ["kind", "club_a", "club_b"]}}},
    {"type": "function", "function": {
        "name": "entity_fans",
        "description": "Age and gender affinity of the audience of a brand, artist or place in Qloo.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
]
AGENT_TOOLS = {s["function"]["name"] for s in SCHEMAS}


def _summary(result: dict) -> str:
    if "error" in result:
        return f"stopped: {result['error']}"
    if "club_fans" in result:
        n = sum(x["club_specific"] for x in result["club_fans"])
        return f"{len(result['club_fans'])} {result['kind']} for {result['club']} fans, {n} specific to the club"
    if "whole_audience" in result:
        return f"{len(result['whole_audience'])} {result['kind']} for {result['audience']}" + (
            f" ({result['notes'][0]})" if result.get("notes") else "")
    if "shared" in result:
        return (f"{len(result['shared'])} shared, {len(result['only_a'])} only {result['club_a']}, "
                f"{len(result['only_b'])} only {result['club_b']}")
    if "age_affinity" in result:
        top_age = next(iter(result["age_affinity"]), "?")
        lean = next(iter(result["gender_affinity"]), "?")
        return f"{result['entity']}: strongest age band {top_age}, leans {lean}"
    return result.get("note", "done")


def _create(**kwargs):
    """One model call, trying each configured model once (no sleeping retry loops)."""
    groq = brief._groq_module()
    failures = []
    for model in brief.MODELS:
        args = dict(kwargs, model=model)
        if model.startswith("openai/gpt-oss"):
            args["reasoning_effort"] = "low"
        try:
            return brief._client().chat.completions.create(**args), model
        except groq.AuthenticationError:
            raise brief.BriefError("auth", "Groq rejected the API key.")
        except groq.RateLimitError as e:
            m = re.search(r"try again in ([\d.]+)s", str(e))
            failures.append(brief.BriefError("rate_limit", f"{model}: rate limit reached",
                                             float(m.group(1)) if m else None))
        except (groq.APITimeoutError, groq.APIConnectionError):
            failures.append(brief.BriefError("timeout", f"{model}: no answer"))
        except groq.APIStatusError as e:          # e.g. 400 tool_use_failed, 404 model gone
            failures.append(brief.BriefError("bad_output", f"{model}: HTTP {e.status_code}"))
    kinds = {f.kind for f in failures}
    waits = [f.retry_after for f in failures if f.retry_after]
    raise brief.BriefError("rate_limit" if kinds == {"rate_limit"} else (failures[0].kind if failures else "config"),
                           "Ask FanDNA could not reach a model. " + "; ".join(map(str, failures)),
                           min(waits) if waits else None)


def _run_tool(name: str, raw_args: str) -> tuple[dict, dict]:
    try:
        args = json.loads(raw_args or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
    except ValueError as e:
        return {}, {"error": f"bad arguments ({e})"}
    if name not in AGENT_TOOLS:
        return args, {"error": f"unknown tool {name}"}
    try:
        return args, tools.TOOLS[name](**args)
    except (TypeError, ValueError) as e:
        return args, {"error": str(e)}
    except qloo.QlooError as e:
        if e.kind == "auth":
            raise
        return args, {"error": str(e)}


def _cache(key: str, value: dict | None = None) -> dict | None:
    with closing(sqlite3.connect(brief._brief_cache_path())) as con, con:
        con.execute("CREATE TABLE IF NOT EXISTS answers (key TEXT PRIMARY KEY, body TEXT, created REAL)")
        if value is not None:
            con.execute("INSERT OR REPLACE INTO answers VALUES (?,?,?)", (key, json.dumps(value), value["created"]))
            return value
        row = con.execute("SELECT body FROM answers WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def ask(question: str, context: str = "", fresh: bool = False) -> dict:
    question = question.strip()
    if not question:
        raise brief.BriefError("config", "Ask a question first.")
    key = hashlib.sha256(json.dumps([question.lower(), context, SYSTEM, brief.MODELS]).encode()).hexdigest()
    if not fresh and (hit := _cache(key)):
        return {**hit, "cached": True}

    t0 = time.time()
    messages = [{"role": "system", "content": SYSTEM.format(context=context or "none")},
                {"role": "user", "content": question}]
    steps, seen, tokens, answer, model = [], set(), 0, "", ""
    for _ in range(MAX_TOOL_CALLS + 2):
        allow = len(steps) < MAX_TOOL_CALLS
        kwargs = dict(messages=messages, max_tokens=STEP_TOKENS, temperature=0.2)
        if allow:
            kwargs.update(tools=SCHEMAS, tool_choice="auto")
        resp, model = _create(**kwargs)
        tokens += getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0
        msg = resp.choices[0].message
        calls = (getattr(msg, "tool_calls", None) or []) if allow else []
        if not calls:
            answer = (msg.content or "").strip()
            break
        messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in calls]})
        for c in calls:
            if len(steps) >= MAX_TOOL_CALLS:
                args, result = {}, {"error": "tool budget used; answer with what you have"}
            else:
                args, result = _run_tool(c.function.name, c.function.arguments)
                steps.append({"tool": c.function.name, "args": args, "summary": _summary(result)})
                seen |= tools.names_in(result)
            messages.append({"role": "tool", "tool_call_id": c.id,
                             "content": json.dumps(result, ensure_ascii=False)})
    if not answer:
        raise brief.BriefError("bad_output", "Ask FanDNA did not produce an answer. Try rephrasing.")

    known = {n.lower() for n in seen}
    bold = list(dict.fromkeys(b.strip() for b in re.findall(r"\*\*(.+?)\*\*", answer)))
    result = {"question": question, "answer": answer, "steps": steps,
              "verified": [b for b in bold if b.lower() in known],
              "unverified": [b for b in bold if b.lower() not in known],
              "model": model, "tokens": tokens, "seconds": round(time.time() - t0, 1),
              "created": time.time(), "cached": False}
    return _cache(key, result)


WHISPER_MODELS = ("whisper-large-v3-turbo", "whisper-large-v3")


def transcribe(audio: bytes, filename: str = "question.wav") -> str:
    """Spoken question -> text, with Groq's Whisper. English, short clips."""
    groq = brief._groq_module()
    for model in WHISPER_MODELS:
        try:
            r = brief._client().audio.transcriptions.create(
                file=(filename, audio), model=model, language="en", response_format="json")
            return (getattr(r, "text", "") or "").strip()
        except groq.AuthenticationError:
            raise brief.BriefError("auth", "Groq rejected the API key.")
        except (groq.NotFoundError, groq.RateLimitError, groq.APIStatusError):
            continue
        except (groq.APITimeoutError, groq.APIConnectionError):
            raise brief.BriefError("timeout", "Groq did not answer while transcribing.")
    raise brief.BriefError("config", "Voice input needs a Groq Whisper model; none is available to this key. "
                                     "Type the question instead.")


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "How do Kaizer Chiefs fans differ from Orlando Pirates fans in brands?"
    r = ask(q, fresh=True)
    for i, s in enumerate(r["steps"], 1):
        print(f"{i}. {s['tool']}({', '.join(f'{k}={v}' for k, v in s['args'].items())}) -> {s['summary']}")
    print("\n" + r["answer"])
    print(f"\n[verified: {', '.join(r['verified']) or '-'} | unverified: {', '.join(r['unverified']) or '-'} | "
          f"{r['model']}, {r['tokens']} tokens, {r['seconds']}s]")
