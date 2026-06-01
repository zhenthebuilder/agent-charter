"""Compile IFEval verifiable instructions into Charter deliverable contracts.

This is the "spec -> contract" step a Charter user would write for their own
domain: each IFEval instruction (with its kwargs) is mapped onto Charter's
*generic* check primitives. The mapping is deliberately conservative and every
instruction type is labelled with a coverage class:

  exact      the Charter check is logically equivalent to the official
             IFEval checker (modulo trivial regex/whitespace differences)
  approx     the Charter check captures the constraint only approximately
             (e.g. whitespace word counts vs. nltk tokenization)
  uncovered  the constraint is not expressible with Charter's generic
             primitives (no check is emitted; the gate cannot see it)

The coverage class of every instruction instance is recorded in the run logs
and reported in the paper — the gate can only fix what it can check.
"""

from __future__ import annotations

import re

EXACT = "exact"
APPROX = "approx"
UNCOVERED = "uncovered"

RESPONSE_FILE = "response.txt"


def _check(ctype: str, description: str, **params) -> dict:
    out = {"type": ctype, "description": description, "path": RESPONSE_FILE}
    out.update(params)
    return out


def compile_instruction(instruction_id: str, kwargs: dict) -> tuple[list[dict], str]:
    """Return (charter_checks, coverage_class) for one IFEval instruction."""
    kw = {k: v for k, v in (kwargs or {}).items() if v is not None}

    if instruction_id == "punctuation:no_comma":
        return [_check("not_contains", "No commas anywhere in the response", pattern=",")], EXACT

    if instruction_id == "keywords:existence":
        checks = [
            _check("contains", f"Response must include the keyword {w!r}",
                   pattern=str(w), regex=True, case_sensitive=False, min_count=1)
            for w in kw.get("keywords", [])
        ]
        return checks, EXACT

    if instruction_id == "keywords:frequency":
        word, n, rel = str(kw["keyword"]), int(kw["frequency"]), kw.get("relation", "at least")
        if rel == "at least":
            return [_check("contains", f"Keyword {word!r} must appear at least {n} times",
                           pattern=word, regex=True, case_sensitive=False, min_count=n)], EXACT
        return [_check("contains", f"Keyword {word!r} must appear less than {n} times",
                       pattern=word, regex=True, case_sensitive=False, min_count=0, max_count=n - 1)], EXACT

    if instruction_id == "keywords:forbidden_words":
        checks = [
            _check("not_contains", f"The word {w!r} must not appear",
                   pattern=r"\b" + str(w) + r"\b", regex=True, case_sensitive=False)
            for w in kw.get("forbidden_words", [])
        ]
        return checks, EXACT

    if instruction_id == "keywords:letter_frequency":
        letter, n, rel = str(kw["letter"]).lower(), int(kw["let_frequency"]), kw.get("let_relation", "at least")
        if rel == "at least":
            return [_check("contains", f"The letter {letter!r} must appear at least {n} times",
                           pattern=re.escape(letter), regex=True, case_sensitive=False, min_count=n)], EXACT
        return [_check("contains", f"The letter {letter!r} must appear less than {n} times",
                       pattern=re.escape(letter), regex=True, case_sensitive=False,
                       min_count=0, max_count=n - 1)], EXACT

    if instruction_id == "detectable_content:number_placeholders":
        n = int(kw["num_placeholders"])
        return [_check("contains", f"At least {n} placeholders in square brackets like [address]",
                       pattern=r"\[.*?\]", regex=True, min_count=n)], EXACT

    if instruction_id == "detectable_content:postscript":
        marker = str(kw.get("postscript_marker", "P.S."))
        if marker == "P.P.S":
            pattern = r"p\.\s?p\.\s?s"
        elif marker == "P.S.":
            pattern = r"p\.\s?s\."
        else:
            pattern = marker.lower()
        return [_check("contains", f"Response must contain a postscript starting with {marker}",
                       pattern=pattern, regex=True, case_sensitive=False, min_count=1)], EXACT

    if instruction_id == "detectable_format:constrained_response":
        return [_check("contains", "Response must contain one of: 'My answer is yes.', 'My answer is no.', 'My answer is maybe.'",
                       pattern=r"My answer is (?:yes|no|maybe)\.", regex=True, min_count=1)], EXACT

    if instruction_id == "detectable_format:multiple_sections":
        spliter = str(kw["section_spliter"])
        n = int(kw["num_sections"])
        return [_check("contains", f"Response must have at least {n} sections marked '{spliter} <number>'",
                       pattern=r"\s?" + spliter + r"\s?\d+\s?", regex=True, min_count=n)], EXACT

    if instruction_id == "detectable_format:title":
        return [_check("contains", "Response must contain a title wrapped in double angular brackets, e.g. <<my title>>",
                       pattern=r"<<[^\n]+>>", regex=True, min_count=1)], EXACT

    if instruction_id == "combination:repeat_prompt":
        prompt = str(kw["prompt_to_repeat"]).strip()
        return [_check("contains", "Response must begin by repeating the original request word for word before answering",
                       pattern=r"\A\s*" + re.escape(prompt), regex=True, case_sensitive=False, min_count=1)], EXACT

    if instruction_id == "startend:end_checker":
        phrase = str(kw["end_phrase"]).strip()
        return [_check("contains", f"Response must end with the exact phrase {phrase!r} and nothing after it",
                       pattern=re.escape(phrase) + r'[\s"]*\Z', regex=True, case_sensitive=False, min_count=1)], EXACT

    if instruction_id == "startend:quotation":
        return [_check("contains", "The entire response must be wrapped in double quotation marks",
                       pattern=r'\A\s*"[\s\S]*"\s*\Z', regex=True, min_count=1)], EXACT

    # ----- approximate mappings -------------------------------------------

    if instruction_id == "length_constraints:number_words":
        n, rel = int(kw["num_words"]), kw.get("relation", "at least")
        if rel == "at least":
            return [_check("word_count", f"Response must contain at least {n} words", min=n)], APPROX
        return [_check("word_count", f"Response must contain less than {n} words", max=n - 1)], APPROX

    if instruction_id == "detectable_format:number_highlighted_sections":
        n = int(kw["num_highlights"])
        return [_check("contains", f"At least {n} sections highlighted with asterisks, e.g. *highlighted section*",
                       pattern=r"\*[^\n\*]+\*", regex=True, min_count=n)], APPROX

    if instruction_id == "detectable_format:number_bullet_lists":
        n = int(kw["num_bullets"])
        return [_check("contains", f"Exactly {n} bullet points (lines starting with '* ' or '- ')",
                       pattern=r"(?m)^\s*(?:\*[^\*]|-).*$", regex=True, min_count=n, max_count=n)], APPROX

    if instruction_id == "detectable_format:json_format":
        return [_check("json_valid", "The entire response must be valid JSON (no surrounding prose or markdown fences)")], APPROX

    if instruction_id == "change_case:english_capital":
        return [_check("not_contains", "The entire response must be in ALL CAPITAL letters (no lowercase characters)",
                       pattern="[a-z]", regex=True)], APPROX

    if instruction_id == "change_case:english_lowercase":
        return [_check("not_contains", "The entire response must be in all lowercase letters (no capital letters)",
                       pattern="[A-Z]", regex=True)], APPROX

    if instruction_id == "change_case:capital_word_frequency":
        n = int(kw["capital_frequency"])
        rel = kw.get("capital_relation", "at least")
        if rel == "at least":
            return [_check("contains", f"At least {n} words in ALL CAPITAL letters",
                           pattern=r"\b[A-Z]+\b", regex=True, min_count=n)], APPROX
        return [_check("contains", f"Less than {n} words in ALL CAPITAL letters",
                       pattern=r"\b[A-Z]+\b", regex=True, min_count=0, max_count=n - 1)], APPROX

    if instruction_id == "length_constraints:number_paragraphs":
        n = int(kw["num_paragraphs"])
        return [_check("contains", f"Exactly {n} paragraphs separated by the markdown divider *** (so exactly {n - 1} dividers)",
                       pattern=r"\*\*\*", regex=True, min_count=max(n - 1, 0), max_count=max(n - 1, 0))], APPROX

    if instruction_id == "combination:two_responses":
        return [_check("contains", "Give two different responses separated by exactly one line of six asterisks (******)",
                       pattern=r"\*\*\*\*\*\*", regex=True, min_count=1, max_count=1)], APPROX

    # ----- not expressible with generic Charter primitives -----------------

    if instruction_id in (
        "language:response_language",
        "length_constraints:number_sentences",
        "length_constraints:nth_paragraph_first_word",
    ):
        return [], UNCOVERED

    # Unknown instruction type: be honest, treat as uncovered.
    return [], UNCOVERED


