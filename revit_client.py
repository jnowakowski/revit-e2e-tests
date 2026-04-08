"""Revit dev server HTTP client. Wraps :52140 API for scripted testing."""

import json
import requests

DEFAULT_BASE = "http://localhost:52140"
DEFAULT_PLUGIN_DLL = "C:/Users/orion/source/repos/auto-detail-views/bin/HotReload/AutoDetailViews.dll"


class RevitClient:
    def __init__(self, base_url=DEFAULT_BASE):
        self.base = base_url

    def _get(self, path):
        r = requests.get(f"{self.base}{path}", timeout=30)
        return r.json()

    def _post(self, path, data=None):
        r = requests.post(f"{self.base}{path}", json=data or {}, timeout=300)
        return r.json()

    # ── Core ──────────────────────────────────────────────

    def status(self):
        return self._get("/status")

    def model_title(self):
        return self.status()["data"].get("title", "")

    def endpoints(self):
        return self._get("/")["data"]["endpoints"]

    # ── Model lifecycle ───────────────────────────────────

    def open_model(self, name):
        return self._post("/open-model", {"model": name})

    def close_model(self):
        return self._post("/close-model")

    def reload_plugin(self, dll=DEFAULT_PLUGIN_DLL):
        return self._post("/reload", {"dll": dll})

    # ── Commands ──────────────────────────────────────────

    def command(self, name):
        return self._post("/command", {"command": name})

    def dump(self):
        return self._post("/dump")

    def set_param(self, element_id, param, value):
        return self._post("/set-param", {
            "elementId": str(element_id),
            "param": param,
            "value": value,
        })

    # ── Convenience ───────────────────────────────────────

    def deep_scan(self):
        return self.command("DeepScan")

    def resolver(self):
        return self.command("DoorScheduleResolver")

    def get_door_details(self):
        return self.command("GetDoorDetails")

    def classifier(self):
        return self.command("DoorFrameClassifier")

    # ── Data helpers ──────────────────────────────────────

    def dump_with_data(self):
        """Run /dump and read the saved data file. Returns (response, data_dict)."""
        resp = self.dump()
        data_file = resp.get("data", {}).get("dataFile", "")
        if data_file:
            with open(data_file) as f:
                return resp, json.load(f)
        return resp, None

    def door_ids(self, data, limit=None):
        """Extract door element IDs from dump data."""
        ids = []
        families = data.get("categories", {}).get("Doors", {}).get("families", {})
        for fam_data in families.values():
            for type_data in fam_data.values():
                if isinstance(type_data, dict) and "instances" in type_data:
                    for inst in type_data["instances"]:
                        ids.append(inst["id"])
                        if limit and len(ids) >= limit:
                            return ids
        return ids

    def schedule_count(self, data):
        """Count schedules in dump data."""
        return len(data.get("schedules", []))
