"""
FanDNA: know what your fans love beyond the 90 minutes.

    streamlit run app.py

Keys: locally from ~/.qloo (the qloo CLI); on Streamlit Cloud from the app's
Secrets (QLOO_API_KEY, GROQ_API_KEY), which Streamlit exposes as environment variables.

Colour carries meaning, taken from the South African flag:
  green = backed by Qloo · gold = appears only for the club's fans
  red = not found in Qloo · blue = the generic-AI answer
"""
import html
import re
import time

import streamlit as st

import agent
import brief as bf
import demo_seed
import qloo

st.set_page_config(page_title="FanDNA", page_icon="static/favicon.png", layout="wide")


@st.cache_resource(show_spinner=False)
def _load_demo_seed() -> int:
    """A fresh host starts with an empty cache: load the reviewed demo once."""
    return demo_seed.load_if_empty()


_load_demo_seed()

AGES = {"18 to 35": "35_and_younger", "36 to 55": "36_to_55", "55 and over": "55_and_older"}
GENDERS = {"Men": "male", "Women": "female", "Everyone": ""}
# Cities whose Qloo location query returns places actually in that city
# (check with: python live_check.py --cities). On 28 Oct 2026 only Johannesburg did:
# "Durban" resolved to Durban-Corbieres in France, Cape Town to unrelated cities,
# Pretoria to nothing. Add a city here as {label: query} once its check passes.
CITIES = {"Johannesburg": "Johannesburg"}
OBJECTIVES = [
    "Fill the stadium for the next home match",
    "Win back fan trust after the ownership change",
    "Grow match-day merchandise sales",
    "Bring a sponsor closer to the fanbase",
]
LABEL = {"artists": "Artists", "brands": "Brands", "places": "Places", "tags": "Taste tags"}
DEFAULT_CLUB = "Kaizer Chiefs"

