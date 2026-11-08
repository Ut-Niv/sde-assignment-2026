"""Main control loop: ingest → detect → experiment → propose → apply → audit."""
from __future__ import annotations

import copy
import logging
import math
import uuid
from datetime import datetime
from typing import Any, Optional

from controller.api import SimClient
from controller.store import Store

log = logging.getLogger("controller")

MIN_DIALS_FOR_RATE = 80
MIN_CONNECTED_FOR_ENGAGE = 40
HALLUCINATION_SPIKE = 0.08
CALLER_ID_DROP = 0.05
PROMOTE_REL_LIFT = 0.04
EVAL_EVERY_HOURS = 6


def _rate(num: int, den: int) -> float:
    return num / den if den else 0.0


def _wilson_lb(successes: int, n: int, z: float = 1.64) -> float:
    """Lower bound of Wilson score interval (approx 95% one-sided)."""
    if n == 0:
        return 0.0
    p = successes / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - spread) / denom


class Controller:
    def __init__(self, sim_url: str, state_path: str = "controller_state.db"):
        self.api = SimClient(sim_url)
        self.store = Store(state_path)
        self.workflows: dict[str, dict] = {}
        self.last_chunk: int = self.store.get_meta("last_chunk", -1)
        self.last_eval_hour: dict[str, int] = self.store.get_meta("last_eval_hour", {})
        self.incident_state: dict[str, dict] = self.store.get_meta("incident_state", {})
        self.credits_used = 0

    def bootstrap(self) -> None:
        wfs = self.api.workflows()
        self.workflows = {w["workflow_id"]: w for w in wfs}
        log.info("workflows: %s", list(self.workflows))

        try:
            hist = self.api.list_chunks(kind="history", after=self.last_chunk)
            for ch in hist:
                n = ch["chunk"]
                if n <= self.last_chunk:
                    continue
                events = self.api.events_chunk(n)
                self._ingest(events)
                self.last_chunk = n
            self.store.set_meta("last_chunk", self.last_chunk)
            log.info("bootstrapped history up to chunk %s", self.last_chunk)
        except Exception as e:
            log.warning("history bootstrap: %s", e)

        for p in self.store.all_pending():
            try:
                ap = self.api.get_approval(p["approval_id"])
                status = ap.get("status")
                if status == "approved":
                    code, data = self.api.apply_approval(p["workflow_id"], p["approval_id"])
                    log.info("reconcile apply %s -> %s %s", p["workflow_id"], code, data)
                    if code == 200:
                        self.store.log_decision(
                            ap.get("ts", ""),
                            p["workflow_id"],
                            "applied_on_restart",
                            {"approval_id": p["approval_id"], "purpose": p["purpose"]},
                        )
                    self.store.clear_pending(p["workflow_id"])
                elif status in ("rejected", "expired", "lapsed"):
                    self.store.log_decision(
                        ap.get("ts", ""),
                        p["workflow_id"],
                        f"terminal_{status}",
                        {"approval_id": p["approval_id"], "reasons": ap.get("reasons")},
                    )
                    self.store.clear_pending(p["workflow_id"])
            except Exception as e:
                log.warning("reconcile pending %s: %s", p, e)

    def run(self) -> None:
        self.bootstrap()
        tick = 15
        while True:
            st = self.api.advance(tick)
            if st.get("finished"):
                log.info("scenario finished")
                break
            ts = st.get("ts", "")
            for ch in st.get("new_chunks", []):
                n = ch["chunk"]
                if n <= self.last_chunk:
                    continue
                events = self.api.events_chunk(n)
                self._ingest(events)
                self.last_chunk = n
            self.store.set_meta("last_chunk", self.last_chunk)

            self._poll_approvals(ts)

            hour = self._hour_from_ts(ts)
            day_index = self._day_from_ts(ts)
            for wid in list(self.workflows):
                self._maybe_act(wid, ts, hour, day_index)

            self.store.set_meta("last_eval_hour", self.last_eval_hour)
            self.store.set_meta("incident_state", self.incident_state)

    def _ingest(self, events: list[dict]) -> None:
        batch: list[dict] = []
        for ev in events:
            et = ev.get("type")
            if et == "call":
                ts = ev.get("ts", "")
                hour = self._hour_from_ts(ts)
                qa = ev.get("qa") or {}
                batch.append(
                    {
                        "workflow_id": ev["workflow_id"],
                        "cohort_id": ev.get("cohort_id") or "default",
                        "variant": ev.get("variant") or {},
                        "connected": bool(ev.get("connected")),
                        "engaged": bool(ev.get("engaged")),
                        "hallucination": bool(qa.get("hallucination_flag")),
                        "hour": hour,
                    }
                )
            elif et == "config_changed":
                self.store.log_decision(
                    ev.get("ts", ""),
                    ev["workflow_id"],
                    "config_changed_external",
                    {
                        "version": ev.get("version"),
                        "actor": ev.get("actor"),
                        "changed_dims": ev.get("changed_dims"),
                        "proposal_id": ev.get("proposal_id"),
                        "approval_id": ev.get("approval_id"),
                    },
                )
                pending = self.store.get_pending(ev["workflow_id"])
                if pending and pending["base_version"] < (ev.get("version") or 0):
                    self.store.clear_pending(ev["workflow_id"])
            elif et == "approval_decided":
                self.store.log_decision(
                    ev.get("ts", ""),
                    ev.get("workflow_id", ""),
                    "approval_decided",
                    {
                        "approval_id": ev.get("approval_id"),
                        "status": ev.get("status"),
                        "reasons": ev.get("reasons"),
                    },
                )
        if batch:
            for i in range(0, len(batch), 5000):
                self.store.record_calls_batch(batch[i : i + 5000])

    def _maybe_act(self, wid: str, ts: str, hour: int, day_index: int) -> None:
        if self.store.get_pending(wid):
            return

        wf = self.workflows.get(wid) or self.api.workflow(wid)
        self.workflows[wid] = wf
        last = self.last_eval_hour.get(wid, -999)
        should_eval = (hour - last) >= EVAL_EVERY_HOURS or (hour == 9 and last < hour)
        if not should_eval and day_index > 0:
            if not self._check_incidents(wid, ts, wf):
                return
        else:
            self.last_eval_hour[wid] = hour
            if self._check_incidents(wid, ts, wf):
                return
            self._run_experiment_cycle(wid, ts, wf)

    def _check_incidents(self, wid: str, ts: str, wf: dict) -> bool:
        prompt_stats = self.store.get_dim_stats(wid, "prompt_version")
        for row in prompt_stats:
            connected = row["connected"]
            if connected < 50:
                continue
            h_rate = row["hallucinations"] / connected
            if h_rate >= HALLUCINATION_SPIKE:
                key = f"hallucination:{row['value']}"
                if self.incident_state.get(wid, {}).get(key) == "handling":
                    continue
                log.warning("%s hallucination spike on %s: %.2f", wid, row["value"], h_rate)
                self.incident_state.setdefault(wid, {})[key] = "handling"
                self.store.set_meta("incident_state", self.incident_state)
                return self._handle_hallucination(wid, ts, wf, row["value"], h_rate)

        health = self.store.get_caller_health(wid)
        healthy = [h for h in health if h["dials"] >= MIN_DIALS_FOR_RATE]
        if len(healthy) >= 2:
            best = max(healthy, key=lambda h: h["pickup"])
            for h in healthy:
                if h["caller_id"] == best["caller_id"]:
                    continue
                if best["pickup"] - h["pickup"] >= CALLER_ID_DROP and h["pickup"] < 0.22:
                    key = f"caller_id:{h['caller_id']}"
                    if self.incident_state.get(wid, {}).get(key) == "rotated":
                        continue
                    log.warning(
                        "%s caller_id %s degraded pickup=%.3f (best %s=%.3f)",
                        wid,
                        h["caller_id"],
                        h["pickup"],
                        best["caller_id"],
                        best["pickup"],
                    )
                    return self._rotate_caller_id(wid, ts, wf, h["caller_id"], best["caller_id"])
        return False

    def _handle_hallucination(
        self, wid: str, ts: str, wf: dict, bad_prompt: str, rate: float
    ) -> bool:
        state = self.api.state()
        if state.get("credits_remaining", 0) < 10:
            log.error("not enough credits for hallucination fix")
            return False

        code, suggestions = self.api.suggest_fixes(
            wid, f"High hallucination rate {rate:.2%} on prompt {bad_prompt}"
        )
        self.credits_used += 3
        if code != 200 or not suggestions:
            log.warning("suggest-fixes failed: %s %s", code, suggestions)
            return False

        alts = (
            suggestions
            if isinstance(suggestions, list)
            else suggestions.get("alternatives")
            or suggestions.get("prompts")
            or []
        )
        if not alts:
            if isinstance(suggestions, dict) and "prompt_versions" in suggestions:
                alts = suggestions["prompt_versions"]
            else:
                log.warning("no alternatives returned: %s", suggestions)
                return False

        best_pv = None
        best_pass = 0.0
        gov = wf.get("governance") or {}
        min_pass = gov.get("min_validation_pass_rate", 0.97)
        for alt in alts[:3]:
            pv = (
                alt
                if isinstance(alt, str)
                else alt.get("prompt_version") or alt.get("version") or alt.get("id")
            )
            if not pv or pv == bad_prompt:
                continue
            n = 150
            cost = math.ceil(n / 100)
            if state.get("credits_remaining", 0) < cost + 3:
                break
            code, val = self.api.validation_run(wid, pv, n=n)
            self.credits_used += cost
            if code != 200:
                continue
            pr = val.get("pass_rate", 0)
            log.info("validated %s pass_rate=%.3f", pv, pr)
            if pr >= min_pass and pr > best_pass:
                best_pass = pr
                best_pv = pv

        if not best_pv:
            log.warning("no safe prompt alternative found")
            return False

        cur = self.api.config(wid)
        cfg = copy.deepcopy(cur["config"])
        if cfg.get("prompt_version") == best_pv:
            return False
        cfg["prompt_version"] = best_pv

        return self._propose(
            wid,
            ts,
            cur["version"],
            cfg,
            change_summary=f"Replace prompt {bad_prompt} after hallucination spike ({rate:.1%})",
            diff={"prompt_version": {"from": bad_prompt, "to": best_pv}},
            affected_leads=self._estimate_affected(wid),
            dry_run={
                "validation_run": {"prompt": best_pv, "pass_rate": best_pass, "n": 150},
                "prior_hallucination_rate": rate,
            },
            impact_score=min(0.9, 0.4 + rate),
            rationale=(
                f"Connected calls on {bad_prompt} showed hallucination_flag rate {rate:.1%} "
                f"(threshold {HALLUCINATION_SPIKE:.0%}). Validated alternative {best_pv} "
                f"at pass_rate {best_pass:.3f} ≥ {min_pass}."
            ),
            evidence={"validation_run_id": None},
            purpose="hallucination_fix",
            force_approval=True,
        )

    def _rotate_caller_id(
        self, wid: str, ts: str, wf: dict, bad: str, good: str
    ) -> bool:
        cur = self.api.config(wid)
        cfg = copy.deepcopy(cur["config"])
        alloc = cfg.setdefault("allocations", {}).setdefault("default", {})
        cid = alloc.get("caller_id") or {}
        if bad not in cid and len(cid) <= 1:
            options = (wf.get("options") or {}).get("caller_id") or []
            new = {c: 1.0 for c in options if c != bad}
            if not new:
                return False
            alloc["caller_id"] = new
        else:
            new = {k: (0.0 if k == bad else max(v, 0.1)) for k, v in cid.items()}
            if bad in new and sum(v for k, v in new.items() if k != bad) == 0:
                options = (wf.get("options") or {}).get("caller_id") or []
                for c in options:
                    if c != bad:
                        new[c] = 1.0
            new.pop(bad, None)
            if not new:
                return False
            alloc["caller_id"] = new

        gov = wf.get("governance") or {}
        aa = gov.get("auto_apply") or {}
        if aa.get("enabled") and "caller_id" in (aa.get("allowed_dims") or []):
            body = {
                "proposal_id": str(uuid.uuid4()),
                "base_version": cur["version"],
                "proposed_config": cfg,
                "rationale": f"Rotate out degraded caller_id {bad}; keep {list(new)}",
            }
            code, data = self.api.auto_apply(wid, body)
            self.store.log_decision(
                ts,
                wid,
                "auto_apply_caller_id",
                {"code": code, "data": data, "bad": bad, "new": new},
            )
            if code == 200:
                self.incident_state.setdefault(wid, {})[f"caller_id:{bad}"] = "rotated"
                self.store.set_meta("incident_state", self.incident_state)
                return True

        return self._propose(
            wid,
            ts,
            cur["version"],
            cfg,
            change_summary=f"Rotate out degraded caller ID {bad}",
            diff={"allocations.default.caller_id": {"removed": bad, "remaining": new}},
            affected_leads=self._estimate_affected(wid),
            dry_run={"observed_pickup": self.store.get_caller_health(wid)},
            impact_score=0.55,
            rationale=f"Caller ID {bad} pickup significantly below peers; reallocating traffic.",
            purpose="caller_id_rotation",
        )

    def _run_experiment_cycle(self, wid: str, ts: str, wf: dict) -> None:
        primary = (wf.get("objective") or {}).get("primary", "engagement_rate")
        options = wf.get("options") or {}
        cur = self.api.config(wid)
        cfg = copy.deepcopy(cur["config"])
        changed = False
        diff: dict = {}

        voices = options.get("voice") or []
        if len(voices) >= 2:
            stats = self.store.get_dim_stats(wid, "voice")
            by_v = {r["value"]: r for r in stats}
            tried = [v for v in voices if by_v.get(v, {}).get("dials", 0) >= 30]
            if len(tried) < min(3, len(voices)):
                new_alloc = {v: 1.0 for v in voices}
                old = cfg.get("allocations", {}).get("default", {}).get("voice")
                if old != new_alloc:
                    cfg.setdefault("allocations", {}).setdefault("default", {})["voice"] = new_alloc
                    changed = True
                    diff["voice"] = {"action": "explore", "alloc": new_alloc}
            else:
                best_v, best_score = None, -1.0
                for v, r in by_v.items():
                    if r["dials"] < MIN_DIALS_FOR_RATE:
                        continue
                    if primary == "engagement_rate":
                        if r["connected"] < MIN_CONNECTED_FOR_ENGAGE:
                            continue
                        score = _wilson_lb(r["engaged"], r["connected"])
                    else:
                        score = _wilson_lb(r["connected"], r["dials"])
                    if score > best_score:
                        best_score = score
                        best_v = v
                if best_v:
                    rest = [v for v in voices if v != best_v]
                    new_alloc = {best_v: 0.7}
                    if rest:
                        share = 0.3 / len(rest)
                        for v in rest:
                            new_alloc[v] = share
                    old = cfg.get("allocations", {}).get("default", {}).get("voice")
                    if self._alloc_differs(old, new_alloc):
                        cfg.setdefault("allocations", {}).setdefault("default", {})["voice"] = new_alloc
                        changed = True
                        diff["voice"] = {"winner": best_v, "score": best_score, "alloc": new_alloc}

        welcomes = options.get("welcome_message") or []
        if len(welcomes) >= 2:
            stats = self.store.get_dim_stats(wid, "welcome_message")
            by_w = {r["value"]: r for r in stats}
            tried = [w for w in welcomes if by_w.get(w, {}).get("dials", 0) >= 30]
            if len(tried) < min(2, len(welcomes)):
                new_alloc = {w: 1.0 for w in welcomes}
                old = cfg.get("allocations", {}).get("default", {}).get("welcome_message")
                if old != new_alloc:
                    cfg.setdefault("allocations", {}).setdefault("default", {})[
                        "welcome_message"
                    ] = new_alloc
                    changed = True
                    diff["welcome_message"] = {"action": "explore", "alloc": new_alloc}
            else:
                best_w, best_score = None, -1.0
                for w, r in by_w.items():
                    if r["dials"] < MIN_DIALS_FOR_RATE:
                        continue
                    if primary == "engagement_rate":
                        if r["connected"] < MIN_CONNECTED_FOR_ENGAGE:
                            continue
                        score = _wilson_lb(r["engaged"], r["connected"])
                    else:
                        score = _wilson_lb(r["connected"], r["dials"])
                    if score > best_score:
                        best_score = score
                        best_w = w
                if best_w:
                    new_alloc = {best_w: 0.75}
                    rest = [w for w in welcomes if w != best_w]
                    if rest:
                        share = 0.25 / len(rest)
                        for w in rest:
                            new_alloc[w] = share
                    old = cfg.get("allocations", {}).get("default", {}).get("welcome_message")
                    if self._alloc_differs(old, new_alloc):
                        cfg.setdefault("allocations", {}).setdefault("default", {})[
                            "welcome_message"
                        ] = new_alloc
                        changed = True
                        diff["welcome_message"] = {
                            "winner": best_w,
                            "score": best_score,
                            "alloc": new_alloc,
                        }

        hourly = self.store.get_hourly(wid)
        if hourly and sum(h["dials"] for h in hourly) > 200:
            scores = {}
            for h in hourly:
                if h["dials"] < 20:
                    continue
                if primary == "engagement_rate":
                    scores[h["hour"]] = (
                        _rate(h["engaged"], h["connected"]) if h["connected"] else 0
                    )
                else:
                    scores[h["hour"]] = _rate(h["connected"], h["dials"])
            if scores:
                mx = max(scores.values()) or 1.0
                weights = {str(hr): max(0.25, (scores[hr] / mx) * 2) for hr in scores}
                window = wf.get("dialling_window") or [9, 21]
                for hr in range(window[0], window[1]):
                    weights.setdefault(str(hr), 0.5)
                old_sched = (cfg.get("schedule") or {}).get("hour_weights") or {}
                old_default = old_sched.get("default") if isinstance(old_sched, dict) else None
                if self._alloc_differs(old_default, weights):
                    cfg.setdefault("schedule", {})["hour_weights"] = {"default": weights}
                    changed = True
                    diff["schedule"] = {"hour_weights.default": weights}

        if not changed:
            return

        changed_dims = set()
        if "voice" in diff or "welcome_message" in diff:
            changed_dims.add("allocations")
        if "schedule" in diff:
            changed_dims.add("schedule")

        gov = wf.get("governance") or {}
        aa = gov.get("auto_apply") or {}
        only_auto_dims = changed_dims and changed_dims.issubset(
            set(aa.get("allowed_dims") or [])
        )
        if aa.get("enabled") and only_auto_dims and "allocations" not in changed_dims:
            body = {
                "proposal_id": str(uuid.uuid4()),
                "base_version": cur["version"],
                "proposed_config": cfg,
                "rationale": f"Schedule optimisation based on observed {primary}",
            }
            code, data = self.api.auto_apply(wid, body)
            self.store.log_decision(
                ts, wid, "auto_apply_schedule", {"code": code, "data": data, "diff": diff}
            )
            if code == 200:
                return

        self._propose(
            wid,
            ts,
            cur["version"],
            cfg,
            change_summary=f"Traffic allocation / schedule update for {primary}",
            diff=diff,
            affected_leads=self._estimate_affected(wid),
            dry_run={
                "voice_stats": self.store.get_dim_stats(wid, "voice"),
                "welcome_stats": self.store.get_dim_stats(wid, "welcome_message"),
                "hourly": hourly[:24] if hourly else [],
            },
            impact_score=0.35,
            rationale=(
                f"Reallocate traffic toward higher-{primary} variants using Wilson lower-bound "
                f"on observed call outcomes; keep residual exploration mass."
            ),
            purpose="experiment",
        )

    def _propose(
        self,
        wid: str,
        ts: str,
        base_version: int,
        proposed_config: dict,
        change_summary: str,
        diff: dict,
        affected_leads: int,
        dry_run: dict,
        impact_score: float,
        rationale: str,
        purpose: str,
        evidence: Optional[dict] = None,
        force_approval: bool = False,
    ) -> bool:
        proposal_id = str(uuid.uuid4())
        body: dict[str, Any] = {
            "proposal_id": proposal_id,
            "workflow_id": wid,
            "base_version": base_version,
            "proposed_config": proposed_config,
            "change_summary": change_summary,
            "diff": diff if diff else {"note": "see proposed_config"},
            "affected_leads": max(0, affected_leads),
            "dry_run": dry_run if dry_run else {"note": "historical rates used"},
            "impact_score": max(0.0, min(1.0, impact_score)),
            "rationale": rationale,
        }
        if evidence:
            body["evidence"] = evidence

        code, data = self.api.submit_approval(body)
        self.store.log_decision(
            ts,
            wid,
            "proposal_submitted",
            {
                "code": code,
                "proposal_id": proposal_id,
                "purpose": purpose,
                "response": data,
                "change_summary": change_summary,
            },
        )
        if code == 200:
            aid = data.get("approval_id")
            if aid:
                self.store.set_pending(
                    wid,
                    aid,
                    proposal_id,
                    base_version,
                    proposed_config,
                    ts,
                    purpose,
                )
                log.info("%s proposal accepted approval_id=%s purpose=%s", wid, aid, purpose)
                return True
        log.warning("%s proposal rejected by API: %s %s", wid, code, data)
        return False

    def _poll_approvals(self, ts: str) -> None:
        for p in self.store.all_pending():
            wid = p["workflow_id"]
            try:
                ap = self.api.get_approval(p["approval_id"])
            except Exception as e:
                log.warning("poll approval %s: %s", p["approval_id"], e)
                continue
            status = ap.get("status")
            if status == "approved":
                code, data = self.api.apply_approval(wid, p["approval_id"])
                self.store.log_decision(
                    ts,
                    wid,
                    "applied",
                    {
                        "approval_id": p["approval_id"],
                        "purpose": p["purpose"],
                        "code": code,
                        "data": data,
                    },
                )
                if code == 200:
                    log.info("%s applied approval %s", wid, p["approval_id"])
                    if p["purpose"] == "hallucination_fix":
                        for k in list(self.incident_state.get(wid, {})):
                            if k.startswith("hallucination:"):
                                self.incident_state[wid][k] = "fixed"
                    self.store.set_meta("incident_state", self.incident_state)
                elif code == 409:
                    log.warning("%s apply stale: %s", wid, data)
                self.store.clear_pending(wid)
            elif status in ("rejected", "expired", "lapsed"):
                self.store.log_decision(
                    ts,
                    wid,
                    f"terminal_{status}",
                    {
                        "approval_id": p["approval_id"],
                        "purpose": p["purpose"],
                        "reasons": ap.get("reasons"),
                    },
                )
                self.store.clear_pending(wid)
                log.info("%s approval %s -> %s", wid, p["approval_id"], status)

    def _estimate_affected(self, wid: str) -> int:
        wf = self.workflows.get(wid) or {}
        batch = (wf.get("daily_batch") or {}).get("size") or 500
        return int(batch * 2.5)

    def _alloc_differs(self, a: Optional[dict], b: Optional[dict], tol: float = 0.05) -> bool:
        if a is None and b is None:
            return False
        if a is None or b is None:
            return True
        keys = set(a) | set(b)
        for k in keys:
            if abs(float(a.get(k, 0)) - float(b.get(k, 0))) > tol:
                return True
        return False

    @staticmethod
    def _hour_from_ts(ts: str) -> int:
        try:
            return int(ts[11:13])
        except Exception:
            return 0

    @staticmethod
    def _day_from_ts(ts: str) -> int:
        try:
            d = datetime.fromisoformat(ts)
            return d.toordinal()
        except Exception:
            return 0
