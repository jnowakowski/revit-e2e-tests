"""
revit-remote server -- HTTP API for Revit UI automation via pywinauto.

Usage:
    python -m server            # default port 8520
    python -m server --port 9000
"""

import argparse
import json
import re
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

try:
    from pywinauto import Application
except ImportError:
    print("ERROR: pywinauto not installed. Run: pip install pywinauto")
    sys.exit(2)


# ── Revit connection ─────────────────────────────────────────────────

_app = None
_main_win = None


def connect():
    global _app, _main_win
    _app = Application(backend="uia").connect(path="Revit.exe")
    for w in _app.windows():
        t = w.window_text()
        if "revit" in t.lower() and (".rvt" in t.lower() or "autodesk" in t.lower()):
            _main_win = w
            return {"connected": True, "window": t}
    # fallback: first window
    wins = _app.windows()
    if wins:
        _main_win = wins[0]
        return {"connected": True, "window": _main_win.window_text()}
    return {"connected": False, "error": "No Revit windows found"}


def ensure_connected():
    global _app, _main_win
    if _main_win is None:
        return connect()
    # verify still alive
    try:
        _main_win.window_text()
        return {"connected": True}
    except Exception:
        _app = None
        _main_win = None
        return connect()


# ── Tree exploration ──────────────────────────────────────────────────

def elem_to_dict(elem, depth=1, max_children=50):
    """Convert a UI element to a JSON-serializable dict."""
    try:
        d = {
            "text": elem.window_text()[:120],
            "type": elem.friendly_class_name(),
        }
        try:
            aid = elem.automation_id()
            if aid:
                d["id"] = aid[:80]
        except Exception:
            pass
    except Exception:
        return {"text": "<error>", "type": "unknown"}

    if depth > 0:
        try:
            kids = elem.children()[:max_children]
            if kids:
                d["children"] = [elem_to_dict(c, depth - 1, max_children) for c in kids]
        except Exception:
            pass
    return d


def elem_inspect(elem):
    """Detailed inspection of a single element."""
    d = elem_to_dict(elem, depth=0)
    try:
        d["automation_id"] = elem.automation_id()
    except Exception:
        pass
    try:
        d["control_type"] = elem.element_info.control_type
    except Exception:
        pass
    try:
        d["class_name"] = elem.element_info.class_name
    except Exception:
        pass
    try:
        rect = elem.rectangle()
        d["rect"] = {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom}
    except Exception:
        pass
    try:
        d["enabled"] = elem.is_enabled()
    except Exception:
        pass
    try:
        d["visible"] = elem.is_visible()
    except Exception:
        pass
    # UIA patterns
    patterns = []
    for pattern_name in ["invoke", "toggle", "selection", "value", "expand_collapse"]:
        try:
            getattr(elem, f"iface_{pattern_name}")
            patterns.append(pattern_name)
        except Exception:
            pass
    if patterns:
        d["patterns"] = patterns
    return d


def get_element_by_path(path):
    """Navigate to an element by index path like '4.13' (child 4, then child 13)."""
    ensure_connected()
    current = _main_win
    for idx_str in path.split("."):
        idx = int(idx_str)
        kids = current.children()
        if idx >= len(kids):
            return None
        current = kids[idx]
    return current


# ── Click ─────────────────────────────────────────────────────────────

def click_element(path=None, text=None, parent_path=None, method="invoke"):
    """Click an element by path or by text search within a parent."""
    ensure_connected()

    if path:
        elem = get_element_by_path(path)
        if not elem:
            return {"clicked": False, "error": f"Element not found at path {path}"}
    elif text:
        parent = _main_win
        if parent_path:
            parent = get_element_by_path(parent_path)
            if not parent:
                return {"clicked": False, "error": f"Parent not found at path {parent_path}"}
        # search children
        elem = None
        for child in parent.children():
            try:
                if child.window_text() == text:
                    elem = child
                    break
            except Exception:
                continue
        if not elem:
            return {"clicked": False, "error": f"No child with text {text!r}"}
    else:
        return {"clicked": False, "error": "Provide 'path' or 'text'"}

    t = elem.window_text()
    ct = elem.friendly_class_name()

    try:
        if method == "invoke":
            try:
                elem.iface_invoke.Invoke()
            except Exception:
                elem.click_input()
        elif method == "toggle":
            elem.iface_toggle.Toggle()
        elif method == "click_input":
            elem.click_input()
        elif method == "click":
            elem.click()
        elif method == "focus_click":
            _main_win.set_focus()
            import time as _t
            _t.sleep(0.3)
            elem.click_input()
        else:
            elem.click_input()
        return {"clicked": True, "text": t, "type": ct, "method": method}
    except Exception as e:
        return {"clicked": False, "text": t, "type": ct, "error": str(e)}


