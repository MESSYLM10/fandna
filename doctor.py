"""
FanDNA setup check. Run it whenever something fails:  python doctor.py

Checks, in order, and stops at the first thing that needs fixing:
  1. Python libraries installed (and not hidden by a local file of the same name)
  2. Qloo key found, and accepted by the hackathon host (1 tiny call)
  3. Groq key found, and accepted (lists models: costs no tokens)
  4. Each model in GROQ_MODELS: available? answers a tiny JSON request? (~50 tokens each)

Keys are never printed in full: only the first 4 and last 4 characters.
"""
import os
import sys

OK, BAD, WARN = "  [ok]  ", "  [FIX] ", "  [!]   "


def mask(key: str) -> str:
    return f"{key[:4]}...{key[-4:]} ({len(key)} chars)" if len(key) > 10 else "(too short to be a key)"


def stop(msg: str):
    print(BAD + msg)
    sys.exit(1)


print("1. Libraries")
print(OK + f"Python {sys.version.split()[0]} at {sys.executable}")
if ".venv" not in sys.executable:
    print(WARN + "Not running inside .venv. Run: source .venv/bin/activate")
try:
    import requests  # noqa: F401
except ImportError:
    stop("requests missing. Run: pip install -r requirements.txt")
import brief  # noqa: E402  (imports groq lazily)
import qloo   # noqa: E402
try:
    groq = brief._groq_module()
except brief.BriefError as e:
    stop(str(e))
print(OK + f"requests and groq installed (groq from {os.path.dirname(groq.__file__)})")

print("\n2. Qloo")
try:
    sess = qloo._session()
except qloo.QlooError as e:
    stop(f"{e} Expected in QLOO_API_KEY or ~/.qloo/config (api_key=...).")
src = "QLOO_API_KEY" if os.environ.get("QLOO_API_KEY") else "~/.qloo/config"
print(OK + f"key from {src}: {mask(sess.headers['X-Api-Key'])}")
try:
    qloo._fetch("/search", {"query": "Kaizer Chiefs", "take": 1})
    print(OK + f"accepted by {qloo.BASE}")
except qloo.QlooError as e:
    stop(f"{e.kind}: {e}")

print("\n3. Groq key")
key = brief._groq_key()
if not key:
    stop("No Groq key. Set GROQ_API_KEY, or run the qloo CLI setup and choose Groq. "
         f"Looked in: GROQ_API_KEY, {brief.HARNESS_AUTH}")
src = "GROQ_API_KEY" if os.environ.get("GROQ_API_KEY") else str(brief.HARNESS_AUTH)
print(OK + f"key from {src}: {mask(key)}")
if not key.startswith("gsk_"):
    print(WARN + "Groq keys normally start with 'gsk_'. This may be a different kind of credential.")
try:
    available = sorted(m.id for m in brief._client().models.list().data)
except groq.AuthenticationError:
    stop("Groq rejected this key. Create one at https://console.groq.com/keys and run: "
         "export GROQ_API_KEY=... (or put it in .streamlit/secrets.toml)")
except Exception as e:  # noqa: BLE001
    stop(f"Could not list Groq models: {type(e).__name__}: {str(e)[:200]}")
print(OK + f"accepted; {len(available)} models available to this key")

print("\n4. Models (GROQ_MODELS, tried in this order by brief.py)")
working = []
for model in brief.MODELS:
    if model not in available:
        print(BAD + f"{model}: not available to this key")
        continue
    kwargs = dict(model=model, max_tokens=300, response_format={"type": "json_object"},
                  messages=[{"role": "system", "content": 'Reply with JSON: {"ok": true}'},
                            {"role": "user", "content": "ping"}])
    if model.startswith("openai/gpt-oss"):
        kwargs["reasoning_effort"] = "low"
    try:
        r = brief._client().chat.completions.create(**kwargs)
        print(OK + f"{model}: answered ({r.usage.total_tokens} tokens)")
        working.append(model)
    except groq.APIStatusError as e:
        print(BAD + f"{model}: HTTP {e.status_code} {str(e)[:220]}")
    except Exception as e:  # noqa: BLE001
        print(BAD + f"{model}: {type(e).__name__}: {str(e)[:220]}")

chat = [m for m in available if not any(w in m for w in ("whisper", "tts", "guard", "embed", "orpheus"))]
whisper = [m for m in available if "whisper" in m]
print(("\n" + OK + "voice input available: " + ", ".join(whisper)) if whisper
      else ("\n" + WARN + "no Whisper model on this key: voice input will ask people to type instead"))
print("\nChat models this key can use:\n    " + "\n    ".join(chat))
if working:
    print(f"\nReady. brief.py will use {working[0]} first.")
else:
    print("\nNo configured model works. Pick from the list above, e.g.:\n"
          f"    export GROQ_MODELS={','.join(chat[:3])}\n    python doctor.py")
    sys.exit(1)
