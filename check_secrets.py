#!/usr/bin/env python3
"""Fail CI/local checks if credentials or personal data would be published.

This MCP needs NO login at all. The check therefore verifies both that no
secret-like strings appear in publishable files and that no credential file
(.env, token file, local config with credentials) has been added.

Usage: python3 check_secrets.py     (exit 1 means DO NOT PUSH)
Only the Python standard library is used.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

PATTERNS: list[tuple[str, str]] = [
    ("real email address", r"\b[A-Za-z0-9._%+-]+@(?!example\.)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ("credential assignment", r"(?:PASSWORD|PASSWD|API_KEY|APIKEY|SECRET|TOKEN)\s*[:=]\s*[\"']?(?!<|$|your-|#|<)[A-Za-z0-9_\-]{8,}"),
    ("JWT", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\b"),
    ("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    ("private key", r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ("bearer token", r"Bearer\s+[A-Za-z0-9._-]{24,}"),
    # Saved HTML fixtures must never carry a live Laravel session/CSRF token.
    ("CSRF / session token", r'name="_token"\s+value="[A-Za-z0-9]{20,}"'),
    ("session cookie value", r"(?:XSRF-TOKEN|laravel_session|masuya_session)=[A-Za-z0-9%]{30,}"),
    ("absolute home path", r"/(?:Users|home)/[A-Za-z0-9._-]+/"),
]

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules"}
# The scanner also checks itself; its own regex strings and the sample image filename
# in a comment are covered by ALLOW_LINES_WITH / ASSET_MARKERS below.
SKIP_FILES = {"details_cache.json", "mcp-config.local.json"}
FORBIDDEN_FILES = {".env", ".env.local", "credentials.json", "token.json", "secrets.json"}

# Documentation legitimately shows placeholder passwords and config paths.
ALLOW_LINES_WITH = ("<", "example", "placeholder", "your-", "no password", "no login",
                    "PASSWORD>", "TOKEN>", "SECRET>", "API_KEY>", "not required",
                    "never", "no credential")

# Public Masuya image filenames such as "GF UHT FULL CREAM 200ML@24.png.webp"
# look like an email to a naive regex. They are catalogue asset URLs, not PII.
ASSET_MARKERS = (".webp", ".png", ".jpg", ".jpeg", ".gif", "/cfind/", "images/imports/products")


def candidate_files() -> list[str]:
    try:
        out = subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=HERE, stderr=subprocess.DEVNULL, text=True,
        )
        files = [f for f in out.splitlines() if f]
    except (subprocess.CalledProcessError, FileNotFoundError):
        files = []
        for root, dirs, names in os.walk(HERE):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in names:
                files.append(os.path.relpath(os.path.join(root, name), HERE))
    return sorted({rel for rel in files
                   if rel not in SKIP_FILES and not rel.startswith(".git/")
                   and os.path.isfile(os.path.join(HERE, rel))})


def main() -> int:
    files = candidate_files()
    problems: list[str] = []
    for rel in files:
        if os.path.basename(rel) in FORBIDDEN_FILES:
            problems.append(f"{rel}: credential file must not be published (this MCP needs no login)")
        try:
            text = open(os.path.join(HERE, rel), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for label, pattern in PATTERNS:
            for match in re.finditer(pattern, text):
                line_no = text.count("\n", 0, match.start()) + 1
                lines = text.splitlines()
                line = lines[line_no - 1].strip().lower() if line_no <= len(lines) else ""
                if any(marker in line for marker in ALLOW_LINES_WITH):
                    continue
                if label == "real email address" and any(marker in line for marker in ASSET_MARKERS):
                    continue
                shown = "<redacted>" if label == "real email address" else match.group(0)[:80]
                problems.append(f"{rel}:{line_no} {label}: {shown}")
    if problems:
        print("SECRET CHECK FAILED - do not push:")
        for problem in problems:
            print("  -", problem)
        return 1
    print(f"SECRET CHECK PASS - scanned {len(files)} publishable files; no credentials, tokens or PII found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
