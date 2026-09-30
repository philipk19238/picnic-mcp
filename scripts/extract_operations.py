"""Extract GraphQL operations from the Picnic web app's JS bundles.

The order app (order.trypicnic.com) is an Otter/CloudKitchens white-label
storefront. Its Apollo documents are compiled into the bundles as JS object
literals (`{kind:"Document",definitions:[...]}`). This script downloads the
entry bundle plus every lazy chunk, converts each document back into GraphQL
source and writes one `.graphql` file per operation.

Usage:
    uv run python scripts/extract_operations.py [--out src/picnic_mcp/operations]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from graphql import print_ast
from graphql.language import ast as gql_ast

ORIGIN = "https://order.trypicnic.com"
USER_AGENT = "Mozilla/5.0"

# Operations that the web app routes to `${origin}/api/picnic/graphql`
# instead of `api.cloudkitchens.com/graphql` are listed in the bundle as a
# `new Set([...])` right after the `GuestEaterOrder` set.
GATEWAY_SET_RE = re.compile(r'new Set\(\["GuestEaterOrder"\]\),\w+=new Set\(\[(.*?)\]\)')


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def download_bundles() -> str:
    html = fetch(f"{ORIGIN}/")
    entry = [s for s in re.findall(r'src="([^"]+\.js)"', html) if s.startswith("/")]
    entry += re.findall(r'href="(/assets/[^"]+\.js)"', html)
    source = "".join(fetch(ORIGIN + path) for path in sorted(set(entry)))

    chunks = sorted(set(re.findall(r'(assets/[A-Za-z0-9_.-]+-[A-Za-z0-9_-]{8}\.js)', source)))
    with ThreadPoolExecutor(16) as pool:
        source += "".join(pool.map(lambda c: fetch(f"{ORIGIN}/{c}"), chunks))
    return source


def iter_documents(source: str):
    """Yield the raw JS text of every `{kind:"Document",...}` literal."""
    for match in re.finditer(r'\{kind:"Document",definitions:', source):
        depth = 0
        for i in range(match.start(), len(source)):
            ch = source[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield source[match.start() : i + 1]
                    break


def js_literal_to_python(text: str):
    """Convert a minified JS AST literal into Python dicts/lists."""
    text = text.replace("!0", "true").replace("!1", "false").replace("void 0", "null")
    text = re.sub(r"([{,])([A-Za-z_$][\w$]*):", r'\1"\2":', text)
    return json.loads(text)


def to_ast(node):
    """Rebuild graphql-core AST nodes from plain dicts."""
    if isinstance(node, list):
        return tuple(to_ast(n) for n in node)
    if not isinstance(node, dict):
        return node
    cls = getattr(gql_ast, node["kind"] + "Node")
    kwargs = {
        re.sub(r"(?<!^)(?=[A-Z])", "_", k).lower(): to_ast(v)
        for k, v in node.items()
        if k not in ("kind", "loc")
    }
    if cls is gql_ast.OperationDefinitionNode:
        kwargs["operation"] = gql_ast.OperationType(kwargs["operation"])
    return cls(**kwargs)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="src/picnic_mcp/operations")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    source = download_bundles()
    gateway_match = GATEWAY_SET_RE.search(source)
    gateway_ops = set(re.findall(r'"(\w+)"', gateway_match.group(1))) if gateway_match else set()

    written: dict[str, str] = {}
    for raw in iter_documents(source):
        try:
            doc = to_ast(js_literal_to_python(raw))
        except Exception as exc:  # noqa: BLE001 - best-effort scraping
            print(f"skip: could not parse document ({exc})", file=sys.stderr)
            continue
        op = next(
            (d for d in doc.definitions if isinstance(d, gql_ast.OperationDefinitionNode)),
            None,
        )
        if op is None or op.name is None:
            continue
        name = op.name.value
        route = "picnic-gateway" if name in gateway_ops else "cloudkitchens"
        body = f"# route: {route}\n{print_ast(doc)}\n"
        (out / f"{name}.graphql").write_text(body)
        written[name] = op.operation.value

    for kind in ("query", "mutation", "subscription"):
        names = sorted(n for n, k in written.items() if k == kind)
        if names:
            print(f"{kind} ({len(names)}): {', '.join(names)}")
    print(f"wrote {len(written)} operations to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
