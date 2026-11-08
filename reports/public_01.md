# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.368 | +7.6% | 0.243 | +1.6% | 0.527 | +5.4% | 40% | 58 (52) |
| nw_kyc | 0.387 | +2.8% | 0.240 | -1.0% | 0.629 | +2.7% | 31% | 19 (21) |
| nw_renewal | 0.429 | +2.1% | 0.260 | -0.7% | 0.569 | +4.0% | 20% | 43 (39) |
| ALL | 0.392 | +4.5% | 0.249 | +0.3% | 0.561 | +4.2% | 31% | 120 (112) |

## Changes applied by the controller

6 applied — beneficial: 5, neutral: 1

- nw_renewal v2 (controller_approved) dims=['welcome_message', 'voice', 'schedule'] engageΔ=+0.068 didΔ=+0.000 → **beneficial**
- nw_kyc v2 (controller_approved) dims=['welcome_message', 'voice', 'schedule'] engageΔ=+0.075 didΔ=+0.000 → **beneficial**
- ks_cancel v2 (controller_approved) dims=['welcome_message', 'voice', 'schedule'] engageΔ=-0.002 didΔ=+0.000 → **neutral**
- ks_cancel v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.021 didΔ=+0.000 → **beneficial**
- nw_renewal v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.046 didΔ=+0.000 → **beneficial**
- ks_cancel v4 (controller_approved) dims=['voice'] engageΔ=+0.051 didΔ=+0.000 → **beneficial**

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
    "ks_cancel": 3
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "3": 0.74,
    "4": 0.747,
    "5": 0.748,
    "6": 0.753,
    "7": 0.738
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0