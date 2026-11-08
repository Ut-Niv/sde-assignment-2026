"""Persistent state for crash recovery and audit.

Uses a simple SQLite DB so the controller can be killed and restarted without
losing pending approvals, learned stats, or the decision log.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional


class Store:
    def __init__(self, path: str | Path = "controller_state.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init(self) -> None:
        with self._tx() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_approvals (
                    workflow_id TEXT PRIMARY KEY,
                    approval_id TEXT NOT NULL,
                    proposal_id TEXT NOT NULL,
                    base_version INTEGER NOT NULL,
                    proposed_config TEXT NOT NULL,
                    created_ts TEXT,
                    purpose TEXT
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS call_stats (
                    workflow_id TEXT NOT NULL,
                    cohort_id TEXT NOT NULL,
                    dim TEXT NOT NULL,
                    value TEXT NOT NULL,
                    dials INTEGER NOT NULL DEFAULT 0,
                    connected INTEGER NOT NULL DEFAULT 0,
                    engaged INTEGER NOT NULL DEFAULT 0,
                    hallucinations INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (workflow_id, cohort_id, dim, value)
                );
                CREATE TABLE IF NOT EXISTS hourly_stats (
                    workflow_id TEXT NOT NULL,
                    hour INTEGER NOT NULL,
                    dials INTEGER NOT NULL DEFAULT 0,
                    connected INTEGER NOT NULL DEFAULT 0,
                    engaged INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (workflow_id, hour)
                );
                CREATE TABLE IF NOT EXISTS caller_id_health (
                    workflow_id TEXT NOT NULL,
                    caller_id TEXT NOT NULL,
                    dials INTEGER NOT NULL DEFAULT 0,
                    connected INTEGER NOT NULL DEFAULT 0,
                    recent_pickup REAL,
                    PRIMARY KEY (workflow_id, caller_id)
                );
                CREATE TABLE IF NOT EXISTS applied_versions (
                    workflow_id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    config TEXT NOT NULL,
                    applied_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_decisions_wf ON decisions(workflow_id);
                """
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._tx() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if row is None:
                return default
            return json.loads(row["value"])

    def set_meta(self, key: str, value: Any) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )

    def set_pending(
        self,
        workflow_id: str,
        approval_id: str,
        proposal_id: str,
        base_version: int,
        proposed_config: dict,
        created_ts: str,
        purpose: str,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO pending_approvals
                   (workflow_id, approval_id, proposal_id, base_version, proposed_config, created_ts, purpose)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    workflow_id,
                    approval_id,
                    proposal_id,
                    base_version,
                    json.dumps(proposed_config),
                    created_ts,
                    purpose,
                ),
            )

    def get_pending(self, workflow_id: str) -> Optional[dict]:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM pending_approvals WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            if not row:
                return None
            return {
                "approval_id": row["approval_id"],
                "proposal_id": row["proposal_id"],
                "base_version": row["base_version"],
                "proposed_config": json.loads(row["proposed_config"]),
                "created_ts": row["created_ts"],
                "purpose": row["purpose"],
            }

    def clear_pending(self, workflow_id: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM pending_approvals WHERE workflow_id=?", (workflow_id,))

    def all_pending(self) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute("SELECT * FROM pending_approvals").fetchall()
            return [
                {
                    "workflow_id": r["workflow_id"],
                    "approval_id": r["approval_id"],
                    "proposal_id": r["proposal_id"],
                    "base_version": r["base_version"],
                    "proposed_config": json.loads(r["proposed_config"]),
                    "created_ts": r["created_ts"],
                    "purpose": r["purpose"],
                }
                for r in rows
            ]

    def log_decision(self, ts: str, workflow_id: str, kind: str, detail: dict) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO decisions(ts, workflow_id, kind, detail) VALUES (?,?,?,?)",
                (ts, workflow_id, kind, json.dumps(detail)),
            )

    def query_decisions(
        self, workflow_id: Optional[str] = None, limit: int = 50
    ) -> list[dict]:
        with self._tx() as conn:
            if workflow_id:
                rows = conn.execute(
                    "SELECT * FROM decisions WHERE workflow_id=? ORDER BY id DESC LIMIT ?",
                    (workflow_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            return [
                {
                    "id": r["id"],
                    "ts": r["ts"],
                    "workflow_id": r["workflow_id"],
                    "kind": r["kind"],
                    "detail": json.loads(r["detail"]),
                }
                for r in rows
            ]

    def record_calls_batch(self, calls: list[dict]) -> None:
        """calls: list of dicts with keys workflow_id, cohort_id, variant, connected, engaged, hallucination, hour"""
        if not calls:
            return
        stats: dict[tuple, list] = {}
        hourly: dict[tuple, list] = {}
        cid_h: dict[tuple, list] = {}

        for c in calls:
            wf = c["workflow_id"]
            cohort = c.get("cohort_id") or "default"
            variant = c.get("variant") or {}
            connected = 1 if c.get("connected") else 0
            engaged = 1 if c.get("engaged") else 0
            hall = 1 if c.get("hallucination") else 0
            hour = c.get("hour", 0)
            for dim in ("welcome_message", "voice", "caller_id", "prompt_version"):
                val = variant.get(dim)
                if val is None:
                    continue
                for co in (cohort, "*"):
                    key = (wf, co, dim, val)
                    if key not in stats:
                        stats[key] = [0, 0, 0, 0]
                    stats[key][0] += 1
                    stats[key][1] += connected
                    stats[key][2] += engaged
                    stats[key][3] += hall
            hk = (wf, hour)
            if hk not in hourly:
                hourly[hk] = [0, 0, 0]
            hourly[hk][0] += 1
            hourly[hk][1] += connected
            hourly[hk][2] += engaged
            cid = variant.get("caller_id")
            if cid:
                ck = (wf, cid)
                if ck not in cid_h:
                    cid_h[ck] = [0, 0]
                cid_h[ck][0] += 1
                cid_h[ck][1] += connected

        with self._tx() as conn:
            for (wf, co, dim, val), (d, cn, e, h) in stats.items():
                conn.execute(
                    """INSERT INTO call_stats(workflow_id, cohort_id, dim, value, dials, connected, engaged, hallucinations)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(workflow_id, cohort_id, dim, value) DO UPDATE SET
                         dials = dials + excluded.dials,
                         connected = connected + excluded.connected,
                         engaged = engaged + excluded.engaged,
                         hallucinations = hallucinations + excluded.hallucinations
                    """,
                    (wf, co, dim, val, d, cn, e, h),
                )
            for (wf, hour), (d, cn, e) in hourly.items():
                conn.execute(
                    """INSERT INTO hourly_stats(workflow_id, hour, dials, connected, engaged)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(workflow_id, hour) DO UPDATE SET
                         dials = dials + excluded.dials,
                         connected = connected + excluded.connected,
                         engaged = engaged + excluded.engaged
                    """,
                    (wf, hour, d, cn, e),
                )
            for (wf, cid), (d, cn) in cid_h.items():
                conn.execute(
                    """INSERT INTO caller_id_health(workflow_id, caller_id, dials, connected)
                       VALUES (?,?,?,?)
                       ON CONFLICT(workflow_id, caller_id) DO UPDATE SET
                         dials = dials + excluded.dials,
                         connected = connected + excluded.connected
                    """,
                    (wf, cid, d, cn),
                )

    def record_call(
        self,
        workflow_id: str,
        cohort_id: str,
        variant: dict,
        connected: bool,
        engaged: bool,
        hallucination: bool,
        hour: int,
    ) -> None:
        self.record_calls_batch(
            [
                {
                    "workflow_id": workflow_id,
                    "cohort_id": cohort_id,
                    "variant": variant,
                    "connected": connected,
                    "engaged": engaged,
                    "hallucination": hallucination,
                    "hour": hour,
                }
            ]
        )

    def get_dim_stats(
        self, workflow_id: str, dim: str, cohort_id: str = "*"
    ) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute(
                """SELECT value, dials, connected, engaged, hallucinations
                   FROM call_stats WHERE workflow_id=? AND cohort_id=? AND dim=?""",
                (workflow_id, cohort_id, dim),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_caller_health(self, workflow_id: str) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM caller_id_health WHERE workflow_id=?", (workflow_id,)
            ).fetchall()
            out = []
            for r in rows:
                dials = r["dials"] or 1
                out.append(
                    {
                        "caller_id": r["caller_id"],
                        "dials": dials,
                        "connected": r["connected"],
                        "pickup": r["connected"] / dials,
                    }
                )
            return out

    def get_hourly(self, workflow_id: str) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM hourly_stats WHERE workflow_id=? ORDER BY hour",
                (workflow_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def set_applied(self, workflow_id: str, version: int, config: dict, applied_at: str) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO applied_versions(workflow_id, version, config, applied_at)
                   VALUES (?,?,?,?)""",
                (workflow_id, version, json.dumps(config), applied_at),
            )

    def get_applied(self, workflow_id: str) -> Optional[dict]:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM applied_versions WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            if not row:
                return None
            return {
                "version": row["version"],
                "config": json.loads(row["config"]),
                "applied_at": row["applied_at"],
            }
