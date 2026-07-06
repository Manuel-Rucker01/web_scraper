# web_scrapper
This repo holds a hybrid web scrapper that uses rules or LLMs to extract certain information about different events via its url. It generates a CSV file with the structured scrapped information.

# Recolector de listados

Una app web sencilla: pegas la dirección de una página con un listado
(eventos, startups, un directorio) y te descargas un CSV con una fila por
elemento.

Trae **tres motores**:

- **Gratis (sin IA)** — motor por defecto. Lee la estructura de la web
  (metadatos, enlaces, etiquetas) sin llamar a ninguna API. Coste: 0 €.
  Funciona muy bien en webs con plantilla (WordPress, directorios, listados).
- **Local (Ollama)** — usa un modelo pequeño que corre en tu propio ordenador
  con [Ollama](https://ollama.com). Gratis, privado y sin cuenta. Ideal para
  webs desordenadas si no quieres depender de una API en la nube.
- **Gemini (nube)** — manda el texto de cada página a Google Gemini. Robusto y
  muy rápido. Usa un modelo *Flash* (barato): céntimos por cientos de páginas.

La idea es empezar con el gratuito y subir a Local o Gemini solo cuando una web
concreta no se lea bien.

### El motor Local (Ollama), en detalle

1. Instala Ollama y descarga un modelo pequeño:
   ```
   ollama pull qwen3:4b
   ```
   (También van bien `llama3.1:8b`, `qwen2.5:7b`, `gemma3:4b`, o el modelo
   `Inference/Schematron:3B`, hecho a medida para extraer datos de webs.)
2. En la app, elige **Local (Ollama)** y pon el nombre del modelo. Si Ollama
   corre en otra máquina de tu red, cambia el servidor a `http://IP:11434`.

Por qué funciona con modelos pequeños: la tarea es *copiar* datos de un texto,
no razonar. Además, la app le pasa a Ollama un **esquema JSON** (`format`), que
obliga al modelo a devolver justo esos campos en JSON válido — se elimina el
fallo típico de "JSON roto". Con eso, un modelo de 3–4B suele bastar.

**Aviso importante:** el motor Local solo funciona si la app corre en una
máquina que llega a Ollama. En Streamlit Cloud (gratis) **no** hay Ollama, así
que allí solo sirven *Gratis* o *Gemini*. Local es para cuando la app corre en
tu ordenador o en un servidor tuyo.

---

## Cómo dar la página

Dos formas de pasarle la página a la app:

- **Fetch a URL** — le das la dirección y ella descarga la página de listado y
  las fichas que enlaza. Es lo normal para webs públicas.
- **Paste HTML** — pegas el HTML de la página (clic derecho → *Inspeccionar*,
  copias el elemento `<html>`, o usas *Ver código fuente*). Útil cuando la web
  pide login o carga con JavaScript y la descarga directa no ve el contenido
  real. Al pegar HTML eliges qué es:
  - *A single item page* → una sola ficha → una fila (funciona sin internet).
  - *A list with items on this same page* → todo el listado está en esa página
    (tarjetas) → una fila por tarjeta.
  - *A list that links to separate item pages* → un listado que enlaza a fichas
    → sigue esos enlaces y los recorre (necesita internet y la URL original
    para resolver los enlaces relativos).

## Cómo usarla en tu ordenador

1. Instala Python 3.10 o superior.
2. En una terminal, dentro de esta carpeta:
   ```
   pip install -r requirements.txt
   streamlit run app.py
   ```
3. Se abre sola en el navegador. Pega la dirección del listado y pulsa
   **Extraer datos**.

## Cómo publicarla en internet (gratis, sin servidores)

Pensado para que lo pueda hacer alguien no técnico:

1. Crea una cuenta en [github.com](https://github.com) y sube esta carpeta a
   un repositorio nuevo.
2. Entra en [share.streamlit.io](https://share.streamlit.io) con esa misma
   cuenta de GitHub.
3. Pulsa **New app**, elige el repositorio y el archivo `app.py`, y **Deploy**.
4. En un par de minutos tendrás una dirección pública para compartir.

Si vas a usar el motor Gemini en la versión publicada, añade la clave en
**Settings → Secrets** de la app con esta línea:
```
GEMINI_API_KEY = "tu_clave"
```

## Cambiar la clave de Gemini

Tres formas, de la más fácil a la más permanente: escribirla en la propia app
(en *Opciones*), pasarla como variable `GEMINI_API_KEY` al arrancar, o ponerla
en `.streamlit/secrets.toml`. Ver `.env.example`.
La clave gratuita se saca en <https://aistudio.google.com/app/apikey>.

---

## Notas para desarrollo

- `scraper.py` — toda la lógica (descarga, detección de fichas, extracción
  por reglas y por Gemini/Ollama). No depende de Streamlit.
- `app.py` — solo la interfaz (en inglés).
- El motor por reglas rellena: nombre, categoría, descripción, imagen, web,
  LinkedIn de la empresa, **fundadores** (a partir de los LinkedIn personales),
  email, fecha/año y ubicación.
- **Los motores de IA (Gemini/Ollama) ahora *enriquecen* el resultado de las
  reglas, no lo sustituyen.** Las reglas aportan los campos estructurados
  (enlaces, imagen, fechas, fundadores) y la IA solo reescribe los campos de
  texto (descripción, categoría, ubicación). Así la IA nunca borra una web o
  una imagen que las reglas sí encontraron. Además hay reintentos con espera
  por si la API responde con límite de peticiones (rate limit).
- Para enseñarle etiquetas nuevas al motor por reglas, amplía los diccionarios
  `_LABELS` en `scraper.py`.
- v1 cubre el patrón "página de listado → fichas".

## Sobre el coste de usar un LLM

El miedo a que "usar un LLM salga caro" viene casi siempre de hacerlo mal:
mandar el HTML entero, usar un modelo grande, o dejar que el LLM haga *todo*
el proceso. Aquí el LLM solo interviene, si lo activas, en el último paso
(sacar los datos de un texto ya limpio) y con un modelo *Flash*. A los precios
de *Flash*, una página son ~1–2 mil tokens: cientos de páginas cuestan
céntimos. El motor gratuito, además, no cuesta nada.
