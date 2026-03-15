"""Revit UI automation helpers via pywinauto.

IMPORTANT: Never use descendants(), child_window(), or print_control_identifiers()
on the main Revit window -- they hang due to the enormous UIA tree.
Use shallow_search() which walks children() level by level instead.
"""

import re
import time

from pywinauto import Application


def connect():
    """Connect to running Revit process. Returns (app, main_win)."""
    app = Application(backend="uia").connect(path="Revit.exe")
    for w in app.windows():
        t = w.window_text()
        if "revit" in t.lower() and (".rvt" in t.lower() or "autodesk" in t.lower()):
            return app, w
    wins = app.windows()
    if wins:
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
    """Find element by automation_id substring."""
    return shallow_search(
        parent,
        lambda e: fragment in (e.automation_id() or ""),
        max_depth=depth,
    )


def find_by_text(parent, text, depth=2):
    """Find element by exact window_text."""
    return shallow_search(
        parent,
        lambda e: e.window_text() == text,
        max_depth=depth,
    )


def click(elem, method="invoke"):
    """Click an element. Methods: invoke, click_input, focus_click."""
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
