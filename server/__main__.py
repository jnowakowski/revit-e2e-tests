"""
revit-remote server -- HTTP API for Revit UI automation via pywinauto.

Usage:
    python -m server            # default port 8520
    python -m server --port 9000
"""

import argparse
import json
import logging
import re
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:
    from pywinauto import Application
except ImportError:
    print("ERROR: pywinauto not installed. Run: pip install pywinauto")
    sys.exit(2)

try:
    import jmespath
except ImportError:
    print("ERROR: jmespath not installed. Run: pip install jmespath")
    sys.exit(2)

from server.cache import Cache

LOG_PATH = Path(__file__).resolve().parent.parent / "server.log"

log = logging.getLogger("revit-remote")


def _setup_logging():
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)-5s  %(message)s", datefmt="%H:%M:%S")
    # file handler -- tail -f server.log
    fh = logging.FileHandler(str(LOG_PATH), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    # console -- only warnings+
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(fmt)
    log.addHandler(ch)


# ── Stats ────────────────────────────────────────────────────────────

class _Stats:
    __slots__ = ("live", "cache_hit", "errors", "_start")

    def __init__(self):
        self.live = 0
        self.cache_hit = 0
        self.errors = 0
        self._start = time.time()

    def summary(self):
        total = self.live + self.cache_hit
        uptime = int(time.time() - self._start)
        h, m, s = uptime // 3600, (uptime % 3600) // 60, uptime % 60
        return (
            f"reqs={total} live={self.live} cache={self.cache_hit} "
            f"errors={self.errors} uptime={h:02d}:{m:02d}:{s:02d}"
        )

_stats = _Stats()


# ── Revit connection ─────────────────────────────────────────────────

_app = None
_main_win = None
_cache = None


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


def _doc_title():
    """Current Revit window title (for cache tagging)."""
    try:
        return _main_win.window_text() if _main_win else None
    except Exception:
        return None


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


def _find_elem_by_dict(parent, target_dict, max_depth=3, _depth=0):
    """Find a live UIA element matching a dict from elem_to_dict (by text+type+id)."""
    try:
        kids = parent.children()
    except Exception:
        return None
    for child in kids:
        try:
            match = True
            if "text" in target_dict and child.window_text()[:120] != target_dict["text"]:
                match = False
            if "type" in target_dict and child.friendly_class_name() != target_dict["type"]:
                match = False
            if "id" in target_dict:
                aid = child.automation_id() or ""
                if aid[:80] != target_dict["id"]:
                    match = False
            if match:
                return child
        except Exception:
            pass
    if _depth < max_depth:
        for child in kids:
            result = _find_elem_by_dict(child, target_dict, max_depth, _depth + 1)
            if result:
                return result
    return None


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
        pass  # we do our own logging

    def respond(self, data, status=200):
        body = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        if status >= 400:
            _stats.errors += 1

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/status":
            result = ensure_connected()
            if _main_win:
                result["window"] = _main_win.window_text()
            result["stats"] = _stats.summary()
            log.info("GET /status  [%s]", _stats.summary())
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

            t0 = time.time()
            tree = elem_to_dict(target, depth=depth, max_children=max_kids)
            ms = int((time.time() - t0) * 1000)
            _stats.live += 1
            if _cache:
                _cache.record(_doc_title(), "/tree", tree, path=parent_path)
            log.info("LIVE /tree  depth=%d path=%s  %dms  [%s]", depth, parent_path, ms, _stats.summary())
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
            t0 = time.time()
            result = elem_inspect(target)
            ms = int((time.time() - t0) * 1000)
            _stats.live += 1
            if _cache:
                _cache.record(_doc_title(), "/inspect", result, path=path)
            log.info("LIVE /inspect  path=%s  %dms  [%s]", path, ms, _stats.summary())
            self.respond(result)

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

            t0 = time.time()
            search_recursive(target, scope or "", 0)
            ms = int((time.time() - t0) * 1000)
            _stats.live += 1
            search_result = {"query": query, "by": by, "results": results}
            if _cache:
                _cache.record(_doc_title(), "/search", search_result, path=scope, query=query)
            log.info("LIVE /search  q=%r by=%s hits=%d  %dms  [%s]", query, by, len(results), ms, _stats.summary())
            self.respond(search_result)

        elif parsed.path == "/q":
            # jmespath query on tree
            # GET /q?s=children[?text=='Graftd']&depth=2&path=0
            # GET /q?s=children[?contains(id,'Elevations')]&depth=3
            selector = qs.get("s", [None])[0]
            if not selector:
                self.respond({"error": "Provide ?s=jmespath_expression"}, 400)
                return
            depth = int(qs.get("depth", ["2"])[0])
            parent_path = qs.get("path", [None])[0]
            use_cache = qs.get("cache", ["auto"])[0]  # auto, only, live

            tree = None
            source = "cache"

            # try cache first (unless live forced)
            if use_cache != "live" and _cache:
                cached = _cache.latest_tree(parent_path, depth)
                if cached:
                    tree = cached

            # live fetch if no cache or cache=live
            if tree is None or use_cache == "live":
                ensure_connected()
                target = _main_win
                if parent_path:
                    target = get_element_by_path(parent_path)
                    if not target:
                        self.respond({"error": f"Path {parent_path} not found"}, 404)
                        return
                t0 = time.time()
                tree = elem_to_dict(target, depth=depth)
                ms = int((time.time() - t0) * 1000)
                source = "live"
                _stats.live += 1
                if _cache:
                    _cache.record(_doc_title(), "/tree", tree, path=parent_path)
                log.info("LIVE /q fetch  depth=%d path=%s  %dms", depth, parent_path, ms)

            if tree is None:
                self.respond({"error": "No tree data available"}, 404)
                return

            # apply jmespath
            try:
                result = jmespath.search(selector, tree)
            except Exception as e:
                self.respond({"error": f"jmespath error: {e}", "selector": selector}, 400)
                return

            if source == "cache":
                _stats.cache_hit += 1
            log.info("%s /q  s=%r  source=%s  [%s]", source.upper(), selector, source, _stats.summary())
            self.respond({"selector": selector, "source": source, "result": result})

        elif parsed.path == "/windows":
            ensure_connected()
            wins = []
            for w in _app.windows():
                try:
                    wins.append(w.window_text())
                except Exception:
                    pass
            _stats.live += 1
            log.info("LIVE /windows  count=%d  [%s]", len(wins), _stats.summary())
            self.respond({"windows": wins})

        # ── Cache endpoints (read from SQLite, no Revit needed) ──────
        elif parsed.path == "/cache/documents":
            _stats.cache_hit += 1
            docs = _cache.documents() if _cache else []
            log.info("CACHE /cache/documents  docs=%d  [%s]", len(docs), _stats.summary())
            self.respond({"documents": docs} if _cache else {"error": "cache disabled"})

        elif parsed.path == "/cache/history":
            if not _cache:
                self.respond({"error": "cache disabled"})
                return
            _stats.cache_hit += 1
            doc = qs.get("document", [None])[0]
            endpoint = qs.get("endpoint", [None])[0]
            limit = int(qs.get("limit", ["50"])[0])
            rows = _cache.history(document=doc, endpoint=endpoint, limit=limit)
            log.info("CACHE /cache/history  doc=%s endpoint=%s rows=%d  [%s]", doc, endpoint, len(rows), _stats.summary())
            self.respond({"history": rows})

        elif parsed.path == "/cache/search":
            if not _cache:
                self.respond({"error": "cache disabled"})
                return
            term = qs.get("q", [None])[0]
            if not term:
                self.respond({"error": "Provide ?q=search_term"}, 400)
                return
            _stats.cache_hit += 1
            doc = qs.get("document", [None])[0]
            limit = int(qs.get("limit", ["50"])[0])
            rows = _cache.search(term, document=doc, limit=limit)
            log.info("CACHE /cache/search  q=%r hits=%d  [%s]", term, len(rows), _stats.summary())
            self.respond({"results": rows})

        elif parsed.path == "/cache/observation":
            if not _cache:
                self.respond({"error": "cache disabled"})
                return
            obs_id = qs.get("id", [None])[0]
            if not obs_id:
                self.respond({"error": "Provide ?id=N"}, 400)
                return
            _stats.cache_hit += 1
            obs = _cache.get_observation(int(obs_id))
            log.info("CACHE /cache/observation  id=%s found=%s  [%s]", obs_id, obs is not None, _stats.summary())
            if not obs:
                self.respond({"error": "Not found"}, 404)
                return
            self.respond(obs)

        else:
            self.respond({"error": "Unknown endpoint", "endpoints": [
                "GET /status", "GET /tree?depth=1&path=4",
                "GET /windows", "GET /inspect?path=X.Y.Z",
                "GET /search?q=term", "POST /click", "POST /connect",
                "GET /cache/documents", "GET /cache/history",
                "GET /cache/search?q=term", "GET /cache/observation?id=N",
            ]}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        content_len = int(self.headers.get("Content-Length", 0))
        body = {}
        if content_len:
            body = json.loads(self.rfile.read(content_len))

        if parsed.path == "/click":
            _stats.live += 1
            selector = body.get("selector")
            if selector:
                # jmespath selector click: find element by querying tree, get its path
                # first resolve via /q logic to find the element path
                ensure_connected()
                depth = body.get("depth", 3)
                parent_path = body.get("parent_path")
                target = _main_win
                if parent_path:
                    target = get_element_by_path(parent_path)
                    if not target:
                        self.respond({"clicked": False, "error": f"Parent path {parent_path} not found"})
                        return
                tree = elem_to_dict(target, depth=depth)
                try:
                    match = jmespath.search(selector, tree)
                except Exception as e:
                    self.respond({"clicked": False, "error": f"jmespath error: {e}"})
                    return
                if not match:
                    self.respond({"clicked": False, "error": f"Selector matched nothing: {selector}"})
                    return
                # find the matching element in actual UIA tree by walking children
                item = match[0] if isinstance(match, list) else match
                elem = _find_elem_by_dict(target, item, depth)
                if not elem:
                    self.respond({"clicked": False, "error": "Matched in JSON but could not resolve UIA element"})
                    return
                method = body.get("method", "invoke")
                t = elem.window_text()
                ct = elem.friendly_class_name()
                try:
                    if method == "invoke":
                        try:
                            elem.iface_invoke.Invoke()
                        except Exception:
                            elem.click_input()
                    elif method == "focus_click":
                        _main_win.set_focus()
                        time.sleep(0.3)
                        elem.click_input()
                    else:
                        elem.click_input()
                    result = {"clicked": True, "text": t, "type": ct, "method": method, "selector": selector}
                except Exception as e:
                    result = {"clicked": False, "text": t, "type": ct, "error": str(e)}
            else:
                result = click_element(
                    path=body.get("path"),
                    text=body.get("text"),
                    parent_path=body.get("parent_path"),
                    method=body.get("method", "invoke"),
                )
            log.info("LIVE /click  path=%s text=%r selector=%r method=%s ok=%s  [%s]",
                     body.get("path"), body.get("text"), body.get("selector"),
                     body.get("method", "invoke"),
                     result.get("clicked"), _stats.summary())
            self.respond(result)

        elif parsed.path == "/connect":
            result = connect()
            log.info("POST /connect  result=%s", result)
            self.respond(result)

        else:
            self.respond({"error": "Unknown endpoint"}, 404)


# ── Main ──────────────────────────────────────────────────────────────

def main():
    global _cache

    parser = argparse.ArgumentParser(description="revit-remote server")
    parser.add_argument("--port", type=int, default=8520)
    parser.add_argument("--no-cache", action="store_true", help="Disable SQLite cache")
    args = parser.parse_args()

    _setup_logging()
    log.info("=" * 60)
    log.info("Server starting on port %d", args.port)

    # init cache
    if not args.no_cache:
        _cache = Cache()
        log.info("Cache: %s", _cache._path)
        print(f"Cache: {_cache._path}")
    else:
        log.info("Cache: disabled")
        print("Cache: disabled")

    # connect on startup
    print("Connecting to Revit...")
    result = connect()
    log.info("Revit connection: %s", result)
    print(f"  {result}")

    print(f"Log: {LOG_PATH}  (tail -f server.log)")
    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Listening on http://127.0.0.1:{args.port}")
    print(f"  GET  /status  /tree  /inspect  /search  /windows")
    print(f"  GET  /cache/documents  /cache/history  /cache/search  /cache/observation")
    print(f"  POST /click   /connect")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.  Final stats: %s", _stats.summary())
        if _cache:
            _cache.close()
        print("\nShutting down.")


if __name__ == "__main__":
    main()
