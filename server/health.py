"""
Revit process health check -- lifecycle state, resource usage, context for debugging.

States:
  not_running  -- no Revit.exe process
  starting     -- process exists, memory < 200MB (loading runtime)
  security_dialog -- children[0] is Security dialog (blocking)
  loading      -- memory growing, no project in title yet
  ready        -- project loaded, Revit responsive
  busy         -- Revit running a command (result dialog or flyout visible)
"""

import time

import psutil


_revit_start_time = None
_last_state = None
_state_since = None


def _find_revit():
    """Find Revit.exe process. Returns psutil.Process or None."""
    for proc in psutil.process_iter(['name', 'pid']):
        try:
            if proc.info['name'] and proc.info['name'].lower() == 'revit.exe':
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None


def _proc_info(proc):
    """Get process stats."""
    try:
        mem = proc.memory_info()
        cpu = proc.cpu_percent(interval=0.5)
        create_time = proc.create_time()
        uptime = time.time() - create_time
        return {
            "pid": proc.pid,
            "memory_mb": round(mem.rss / 1024 / 1024),
            "cpu_pct": cpu,
            "uptime_s": round(uptime),
            "started_ago": f"{int(uptime)}s ago",
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def _detect_state(proc_info, main_win):
    """Determine lifecycle state from process info and window state."""
    if proc_info is None:
        return "not_running"

    mem = proc_info["memory_mb"]
    uptime = proc_info["uptime_s"]

    if mem < 200 and uptime < 60:
        return "starting"

    # check window
    title = ""
    first_child_text = ""
    try:
        title = main_win.window_text() if main_win else ""
    except Exception:
        pass

    try:
        if main_win:
            kids = main_win.children()
            if kids:
                first_child_text = kids[0].window_text()
    except Exception:
        pass

    if "Security" in first_child_text or "Unsigned" in first_child_text:
        return "security_dialog"

    if "Command Failure" in first_child_text:
        return "busy"

    if first_child_text and ("GenerateElevations" in first_child_text or "GetDetails" in first_child_text):
        return "busy"

    if ".rvt" in title.lower():
        return "ready"

    if title == "" or "Home" in title:
        return "loading"

    return "ready"


def check(main_win=None):
    """Full health check. Returns dict with state, process info, context."""
    global _last_state, _state_since

    proc = _find_revit()
    info = _proc_info(proc) if proc else None
    state = _detect_state(info, main_win)

    # track state transitions
    now = time.time()
    if state != _last_state:
        _state_since = now
        _last_state = state
    in_state_s = round(now - _state_since) if _state_since else 0

    result = {
        "state": state,
        "in_state_s": in_state_s,
        "in_state": f"{in_state_s}s in {state}",
    }

    if info:
        result["process"] = info

    # window context
    if main_win:
        try:
            title = main_win.window_text()
            if title:
                result["window"] = title
        except Exception:
            pass

    # action hints for debugging
    if state == "not_running":
        result["hint"] = "Run: .\\dev-restart-quick.ps1"
    elif state == "security_dialog":
        result["hint"] = "Run: curl -X POST localhost:8520/click -d '{\"path\":\"0.3\",\"method\":\"invoke\"}' -H 'Content-Type: application/json'"
    elif state == "loading":
        result["hint"] = "Revit is loading. Wait for title to contain .rvt"
    elif state == "starting":
        result["hint"] = "Revit just started. Wait ~30s for UI"

    return result
