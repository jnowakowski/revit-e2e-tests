"""HTTP client for revit-e2e-tests server.

All Revit interaction goes through the server.
Runner never touches pywinauto directly.
"""

import json
import time
import urllib.request
import urllib.error

SERVER = "http://127.0.0.1:8520"


class RevitAPI:
    def __init__(self, base_url=SERVER):
        self.base = base_url

    def _log(self, method, url, body=None):
        if body:
            print(f"  >> {method} {url}  body={json.dumps(body)}", flush=True)
        else:
            print(f"  >> GET {url}", flush=True)

    def _get(self, path, timeout=60):
        url = f"{self.base}{path}"
        self._log("GET", url)
        t0 = time.time()
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                result = json.loads(r.read())
                print(f"  << {time.time()-t0:.1f}s", flush=True)
                return result
        except urllib.error.HTTPError as e:
            print(f"  << {time.time()-t0:.1f}s HTTP {e.code}", flush=True)
            return json.loads(e.read())
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"  << {time.time()-t0:.1f}s TIMEOUT/ERROR: {e}", flush=True)
            return {"error": f"Server: {e}"}

    def _post(self, path, body, timeout=60):
        url = f"{self.base}{path}"
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data)
        req.add_header("Content-Type", "application/json")
        self._log("POST", url, body)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                result = json.loads(r.read())
                print(f"  << {time.time()-t0:.1f}s", flush=True)
                return result
        except urllib.error.HTTPError as e:
            print(f"  << {time.time()-t0:.1f}s HTTP {e.code}", flush=True)
            return json.loads(e.read())
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"  << {time.time()-t0:.1f}s TIMEOUT/ERROR: {e}", flush=True)
            return {"error": f"Server: {e}"}

    def health(self):
        return self._get("/health")

    def status(self):
        return self._get("/status")

    def connect(self):
        return self._post("/connect", {})

    def query(self, selector, depth=2, cache="auto"):
        path = f"/q?s={urllib.request.quote(selector)}&depth={depth}&cache={cache}"
        return self._get(path)

    def click(self, path=None, selector=None, method="invoke", depth=2):
        body = {"method": method}
        if path:
            body["path"] = path
        if selector:
            body["selector"] = selector
            body["depth"] = depth
        return self._post("/click", body)

    def tree(self, path=None, depth=1):
        qs = f"?depth={depth}"
        if path:
            qs += f"&path={path}"
        return self._get(f"/tree{qs}")

    def windows(self):
        return self._get("/windows")
