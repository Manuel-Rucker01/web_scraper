"""
scraper.py — Collects items from a listing/directory page into structured rows.

Two extraction engines:
  - "rules"  (default): free, no API calls. Uses page metadata (OpenGraph /
             JSON-LD), link classification and a label -> value heuristic.
             Works well on templated sites (WordPress, listings, directories).
  - "gemini" (optional): sends the cleaned page text to Google Gemini and asks
             for structured JSON. Robust on messy / non-templated sites.
             Uses a cheap Flash model, so cost is a fraction of a cent per page.

The two engines share the same output shape, so the CSV looks identical either
way. You can start free and only flip to Gemini for sites the rules engine
can't parse well.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Default columns. Fits both event listings and startup/company directories.
# The UI lets the user edit this list; unknown fields just come back empty.
DEFAULT_FIELDS = [
    "name",
    "category",
    "description",
    "date",
    "location",
    "website",
    "linkedin",
    "founders",
    "email",
    "image",
    "source_url",
]

_UA = "Mozilla/5.0 (compatible; ListingScraper/1.0)"

# Label vocabulary (Spanish + English) used to pair a heading/label with the
# text that follows it. Extend these lists to teach the rules engine new labels.
_LABELS = {
    "category": ["category", "categoria", "categoría", "sector", "sectores",
                 "industry", "industria", "area", "área", "tipo", "ambito", "ámbito"],
    "date":     ["date", "fecha", "año", "ano", "year", "when", "cuando", "cuándo",
                 "dia", "día", "edicion", "edición"],
    "location": ["location", "ubicacion", "ubicación", "ciudad", "city", "lugar",
                 "where", "donde", "dónde", "place", "sede", "pais", "país", "region", "región"],
    "founders": ["founders", "fundadores", "fundador", "team", "equipo", "ceo", "founder"],
}

# Every label word, so a value-lookup never returns another field's label
# (this is what caused date="CIUDAD": the value slot grabbed the next label).
_ALL_LABELS = {w for words in _LABELS.values() for w in words}

# First-path-segments that are almost always navigation, not real detail items.
_NAV_SEGMENTS = {
    "", "tag", "tags", "category", "categoria", "author", "page", "search",
    "wp-content", "wp-login", "wp-admin", "feed", "contact", "contacto",
    "privacy", "privacidad", "cookies", "legal", "about", "blog", "news",
    "login", "signin", "signup", "cart", "checkout",
}

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def fetch(url: str, session: requests.Session | None = None, timeout: int = 20) -> str:
    """Download a URL and return its HTML. Raises on HTTP errors."""
    getter = session or requests
    resp = getter.get(
        url,
        headers={"User-Agent": _UA, "Accept-Language": "es,en;q=0.8"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.text


def _soup(html: str) -> BeautifulSoup:
    # html.parser is pure-Python: no lxml build step, so deployment stays simple.
    return BeautifulSoup(html, "html.parser")


# --------------------------------------------------------------------------- #
# Step 1 — find the detail-page links on a listing page
# --------------------------------------------------------------------------- #
def discover_links(base_url: str, html: str, pattern: str | None = None,
                   min_group: int = 3) -> list[str]:
    """
    Return the list of item/detail URLs found on a listing page.

    If `pattern` (a regex) is given, keep only links matching it — this is the
    most reliable option (e.g. "/startup/" or "/event/").

    Otherwise, infer the pattern: group same-site links by their first path
    segment and pick the largest plausible group. On a directory page the real
    items ("/startup/x", "/startup/y", ...) vastly outnumber nav links, so this
    usually lands on the right group by itself.
    """
    soup = _soup(html)
    base_host = urlparse(base_url).netloc
    base_clean = base_url.split("#")[0].rstrip("/")

    seen: set[str] = set()
    uniq: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absu = urljoin(base_url, href).split("#")[0].rstrip("/")
        if urlparse(absu).netloc != base_host:
            continue
        if absu == base_clean or absu in seen:
            continue
        seen.add(absu)
        uniq.append(absu)

    if pattern:
        rx = re.compile(pattern)
        return [u for u in uniq if rx.search(u)]

    def first_seg(u: str) -> str:
        segs = [s for s in urlparse(u).path.split("/") if s]
        return segs[0] if segs else ""

    groups = Counter(first_seg(u) for u in uniq)
    best_seg, best_n = None, 0
    for seg, n in groups.items():
        if seg in _NAV_SEGMENTS:
            continue
        if n > best_n and n >= min_group:
            best_seg, best_n = seg, n

    if best_seg is None:
        return uniq  # couldn't infer — hand everything back, user can filter
    return [u for u in uniq if first_seg(u) == best_seg]


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _meta(soup: BeautifulSoup, *keys: str) -> str:
    for key in keys:
        tag = (soup.find("meta", attrs={"property": key})
               or soup.find("meta", attrs={"name": key}))
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _strip_site_suffix(title: str, site_name: str) -> str:
    """Turn 'Bumerania Robotics - Alhambra Venture' into 'Bumerania Robotics'."""
    if not title:
        return ""
    if site_name:
        for sep in (" - ", " | ", " — ", " · "):
            suffix = f"{sep}{site_name}"
            if title.endswith(suffix):
                return title[: -len(suffix)].strip()
    return re.split(r"\s+[-|—·]\s+", title)[0].strip()


def _content_soup(html: str) -> BeautifulSoup:
    """Soup with nav/footer/scripts removed, so we only see the real content."""
    soup = _soup(html)
    for tag in soup(["script", "style", "nav", "footer", "header",
                     "form", "noscript", "svg"]):
        tag.decompose()
    return soup


def _linear_blocks(soup: BeautifulSoup) -> list[str]:
    """Ordered list of visible text chunks — used for label -> value pairing."""
    blocks: list[str] = []
    for node in soup.find_all(string=True):
        txt = _clean(str(node))
        if txt:
            blocks.append(txt)
    return blocks


def _value_for_label(blocks: list[str], names: list[str]) -> str:
    """Find a short block that IS one of `names`, return the next real block.

    The value is never allowed to be another field's label word, otherwise a
    layout like  año / CIUDAD / Alicante  would return "CIUDAD" for the year.
    """
    lowered = [b.lower() for b in blocks]
    for i, b in enumerate(lowered):
        if len(b) <= 24 and b.strip(": ") in names:
            for j in range(i + 1, min(i + 4, len(blocks))):
                candidate = blocks[j].lower().strip(": ")
                if len(blocks[j]) > 1 and candidate not in _ALL_LABELS:
                    return blocks[j]
    return ""


def _name_from_slug(slug: str) -> str:
    """linkedin.com/in/raul-rojano-cruz-0a8a51316 -> 'Raul Rojano Cruz'."""
    slug = slug.strip("/").split("?")[0].split("/")[0]
    slug = re.sub(r"-[0-9a-f]{5,}$", "", slug)          # drop trailing id hash
    parts = [p for p in slug.split("-") if p and not p.isdigit()]
    return " ".join(w.capitalize() for w in parts).strip()


def main_text(html: str, limit: int = 6000) -> str:
    """Cleaned, readable page text — what we hand to Gemini."""
    soup = _content_soup(html)
    text = _clean(soup.get_text(" "))
    return text[:limit]


# --------------------------------------------------------------------------- #
# Step 2a — rules engine (free)
# --------------------------------------------------------------------------- #
def extract_rules(url: str, html: str, fields: list[str]) -> dict:
    soup = _soup(html)
    site_name = _meta(soup, "og:site_name")
    content = _content_soup(html)
    blocks = _linear_blocks(content)

    # Classify outbound links. Keep three buckets distinct so the same URL can
    # never appear in two columns:
    #   - company LinkedIn      -> linkedin
    #   - personal LinkedIn(s)  -> founders (people, not the company)
    #   - a real external site  -> website  (never a LinkedIn/social/image URL)
    website = linkedin = email = ""
    founders: list[str] = []
    host = urlparse(url).netloc
    img_host = urlparse(_meta(soup, "og:image")).netloc
    _social = ("facebook.", "twitter.", "x.com", "instagram.",
               "youtube.", "t.me", "tiktok.", "pinterest.")
    for a in content.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if href.startswith("mailto:"):
            email = email or href[7:].split("?")[0]
            continue
        if not href.startswith("http"):
            continue
        netloc = urlparse(href).netloc

        if "linkedin.com/company/" in low:
            linkedin = linkedin or href
        elif "linkedin.com/in/" in low:
            txt = _clean(a.get_text())
            name = txt if (len(txt) > 3 and txt.upper() not in
                           ("LINKEDIN", "LINKED IN", "WEB")) else \
                _name_from_slug(low.split("/in/", 1)[1])
            if name and name not in founders:
                founders.append(name)
        elif ("linkedin.com" not in low and not any(s in low for s in _social)
              and netloc not in ("", host, img_host)):
            website = website or href

    # If there was no company page, fall back to a personal LinkedIn for the
    # linkedin column so the field isn't empty when only founders are listed.
    if not linkedin:
        for a in content.find_all("a", href=True):
            if "linkedin.com/in/" in a["href"].lower():
                linkedin = a["href"].strip()
                break

    if not email:
        m = _EMAIL_RE.search(content.get_text(" "))
        if m:
            email = m.group(0)

    # Name: OG title (minus site suffix) -> first heading -> <title>.
    name = _strip_site_suffix(_meta(soup, "og:title"), site_name)
    if not name:
        h1 = soup.find(["h1", "h2"])
        name = _clean(h1.get_text()) if h1 else _strip_site_suffix(
            _clean(soup.title.get_text()) if soup.title else "", site_name)

    description = _meta(soup, "og:description", "description")
    if not description:
        for p in content.find_all("p"):
            t = _clean(p.get_text())
            if len(t) > 60:
                description = t
                break

    date = _value_for_label(blocks, _LABELS["date"])
    if not any(ch.isdigit() for ch in date):        # a real date has a number
        m = _YEAR_RE.search(" ".join(blocks[-40:]))  # years often sit near the end
        date = m.group(0) if m else ""

    # Founders: names gathered from personal LinkedIn links, else a labeled block.
    founders_val = "; ".join(founders) or _value_for_label(blocks, _LABELS["founders"])

    # Category: try a labeled block first; else the "eyebrow" — a short text
    # chunk sitting just before the name (breadcrumb / tag on many templates).
    category = _value_for_label(blocks, _LABELS["category"])
    if not category and name:
        try:
            name_idx = next(i for i, b in enumerate(blocks) if b == name)
            for b in reversed(blocks[:name_idx]):
                if 1 < len(b) <= 30 and not _YEAR_RE.fullmatch(b) and not b.isdigit():
                    category = b
                    break
        except StopIteration:
            pass

    values = {
        "name": name,
        "category": category,
        "description": description,
        "date": date,
        "location": _value_for_label(blocks, _LABELS["location"]),
        "founders": founders_val,
        "website": website,
        "linkedin": linkedin,
        "email": email,
        "image": _meta(soup, "og:image"),
        "source_url": url,
    }
    return {f: _clean(values.get(f, "")) for f in fields}


# --------------------------------------------------------------------------- #
# LLM enrichment (shared by Gemini and Ollama)
# --------------------------------------------------------------------------- #
# The rules engine reliably gets the *structured* fields (links, image, dates,
# founders from LinkedIn). An LLM only sees cleaned text, so it can't recover a
# URL or an og:image — asking it to is how the AI run ended up with empty
# website/linkedin/image columns. So we let the LLM improve only the *prose*
# fields, and never let it blank out something the rules engine already found.
_LLM_CONTENT_FIELDS = ["name", "category", "description", "location", "date", "founders"]
_LLM_PREFER = {"category", "description", "location"}  # LLM wins if non-empty
_LLM_FILL_ONLY = {"name", "date", "founders"}          # LLM only fills the gaps


def _with_retries(fn, tries: int = 3, base_delay: float = 2.0):
    """Call fn(); on failure (e.g. a rate-limit 429) back off and retry."""
    last = None
    for k in range(tries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if k < tries - 1:
                time.sleep(base_delay * (2 ** k))
    raise last


def _merge_llm(base: dict, data: dict, ask: list[str]) -> dict:
    for f in ask:
        val = _clean(str(data.get(f, "")))
        if not val:
            continue
        if f in _LLM_PREFER:
            base[f] = val
        elif f in _LLM_FILL_ONLY and not base.get(f):
            base[f] = val
    return base


# --------------------------------------------------------------------------- #
# Step 2b — Gemini engine (cloud). Enriches the rules result.
# --------------------------------------------------------------------------- #
def extract_gemini(url: str, html: str, fields: list[str], api_key: str,
                   model: str = "gemini-2.5-flash") -> dict:
    from google import genai
    from google.genai import types

    base = extract_rules(url, html, fields)
    ask = [f for f in fields if f in _LLM_CONTENT_FIELDS]
    if not ask:
        return base

    client = genai.Client(api_key=api_key)
    prompt = (
        "This page describes one item. Return ONLY a JSON object with these "
        f"keys: {', '.join(ask)}.\n"
        "Copy values from the text; use an empty string if absent; never "
        "invent. Write 'description' as at most two clear sentences.\n\n"
        f"PAGE TEXT:\n{main_text(html)}"
    )

    def call():
        r = client.models.generate_content(
            model=model, contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0, response_mime_type="application/json"),
        )
        return json.loads(r.text)

    try:
        data = _with_retries(call)
    except Exception:  # noqa: BLE001 — fall back to the rules result
        data = {}
    return _merge_llm(base, data, ask)


# --------------------------------------------------------------------------- #
# Step 2c — local model via Ollama (free, private). Enriches the rules result.
# --------------------------------------------------------------------------- #
def extract_ollama(url: str, html: str, fields: list[str],
                   model: str = "qwen3:4b",
                   host: str = "http://localhost:11434") -> dict:
    """
    Like the Gemini engine but the model runs on your own machine via Ollama.
    `format` (a JSON schema) constrains the model to valid JSON — this is what
    makes small local models reliable here. `host` can point at another machine
    on the network, e.g. "http://192.168.1.50:11434".
    """
    import ollama

    base = extract_rules(url, html, fields)
    ask = [f for f in fields if f in _LLM_CONTENT_FIELDS]
    if not ask:
        return base

    client = ollama.Client(host=host)
    schema = {
        "type": "object",
        "properties": {f: {"type": "string"} for f in ask},
        "required": ask,
    }
    prompt = (
        "This page describes one item. Fill exactly these keys: "
        f"{', '.join(ask)}.\n"
        "Copy values from the text; use an empty string if absent; never "
        "invent. Write 'description' as at most two clear sentences.\n\n"
        f"PAGE TEXT:\n{main_text(html)}"
    )

    def call():
        r = client.chat(
            model=model, messages=[{"role": "user", "content": prompt}],
            format=schema, options={"temperature": 0},
        )
        return json.loads(r["message"]["content"])

    try:
        data = _with_retries(call)
    except Exception:  # noqa: BLE001
        data = {}
    return _merge_llm(base, data, ask)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _extract_by_engine(url, html, fields, engine, api_key, model, ollama_host):
    if engine == "gemini":
        return extract_gemini(url, html, fields, api_key, model)
    if engine == "ollama":
        return extract_ollama(url, html, fields, model, ollama_host)
    return extract_rules(url, html, fields)


def _scrape_detail_links(links, engine, fields, api_key, model, ollama_host,
                         delay, progress, session=None):
    session = session or requests.Session()
    rows: list[dict] = []
    for i, url in enumerate(links):
        if progress:
            progress(i, len(links), url)
        try:
            page = fetch(url, session=session)
            row = _extract_by_engine(url, page, fields, engine, api_key,
                                     model, ollama_host)
        except Exception as exc:  # keep going; record the failure in the row
            row = {f: "" for f in fields}
            if "source_url" in fields:
                row["source_url"] = url
            row["_error"] = str(exc)
        rows.append(row)
        if delay:
            time.sleep(delay)
    return rows


def scrape_listing(list_url: str, engine: str = "rules",
                   fields: list[str] | None = None, pattern: str | None = None,
                   limit: int | None = None, api_key: str | None = None,
                   model: str = "gemini-2.5-flash",
                   ollama_host: str = "http://localhost:11434",
                   delay: float = 0.4, progress=None) -> tuple[list[dict], list[str]]:
    """
    Scrape a listing page (given by URL) and every detail page it links to.
    Returns (rows, discovered_links).
    """
    fields = fields or DEFAULT_FIELDS
    session = requests.Session()
    list_html = fetch(list_url, session=session)
    links = discover_links(list_url, list_html, pattern=pattern)
    if limit:
        links = links[:limit]
    rows = _scrape_detail_links(links, engine, fields, api_key, model,
                                ollama_host, delay, progress, session)
    return rows, links


# --------------------------------------------------------------------------- #
# Cards on a single page (no detail links, or all data inline)
# --------------------------------------------------------------------------- #
def extract_cards(html: str, base_url: str = "", fields: list[str] | None = None,
                  limit: int | None = None) -> list[dict]:
    """
    Pull one row per item from a single page whose items sit in a repeated
    "card" layout. Rules-only (each card holds little text, so this reads
    whatever is there: a title, its link, an image). `base_url` lets relative
    links resolve to full URLs.
    """
    fields = fields or DEFAULT_FIELDS
    content = _content_soup(html)

    # Candidate cards = links that repeat under a common parent (a grid).
    anchors = []
    for a in content.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        anchors.append(a)

    # Group anchors by their parent element; the biggest group is the grid.
    from collections import defaultdict
    by_parent = defaultdict(list)
    for a in anchors:
        by_parent[id(a.parent)].append(a)
    groups = sorted(by_parent.values(), key=len, reverse=True)

    cards: list = []
    if groups and len(groups[0]) >= 2:
        cards = groups[0]
    else:
        # Fall back: treat each heading as an item.
        cards = content.find_all(["h2", "h3"])

    rows: list[dict] = []
    seen: set[str] = set()
    for el in cards:
        a = el if getattr(el, "name", "") == "a" else el.find("a", href=True)
        href = urljoin(base_url, a["href"].strip()) if a and a.get("href") else ""
        heading = el.find(["h1", "h2", "h3", "h4"])
        name = _clean(heading.get_text()) if heading else _clean(el.get_text())
        name = name[:120]
        if not name or name in seen:
            continue
        seen.add(name)
        img = el.find("img")
        image = urljoin(base_url, img["src"]) if img and img.get("src") else ""
        text = _clean(el.get_text(" "))
        desc = text[len(name):].strip() if text.startswith(name) else text
        values = {"name": name, "description": desc[:400], "image": image,
                  "website": href, "source_url": href or base_url}
        rows.append({f: _clean(values.get(f, "")) for f in fields})
        if limit and len(rows) >= limit:
            break
    return rows


# --------------------------------------------------------------------------- #
# Entry point for pasted HTML (from "Inspect" / view-source / a saved file)
# --------------------------------------------------------------------------- #
def scrape_html(html: str, kind: str = "one", base_url: str = "",
                engine: str = "rules", fields: list[str] | None = None,
                pattern: str | None = None, limit: int | None = None,
                api_key: str | None = None, model: str = "gemini-2.5-flash",
                ollama_host: str = "http://localhost:11434",
                delay: float = 0.4, progress=None) -> tuple[list[dict], list[str]]:
    """
    Handle HTML the user pasted instead of a URL.

    kind:
      "one"   — the HTML is a single item page -> one row (fully offline).
      "cards" — the HTML lists items on this same page -> one row per card.
      "links" — the HTML is a listing that links to item pages -> follow those
                links and scrape each (needs network + a base_url to resolve
                relative links).
    """
    fields = fields or DEFAULT_FIELDS

    if kind == "cards":
        rows = extract_cards(html, base_url=base_url, fields=fields, limit=limit)
        return rows, [r.get("source_url", "") for r in rows]

    if kind == "links":
        links = discover_links(base_url or "http://example.com/", html,
                               pattern=pattern)
        if limit:
            links = links[:limit]
        rows = _scrape_detail_links(links, engine, fields, api_key, model,
                                    ollama_host, delay, progress)
        return rows, links

    # kind == "one"
    row = _extract_by_engine(base_url, html, fields, engine, api_key, model,
                             ollama_host)
    return [row], [base_url or row.get("source_url", "")]