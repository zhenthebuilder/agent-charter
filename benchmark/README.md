# IFEval A/B: single-shot vs. Charter gate-and-revise

This directory contains everything needed to reproduce the benchmark result in
the paper: data, harness, contract compiler, the official scorer, raw logs of
the actual run, and the analysis.

## What is being compared

| arm | pipeline |
|---|---|
| **control** | one model call per IFEval prompt; response used as-is |
| **treatment** | starts from the *same* control response (exact pairing); the prompt's verifiable instructions are compiled into a Charter contract (`compile_contract.py`); the real `charter check` CLI gates the response; on failure, the structured failure report is sent back to the model for revision (≤2 revisions); the gate keeps the attempt with the fewest failed checks |

Model for both arms: `claude-haiku-4-5-20251001`, called through the Claude
Code CLI in print mode with a minimal assistant system prompt and no tools.
Scoring for both arms: the **official, unmodified** google-research
`instruction_following_eval` library (vendored under `vendor/`, Apache 2.0).

The treatment's checks intentionally overlap with what IFEval scores — that is
the point being measured (machine-checkable acceptance criteria + gating). The
analysis therefore also reports *coverage*: which instruction types Charter's
generic primitives can express exactly, approximately, or not at all, and the
accuracy split by class.

## Files

```
data/input_data.jsonl        IFEval prompts (downloaded from google-research)
vendor/                      official IFEval scorer, unmodified, + license
compile_contract.py          IFEval instruction -> Charter checks (+ coverage class)
harness.py                   runs both arms, resumable, writes raw logs
score.py                     official scoring for both arms -> results/results.json
analyze.py                   paired stats, coverage breakdown, cost -> results/analysis.json
logs/                        raw logs of the actual run (every model call, every check)
results/                     scored results of the actual run
```

## Reproduce

```bash
# 1. environment for the official scorer (the harness itself is stdlib-only)
uv venv .venv-eval --python 3.12
uv pip install --python .venv-eval/bin/python absl-py langdetect nltk immutabledict
.venv-eval/bin/python -c "import nltk; nltk.download('punkt'); nltk.download('punkt_tab')"

# 2. run both arms (requires the Claude Code CLI to be authenticated)
python3 harness.py --workers 6            # full 541 prompts; resumable
python3 harness.py --limit 8              # or a quick pilot

# 3. score with the official library and analyze
.venv-eval/bin/python score.py
python3 analyze.py
```

`harness.py` writes every model input/output and every Charter check verdict to
`logs/*.jsonl`; nothing in `results/` is hand-edited. Re-running `score.py` and
`analyze.py` on the committed logs reproduces the numbers in the paper exactly.

## Notes and caveats

- IFEval is a single-turn instruction-following benchmark, not a long-horizon
  agent benchmark; it is used to anchor the claim because it is public,
  recognized, and has verifiable per-output acceptance criteria. See the
  paper's Limitations section.
- The treatment reuses the control response as its first attempt. This removes
  initial-sampling noise from the comparison and isolates exactly the effect of
  the gate-and-revise step; it also means the treatment can only differ from
  control on prompts where a Charter check failed.
- Coverage classes are assigned in `compile_contract.py` and recorded per
  instruction instance in `logs/treatment_raw.jsonl`.
