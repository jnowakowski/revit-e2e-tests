"""
revit-remote client -- talk to the revit-remote server.

Usage:
    python -m client status
    python -m client tree --depth 2 --path 4
    python -m client click --path 4.13
    python -m client click --text Graftd --parent 4
"""

import argparse
import json
import sys
import urllib.request
import urllib.error

DEFAULT_URL = "http://127.0.0.1:8520"


def request(base, method, path, body=None):
    url = f"{base}{path}"
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())
    except urllib.error.URLError as e:
        return {"error": f"Connection failed: {e}"}


def print_tree(node, indent=0):
    prefix = "  " * indent
    text = node.get("text", "")
    typ = node.get("type", "")
    label = f"{typ}: {text!r}" if text else typ
    print(f"{prefix}{label}")
    for child in node.get("children", []):
        print_tree(child, indent + 1)


def main():
    parser = argparse.ArgumentParser(description="revit-remote client")
    parser.add_argument("command", choices=["status", "tree", "click", "windows", "connect"])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--depth", type=int, default=1)
    parser.add_argument("--path", type=str, default=None)
    parser.add_argument("--text", type=str, default=None)
    parser.add_argument("--parent", type=str, default=None)
    parser.add_argument("--method", type=str, default="invoke")
    parser.add_argument("--max", type=int, default=50)
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    if args.command == "status":
        result = request(args.url, "GET", "/status")

    elif args.command == "windows":
        result = request(args.url, "GET", "/windows")

    elif args.command == "connect":
        result = request(args.url, "POST", "/connect")

    elif args.command == "tree":
        qs = f"?depth={args.depth}&max={args.max}"
        if args.path:
            qs += f"&path={args.path}"
        result = request(args.url, "GET", f"/tree{qs}")
        if not args.json and "type" in result:
            print_tree(result)
            return

    elif args.command == "click":
        body = {"method": args.method}
        if args.path:
            body["path"] = args.path
        if args.text:
            body["text"] = args.text
        if args.parent:
            body["parent_path"] = args.parent
        result = request(args.url, "POST", "/click", body)

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
