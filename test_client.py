#!/usr/bin/env python3
"""
End-to-end MCP stdio test client (no dependencies).

Spawns server.py as a subprocess and drives a real MCP conversation over
newline-delimited JSON. Run: python3 test_client.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")


def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, SERVER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    def send(obj: dict) -> dict | None:
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        if "id" not in obj:  # notification -> no response
            return None
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("server closed stdout; stderr:\n" + proc.stderr.read())
        return json.loads(line)

    failures = 0
    try:
        init = send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test-client", "version": "1.0"}},
        })
        assert init["result"]["protocolVersion"] == "2025-06-18", init
        print("initialize      ->", init["result"]["serverInfo"], "protocol", init["result"]["protocolVersion"])
        assert "NO login" in init["result"]["instructions"]
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        print("initialized     -> notification sent")

        tools = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        print("tools/list      ->", len(tools["result"]["tools"]), "tools")
        for tool in tools["result"]["tools"]:
            assert tool["name"].startswith("masuya_"), tool["name"]
            assert tool["inputSchema"]["type"] == "object", tool["name"]
            assert tool["outputSchema"]["type"] == "object", tool["name"]

        res = send({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
        print("resources/list  ->", [r["uri"] for r in res["result"]["resources"]])

        calls = [
            ("masuya_catalog_status", {}),
            ("masuya_list_categories", {}),
            ("masuya_list_brands", {"limit": 5}),
            ("masuya_search_products", {"query": "mozzarella", "limit": 3}),
            ("masuya_live_search", {"keyword": "cheese", "page": 1}),
            ("masuya_list_products", {"category": "Dairy", "size": 2}),
            ("masuya_get_product", {"slug": "df0027"}),
            ("masuya_compare_products", {"slugs": ["df0027", "mozzarella-shredded-cheese-pouch"]}),
            ("masuya_find_alternatives", {"target": "mozzarella-shredded-cheese-pouch", "limit": 3}),
            ("masuya_category_products", {"category": "Dairy", "size": 2}),
            ("masuya_match_names", {"names": ["parmesan grated", "whipping cream"], "limit_per_name": 2}),
            ("masuya_catalog_stats", {}),
            ("masuya_sitemap", {"limit": 5}),
            ("masuya_api_map", {"section": "endpoints"}),
        ]
        for idx, (name, args) in enumerate(calls, start=4):
            response = send({"jsonrpc": "2.0", "id": idx, "method": "tools/call",
                             "params": {"name": name, "arguments": args}})
            body = response["result"]
            text = body["content"][0]["text"]
            ok = not body.get("isError") and isinstance(body.get("structuredContent"), dict)
            try:
                json.loads(text) if not ok else None
            except json.JSONDecodeError:
                ok = False
            print(f"call {name:<32} -> {'OK' if ok else 'ERROR'} ({len(text)} chars)")
            if not ok:
                failures += 1
                print("   ", text[:400])
            if name == "masuya_api_map":
                assert "/product" in text, "missing endpoint list"
            if name == "masuya_sitemap":
                assert json.loads(text)["total_urls"] > 100, "sitemap filter returned too few product URLs"
            if name == "masuya_get_product":
                parsed = json.loads(text)
                assert parsed["specs"]["packaging"] == "12X1000ML", parsed.get("specs")
                assert parsed["price"] is None, "price must never be invented"

        for idx, uri in enumerate(
            ["masuya://api-map", "masuya://catalog", "masuya://categories", "masuya://brands"], start=30
        ):
            response = send({"jsonrpc": "2.0", "id": idx, "method": "resources/read",
                             "params": {"uri": uri}})
            text = response["result"]["contents"][0]["text"]
            json.loads(text)
            print(f"read {uri:<30} -> OK ({len(text)} chars)")

        response = send({"jsonrpc": "2.0", "id": 40, "method": "ping"})
        assert response["result"] == {}, response
        print("ping            -> OK")

        response = send({"jsonrpc": "2.0", "id": 41, "method": "no/such/method"})
        assert "error" in response and response["error"]["code"] == -32601
        print("unknown method  -> -32601 as expected")

        response = send({"jsonrpc": "2.0", "id": 42, "method": "tools/call",
                         "params": {"name": "masuya_get_product", "arguments": {"slug": ""}}})
        assert response["result"]["isError"] is True
        print("invalid argument -> isError as expected")
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        leftover = proc.stderr.read()
        if leftover.strip():
            print("\n[server stderr]\n" + leftover.strip()[:1500])

    print("\nRESULT:", "PASS" if failures == 0 else f"{failures} FAILURE(S)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
