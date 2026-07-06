"""
app.py — Interfaz web para extraer datos de páginas de listados a un CSV.

Ejecutar en local:   streamlit run app.py
"""

import io
import os

import pandas as pd
import streamlit as st

import scraper

st.set_page_config(page_title="Recolector de listados", layout="centered")

# --------------------------------------------------------------------------- #
# Aspecto: identidad "mesa de trabajo de datos". Tipografías Space Grotesk
# (títulos) + IBM Plex Sans (texto) + IBM Plex Mono (datos/registro).
# --------------------------------------------------------------------------- #
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap');

:root {
  --ink:#17252b; --muted:#5f7176; --canvas:#eceff0; --line:#d5dcdd;
  --accent:#146b63; --signal:#b5642a; --surface:#ffffff;
}

/* Ocultar el cromo por defecto de Streamlit */
#MainMenu, header[data-testid="stHeader"], [data-testid="stToolbar"],
footer, [data-testid="stDecoration"] { display:none !important; }

.stApp { background:var(--canvas); }
.block-container { max-width:760px; padding-top:2.4rem; padding-bottom:4rem; }

html, body, [class*="css"], .stMarkdown, p, label, input, textarea, div {
  font-family:'IBM Plex Sans', system-ui, sans-serif; color:var(--ink);
}

/* Cabecera */
.wm { font-family:'Space Grotesk', sans-serif; font-weight:700;
      font-size:1.75rem; letter-spacing:-0.02em; color:var(--ink);
      display:flex; align-items:center; gap:.55rem; margin:0; }
.wm-mark { width:14px; height:14px; border-radius:3px; background:var(--accent);
           box-shadow:5px 0 0 var(--signal); }
.sub { color:var(--muted); font-size:.95rem; margin:.35rem 0 1.6rem 1.9rem;
       max-width:46ch; }

/* Tarjetas / bloques */
.panel { background:var(--surface); border:1px solid var(--line);
         border-radius:10px; padding:1.1rem 1.2rem; margin-bottom:1rem; }

/* Campos de texto en mono, como una consola de datos */
.stTextInput input, .stNumberInput input {
  font-family:'IBM Plex Mono', monospace !important; font-size:.92rem;
  border-radius:7px !important; border:1px solid var(--line) !important;
}
.stTextInput input:focus { border-color:var(--accent) !important;
  box-shadow:0 0 0 2px rgba(20,107,99,.15) !important; }

/* Botón primario */
.stButton>button, .stDownloadButton>button {
  font-family:'Space Grotesk', sans-serif !important; font-weight:600 !important;
  border-radius:7px !important; border:1px solid var(--accent) !important;
  background:var(--accent) !important; color:#fff !important;
  padding:.5rem 1.3rem !important; letter-spacing:.01em;
}
.stButton>button:hover, .stDownloadButton>button:hover { filter:brightness(1.08); }
.stDownloadButton>button { background:var(--surface) !important; color:var(--accent) !important; }

/* Registro de ejecución (elemento distintivo) */
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
# Cabecera
# --------------------------------------------------------------------------- #
st.markdown(
    '<div class="wm"><span class="wm-mark"></span>Recolector de listados</div>'
    '<div class="sub">Pega la dirección de una página con un listado '
    '(eventos, startups, un directorio) y descarga una tabla en CSV con una '
    'fila por elemento.</div>',
    unsafe_allow_html=True,
)

# --------------------------------------------------------------------------- #
# Estado
# --------------------------------------------------------------------------- #
if "rows" not in st.session_state:
    st.session_state.rows = None
    st.session_state.fields = None

# --------------------------------------------------------------------------- #
# Entrada principal
# --------------------------------------------------------------------------- #
url = st.text_input(
    "Dirección de la página de listado",
    placeholder="https://alhambraventure.com/startups/",
)

with st.expander("Opciones"):
    engine_label = st.radio(
        "Motor de extracción",
        ["Gratis (sin IA)", "Gemini (para webs difíciles)"],
        help="Empieza con el gratuito. Si una web no se lee bien, prueba Gemini.",
    )
    engine = "gemini" if engine_label.startswith("Gemini") else "rules"

    cols_text = st.text_input(
        "Columnas del CSV (separadas por comas)",
        value=", ".join(scraper.DEFAULT_FIELDS),
    )
    fields = [c.strip() for c in cols_text.split(",") if c.strip()]

    c1, c2 = st.columns(2)
    with c1:
        pattern = st.text_input(
            "Filtro de enlaces (opcional)",
            placeholder="/startup/",
            help="Un fragmento de la URL de las fichas. Si se deja vacío, se "
                 "detecta solo.",
        )
    with c2:
        limit = st.number_input("Máximo de fichas (0 = todas)", 0, 5000, 0, step=10)

    api_key = ""
    model = "gemini-2.5-flash"
    if engine == "gemini":
        default_key = os.environ.get("GEMINI_API_KEY", "")
        try:
            default_key = default_key or st.secrets.get("GEMINI_API_KEY", "")
        except Exception:
            pass
        api_key = st.text_input(
            "Clave de la API de Gemini", value=default_key, type="password",
            help="Se consigue gratis en aistudio.google.com/app/apikey",
        )
        model = st.text_input("Modelo", value="gemini-2.5-flash")

run = st.button("Extraer datos")

# --------------------------------------------------------------------------- #
# Ejecución
# --------------------------------------------------------------------------- #
if run:
    if not url.strip():
        st.warning("Escribe la dirección de una página para empezar.")
    elif engine == "gemini" and not api_key.strip():
        st.warning("El motor Gemini necesita una clave de API. Añádela en Opciones.")
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
            rows, links = scraper.scrape_listing(
                url.strip(), engine=engine, fields=fields,
                pattern=pattern.strip() or None,
                limit=int(limit) or None, api_key=api_key.strip(),
                model=model.strip(), progress=show,
            )
            bar.progress(100)
            if not links:
                st.error(
                    "No se encontraron fichas en esa página. Prueba a rellenar "
                    "el «Filtro de enlaces» en Opciones (por ejemplo /startup/)."
                )
            st.session_state.rows = rows
            st.session_state.fields = fields
        except Exception as exc:
            st.error(f"No se pudo leer la página: {exc}")

# --------------------------------------------------------------------------- #
# Resultados
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
            f'<div class="count-label">fichas</div>', unsafe_allow_html=True)
    with b:
        st.markdown('<div class="eyebrow">Resultado</div>', unsafe_allow_html=True)
        st.markdown(
            f"{filled} de {len(rows)} fichas con datos. Revisa la tabla y "
            "descarga el CSV. Las celdas vacías se pueden completar cambiando "
            "de motor o afinando las columnas.")

    st.dataframe(ok, use_container_width=True, hide_index=True)

    buf = io.StringIO()
    ok.to_csv(buf, index=False)
    st.download_button(
        "Descargar CSV", buf.getvalue(), file_name="listado.csv", mime="text/csv")
