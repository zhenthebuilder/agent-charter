#!/usr/bin/env python3
"""Charter — declarative deliverable contracts for AI agent runs.

A charter is a small TOML (or JSON) file that states which artifacts a
long-running agent task must produce and which machine-checkable acceptance
criteria they must pass. The same contract:

  * instructs the agent          (`charter explain`)
  * gates the run, CI-style      (`charter check`, exit code 0/1)
  * produces an audit trail      (`charter report`)

Zero runtime dependencies. Python >= 3.11. Single file by design so that
"download charter.py and run it" is a valid install path.

Project: https://github.com/zhenthebuilder/agent-charter
License: MIT
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import glob as _glob
import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
import tomllib

__version__ = "0.1.0"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

SEVERITIES = ("error", "warn")


@dataclasses.dataclass
class Check:
    """A single machine-checkable acceptance criterion."""

    type: str
    id: str
    severity: str = "error"
    description: str = ""
    params: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Deliverable:
    """A required (or optional) artifact of the run."""

    id: str
    title: str = ""
    description: str = ""
    path: str | None = None
    required: bool = True
    checks: list[Check] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Charter:
    name: str
    description: str = ""
    version: str = "1"
    root: str = "."
    deliverables: list[Deliverable] = dataclasses.field(default_factory=list)
    source_path: str | None = None


@dataclasses.dataclass
class CheckResult:
    check_id: str
    check_type: str
    severity: str
    status: str  # "pass" | "fail" | "error"
    message: str
    path: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "pass"


@dataclasses.dataclass
class DeliverableResult:
    id: str
    title: str
    required: bool
    checks: list[CheckResult]

    @property
    def status(self) -> str:
        if any(c.status in ("fail", "error") and c.severity == "error" for c in self.checks):
            return "fail"
        if any(c.status in ("fail", "error") for c in self.checks):
            return "warn"
        return "pass"


class CharterError(Exception):
    """Raised for contract loading / validation problems."""


# ---------------------------------------------------------------------------
# Contract loading and validation
# ---------------------------------------------------------------------------

DEFAULT_CHARTER_FILES = ("charter.toml", "charter.json", ".charter.toml")


def find_charter_file(start_dir: str = ".") -> str | None:
    for name in DEFAULT_CHARTER_FILES:
        candidate = os.path.join(start_dir, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def load_charter(path: str) -> Charter:
    if not os.path.isfile(path):
        raise CharterError(f"charter file not found: {path}")
    try:
        with open(path, "rb") as fh:
            if path.endswith(".json"):
                raw = json.load(fh)
            else:
                raw = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        raise CharterError(f"could not parse {path}: {exc}") from exc

    meta = raw.get("charter", {})
    if not isinstance(meta, dict):
        raise CharterError("[charter] section must be a table")

    settings = raw.get("settings", {})
    charter = Charter(
        name=str(meta.get("name", os.path.basename(os.path.dirname(os.path.abspath(path))) or "unnamed")),
        description=str(meta.get("description", "")),
        version=str(meta.get("version", "1")),
        root=str(settings.get("root", ".")),
        source_path=path,
    )

    raw_deliverables = raw.get("deliverable", [])
    if isinstance(raw_deliverables, dict):
        raw_deliverables = [raw_deliverables]
    if not raw_deliverables:
        raise CharterError("charter defines no [[deliverable]] entries")

    seen_ids: set[str] = set()
    for i, draw in enumerate(raw_deliverables):
        if not isinstance(draw, dict):
            raise CharterError(f"deliverable #{i + 1} is not a table")
        did = str(draw.get("id", f"deliverable-{i + 1}"))
        if did in seen_ids:
            raise CharterError(f"duplicate deliverable id: {did!r}")
        seen_ids.add(did)
        deliverable = Deliverable(
            id=did,
            title=str(draw.get("title", did)),
            description=str(draw.get("description", "")),
            path=draw.get("path"),
            required=bool(draw.get("required", True)),
        )
        raw_checks = draw.get("check", [])
        if isinstance(raw_checks, dict):
            raw_checks = [raw_checks]
        for j, craw in enumerate(raw_checks):
            if not isinstance(craw, dict):
                raise CharterError(f"check #{j + 1} of deliverable {did!r} is not a table")
            ctype = craw.get("type")
            if not ctype:
                raise CharterError(f"check #{j + 1} of deliverable {did!r} is missing 'type'")
            params = {k: v for k, v in craw.items() if k not in ("type", "id", "severity", "description")}
            check = Check(
                type=str(ctype),
                id=str(craw.get("id", f"{did}.{j + 1}")),
                severity=str(craw.get("severity", "error")),
                description=str(craw.get("description", "")),
                params=params,
            )
            deliverable.checks.append(check)
        # A deliverable with a path but no checks gets an implicit existence check.
        if not deliverable.checks:
            if deliverable.path is None:
                raise CharterError(
                    f"deliverable {did!r} has no checks and no path; nothing to verify"
                )
            deliverable.checks.append(
                Check(type="exists", id=f"{did}.exists", params={"path": deliverable.path})
            )
        charter.deliverables.append(deliverable)

    validate_charter(charter)
    return charter


def validate_charter(charter: Charter) -> list[str]:
    """Validate contract structure. Raises CharterError on fatal problems."""
    problems: list[str] = []
    for deliverable in charter.deliverables:
        for check in deliverable.checks:
            spec = CHECK_TYPES.get(check.type)
            if spec is None:
                problems.append(
                    f"{deliverable.id}/{check.id}: unknown check type {check.type!r} "
                    f"(known: {', '.join(sorted(CHECK_TYPES))})"
                )
                continue
            if check.severity not in SEVERITIES:
                problems.append(
                    f"{deliverable.id}/{check.id}: invalid severity {check.severity!r}"
                )
            missing = [
                p for p in spec.required_params
                if p not in check.params
                and not (p == "path" and deliverable.path is not None)
            ]
            if missing:
                problems.append(
                    f"{deliverable.id}/{check.id} ({check.type}): missing parameter(s) {', '.join(missing)}"
                )
            unknown = [
                p for p in check.params
                if p not in spec.required_params and p not in spec.optional_params
            ]
            if unknown:
                problems.append(
                    f"{deliverable.id}/{check.id} ({check.type}): unknown parameter(s) {', '.join(unknown)}"
                )
    if problems:
        raise CharterError("invalid charter:\n  - " + "\n  - ".join(problems))
    return problems


# ---------------------------------------------------------------------------
# Check implementations
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class CheckSpec:
    func: object
    required_params: tuple[str, ...]
    optional_params: tuple[str, ...]
    doc: str


class _Ctx:
    """Evaluation context: the root directory checks resolve paths against."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    def resolve_glob(self, pattern: str) -> list[str]:
        matches = _glob.glob(pattern, root_dir=self.root, recursive=True)
        return sorted(m for m in matches if os.path.isfile(os.path.join(self.root, m)))

    def abspath(self, rel: str) -> str:
        return os.path.join(self.root, rel)

    def read_text(self, rel: str) -> str:
        with open(self.abspath(rel), "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()


def _check_path(check: Check, deliverable: Deliverable) -> str:
    path = check.params.get("path", deliverable.path)
    if path is None:
        raise CharterError(f"check {check.id} has no path")
    return str(path)


def _result(check: Check, status: str, message: str, path: str | None = None) -> CheckResult:
    return CheckResult(
        check_id=check.id,
        check_type=check.type,
        severity=check.severity,
        status=status,
        message=message,
        path=path,
    )


def _eval_exists(check: Check, deliverable: Deliverable, ctx: _Ctx) -> CheckResult:
    pattern = _check_path(check, deliverable)
    min_count = int(check.params.get("min_count", 1))
    matches = ctx.resolve_glob(pattern)
    if len(matches) >= min_count:
        return _result(check, "pass", f"{len(matches)} file(s) match {pattern!r}", pattern)
    return _result(
        check, "fail",
        f"expected at least {min_count} file(s) matching {pattern!r}, found {len(matches)}",
        pattern,
    )


def _eval_not_exists(check: Check, deliverable: Deliverable, ctx: _Ctx) -> CheckResult:
    pattern = _check_path(check, deliverable)
    matches = ctx.resolve_glob(pattern)
    if not matches:
        return _result(check, "pass", f"no files match {pattern!r}", pattern)
    sample = ", ".join(matches[:5])
    return _result(check, "fail", f"{len(matches)} forbidden file(s) match {pattern!r}: {sample}", pattern)


def _files_or_fail(check: Check, deliverable: Deliverable, ctx: _Ctx) -> tuple[list[str], CheckResult | None]:
    pattern = _check_path(check, deliverable)
    matches = ctx.resolve_glob(pattern)
    if not matches:
        return [], _result(check, "fail", f"no files match {pattern!r}", pattern)
    return matches, None


def _eval_min_bytes(check: Check, deliverable: Deliverable, ctx: _Ctx) -> CheckResult:
    files, err = _files_or_fail(check, deliverable, ctx)
    if err:
        return err
    minimum = int(check.params["value"])
    small = [(f, os.path.getsize(ctx.abspath(f))) for f in files if os.path.getsize(ctx.abspath(f)) < minimum]
    if not small:
        return _result(check, "pass", f"all {len(files)} file(s) are >= {minimum} bytes", check.params.get("path", deliverable.path))
    detail = ", ".join(f"{f} ({s}B)" for f, s in small[:5])
    return _result(check, "fail", f"file(s) below {minimum} bytes: {detail}", check.params.get("path", deliverable.path))


def _eval_max_bytes(check: Check, deliverable: Deliverable, ctx: _Ctx) -> CheckResult:
    files, err = _files_or_fail(check, deliverable, ctx)
    if err:
        return err
    maximum = int(check.params["value"])
    big = [(f, os.path.getsize(ctx.abspath(f))) for f in files if os.path.getsize(ctx.abspath(f)) > maximum]
    if not big:
        return _result(check, "pass", f"all {len(files)} file(s) are <= {maximum} bytes", check.params.get("path", deliverable.path))
    detail = ", ".join(f"{f} ({s}B)" for f, s in big[:5])
    return _result(check, "fail", f"file(s) above {maximum} bytes: {detail}", check.params.get("path", deliverable.path))


def _compile_pattern(check: Check) -> re.Pattern:
    pattern = str(check.params["pattern"])
    flags = 0
    if not bool(check.params.get("case_sensitive", True)):
        flags |= re.IGNORECASE
    if bool(check.params.get("regex", False)):
        return re.compile(pattern, flags)
    return re.compile(re.escape(pattern), flags)


def _eval_contains(check: Check, deliverable: Deliverable, ctx: _Ctx) -> CheckResult:
    files, err = _files_or_fail(check, deliverable, ctx)
    if err:
        return err
    rx = _compile_pattern(check)
    min_count = int(check.params.get("min_count", 1))
    max_count = check.params.get("max_count")
    total = sum(len(rx.findall(ctx.read_text(f))) for f in files)
    label = check.params["pattern"]
    problems = []
    if total < min_count:
        problems.append(f"expected at least {min_count}")
    if max_count is not None and total > int(max_count):
        problems.append(f"expected at most {max_count}")
    if not problems:
        return _result(check, "pass", f"pattern {label!r} found {total} time(s)", check.params.get("path", deliverable.path))
    return _result(
        check, "fail",
        f"pattern {label!r} found {total} time(s); {' and '.join(problems)}",
        check.params.get("path", deliverable.path),
    )


def _eval_not_contains(check: Check, deliverable: Deliverable, ctx: _Ctx) -> CheckResult:
    files, err = _files_or_fail(check, deliverable, ctx)
    if err:
        return err
    rx = _compile_pattern(check)
    label = check.params["pattern"]
    offenders = []
    for f in files:
        hits = len(rx.findall(ctx.read_text(f)))
        if hits:
            offenders.append(f"{f} ({hits}x)")
    if not offenders:
        return _result(check, "pass", f"forbidden pattern {label!r} not found", check.params.get("path", deliverable.path))
    return _result(
        check, "fail",
        f"forbidden pattern {label!r} found in: {', '.join(offenders[:5])}",
        check.params.get("path", deliverable.path),
    )


PLACEHOLDER_PATTERNS = [
    r"\bTODO\b", r"\bFIXME\b", r"\bTBD\b", r"\bXXX\b",
    r"lorem ipsum", r"\[insert[^\]]*\]", r"<placeholder", r"\bplaceholder text\b",
]


def _eval_no_placeholder(check: Check, deliverable: Deliverable, ctx: _Ctx) -> CheckResult:
    files, err = _files_or_fail(check, deliverable, ctx)
    if err:
        return err
    extra = check.params.get("extra_patterns", [])
    patterns = PLACEHOLDER_PATTERNS + [str(p) for p in extra]
    rx = re.compile("|".join(patterns), re.IGNORECASE)
    offenders = []
    for f in files:
        hits = rx.findall(ctx.read_text(f))
        if hits:
            offenders.append(f"{f} ({len(hits)}x, e.g. {hits[0]!r})")
    if not offenders:
        return _result(check, "pass", f"no placeholder text in {len(files)} file(s)", check.params.get("path", deliverable.path))
    return _result(check, "fail", f"placeholder text found in: {', '.join(offenders[:5])}", check.params.get("path", deliverable.path))


def _eval_word_count(check: Check, deliverable: Deliverable, ctx: _Ctx) -> CheckResult:
    files, err = _files_or_fail(check, deliverable, ctx)
    if err:
        return err
    minimum = check.params.get("min")
    maximum = check.params.get("max")
    bad = []
    counts = []
    for f in files:
        n = len(ctx.read_text(f).split())
        counts.append(f"{f}: {n} words")
        if minimum is not None and n < int(minimum):
            bad.append(f"{f} has {n} words (< {minimum})")
        if maximum is not None and n > int(maximum):
            bad.append(f"{f} has {n} words (> {maximum})")
    if not bad:
        return _result(check, "pass", "; ".join(counts[:5]), check.params.get("path", deliverable.path))
    return _result(check, "fail", "; ".join(bad[:5]), check.params.get("path", deliverable.path))


def _eval_line_count(check: Check, deliverable: Deliverable, ctx: _Ctx) -> CheckResult:
    files, err = _files_or_fail(check, deliverable, ctx)
    if err:
        return err
    minimum = check.params.get("min")
    maximum = check.params.get("max")
    bad = []
    counts = []
    for f in files:
        n = len(ctx.read_text(f).splitlines())
        counts.append(f"{f}: {n} lines")
        if minimum is not None and n < int(minimum):
            bad.append(f"{f} has {n} lines (< {minimum})")
        if maximum is not None and n > int(maximum):
            bad.append(f"{f} has {n} lines (> {maximum})")
    if not bad:
        return _result(check, "pass", "; ".join(counts[:5]), check.params.get("path", deliverable.path))
    return _result(check, "fail", "; ".join(bad[:5]), check.params.get("path", deliverable.path))


def _eval_file_count(check: Check, deliverable: Deliverable, ctx: _Ctx) -> CheckResult:
    pattern = _check_path(check, deliverable)
    matches = ctx.resolve_glob(pattern)
    minimum = check.params.get("min")
    maximum = check.params.get("max")
    n = len(matches)
    problems = []
    if minimum is not None and n < int(minimum):
        problems.append(f"found {n} file(s) matching {pattern!r}, expected >= {minimum}")
    if maximum is not None and n > int(maximum):
        problems.append(f"found {n} file(s) matching {pattern!r}, expected <= {maximum}")
    if not problems:
        return _result(check, "pass", f"{n} file(s) match {pattern!r}", pattern)
    return _result(check, "fail", "; ".join(problems), pattern)


def _dotted_get(data: object, dotted: str) -> tuple[bool, object]:
    current = data
    for part in dotted.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False, None
    return True, current


def _eval_json_valid(check: Check, deliverable: Deliverable, ctx: _Ctx) -> CheckResult:
    files, err = _files_or_fail(check, deliverable, ctx)
    if err:
        return err
    required_keys = [str(k) for k in check.params.get("keys", [])]
    bad = []
    for f in files:
        try:
            data = json.loads(ctx.read_text(f))
        except json.JSONDecodeError as exc:
            bad.append(f"{f}: invalid JSON ({exc.msg} at line {exc.lineno})")
            continue
        for key in required_keys:
            found, _ = _dotted_get(data, key)
            if not found:
                bad.append(f"{f}: missing key {key!r}")
    if not bad:
        suffix = f" with key(s) {', '.join(required_keys)}" if required_keys else ""
        return _result(check, "pass", f"{len(files)} valid JSON file(s){suffix}", check.params.get("path", deliverable.path))
    return _result(check, "fail", "; ".join(bad[:5]), check.params.get("path", deliverable.path))


_HEADING_RX = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)


