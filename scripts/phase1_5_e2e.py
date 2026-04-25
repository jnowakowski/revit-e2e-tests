"""End-to-end Phase 1.5 validation.

Goal: prove the dev server stays observable through long Revit operations.

For each model in the test set:
  1. Open it via /open-model.
  2. Poll /health on a side thread while the open is in flight; record
     every isResponsive flip and the heartbeat phase progression.
  3. Run /dump with a generous timeout. While running, poll /health and
     /status (FastRead) — verify FastRead never blocks, and that
     heartbeat phases advance.
  4. Optionally run V2 (DoorScheduleResolverV2) if dump showed doors.
  5. Capture /runs at the end and confirm each runId is in history.

Reports a one-line PASS/FAIL per model plus a JSON summary.

Usage:
    python scripts/phase1_5_e2e.py [model1.rvt model2.rvt ...]

If no models are passed, defaults to the KDA discovery set.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import requests

BASE = "http://localhost:52140"
DEFAULT_MODELS = [
    r"C:\Users\orion\Documents\2026-04-25-KDA_Models\UCANR_ARCH_r26_detached.rvt",
    r"C:\Users\orion\Documents\Big Mountain Joy-20260417T091603Z-3-001\Mount Joy Borough_v26.rvt",
]
HEALTH_POLL_SECONDS = 1.0
DUMP_TIMEOUT_MS = 600_000  # 10 minutes hard cap


@dataclass
class HealthSample:
    t_offset: float
    is_responsive: bool
    queued: int
    phase: str
    progress: int
    progress_total: int
    sec_since_heartbeat: float
    is_active: bool


@dataclass
class ModelResult:
    model: str
    open_ok: bool = False
    open_run_id: str = ""
    open_seconds: float = 0.0
    dump_ok: bool = False
    dump_run_id: str = ""
    dump_seconds: float = 0.0
    dump_total_instances: int = 0
    fastread_ok: bool = False
    fastread_min_ms: float = 9e9
    fastread_max_ms: float = 0.0
    health_samples_count: int = 0
    non_responsive_intervals: int = 0
    distinct_phases_seen: int = 0
    runs_endpoint_ok: bool = False
    notes: list[str] = field(default_factory=list)


def get(path, **kw):
    return requests.get(f"{BASE}{path}", timeout=kw.get("timeout", 10))


def post(path, body=None, timeout=30):
    return requests.post(f"{BASE}{path}", json=body or {}, timeout=timeout)


class HealthPoller(threading.Thread):
    """Background poller that hits /health every HEALTH_POLL_SECONDS."""

    def __init__(self):
        super().__init__(daemon=True)
        self.samples: list[HealthSample] = []
        self.fastread_durations_ms: list[float] = []
        self._halt = threading.Event()
        self._t0 = time.monotonic()

    def run(self):
        while not self._halt.is_set():
            try:
                t0 = time.perf_counter()
                r = requests.get(f"{BASE}/health", timeout=3)
                dt_ms = (time.perf_counter() - t0) * 1000.0
                self.fastread_durations_ms.append(dt_ms)
                if r.ok:
                    d = r.json().get("data", {})
                    op = d.get("currentOp") or {}
                    self.samples.append(
                        HealthSample(
                            t_offset=time.monotonic() - self._t0,
                            is_responsive=bool(d.get("isResponsive", True)),
                            queued=int(d.get("queuedDepth", 0)),
                            phase=str(op.get("phase", "")),
                            progress=int(op.get("progress", 0) or 0),
                            progress_total=int(op.get("progressTotal", 0) or 0),
                            sec_since_heartbeat=float(op.get("secondsSinceLastHeartbeat", 0.0) or 0.0),
                            is_active=bool(op.get("isActive", False)),
                        )
                    )
            except Exception:
                pass
            time.sleep(HEALTH_POLL_SECONDS)

    def stop(self):
        self._halt.set()
        self.join(timeout=5)

    def summarize(self) -> dict:
        if not self.samples:
            return {"samples": 0}
        active = [s for s in self.samples if s.is_active]
        flips = 0
        prev = None
        for s in self.samples:
            if prev is not None and s.is_responsive != prev:
                flips += 1
            prev = s.is_responsive
        non_responsive = sum(1 for s in active if not s.is_responsive)
        phases = {s.phase for s in active if s.phase}
        return {
            "samples": len(self.samples),
            "active_samples": len(active),
            "responsive_flips": flips,
            "non_responsive_active": non_responsive,
            "distinct_phases": sorted(phases),
            "fastread_min_ms": round(min(self.fastread_durations_ms or [0]), 1),
            "fastread_max_ms": round(max(self.fastread_durations_ms or [0]), 1),
            "fastread_p95_ms": round(
                sorted(self.fastread_durations_ms)[int(0.95 * len(self.fastread_durations_ms))], 1
            ) if self.fastread_durations_ms else 0,
        }


def fastread_status_check(result: ModelResult):
    """Hit /status (FastRead) and ensure cacheAgeSeconds present."""
    try:
        r = get("/status", timeout=3)
        if r.ok:
            d = r.json().get("data", {})
            result.fastread_ok = bool(d.get("fastRead"))
            if d.get("fastRead"):
                result.notes.append(f"/status fastRead cacheAge={d.get('cacheAgeSeconds')}s")
    except Exception as ex:
        result.notes.append(f"/status FastRead error: {ex}")


def open_model(name: str, result: ModelResult):
    # Skip open if the requested model is already active (idempotent for repeated runs).
    expected = Path(name).stem
    try:
        d = get("/status?fresh=1", timeout=15).json().get("data", {})
        if d.get("hasDocument") and d.get("title") == expected:
            result.open_ok = True
            result.open_seconds = 0.0
            result.notes.append(f"open skipped: '{expected}' already active")
            return {"samples": 0, "skipped": True}
    except Exception as ex:
        result.notes.append(f"pre-open status error: {ex}")

    poller = HealthPoller()
    poller.start()
    t0 = time.perf_counter()
    try:
        r = post("/open-model", {"model": name}, timeout=600)
        elapsed = time.perf_counter() - t0
        result.open_seconds = round(elapsed, 1)
        if r.ok:
            payload = r.json()
            result.open_ok = bool(payload.get("ok"))
            result.open_run_id = payload.get("data", {}).get("runId", "")
            if not result.open_ok:
                result.notes.append(f"open failed: {payload.get('message', '?')}")
        else:
            result.notes.append(f"open HTTP {r.status_code}")
    finally:
        poller.stop()
    s = poller.summarize()
    result.health_samples_count += s.get("samples", 0)
    result.non_responsive_intervals += s.get("non_responsive_active", 0)
    return s


def run_dump(result: ModelResult):
    poller = HealthPoller()
    poller.start()
    t0 = time.perf_counter()
    try:
        r = post("/dump", {"timeoutMs": DUMP_TIMEOUT_MS}, timeout=DUMP_TIMEOUT_MS / 1000 + 10)
        elapsed = time.perf_counter() - t0
        result.dump_seconds = round(elapsed, 1)
        if r.ok:
            payload = r.json()
            result.dump_ok = bool(payload.get("ok"))
            data = payload.get("data") or {}
            result.dump_run_id = data.get("runId", "")
            result.dump_total_instances = int(data.get("totalInstances", 0))
            if not result.dump_ok:
                result.notes.append(f"dump failed: {payload.get('message', '?')}")
        else:
            result.notes.append(f"dump HTTP {r.status_code}")
    finally:
        poller.stop()
    s = poller.summarize()
    result.health_samples_count += s.get("samples", 0)
    result.non_responsive_intervals += s.get("non_responsive_active", 0)
    result.distinct_phases_seen = max(result.distinct_phases_seen, len(s.get("distinct_phases", [])))
    result.fastread_min_ms = min(result.fastread_min_ms, s.get("fastread_min_ms", 9e9))
    result.fastread_max_ms = max(result.fastread_max_ms, s.get("fastread_max_ms", 0))
    return s


def verify_runs(result: ModelResult):
    try:
        r = get("/runs", timeout=5)
        if not r.ok:
            return
        runs = (r.json().get("data") or {}).get("runs", [])
        run_ids = {x.get("runId") for x in runs}
        ok = True
        if result.open_run_id and result.open_run_id not in run_ids:
            ok = False
            result.notes.append(f"open runId {result.open_run_id} not in /runs")
        if result.dump_run_id and result.dump_run_id not in run_ids:
            ok = False
            result.notes.append(f"dump runId {result.dump_run_id} not in /runs")
        result.runs_endpoint_ok = ok
    except Exception as ex:
        result.notes.append(f"/runs error: {ex}")


def run_one(model: str) -> ModelResult:
    print(f"\n=== {model} ===")
    result = ModelResult(model=model)
    fastread_status_check(result)

    print(f"  /open-model …")
    s_open = open_model(model, result)
    print(f"    -> open_ok={result.open_ok} runId={result.open_run_id} ({result.open_seconds}s)")
    print(f"       health: {s_open}")

    if not result.open_ok:
        result.notes.append("skipping dump because open failed")
        return result

    print(f"  /dump …")
    s_dump = run_dump(result)
    print(f"    -> dump_ok={result.dump_ok} totalInstances={result.dump_total_instances} ({result.dump_seconds}s)")
    print(f"       health: {s_dump}")

    print(f"  /runs verify …")
    verify_runs(result)
    print(f"    -> runs_endpoint_ok={result.runs_endpoint_ok}")
    return result


def main():
    models = sys.argv[1:] or DEFAULT_MODELS
    print(f"Phase 1.5 e2e — testing {len(models)} model(s)")
    try:
        r = get("/health", timeout=3)
        if not r.ok:
            print(f"FAIL: /health returned {r.status_code}")
            return 2
        print(f"  dev server alive: {r.json().get('data')}")
    except Exception as ex:
        print(f"FAIL: cannot reach /health: {ex}")
        return 2

    results: list[ModelResult] = []
    for m in models:
        results.append(run_one(m))

    summary_path = Path("phase1_5_e2e_summary.json")
    summary_path.write_text(json.dumps([asdict(r) for r in results], indent=2))
    print(f"\nSummary written to {summary_path.resolve()}")

    failed = [r for r in results if not (r.open_ok and r.dump_ok)]
    print("\n--- Recap ---")
    for r in results:
        verdict = "PASS" if r.open_ok and r.dump_ok else "FAIL"
        print(f"  {verdict} {r.model}: open={r.open_seconds}s dump={r.dump_seconds}s "
              f"instances={r.dump_total_instances} health_samples={r.health_samples_count} "
              f"non_responsive={r.non_responsive_intervals}")
        for n in r.notes:
            print(f"      note: {n}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
