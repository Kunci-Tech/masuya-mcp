#!/usr/bin/env python3
"""
Zero-dependency, read-only client for the public Masuya product catalog.

Source: https://masuya.co.id/product  (PT Masuya Graha Trikencana)

There is NO login, NO API key and NO authenticated API. Everything this client
reads is public HTML that a normal browser can see. It deliberately never
posts to inquiry/contact forms, never follows cart or account flows, and never
sends credentials, cookies or personal data.

Discovered surface (verified 2026-11-12):
  GET /product                         product grid, 10 items/page, 62 pages
  GET /product?keyword=<text>          server-side product search
  GET /product?cat=<b64id>             top-level category filter
  GET /product?cat=<b64id>&sub=<b64id> subcategory filter
  GET /product?brand=<b64id>[,...]     brand filter (comma separated)
  GET /product?sort=<value>            asc | desc | alphabet-asc | alphabet-desc
  GET /product?page=<n>                pagination (filters are preserved)
  GET /<product-slug>                  product detail (case-sensitive slug!)
  GET /sitemap.xml                     product + page URL inventory
  GET /listProductCategory?page=N&url=product  AJAX "load more" (returns
      {"html":..., "loadmore":...}; only useful inside a browser session)

Category/brand/subcategory IDs are base64-encoded integers (e.g. MTg= -> 18,
NDc= -> 47). They are passed through unchanged; a decoded numeric id is
exposed for readability only.

Public data available per product: name, brand, image, item code (usually in
the image filename), description, packaging (carton), storage, packing size.
NOT published: prices, stock, ingredients/composition, halal certificates,
nutritional data. The site is inquiry-based ("MAKE A INQUIRY").

All network calls are throttled and cached. A full catalog snapshot is stored
in catalog_index.json so normal research is instant and does not re-hit the
site. Run `python3 server.py --sync` to refresh it.
"""

from __future__ import annotations

import base64
import datetime
import html as html_lib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE_URL = "https://masuya.co.id"
PRODUCT_URL = BASE_URL + "/product"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(HERE, "catalog_index.json")
DETAILS_PATH = os.path.join(HERE, "details_cache.json")
MAP_PATH = os.path.join(HERE, "api_map.json")

DEFAULT_TIMEOUT = 25
MIN_REQUEST_INTERVAL = 0.12       # seconds between live HTTP calls (politeness)
HTTP_CACHE_TTL = 600              # seconds
DETAIL_CACHE_TTL = 7 * 24 * 3600  # detail pages change rarely
SNAPSHOT_TTL = 24 * 3600          # suggest refresh after a day

SORT_VALUES = ["asc", "desc", "alphabet-asc", "alphabet-desc"]
STOP_WORDS = {
    "the", "and", "with", "for", "of", "a", "in", "x", "pack", "pouch",
    "sauce", "premium", "original", "fresh", "product", "ml", "gr", "kg",
    "g", "l", "lt", "gram", "grams", "pcs", "pc",
}

# Generic food words are too weak to justify calling something an alternative.
GENERIC_TOKENS = {
    "cheese", "cream", "milk", "butter", "yogurt", "sauce", "dressing", "food",
    "powder", "juice", "water", "oil", "noodle", "snack", "block", "pack",
}


class MasuyaError(Exception):
    """Any recoverable error while reading the public catalog."""


def log(*args: Any) -> None:
    print(*args, file=sys.stderr, flush=True)


def clean_text(raw: str) -> str:
    """Strip tags/comments/entities and collapse whitespace."""
    text = re.sub(r"<!--[\s\S]*?-->", " ", raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def decode_id(encoded: str) -> str | None:
    """MTg= -> 18, MTQz -> 143. Returns None when it is not a b64 number."""
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        raw = base64.b64decode(padded).decode("utf-8")
        return raw if raw.isdigit() else None
    except Exception:  # noqa: BLE001
        return None


def encode_id(number: int | str) -> str:
    """18 -> MTg= (useful when an agent only knows the numeric id)."""
    return base64.b64encode(str(number).encode("utf-8")).decode("ascii")


def normalize_storage(raw: str | None) -> str:
    value = (raw or "").upper()
    if "FROZ" in value or "FREEZ" in value:
        return "frozen"
    if "CHILL" in value or "COLD" in value:
        return "chilled"
    if "DRY" in value or "AMBIENT" in value or "ROOM" in value:
        return "ambient"
    return "unknown"


def parse_pack_size(raw: str | None) -> dict:
    """'1000 ml' -> {value:1000, unit:'ml', base_value:1000, base_unit:'ml'}."""
    if not raw:
        return {}
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|kilo|gr|gram|g|ml|lt|liter|litre|l|pcs|pc)\b",
                      str(raw), re.I)
    if not match:
        return {}
    number = float(match.group(1).replace(",", "."))
    unit = match.group(2).lower()
    if unit in ("kg", "kilo"):
        base, base_unit = number * 1000, "g"
    elif unit in ("gr", "gram", "g"):
        base, base_unit = number, "g"
    elif unit in ("lt", "liter", "litre", "l"):
        base, base_unit = number * 1000, "ml"
    elif unit == "ml":
        base, base_unit = number, "ml"
    else:
        base, base_unit = number, "pcs"
    return {
        "value": number,
        "unit": unit,
        "base_value": round(base, 2),
        "base_unit": base_unit,
    }


