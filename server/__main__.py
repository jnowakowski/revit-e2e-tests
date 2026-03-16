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
from server import health

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
    ch.setLevel(logging.INFO)
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
_connected = False


def connect():
    global _app, _main_win, _connected
    try:
        _app = Application(backend="uia").connect(path="Revit.exe")
    except Exception as e:
        _app = None
        _main_win = None
        _connected = False
        log.warning("Revit not found: %s", e)
        return {"connected": False, "error": str(e)}
    for w in _app.windows():
        t = w.window_text()
        if "revit" in t.lower() and (".rvt" in t.lower() or "autodesk" in t.lower()):
            _main_win = w
            _connected = True
            log.info("Connected to Revit: %s", t)
            return {"connected": True, "window": t}
    wins = _app.windows()
    if wins:
        _main_win = wins[0]
        _connected = True
        t = _main_win.window_text()
        log.info("Connected to Revit (fallback): %r", t)
        return {"connected": True, "window": t}
    _connected = False
    return {"connected": False, "error": "No Revit windows found"}


def ensure_connected():
    global _app, _main_win, _connected
    if _main_win is None:
        return connect()
    try:
        _main_win.window_text()
        return {"connected": True}
    except Exception:
        _app = None
        _main_win = None
        _connected = False
        return connect()


def _deep_scan():
    """Build deep cache of Revit UI tree. Called after connect."""
    if not _main_win or not _cache:
        return
    log.info("DEEP SCAN: starting...")
    try:
        # root depth=2 (main window children + their children)
        t0 = time.time()
        tree = elem_to_dict(_main_win, depth=2)
        _cache.record(_doc_title(), "/tree", tree, path=None)
        log.info("DEEP SCAN: root depth=2  %dms", int((time.time() - t0) * 1000))

        # ListBox[0] depth=4 (ribbon tabs -> panels -> buttons)
        t0 = time.time()
        kids = _main_win.children()
        if kids:
            lb = kids[0]
            lb_tree = elem_to_dict(lb, depth=4)
            _cache.record(_doc_title(), "/tree", lb_tree, path="0")
            log.info("DEEP SCAN: ListBox[0] depth=4  %dms", int((time.time() - t0) * 1000))

        # mMainTabs depth=1 (tab buttons -- already in root but explicit)
        for i, kid in enumerate(kids):
            try:
                aid = kid.automation_id()
                if aid == "mMainTabs":
                    t0 = time.time()
                    tabs_tree = elem_to_dict(kid, depth=1)
                    _cache.record(_doc_title(), "/tree", tabs_tree, path=str(i))
                    log.info("DEEP SCAN: mMainTabs[%d] depth=1  %dms", i, int((time.time() - t0) * 1000))
                    break
            except Exception:
                pass

        _rebuild_id_map()
        log.info("DEEP SCAN: complete")
    except Exception as e:
        log.warning("DEEP SCAN: failed: %s", e)


def _heartbeat_loop():
    """Background thread: monitor Revit process, auto-reconnect, deep scan."""
    global _connected
    import psutil
    _last_title = ""
    while True:
        time.sleep(3)
        # check if Revit process exists
        revit_alive = any(
            p.info['name'] and p.info['name'].lower() == 'revit.exe'
            for p in psutil.process_iter(['name'])
        )
        if not revit_alive:
            if _connected:
                log.info("HEARTBEAT: Revit process gone. Marking disconnected.")
                _connected = False
                globals()['_app'] = None
                globals()['_main_win'] = None
                _clear_wrappers()
                _last_title = ""
            continue

        if not _connected:
            log.info("HEARTBEAT: Revit process found. Reconnecting...")
            time.sleep(2)
            result = connect()
            log.info("HEARTBEAT: reconnect: %s", result)
            if _connected:
                # deep scan after fresh connect
                time.sleep(2)
                _deep_scan()
            continue

        # connected -- check for title changes (project loaded/changed)
        try:
            title = _main_win.window_text() if _main_win else ""
            if title != _last_title:
                if _last_title == "" and title:
                    log.info("HEARTBEAT: Project loaded: %s. Refreshing cache...", title)
                    _deep_scan()
                elif title == "" and _last_title:
                    log.info("HEARTBEAT: Project closed or Revit restarting.")
                _last_title = title
        except Exception:
            log.info("HEARTBEAT: Window stale. Reconnecting...")
            _connected = False
            _last_title = ""
            connect()


def _doc_title():
    """Current Revit window title (for cache tagging)."""
    try:
        return _main_win.window_text() if _main_win else None
    except Exception:
        return None


# ── AutomationId map + wrapper cache ─────────────────────────────────

_id_map = {}  # auto_id -> [{path, type, text}]
_id_map_child_count = None
_wrappers = {}  # auto_id -> pywinauto wrapper (live element reference)


