# IFEval A/B analysis summary

- Paired prompts: 541
- Prompt-level strict accuracy: control 0.8706 -> treatment 0.9390 (delta +0.0684, 95% CI [+0.0462, +0.0906], McNemar p = 7.8e-10)
- Prompt-level loose accuracy: control 0.9039 -> treatment 0.9538 (delta +0.0499)
- Discordant pairs: treatment-only pass 39, control-only pass 2

## Instruction-level strict accuracy by Charter coverage class

| coverage | n | control | treatment | delta |
|---|---|---|---|---|
| all | 834 | 0.9101 | 0.9604 | +0.0504 |
| approx | 288 | 0.8958 | 0.9757 | +0.0799 |
| exact | 451 | 0.9424 | 0.9845 | +0.0421 |
| uncovered | 95 | 0.8000 | 0.8000 | +0.0000 |

## Gate dynamics

- Prompts failing at least one Charter check initially: 61
- Prompts still failing a Charter check after the gate loop: 22
- Prompts where the gate selected a revised response: 61

## Cost

- Control model calls: 541
- Extra model calls for the treatment (revisions): 90 (0.166 per prompt on average)
- Mean response length (chars): control 1537, treatment 1546