def item_code_from_image(image: str, slug: str) -> str:
    """Product codes live in the image filename: 'DF0027  GF ... .png.webp'."""
    try:
        name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(image).path))
        name = re.sub(r"\.(webp|png|jpe?g|gif)$", "", name, flags=re.I)
        token = re.split(r"\s+", name.strip())[0].strip("()_-")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-_/]{1,15}", token):
            return token
    except Exception:  # noqa: BLE001
        pass
    return slug


def _product_from_card(url: str, body: str, page_number: int) -> dict:
    name_m = re.search(r"<h4[^>]*>([\s\S]*?)</h4>", body, re.I)
    brand_m = re.search(r"<h5[^>]*>([\s\S]*?)</h5>", body, re.I)
    img_m = re.search(r"<img[^>]*src=[\"']([^\"']+)[\"']", body, re.I)
    name = clean_text(name_m.group(1)) if name_m else ""
    brand = clean_text(brand_m.group(1)) if brand_m else ""
    image = html_lib.unescape(img_m.group(1)) if img_m else ""
    if image.startswith("/"):
        image = BASE_URL + image
    full_url = urllib.parse.urljoin(BASE_URL, html_lib.unescape(url))
    slug = urllib.parse.unquote(urllib.parse.urlparse(full_url).path).strip("/")
    return {
        "slug": slug,
        "url": full_url,
        "name": name,
        "brand": brand,
        "image": image,
        "item_code": item_code_from_image(image, slug),
        "catalog_page": page_number,
        "categories": [],
    }


def parse_catalog(html_text: str, page_number: int = 1) -> dict:
    """Parse a /product listing page into compact product rows + page count."""
    cards = []
    pattern = re.compile(
        r"<a\s+href=[\"']([^\"']+)[\"']\s+class=[\"']product-list__item[\"'][^>]*>([\s\S]*?)</a>",
        re.I,
    )
    for match in pattern.finditer(html_text):
        product = _product_from_card(match.group(1), match.group(2), page_number)
        if product["name"]:
            cards.append(product)

    page_numbers = [int(n) for n in re.findall(r"product\?[^\"']*?page=(\d+)", html_text)]
    total_pages = max(page_numbers) if page_numbers else page_number
    # A "next" arrow is a reliable signal that more pages exist.
    return {
        "products": cards,
        "page": page_number,
        "total_pages": max(total_pages, page_number),
        "has_next": bool(re.search(r"class=\"arr next\"", html_text)),
    }


def parse_filters(html_text: str) -> dict:
    """Parse the category tree and brand checkbox list from the sidebar."""
    sidebar = html_text.split('<div class="product-list">')[0]
    categories: list[dict] = []
    cat_by_value: dict[str, dict] = {}

    anchor_re = re.compile(r"<a\s+href=[\"']([^\"']*product\?[^\"']*)[\"'][^>]*>([\s\S]*?)</a>", re.I)
    for match in anchor_re.finditer(sidebar):
        href = html_lib.unescape(match.group(1))
        label = clean_text(match.group(2))
        if not label or label.isdigit() or len(label) > 60:
            continue
        query = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        cat = (query.get("cat") or [None])[0]
        sub = (query.get("sub") or [None])[0]
        if not cat:
            continue
        if sub:
            parent = cat_by_value.get(cat)
            if parent is None:
                parent = {"value": cat, "id": decode_id(cat), "name": cat, "subcategories": []}
                cat_by_value[cat] = parent
                categories.append(parent)
            if not any(s["value"] == sub for s in parent["subcategories"]):
                parent["subcategories"].append({
                    "value": sub,
                    "id": decode_id(sub),
                    "name": label,
                    "url": f"{PRODUCT_URL}?cat={urllib.parse.quote(cat, safe='')}&sub={urllib.parse.quote(sub, safe='')}",
                })
        else:
            node = cat_by_value.get(cat)
            if node is None:
                node = {"value": cat, "id": decode_id(cat), "name": label, "subcategories": []}
                cat_by_value[cat] = node
                categories.append(node)
            elif node["name"] == cat:
                node["name"] = label

    for node in categories:
        node["url"] = f"{PRODUCT_URL}?cat={urllib.parse.quote(node['value'], safe='')}"

    brand_re = re.compile(
        r"<input[^>]*origin=[\"']brand[\"'][^>]*value=[\"']([^\"']+)[\"'][^>]*>[\s\S]{0,320}?<label>([^<]*)</label>",
        re.I,
    )
    brands = []
    seen_brands: set[str] = set()
    for match in brand_re.finditer(html_text):
        value, label = match.group(1), clean_text(match.group(2))
        if value in seen_brands or not label:
            continue
        seen_brands.add(value)
        brands.append({
            "value": value,
            "id": decode_id(value),
            "name": label,
            "url": f"{PRODUCT_URL}?brand={urllib.parse.quote(value, safe='')}",
        })
    return {"categories": categories, "brands": brands}


