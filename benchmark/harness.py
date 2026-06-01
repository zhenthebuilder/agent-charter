"""A/B harness: single-shot generation vs. Charter gate-and-revise on IFEval.

Arms
----
control    one model call per prompt; the response is used as-is.
treatment  starts from the *same* control response (exact pairing); the
           harness compiles the prompt's verifiable instructions into a
           Charter contract (compile_contract.py), runs the real `charter
           check` CLI on the response, and if any check fails, asks the model
           to revise given the structured failure report (up to
           --max-revisions). The gate keeps the attempt with the fewest
           failed checks.

The model is called through the Claude Code CLI in print mode with a minimal
"helpful assistant" system prompt and no tools; the identical stack is used
for both arms. Raw inputs/outputs of every call are written to logs/ as
JSONL; nothing is post-edited.

Usage:
    python3 harness.py --limit 8            # pilot
    python3 harness.py                      # full 541-prompt run
    python3 harness.py --workers 6          # tune concurrency
The harness is resumable: prompts already present in the raw logs are skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from compile_contract import compile_record

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
# charter.py lives in ../charter/ in the research workspace and at the repo
# root in the released repository; use whichever exists.
CHARTER_PY = next(
    p for p in (
        os.path.join(BENCH_DIR, "..", "charter", "charter.py"),
        os.path.join(BENCH_DIR, "..", "charter.py"),
    ) if os.path.isfile(p)
)
DATA_PATH = os.path.join(BENCH_DIR, "data", "input_data.jsonl")
LOG_DIR = os.path.join(BENCH_DIR, "logs")

MODEL = "claude-haiku-4-5-20251001"
SYSTEM_PROMPT = (
    "You are a helpful assistant. Respond to the user's request directly and "
    "completely, following every formatting and content constraint exactly. "
    "Output only the response itself, with no preamble or commentary about it."
)

REVISION_TEMPLATE = """\
Your previous response to a request did not satisfy all of the requirements.

Original request:
---
{prompt}
---

Your previous response:
---
{response}
---

Automated acceptance checks found these problems:
{failures}