CSS = """
<style>
@font-face { font-family: "FD Archivo"; src: url("app/static/archivo.woff2") format("woff2");
             font-weight: 100 900; font-stretch: 62% 125%; font-display: swap; }
:root { --ink:#1D2B24; --muted:#5B6B63; --green:#007A4D; --gold:#FFB612; --red:#C42F28;
        --blue:#002395; --mint:#F1F6F3; --line:#D9E3DD; }
[data-testid="stDecoration"] { display:none; }
[data-testid="stHeader"] { background: transparent; }
[data-testid="stMainBlockContainer"] { max-width: 1160px; padding-top: 0.5rem; }

.fd-y { display:block; width:100%; height:56px; margin: 0 0 28px; }
.fd-mark { font: 800 1.05rem/1 "FD Archivo", sans-serif; font-variation-settings: "wdth" 85;
           letter-spacing: .01em; color: var(--green); margin-bottom: 18px; }
.fd-h1 { font: 800 clamp(2.4rem, 6.2vw, 4.6rem)/0.95 "FD Archivo", sans-serif;
         font-variation-settings: "wdth" 68; letter-spacing: -0.01em; color: var(--ink);
         max-width: 21ch; margin: 0 0 18px; }
.fd-lede { font: 400 1.15rem/1.55 "FD Archivo", sans-serif; color: var(--muted); max-width: 60ch; margin: 0 0 30px; }

.fd-versus { border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); padding: 6px 0; margin: 0 0 30px; }
.fd-row { display:grid; grid-template-columns: 250px 1fr; gap: 20px; align-items: baseline; padding: 14px 0; }
.fd-row + .fd-row { border-top: 1px dashed var(--line); }
.fd-who { font: 500 .95rem/1.35 "FD Archivo", sans-serif; color: var(--muted); }
.fd-items { font: 700 clamp(1.35rem, 2.6vw, 2rem)/1.2 "FD Archivo", sans-serif;
            font-variation-settings: "wdth" 75; color: #8A9891; }
.fd-items.club { color: var(--ink); }
.fd-src { font: 400 .85rem/1.4 "FD Archivo", sans-serif; color: var(--muted); margin: 8px 0 0; }

mark.fd-new { background: linear-gradient(transparent 58%, rgba(255,182,18,.6) 58%); color: inherit; padding: 0 .08em; }

.fd-promise { display:grid; grid-template-columns: repeat(3, 1fr); gap: 28px; margin: 0 0 34px; }
.fd-promise p { font: 400 .95rem/1.5 "FD Archivo", sans-serif; color: var(--muted); margin: 0; }
.fd-promise b { color: var(--ink); font-weight: 650; }

.fd-h2 { display:flex; align-items: baseline; gap: 14px; margin: 46px 0 6px;
         font: 800 clamp(1.6rem, 3vw, 2.2rem)/1.05 "FD Archivo", sans-serif; font-variation-settings: "wdth" 75; color: var(--ink); }
.fd-step { font: 800 1rem/1 "FD Archivo", sans-serif; color: #fff; background: var(--green);
           border-radius: 50%; width: 1.9rem; height: 1.9rem; display:inline-flex; align-items:center;
           justify-content:center; flex: none; transform: translateY(-0.2rem); }
.fd-sub { font: 400 1rem/1.5 "FD Archivo", sans-serif; color: var(--muted); margin: 0 0 16px; max-width: 75ch; }

.fd-kind { font: 750 1.05rem/1.2 "FD Archivo", sans-serif; color: var(--ink); }
.fd-meta { font: 400 .8rem/1.3 "FD Archivo", sans-serif; color: var(--muted); }
.fd-col-h { font: 500 .8rem/1.3 "FD Archivo", sans-serif; color: var(--muted); margin: 10px 0 4px; }
ol.fd-list { margin: 0; padding-left: 1.4rem; font: 400 .98rem/1.65 "FD Archivo", sans-serif; }
ol.fd-list li::marker { color: #A3B0A9; font-size: .85em; }

.fd-ev { font: 400 1rem/1.5 "FD Archivo", sans-serif; }
.fd-ev b { color: var(--green); }
.fd-ev .t { color: var(--muted); font-size: .85rem; }
.fd-miss { color: var(--red); font: 500 .92rem/1.4 "FD Archivo", sans-serif; margin: 4px 0 14px; }
.fd-ev { margin: 0 0 14px; }
.fd-q { font: 700 1.1rem/1.35 "FD Archivo", sans-serif; font-variation-settings: "wdth" 85; margin: 0 0 8px; }
.fd-a { font: 400 1.02rem/1.6 "FD Archivo", sans-serif; margin: 0 0 12px; max-width: 75ch; }
.fd-okname { color: var(--green); }
.fd-noname { color: var(--red); text-decoration: underline wavy; text-underline-offset: 3px; }
ol.fd-trace { margin: 4px 0 10px; padding-left: 1.3rem; font: 400 .88rem/1.5 "FD Archivo", sans-serif; color: var(--muted); }
ol.fd-trace b { color: var(--ink); font-weight: 600; }
.fd-obs { font: 500 .9rem/1.45 "FD Archivo", sans-serif; color: var(--green); background: #EAF4EE;
          border-radius: 6px; padding: 5px 9px; margin: 4px 0 4px; display: inline-block; }
.fd-read { font: 400 .98rem/1.5 "FD Archivo", sans-serif; color: var(--ink); }
ul.fd-check { list-style: none; padding: 0; margin: 0 0 18px; }
ul.fd-check li { position: relative; padding-left: 1.6rem; margin: 0 0 8px; font: 400 .95rem/1.45 "FD Archivo", sans-serif; }
ul.fd-check li::before { content: ""; position: absolute; left: 0; top: .2rem; width: .95rem; height: .95rem;
                         border: 1.5px solid #8A9891; border-radius: 3px; }
.fd-lead { border-left: 4px solid var(--green); background: #F4F9F6; padding: 12px 16px; margin: 6px 0 14px;
           font: 400 1rem/1.5 "FD Archivo", sans-serif; border-radius: 0 6px 6px 0; }

.fd-cmp-h { font: 800 1.25rem/1.2 "FD Archivo", sans-serif; font-variation-settings: "wdth" 80; margin: 0 0 2px; }
.fd-cmp-h.qloo { color: var(--green); } .fd-cmp-h.gen { color: var(--blue); }
.fd-score { font: 800 2.6rem/1 "FD Archivo", sans-serif; font-variation-settings: "wdth" 70; color: var(--ink); margin: 6px 0 2px; }
.fd-chip { display:inline-block; font: 500 .85rem/1 "FD Archivo", sans-serif; padding: 5px 9px;
           border-radius: 999px; margin: 3px 4px 3px 0; }
.fd-chip.ok { background: #E3F2EA; color: var(--green); } .fd-chip.no { background: #FBE7E5; color: var(--red); }
.fd-cname { font: 650 .95rem/1.3 "FD Archivo", sans-serif; margin: 12px 0 2px; }

[data-testid="stMetricValue"] { font-variation-settings: "wdth" 72; font-weight: 800; }
[data-testid="stForm"] { background: var(--mint); border: none; padding: 1.4rem 1.4rem 1.1rem; }
[data-testid="stForm"] [data-testid="stTextInputRootElement"],
[data-testid="stForm"] [data-testid="stSelectbox"] > div > div,
[data-testid="stForm"] [data-testid="stNumberInputContainer"]
  { background: #fff !important; border: 1px solid var(--line) !important; }
[data-testid="stForm"] [data-testid="stNumberInputContainer"] * { background: #fff !important; }
button[data-baseweb="tab"] p { font-size: 1.02rem; font-weight: 650; }

@media (max-width: 700px) {
  .fd-row { grid-template-columns: 1fr; gap: 4px; }
  .fd-promise { grid-template-columns: 1fr; gap: 10px; }
  .fd-y { height: 36px; margin-bottom: 18px; }
}
</style>
"""

