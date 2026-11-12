#!/usr/bin/env python3
"""
Masuya public catalog MCP server (stdio transport, zero dependencies).

Exposes the public https://masuya.co.id/product catalog as MCP tools so an AI
agent can research products, brands, categories, packaging and storage without
any login, API key or account.

Run:
    python3 server.py                # MCP stdio server (for an MCP client)
    python3 server.py --check        # verify public site connectivity
    python3 server.py --self-test    # local handshake + tool regression
    python3 server.py --sync         # rebuild the full catalog snapshot
    python3 server.py --show-config  # print a ready-to-paste MCP config

Implements MCP 2024-11-05 / 2025-03-26 / 2025-06-18 newline-delimited
JSON-RPC 2.0 over stdin/stdout. Tool results carry both text and typed
structuredContent. All diagnostics go to stderr; stdout carries protocol
messages only.

SAFETY: every tool is read-only. No login, no cookies, no form posts, no
inquiry submissions, no credentials and no personal data are ever used.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
import urllib.parse
from typing import Any

from masuya_client import (
    BASE_URL,
    MAP_PATH,
    PRODUCT_URL,
    MasuyaClient,
    MasuyaError,
    parse_catalog,
    parse_detail,
    parse_filters,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_NAME = "masuya-catalog"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOLS = ["2024-11-05", "2025-03-26", "2025-06-18"]
MAX_TEXT = 90000

_client: MasuyaClient | None = None


def client() -> MasuyaClient:
    global _client
    if _client is None:
        _client = MasuyaClient()
    return _client


def log(*args: Any) -> None:
    print(*args, file=sys.stderr, flush=True)


def as_text(value: Any) -> str:
    """Serialize a tool result as VALID JSON that stays under MAX_TEXT.

    Truncating the serialized string would produce broken JSON, so instead we
    retry compact separators and then trim the largest result list while
    attaching a _truncated notice telling the agent how to page through it.
    """
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(text) <= MAX_TEXT:
        return text

    def dump(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)

    compact = dump(value)
    if len(compact) <= MAX_TEXT:
        return compact

    if isinstance(value, dict):
        keys = ("products", "results", "alternatives", "brands", "categories", "matches", "urls", "data")
        key = next((k for k in keys if isinstance(value.get(k), list) and len(value[k]) > 1), None)
        if key:
            rows = value[key]

            def trimmed(n: int) -> dict:
                out = dict(value)
                out[key] = rows[:n]
                out["_truncated"] = {
                    "field": key,
                    "kept": n,
                    "total": len(rows),
                    "hint": "Result exceeded size limit; use smaller `limit`/`size` and `page`/`offset`.",
                }
                return out

            lo, hi = 0, len(rows)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if len(dump(trimmed(mid))) <= MAX_TEXT:
                    lo = mid
                else:
                    hi = mid - 1
            if lo > 0:
                return dump(trimmed(lo))

    return dump({"_truncated": True, "preview": compact[: MAX_TEXT - 300]})


# ----------------------------------------------------------------- tool defs
def _str(desc: str, **extra: Any) -> dict:
    return {"type": "string", "description": desc, **extra}


def _int(desc: str, **extra: Any) -> dict:
    return {"type": "integer", "description": desc, **extra}


def _bool(desc: str) -> dict:
    return {"type": "boolean", "description": desc}


def _arr(desc: str, items: dict, **extra: Any) -> dict:
    return {"type": "array", "description": desc, "items": items, **extra}


def _tool(name: str, desc: str, props: dict, required: list[str] | None = None) -> dict:
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return {
        "name": name,
        "description": desc,
        "inputSchema": schema,
        "outputSchema": {"type": "object", "additionalProperties": True},
    }


PAGING = {
    "page": _int("Page number, 1-based. Default 1.", minimum=1),
    "size": _int("Results per page (1-100). Default 20.", minimum=1, maximum=100),
}

TOOLS = [
    _tool(
        "masuya_catalog_status",
        "Snapshot freshness, catalog size, authentication model and exactly which fields Masuya publishes. "
        "Call this first to understand the data (there are NO prices, stock levels or login).",
        {},
    ),
    _tool(
        "masuya_list_categories",
        "List all top-level categories and their subcategories with base64 filter ids and product counts.",
        {},
    ),
    _tool(
        "masuya_list_brands",
        "List all brands with their base64 filter ids and how many catalog products each brand has.",
        {
            "limit": _int("Max brands to return (1-200). Default 100.", minimum=1, maximum=200),
            "only_with_products": _bool("Hide brands with zero products in the current snapshot. Default false."),
        },
    ),
    _tool(
        "masuya_search_products",
        "Fast local search over the full catalog snapshot by product name, brand, slug, item code or category. "
        "Results carry match_quality (strong/possible/weak) and an interpretation warning when no convincing "
        "match exists - never treat a weak hit as the requested ingredient.",
        {
            "query": _str("Search text, e.g. 'mozzarella', 'whipping cream', 'DF0027', 'Greenfield'."),
            "category": _str("Optional category name filter, e.g. 'Dairy'."),
            "brand": _str("Optional brand name filter, e.g. 'Greenfield'."),
            "limit": _int("Max results (1-100). Default 20.", minimum=1, maximum=100),
            "offset": _int("Result offset for paging. Default 0.", minimum=0),
        },
        ["query"],
    ),
    _tool(
        "masuya_live_search",
        "Query Masuya's own server-side product search (/product?keyword=...). It also matches descriptions "
        "and tags, so it can find products not obvious from the name. Returns one page of live results.",
        {
            "keyword": _str("Keyword to search on masuya.co.id (min 2 characters)."),
            "page": _int("Result page, 1-based. Default 1.", minimum=1),
        },
        ["keyword"],
    ),
    _tool(
        "masuya_list_products",
        "Browse the full catalog snapshot page by page, optionally filtered by category or brand.",
        {
            **PAGING,
            "category": _str("Optional category name, e.g. 'Dairy', 'Sauce & Seasoning'."),
            "brand": _str("Optional brand name."),
            "sort": _str("Sort order: name (default), brand, or catalog (site order).",
                         enum=["name", "brand", "catalog"]),
        },
    ),
    _tool(
        "masuya_get_product",
        "Get full public detail for one product: description, carton packaging, storage class, packing size, "
        "images, item code and brand. Accepts a slug (e.g. 'df0027') or a full masuya.co.id URL.",
        {
            "slug": _str("Product slug or full product URL, exactly as listed (slugs are case-sensitive)."),
            "refresh": _bool("Bypass the local detail cache and re-fetch the page. Default false."),
        },
        ["slug"],
    ),
    _tool(
        "masuya_compare_products",
        "Side-by-side comparison of 2-8 products on brand, item code, packaging, packing size, storage, "
        "category and description. Price comparison is impossible: Masuya publishes no prices.",
        {
            "slugs": _arr("Product slugs or URLs (2-8).", _str("slug or URL"), minItems=2, maxItems=8),
        },
        ["slugs"],
    ),
    _tool(
        "masuya_find_alternatives",
        "Quality-preserving alternative shortlist for a product. Requires shared name attributes, weights "
        "product-family tokens, identical storage class and comparable pack size; fetches detail pages for "
        "the finalists and flags different product families. Returns explicit quality caveats because "
        "ingredients/composition are not published.",
        {
            "target": _str("Product slug, URL or a name to resolve, e.g. 'mozzarella-shredded-cheese-pouch'."),
            "limit": _int("Max alternatives to return (1-20). Default 8.", minimum=1, maximum=20),
            "same_category": _bool("Only consider products in the same category. Default true."),
            "same_storage": _bool("Penalise candidates with a different storage class. Default true."),
            "exclude_same_brand": _bool("Exclude products from the same brand. Default false."),
        },
        ["target"],
    ),
    _tool(
        "masuya_category_products",
        "Products in one category (from the snapshot) or in a subcategory (live site filter). "
        "Accepts a category name, numeric id or base64 filter value.",
        {
            "category": _str("Category name/id, e.g. 'Dairy', '18' or 'MTg='."),
            "sub": _str("Optional subcategory name/id, e.g. 'Cheese', '47' or 'NDc='. Uses a live filter."),
            **PAGING,
            "live": _bool("Force a live site filter query instead of the snapshot. Default false."),
        },
        ["category"],
    ),
    _tool(
        "masuya_match_names",
        "Batch cross-reference: give a list of your own ingredient names and get the best Masuya catalog "
        "matches for each, with confidence labels and a likely_absent_from_catalog list. Useful for "
        "checking whether Masuya even stocks an ingredient before comparing suppliers.",
        {
            "names": _arr("Ingredient/product names to look up (max 50).", _str("name"), minItems=1, maxItems=50),
            "limit_per_name": _int("Matches per name (1-10). Default 3.", minimum=1, maximum=10),
        },
        ["names"],
    ),
    _tool(
        "masuya_catalog_stats",
        "Catalog shape: product counts by brand/category, duplicate product names, item-code prefixes, "
        "untagged products and how many detail pages have been cached.",
        {},
    ),
    _tool(
        "masuya_sitemap",
        "Read the public sitemap.xml and return product-page URLs (plus optional page URLs). "
        "Helpful for finding product pages that are not in the paginated grid.",
        {
            "products_only": _bool("Return only product-detail URLs, excluding category/site pages. Default true."),
            "limit": _int("Max URLs to return (1-500). Default 100.", minimum=1, maximum=500),
        },
    ),
    _tool(
        "masuya_api_map",
        "Reverse-engineered map of Masuya's public HTTP surface: endpoints, query parameters, base64 ids, "
        "HTML structures, data available and data NOT published.",
        {
            "section": _str("Optional section name. Omit to list all sections."),
        },
    ),
    _tool(
        "masuya_refresh_catalog",
        "Rebuild catalog_index.json by crawling all public /product pages (62 pages, ~130 GET requests, "
        "politely throttled). This refreshes local cache only; it never writes to masuya.co.id. "
        "Takes a few minutes - use when the snapshot is stale.",
        {
            "tag_categories": _bool("Also crawl each category to tag products and count them. Default true."),
            "max_pages": _int("Safety cap on pages per crawl (1-100). Default 100.", minimum=1, maximum=100),
        },
    ),
]


RESOURCES = [
    {"uri": "masuya://api-map", "name": "Masuya public HTTP/API map",
     "description": "Endpoints, parameters, HTML selectors, id encoding and published fields.",
     "mimeType": "application/json"},
    {"uri": "masuya://catalog", "name": "Masuya catalog snapshot status",
     "description": "Freshness, size and field coverage of the local catalog snapshot.",
     "mimeType": "application/json"},
    {"uri": "masuya://categories", "name": "Masuya categories",
     "description": "Top-level categories and subcategories with filter ids.",
     "mimeType": "application/json"},
    {"uri": "masuya://brands", "name": "Masuya brands",
     "description": "All brands with filter ids and product counts.",
     "mimeType": "application/json"},
]


# -------------------------------------------------------------- tool handlers
def _api_map(section: str | None = None) -> Any:
    if not os.path.isfile(MAP_PATH):
        raise MasuyaError("api_map.json is missing from this installation")
    with open(MAP_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    if section:
        if section not in data:
            raise MasuyaError(f"Unknown section {section!r}; available: {', '.join(sorted(data))}")
        return {"section": section, "data": data[section]}
    return {"sections": sorted(data), "map": data}


def handle_tool(name: str, arguments: dict) -> Any:
    a = arguments or {}
    c = client()

    if name == "masuya_catalog_status":
        return c.catalog_status()

    if name == "masuya_list_categories":
        return c.categories()

    if name == "masuya_list_brands":
        result = c.brands(limit=int(a.get("limit", 100)))
        if a.get("only_with_products"):
            result["brands"] = [b for b in result["brands"] if b["products_in_catalog"]]
            result["returned"] = len(result["brands"])
        return result

    if name == "masuya_search_products":
        return c.search(
            query=str(a.get("query", "")),
            category=a.get("category"),
            brand=a.get("brand"),
            limit=int(a.get("limit", 20)),
            offset=int(a.get("offset", 0)),
        )

    if name == "masuya_live_search":
        return c.live_search(str(a.get("keyword", "")), page=int(a.get("page", 1)))

    if name == "masuya_list_products":
        return c.list_products(
            page=int(a.get("page", 1)),
            size=int(a.get("size", 20)),
            category=a.get("category"),
            brand=a.get("brand"),
            sort=str(a.get("sort", "name")),
        )

    if name == "masuya_get_product":
        slug = a.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            raise MasuyaError("slug is required")
        return c.product_detail(slug.strip(), refresh=bool(a.get("refresh", False)))

    if name == "masuya_compare_products":
        slugs = a.get("slugs")
        if not isinstance(slugs, list) or not all(isinstance(s, str) for s in slugs):
            raise MasuyaError("slugs must be a list of strings")
        return c.compare(slugs)

    if name == "masuya_find_alternatives":
        target = a.get("target")
        if not isinstance(target, str) or not target.strip():
            raise MasuyaError("target is required")
        return c.find_alternatives(
            target.strip(),
            limit=int(a.get("limit", 8)),
            same_category=bool(a.get("same_category", True)),
            same_storage=bool(a.get("same_storage", True)),
            exclude_same_brand=bool(a.get("exclude_same_brand", False)),
        )

    if name == "masuya_category_products":
        category = a.get("category")
        if not isinstance(category, str) or not category.strip():
            raise MasuyaError("category is required")
        return c.category_products(
            category.strip(),
            sub=a.get("sub"),
            page=int(a.get("page", 1)),
            size=int(a.get("size", 20)),
            live=bool(a.get("live", False)),
        )

    if name == "masuya_match_names":
        names = a.get("names")
        if not isinstance(names, list):
            raise MasuyaError("names must be a list of strings")
        return c.match_names(names, limit_per_name=int(a.get("limit_per_name", 3)))

    if name == "masuya_catalog_stats":
        return c.stats()

    if name == "masuya_sitemap":
        all_urls = c.sitemap()
        urls = list(all_urls)
        if a.get("products_only", True):
            skip_paths = {
                "", "product", "produk", "about-us", "distribution", "contact-us", "catalog-en",
                "recipes", "news", "tentang-kami", "id", "en", "sitemap.xml", "robots.txt",
                "distribusi", "kontak-kami", "news-en", "lorem-ipsum", "fhi-food-hotel-indonesia",
                "news-title-western-food",
            }
            kept = []
            for url in urls:
                parsed = urllib.parse.urlparse(url)
                path = urllib.parse.unquote(parsed.path).strip("/")
                if parsed.query or path in skip_paths or path.startswith(("id/", "en/")):
                    continue
                kept.append(url)
            urls = kept
        limit = int(a.get("limit", 100))
        return {"sitemap_total_urls": len(all_urls), "total_urls": len(urls),
                "returned": min(limit, len(urls)), "urls": urls[:limit],
                "note": "Sitemap slugs can differ in case from listing slugs; masuya_get_product tries case variants. "
                        "The sitemap is less complete than the 613-product grid, which is the authoritative source."}

    if name == "masuya_api_map":
        return _api_map(a.get("section"))

    if name == "masuya_refresh_catalog":
        log("masuya_refresh_catalog: crawling public listing pages (local cache only)")
        result = c.sync(
            tag_categories=bool(a.get("tag_categories", True)),
            max_pages=int(a.get("max_pages", 100)),
        )
        return {"refreshed": True, **result}

    raise MasuyaError(f"Unknown tool: {name}")


# ------------------------------------------------------------- MCP protocol
def _ok(msg_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_message(msg: dict) -> dict | None:
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOLS else "2024-11-05"
        return _ok(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Masuya (PT Masuya Graha Trikencana) public product catalog. NO login or API key is required. "
                "Start with masuya_catalog_status, then masuya_list_categories / masuya_list_brands to learn the "
                "range, masuya_search_products or masuya_category_products to browse, masuya_get_product for "
                "packaging/storage/description, masuya_compare_products and masuya_find_alternatives for "
                "substitute research. Masuya publishes NO prices, stock, ingredients or halal data - never "
                "invent them. All tools are read-only."
            ),
        })

    if method in ("notifications/initialized", "notifications/cancelled", "notifications/progress"):
        return None
    if method == "ping":
        return _ok(msg_id, {})
    if method == "tools/list":
        return _ok(msg_id, {"tools": TOOLS})
    if method == "resources/list":
        return _ok(msg_id, {"resources": RESOURCES})
    if method == "resources/read":
        uri = params.get("uri")
        handlers = {
            "masuya://api-map": lambda: _api_map(),
            "masuya://catalog": lambda: client().catalog_status(),
            "masuya://categories": lambda: client().categories(),
            "masuya://brands": lambda: client().brands(),
        }
        if uri not in handlers:
            return _err(msg_id, -32602, f"Unknown resource: {uri}")
        text = as_text(handlers[uri]())
        return _ok(msg_id, {"contents": [{"uri": uri, "mimeType": "application/json", "text": text}]})
    if method in ("resources/templates/list", "prompts/list"):
        key = "resourceTemplates" if method.startswith("resources") else "prompts"
        return _ok(msg_id, {key: []})
    if method == "logging/setLevel":
        return _ok(msg_id, {})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            result = handle_tool(name, arguments)
            text = as_text(result)
            try:
                structured = json.loads(text)
            except json.JSONDecodeError:
                structured = {"data": text}
            if not isinstance(structured, dict):
                structured = {"data": structured}
            return _ok(msg_id, {
                "content": [{"type": "text", "text": text}],
                "structuredContent": structured,
                "isError": False,
            })
        except MasuyaError as exc:
            return _ok(msg_id, {"content": [{"type": "text", "text": f"Masuya catalog error: {exc}"}],
                                "isError": True})
        except Exception as exc:  # noqa: BLE001
            log("TOOL ERROR:", traceback.format_exc())
            return _ok(msg_id, {"content": [{"type": "text", "text": f"Tool error: {exc}"}],
                                "isError": True})

    return _err(msg_id, -32601, f"Method not found: {method}")


def _emit(payload: str) -> bool:
    try:
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()
        return True
    except (BrokenPipeError, OSError):
        log("client disconnected")
        return False


def serve_stdio() -> None:
    log(f"{SERVER_NAME} v{SERVER_VERSION} MCP server ready (stdio, no authentication)")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            log("Bad JSON:", exc)
            continue
        if isinstance(msg, list):
            responses = [r for m in msg if (r := handle_message(m)) is not None]
            if responses and not _emit(json.dumps(responses, ensure_ascii=False)):
                break
            continue
        response = handle_message(msg)
        if response is not None and not _emit(json.dumps(response, ensure_ascii=False)):
            break


# --------------------------------------------------------------------- CLI
def show_config() -> int:
    print(json.dumps({
        "mcpServers": {
            "masuya-catalog": {
                "command": "python3",
                "args": [os.path.join(HERE, "server.py")],
                "env": {},
            }
        }
    }, ensure_ascii=False, indent=2))
    return 0


def run_check() -> int:
    try:
        c = client()
        first = c.listing(page=1)
        meta = c.filters()
        detail = c.product_detail("df0027")
        log("OK: public catalog reachable, no authentication used")
        log("Listing page 1:", len(first["products"]), "products; total pages:", first["total_pages"])
        log("Categories:", len(meta["categories"]), "subcategories:",
            sum(len(x["subcategories"]) for x in meta["categories"]), "brands:", len(meta["brands"]))
        log("Detail sample:", detail["name"], "|", detail.get("packaging"), "|", detail.get("storage"))
        status = c.catalog_status()
        log("Snapshot:", status["total_products"], "products, partial =", status["snapshot_is_partial"])
        return 0
    except Exception as exc:  # noqa: BLE001
        log("FAILED:", exc)
        return 1


def run_self_test() -> int:
    failures = 0

    # Offline regressions: parsers must handle saved HTML and oversized payloads.
    try:
        sample = os.path.join(HERE, "test_fixtures", "product_page.html")
        if os.path.isfile(sample):
            with open(sample, encoding="utf-8") as fh:
                html_text = fh.read()
            parsed = parse_catalog(html_text, 1)
            assert len(parsed["products"]) == 10 and parsed["total_pages"] == 62
            meta = parse_filters(html_text)
            assert len(meta["categories"]) == 10 and len(meta["brands"]) == 119
            log("  offline parse_catalog/filters       OK (10 cards, 62 pages, 10 cats, 119 brands)")
        detail_fixture = os.path.join(HERE, "test_fixtures", "detail_page.html")
        if os.path.isfile(detail_fixture):
            with open(detail_fixture, encoding="utf-8") as fh:
                detail = parse_detail(fh.read(), "https://masuya.co.id/df0027")
            assert detail["name"] == "Whipping Cream"
            assert detail["specs"]["packaging"] == "12X1000ML"
            assert detail["storage_class"] == "chilled"
            log("  offline parse_detail fixture        OK")
        big = {"products": [{"name": "x" * 500, "slug": str(i)} for i in range(400)]}
        text = as_text(big)
        parsed = json.loads(text)
        assert len(text) <= MAX_TEXT and "_truncated" in parsed
        log(f"  as_text oversized payload          OK (kept {parsed['_truncated']['kept']} rows)")
    except Exception as exc:  # noqa: BLE001
        log("FAIL: offline regression:", exc)
        failures += 1

    checks = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "selftest", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "masuya_catalog_status", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "masuya_list_categories", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "masuya_search_products", "arguments": {"query": "cheese", "limit": 3}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "masuya_get_product", "arguments": {"slug": "df0027"}}},
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
         "params": {"name": "masuya_api_map", "arguments": {"section": "endpoints"}}},
        {"jsonrpc": "2.0", "id": 9, "method": "resources/read",
         "params": {"uri": "masuya://catalog"}},
        {"jsonrpc": "2.0", "id": 10, "method": "ping"},
    ]
    for msg in checks:
        resp = handle_message(msg)
        if msg.get("method") == "notifications/initialized":
            if resp is not None:
                log("FAIL: notification produced a response")
                failures += 1
            continue
        if resp is None or "error" in resp:
            log("FAIL:", msg.get("method"), resp)
            failures += 1
            continue
        if msg["method"] == "tools/list":
            log("tools/list ->", len(resp["result"]["tools"]), "tools")
        elif msg["method"] == "tools/call":
            body = resp["result"]
            ok = not body.get("isError") and isinstance(body.get("structuredContent"), dict)
            log(f"  {msg['params']['name']:<30} {'OK' if ok else 'ERROR'} ({len(body['content'][0]['text'])} chars)")
            if not ok:
                failures += 1
                log("   ", body["content"][0]["text"][:300])
        else:
            log(f"  {msg['method']:<30} OK")
    log("SELF-TEST:", "PASS" if failures == 0 else f"{failures} FAILURE(S)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--version" in argv:
        print(f"{SERVER_NAME} {SERVER_VERSION}")
        raise SystemExit(0)
    if "--show-config" in argv:
        raise SystemExit(show_config())
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        raise SystemExit(0)
    if "--check" in argv:
        raise SystemExit(run_check())
    if "--self-test" in argv:
        raise SystemExit(run_self_test())
    if "--sync" in argv:
        log("Refreshing Masuya catalog snapshot (public pages only, no authentication)...")
        result = client().sync(tag_categories=True, progress=True)
        log("SYNC DONE:", json.dumps(result, ensure_ascii=False))
        raise SystemExit(0 if not result.get("errors") else 1)
    serve_stdio()