def compile_record(record: dict) -> tuple[dict, dict]:
    """Compile one IFEval input record into a Charter contract (JSON form).

    Returns (contract_dict, coverage_info) where coverage_info maps each
    instruction_id (with positional index) to its coverage class.
    """
    checks: list[dict] = []
    coverage: list[dict] = []
    for idx, (iid, kwargs) in enumerate(zip(record["instruction_id_list"], record["kwargs"])):
        instr_checks, klass = compile_instruction(iid, kwargs)
        for c in instr_checks:
            c = dict(c)
            c["id"] = f"{iid}#{idx}.{len([x for x in checks if x.get('id', '').startswith(iid)])}"
            checks.append(c)
        coverage.append({"instruction_id": iid, "index": idx, "coverage": klass,
                         "n_checks": len(instr_checks)})

    contract = {
        "charter": {
            "name": f"ifeval-{record['key']}",
            "description": "Acceptance checks compiled from the verifiable instructions of one IFEval prompt.",
        },
        "deliverable": [
            {
                "id": "response",
                "title": "Response to the writing task",
                "path": RESPONSE_FILE,
                "required": True,
                "check": [{"type": "exists", "path": RESPONSE_FILE}] + checks,
            }
        ],
    }
    info = {
        "n_instructions": len(record["instruction_id_list"]),
        "n_charter_checks": len(checks),
        "instructions": coverage,
        "n_exact": sum(1 for c in coverage if c["coverage"] == EXACT),
        "n_approx": sum(1 for c in coverage if c["coverage"] == APPROX),
        "n_uncovered": sum(1 for c in coverage if c["coverage"] == UNCOVERED),
    }
    return contract, info


if __name__ == "__main__":
    # Quick self-report: coverage statistics over the full benchmark.
    import collections
    import json
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "data/input_data.jsonl"
    by_class = collections.Counter()
    by_type: dict[str, str] = {}
    n_prompts = 0
    with open(path) as fh:
        for line in fh:
            rec = json.loads(line)
            n_prompts += 1
            _, info = compile_record(rec)
            for instr in info["instructions"]:
                by_class[instr["coverage"]] += 1
                by_type[instr["instruction_id"]] = instr["coverage"]
    total = sum(by_class.values())
    print(f"{n_prompts} prompts, {total} instruction instances")
    for klass in (EXACT, APPROX, UNCOVERED):
        print(f"  {klass:10s} {by_class[klass]:4d}  ({by_class[klass] / total:.1%})")
    print("\nper instruction type:")
    for iid, klass in sorted(by_type.items()):
        print(f"  {klass:10s} {iid}")