def _clear_wrappers():
    """Clear cached wrappers (on reconnect, dialog change, etc.)."""
    global _wrappers
    _wrappers.clear()
    log.info("WRAPPERS: cleared")


def _rebuild_id_map():
    """Rebuild auto_id -> path map from all cached trees."""
    global _id_map, _id_map_child_count
    _id_map = {}
    _clear_wrappers()

    if not _cache:
        return

    rows = _cache._conn.execute(
        "SELECT path, response FROM observations WHERE endpoint='/tree' "
        "ORDER BY created_at DESC"
    ).fetchall()

    seen = set()
    for row in rows:
        tree = json.loads(row["response"])
        base = row["path"] or ""
        _index_tree(tree, base, seen)

    # track child count for invalidation
    try:
        if _main_win:
            _id_map_child_count = len(_main_win.children())
    except Exception:
        pass

    log.info("ID_MAP: rebuilt. %d auto_ids, %d entries, child_count=%s",
             len(_id_map), sum(len(v) for v in _id_map.values()), _id_map_child_count)


def _index_tree(node, current_path, seen):
    """Recursively index auto_ids from a JSON tree node."""
    aid = node.get("id", "")
    if aid:
        key = f"{aid}:{node.get('type','')}:{current_path}"
        if key not in seen:
            seen.add(key)
            if aid not in _id_map:
                _id_map[aid] = []
            _id_map[aid].append({
                "path": current_path,
                "type": node.get("type", ""),
                "text": node.get("text", ""),
            })
    for i, child in enumerate(node.get("children", [])):
        child_path = f"{current_path}.{i}" if current_path else str(i)
        _index_tree(child, child_path, seen)


_id_map_last_check = 0

def _check_id_map_valid():
    """Check if id_map needs rebuild. Only checks every 10s to avoid slow children() calls."""
    global _id_map_child_count, _id_map_last_check
    now = time.time()
    if now - _id_map_last_check < 10:
        return  # checked recently, skip
    _id_map_last_check = now
    try:
        if _main_win:
            count = len(_main_win.children())
            if count != _id_map_child_count:
                log.info("ID_MAP: child count changed %s -> %s, rebuilding...",
                         _id_map_child_count, count)
                _rebuild_id_map()
    except Exception:
        pass


def resolve_auto_id(auto_id, control_type=None):
    """Look up auto_id in map. Returns list of {path, type, text}."""
    _check_id_map_valid()
    entries = _id_map.get(auto_id, [])
    if control_type:
        entries = [e for e in entries if e["type"] == control_type]
    return entries


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


_children_cache = {}  # wrapper_id -> (timestamp, [children])
_CHILDREN_TTL = 5  # seconds

def _cached_children(elem):
    """Get children with short TTL cache to avoid repeated slow UIA calls."""
    key = id(elem)
    now = time.time()
    if key in _children_cache:
        ts, kids = _children_cache[key]
        if now - ts < _CHILDREN_TTL:
            return kids
    kids = elem.children()
    _children_cache[key] = (now, kids)
    return kids


def get_element_by_path(path):
    """Navigate to an element by index path like '4.13' (child 4, then child 13)."""
    ensure_connected()
    current = _main_win
    for idx_str in path.split("."):
        idx = int(idx_str)
        kids = _cached_children(current)
        if idx >= len(kids):
            return None
        current = kids[idx]
    return current


def _search_json_recursive(node, query, by, current_path, results, seen_ids):
    """Recursively search a cached JSON tree. Deduplicates by auto_id."""
    match = False
    node_id = node.get("id", "")
    if by == "auto_id" and node_id and query.lower() in node_id.lower():
        match = True
    elif by == "text":
        t = node.get("text", "")
        if t and query.lower() in t.lower():
            match = True
    if match:
        dedup_key = f"{node_id}:{node.get('text','')}:{node.get('type','')}"
        if dedup_key not in seen_ids:
            seen_ids.add(dedup_key)
            results.append({
                "path": current_path,
                "text": node.get("text", "")[:120],
                "type": node.get("type", ""),
                "auto_id": node_id,
            })
    for i, child in enumerate(node.get("children", [])):
        child_path = f"{current_path}.{i}" if current_path else str(i)
        _search_json_recursive(child, query, by, child_path, results, seen_ids)


