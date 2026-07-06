"""
app.py — Web UI to collect data from listing pages into a CSV.

Run locally:   streamlit run app.py
"""

import io
import os

import pandas as pd
import streamlit as st

import scraper

st.set_page_config(page_title="Listing Collector", layout="centered")

# --------------------------------------------------------------------------- #
# Look & feel: a calm "data workbench" identity. Space Grotesk (headings) +
# IBM Plex Sans (body) + IBM Plex Mono (data / run log).
# --------------------------------------------------------------------------- #
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap');

:root {
  --ink:#17252b; --muted:#5f7176; --canvas:#eceff0; --line:#d5dcdd;
  --accent:#146b63; --signal:#b5642a; --surface:#ffffff;
}

#MainMenu, header[data-testid="stHeader"], [data-testid="stToolbar"],
footer, [data-testid="stDecoration"] { display:none !important; }

.stApp { background:var(--canvas); }
.block-container { max-width:760px; padding-top:2.4rem; padding-bottom:4rem; }

html, body, [class*="css"], .stMarkdown, p, label, input, textarea, div {
  font-family:'IBM Plex Sans', system-ui, sans-serif; color:var(--ink);
}

.wm { font-family:'Space Grotesk', sans-serif; font-weight:700;
      font-size:1.75rem; letter-spacing:-0.02em; color:var(--ink);
      display:flex; align-items:center; gap:.55rem; margin:0; }
.wm-mark { width:14px; height:14px; border-radius:3px; background:var(--accent);
           box-shadow:5px 0 0 var(--signal); }
.sub { color:var(--muted); font-size:.95rem; margin:.35rem 0 1.6rem 1.9rem;
       max-width:52ch; }

.stTextInput input, .stNumberInput input {
  font-family:'IBM Plex Mono', monospace !important; font-size:.92rem;
  border-radius:7px !important; border:1px solid var(--line) !important;
}
.stTextInput input:focus { border-color:var(--accent) !important;
  box-shadow:0 0 0 2px rgba(20,107,99,.15) !important; }

.stButton>button, .stDownloadButton>button {
  font-family:'Space Grotesk', sans-serif !important; font-weight:600 !important;
  border-radius:7px !important; border:1px solid var(--accent) !important;
  background:var(--accent) !important; color:#fff !important;
  padding:.5rem 1.3rem !important; letter-spacing:.01em;
}
.stButton>button:hover, .stDownloadButton>button:hover { filter:brightness(1.08); }
.stDownloadButton>button { background:var(--surface) !important; color:var(--accent) !important; }

.log { font-family:'IBM Plex Mono', monospace; font-size:.8rem; line-height:1.55;
  background:var(--ink); color:#cfe3df; border-radius:9px; padding:.9rem 1rem;
  max-height:230px; overflow:auto; white-space:pre-wrap; }