def _eval_markdown_sections(check: Check, deliverable: Deliverable, ctx: _Ctx) -> CheckResult:
    files, err = _files_or_fail(check, deliverable, ctx)
    if err:
        return err
    required = [str(s) for s in check.params.get("sections", [])]
    bad = []
    for f in files:
        headings = [h.strip().lower() for h in _HEADING_RX.findall(ctx.read_text(f))]
        for section in required:
            if not any(section.lower() in h for h in headings):
                bad.append(f"{f}: missing section {section!r}")
    if not bad:
        return _result(check, "pass", f"all {len(required)} required section(s) present", check.params.get("path", deliverable.path))
    return _result(check, "fail", "; ".join(bad[:8]), check.params.get("path", deliverable.path))


def _eval_command(check: Check, deliverable: Deliverable, ctx: _Ctx) -> CheckResult:
    cmd = str(check.params["run"])
    timeout = int(check.params.get("timeout", 120))
    expect_exit = int(check.params.get("expect_exit", 0))
    cwd = ctx.abspath(str(check.params.get("cwd", ".")))
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _result(check, "fail", f"command timed out after {timeout}s: {cmd}", None)
    except OSError as exc:
        return _result(check, "error", f"could not run command {cmd!r}: {exc}", None)
    if proc.returncode == expect_exit:
        return _result(check, "pass", f"command exited {proc.returncode}: {cmd}", None)
    tail = (proc.stdout + "\n" + proc.stderr).strip()[-400:]
    return _result(
        check, "fail",
        f"command exited {proc.returncode} (expected {expect_exit}): {cmd}\n{textwrap.indent(tail, '      ')}",
        None,
    )


