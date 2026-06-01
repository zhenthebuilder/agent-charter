"""Score both arms with the official IFEval evaluation library.

Uses the vendored, unmodified google-research `instruction_following_eval`
code (Apache 2.0) for all accuracy numbers. Writes:

  results/eval_results_{strict,loose}_{control,treatment}.jsonl   per-example
  results/results.json                                            aggregate

Run with the eval virtualenv (needs nltk, langdetect, absl-py, immutabledict):

    .venv-eval/bin/python score.py
"""

from __future__ import annotations

import json
import os
import sys

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BENCH_DIR, "vendor"))

from instruction_following_eval import evaluation_lib  # noqa: E402

DATA = os.path.join(BENCH_DIR, "data", "input_data.jsonl")
LOGS = os.path.join(BENCH_DIR, "logs")
RESULTS = os.path.join(BENCH_DIR, "results")

ARMS = {
    "control": os.path.join(LOGS, "control_responses.jsonl"),
    "treatment": os.path.join(LOGS, "treatment_responses.jsonl"),
}


def score_arm(arm: str, response_path: str, inputs) -> dict:
    prompt_to_response = evaluation_lib.read_prompt_to_response_dict(response_path)
    have = [i for i in inputs if i.prompt in prompt_to_response]
    missing = len(inputs) - len(have)
    out = {"n_prompts_scored": len(have), "n_prompts_missing": missing}

    for label, func in [
        ("strict", evaluation_lib.test_instruction_following_strict),
        ("loose", evaluation_lib.test_instruction_following_loose),
    ]:
        outputs = [func(inp, prompt_to_response) for inp in have]
        prompt_acc = sum(o.follow_all_instructions for o in outputs) / len(outputs)
        all_flags = [flag for o in outputs for flag in o.follow_instruction_list]
        instr_acc = sum(all_flags) / len(all_flags)
        out[f"prompt_level_{label}_accuracy"] = round(prompt_acc, 6)
        out[f"instruction_level_{label}_accuracy"] = round(instr_acc, 6)
        out[f"n_instructions_{label}"] = len(all_flags)

        per_example_path = os.path.join(RESULTS, f"eval_results_{label}_{arm}.jsonl")
        evaluation_lib.write_outputs(per_example_path, outputs)
    return out


def main() -> int:
    os.makedirs(RESULTS, exist_ok=True)
    inputs = evaluation_lib.read_prompt_list(DATA)
    results = {"benchmark": "IFEval (Zhou et al., 2023)", "n_prompts_total": len(inputs)}
    for arm, path in ARMS.items():
        if not os.path.isfile(path):
            print(f"missing responses for arm {arm}: {path}", file=sys.stderr)
            return 2
        results[arm] = score_arm(arm, path, inputs)
        print(f"{arm}: prompt-strict={results[arm]['prompt_level_strict_accuracy']:.4f} "
              f"instr-strict={results[arm]['instruction_level_strict_accuracy']:.4f} "
              f"prompt-loose={results[arm]['prompt_level_loose_accuracy']:.4f} "
              f"(n={results[arm]['n_prompts_scored']}, missing={results[arm]['n_prompts_missing']})")

    with open(os.path.join(RESULTS, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    print(f"wrote {os.path.join(RESULTS, 'results.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