Write a new, complete response to the original request that fixes ALL of the
problems listed above while still satisfying every other requirement of the
original request. Output only the new response itself, nothing else.
"""

_write_lock = threading.Lock()
_print_lock = threading.Lock()


def log_append(path: str, record: dict) -> None:
    with _write_lock:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def call_model(prompt: str, scratch_dir: str, timeout: int = 300, retries: int = 3) -> tuple[str, float, int]:
    """One model call via the Claude Code CLI in print mode. Returns (text, seconds, attempts)."""
    cmd = [
        "claude", "-p",
        "--model", MODEL,
        "--system-prompt", SYSTEM_PROMPT,
        "--tools", "",
        "--strict-mcp-config",
        "--no-session-persistence",
        prompt,
    ]
    last_err = ""
    for attempt in range(1, retries + 1):
        start = time.time()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=scratch_dir)
        except subprocess.TimeoutExpired:
            last_err = f"timeout after {timeout}s"
            continue
        elapsed = time.time() - start
        text = proc.stdout.strip()
        if proc.returncode == 0 and text:
            return text, elapsed, attempt
        last_err = f"exit={proc.returncode} stderr={proc.stderr.strip()[:300]}"
        time.sleep(2 * attempt)
    raise RuntimeError(f"model call failed after {retries} attempts: {last_err}")


def charter_check(contract: dict, response: str, workdir: str) -> dict:
    """Write response + contract into workdir and run the real charter CLI."""
    charter_path = os.path.join(workdir, "charter.json")
    with open(charter_path, "w", encoding="utf-8") as fh:
        json.dump(contract, fh)
    with open(os.path.join(workdir, "response.txt"), "w", encoding="utf-8") as fh:
        fh.write(response)
    proc = subprocess.run(
        [sys.executable, CHARTER_PY, "check", "--json", "--charter", charter_path, "--root", workdir],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"charter check errored: {proc.stderr[:500]}")
    return json.loads(proc.stdout)


def failed_checks(check_output: dict) -> list[dict]:
    out = []
    for d in check_output["deliverables"]:
        for c in d["checks"]:
            if c["status"] != "pass" and c["severity"] == "error":
                out.append(c)
    return out


def format_failures(contract: dict, fails: list[dict]) -> str:
    desc_by_id = {}
    for d in contract["deliverable"]:
        for c in d["check"]:
            if "id" in c:
                desc_by_id[c["id"]] = c.get("description", "")
    lines = []
    for f in fails:
        desc = desc_by_id.get(f["check_id"], "")
        prefix = f"- {desc}: " if desc else "- "
        lines.append(f"{prefix}{f['message']}")
    return "\n".join(lines)


def process_prompt(record: dict, args, scratch_root: str) -> dict:
    key = record["key"]
    prompt = record["prompt"]
    contract, coverage = compile_record(record)
    workdir = os.path.join(scratch_root, str(key))
    os.makedirs(workdir, exist_ok=True)

    # ----- control: single shot ------------------------------------------
    t0 = time.time()
    control_response, control_secs, control_tries = call_model(prompt, workdir)
    log_append(os.path.join(LOG_DIR, "control_raw.jsonl"), {
        "key": key, "model": MODEL, "prompt": prompt, "response": control_response,
        "duration_s": round(control_secs, 2), "cli_attempts": control_tries,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })
    log_append(os.path.join(LOG_DIR, "control_responses.jsonl"),
               {"prompt": prompt, "response": control_response})

    # ----- treatment: charter gate-and-revise, starting from the same response
    attempts = []
    current = control_response
    n_revision_calls = 0
    for attempt_idx in range(args.max_revisions + 1):
        check_out = charter_check(contract, current, workdir)
        fails = failed_checks(check_out)
        attempts.append({
            "attempt": attempt_idx,
            "response": current,
            "charter_ok": check_out["ok"],
            "n_failed_checks": len(fails),
            "failed_checks": [{"id": f["check_id"], "type": f["check_type"], "message": f["message"]} for f in fails],
        })
        if check_out["ok"] or attempt_idx == args.max_revisions:
            break
        revision_prompt = REVISION_TEMPLATE.format(
            prompt=prompt, response=current, failures=format_failures(contract, fails),
        )
        current, secs, tries = call_model(revision_prompt, workdir)
        n_revision_calls += 1

    # The gate selects the attempt with the fewest failed checks (latest wins ties).
    best = min(reversed(attempts), key=lambda a: a["n_failed_checks"])
    final_response = best["response"]

    log_append(os.path.join(LOG_DIR, "treatment_raw.jsonl"), {
        "key": key, "model": MODEL, "prompt": prompt,
        "charter_contract": contract, "coverage": coverage,
        "attempts": attempts, "selected_attempt": best["attempt"],
        "final_response": final_response,
        "n_revision_calls": n_revision_calls,
        "total_duration_s": round(time.time() - t0, 2),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })
    log_append(os.path.join(LOG_DIR, "treatment_responses.jsonl"),
               {"prompt": prompt, "response": final_response})

    return {"key": key, "n_revision_calls": n_revision_calls,
            "control_failed_checks": attempts[0]["n_failed_checks"],
            "final_failed_checks": best["n_failed_checks"]}


def already_done() -> set:
    done = set()
    path = os.path.join(LOG_DIR, "treatment_raw.jsonl")
    if os.path.isfile(path):
        with open(path) as fh:
            for line in fh:
                try:
                    done.add(json.loads(line)["key"])
                except (json.JSONDecodeError, KeyError):
                    continue
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="only run the first N prompts (pilot)")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--max-revisions", type=int, default=2)
    args = parser.parse_args()

    os.makedirs(LOG_DIR, exist_ok=True)
    records = []
    with open(DATA_PATH) as fh:
        for line in fh:
            records.append(json.loads(line))
    if args.limit:
        records = records[: args.limit]

    done = already_done()
    todo = [r for r in records if r["key"] not in done]
    print(f"{len(records)} prompts requested, {len(done)} already done, {len(todo)} to run "
          f"({args.workers} workers, model {MODEL})", flush=True)

    scratch_root = tempfile.mkdtemp(prefix="ifeval-charter-")
    started = time.time()
    n_ok = n_err = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_prompt, rec, args, scratch_root): rec["key"] for rec in todo}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                res = fut.result()
                n_ok += 1
                with _print_lock:
                    print(f"[{n_ok + n_err}/{len(todo)}] key={key} ok "
                          f"(revisions={res['n_revision_calls']}, "
                          f"gate fails {res['control_failed_checks']} -> {res['final_failed_checks']}) "
                          f"elapsed={time.time() - started:.0f}s", flush=True)
            except Exception as exc:
                n_err += 1
                log_append(os.path.join(LOG_DIR, "errors.jsonl"),
                           {"key": key, "error": str(exc), "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
                with _print_lock:
                    print(f"[{n_ok + n_err}/{len(todo)}] key={key} ERROR: {exc}", flush=True)

    print(f"done: {n_ok} ok, {n_err} errors, {time.time() - started:.0f}s total", flush=True)
    return 0 if n_err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
