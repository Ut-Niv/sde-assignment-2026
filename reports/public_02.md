# Grade report — public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.248 | +1.8% | 0.184 | +1.0% | 0.476 | +0.9% | 5% | 324 (310) |
| nw_kyc | 0.379 | -1.2% | 0.246 | -0.8% | 0.587 | -2.1% | -24% | 30 (38) |
| nw_renewal | 0.411 | -4.5% | 0.261 | -0.7% | 0.529 | -3.3% | -17% | 68 (58) |
| ALL | 0.330 | -1.6% | 0.223 | -0.1% | 0.521 | -1.7% | -7% | 422 (406) |

## Changes applied by the controller

6 applied — beneficial: 3, harmful: 3

- nw_renewal v2 (controller_approved) dims=['welcome_message', 'voice', 'schedule'] engageΔ=+0.041 didΔ=+0.000 → **beneficial**
- nw_kyc v2 (controller_approved) dims=['welcome_message', 'voice', 'schedule'] engageΔ=-0.019 didΔ=+0.000 → **harmful**
- ks_cancel v2 (controller_approved) dims=['welcome_message', 'voice', 'schedule'] engageΔ=+0.023 didΔ=+0.000 → **beneficial**
- ks_cancel v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.014 didΔ=+0.000 → **harmful**
- nw_renewal v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.025 didΔ=+0.000 → **harmful**
- nw_renewal v4 (controller_approved) dims=['welcome_message'] engageΔ=+0.109 didΔ=+0.000 → **beneficial**

## Governance

```
{
  "approval_requests": 6,
  "decisions": {
    "approved": 6,
    "expired": 0
  },
  "rejection_reasons": {},
  "applied_via_approval": 6,
  "auto_applied": 0,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 2,
    "nw_kyc": 1,
    "ks_cancel": 2
  }
}
```

## Incidents

```
[
  {
    "type": "did_flagged",
    "workflow": "ks_cancel",
    "at": "D2 10:00",
    "flagged_did_share_by_day": {
      "2": 0.502,
      "3": 0.501,
      "4": 0.498,
      "5": 0.501,
      "6": 0.49,
      "7": 0.5,
      "8": 0.497,
      "9": 0.498
    }
  },
  {
    "type": "supervisor_edit",
    "workflow": "ks_cancel",
    "at": "D3 11:30",
    "mttr_minutes": null,
    "fixed_by": null
  },
  {
    "type": "supervisor_edit",
    "workflow": "nw_renewal",
    "at": "D5 15:00"
  }
]
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0
