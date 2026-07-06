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
    """Find a short block that IS one of `names`, return the next block as value."""
    lowered = [b.lower() for b in blocks]
    for i, b in enumerate(lowered):
        if len(b) <= 24 and b.strip(": ") in names:
            for j in range(i + 1, min(i + 3, len(blocks))):
                if blocks[j].lower().strip(": ") not in names and len(blocks[j]) > 1:
                    return blocks[j]
    return ""


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

    # Classify outbound links in the main content.
    website = linkedin = email = ""
    host = urlparse(url).netloc
    for a in content.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("mailto:"):
            email = email or href[7:].split("?")[0]
            continue
        low = href.lower()
        if "linkedin.com" in low and not linkedin:
            linkedin = href
        elif href.startswith("http") and urlparse(href).netloc not in ("", host):
            if not any(s in low for s in ("facebook.", "twitter.", "x.com",
                                          "instagram.", "youtube.", "t.me")):
                website = website or href

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
    if not date:
        m = _YEAR_RE.search(" ".join(blocks[-30:]))  # years often sit near the end
        date = m.group(0) if m else ""

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
        "founders": _value_for_label(blocks, _LABELS["founders"]),
        "website": website,
        "linkedin": linkedin,
        "email": email,
        "image": _meta(soup, "og:image"),
        "source_url": url,
    }
    return {f: _clean(values.get(f, "")) for f in fields}


# --------------------------------------------------------------------------- #
# Step 2b — Gemini engine (optional, cheap)
# --------------------------------------------------------------------------- #
def extract_gemini(url: str, html: str, fields: list[str], api_key: str,
                   model: str = "gemini-2.5-flash") -> dict:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    text = main_text(html)
    keys = ", ".join(fields)
    prompt = (
        "Extract information about the single item described on this page.\n"
        f"Return ONLY a JSON object with exactly these keys: {keys}.\n"
        "Use an empty string for anything not present. Do not invent values. "
        "Keep 'description' to at most two sentences.\n"
        f"Always set source_url to: {url}\n\n"
        f"PAGE TEXT:\n{text}"
    )
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
        ),
    )
    try:
        data = json.loads(resp.text)
    except (json.JSONDecodeError, TypeError):
        data = {}
    row = {f: _clean(str(data.get(f, ""))) for f in fields}
    if "source_url" in fields:
        row["source_url"] = url
    return row


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def scrape_listing(list_url: str, engine: str = "rules",
                   fields: list[str] | None = None, pattern: str | None = None,
                   limit: int | None = None, api_key: str | None = None,
                   model: str = "gemini-2.5-flash", delay: float = 0.4,
                   progress=None) -> tuple[list[dict], list[str]]:
    """
    Scrape one listing page and every detail page it links to.

    `progress(done, total, url)` is called before each detail fetch so a UI can
    show a live status. Returns (rows, discovered_links).
    """
    fields = fields or DEFAULT_FIELDS
    session = requests.Session()

    list_html = fetch(list_url, session=session)
    links = discover_links(list_url, list_html, pattern=pattern)
    if limit:
        links = links[:limit]

    rows: list[dict] = []
    for i, url in enumerate(links):
        if progress:
            progress(i, len(links), url)
        try:
            page = fetch(url, session=session)
            if engine == "gemini":
                row = extract_gemini(url, page, fields, api_key, model)
            else:
                row = extract_rules(url, page, fields)
        except Exception as exc:  # keep going; record the failure in the row
            row = {f: "" for f in fields}
            if "source_url" in fields:
                row["source_url"] = url
            row["_error"] = str(exc)
        rows.append(row)
        if delay:
            time.sleep(delay)

    return rows, links
