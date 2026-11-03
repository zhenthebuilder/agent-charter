# Charter

**Declarative deliverable contracts for AI agent runs.**

You give a long-horizon agent a brief and walk away for hours. What comes back
is whatever the agent decided "done" means. Charter makes "done" something you
declare up front and check by machine: a small `charter.toml` that lists the
artifacts the run must produce and the acceptance checks each one must pass.

The same contract is used three ways:

| | command | what it does |
|---|---|---|
| **Instruct** | `charter explain` | renders the contract as agent-readable instructions to put in the prompt |
| **Gate** | `charter check` | evaluates the workspace, exit code 0/1 — drop it into CI, a Claude Code stop hook, or pre-commit |
| **Audit** | `charter report` | writes a pass/fail report with file hashes and timestamps you can review without re-reading the transcript |

No LLM judge, no API key, no dependencies. Checks are deterministic and cheap,
so you can run them on every iteration of the agent loop.

## Install (under a minute)

With [uv](https://docs.astral.sh/uv/) or pipx:

```bash
uv tool install git+https://github.com/zhenthebuilder/agent-charter
# or: pipx install git+https://github.com/zhenthebuilder/agent-charter
# or: pip install git+https://github.com/zhenthebuilder/agent-charter
charter --version
```

Or just take the single file — Charter is one stdlib-only module (Python ≥ 3.11):

```bash
curl -fsSLO https://raw.githubusercontent.com/zhenthebuilder/agent-charter/main/charter.py
python3 charter.py --version
```

## Quick start

```bash
cd my-agent-workspace
charter init          # writes a commented charter.toml
$EDITOR charter.toml  # declare your deliverables
charter check         # exit 0 if the contract is satisfied, 1 if not
```

Here's an example contract:

```toml
[charter]
name = "competitive analysis run"
description = "What this run must produce, and how it is accepted."

[[deliverable]]
id = "report"
title = "Analysis report"
path = "report.md"

  [[deliverable.check]]
  type = "word_count"
  min = 1500

  [[deliverable.check]]
  type = "markdown_sections"
  sections = ["Executive summary", "Competitor profiles", "Recommendations"]

  [[deliverable.check]]
  type = "no_placeholder"          # no TODO / FIXME / lorem ipsum left behind

  [[deliverable.check]]
  type = "not_contains"
  pattern = "as an AI"
  case_sensitive = false

[[deliverable]]
id = "data"
title = "Source data behind every claim"
path = "data/*.csv"

  [[deliverable.check]]
  type = "file_count"
  min = 3

[[deliverable]]
id = "tests"
title = "The included scripts actually run"

  [[deliverable.check]]
  type = "command"
  run = "python3 -m pytest -q"
  timeout = 300
```

## Check types

| type | verifies |
|---|---|
| `exists` / `not_exists` | files matching a glob exist (or don't) |
| `file_count` | number of files matching a glob is within bounds |
| `min_bytes` / `max_bytes` | file size bounds |
| `contains` / `not_contains` | a literal or regex pattern occurs (between `min_count` and `max_count` times) |
| `no_placeholder` | no TODO / FIXME / TBD / lorem-ipsum style placeholder text |
| `word_count` / `line_count` | document length bounds |
| `json_valid` | file parses as JSON and contains required (dotted) keys |
| `markdown_sections` | required headings are present |
| `command` | an arbitrary command (tests, linter, build, schema validator) exits 0 |

`charter checks` prints the full reference. Checks default to `severity = "error"`;
use `severity = "warn"` for advisory checks that should not fail the run.

## Putting it in the loop

**Claude Code** — block the agent from stopping until the contract passes:

```jsonc
// .claude/settings.json
{
  "hooks": {
    "Stop": [
      { "hooks": [ { "type": "command", "command": "charter check --hook claude" } ] }
    ]
  }
}
```

When checks fail, the failure list is fed back to the agent and it keeps
working; when they pass, the run is allowed to finish.

**CI** — gate merges and releases on the contract (`charter hook github` prints
a ready-made workflow). **pre-commit** — `charter hook precommit`.

**Any agent framework** — run `charter check --json` after each iteration and
feed the failed checks back to the model. The structured failure report is
designed to be pasted directly into a revision prompt.

## Does it work?

We evaluated the gate-and-revise loop on [IFEval](https://arxiv.org/abs/2311.07911)
(541 prompts with verifiable output constraints), pairing single-shot
generation against the same model (`claude-haiku-4-5`) gated by Charter checks
compiled from each prompt's constraints:

| | single shot | + Charter gate |
|---|---|---|
| prompt-level strict accuracy (official scorer) | 87.1% | **93.9%** (+6.8 pp, McNemar p ≈ 8×10⁻¹⁰) |
| constraint types Charter could not express | 80.0% | 80.0% (unchanged — a gate only fixes what it can check) |
| extra model calls | — | 0.17 per prompt |

The [paper](paper/charter.pdf) (NeurIPS format), the
[benchmark harness, raw logs, and scoring code](benchmark/) are in this
repository — every number above is regenerated from the committed run
artifacts, and the limitations are reported alongside the gains.

## Philosophy

- **Contracts, not vibes.** Acceptance criteria live in a reviewable file, not
  in the prompt history.
- **Deterministic and local.** No model in the verification path; the same
  input always produces the same verdict.
- **Small surface.** One file, stdlib only, a dozen check types that compose.
  Anything fancier belongs in a `command` check you control.

## Development

```bash
git clone https://github.com/zhenthebuilder/agent-charter
cd agent-charter
uv venv && uv pip install pytest && .venv/bin/python -m pytest -q
```

MIT licensed.