CHECK_TYPES: dict[str, CheckSpec] = {
    "exists": CheckSpec(_eval_exists, ("path",), ("min_count",),
                        "At least `min_count` (default 1) files match `path` (glob allowed)."),
    "not_exists": CheckSpec(_eval_not_exists, ("path",), (),
                            "No file matches `path` (glob allowed)."),
    "min_bytes": CheckSpec(_eval_min_bytes, ("value",), ("path",),
                           "Every matched file is at least `value` bytes."),
    "max_bytes": CheckSpec(_eval_max_bytes, ("value",), ("path",),
                           "Every matched file is at most `value` bytes."),
    "contains": CheckSpec(_eval_contains, ("pattern",), ("path", "min_count", "max_count", "regex", "case_sensitive"),
                          "Pattern occurs between `min_count` (default 1) and `max_count` (default unlimited) times across matched files (`regex = true` for regex)."),
    "not_contains": CheckSpec(_eval_not_contains, ("pattern",), ("path", "regex", "case_sensitive"),
                              "Pattern occurs nowhere in matched files."),
    "no_placeholder": CheckSpec(_eval_no_placeholder, (), ("path", "extra_patterns"),
                                "No TODO/FIXME/TBD/lorem-ipsum style placeholder text in matched files."),
    "word_count": CheckSpec(_eval_word_count, (), ("path", "min", "max"),
                            "Each matched file's word count is within [min, max]."),
    "line_count": CheckSpec(_eval_line_count, (), ("path", "min", "max"),
                            "Each matched file's line count is within [min, max]."),
    "file_count": CheckSpec(_eval_file_count, (), ("path", "min", "max"),
                            "The number of files matching `path` is within [min, max]."),
    "json_valid": CheckSpec(_eval_json_valid, (), ("path", "keys"),
                            "Each matched file parses as JSON and contains the dotted `keys`."),
    "markdown_sections": CheckSpec(_eval_markdown_sections, ("sections",), ("path",),
                                   "Each matched markdown file contains all required headings."),
    "command": CheckSpec(_eval_command, ("run",), ("cwd", "timeout", "expect_exit"),
                         "Shell command exits with `expect_exit` (default 0). Use for tests, linters, builds."),
}


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate(charter: Charter, root: str | None = None) -> list[DeliverableResult]:
    base = root or charter.root
    if charter.source_path and not os.path.isabs(base):
        base = os.path.join(os.path.dirname(os.path.abspath(charter.source_path)), base)
    ctx = _Ctx(base)
    results: list[DeliverableResult] = []
    for deliverable in charter.deliverables:
        check_results: list[CheckResult] = []
        for check in deliverable.checks:
            spec = CHECK_TYPES[check.type]
            try:
                check_results.append(spec.func(check, deliverable, ctx))
            except CharterError as exc:
                check_results.append(_result(check, "error", str(exc)))
            except Exception as exc:  # pragma: no cover - defensive
                check_results.append(_result(check, "error", f"internal error: {exc!r}"))
        results.append(
            DeliverableResult(
                id=deliverable.id,
                title=deliverable.title,
                required=deliverable.required,
                checks=check_results,
            )
        )
    return results


