# FanDNA

**Know what your fans love beyond the 90 minutes, and prove it before you spend.**

FanDNA turns Qloo's taste data for a football club's supporters into three campaign concepts with budgets and KPIs. Every claim is checked against the data before anyone sees it. It is built for the marketing and sponsorship teams of South African football clubs and their sponsors.

Live demo: `<your Streamlit URL>` · Built for the [Qloo Agentic Hackathon](https://qloo.devpost.com/)

## What it does

1. **Shows what Qloo knows about the fans.** For young men in Johannesburg, Qloo's top brands are Topman, BMW and Tag Heuer. Add Kaizer Chiefs to the audience and they become Hunter's Cider, Power Play, TelkomSA, Cell C and Bathu. FanDNA shows both lists side by side and marks what appears only for the club's fans.
2. **Writes three campaign concepts from that evidence** and recommends one to run first. Each concept has an activation plan, channels, a budget in rand, KPIs, and a list of what to validate before spending.
3. **Separates what Qloo shows from what the model suggests.** The "Qloo:" line under each signal (its rank, and whether it is specific to the club) is written by code from the API response. The model only writes the interpretation.
4. **Compares itself with a generic AI.** The same request is sent to the same model without Qloo's data, and its claims are checked against Qloo's results. That shows what Qloo adds.

## How it works

```
form -> qloo.py: 9 fixed Qloo calls, cached in SQLite -> evidence JSON
     -> brief.py: one Groq call (~1,100 tokens in, at most 2,400 out)
     -> checks in code -> app.py
```

The Qloo calls are fixed: there is no agent loop deciding what to fetch, so cost and latency are predictable.

| Call | Purpose |
| --- | --- |
| `GET /search?query=<club>` | Resolve the club to a Qloo entity. A match counts only if every word of the query is in its name, and brand entities are preferred. |
| `GET /v2/insights` × 4, audience only | Artists, brands, places and taste tags for the audience (`signal.location.query`, `signal.demographics.age`, `signal.demographics.gender`). |
| `GET /v2/insights` × 4, audience + club | The same, with `signal.interests.entities=<club id>`. Items not in the audience-only list are the club-specific signals. |

**Checks done in code, not by the model:**

- Every cited signal must appear in Qloo's results; otherwise it is shown as "not in Qloo's results, so unproven".
- The "Qloo:" line for each signal is generated from the response. A model "reading" that claims behaviour Qloo can't show ("the audience frequents this venue") is replaced with a neutral line and marked as rewritten.
- Budget amounts are computed in code: the model gives percentages of a working budget, and 10% of the total is held back as contingency.
- Any number the model writes in a KPI baseline is flagged as unverified.
- For every cited brand or place, a check ("…is willing to take part, and at what cost") is added if the model didn't include one. Concepts involving alcohol get an age-gating and sponsorship-rules check.
- Other sports (rugby, cricket and so on) are set aside before the model sees the evidence, and shown as set aside.

## Caching and rate limits

- **Qloo:** results are cached in SQLite by endpoint and parameters for 7 days. A repeat brief makes 0 calls, and a second club with the same audience makes 5. If Qloo is unreachable, the last cached result is served and labelled as an older result.
- **Groq:** briefs are cached by their inputs, so a reviewed brief is the one people see, and "Regenerate" writes a new one. The Groq SDK's own retries are off. On a rate limit, FanDNA tries the next model once (`openai/gpt-oss-120b`, then `openai/gpt-oss-20b`) and never retries in a loop.
- **Retries:** Qloo timeouts and 5xx errors get 2 retries with backoff. A 429 is retried once only if Qloo asks for a wait of 10 seconds or less. A 401 stops immediately with a setup message.
- **Demo seed:** `demo_seed.json` holds the reviewed demo. A fresh host loads it once into its empty cache, so the first visitor needs no API call.

## Run it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export QLOO_API_KEY=...          # or ~/.qloo/config from the qloo CLI
export GROQ_API_KEY=...          # or ~/.qloo/agent/auth.json from the qloo CLI
python doctor.py                 # checks both keys and each model
streamlit run app.py
```

| Variable | Required | Default |
| --- | --- | --- |
| `QLOO_API_KEY` | yes | read from `~/.qloo/config` when unset |
| `GROQ_API_KEY` | yes | read from `~/.qloo/agent/auth.json` when unset |
| `QLOO_BASE_URL` | no | `https://hackathon.api.qloo.com` |
| `GROQ_MODELS` | no | `openai/gpt-oss-120b,openai/gpt-oss-20b` |
| `FANDNA_CACHE` | no | `fandna_cache.sqlite` |
| `FANDNA_CACHE_TTL` | no | `604800` (7 days) |
| `FANDNA_EXCLUDE` | no | `Grindr,Tinder,Bumble,Hinge` (names left out of a club brief) |
| `FANDNA_OFF_BRIEF` | no | `rugby,springbok,cricket,proteas,netball` |

On Streamlit Community Cloud, put `QLOO_API_KEY` and `GROQ_API_KEY` in the app's **Secrets**. Keys are only ever used server-side.

## Tests

```bash
pip install pytest
python -m pytest -q        # 36 offline tests, no keys or network needed
python live_check.py       # live: same brief twice, a weak-data club, a Groq limit
```

## Known limitations

These come from testing Qloo's coverage of southern Africa, and they are part of why FanDNA checks its data rather than trusting it.

- **Only Johannesburg resolves reliably as a location.** In our checks, "Durban" resolved to Durban-Corbières in France, Cape Town returned venues in other countries, and Pretoria returned nothing. The app lists only Johannesburg until another city checks out (`python live_check.py --cities`).
- **Smaller African clubs may not be in Qloo.** "Mbabane Swallows" matched "Swallow's Nest", a place. FanDNA rejects such matches, says so, and falls back to the audience alone.
- **Local music coverage looks thin.** The artists specific to Kaizer Chiefs fans are mostly Nigerian Afrobeats and US R&B, with no amapiano.
- **Club-versus-club differences are small.** Kaizer Chiefs and Orlando Pirates return similar brands and places. The strongest contrast is club fans versus the general audience.
- **Budgets are illustrative splits, not quotes, and KPI targets are left to the club.**

## Privacy

FanDNA describes audiences in aggregate and stores no personal data. There is no sign-in, and the cache holds only audience parameters and Qloo results.

## Licence

MIT. See [LICENSE](LICENSE). The bundled Archivo font is under the SIL Open Font License ([static/archivo-OFL.txt](static/archivo-OFL.txt)).