# The flag's Y, lying on its side: two arms (everyone, the club's fans) join one band (the campaign).
Y_BAND = """
<svg class="fd-y" viewBox="0 0 1200 56" preserveAspectRatio="none" aria-hidden="true">
  <polygon points="0,0 64,0 330,18 1200,18 1200,38 330,38 64,56 0,56 0,42 220,28 0,14"
           fill="#007A4D" stroke="#FFB612" stroke-width="3" stroke-linejoin="round"
           vector-effect="non-scaling-stroke"/>
</svg>
"""


def esc(s) -> str:
    return html.escape(str(s))


def when(ts: float) -> str:
    return time.strftime("%d %b, %H:%M", time.localtime(ts))


def source(s: dict) -> str:
    if s.get("stale"):
        return f"older cached result from {when(s['fetched_at'])}"
    return f"cached, retrieved {when(s['fetched_at'])}" if s["cached"] else "live from Qloo"


def explain(e: Exception) -> str:
    kind = getattr(e, "kind", "")
    if kind == "rate_limit":
        wait = int(getattr(e, "retry_after", None) or 60)
        return (f"{e} Wait about {wait} seconds and press the button again. "
                "Nothing is lost: the Qloo results are cached.")
    if kind == "auth":
        return f"{e} This is a setup problem, not your input. Run `python doctor.py`."
    if kind == "bad_output":
        return "The language model returned an unusable answer. Press Regenerate to try again."
    return str(e)


def heading(step: int, title: str, sub: str = ""):
    st.markdown(f'<div class="fd-h2"><span class="fd-step">{step}</span>{esc(title)}</div>'
                + (f'<p class="fd-sub">{sub}</p>' if sub else ""), unsafe_allow_html=True)


@st.cache_resource(show_spinner=False, ttl=6 * 3600)
def demo_evidence():
    """The hero's live example. Served from the Qloo cache after the first visit."""
    try:
        return qloo.build_evidence(qloo.Audience(), seed_query=DEFAULT_CLUB)
    except Exception:  # noqa: BLE001  the hero must never break the page
        return None


def versus_html(ev: dict) -> str:
    if not ev or not ev.get("seed_effect"):
        return ""
    pick = (("brands", 2), ("places", 2))
    base = [i["name"] for k, n in pick for i in ev["baseline"].get(k, {}).get("items", [])[:n]]
    club = [x for k, n in pick for x in (ev["signals"].get(k, {}).get("new_vs_baseline") or [])[:n]]
    if len(base) < 3 or len(club) < 3:
        return ""
    return (
        '<div class="fd-versus">'
        f'<div class="fd-row"><span class="fd-who">Men aged 18 to 35 in Johannesburg</span>'
        f'<span class="fd-items">{esc(", ".join(base))}</span></div>'
        f'<div class="fd-row"><span class="fd-who">The same men, once they follow {esc(ev["seed"]["name"])}</span>'
        f'<span class="fd-items club">{", ".join(f"<mark class=fd-new>{esc(n)}</mark>" for n in club)}</span></div>'
        '<p class="fd-src">Live results from Qloo\'s taste data: the brands and places this audience ranks highest. '
        'Gold marks what appears only for the club\'s fans.</p></div>')


