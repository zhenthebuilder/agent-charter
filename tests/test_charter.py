"""Tests for charter.py — run with `python -m pytest -q` from charter/."""

import json
import os
import subprocess
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import charter as ch


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def write(tmp_path, rel, content):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def make_charter(tmp_path, body):
    toml = textwrap.dedent(body)
    path = tmp_path / "charter.toml"
    path.write_text(toml, encoding="utf-8")
    return str(path)


def run_checks(tmp_path, body):
    path = make_charter(tmp_path, body)
    contract = ch.load_charter(path)
    results = ch.evaluate(contract)
    return contract, results


def flat(results):
    return [c for r in results for c in r.checks]


# ---------------------------------------------------------------------------
# loading / validation
# ---------------------------------------------------------------------------

def test_load_minimal(tmp_path):
    write(tmp_path, "out.txt", "hello")
    contract, results = run_checks(tmp_path, """
        [charter]
        name = "t"
        [[deliverable]]
        id = "out"
        path = "out.txt"
    """)
    assert contract.name == "t"
    # implicit existence check added when no checks given
    assert results[0].status == "pass"


def test_load_json_contract(tmp_path):
    write(tmp_path, "a.txt", "x")
    cfg = {
        "charter": {"name": "j"},
        "deliverable": [{"id": "a", "path": "a.txt"}],
    }
    p = tmp_path / "charter.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    contract = ch.load_charter(str(p))
    results = ch.evaluate(contract)
    assert ch.overall_ok(results, contract)


def test_unknown_check_type_rejected(tmp_path):
    with pytest.raises(ch.CharterError, match="unknown check type"):
        run_checks(tmp_path, """
            [charter]
            name = "t"
            [[deliverable]]
            id = "x"
            [[deliverable.check]]
            type = "does_not_exist"
        """)


def test_missing_required_param_rejected(tmp_path):
    with pytest.raises(ch.CharterError, match="missing parameter"):
        run_checks(tmp_path, """
            [charter]
            name = "t"
            [[deliverable]]
            id = "x"
            [[deliverable.check]]
            type = "contains"
            path = "a.txt"
        """)


def test_unknown_param_rejected(tmp_path):
    with pytest.raises(ch.CharterError, match="unknown parameter"):
        run_checks(tmp_path, """
            [charter]
            name = "t"
            [[deliverable]]
            id = "x"
            path = "a.txt"
            [[deliverable.check]]
            type = "exists"
            bogus = 1
        """)


def test_duplicate_deliverable_id_rejected(tmp_path):
    with pytest.raises(ch.CharterError, match="duplicate deliverable id"):
        run_checks(tmp_path, """
            [charter]
            name = "t"
            [[deliverable]]
            id = "x"
            path = "a.txt"
            [[deliverable]]
            id = "x"
            path = "b.txt"
        """)


def test_no_deliverables_rejected(tmp_path):
    with pytest.raises(ch.CharterError, match="no \\[\\[deliverable\\]\\]"):
        run_checks(tmp_path, """
            [charter]
            name = "t"
        """)


def test_missing_charter_file():
    with pytest.raises(ch.CharterError, match="not found"):
        ch.load_charter("/nonexistent/charter.toml")


# ---------------------------------------------------------------------------
# check primitives
# ---------------------------------------------------------------------------

def test_exists_glob_and_min_count(tmp_path):
    write(tmp_path, "logs/a.json", "{}")
    write(tmp_path, "logs/b.json", "{}")
    _, results = run_checks(tmp_path, """
        [charter]
        name = "t"
        [[deliverable]]
        id = "logs"
        [[deliverable.check]]
        type = "exists"
        path = "logs/*.json"
        min_count = 2
        [[deliverable.check]]
        type = "exists"
        path = "logs/*.csv"
        severity = "warn"
    """)
    checks = flat(results)
    assert checks[0].status == "pass"
    assert checks[1].status == "fail"
    assert results[0].status == "warn"  # only a warn-severity failure


def test_not_exists(tmp_path):
    write(tmp_path, "secrets.env", "KEY=1")
    _, results = run_checks(tmp_path, """
        [charter]
        name = "t"
        [[deliverable]]
        id = "clean"
        [[deliverable.check]]
        type = "not_exists"
        path = "*.env"
    """)
    assert flat(results)[0].status == "fail"


