"""Revit UI automation helpers via pywinauto.

IMPORTANT: Never use descendants(), child_window(), or print_control_identifiers()
on the main Revit window -- they hang due to the enormous UIA tree.
Use shallow_search() which walks children() level by level instead.

Elements found by auto_id are cached. First lookup is slow (BFS),
subsequent lookups are instant. Cache is invalidated on connect().
"""

import time

from pywinauto import Application


# -- Element cache (auto_id -> path of child indices) ----------------------

_path_cache = {}   # auto_id_fragment -> [int, int, ...]
_main_win = None


def _cache_key(parent, fragment):
    """Cache key combining parent handle and fragment."""
    try:
        return (parent.handle, fragment)
    except Exception:
        return (id(parent), fragment)


def _resolve_path(root, path):
    """Walk a cached index path. Returns element or None if stale."""
    current = root
    try:
        for idx in path:
            kids = current.children()
            if idx >= len(kids):
                return None
            current = kids[idx]
        return current
    except Exception:
        return None


def _record_path(parent, elem, match_fn, max_depth=3, _depth=0, _path=None):
    """Like shallow_search but records the index path to the found element."""
    if _path is None:
        _path = []
    try:
        kids = parent.children()
    except Exception:
        return None, None
    for i, child in enumerate(kids):
        try:
            if match_fn(child):
                return child, _path + [i]
        except Exception:
            pass
    if _depth < max_depth:
        for i, child in enumerate(kids):
            result, result_path = _record_path(child, elem, match_fn, max_depth, _depth + 1, _path + [i])
            if result:
                return result, result_path
    return None, None


# -- Public API ------------------------------------------------------------

def connect():
    """Connect to running Revit process. Returns (app, main_win)."""
    global _main_win, _path_cache
    _path_cache.clear()

    app = Application(backend="uia").connect(path="Revit.exe")
    for w in app.windows():
        t = w.window_text()
        if "revit" in t.lower() and (".rvt" in t.lower() or "autodesk" in t.lower()):
            _main_win = w
            return app, w
    wins = app.windows()
    if wins:
        _main_win = wins[0]
        return app, wins[0]
    raise RuntimeError("No Revit windows found")


def shallow_search(parent, match_fn, max_depth=3, _depth=0):
    """BFS search through children() up to max_depth.
    match_fn(elem) -> bool. Returns element or None."""
    try:
        kids = parent.children()
    except Exception:
        return None
    for child in kids:
        try:
            if match_fn(child):
                return child
        except Exception:
            pass
    if _depth < max_depth:
        for child in kids:
            result = shallow_search(child, match_fn, max_depth, _depth + 1)
            if result:
                return result
    return None


def find_by_auto_id(parent, fragment, depth=3):
    """Find element by automation_id substring. Cached after first lookup."""
    key = _cache_key(parent, fragment)

    # try cache first
    if key in _path_cache:
        elem = _resolve_path(parent, _path_cache[key])
        if elem:
            try:
                aid = elem.automation_id() or ""
                if fragment in aid:
                    return elem
            except Exception:
                pass
        # cache stale, remove
        del _path_cache[key]

    # slow path: BFS with path recording
    match_fn = lambda e: fragment in (e.automation_id() or "")
    elem, path = _record_path(parent, None, match_fn, max_depth=depth)
    if elem and path:
        _path_cache[key] = path
    return elem


def find_by_text(parent, text, depth=2):
    """Find element by exact window_text. Cached after first lookup."""
    key = _cache_key(parent, f"text:{text}")

    if key in _path_cache:
        elem = _resolve_path(parent, _path_cache[key])
        if elem:
            try:
                if elem.window_text() == text:
                    return elem
            except Exception:
                pass
        del _path_cache[key]

    match_fn = lambda e: e.window_text() == text
    elem, path = _record_path(parent, None, match_fn, max_depth=depth)
    if elem and path:
        _path_cache[key] = path
    return elem


def dismiss_security_dialogs(win, timeout=5):
    """Dismiss any 'Always Load' security dialogs. Call before running flows."""
    deadline = time.time() + timeout
    dismissed = 0
    while time.time() < deadline:
        btn = shallow_search(win, lambda e: "Always Load" in e.window_text(), max_depth=4)
        if btn:
            btn.click_input()
            dismissed += 1
            time.sleep(1)
        else:
            break
    return dismissed


def click(elem, method="invoke"):
    """Click an element. Methods: invoke, click_input."""
    if method == "invoke":
        try:
            elem.iface_invoke.Invoke()
            return
        except Exception:
            pass
    elem.click_input()


def screenshot(win, path):
    """Capture window screenshot to file."""
    img = win.capture_as_image()
    if img:
        img.save(path)
    return img


def list_windows(app):
    """List all top-level windows as [(title, wrapper), ...]"""
    result = []
    try:
        for w in app.windows():
            try:
                result.append((w.window_text(), w))
            except Exception:
                pass
    except Exception:
        pass
    return result


def wait_for_window(app, match_fn, timeout=60, poll=2):
    """Wait for a window matching match_fn(title) -> bool. Returns wrapper or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for title, win in list_windows(app):
            if match_fn(title):
                return win
        time.sleep(poll)
    return None


def cache_stats():
    """Return cache contents for debugging."""
    return {str(k): v for k, v in _path_cache.items()}