def parse_detail(html_text: str, url: str) -> dict:
    """Parse one public product detail page."""
    start = html_text.find('class="product-detail"')
    section = html_text[start: html_text.find("</section>", start)] if start >= 0 else html_text

    title_m = re.search(r"<div class=\"product-detail__description\">[\s\S]*?<h2[^>]*>([\s\S]*?)</h2>", section, re.I)
    title = clean_text(title_m.group(1)) if title_m else ""

    desc_m = re.search(r"</h2>([\s\S]*?)<div class=\"box-spec", section, re.I)
    description = clean_text(desc_m.group(1)) if desc_m else ""

    specs: dict[str, str] = {}
    for key, value in re.findall(
        r"<div class=\"box-spec__list\">\s*<span><b>([^<]*)</b></span>\s*<span>([^<]*)</span>",
        section, re.I,
    ):
        key_clean = re.sub(r"[^a-z0-9]+", "_", clean_text(key).lower()).strip("_")
        value_clean = clean_text(value)
        if key_clean and value_clean and key_clean not in specs:  # site sometimes duplicates rows
            specs[key_clean] = value_clean

    images = []
    for src in re.findall(r"<div class=\"slider-large__list\">[\s\S]*?<img[^>]*src=[\"']([^\"']+)[\"']", section, re.I):
        full = html_lib.unescape(src)
        if full.startswith("/"):
            full = BASE_URL + full
        if full not in images:
            images.append(full)

    storage = specs.get("storage")
    pack_size = specs.get("packing_size") or specs.get("pack_size")
    slug = urllib.parse.unquote(urllib.parse.urlparse(url).path).strip("/")
    page_title_m = re.search(r"<title>(.*?)</title>", html_text, re.S | re.I)
    page_title = clean_text(page_title_m.group(1)) if page_title_m else ""
    return {
        "slug": slug,
        "url": url,
        "name": title or page_title or slug,
        "description": description,
        "specs": specs,
        "packaging": specs.get("packaging"),
        "storage": storage,
        "storage_class": normalize_storage(storage),
        "packing_size": pack_size,
        "pack_size_normalized": parse_pack_size(pack_size),
        "images": images,
        "item_code": item_code_from_image(images[0], slug) if images else slug,
        "inquiry_only": True,
        "price": None,
        "data_notes": (
            "Masuya publishes no price, stock, ingredient/composition or halal data. "
            "Confirm specifications and pricing with a Masuya sales representative."
        ),
    }