# ---------------------------------------------------------------- hero

st.markdown(CSS + Y_BAND, unsafe_allow_html=True)
st.markdown(
    '<div class="fd-mark">FanDNA</div>'
    '<h1 class="fd-h1">Know what your fans love beyond the 90 minutes.</h1>'
    '<p class="fd-lede">FanDNA reads Qloo\'s taste data for a club\'s supporters and turns it into '
    'campaign concepts with budgets and KPIs, then researches your follow-up questions in Qloo itself. Every claim is checked against that data before you see it.</p>',
    unsafe_allow_html=True)
hero_slot = st.empty()          # filled at the end, so the form never waits for it
st.markdown(
    '<div class="fd-promise">'
    '<p><b>Evidence first.</b> Findings come from Qloo. A language model writes the brief, and the code '
    'checks every name it cites.</p>'
    '<p><b>Honest about gaps.</b> Anything Qloo didn\'t show is labelled as an assumption, and thin data is flagged.</p>'
    '<p><b>No personal data.</b> FanDNA describes audiences as a whole. No sign-in, nothing stored about anyone.</p>'
    '</div>', unsafe_allow_html=True)

# ---------------------------------------------------------------- 1. inputs

heading(1, "Who are the fans?", "The defaults are the demo: young men in Johannesburg who follow Kaizer Chiefs.")
with st.form("brief_form", border=False):
    a, b, c = st.columns(3)
    club = a.text_input("Club or brand", DEFAULT_CLUB,
                        help="Leave empty to describe the audience without a club.")
    location = b.selectbox("City", list(CITIES), help="Only cities where Qloo's location data checks out are listed.")
    age = c.selectbox("Age", list(AGES))
    d, e, f = st.columns(3)
    gender = d.selectbox("Audience", list(GENDERS))
    objective = e.selectbox("Objective", OBJECTIVES)
    budget = f.number_input("Budget (rand)", min_value=20_000, max_value=10_000_000,
                            value=250_000, step=10_000)
    submitted = st.form_submit_button("Build the brief", type="primary")

if submitted:
    st.session_state.update(
        inputs=dict(club=club.strip(), audience=qloo.Audience(CITIES[location], GENDERS[gender], AGES[age]),
                    objective=objective, budget=int(budget), age_label=age, gender_label=gender),
        evidence=None, result=None, generic=None, error=None)


def run(inp: dict, fresh: bool = False):
    ss = st.session_state
    ss.error = None
    with st.status("Building the brief", expanded=True) as status:
        try:
            if ss.evidence is None:
                st.write("Reading this audience's tastes in Qloo")
                ss.evidence = qloo.build_evidence(inp["audience"], seed_query=inp["club"] or None)
            st.write("Writing three campaign concepts from that evidence")
            seed = ss.evidence.get("seed") or {}
            club_for_brief = seed.get("name") if ss.evidence.get("seed_effect") else None
            ss.result = bf.cached_brief(ss.evidence, club_for_brief, inp["objective"],
                                        inp["budget"], fresh=fresh)
            status.update(label="Brief ready", state="complete", expanded=False)
        except (qloo.QlooError, bf.BriefError) as e:
            ss.error = explain(e)
            status.update(label="Stopped", state="error", expanded=False)


