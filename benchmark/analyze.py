"""Paired analysis of the IFEval A/B run: effect sizes, significance, coverage, cost.

Reads the per-example outputs of the official scorer (score.py) plus the raw
treatment logs (gate dynamics, coverage classes, revision counts) and writes
results/analysis.json and results/summary.md. Pure stdlib.

Run:  python3 analyze.py
"""

from __future__ import annotations

import json
import math
import os
import random
import statistics
import sys

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(BENCH_DIR, "results")
LOGS = os.path.join(BENCH_DIR, "logs")


def read_jsonl(path: str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar test on discordant pair counts (b, c)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    # two-sided binomial test with p = 0.5
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def bootstrap_ci(pairs: list[tuple[int, int]], iters: int = 10000, seed: int = 7) -> tuple[float, float]:
    """95% bootstrap CI for the difference in prompt-level accuracy (treatment - control)."""
    rng = random.Random(seed)
    n = len(pairs)
    deltas = []
    for _ in range(iters):
        sample = [pairs[rng.randrange(n)] for _ in range(n)]
        c_acc = sum(p[0] for p in sample) / n
        t_acc = sum(p[1] for p in sample) / n
        deltas.append(t_acc - c_acc)
    deltas.sort()
    return deltas[int(0.025 * iters)], deltas[int(0.975 * iters)]


def main() -> int:
    strict_control = {r["prompt"]: r for r in read_jsonl(os.path.join(RESULTS, "eval_results_strict_control.jsonl"))}
    strict_treatment = {r["prompt"]: r for r in read_jsonl(os.path.join(RESULTS, "eval_results_strict_treatment.jsonl"))}
    loose_control = {r["prompt"]: r for r in read_jsonl(os.path.join(RESULTS, "eval_results_loose_control.jsonl"))}
    loose_treatment = {r["prompt"]: r for r in read_jsonl(os.path.join(RESULTS, "eval_results_loose_treatment.jsonl"))}
    treatment_raw = read_jsonl(os.path.join(LOGS, "treatment_raw.jsonl"))
    control_raw = read_jsonl(os.path.join(LOGS, "control_raw.jsonl"))

    prompts = [p for p in strict_control if p in strict_treatment]
    n = len(prompts)

    # ----- prompt-level paired comparison (strict) -------------------------
    pairs = [(int(strict_control[p]["follow_all_instructions"]),
              int(strict_treatment[p]["follow_all_instructions"])) for p in prompts]
    c_acc = sum(p[0] for p in pairs) / n
    t_acc = sum(p[1] for p in pairs) / n
    b = sum(1 for cc, tt in pairs if cc == 1 and tt == 0)   # control pass, treatment fail
    c_disc = sum(1 for cc, tt in pairs if cc == 0 and tt == 1)  # control fail, treatment pass
    p_value = mcnemar_exact(b, c_disc)
    ci_lo, ci_hi = bootstrap_ci(pairs)

    loose_pairs = [(int(loose_control[p]["follow_all_instructions"]),
                    int(loose_treatment[p]["follow_all_instructions"])) for p in prompts]
    lc_acc = sum(p[0] for p in loose_pairs) / n
    lt_acc = sum(p[1] for p in loose_pairs) / n

    # ----- instruction-level, overall and by coverage class ----------------
    coverage_by_prompt = {}
    for rec in treatment_raw:
        coverage_by_prompt[rec["prompt"]] = {i["index"]: i["coverage"] for i in rec["coverage"]["instructions"]}

    instr = {"control": {"all": [0, 0]}, "treatment": {"all": [0, 0]}}  # class -> [followed, total]
    for arm, table in (("control", strict_control), ("treatment", strict_treatment)):
        for p in prompts:
            rec = table[p]
            classes = coverage_by_prompt.get(p, {})
            for idx, followed in enumerate(rec["follow_instruction_list"]):
                klass = classes.get(idx, "unknown")
                for key in ("all", klass):
                    bucket = instr[arm].setdefault(key, [0, 0])
                    bucket[0] += int(followed)
                    bucket[1] += 1

    instruction_level = {}
    for klass in sorted({k for arm in instr.values() for k in arm}):
        c_f, c_t = instr["control"].get(klass, [0, 0])
        t_f, t_t = instr["treatment"].get(klass, [0, 0])
        instruction_level[klass] = {
            "n_instances": c_t,
            "control_accuracy": round(c_f / c_t, 6) if c_t else None,
            "treatment_accuracy": round(t_f / t_t, 6) if t_t else None,
            "delta": round((t_f / t_t) - (c_f / c_t), 6) if c_t and t_t else None,
        }

    # ----- gate dynamics and cost ------------------------------------------
    n_revisions = [r["n_revision_calls"] for r in treatment_raw]
    initial_fail = sum(1 for r in treatment_raw if r["attempts"][0]["n_failed_checks"] > 0)
    final_fail = sum(1 for r in treatment_raw if min(a["n_failed_checks"] for a in r["attempts"]) > 0)
    selected_not_first = sum(1 for r in treatment_raw if r["selected_attempt"] != 0)
    control_chars = [len(r["response"]) for r in control_raw]
    treatment_chars = [len(r["final_response"]) for r in treatment_raw]
    control_secs = [r["duration_s"] for r in control_raw]

    cost = {
        "n_prompts": n,
        "control_model_calls": len(control_raw),
        "treatment_extra_model_calls": sum(n_revisions),
        "treatment_total_model_calls_including_shared_initial": len(control_raw) + sum(n_revisions),
        "prompts_with_at_least_one_revision": sum(1 for x in n_revisions if x > 0),
        "mean_revisions_per_prompt": round(statistics.mean(n_revisions), 4),
        "max_revisions_per_prompt": max(n_revisions),
        "mean_response_chars_control": round(statistics.mean(control_chars), 1),
        "mean_response_chars_treatment": round(statistics.mean(treatment_chars), 1),
        "mean_control_call_seconds": round(statistics.mean(control_secs), 2),
    }
    gate = {
        "prompts_failing_charter_initially": initial_fail,
        "prompts_failing_charter_after_gate": final_fail,
        "prompts_where_gate_selected_a_revision": selected_not_first,
    }

    analysis = {
        "n_prompts_paired": n,
        "prompt_level_strict": {
            "control_accuracy": round(c_acc, 6),
            "treatment_accuracy": round(t_acc, 6),
            "delta": round(t_acc - c_acc, 6),
            "bootstrap_95ci_delta": [round(ci_lo, 6), round(ci_hi, 6)],
            "mcnemar_discordant_control_only_pass": b,
            "mcnemar_discordant_treatment_only_pass": c_disc,
            "mcnemar_exact_p_value": float(f"{p_value:.3g}"),
        },
        "prompt_level_loose": {
            "control_accuracy": round(lc_acc, 6),
            "treatment_accuracy": round(lt_acc, 6),
            "delta": round(lt_acc - lc_acc, 6),
        },
        "instruction_level_strict_by_coverage": instruction_level,
        "gate_dynamics": gate,
        "cost": cost,
    }

    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, "analysis.json"), "w", encoding="utf-8") as fh:
        json.dump(analysis, fh, indent=2)

    # ----- human-readable summary ------------------------------------------
    lines = [
        "# IFEval A/B analysis summary",
        "",
        f"- Paired prompts: {n}",
        f"- Prompt-level strict accuracy: control {c_acc:.4f} -> treatment {t_acc:.4f} "
        f"(delta {t_acc - c_acc:+.4f}, 95% CI [{ci_lo:+.4f}, {ci_hi:+.4f}], McNemar p = {p_value:.2g})",
        f"- Prompt-level loose accuracy: control {lc_acc:.4f} -> treatment {lt_acc:.4f} (delta {lt_acc - lc_acc:+.4f})",
        f"- Discordant pairs: treatment-only pass {c_disc}, control-only pass {b}",
        "",
        "## Instruction-level strict accuracy by Charter coverage class",
        "",
        "| coverage | n | control | treatment | delta |",
        "|---|---|---|---|---|",
    ]
    for klass, row in instruction_level.items():
        lines.append(f"| {klass} | {row['n_instances']} | {row['control_accuracy']:.4f} | "
                     f"{row['treatment_accuracy']:.4f} | {row['delta']:+.4f} |")
    lines += [
        "",
        "## Gate dynamics",
        "",
        f"- Prompts failing at least one Charter check initially: {initial_fail}",
        f"- Prompts still failing a Charter check after the gate loop: {final_fail}",
        f"- Prompts where the gate selected a revised response: {selected_not_first}",
        "",
        "## Cost",
        "",
        f"- Control model calls: {cost['control_model_calls']}",
        f"- Extra model calls for the treatment (revisions): {cost['treatment_extra_model_calls']} "
        f"({cost['mean_revisions_per_prompt']:.3f} per prompt on average)",
        f"- Mean response length (chars): control {cost['mean_response_chars_control']:.0f}, "
        f"treatment {cost['mean_response_chars_treatment']:.0f}",
    ]
    with open(os.path.join(RESULTS, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