def _find_path_in_json(tree, target, _prefix=""):
    """Find index path of a node in a JSON tree by matching text+type+id."""
    for i, child in enumerate(tree.get("children", [])):
        path = f"{_prefix}.{i}" if _prefix else str(i)
        if (child.get("text") == target.get("text") and
            child.get("type") == target.get("type") and
            child.get("id", "") == target.get("id", "")):
            return path
        deeper = _find_path_in_json(child, target, path)
        if deeper:
            return deeper
    return None


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

        if parsed.path == "/health":
            def _reconnect():
                global _main_win
                try:
                    connect()
                except Exception:
                    pass
                return _main_win
            h = health.check(_main_win, reconnect_fn=_reconnect)
            log.info("GET /health  state=%s  %s", h["state"], h.get("in_state", ""))
            self.respond(h)

        elif parsed.path == "/status":
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
            query = qs.get("q", [None])[0]
            by = qs.get("by", ["auto_id"])[0]
            scope = qs.get("scope", [None])[0]
            use_cache = qs.get("cache", ["auto"])[0]  # auto, only, live

            if not query:
                self.respond({"error": "Provide ?q=search_term"}, 400)
                return

            # try cache first via jmespath (unless live forced)
            results = []
            source = "live"
            if use_cache != "live" and _cache:
                t0 = time.time()
                # search ALL cached trees (root and subtrees)
                all_matches = []
                rows = _cache._conn.execute(
                    "SELECT path, response FROM observations WHERE endpoint='/tree' "
                    "ORDER BY created_at DESC"
                ).fetchall()
                seen_ids = set()
                for row in rows:
                    cached_tree = json.loads(row["response"])
                    base_path = row["path"] or ""
                    _search_json_recursive(cached_tree, query, by, base_path, all_matches, seen_ids)
                for m in all_matches:
                    results.append(m)
                ms = int((time.time() - t0) * 1000)
                if results:
                    source = "cache"
                    _stats.cache_hit += 1
                    log.info("CACHE /search  q=%r by=%s hits=%d  %dms  [%s]", query, by, len(results), ms, _stats.summary())

            # live fallback (unless cache-only)
            if not results and use_cache != "only":
                ensure_connected()
                max_depth = int(qs.get("depth", ["3"])[0])
                target = _main_win
                if scope:
                    target = get_element_by_path(scope)
                    if not target:
                        self.respond({"error": f"Scope path {scope} not found"}, 404)
                        return

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
                source = "live"
                log.info("LIVE /search  q=%r by=%s hits=%d  %dms  [%s]", query, by, len(results), ms, _stats.summary())
            search_result = {"query": query, "by": by, "source": source, "results": results}
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

        elif parsed.path == "/resolve":
            # GET /resolve?auto_id=Graftd&type=Button
            aid = qs.get("auto_id", [None])[0]
            if not aid:
                self.respond({"error": "Provide ?auto_id=X"}, 400)
                return
            ctype = qs.get("type", [None])[0]
            entries = resolve_auto_id(aid, control_type=ctype)
            log.info("GET /resolve  auto_id=%r type=%s  hits=%d", aid, ctype, len(entries))
            self.respond({"auto_id": aid, "type": ctype, "results": entries})

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

            # auto_id click: resolve from map, click by path
            auto_id = body.get("auto_id")
            if auto_id:
                ensure_connected()
                ctype = body.get("type")
                method = body.get("method", "invoke")
                cache_key = f"{auto_id}:{ctype or '*'}"

                # 1. Try cached wrapper (instant)
                elem = None
                if cache_key in _wrappers:
                    elem = _wrappers[cache_key]
                    log.info("CLICK auto_id=%r: wrapper cache HIT", auto_id)

                # 2. Resolve from id_map + walk path (slow but only once)
                entry_path = ""
                if not elem:
                    entries = resolve_auto_id(auto_id, control_type=ctype)
                    if not entries:
                        _deep_scan()
                        entries = resolve_auto_id(auto_id, control_type=ctype)
                    if not entries:
                        self.respond({"clicked": False, "error": f"auto_id {auto_id!r} not found"})
                        return
                    entry_path = entries[0]["path"]
                    elem = get_element_by_path(entry_path)
                    if not elem:
                        _rebuild_id_map()
                        entries = resolve_auto_id(auto_id, control_type=ctype)
                        if entries:
                            entry_path = entries[0]["path"]
                            elem = get_element_by_path(entry_path)
                    if elem:
                        _wrappers[cache_key] = elem  # cache for next time
                        log.info("CLICK auto_id=%r: resolved path=%s, wrapper CACHED", auto_id, entry_path)

                if not elem:
                    self.respond({"clicked": False, "error": f"auto_id {auto_id!r} not resolved"})
                    return

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
                    result = {"clicked": True, "text": t, "type": ct, "method": method,
                              "auto_id": auto_id, "path": entry_path, "cached": cache_key in _wrappers}
                except Exception as e:
                    # wrapper failed, clear it
                    _wrappers.pop(cache_key, None)
                    result = {"clicked": False, "text": t, "error": str(e)}
                log.info("CLICK auto_id=%r ok=%s  [%s]", auto_id, result.get("clicked"), _stats.summary())
                self.respond(result)
                return

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

    # start heartbeat
    import threading
    hb = threading.Thread(target=_heartbeat_loop, daemon=True)
    hb.start()
    print("Heartbeat: monitoring Revit process every 3s")

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