def test_min_max_bytes(tmp_path):
    write(tmp_path, "big.txt", "x" * 100)
    _, results = run_checks(tmp_path, """
        [charter]
        name = "t"
        [[deliverable]]
        id = "f"
        path = "big.txt"
        [[deliverable.check]]
        type = "min_bytes"
        value = 50
        [[deliverable.check]]
        type = "max_bytes"
        value = 60
    """)
    checks = flat(results)
    assert checks[0].status == "pass"
    assert checks[1].status == "fail"


def test_contains_and_not_contains(tmp_path):
    write(tmp_path, "report.md", "# Results\nThe accuracy was 0.91.\nTODO finish")
    _, results = run_checks(tmp_path, """
        [charter]
        name = "t"
        [[deliverable]]
        id = "report"
        path = "report.md"
        [[deliverable.check]]
        type = "contains"
        pattern = "accuracy"
        [[deliverable.check]]
        type = "contains"
        pattern = "[0-9]\\\\.[0-9]+"
        regex = true
        [[deliverable.check]]
        type = "not_contains"
        pattern = "TODO"
    """)
    checks = flat(results)
    assert checks[0].status == "pass"
    assert checks[1].status == "pass"
    assert checks[2].status == "fail"


def test_contains_case_insensitive_min_count(tmp_path):
    write(tmp_path, "a.txt", "Foo foo FOO")
    _, results = run_checks(tmp_path, """
        [charter]
        name = "t"
        [[deliverable]]
        id = "a"
        path = "a.txt"
        [[deliverable.check]]
        type = "contains"
        pattern = "foo"
        case_sensitive = false
        min_count = 3
    """)
    assert flat(results)[0].status == "pass"


def test_contains_max_count(tmp_path):
    write(tmp_path, "a.txt", "x, y, z")
    _, results = run_checks(tmp_path, """
        [charter]
        name = "t"
        [[deliverable]]
        id = "a"
        path = "a.txt"
        [[deliverable.check]]
        type = "contains"
        pattern = ","
        min_count = 0
        max_count = 1
        [[deliverable.check]]
        type = "contains"
        pattern = ","
        min_count = 2
        max_count = 5
    """)
    checks = flat(results)
    assert checks[0].status == "fail"
    assert checks[1].status == "pass"


def test_no_placeholder(tmp_path):
    write(tmp_path, "ok.md", "All done, results final.")
    write(tmp_path, "bad.md", "Insert results here: lorem ipsum dolor")
    _, results = run_checks(tmp_path, """
        [charter]
        name = "t"
        [[deliverable]]
        id = "ok"
        path = "ok.md"
        [[deliverable.check]]
        type = "no_placeholder"
        [[deliverable]]
        id = "bad"
        path = "bad.md"
        [[deliverable.check]]
        type = "no_placeholder"
    """)
    assert results[0].status == "pass"
    assert results[1].status == "fail"


def test_word_and_line_count(tmp_path):
    write(tmp_path, "essay.txt", "one two three four five\nsix seven\n")
    _, results = run_checks(tmp_path, """
        [charter]
        name = "t"
        [[deliverable]]
        id = "essay"
        path = "essay.txt"
        [[deliverable.check]]
        type = "word_count"
        min = 5
        max = 10
        [[deliverable.check]]
        type = "line_count"
        min = 3
    """)
    checks = flat(results)
    assert checks[0].status == "pass"
    assert checks[1].status == "fail"


def test_file_count(tmp_path):
    write(tmp_path, "figs/a.png", "x")
    write(tmp_path, "figs/b.png", "x")
    _, results = run_checks(tmp_path, """
        [charter]
        name = "t"
        [[deliverable]]
        id = "figs"
        [[deliverable.check]]
        type = "file_count"
        path = "figs/*.png"
        min = 2
        max = 3
    """)
    assert flat(results)[0].status == "pass"


