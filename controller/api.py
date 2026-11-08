"""Thin HTTP client for the simulator."""
from __future__ import annotations

import time
from typing import Any, Optional

import httpx


class SimClient:
    def __init__(self, base_url: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self.client.close()

    def _req(self, method: str, path: str, **kwargs) -> httpx.Response:
        for attempt in range(5):
            try:
                r = self.client.request(method, path, **kwargs)
                if r.status_code in (429, 502, 503, 504):
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return r
            except (httpx.TransportError, httpx.TimeoutException):
                time.sleep(0.5 * (attempt + 1))
        return self.client.request(method, path, **kwargs)

    def get_json(self, path: str, **params) -> Any:
        r = self._req("GET", path, params=params)
        r.raise_for_status()
        return r.json()

    def post_json(self, path: str, body: Optional[dict] = None) -> tuple[int, Any]:
        r = self._req("POST", path, json=body or {})
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}
        return r.status_code, data

    def put_json(self, path: str, body: dict) -> tuple[int, Any]:
        r = self._req("PUT", path, json=body)
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}
        return r.status_code, data

    def state(self) -> dict:
        return self.get_json("/sim/state")

    def workflows(self) -> list[dict]:
        return self.get_json("/workflows")

    def workflow(self, wid: str) -> dict:
        return self.get_json(f"/workflows/{wid}")

    def config(self, wid: str) -> dict:
        return self.get_json(f"/workflows/{wid}/config")

    def advance(self, minutes: int = 15) -> dict:
        code, data = self.post_json("/sim/advance", {"minutes": minutes})
        if code == 409:
            return {"finished": True, "new_chunks": [], "ts": data.get("ts")}
        if code >= 400:
            raise RuntimeError(f"advance failed {code}: {data}")
        return data

    def events_chunk(self, n: int) -> list[dict]:
        r = self._req("GET", f"/events/chunks/{n}")
        r.raise_for_status()
        lines = r.text.strip().splitlines()
        import json

        return [json.loads(line) for line in lines if line.strip()]

    def list_chunks(self, kind: str = "events", after: int = -1) -> list[dict]:
        return self.get_json("/events/chunks", kind=kind, after=after)

    def submit_approval(self, body: dict) -> tuple[int, Any]:
        return self.post_json("/approvals", body)

    def get_approval(self, aid: str) -> dict:
        return self.get_json(f"/approvals/{aid}")

    def apply_approval(self, wid: str, approval_id: str) -> tuple[int, Any]:
        return self.post_json(f"/workflows/{wid}/apply", {"approval_id": approval_id})

    def auto_apply(self, wid: str, body: dict) -> tuple[int, Any]:
        return self.put_json(f"/workflows/{wid}/config", body)

    def suggest_fixes(self, workflow_id: str, problem: str) -> tuple[int, Any]:
        return self.post_json(
            "/llm/suggest-fixes", {"workflow_id": workflow_id, "problem": problem}
        )

    def validation_run(
        self, workflow_id: str, prompt_version: str, n: int = 200
    ) -> tuple[int, Any]:
        return self.post_json(
            "/validation/runs",
            {"workflow_id": workflow_id, "prompt_version": prompt_version, "n": n},
        )
