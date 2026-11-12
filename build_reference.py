#!/usr/bin/env python3
"""Generate a standalone, self-contained HTML reference from api_map.json.

Run: python3 build_reference.py
Output: api_reference.html (no external CSS/JS/images, safe to publish).
"""

from __future__ import annotations

import html
import json
import os
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
MAP_PATH = os.path.join(HERE, "api_map.json")
INDEX_PATH = os.path.join(HERE, "catalog_index.json")
OUT_PATH = os.path.join(HERE, "api_reference.html")


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def table(headers: list[str], rows: list[list[object]]) -> str:
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def main() -> int:
    with open(MAP_PATH, encoding="utf-8") as fh:
        api = json.load(fh)
    with open(INDEX_PATH, encoding="utf-8") as fh:
        index = json.load(fh)

    products = index.get("products", [])
    brand_counts = Counter(p.get("brand") or "(none)" for p in products)
    cat_counts = {c["name"]: c.get("product_count", 0) for c in api.get("categories", [])}
    stats = [
        ("Products", len(products)),
        ("Listing pages", index.get("pages_crawled", "?")),
        ("Categories", len(api.get("categories", []))),
        ("Subcategories", sum(len(c.get("subcategories", [])) for c in api.get("categories", []))),
        ("Brands", len(index.get("brands", []))),
        ("Authentication", "None"),
    ]
    stat_html = "".join(
        f'<div class="stat"><span class="stat-n">{esc(v)}</span><span class="stat-l">{esc(k)}</span></div>'
        for k, v in stats
    )

    endpoint_rows = []
    for path, info in api["endpoints"].items():
        if isinstance(info, dict):
            endpoint_rows.append([path, info.get("description", ""), info.get("example", info.get("response", ""))])
        elif isinstance(info, list):
            endpoint_rows.append([path, "Intentionally NOT used: " + "; ".join(info), "write / personal-data routes"])
    endpoints_html = table(["Endpoint", "Description", "Example / response"], endpoint_rows)

    param_rows = [[k, v] for k, v in api["query_parameters"].items()]
    params_html = table(["Parameter", "Meaning"], param_rows)

    cat_rows = []
    for cat in api["categories"]:
        subs = ", ".join(cat.get("subcategories", [])) or "—"
        cat_rows.append([cat["name"], cat["value"], cat_counts.get(cat["name"], "—"), subs])
    cat_html = table(["Category", "Filter id", "Products", "Subcategories"], cat_rows)

    published = "".join(f"<li>{esc(x)}</li>" for x in api["published_product_fields"])
    missing = "".join(
        f"<li><b>{esc(k)}</b> — {esc(v)}</li>" for k, v in api["data_not_published"].items()
        if isinstance(v, str)
    )
    tools = "".join(f"<li><code>{esc(t)}</code></li>" for t in api["mcp_tools"])
    resources = "".join(f"<li><code>{esc(r)}</code></li>" for r in api["mcp_resources"])
    brands_html = table(
        ["Brand", "Products"],
        [[name, count] for name, count in brand_counts.most_common(15)],
    )
    verification = api.get("verification", {})
    verify_rows = [[k, json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v]
                   for k, v in verification.items()]
    verify_html = table(["Check", "Observed"], verify_rows)
    ethics = api["rate_limiting_and_ethics"]
    comparison = api["comparison_with_sukanda_mcp"]

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Masuya Catalog — Public API &amp; MCP Reference</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:0 20px 80px; background:#f6f7f9; color:#1f2733;
         font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }}
  main {{ max-width:1080px; margin:0 auto; }}
  header {{ background:linear-gradient(135deg,#0f5c4a,#1b8a6b); color:#fff; padding:38px 34px;
            border-radius:0 0 18px 18px; margin-bottom:26px; }}
  header h1 {{ margin:0 0 6px; font-size:30px; letter-spacing:-.4px; }}
  header p {{ margin:0; opacity:.92; }}
  .badge {{ display:inline-block; background:rgba(255,255,255,.18); border:1px solid rgba(255,255,255,.4);
            padding:3px 10px; border-radius:20px; font-size:12px; margin:12px 8px 0 0; }}
  .stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin:22px 0 30px; }}
  .stat {{ background:#fff; border:1px solid #e2e7ec; border-radius:12px; padding:16px; text-align:center; }}
  .stat-n {{ display:block; font-size:24px; font-weight:700; color:#0f5c4a; }}
  .stat-l {{ display:block; font-size:12px; color:#647082; text-transform:uppercase; letter-spacing:.6px; }}
  h2 {{ margin:34px 0 12px; font-size:20px; border-bottom:2px solid #d7e3dd; padding-bottom:8px; color:#0f5c4a; }}
  h3 {{ font-size:16px; margin:22px 0 8px; }}
  section {{ background:#fff; border:1px solid #e2e7ec; border-radius:12px; padding:8px 22px 22px; margin-bottom:20px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13.5px; }}
  th {{ background:#eef4f1; text-align:left; padding:9px 10px; border-bottom:2px solid #cde0d7; color:#0f5c4a; }}
  td {{ padding:8px 10px; border-bottom:1px solid #edf0f2; vertical-align:top; }}
  tr:last-child td {{ border-bottom:none; }}
  td:first-child {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; white-space:nowrap; }}
  code {{ background:#eef2f4; padding:1px 5px; border-radius:4px; font-size:13px; }}
  ul {{ columns:2; column-gap:36px; padding-left:20px; }}
  li {{ break-inside:avoid; margin-bottom:4px; }}
  .note {{ background:#fff8e6; border-left:4px solid #e0a52b; padding:12px 16px; border-radius:6px; }}
  .good {{ background:#e9f6ef; border-left:4px solid #1b8a6b; padding:12px 16px; border-radius:6px; }}
  footer {{ color:#647082; font-size:12px; text-align:center; margin-top:30px; }}
  a {{ color:#0f5c4a; }}
  @media (max-width:700px) {{ ul {{ columns:1; }} td:first-child {{ white-space:normal; }} }}
</style>
</head>
<body>
<header><main>
  <h1>Masuya Catalog — Public API &amp; MCP Reference</h1>
  <p>PT Masuya Graha Trikencana · <a href="https://masuya.co.id/product" style="color:#fff">masuya.co.id/product</a></p>
  <span class="badge">No login required</span>
  <span class="badge">No API key</span>
  <span class="badge">Read-only GET</span>
  <span class="badge">No prices published</span>
  <span class="badge">Recon: {esc(api['overview']['recon_date'])}</span>
</main></header>
<main>
<div class="stats">{stat_html}</div>

<section>
  <h2>1. What this is</h2>
  <p>{esc(api['overview']['purpose'])} {esc(api['overview']['authentication'])}</p>
  <p class="good"><b>Safety:</b> {esc(api['overview']['safety'])}</p>
  <p>Inferred stack: {esc(api['overview']['stack_inferred'])}. There is <b>no public JSON product API</b>;
     data is extracted from server-rendered HTML.</p>
</section>

<section>
  <h2>2. HTTP surface</h2>
  {endpoints_html}
  <h3>Query parameters</h3>
  {params_html}
  <h3>ID encoding</h3>
  <p>{esc(api['id_encoding']['format'])} {esc(api['id_encoding']['handling'])}</p>
  <p><code>{esc('; '.join(f'{k} -> {v}' for k, v in api['id_encoding']['examples'].items()))}</code></p>
</section>

<section>
  <h2>3. Catalog shape</h2>
  {cat_html}
  <h3>Top brands in the snapshot</h3>
  {brands_html}
  <p class="note">Some catalog products share identical names (variants under different slugs), and 25 products
     carry the literal brand label <code>No Brand</code>. Always key on <code>slug</code>, not on name.</p>
</section>

<section>
  <h2>4. Published vs missing data</h2>
  <h3>Available per product</h3>
  <ul>{published}</ul>
  <h3>Not published — do not invent</h3>
  <ul>{missing}</ul>
  <p class="note"><b>{esc(api['data_not_published']['consequence'])}</b></p>
</section>

<section>
  <h2>5. MCP tools &amp; resources</h2>
  <ul>{tools}</ul>
  <h3>Resources</h3>
  <ul>{resources}</ul>
  <p>All tools are read-only. Results include typed <code>structuredContent</code>; oversized results are truncated
     as valid JSON with a <code>_truncated</code> notice. Search hits carry <code>match_quality</code>
     (strong / possible / weak) and <code>masuya_match_names</code> reports
     <code>likely_absent_from_catalog</code>, so a loose token hit is never mistaken for a real product.</p>
  <h3>Compared with the Sukanda MCP</h3>
  <p><b>Sukanda:</b> {esc(comparison['sukanda'])}<br>
     <b>Masuya:</b> {esc(comparison['masuya'])}<br>
     <b>Usage:</b> {esc(comparison['shared_use_case'])}</p>
</section>

<section>
  <h2>6. Politeness &amp; verification</h2>
  <p><b>Observed:</b> {esc(ethics['observed'])}</p>
  <p><b>Client policy:</b> {esc(ethics['client_policy'])}</p>
  <p><b>Scope:</b> {esc(ethics['scope'])}</p>
  {verify_html}
</section>

<footer>Generated by build_reference.py from api_map.json and catalog_index.json · No external assets · No credentials</footer>
</main>
</body>
</html>
"""
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(doc)
    print(f"Wrote {OUT_PATH} ({len(doc)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
