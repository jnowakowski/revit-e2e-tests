# Speed Optimization TODO

## Problem
Every `children()` call on Revit UIA costs 3-5s (COM roundtrip).
Path `0.18.0.0.1.0.0` = 7 calls = 35s just to reach an element.

## Current State
- SQLite cache has the tree as JSON (fast to query)
- id_map resolves auto_id -> index path (instant lookup)
- But `/click` still walks the live UIA tree via `get_element_by_path()` (slow)

## Fix: Cache Live Wrappers

After first `get_element_by_path()`, store the pywinauto wrapper object:

```python
_wrapper_cache = {}  # auto_id -> pywinauto wrapper

def click_by_auto_id(auto_id):
    if auto_id in _wrapper_cache:
        elem = _wrapper_cache[auto_id]
        try:
            elem.click_input()  # instant, no tree walk
            return
        except:
            del _wrapper_cache[auto_id]  # stale, remove

    # slow path: resolve + walk
    path = id_map[auto_id]
    elem = get_element_by_path(path)
    _wrapper_cache[auto_id] = elem  # cache for next time
    elem.click_input()
```

Invalidate wrapper cache on:
- Revit restart (heartbeat detects process gone)
- Child count change (dialog popup/dismiss)
- Any click that throws (wrapper stale)

## Expected Improvement
- First click on auto_id: 8-35s (path walk, unavoidable)
- Second click on same auto_id: <100ms (cached wrapper)
- Full flow second run: ~5s overhead instead of ~85s

## Alternative: Fewer Levels
Instead of path `0.18.0.0.1.0.0` (7 levels), cache intermediate
wrappers too. After resolving child[0] (ListBox), cache it.
Next time we need child[0].child[18], start from cached ListBox
wrapper instead of main_win.