def overall_ok(results: list[DeliverableResult], charter: Charter) -> bool:
    required_ids = {d.id for d in charter.deliverables if d.required}
    return all(r.status != "fail" for r in results if r.id in required_ids)


def results_to_dict(charter: Charter, results: list[DeliverableResult], root: str | None = None) -> dict:
    summary = {
        "deliverables_total": len(results),
        "deliverables_passed": sum(1 for r in results if r.status == "pass"),
        "checks_total": sum(len(r.checks) for r in results),
        "checks_passed": sum(1 for r in results for c in r.checks if c.ok),
        "checks_failed": sum(
            1 for r in results for c in r.checks if not c.ok and c.severity == "error"
        ),
        "warnings": sum(
            1 for r in results for c in r.checks if not c.ok and c.severity == "warn"
        ),
    }
    return {
        "charter": charter.name,
        "charter_version": charter.version,
        "charter_file": charter.source_path,
        "root": root or charter.root,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "tool_version": __version__,
        "ok": overall_ok(results, charter),
        "summary": summary,
        "deliverables": [
            {
                "id": r.id,
                "title": r.title,
                "required": r.required,
                "status": r.status,
                "checks": [dataclasses.asdict(c) for c in r.checks],
            }
            for r in results
        ],
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _use_color(stream) -> bool:
    return stream.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str, enabled: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled else text


def render_check_output(charter: Charter, results: list[DeliverableResult], quiet: bool, stream=None) -> str:
    stream = stream or sys.stdout
    color = _use_color(stream)
    lines: list[str] = []
    if not quiet:
        lines.append(_c("1", f"charter: {charter.name}", color))
    for r in results:
        mark = {"pass": "✓", "warn": "⚠", "fail": "✗"}[r.status]
        code = {"pass": "32", "warn": "33", "fail": "31"}[r.status]
        opt = "" if r.required else " (optional)"
        lines.append(_c(code, f"{mark} {r.id}{opt} — {r.title}", color))
        for c in r.checks:
            if c.ok and quiet:
                continue
            cmark = "✓" if c.ok else ("⚠" if c.severity == "warn" else "✗")
            ccode = "32" if c.ok else ("33" if c.severity == "warn" else "31")
            lines.append("    " + _c(ccode, f"{cmark} [{c.check_type}] {c.message}", color))
    summary = results_to_dict(charter, results)["summary"]
    status_text = "PASS" if overall_ok(results, charter) else "FAIL"
    status_code = "1;32" if status_text == "PASS" else "1;31"
    lines.append(
        _c(status_code, f"{status_text}", color)
        + f" — {summary['checks_passed']}/{summary['checks_total']} checks passed, "
        + f"{summary['checks_failed']} failed, {summary['warnings']} warning(s)"
    )
    return "\n".join(lines)


def render_failures_brief(results: list[DeliverableResult]) -> str:
    """Compact failure list, suitable for feeding back to an agent."""
    lines = []
    for r in results:
        for c in r.checks:
            if not c.ok:
                sev = "WARN" if c.severity == "warn" else "FAIL"
                lines.append(f"[{sev}] {r.id} :: {c.check_type} :: {c.message}")
    return "\n".join(lines) if lines else "all checks passed"


def render_explain(charter: Charter) -> str:
    """Render the contract as instructions an agent (or human) can follow."""
    lines = [f"# Deliverable contract: {charter.name}", ""]
    if charter.description:
        lines += [charter.description, ""]
    lines += [
        "Your work is accepted only if every required deliverable below exists and",
        "passes all of its checks. Checks are run automatically with `charter check`;",
        "run it yourself before declaring the task complete.",
        "",
    ]
    for d in charter.deliverables:
        req = "required" if d.required else "optional"
        lines.append(f"## {d.title} ({req})")
        if d.description:
            lines.append(d.description)
        if d.path:
            lines.append(f"- Expected path: `{d.path}`")
        for c in d.checks:
            desc = c.description or _describe_check(c, d.path)
            lines.append(f"- {desc}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _describe_check(c: Check, default_path: str | None = None) -> str:
    p = dict(c.params)
    if "path" not in p and default_path:
        p["path"] = default_path
    t = c.type
    if t == "exists":
        return f"File(s) matching `{p.get('path', '')}` must exist" + (
            f" (at least {p['min_count']})" if "min_count" in p else ""
        )
    if t == "not_exists":
        return f"No file matching `{p.get('path', '')}` may exist"
    if t == "min_bytes":
        return f"`{p.get('path', '')}` must be at least {p['value']} bytes"
    if t == "max_bytes":
        return f"`{p.get('path', '')}` must be at most {p['value']} bytes"
    if t == "contains":
        bounds = [f"at least {p.get('min_count', 1)}"]
        if "max_count" in p:
            bounds.append(f"at most {p['max_count']}")
        return f"`{p.get('path', '')}` must contain `{p['pattern']}` {' and '.join(bounds)} time(s)"
    if t == "not_contains":
        return f"`{p.get('path', '')}` must not contain `{p['pattern']}`"
    if t == "no_placeholder":
        return f"`{p.get('path', '')}` must not contain placeholder text (TODO, FIXME, lorem ipsum, ...)"
    if t == "word_count":
        bounds = []
        if "min" in p:
            bounds.append(f"at least {p['min']}")
        if "max" in p:
            bounds.append(f"at most {p['max']}")
        return f"`{p.get('path', '')}` must have {' and '.join(bounds)} words"
    if t == "line_count":
        bounds = []
        if "min" in p:
            bounds.append(f"at least {p['min']}")
        if "max" in p:
            bounds.append(f"at most {p['max']}")
        return f"`{p.get('path', '')}` must have {' and '.join(bounds)} lines"
    if t == "file_count":
        bounds = []
        if "min" in p:
            bounds.append(f"at least {p['min']}")
        if "max" in p:
            bounds.append(f"at most {p['max']}")
        return f"{' and '.join(bounds)} file(s) must match `{p.get('path', '')}`"
    if t == "json_valid":
        keys = p.get("keys", [])
        suffix = f" with key(s): {', '.join(keys)}" if keys else ""
        return f"`{p.get('path', '')}` must be valid JSON{suffix}"
    if t == "markdown_sections":
        return f"`{p.get('path', '')}` must contain the section heading(s): {', '.join(p['sections'])}"
    if t == "command":
        return f"The command `{p['run']}` must exit successfully"
    return f"{t}: {p}"


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def render_report_md(charter: Charter, results: list[DeliverableResult], root: str | None = None) -> str:
    data = results_to_dict(charter, results, root)
    base = root or charter.root
    if charter.source_path and not os.path.isabs(base):
        base = os.path.join(os.path.dirname(os.path.abspath(charter.source_path)), base)
    lines = [
        f"# Charter audit report — {charter.name}",
        "",
        f"- Generated: {data['generated_at']}",
        f"- Charter file: `{charter.source_path}`"
        + (f" (sha256 `{_sha256(charter.source_path)[:16]}…`)" if charter.source_path and os.path.isfile(charter.source_path) else ""),
        f"- Tool: charter {__version__}",
        f"- Overall: **{'PASS' if data['ok'] else 'FAIL'}** "
        f"({data['summary']['checks_passed']}/{data['summary']['checks_total']} checks passed)",
        "",
    ]
    for r, d in zip(results, charter.deliverables):
        lines.append(f"## {r.title} — {r.status.upper()}")
        if d.path:
            ctx = _Ctx(base)
            for f in ctx.resolve_glob(d.path)[:10]:
                full = ctx.abspath(f)
                size = os.path.getsize(full)
                mtime = _dt.datetime.fromtimestamp(os.path.getmtime(full), _dt.timezone.utc).isoformat(timespec="seconds")
                lines.append(f"- artifact `{f}` — {size} bytes, sha256 `{_sha256(full)[:16]}…`, modified {mtime}")
        lines.append("")
        lines.append("| check | type | severity | result | detail |")
        lines.append("|---|---|---|---|---|")
        for c in r.checks:
            detail = c.message.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {c.check_id} | {c.check_type} | {c.severity} | {'pass' if c.ok else c.status} | {detail} |")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# init / hook templates
# ---------------------------------------------------------------------------

INIT_TEMPLATE = '''\
# charter.toml — deliverable contract for an agent run.
# Docs: https://github.com/zhenthebuilder/agent-charter
#
# The same file instructs the agent (`charter explain`), gates the run
# (`charter check`, exit code 0/1) and produces an audit trail
# (`charter report`).

[charter]
name = "{name}"
description = "What this run must produce, and how it is accepted."
version = "1"

[settings]
# Paths below are resolved relative to this directory.
root = "."

# --- One [[deliverable]] block per required artifact -----------------------

[[deliverable]]
id = "report"
title = "Final report"
path = "report.md"
required = true

  [[deliverable.check]]
  type = "exists"

  [[deliverable.check]]
  type = "word_count"
  min = 500

  [[deliverable.check]]
  type = "markdown_sections"
  sections = ["Summary", "Results", "Limitations"]

  [[deliverable.check]]
  type = "no_placeholder"
  description = "No TODO / FIXME / lorem ipsum left in the report"

[[deliverable]]
id = "data"
title = "Raw results data"
path = "results/*.json"
required = true

  [[deliverable.check]]
  type = "exists"
  min_count = 1

  [[deliverable.check]]
  type = "json_valid"

[[deliverable]]
id = "tests"
title = "Test suite passes"
required = false

  [[deliverable.check]]
  type = "command"
  run = "python3 -m pytest -q"
  timeout = 300
'''


HOOK_CLAUDE = '''\
# Claude Code — block the agent from stopping until the contract passes.
# Add to .claude/settings.json in the project the agent works in:
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "charter check --hook claude" }
        ]
      }
    ]
  }
}
# `charter check --hook claude` exits 2 with the failure list on stderr when the
# contract is not satisfied, which makes Claude Code continue working on the
# unmet checks instead of stopping.
'''

HOOK_GITHUB = '''\
# GitHub Actions — gate merges/releases on the deliverable contract.
# .github/workflows/charter.yml
name: charter
on: [push, pull_request]
jobs:
  charter:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install git+https://github.com/zhenthebuilder/agent-charter
      - run: charter check
'''

HOOK_PRECOMMIT = '''\
# pre-commit — refuse commits that break the contract.
# .pre-commit-config.yaml
repos:
  - repo: local
    hooks:
      - id: charter
        name: charter check
        entry: charter check --quiet
        language: system
        pass_filenames: false
        always_run: true
'''


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_for_cli(args) -> Charter:
    path = args.charter
    if path is None:
        path = find_charter_file(".")
        if path is None:
            raise CharterError(
                "no charter.toml found in the current directory (use --charter PATH or `charter init`)"
            )
    return load_charter(path)


def cmd_init(args) -> int:
    out = args.output
    if os.path.exists(out) and not args.force:
        print(f"refusing to overwrite existing {out} (use --force)", file=sys.stderr)
        return 2
    name = args.name or os.path.basename(os.path.abspath("."))
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(INIT_TEMPLATE.format(name=name))
    print(f"wrote {out} — edit it, then run `charter check`")
    return 0


def cmd_validate(args) -> int:
    try:
        charter = _load_for_cli(args)
    except CharterError as exc:
        print(f"charter: {exc}", file=sys.stderr)
        return 2
    n_checks = sum(len(d.checks) for d in charter.deliverables)
    print(f"ok: {charter.source_path} — {len(charter.deliverables)} deliverable(s), {n_checks} check(s)")
    return 0


def cmd_check(args) -> int:
    try:
        charter = _load_for_cli(args)
    except CharterError as exc:
        print(f"charter: {exc}", file=sys.stderr)
        return 2
    results = evaluate(charter, root=args.root)
    ok = overall_ok(results, charter)

    if args.hook == "claude":
        # Claude Code Stop-hook protocol: exit 2 + stderr message blocks the stop.
        if ok:
            return 0
        print(
            "Deliverable contract not satisfied. Fix these before finishing:\n"
            + render_failures_brief(results),
            file=sys.stderr,
        )
        return 2

    if args.json:
        print(json.dumps(results_to_dict(charter, results, args.root), indent=2))
    else:
        print(render_check_output(charter, results, quiet=args.quiet))
    if args.no_fail:
        return 0
    return 0 if ok else 1


def cmd_explain(args) -> int:
    try:
        charter = _load_for_cli(args)
    except CharterError as exc:
        print(f"charter: {exc}", file=sys.stderr)
        return 2
    print(render_explain(charter), end="")
    return 0


def cmd_report(args) -> int:
    try:
        charter = _load_for_cli(args)
    except CharterError as exc:
        print(f"charter: {exc}", file=sys.stderr)
        return 2
    results = evaluate(charter, root=args.root)
    if args.format == "json":
        out = json.dumps(results_to_dict(charter, results, args.root), indent=2)
    else:
        out = render_report_md(charter, results, args.root)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(out + ("\n" if not out.endswith("\n") else ""))
        print(f"wrote {args.output}")
    else:
        print(out)
    return 0 if overall_ok(results, charter) or args.no_fail else 1


def cmd_hook(args) -> int:
    snippets = {"claude": HOOK_CLAUDE, "github": HOOK_GITHUB, "precommit": HOOK_PRECOMMIT}
    targets = list(snippets) if args.target == "all" else [args.target]
    print("\n".join(snippets[t] for t in targets), end="")
    return 0


def cmd_checks(args) -> int:
    print("Available check types:\n")
    for name, spec in sorted(CHECK_TYPES.items()):
        req = ", ".join(spec.required_params) or "—"
        opt = ", ".join(spec.optional_params) or "—"
        print(f"  {name}\n      {spec.doc}\n      required: {req}   optional: {opt}\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="charter",
        description="Declarative deliverable contracts for AI agent runs.",
    )
    parser.add_argument("--version", action="version", version=f"charter {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--charter", "-c", default=None, help="path to charter.toml / charter.json")
        p.add_argument("--root", default=None, help="directory to evaluate against (default: charter file's directory)")

    p_init = sub.add_parser("init", help="write a starter charter.toml")
    p_init.add_argument("--output", "-o", default="charter.toml")
    p_init.add_argument("--name", default=None)
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_validate = sub.add_parser("validate", help="validate the contract file itself")
    add_common(p_validate)
    p_validate.set_defaults(func=cmd_validate)

    p_check = sub.add_parser("check", help="evaluate the workspace against the contract (exit 0/1)")
    add_common(p_check)
    p_check.add_argument("--json", action="store_true", help="machine-readable output")
    p_check.add_argument("--quiet", "-q", action="store_true", help="only show failures and the summary")
    p_check.add_argument("--no-fail", action="store_true", help="always exit 0")
    p_check.add_argument("--hook", choices=["claude"], default=None, help=argparse.SUPPRESS)
    p_check.set_defaults(func=cmd_check)

    p_explain = sub.add_parser("explain", help="render the contract as agent-facing instructions")
    add_common(p_explain)
    p_explain.set_defaults(func=cmd_explain)

    p_report = sub.add_parser("report", help="write an audit report (markdown or json)")
    add_common(p_report)
    p_report.add_argument("--output", "-o", default=None)
    p_report.add_argument("--format", choices=["md", "json"], default="md")
    p_report.add_argument("--no-fail", action="store_true", help="always exit 0")
    p_report.set_defaults(func=cmd_report)

    p_hook = sub.add_parser("hook", help="print ready-to-paste integrations (Claude Code, GitHub Actions, pre-commit)")
    p_hook.add_argument("target", nargs="?", choices=["claude", "github", "precommit", "all"], default="all")
    p_hook.set_defaults(func=cmd_hook)

    p_checks = sub.add_parser("checks", help="list available check types")
    p_checks.set_defaults(func=cmd_checks)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