def results(inp: dict):
    ss = st.session_state
    if ss.result is None and ss.error is None:
        run(inp)
    ev = ss.evidence
    if ss.error:
        st.error(ss.error)
    if ev is None:
        return

    # ------------------------------------------------------------ 2. signals
    seed = ev.get("seed") or {}
    club_name = seed.get("name") if ev.get("seed_effect") else None
    who = (f"{inp['gender_label'].lower() if inp['gender_label'] != 'Everyone' else 'people'} "
           f"aged {inp['age_label']} in {inp['audience'].location}")
    heading(2, "What Qloo shows about these fans",
            f"Left: all {esc(who)}. Right: the same audience once <b>{esc(club_name)}</b> fans are added. "
            "<mark class=fd-new>Gold</mark> marks what appears only for the club's fans."
            if club_name else f"Qloo's top signals for {esc(who)}.")
    for w in ev["warnings"]:
        st.info(w)

    def ranked(items: list, new: set = frozenset()) -> str:
        rows = []
        for i in items[:bf.PER_KIND]:
            name = esc(i["name"])
            body = f"<mark class=fd-new>{name}</mark>" if i["name"] in new else name
            rows.append(f'<li value="{i["rank"]}">{body}</li>')
        return f'<ol class="fd-list">{"".join(rows)}</ol>' if rows else '<p class="fd-meta">No results</p>'

    kinds = [k for k in LABEL if ev["signals"].get(k)]
    grid = st.columns(2, gap="medium")
    for n, kind in enumerate(kinds):
        s = ev["signals"][kind]
        with grid[n % 2].container(border=True):
            st.markdown(f'<span class="fd-kind">{LABEL[kind]}</span> '
                        f'<span class="fd-meta">&nbsp;{esc(source(s))}</span>', unsafe_allow_html=True)
            if club_name:
                left, right = st.columns(2)
                left.markdown('<div class="fd-col-h">Whole audience</div>'
                              + ranked(ev["baseline"].get(kind, {}).get("items", [])), unsafe_allow_html=True)
                right.markdown(f'<div class="fd-col-h">{esc(club_name)} fans</div>'
                               + ranked(s["items"], set(s.get("new_vs_baseline") or [])), unsafe_allow_html=True)
            else:
                st.markdown(ranked(s["items"]), unsafe_allow_html=True)
            notes = []
            if s.get("set_aside"):
                notes.append("Set aside as another sport: " + ", ".join(s["set_aside"]) + ".")
            if s.get("excluded"):
                notes.append(f"{s['excluded']} result(s) left out as unsuitable for a club brief.")
            if notes:
                st.caption(" ".join(notes))

    # ------------------------------------------------------------ 3. brief
    r = ss.result
    heading(3, "Three campaign concepts", esc(r["headline"]) if r else "")
    if r is None:
        if st.button("Try writing the brief again"):
            run(inp)
            st.rerun()
        return

    g = r["grounding"]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Claims found in Qloo data", f"{g['verified']} of {g['cited']}")
    m2.metric("Concepts backed by 2+ signals", f"{g['grounded_concepts']} of {len(r['concepts'])}")
    m3.metric("Budget", f"R{inp['budget']:,}")
    m4.metric("Brief", "Saved" if r["meta"].get("cached") else f"{r['meta']['seconds']}s",
              help=f"Model: {r['meta']['model']}. Written {when(r['meta']['created'])}.")

    lead = r.get("lead") or {}
    if lead.get("name"):
        st.markdown(f'<div class="fd-lead"><b>Run first: {esc(lead["name"])}.</b> {esc(lead.get("why", ""))}</div>',
                    unsafe_allow_html=True)
    tabs = st.tabs([("Recommended: " if c.get("lead") else "") + c.get("name", "Concept") for c in r["concepts"]])
    for tab, c in zip(tabs, r["concepts"]):
        with tab:
            st.markdown(c.get("idea", ""))
            left, right = st.columns([3, 2], gap="large")
            with left:
                st.markdown("**What Qloo shows, and what it suggests**")
                st.markdown("".join(
                    f'<div class="fd-ev"><b>{esc(e["name"])}</b> <span class="t">{LABEL.get(e["type"], "")}</span>'
                    f'<div class="fd-obs">{esc(e["observed"])}</div>'
                    f'<div class="fd-read">Suggests: {esc(e["reading"])}'
                    + (' <span class="t">(rewritten by FanDNA: the model claimed a behaviour Qloo can\'t show)</span>'
                       if e.get("rewritten") else "") + '</div></div>'
                    for e in c["evidence"]) or '<p class="fd-miss">No cited signal was found in Qloo\'s results.</p>',
                    unsafe_allow_html=True)
                if c["unverified"]:
                    st.markdown(f'<p class="fd-miss">Not in Qloo\'s results, so unproven: '
                                f'{esc(", ".join(c["unverified"]))}</p>', unsafe_allow_html=True)
                st.markdown("**Activation**")
                st.markdown("\n".join(f"{n}. {step}" for n, step in enumerate(c["activation"], 1)))
                st.markdown(f"**Channels:** {', '.join(c['channels'])}")
            with right:
                st.markdown("**To validate before spending**")
                st.markdown('<ul class="fd-check">' + "".join(f"<li>{esc(x)}</li>" for x in c["to_validate"])
                            + "</ul>", unsafe_allow_html=True)
                st.markdown("**Budget**")
                st.markdown("| Item | Rand | Share |\n|---|---:|---:|\n" + "\n".join(
                    f"| {b['item']} | R{b['amount_zar']:,} | {b['share_pct']}% |" for b in c["budget"]))
                st.caption(f"Illustrative split, not quotes. {bf.CONTINGENCY_PCT}% is held back as contingency.")
            if c["kpis"]:
                st.markdown("**How to measure it**")
                st.markdown("| KPI | Baseline | How to measure | Window |\n|---|---|---|---|\n" + "\n".join(
                    f"| {k['metric']} | {k['baseline'] or '-'} | {k['method'] or '-'} | {k['window'] or '-'} |"
                    for k in c["kpis"]))
                st.caption("Targets are the club's to set against its own baseline. FanDNA doesn't invent them.")
    if r["evidence_gaps"]:
        st.caption("Notes on the evidence: " + "; ".join(r["evidence_gaps"]))

    b1, b2, _ = st.columns([1, 1, 3])
    if b1.button("Regenerate concepts", help="Writes a new brief from the same Qloo data."):
        run(inp, fresh=True)
        st.rerun()
    b2.download_button("Download the brief", bf.to_markdown(r, club_name, inp["budget"]),
                       file_name="fandna-brief.md", mime="text/markdown")

    # ------------------------------------------------------------ 4. what Qloo adds
    heading(4, "What does Qloo add?",
            "The same request, answered by the same model without Qloo's data. "
            "Its claims are then checked against the same Qloo results.")
    if ss.generic is None:
        if st.button("Compare with a generic AI answer"):
            try:
                with st.spinner("Asking the model without Qloo's data"):
                    ss.generic = bf.cached_brief(ev, club_name, inp["objective"], inp["budget"], generic=True)
            except bf.BriefError as e:
                st.error(explain(e))
            st.rerun()
    else:
        gen, gg = ss.generic, ss.generic["grounding"]

        def side(cls: str, title: str, gr: dict, concepts: list) -> str:
            rows = "".join(
                f'<div class="fd-cname">{esc(c.get("name", "Concept"))}</div>'
                + "".join(f'<span class="fd-chip ok">✓ {esc(e["name"])}</span>' for e in c["evidence"])
                + "".join(f'<span class="fd-chip no">✗ {esc(x)}</span>' for x in c["unverified"])
                for c in concepts)
            return (f'<div class="fd-cmp-h {cls}">{title}</div>'
                    f'<div class="fd-score">{gr["verified"]} of {gr["cited"]}</div>'
                    f'<div class="fd-meta">claims found in Qloo\'s results</div>{rows}')

        left, right = st.columns(2, gap="large")
        left.markdown(side("qloo", "FanDNA, built on Qloo", g, r["concepts"]), unsafe_allow_html=True)
        right.markdown(side("gen", "Generic AI, no Qloo data", gg, gen["concepts"]), unsafe_allow_html=True)

    # ------------------------------------------------------------ 5. ask FanDNA
    heading(5, "Ask FanDNA",
            "Ask a follow-up. FanDNA decides which Qloo lookups to run, shows each one, and checks every "
            "name in its answer against what Qloo returned.")
    other = "Orlando Pirates" if "chiefs" in (club_name or "").lower() else "Kaizer Chiefs"
    spec_brand = next(iter(ev["signals"].get("brands", {}).get("new_vs_baseline") or []), None)
    short = re.sub(r"\s+F\.?C\.?$", "", club_name or "")
    suggestions = ([(f"Compare with {other} fans", f"How do {short} fans differ from {other} fans in brands?")]
                   if club_name else []) \
        + ([(f"Who are {spec_brand}'s fans?", f"Who are {spec_brand}'s fans?")] if spec_brand else []) \
        + [("Target women 18 to 35 instead", "What would change if we targeted women aged 18 to 35?")]
    context = (f"the brief on screen is for {club_name or 'no club'}; audience: {who}; "
               f"objective: {inp['objective']}")
    picked = None
    for col, (label, full) in zip(st.columns(len(suggestions)), suggestions):
        if col.button(label, width="stretch", help=full):
            picked = full
    with st.form("ask_form", border=False):
        typed = st.text_input("Your question", label_visibility="collapsed",
                              placeholder="Ask about these fans, e.g. which places do they rank that "
                                          "the whole audience doesn't?")
        asked = st.form_submit_button("Ask FanDNA")
    spoken = st.audio_input("Or ask by voice", help="Speak your question; FanDNA transcribes it with Whisper.")
    heard = None
    if spoken is not None:
        audio = spoken.getvalue()
        sig = hash(audio)
        if ss.get("voice_sig") != sig:          # transcribe each recording once, not on every rerun
            try:
                with st.spinner("Transcribing"):
                    ss.voice_text = agent.transcribe(audio)
                ss.voice_sig = sig
                heard = ss.voice_text or None
            except bf.BriefError as e:
                st.error(explain(e))
        if ss.get("voice_text"):
            st.caption(f"Heard: \u201c{ss.voice_text}\u201d")
    question = picked or (typed.strip() if asked and typed.strip() else None) or heard
    if question:
        try:
            with st.spinner("Researching in Qloo"):
                ans = agent.ask(question, context)
            ss.answers = [ans] + [a for a in ss.get("answers", []) if a["question"] != ans["question"]]
        except (bf.BriefError, qloo.QlooError) as e:
            st.error(explain(e))

    labels = {"audience_tastes": "Audience tastes", "compare_clubs": "Compare clubs",
              "entity_fans": "Fans of", "find_entity": "Find"}

    def answer_html(a: dict) -> str:
        ok = {v.lower() for v in a["verified"]}

        def mark(m):
            name = html.unescape(m.group(1))
            cls = "fd-okname" if name.lower() in ok else "fd-noname"
            tip = "" if cls == "fd-okname" else ' title="Not found in what Qloo returned"'
            return f'<b class="{cls}"{tip}>{m.group(1)}</b>'

        body = re.sub(r"\*\*(.+?)\*\*", mark, esc(a["answer"])).replace("\n", "<br>")
        steps = "".join(
            f'<li><b>{labels.get(x["tool"], x["tool"])}</b> '
            f'{esc(", ".join(str(v) for v in x["args"].values()))}: {esc(x["summary"])}</li>' for x in a["steps"])
        n = len(a["verified"]) + len(a["unverified"])
        meta = (f'{len(a["verified"])} of {n} names found in Qloo\'s results. ' if n else "") + (
            "Saved answer." if a.get("cached") else f'{a["seconds"]}s, {a["model"]}.')
        miss = (f'<p class="fd-miss">Not in what Qloo returned, so unproven: {esc(", ".join(a["unverified"]))}</p>'
                if a["unverified"] else "")
        trace = (f'<div class="fd-meta">Qloo lookups</div><ol class="fd-trace">{steps}</ol>' if steps
                 else '<div class="fd-meta">No Qloo lookup was needed.</div>')
        return (f'<div class="fd-q">{esc(a["question"])}</div><div class="fd-a">{body}</div>{miss}'
                f'{trace}<div class="fd-meta">{meta}</div>')

    for a in ss.get("answers", [])[:3]:
        with st.container(border=True):
            st.markdown(answer_html(a), unsafe_allow_html=True)

    # ------------------------------------------------------------ provenance
    st.write("")
    with st.expander("Data sources and method"):
        st.markdown(
            f"- **Qloo host:** {qloo.BASE}\n"
            f"- **Audience signals:** `{inp['audience'].params()}`\n"
            f"- **Club entity:** {seed.get('name', 'none')} `{seed.get('id', '')}` {seed.get('types', '')}\n"
            f"- **Club changed Qloo's results:** {ev.get('seed_effect')}\n"
            + "\n".join(f"- **{LABEL[k]}:** {s['count']} results, {source(s)}" for k, s in ev["signals"].items())
            + f"\n- **Brief model:** {r['meta']['model']}, {r['meta'].get('tokens')} tokens\n"
            "- **Method:** fixed Qloo insight queries, cached; one model call; every cited signal "
            "is checked against Qloo's results, and budget amounts are computed in code.")


inp = st.session_state.get("inputs")
if inp:
    results(inp)

st.caption("FanDNA describes audiences in aggregate using Qloo's taste data and stores no personal data. "
           "Qloo's coverage of South African culture is still growing, so thin results are flagged.")

# Last: the hero's live example. The page above is already on screen while this loads.
hero_slot.markdown(versus_html(demo_evidence()), unsafe_allow_html=True)