# ── HTTP handler ──────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # quieter logging
        print(f"[{time.strftime('%H:%M:%S')}] {args[0]}")

    def respond(self, data, status=200):
        body = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/status":
            result = ensure_connected()
            if _main_win:
                result["window"] = _main_win.window_text()
            self.respond(result)

        elif parsed.path == "/tree":
            ensure_connected()
            depth = int(qs.get("depth", ["1"])[0])
            parent_path = qs.get("path", [None])[0]
            max_kids = int(qs.get("max", ["50"])[0])

            target = _main_win
            if parent_path:
                target = get_element_by_path(parent_path)
                if not target:
                    self.respond({"error": f"Path {parent_path} not found"}, 404)
                    return

            tree = elem_to_dict(target, depth=depth, max_children=max_kids)
            self.respond(tree)

        elif parsed.path == "/inspect":
            ensure_connected()
            path = qs.get("path", [None])[0]
            if not path:
                self.respond({"error": "Provide ?path=X.Y.Z"}, 400)
                return
            target = get_element_by_path(path)
            if not target:
                self.respond({"error": f"Path {path} not found"}, 404)
                return
            self.respond(elem_inspect(target))

        elif parsed.path == "/search":
            ensure_connected()
            query = qs.get("q", [None])[0]
            by = qs.get("by", ["auto_id"])[0]  # auto_id, text
            scope = qs.get("scope", [None])[0]  # path to search within
            max_depth = int(qs.get("depth", ["3"])[0])

            if not query:
                self.respond({"error": "Provide ?q=search_term"}, 400)
                return

            target = _main_win
            if scope:
                target = get_element_by_path(scope)
                if not target:
                    self.respond({"error": f"Scope path {scope} not found"}, 404)
                    return

            results = []
            def search_recursive(elem, current_path, depth):
                if depth > max_depth:
                    return
                try:
                    kids = elem.children()
                except Exception:
                    return
                for i, child in enumerate(kids):
                    child_path = f"{current_path}.{i}" if current_path else str(i)
                    try:
                        match = False
                        if by == "auto_id":
                            aid = child.automation_id()
                            if query.lower() in aid.lower():
                                match = True
                        elif by == "text":
                            t = child.window_text()
                            if query.lower() in t.lower():
                                match = True
                        if match:
                            results.append({
                                "path": child_path,
                                "text": child.window_text()[:120],
                                "type": child.friendly_class_name(),
                                "auto_id": child.automation_id()[:80] if child.automation_id() else "",
                            })
                    except Exception:
                        pass
                    if depth < max_depth:
                        search_recursive(child, child_path, depth + 1)

            search_recursive(target, scope or "", 0)
            self.respond({"query": query, "by": by, "results": results})

        elif parsed.path == "/windows":
            ensure_connected()
            wins = []
            for w in _app.windows():
                try:
                    wins.append(w.window_text())
                except Exception:
                    pass
            self.respond({"windows": wins})

        else:
            self.respond({"error": "Unknown endpoint", "endpoints": [
                "GET /status", "GET /tree?depth=1&path=4",
                "GET /windows", "POST /click"
            ]}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        content_len = int(self.headers.get("Content-Length", 0))
        body = {}
        if content_len:
            body = json.loads(self.rfile.read(content_len))

        if parsed.path == "/click":
            result = click_element(
                path=body.get("path"),
                text=body.get("text"),
                parent_path=body.get("parent_path"),
                method=body.get("method", "invoke"),
            )
            self.respond(result)

        elif parsed.path == "/connect":
            result = connect()
            self.respond(result)

        else:
            self.respond({"error": "Unknown endpoint"}, 404)


# ── Main ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="revit-remote server")
    parser.add_argument("--port", type=int, default=8520)
    args = parser.parse_args()

    # connect on startup
    print("Connecting to Revit...")
    result = connect()
    print(f"  {result}")

    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Listening on http://127.0.0.1:{args.port}")
    print(f"  GET  /status  /tree?depth=1&path=4  /windows")
    print(f"  POST /click   /connect")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()