def test_json_valid_and_keys(tmp_path):
    write(tmp_path, "results.json", json.dumps({"summary": {"accuracy": 0.9}, "runs": [1, 2]}))
    write(tmp_path, "broken.json", "{not json")
    _, results = run_checks(tmp_path, """
        [charter]
        name = "t"
        [[deliverable]]
        id = "good"
        path = "results.json"
        [[deliverable.check]]
        type = "json_valid"
        keys = ["summary.accuracy", "runs.0"]
        [[deliverable]]
        id = "missing-key"
        path = "results.json"
        [[deliverable.check]]
        type = "json_valid"
        keys = ["summary.f1"]
        [[deliverable]]
        id = "broken"
        path = "broken.json"
        [[deliverable.check]]
        type = "json_valid"
    """)
    assert results[0].status == "pass"
    assert results[1].status == "fail"
    assert results[2].status == "fail"


def test_markdown_sections(tmp_path):
    write(tmp_path, "paper.md", "# Title\n\n## Methods\ntext\n\n## Results\nnumbers\n")
    _, results = run_checks(tmp_path, """
        [charter]
        name = "t"
        [[deliverable]]
        id = "paper"
        path = "paper.md"
        [[deliverable.check]]
        type = "markdown_sections"
        sections = ["Methods", "Results"]
        [[deliverable.check]]
        type = "markdown_sections"
        sections = ["Limitations"]
    """)
    checks = flat(results)
    assert checks[0].status == "pass"
    assert checks[1].status == "fail"


def test_command_check(tmp_path):
    write(tmp_path, "data.txt", "42\n")
    _, results = run_checks(tmp_path, """
        [charter]
        name = "t"
        [[deliverable]]
        id = "cmd"
        [[deliverable.check]]
        type = "command"
        run = "grep -q 42 data.txt"
        [[deliverable.check]]
        type = "command"
        run = "grep -q missing data.txt"
    """)
    checks = flat(results)
    assert checks[0].status == "pass"
    assert checks[1].status == "fail"


def test_missing_file_fails_content_checks(tmp_path):
    _, results = run_checks(tmp_path, """
        [charter]
        name = "t"
        [[deliverable]]
        id = "gone"
        path = "missing.md"
        [[deliverable.check]]
        type = "word_count"
        min = 10
    """)
    assert flat(results)[0].status == "fail"
    assert "no files match" in flat(results)[0].message


# ---------------------------------------------------------------------------
# aggregation, optional deliverables, rendering
# ---------------------------------------------------------------------------

def test_optional_deliverable_does_not_fail_run(tmp_path):
    write(tmp_path, "a.txt", "x")
    contract, results = run_checks(tmp_path, """
        [charter]
        name = "t"
        [[deliverable]]
        id = "a"
        path = "a.txt"
        [[deliverable]]
        id = "extra"
        path = "extra.txt"
        required = false
    """)
    assert results[1].status == "fail"
    assert ch.overall_ok(results, contract) is True


def test_required_failure_fails_run(tmp_path):
    contract, results = run_checks(tmp_path, """
        [charter]
        name = "t"
        [[deliverable]]
        id = "a"
        path = "a.txt"
    """)
    assert ch.overall_ok(results, contract) is False


def test_results_to_dict_summary(tmp_path):
    write(tmp_path, "a.txt", "x")
    contract, results = run_checks(tmp_path, """
        [charter]
        name = "t"
        [[deliverable]]
        id = "a"
        path = "a.txt"
        [[deliverable]]
        id = "b"
        path = "b.txt"
    """)
    data = ch.results_to_dict(contract, results)
    assert data["summary"]["checks_total"] == 2
    assert data["summary"]["checks_passed"] == 1
    assert data["summary"]["checks_failed"] == 1
    assert data["ok"] is False


def test_explain_lists_all_deliverables(tmp_path):
    contract, _ = run_checks(tmp_path, """
        [charter]
        name = "t"
        description = "demo"
        [[deliverable]]
        id = "paper"
        title = "Paper PDF"
        path = "paper.pdf"
        [[deliverable.check]]
        type = "min_bytes"
        value = 1000
        [[deliverable]]
        id = "logs"
        title = "Run logs"
        path = "logs/*.jsonl"
        required = false
    """)
    text = ch.render_explain(contract)
    assert "Paper PDF" in text and "Run logs" in text
    assert "at least 1000 bytes" in text
    assert "(optional)" in text