.log .n { color:var(--signal); }
.log .u { color:#8fb7b1; }

.count { font-family:'Space Grotesk', sans-serif; font-weight:700;
  font-size:2.4rem; color:var(--accent); line-height:1; }
.count-label { font-family:'IBM Plex Mono', monospace; font-size:.78rem;
  color:var(--muted); text-transform:uppercase; letter-spacing:.08em; }

.eyebrow { font-family:'IBM Plex Mono', monospace; font-size:.72rem;
  text-transform:uppercase; letter-spacing:.1em; color:var(--muted);
  margin-bottom:.3rem; }

[data-testid="stDataFrame"] { border:1px solid var(--line); border-radius:8px; }
hr { border-color:var(--line); }
</style>
""", unsafe_allow_html=True)

# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
st.markdown(
    '<div class="wm"><span class="wm-mark"></span>Listing Collector</div>'
    '<div class="sub">Paste the address of a page that lists things '
    '(events, startups, a directory) and download a CSV with one row per '
    'item.</div>',
    unsafe_allow_html=True,
)

# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
if "rows" not in st.session_state:
    st.session_state.rows = None
    st.session_state.fields = None

# --------------------------------------------------------------------------- #
# Main input
# --------------------------------------------------------------------------- #
source = st.radio("How do you want to provide the page?",
                  ["Fetch a URL", "Paste HTML"], horizontal=True)

url = ""
html_text = ""
html_kind = "one"
base_url = ""

if source == "Fetch a URL":
    url = st.text_input(
        "Listing page address",
        placeholder="https://alhambraventure.com/startups/",
    )
else:
    html_text = st.text_area(
        "Paste the page HTML",
        height=170,
        placeholder="In the browser: right-click > Inspect, copy the <html> "
                    "element (or use View source), and paste it here. Useful "
                    "for pages behind a login or built with JavaScript.",
    )
    hk = st.radio(
        "What is this HTML?",
        ["A single item page", "A list with items on this same page",
         "A list that links to separate item pages"],
    )
    html_kind = {
        "A single item page": "one",
        "A list with items on this same page": "cards",
        "A list that links to separate item pages": "links",
    }[hk]
    base_url = st.text_input(
        "Original page URL (optional)",
        placeholder="https://alhambraventure.com/startups/",
        help="Helps turn relative links (/startup/x) into full ones. Needed "
             "for the \"links to separate pages\" option.",
    )

with st.expander("Options"):
    engine_label = st.radio(
        "Extraction engine",
        ["Free (no AI)", "Local (Ollama)", "Gemini (cloud)"],
        help="Start with Free. If a site doesn't read well, try a local model "
             "(Ollama) or Gemini.",
    )
    engine = {"Free (no AI)": "rules", "Local (Ollama)": "ollama",
              "Gemini (cloud)": "gemini"}[engine_label]

    cols_text = st.text_input(
        "CSV columns (comma-separated)",
        value=", ".join(scraper.DEFAULT_FIELDS),
    )
    fields = [c.strip() for c in cols_text.split(",") if c.strip()]

    c1, c2 = st.columns(2)
    with c1:
        pattern = st.text_input(
            "Link filter (optional)",
            placeholder="/startup/",
            help="A fragment of the item URLs. Leave empty to auto-detect.",
        )
    with c2:
        limit = st.number_input("Max items (0 = all)", 0, 5000, 0, step=10)

    api_key = ""
    model = "gemini-2.5-flash"
    ollama_host = "http://localhost:11434"

    if engine == "gemini":
        default_key = os.environ.get("GEMINI_API_KEY", "")
        try:
            default_key = default_key or st.secrets.get("GEMINI_API_KEY", "")
        except Exception:
            pass
        api_key = st.text_input(
            "Gemini API key", value=default_key, type="password",
            help="Get one free at aistudio.google.com/app/apikey",
        )
        model = st.text_input("Model", value="gemini-2.5-flash")
    elif engine == "ollama":
        oc1, oc2 = st.columns(2)
        with oc1:
            model = st.text_input("Ollama model", value="qwen3:4b",
                                  help="Must be pulled: ollama pull qwen3:4b")
        with oc2:
            ollama_host = st.text_input(
                "Ollama server", value="http://localhost:11434",
                help="Same machine: localhost. Another one: http://IP:11434",
            )

run = st.button("Collect data")

# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
if run:
    if source == "Fetch a URL" and not url.strip():
        st.warning("Enter a page address to start.")
    elif source == "Paste HTML" and not html_text.strip():
        st.warning("Paste some HTML to start.")
    elif source == "Paste HTML" and html_kind == "links" and not base_url.strip():
        st.warning("For \"links to separate pages\", add the original page URL "
                   "so the links can be resolved and followed.")
    elif engine == "gemini" and not api_key.strip():
        st.warning("The Gemini engine needs an API key. Add it under Options.")
    else:
        log_box = st.empty()
        bar = st.progress(0)
        lines: list[str] = []

        def show(done, total, current):
            pct = int(done / total * 100) if total else 0
            short = current.replace("https://", "").replace("http://", "")
            lines.append(
                f'<span class="n">{done + 1:>3}/{total}</span>  '
                f'<span class="u">{short[:70]}</span>'
            )
            log_box.markdown(
                '<div class="log">' + "\n".join(lines[-40:]) + "</div>",
                unsafe_allow_html=True,
            )
            bar.progress(min(pct, 100))

        try:
            with st.spinner("Reading…"):
                if source == "Fetch a URL":
                    rows, links = scraper.scrape_listing(
                        url.strip(), engine=engine, fields=fields,
                        pattern=pattern.strip() or None,
                        limit=int(limit) or None, api_key=api_key.strip(),
                        model=model.strip(), ollama_host=ollama_host.strip(),
                        progress=show,
                    )
                else:
                    rows, links = scraper.scrape_html(
                        html_text, kind=html_kind, base_url=base_url.strip(),
                        engine=engine, fields=fields,
                        pattern=pattern.strip() or None,
                        limit=int(limit) or None, api_key=api_key.strip(),
                        model=model.strip(), ollama_host=ollama_host.strip(),
                        progress=show,
                    )
            bar.progress(100)
            if not rows:
                st.error(
                    "Nothing was extracted. If you pasted a list, check the "
                    "\"What is this HTML?\" choice, or add a link filter."
                )
            st.session_state.rows = rows
            st.session_state.fields = fields
        except Exception as exc:
            st.error(f"Couldn't process the page: {exc}")

# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
if st.session_state.rows:
    rows = st.session_state.rows
    fields = st.session_state.fields
    df = pd.DataFrame(rows)

    ok = df[[c for c in fields if c in df.columns]].copy()
    filled = sum(1 for r in rows if any(str(r.get(f, "")).strip()
                 for f in fields if f != "source_url"))

    st.markdown("<hr>", unsafe_allow_html=True)
    a, b = st.columns([1, 3])
    with a:
        st.markdown(
            f'<div class="count">{len(rows)}</div>'
            f'<div class="count-label">items</div>', unsafe_allow_html=True)
    with b:
        st.markdown('<div class="eyebrow">Result</div>', unsafe_allow_html=True)
        st.markdown(
            f"{filled} of {len(rows)} items have data. Review the table and "
            "download the CSV. Empty cells can be filled by switching engine "
            "or adjusting the columns.")

    st.dataframe(ok, use_container_width=True, hide_index=True)

    buf = io.StringIO()
    ok.to_csv(buf, index=False)
    st.download_button(
        "Download CSV", buf.getvalue(), file_name="listing.csv", mime="text/csv")