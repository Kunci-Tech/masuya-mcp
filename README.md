# Masuya Catalog MCP

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server that lets an AI assistant research
**PT Masuya Graha Trikencana's public product catalog** at <https://masuya.co.id/product>.

**No login. No API key. No credentials. No `.env`.** Everything this server reads is public HTML that any browser
can see. It never posts forms, never submits inquiries, never touches an account or cart, and never stores cookies
or personal data.

**Public repository:** <https://github.com/Kunci-Tech/masuya-mcp>

> **Unofficial project.** This is an independent, read-only integration built from Masuya's public catalog pages,
> not an official Masuya product. The site's HTML can change without notice. Use it politely and respect the
> site's terms of service.

> This is the companion to [`sukanda-onelink-mcp`](https://github.com/Kunci-Tech/sukanda-onelink-mcp). Sukanda is
> an authenticated buyer portal with prices, stock and order history. Masuya is a public marketing catalog with
> **no prices and no stock**. Use both: Sukanda for buying decisions, Masuya for range discovery and alternative
> shortlists.

---

## 1. What you can learn about Masuya products

Verified on 2026-11-12 from the live site and bundled as a snapshot:

| | |
|---|---|
| Products | **613** across **62** listing pages |
| Top-level categories | **10** |
| Subcategories | **39** |
| Filterable brands | **119** (120 brand labels appear on cards; 25 products are literally labelled `No Brand`) |
| Biggest categories | Sauce & Seasoning 143, Groceries 136, Dairy 126, Beverages 107, Fruit Based Product 40 |
| Biggest brands | Greenfield 54, Heavenly Blush 38, Kewpie 26, Nissin 22, Beerenberg 20, Andros 20 |
| Per product | name, brand, image, item code, description, carton **packaging** (e.g. `12X1000ML`), **storage** (`CHILLER` / `FROZEN` / `DRY`), **packing size** (e.g. `200 gr`), category |

**Masuya does NOT publish:** prices, stock/availability, ingredients or composition, halal certificates, nutrition,
country of origin, MOQ. Every product ends with a `MAKE A INQUIRY` button. The server therefore **never invents
prices** and explicitly labels alternative research as a shortlist requiring human verification.

---

## 2. Guided setup (3 steps, ~1 minute)

### Step 1 — Get the code and check Python

```bash
git clone https://github.com/Kunci-Tech/masuya-mcp.git
cd masuya-mcp
python3 --version
```

Python 3.9+ is enough. **There are no pip dependencies** — the client, server and tests use only the standard
library. No virtualenv, no `npm install`, no API keys, no password prompt. If you already have a local copy,
just `cd` into it and skip the clone.

### Step 2 — Verify the public site and the bundled snapshot

```bash
cd <path-to>/masuya-mcp
python3 server.py --check
```

Expected output (on stderr):

```
OK: public catalog reachable, no authentication used
Listing page 1: 10 products; total pages: 62
Categories: 10 subcategories: 39 brands: 119
Detail sample: Whipping Cream | 12X1000ML | CHILLER
Snapshot: 613 products, partial = False
```

`catalog_index.json` is a bundled snapshot of all 613 products, so most questions need **zero network calls**.
If you ever delete it, the server builds a 1-page fallback and tells you to run `--sync`.

### Step 3 — Register the MCP server

Print a config with your real absolute path already filled in:

```bash
python3 server.py --show-config
```

Copy the `mcpServers.masuya-catalog` block into your MCP client's config file (the same place where
`sukanda-onelink` is registered). It looks like this:

```json
{
  "mcpServers": {
    "masuya-catalog": {
      "command": "python3",
      "args": ["/absolute/path/to/masuya-mcp/server.py"]
    }
  }
}
```

`mcp-config.example.json` also shows how to enable **both** suppliers at once. Restart/reload the MCP client,
then ask your assistant a question from section 4.

> Nothing is stored in `env`, because there is nothing to store. `--show-config` returns `"env": {}` on purpose.

### Optional — run the regression suite

```bash
python3 test_client.py        # real stdio MCP handshake + all 15 tools + 4 resources
python3 server.py --self-test # offline parser regressions + handshake
python3 check_secrets.py      # proves no credentials/PII in publishable files
```

---

## 3. Tools (15, all read-only)