class MasuyaClient:
    """Throttled, cached, read-only HTTP client + catalog index."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT, min_interval: float = MIN_REQUEST_INTERVAL) -> None:
        self.timeout = timeout
        self.min_interval = min_interval
        self._http_cache: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._index: dict | None = None
        self._details: dict | None = None

    # ------------------------------------------------------------------ HTTP
    def _throttle(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def get(self, path_or_url: str, params: dict | None = None, tries: int = 2) -> str:
        url = path_or_url if path_or_url.startswith("http") else BASE_URL + "/" + path_or_url.lstrip("/")
        if params:
            clean = {k: v for k, v in params.items() if v not in (None, "", [])}
            if clean:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean, doseq=True)
        now = time.monotonic()
        cached = self._http_cache.get(url)
        if cached and now - cached[0] < HTTP_CACHE_TTL:
            return cached[1]

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
            "Referer": PRODUCT_URL,
            "Connection": "close",
        }
        last_error: Exception | None = None
        for attempt in range(tries):
            self._throttle()
            try:
                req = urllib.request.Request(url, headers=headers, method="GET")
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    payload = response.read().decode("utf-8", errors="replace")
                self._http_cache[url] = (time.monotonic(), payload)
                return payload
            except urllib.error.HTTPError as exc:
                last_error = MasuyaError(f"HTTP {exc.code} for {url}")
                if exc.code in (400, 401, 403, 404):
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = MasuyaError(f"network error for {url}: {exc}")
            if attempt + 1 < tries:
                time.sleep(0.6 * (attempt + 1))
        raise last_error or MasuyaError(f"request failed: {url}")

    # --------------------------------------------------------------- listings
    def listing(self, page: int = 1, keyword: str | None = None, cat: str | None = None,
                sub: str | None = None, brand: str | None = None, sort: str | None = None) -> dict:
        if sort and sort not in SORT_VALUES:
            raise MasuyaError(f"sort must be one of {', '.join(SORT_VALUES)}")
        params: dict[str, Any] = {"page": page}
        if keyword:
            params["keyword"] = keyword
        if cat:
            params["cat"] = cat
        if sub:
            params["sub"] = sub
        if brand:
            params["brand"] = brand
        if sort:
            params["sort"] = sort
        html_text = self.get("/product", params)
        result = parse_catalog(html_text, page)
        result["filters"] = {k: v for k, v in params.items() if k != "page" and v}
        return result

    def filters(self) -> dict:
        return parse_filters(self.get("/product"))

    def sitemap(self) -> list[str]:
        xml = self.get("/sitemap.xml")
        return [html_lib.unescape(x) for x in re.findall(r"<loc>(.*?)</loc>", xml, re.S)]

    # ----------------------------------------------------------------- index
    def index(self) -> dict:
        """Load the bundled catalog snapshot (or build a minimal one)."""
        if self._index is not None:
            return self._index
        if os.path.isfile(INDEX_PATH):
            with open(INDEX_PATH, encoding="utf-8") as fh:
                self._index = json.load(fh)
            return self._index
        log("catalog_index.json missing - building a minimal 1-page snapshot; run --sync for the full catalog")
        try:
            first = self.listing(1)
            meta = self.filters()
        except Exception as exc:  # noqa: BLE001
            self._index = {"products": [], "categories": [], "brands": [], "partial": True, "error": str(exc)}
            return self._index
        self._index = {
            "source": PRODUCT_URL,
            "synced_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "pages_crawled": 1,
            "total_products": len(first["products"]),
            "products": first["products"],
            "categories": meta["categories"],
            "brands": meta["brands"],
            "partial": True,
            "note": "Only page 1 was fetched. Run `python3 server.py --sync` for all products.",
        }
        return self._index

    def save_index(self, index: dict) -> None:
        tmp = INDEX_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(index, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, INDEX_PATH)
        self._index = index

    def sync(self, tag_categories: bool = True, max_pages: int = 100, progress: bool = False) -> dict:
        """Crawl every public listing page and rebuild catalog_index.json."""
        first_html = self.get("/product", {"page": 1})
        first = parse_catalog(first_html, 1)
        meta = parse_filters(first_html)
        total_pages = min(first["total_pages"], max_pages)
        by_url: dict[str, dict] = {}
        errors: list[str] = []

        for page_number in range(1, total_pages + 1):
            try:
                listing = first if page_number == 1 else self.listing(page=page_number)
                for product in listing["products"]:
                    by_url.setdefault(product["url"], product)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"page {page_number}: {exc}")
            if progress and (page_number % 10 == 0 or page_number == total_pages):
                log(f"  catalog page {page_number}/{total_pages} - {len(by_url)} products")

        categories = meta["categories"]
        if tag_categories:
            for node in categories:
                try:
                    cat_first = self.listing(page=1, cat=node["value"])
                    cat_pages = min(cat_first["total_pages"], max_pages)
                    tagged = set()
                    for page_number in range(1, cat_pages + 1):
                        rows = cat_first["products"] if page_number == 1 else self.listing(
                            page=page_number, cat=node["value"])["products"]
                        for row in rows:
                            tagged.add(row["url"])
                    for url in tagged:
                        if url in by_url and node["name"] not in by_url[url]["categories"]:
                            by_url[url]["categories"].append(node["name"])
                    node["product_count"] = len(tagged)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"category {node['name']}: {exc}")
                    node["product_count"] = None
                if progress:
                    log(f"  category {node['name']:<24} {node.get('product_count')} products")

        products = sorted(by_url.values(), key=lambda p: (p["name"].lower(), p["brand"].lower()))
        index = {
            "source": PRODUCT_URL,
            "supplier": "PT Masuya Graha Trikencana",
            "auth_required": False,
            "synced_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "pages_crawled": total_pages,
            "total_products": len(products),
            "categories": categories,
            "brands": meta["brands"],
            "products": products,
            "partial": False,
            "errors": errors,
            "fields": ["slug", "url", "name", "brand", "image", "item_code", "categories", "catalog_page"],
            "not_published": ["price", "stock", "ingredients", "composition", "halal", "nutrition"],
        }
        self.save_index(index)
        return {
            "synced_at": index["synced_at"],
            "total_products": len(products),
            "pages_crawled": total_pages,
            "categories": len(categories),
            "subcategories": sum(len(c["subcategories"]) for c in categories),
            "brands": len(meta["brands"]),
            "products_with_category": sum(1 for p in products if p["categories"]),
            "errors": errors,
        }

    # ---------------------------------------------------------------- details
    def _load_details(self) -> dict:
        if self._details is None:
            self._details = {}
            if os.path.isfile(DETAILS_PATH):
                try:
                    with open(DETAILS_PATH, encoding="utf-8") as fh:
                        self._details = json.load(fh)
                except (OSError, json.JSONDecodeError):
                    self._details = {}
        return self._details

    def _save_details(self) -> None:
        tmp = DETAILS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._details, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, DETAILS_PATH)

    def product_detail(self, slug_or_url: str, refresh: bool = False) -> dict:
        """Fetch one product detail page. Slugs are case-sensitive on this site."""
        if slug_or_url.startswith("http"):
            url = slug_or_url
        else:
            url = BASE_URL + "/" + urllib.parse.quote(slug_or_url.strip("/"), safe="")
        slug = urllib.parse.unquote(urllib.parse.urlparse(url).path).strip("/")
        cache = self._load_details()
        entry = cache.get(slug)
        now = time.time()
        if entry and not refresh and now - entry.get("cached_at", 0) < DETAIL_CACHE_TTL:
            return {k: v for k, v in entry.items() if k != "cached_at"}

        # Some listing slugs are lowercase while the sitemap uses uppercase
        # (and vice versa); try a couple of harmless variants on 404 only.
        candidates = [url]
        if slug:
            for variant in (slug.lower(), slug.upper()):
                candidate = BASE_URL + "/" + urllib.parse.quote(variant, safe="")
                if candidate not in candidates:
                    candidates.append(candidate)
        html_text = ""
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                html_text = self.get(candidate)
                url = candidate
                break
            except MasuyaError as exc:
                last_error = exc
        if not html_text:
            raise last_error or MasuyaError(f"product not found: {slug}")

        detail = parse_detail(html_text, url)
        index = self.index()
        match = next((p for p in index.get("products", []) if p["url"] == url or p["slug"] == slug), None)
        if match:
            detail["brand"] = match["brand"]
            detail["categories"] = match.get("categories", [])
            detail["item_code"] = match.get("item_code") or detail["item_code"]
            if match.get("image"):
                detail["catalog_image"] = match["image"]
        detail["fetched_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        cache[slug] = {**detail, "cached_at": now}
        self._save_details()
        return detail

    # ------------------------------------------------------------------ query
    def catalog_status(self) -> dict:
        index = self.index()
        synced = index.get("synced_at")
        age_hours = None
        if synced:
            try:
                stamp = datetime.datetime.fromisoformat(synced.replace("Z", "+00:00"))
                age_hours = round((datetime.datetime.now(datetime.timezone.utc) - stamp).total_seconds() / 3600, 1)
            except ValueError:
                pass
        return {
            "supplier": "PT Masuya Graha Trikencana",
            "source": PRODUCT_URL,
            "authentication": "none - public HTML, no login, no API key, no cookies stored",
            "snapshot_synced_at": synced,
            "snapshot_age_hours": age_hours,
            "snapshot_is_partial": bool(index.get("partial")),
            "refresh_recommended": bool(index.get("partial")) or age_hours is None or age_hours > SNAPSHOT_TTL / 3600,
            "total_products": index.get("total_products", len(index.get("products", []))),
            "categories": len(index.get("categories", [])),
            "subcategories": sum(len(c.get("subcategories", [])) for c in index.get("categories", [])),
            "brands": len(index.get("brands", [])),
            "cached_product_details": len(self._load_details()),
            "data_published": ["name", "brand", "image", "item_code", "description", "packaging", "storage", "packing_size", "category"],
            "data_not_published": index.get("not_published", ["price", "stock", "ingredients", "composition"]),
            "how_to_refresh": "python3 server.py --sync  (or the masuya_refresh_catalog tool)",
        }

    def categories(self) -> dict:
        index = self.index()
        cats = index.get("categories", [])
        return {
            "total_categories": len(cats),
            "total_subcategories": sum(len(c.get("subcategories", [])) for c in cats),
            "id_format": "base64-encoded integer (MTg= -> 18); pass `value` to category/sub filters",
            "categories": cats,
        }

    def brands(self, limit: int = 200) -> dict:
        index = self.index()
        counts: dict[str, int] = {}
        for product in index.get("products", []):
            counts[product["brand"]] = counts.get(product["brand"], 0) + 1
        rows = []
        for brand in index.get("brands", []):
            rows.append({**brand, "products_in_catalog": counts.get(brand["name"], 0)})
        rows.sort(key=lambda b: (-b["products_in_catalog"], b["name"].lower()))
        return {
            "total_brands": len(rows),
            "brands_with_products": sum(1 for b in rows if b["products_in_catalog"]),
            "brands": rows[:limit],
            "note": "Brand filters accept the base64 `value`; comma-separate several values.",
        }

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 1 and t not in STOP_WORDS}

    @staticmethod
    def _quality(score: int) -> str:
        if score >= 80:
            return "strong"
        if score >= 40:
            return "possible"
        return "weak"

    def _score(self, product: dict, query: str, tokens: set[str]) -> int:
        name = product["name"].lower()
        brand = (product.get("brand") or "").lower()
        slug = product["slug"].lower()
        code = (product.get("item_code") or "").lower()
        categories = " ".join(product.get("categories", [])).lower()
        score = 0
        if query in name:
            score += 120
        if query in brand:
            score += 45
        if query and query in code.lower():
            score += 30
        for token in tokens:
            if token in name:
                score += 22
            if token in brand:
                score += 12
            if token in slug:
                score += 8
            if token in categories:
                score += 5
            if token in code:
                score += 10
        return score

    def search(self, query: str, category: str | None = None, brand: str | None = None,
               limit: int = 20, offset: int = 0) -> dict:
        index = self.index()
        query_clean = (query or "").strip().lower()
        if not query_clean:
            raise MasuyaError("query must not be empty")
        tokens = self._tokens(query_clean)
        category_clean = (category or "").strip().lower()
        brand_clean = (brand or "").strip().lower()
        matches = []
        for product in index.get("products", []):
            if category_clean and not any(category_clean in c.lower() for c in product.get("categories", [])):
                continue
            if brand_clean and brand_clean not in (product.get("brand") or "").lower():
                continue
            score = self._score(product, query_clean, tokens)
            if score > 0:
                matches.append((score, product))
        matches.sort(key=lambda pair: (-pair[0], pair[1]["name"].lower()))
        total = len(matches)
        rows = [{"score": score, "match_quality": self._quality(score), **product}
                for score, product in matches[offset: offset + limit]]
        best_score = matches[0][0] if matches else 0
        interpretation = None
        if best_score < 40:
            interpretation = (
                "No convincing name match. The hits below share only a loose token and should NOT be treated "
                "as the requested ingredient. Try masuya_live_search or a broader category query."
            )
        elif not any(query_clean in p["name"].lower() for _, p in matches[:10]):
            interpretation = "Partial/token-level matches only; verify with masuya_get_product."
        return {
            "query": query,
            "filters": {"category": category or None, "brand": brand or None},
            "total_matches": total,
            "returned": len(rows),
            "offset": offset,
            "best_score": best_score,
            "interpretation": interpretation,
            "products": rows,
            "searched_fields": ["name", "brand", "slug", "item_code", "category"],
            "catalog_is_partial": bool(index.get("partial")),
            "tip": "Use masuya_get_product for packaging/storage/description; masuya_live_search also matches product descriptions.",
        }

    def live_search(self, keyword: str, page: int = 1) -> dict:
        if not keyword or len(keyword.strip()) < 2:
            raise MasuyaError("keyword must be at least 2 characters")
        result = self.listing(page=page, keyword=keyword.strip())
        return {
            "keyword": keyword,
            "page": page,
            "total_pages": result["total_pages"],
            "has_next": result["has_next"],
            "returned": len(result["products"]),
            "products": result["products"],
            "note": "Server-side search also matches descriptions/tags, so results can be broader than name-only search.",
        }

    def list_products(self, page: int = 1, size: int = 20, category: str | None = None,
                      brand: str | None = None, sort: str = "name") -> dict:
        index = self.index()
        rows = list(index.get("products", []))
        if category:
            needle = category.lower()
            rows = [r for r in rows if any(needle in c.lower() for c in r.get("categories", []))]
        if brand:
            needle = brand.lower()
            rows = [r for r in rows if needle in (r.get("brand") or "").lower()]
        if sort == "name":
            rows.sort(key=lambda r: r["name"].lower())
        elif sort == "brand":
            rows.sort(key=lambda r: ((r.get("brand") or "").lower(), r["name"].lower()))
        elif sort == "catalog":
            rows.sort(key=lambda r: (r.get("catalog_page", 0), r["name"].lower()))
        total = len(rows)
        start = (page - 1) * size
        return {
            "page": page,
            "size": size,
            "total": total,
            "total_pages": max(1, (total + size - 1) // size),
            "filters": {"category": category, "brand": brand, "sort": sort},
            "products": rows[start: start + size],
        }

    def category_products(self, category: str, sub: str | None = None,
                          page: int = 1, size: int = 20, live: bool = False) -> dict:
        """Resolve a category name/id (optionally subcategory) to products."""
        index = self.index()
        cats = index.get("categories", [])
        node = None
        for candidate in cats:
            if category.lower() in (candidate["name"].lower(), str(candidate.get("id") or ""),
                                   candidate["value"].lower()):
                node = candidate
                break
        if node is None:
            raise MasuyaError(f"unknown category {category!r}; use masuya_list_categories")
        sub_node = None
        if sub:
            for candidate in node.get("subcategories", []):
                if sub.lower() in (candidate["name"].lower(), str(candidate.get("id") or ""),
                                   candidate["value"].lower()):
                    sub_node = candidate
                    break
            if sub_node is None:
                raise MasuyaError(f"unknown subcategory {sub!r} in {node['name']!r}")

        if sub_node or live:
            params_page = max(1, page)
            listing = self.listing(page=params_page, cat=node["value"],
                                   sub=sub_node["value"] if sub_node else None)
            return {
                "category": node["name"],
                "subcategory": sub_node["name"] if sub_node else None,
                "source": "live site filter",
                "page": params_page,
                "total_pages": listing["total_pages"],
                "returned": len(listing["products"]),
                "products": listing["products"],
            }
        return {
            "category": node["name"],
            "subcategory": None,
            "subcategories": node.get("subcategories", []),
            "source": "catalog snapshot",
            **{k: v for k, v in self.list_products(page=page, size=size, category=node["name"]).items()
               if k not in ("filters",)},
            "tip": f"Pass sub=<name> (e.g. {', '.join(s['name'] for s in node.get('subcategories', [])[:3]) or 'n/a'}) to narrow further.",
        }

    def match_names(self, names: list[str], limit_per_name: int = 3) -> dict:
        """Batch cross-reference: map your own item names to Masuya products."""
        if not isinstance(names, list) or not names:
            raise MasuyaError("names must be a non-empty list of strings")
        results = []
        for name in names[:50]:
            if not isinstance(name, str) or not name.strip():
                continue
            hit = self.search(name.strip(), limit=limit_per_name)
            best = hit["best_score"]
            results.append({
                "requested": name,
                "best_score": best,
                "confidence": self._quality(best) if best else "none",
                "likely_no_match": best < 40,
                "interpretation": hit.get("interpretation"),
                "matches": [
                    {"name": p["name"], "brand": p["brand"], "slug": p["slug"],
                     "item_code": p["item_code"], "score": p["score"],
                     "match_quality": p["match_quality"], "url": p["url"]}
                    for p in hit["products"]
                ],
            })
        return {
            "requested_names": len(names),
            "matched_with_candidates": sum(1 for r in results if r["matches"]),
            "likely_absent_from_catalog": [r["requested"] for r in results if r["likely_no_match"]],
            "results": results,
            "note": "Name matching only. `likely_no_match` means the loose hits are NOT the requested ingredient. "
                    "Confirm pack size, storage and specification with masuya_get_product.",
        }

    def compare(self, slugs: list[str]) -> dict:
        if not isinstance(slugs, list) or len(slugs) < 2:
            raise MasuyaError("provide at least 2 product slugs/URLs")
        if len(slugs) > 8:
            raise MasuyaError("compare at most 8 products at a time")
        rows, errors = [], []
        for slug in slugs:
            try:
                rows.append(self.product_detail(slug))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{slug}: {exc}")
        columns = ["name", "brand", "item_code", "packaging", "packing_size",
                   "storage", "storage_class", "categories", "description"]
        return {
            "compared": len(rows),
            "columns": columns,
            "products": [{k: row.get(k) for k in columns} for row in rows],
            "price_comparison_available": False,
            "price_note": "Masuya does not publish prices; a price comparison is impossible from public data.",
            "errors": errors,
        }

    def find_alternatives(self, target: str, limit: int = 8, same_category: bool = True,
                          same_storage: bool = True, exclude_same_brand: bool = False) -> dict:
        """Quality-preserving alternative research using public attributes."""
        index = self.index()
        products = index.get("products", [])
        exact = next((p for p in products if p["slug"].lower() == target.strip("/").lower()
                      or p["url"] == target), None)
        if exact is None:
            hits = self.search(target, limit=1)["products"]
            exact = hits[0] if hits else None
        if exact is None:
            raise MasuyaError(f"no product matches {target!r}")
        try:
            target_detail = self.product_detail(exact["slug"])
        except Exception as exc:  # noqa: BLE001
            raise MasuyaError(f"could not load target detail: {exc}") from exc

        target_tokens = self._tokens(exact["name"])
        distinctive = target_tokens - GENERIC_TOKENS
        target_categories = set(exact.get("categories", []))
        target_size = target_detail.get("pack_size_normalized") or {}
        preliminary = []
        for product in products:
            if product["slug"] == exact["slug"]:
                continue
            cats = set(product.get("categories", []))
            if same_category and target_categories and not (cats & target_categories):
                continue
            if exclude_same_brand and (product.get("brand") or "").lower() == (exact.get("brand") or "").lower():
                continue
            candidate_tokens = self._tokens(product["name"])
            overlap = len(target_tokens & candidate_tokens)
            if overlap == 0:
                continue  # category alone is not evidence of substitutability
            family_match = bool(distinctive & candidate_tokens)
            coverage = overlap / max(1, len(target_tokens))
            score = overlap * 20 + round(coverage * 60) + (15 if cats & target_categories else 0)
            reasons = [f"shares {overlap} name attribute(s) with the target"]
            if (product.get("brand") or "").lower() == (exact.get("brand") or "").lower():
                score += 6
                reasons.append("same brand")
            if product.get("item_code", "").lower() == exact.get("item_code", "").lower():
                score += 25  # same code, different slug = likely a duplicate/variant listing
                reasons.append("same item code (variant listing)")
            if distinctive and not family_match:
                score -= 30
                reasons.append("different product family - verify suitability before substituting")
            if score <= 0:
                continue
            preliminary.append((score, product, reasons))
        preliminary.sort(key=lambda pair: (-pair[0], pair[1]["name"].lower()))

        final_rows = []
        for base_score, product, reasons in preliminary[: max(limit * 2, 12)]:
            row = {
                "name": product["name"], "brand": product["brand"], "slug": product["slug"],
                "item_code": product["item_code"], "url": product["url"],
                "categories": product.get("categories", []),
                "reasons": [f"base score {base_score}"] + reasons,
            }
            score = base_score
            try:
                detail = self.product_detail(product["slug"])
                row["packaging"] = detail.get("packaging")
                row["packing_size"] = detail.get("packing_size")
                row["storage"] = detail.get("storage")
                row["storage_class"] = detail.get("storage_class")
                if detail.get("storage_class") == target_detail.get("storage_class"):
                    score += 25
                    row["reasons"].append(f"same storage class ({detail.get('storage_class')})")
                elif same_storage:
                    score -= 20
                    row["reasons"].append(f"different storage ({detail.get('storage_class')})")
                cand_size = detail.get("pack_size_normalized") or {}
                if (target_size and cand_size and
                        target_size.get("base_unit") == cand_size.get("base_unit") and
                        target_size.get("base_value")):
                    ratio = cand_size["base_value"] / target_size["base_value"]
                    if 0.8 <= ratio <= 1.25:
                        score += 15
                        row["reasons"].append("comparable pack size")
                    elif ratio > 1.25:
                        score += 5
                        row["reasons"].append("larger pack (bulk option)")
            except Exception as exc:  # noqa: BLE001
                row["detail_error"] = str(exc)
            row["score"] = score
            final_rows.append(row)
        final_rows.sort(key=lambda r: (-r["score"], r["name"].lower()))

        return {
            "target": {
                "name": exact["name"], "brand": exact["brand"], "slug": exact["slug"],
                "item_code": exact.get("item_code"), "categories": exact.get("categories", []),
                "packing_size": target_detail.get("packing_size"),
                "storage": target_detail.get("storage"),
                "storage_class": target_detail.get("storage_class"),
                "description": target_detail.get("description"),
            },
            "alternatives": final_rows[:limit],
            "candidates_considered": len(preliminary),
            "filters": {"same_category": same_category, "same_storage": same_storage,
                        "exclude_same_brand": exclude_same_brand},
            "quality_caveat": (
                "Ranked on public attributes only (category, name, storage class, pack size, brand). "
                "Masuya publishes no ingredient/composition, fat content, origin or price, so this is "
                "a shortlist for human verification - not a quality guarantee. Request specs and samples "
                "before substituting, especially for dairy vs vegetable-fat products."
            ),
        }

    def stats(self) -> dict:
        index = self.index()
        products = index.get("products", [])
        by_brand: dict[str, int] = {}
        by_category: dict[str, int] = {}
        by_storage: dict[str, int] = {}
        by_code_prefix: dict[str, int] = {}
        names: dict[str, int] = {}
        for product in products:
            by_brand[product.get("brand") or "(no brand)"] = by_brand.get(product.get("brand") or "(no brand)", 0) + 1
            for cat in product.get("categories", []):
                by_category[cat] = by_category.get(cat, 0) + 1
            names[product["name"].lower()] = names.get(product["name"].lower(), 0) + 1
            code = product.get("item_code") or ""
            prefix = re.match(r"^[A-Za-z]+", code)
            if prefix:
                by_code_prefix[prefix.group(0).upper()] = by_code_prefix.get(prefix.group(0).upper(), 0) + 1
        details = self._load_details()
        for detail in details.values():
            key = detail.get("storage_class") or "unknown"
            by_storage[key] = by_storage.get(key, 0) + 1
        duplicate_names = sorted([(n, c) for n, c in names.items() if c > 1], key=lambda x: -x[1])
        return {
            "total_products": len(products),
            "total_brands_in_use": len(by_brand),
            "top_brands": sorted(by_brand.items(), key=lambda x: -x[1])[:15],
            "products_by_category": sorted(by_category.items(), key=lambda x: -x[1]),
            "products_without_category_tag": sum(1 for p in products if not p.get("categories")),
            "duplicate_product_names": [{"name": n, "count": c} for n, c in duplicate_names[:15]],
            "item_code_prefixes": sorted(by_code_prefix.items(), key=lambda x: -x[1])[:15],
            "cached_details": len(details),
            "storage_classes_in_cached_details": by_storage,
            "note": "Storage stats only cover product detail pages fetched so far; they grow as details are requested.",
        }