def test_failures_brief(tmp_path):
    _, results = run_checks(tmp_path, """
        [charter]
        name = "t"
        [[deliverable]]
        id = "a"
        path = "a.txt"
    """)
    brief = ch.render_failures_brief(results)
    assert "[FAIL]" in brief and "a ::" in brief


def test_report_md_contains_hashes(tmp_path):
    write(tmp_path, "a.txt", "hello world")
    contract, results = run_checks(tmp_path, """
        [charter]
        name = "t"
        [[deliverable]]
        id = "a"
        path = "a.txt"
    """)
    report = ch.render_report_md(contract, results)
    assert "sha256" in report
    assert "PASS" in report


# ---------------------------------------------------------------------------
# CLI end-to-end (subprocess)
# ---------------------------------------------------------------------------

CHARTER_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "charter.py")


def cli(args, cwd):
    return subprocess.run(
        [sys.executable, CHARTER_PY, *args],
        cwd=cwd, capture_output=True, text=True, timeout=60,
    )


def test_cli_init_validate_check(tmp_path):
    r = cli(["init"], tmp_path)
    assert r.returncode == 0
    assert (tmp_path / "charter.toml").exists()

    r = cli(["validate"], tmp_path)
    assert r.returncode == 0, r.stderr

    # the starter contract requires report.md etc., which don't exist yet
    r = cli(["check"], tmp_path)
    assert r.returncode == 1
    assert "FAIL" in r.stdout

    r = cli(["check", "--no-fail"], tmp_path)
    assert r.returncode == 0

    r = cli(["check", "--json"], tmp_path)
    data = json.loads(r.stdout)
    assert data["ok"] is False


def test_cli_init_refuses_overwrite(tmp_path):
    assert cli(["init"], tmp_path).returncode == 0
    assert cli(["init"], tmp_path).returncode == 2
    assert cli(["init", "--force"], tmp_path).returncode == 0


def test_cli_check_passes_when_contract_met(tmp_path):
    write(tmp_path, "out/result.json", '{"score": 1}')
    make = textwrap.dedent("""
        [charter]
        name = "demo"
        [[deliverable]]
        id = "result"
        path = "out/result.json"
        [[deliverable.check]]
        type = "json_valid"
        keys = ["score"]
    """)
    (tmp_path / "charter.toml").write_text(make, encoding="utf-8")
    r = cli(["check"], tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout


def test_cli_claude_hook_mode(tmp_path):
    make = textwrap.dedent("""
        [charter]
        name = "demo"
        [[deliverable]]
        id = "missing"
        path = "missing.md"
    """)
    (tmp_path / "charter.toml").write_text(make, encoding="utf-8")
    r = cli(["check", "--hook", "claude"], tmp_path)
    assert r.returncode == 2
    assert "not satisfied" in r.stderr

    write(tmp_path, "missing.md", "now present")
    r = cli(["check", "--hook", "claude"], tmp_path)
    assert r.returncode == 0


def test_cli_no_charter_file(tmp_path):
    r = cli(["check"], tmp_path)
    assert r.returncode == 2
    assert "no charter.toml" in r.stderr


def test_cli_explain_report_hook_checks(tmp_path):
    write(tmp_path, "a.txt", "x")
    make = textwrap.dedent("""
        [charter]
        name = "demo"
        [[deliverable]]
        id = "a"
        path = "a.txt"
    """)
    (tmp_path / "charter.toml").write_text(make, encoding="utf-8")
    assert "Deliverable contract" in cli(["explain"], tmp_path).stdout
    r = cli(["report", "-o", "audit.md"], tmp_path)
    assert r.returncode == 0
    assert (tmp_path / "audit.md").exists()
    assert "Stop" in cli(["hook", "claude"], tmp_path).stdout
    assert "exists" in cli(["checks"], tmp_path).stdout


def test_cli_version(tmp_path):
    r = cli(["--version"], tmp_path)
    assert r.returncode == 0
    assert ch.__version__ in r.stdout