| Tool | What it answers |
|---|---|
| `masuya_catalog_status` | Snapshot freshness, size, what data exists and what does not. **Start here.** |
| `masuya_list_categories` | 10 categories + 39 subcategories with filter ids and counts. |
| `masuya_list_brands` | 119 brands, filter ids and product counts. |
| `masuya_search_products` | Fast local search across all 613 products by name / brand / slug / item code / category. |
| `masuya_live_search` | Masuya's own `?keyword=` search — also matches descriptions and tags. |
| `masuya_list_products` | Page through the catalog with category/brand filters. |
| `masuya_get_product` | Full detail: description, packaging, storage, packing size, images, item code, brand. |
| `masuya_compare_products` | 2–8 products side by side (brand, pack, storage, description). |
| `masuya_find_alternatives` | Quality-preserving substitute shortlist with explicit caveats. |
| `masuya_category_products` | All products in a category, or a live subcategory filter. |
| `masuya_match_names` | Batch-match your own ingredient list against Masuya's range. |
| `masuya_catalog_stats` | Brand/category counts, duplicate names, item-code prefixes. |
| `masuya_sitemap` | Product URLs from the public `sitemap.xml`. |
| `masuya_api_map` | The reverse-engineered HTTP/HTML map (`api_map.json`) by section. |
| `masuya_refresh_catalog` | Rebuild `catalog_index.json` from public pages (local cache only). |

Resources: `masuya://api-map`, `masuya://catalog`, `masuya://categories`, `masuya://brands`.

Protocol: MCP stdio, newline-delimited JSON-RPC 2.0, protocol versions `2024-11-05`, `2025-03-26`,
`2025-06-18`. Tool results include both a text block and typed `structuredContent`. Large results are truncated
as **valid JSON** with a `_truncated` notice instead of being cut mid-string.

---

## 4. Questions to try

- *"What does Masuya sell? Give me the category map and the biggest brands."*
- *"List all their cheeses and dairy creams with pack sizes and storage."*
- *"Find every Greenfield product in the Dairy category."*
- *"Show me the full spec for `df0027` and compare it with the shredded mozzarella."*
- *"Which Masuya products could replace parmesan / mozzarella without lowering quality?"*
- *"Cross-reference my supplier list — parmesan grated, whipping cream, unsalted butter — against Masuya."*
- *"Compare Masuya's range with Sukanda's for chilled dairy."* (enable both MCP servers)

For the cheese question specifically, `masuya_find_alternatives` requires shared name attributes, weights
product-family tokens, **identical storage class** and **comparable pack size**, then fetches the finalists'
detail pages. It returns reasons plus a `quality_caveat` reminding you that Masuya publishes no
ingredient/composition data — critical when distinguishing real dairy cream from blended vegetable-fat cream.

Searches are deliberately honest: `match_quality` labels every hit `strong` / `possible` / `weak`, and
`masuya_match_names` returns a `likely_absent_from_catalog` list. For example, asking this snapshot for
"parmesan grated" returns only a weak `Grated Onion Dressing` hit — **Masuya's published catalog contains no
parmesan at all**, so the tool says so instead of inventing a substitute.

---

## 5. How it works (reverse-engineered surface)

```
GET /product                          product grid, 10 cards/page, 62 pages
GET /product?keyword=cheese           server-side search
GET /product?cat=MTg%3D               category filter (Dairy)
GET /product?cat=MTg%3D&sub=NDc%3D    subcategory filter (Dairy > Cheese)
GET /product?brand=MTMz,MTM0          brand filter (comma separated)
GET /product?sort=alphabet-asc        asc | desc | alphabet-asc | alphabet-desc
GET /product?page=N                   pagination, filters preserved
GET /<slug>                           product detail (SLUG IS CASE-SENSITIVE)
GET /sitemap.xml                      422 URLs
GET /listProductCategory?page=N       AJAX "load more" — session-dependent, not used
```

Category/brand/subcategory ids are **base64-encoded integers**: `MTg=` → 18 (Dairy), `NDc=` → 47 (Cheese),
`MTMz` → 133 (Yamamotoyama). The client passes them through unchanged and exposes the decoded number for
readability. Full details, CSS selectors and edge cases are in `api_map.json`, `api_reference.html` and the
`masuya://api-map` resource.

Parsing notes that matter:

- Product cards are `a.product-list__item` with `h4` = name, `h5` = brand, and an image whose filename
  usually starts with the item code (`DF0027   GF WHIPPING CREAM 1LT.png.webp` → `DF0027`).
- Detail pages repeat spec rows on some products; the parser deduplicates by label.
- The detail page does **not** repeat the brand, so the client joins it from the listing snapshot.
- `/q083m` works while `/Q083M` returns 404, but `/C559M` works — so `masuya_get_product` uses the exact
  listing URL and only tries case variants after a 404.
- Some products share a name (e.g. four `Pocari Sweat Pet` variants, three `Apricot Jam` entries). They are
  distinct slugs/variants; `masuya_catalog_stats` reports duplicates.
- `sitemap.xml` has 422 entries but is noisier and less complete than the 613-product grid: about 177 of them
  are category/filter URLs and some are news/demo pages. The grid is the authoritative source.

---

## 6. Refreshing the catalog

```bash
python3 server.py --sync
```

Crawls all 62 listing pages plus each of the 10 category filters (~130 GET requests, ~2 minutes), rewrites
`catalog_index.json` atomically, and tags every product with its category. It is polite by design: a 0.12 s
minimum interval between requests, 2 retries, a 25 s timeout, an in-memory 10-minute HTTP cache and a 7-day
detail cache. The same operation is available as the `masuya_refresh_catalog` tool.

Recommendation: refresh at most daily. `--sync` and `masuya_refresh_catalog` **write only local cache files**;
they never write to masuya.co.id.

`details_cache.json` grows lazily as `masuya_get_product` / `masuya_compare_products` /
`masuya_find_alternatives` are used. It is gitignored.

---

## 7. Safety, privacy and limits

- **Read-only.** Only HTTP GET. No login, no session, no cookies, no CSRF tokens, no reCAPTCHA, no inquiry POST.
- **No secrets.** There is no `.env`, no password field and no credential store. `check_secrets.py` scans every
  publishable file for emails, tokens, JWTs, CSRF/session values, private keys and home-directory paths before
  any push. The saved HTML fixtures have the live Laravel `_token` replaced with `REDACTED_CSRF_TOKEN`, and
  `mcp-config.local.json` (which contains an absolute local path) is gitignored.
- **No invented data.** `price` is always `null`, `inquiry_only` is always `true`, and every result carries a
  note listing the fields Masuya does not publish.
- **HTML scraping is fragile.** If Masuya redesigns the site, re-run `--sync` and check `api_map.json`
  selectors. Parser fixtures in `test_fixtures/` let you verify changes offline.
- **Not a purchasing system.** No cart, no ordering, no availability. Contact a Masuya sales representative for
  pricing, MOQ, specifications and samples.

---

## 8. Files

| File | Purpose |
|---|---|
| `server.py` | MCP stdio server, 15 tools, 4 resources, CLI (`--check`, `--self-test`, `--sync`, `--show-config`) |
| `masuya_client.py` | Zero-dependency HTTP client, HTML parsers, catalog index and search/alternatives logic |
| `catalog_index.json` | Bundled snapshot of all 613 products + category tree + brand list |
| `api_map.json` | Reverse-engineered endpoint/selector/data map |
| `api_reference.html` | Human-readable standalone reference generated from the map |
| `build_reference.py` | Regenerates `api_reference.html` |
| `test_client.py` | End-to-end MCP stdio regression suite |
| `test_fixtures/` | Saved public HTML for offline parser tests |
| `check_secrets.py` | Pre-publish credential/PII scan |
| `mcp-config.example.json` | Example MCP client config (placeholder paths only) |
| `.gitignore` | Excludes local detail cache and any credential files |

---

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `Snapshot: 10 products, partial = True` | Run `python3 server.py --sync`. |
| `HTTP 404` on a product | Slugs are case-sensitive; use the exact `slug` from `masuya_search_products`. The client tries lower/upper variants automatically. |
| `network error ... timed out` | Check your internet connection; the client retries twice and caches successful pages for 10 minutes. |
| Tool result ends with `_truncated` | Lower `limit`/`size` and page with `offset`/`page`. |
| MCP client shows nothing | Ensure `args` contains the **absolute** path to `server.py`, and that nothing else writes to stdout. Diagnostics go to stderr only. |
| Category counts look stale | Run `python3 server.py --sync`. |
