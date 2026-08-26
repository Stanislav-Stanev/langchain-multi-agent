🇧🇬 [Български](README.md) | 🇬🇧 **English**

# Multi-Bot — a Multi-Agent SDLC System with LangChain + LangGraph

A teaching project: **Multi-Bot** is a multi-agent system that automates
part of the **SDLC process** (Software Development Life Cycle) — from a
requirement in a ticket to reviewed and approved code.

> The interface, console output, and agents are bilingual: set
> `APP_LANG=bg|en` in `.env` (or use the language switch in the web UI).
> Code comments are intentionally in Bulgarian — this is a Bulgarian
> teaching project; this file is the English mirror of README.md.

## Architecture — the "Supervisor" pattern

```
       user -> init_run -> SUPERVISOR (one-time triage)
                              |
   ANALYST -> dev_plan -> [approve_plan] -> DEVELOPER -> qa_plan -> QA
 requirements  plan          human         writes code  test plan  review
                 ^ "changes"                    ^                    |
                 +------------------------------+---- NEEDS_WORK ----+
                                                                     v
                                          [approve_publish] -> finalize -> END
   DoD failure / rework limit -> [escalation_gate] -> retry with guidance | stop
   [ ... ] = Human-in-the-Loop gate (pauses and waits for a human)
```

| Node / agent        | SDLC phase     | What it does                                                         |
|---------------------|----------------|----------------------------------------------------------------------|
| `init_run`          | —              | code: folder `runs/<date_time>_<ticket>/`, ticket key                 |
| **Supervisor**      | management     | LLM: one-time triage — where the task enters (or FINISH)              |
| **Analyst**         | Requirements   | agent: specification; `get_ticket_details` (mock Jira / Jira in prod) |
| `dev_plan`          | Planning       | LLM (structured): implementation plan with checkboxes                 |
| `approve_plan`      | —              | a **human** approves the plan / requests changes / aborts             |
| **Developer**       | Implementation | agent: code; `get_coding_standards`, `update_plan_step` (progress)    |
| `qa_plan`           | Test planning  | LLM (structured): test plan per acceptance criterion                  |
| **QA**              | Testing/Review | agent: `check_code_syntax`, `run_test_checklist`, `update_test_case`  |
| `approve_publish`   | —              | a **human** approves publishing (prod: commit / push / draft PR)      |
| `escalation_gate`   | —              | a **human** on escalation: retry with guidance or stop                |
| `finalize`          | —              | code: traceability matrix, `summary.json`, publishing (prod)          |

The Supervisor (an LLM) performs only a **one-time triage** — where the
task enters; `dev_plan`/`qa_plan` each make one **structured** LLM call
(the plan is content — the model decides it; the format and the progress
are code). Everything else is **deterministic code** over typed
artifacts (spec, plan, code, test plan, QA verdict):

- every phase has a **Definition of Done** checked by code (non-empty
  spec, a plan with steps, extracted and syntactically valid code) — a
  missed DoD gives the agent one retry, then **escalates to a human**
  (`escalation_gate`);
- the QA verdict is **structured** (`QAVerdict`, Pydantic) — on
  `NEEDS_WORK` the task goes back to the Developer (the plan gets a
  "Rework N" section with the issues, no re-planning), but at most
  `MAX_REWORK` times (default 3), then escalation instead of an endless loop;
- **Human-in-the-Loop** — the gates `plan`, `publish`, `escalation`
  (`HITL_GATES`) pause the graph with `interrupt()` and wait for a
  decision: approve / request changes (with guidance) / abort; every
  decision is recorded;
- **traceability** — every run leaves a folder `runs/<date_time>_<ticket>/`
  with a plan before and the execution after **every step**
  (`steps/NN-<node>.md`), the plans with checkboxes, `STATUS.md`, a journal,
  a traceability matrix and `summary.json` (see [Modes, plans and Human-in-the-Loop](#modes-plans-and-human-in-the-loop));
- optional: a different model per role (`MODEL_SUPERVISOR`, ...), a
  hard per-run budget limit (`MAX_COST_USD_PER_RUN`), a fallback model
  when the primary fails (`MODEL_FALLBACK`), and **checkpointing** into
  SQLite (`CHECKPOINT_SQLITE_PATH`) — an interrupted run resumes from
  the last saved step (one thread per task).

## Project structure

```
langchain-multi-agent/
├── main.py              # entry point — runs the graph (console)
├── app.py               # Streamlit web UI with live visualization
├── draw_graph.py        # Mermaid diagram of the graph (local, no LLM)
├── requirements.txt     # dependencies (pinned versions)
├── pytest.ini           # pytest configuration (the tests in tests/)
├── ruff.toml            # linter configuration (ruff)
├── improvement.md       # the productionization plan (what and why)
├── .env.example         # settings template (copy as .env)
├── CHANGELOG.md         # release notes (Keep a Changelog)
├── README.en.md         # this file — English mirror of README.md
├── .github/workflows/
│   └── ci.yml           # CI: ruff + pytest + 85% coverage of src/
├── .githooks/
│   ├── pre-commit       # hook: protects main from direct commits
│   └── pre-push         # hook: protects main + Claude syncs docs/changelog
├── docs/
│   ├── prod-mode-plan.md    # the plan for the Demo/Prod mode, HITL and tracing
│   └── plans/               # a plan before every implementation step (date_time)
├── src/
│   ├── config.py        # settings + LLM client factory (anthropic/ollama)
│   ├── modes.py         # demo/prod modes (APP_MODE), repositories, prod validation
│   ├── i18n.py          # bilingual texts (bg/en) + t() helper
│   ├── tools.py         # the tools (mock Jira, linter, QA checklist)
│   ├── toolsets.py      # which tools each role gets per mode
│   ├── plans.py         # Developer/QA plans: schemas, checkboxes, progress
│   ├── dod.py           # Definition of Done policies (demo / prod)
│   ├── hitl.py          # Human-in-the-Loop gates (interrupt + decision)
│   ├── run_tracker.py   # runs/<date_time>_<ticket>/ - journal, steps, status
│   ├── step_plans.py    # the "plan before / execution after" templates per step
│   ├── run_context.py   # RunCtx - the run context for the tools
│   ├── agents.py        # the three worker agents (ReAct)
│   └── graph.py         # supervisor + plans + gates + graph assembly
├── runs/                # (git-ignored) the artifacts of every run
└── tests/               # pytest suite — the full workflow, no real LLM calls
    └── evals/           # golden evals with a REAL LLM (run explicitly)
```

Recommended reading order for learning:
`tools.py` → `agents.py` → `plans.py` → `hitl.py` → `graph.py` → `main.py`

## Installation and running

```bash
# 1. Virtual environment (good practice — isolates dependencies)
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/macOS

# 2. Dependencies
pip install -r requirements.txt

# 3. Settings
copy .env.example .env        # Windows (cp on Linux/macOS)
# edit .env: add your key from https://platform.claude.com/
# and set APP_LANG=en for an English interface
# APP_MODE=prod is the default; for the teaching mode set APP_MODE=demo

# 4. Run with the demo task (ticket DEV-101) - in demo mode
python main.py                # (PowerShell: $env:APP_MODE="demo"; python main.py)
# the graph pauses at the approve_plan gate and waits: [a] approve / [r: guidance] / [q]

# ...or with your own task
python main.py "Implement ticket DEV-102"
python main.py "Write a function that reverses a string"

# Web UI (language, provider, mode and Human-in-the-Loop gates in the sidebar)
streamlit run app.py

# The run's artifacts: runs/<date_time>_DEV-101/ (plans, steps, STATUS.md)
```

## Modes, plans and Human-in-the-Loop

**Mode** (`APP_MODE`, a dropdown in the UI; default **prod**):

| | demo | prod |
|---|------|------|
| Tickets | the sample DEV-101 / DEV-102 (`src/tools.py`) | real Jira via the official Atlassian MCP server *(next step)* |
| Code | a ```python block in the Developer's answer | changes in cloned git repositories → draft PR *(next step)* |
| Plans, steps, HITL | yes | yes |

In this version prod mode is selectable, but its integrations (Jira MCP,
git workspace, draft PR) arrive in the next steps of
[docs/prod-mode-plan.md](docs/prod-mode-plan.md) — without configuration
the UI shows what is missing and does not start a run.

**Plans before work.** Before the Developer the `dev_plan` node produces
an implementation plan (acceptance criteria AC-x, steps S-x with files and
covered criteria); before QA the `qa_plan` node produces a test plan
(cases T-x ↔ criteria). The plan is a Pydantic object (structured output),
JSON is the source of truth, and the markdown with checkboxes is
re-rendered on every change. The agents report progress with the tools
`update_plan_step` / `update_test_case` (`- [ ]` → `- [~]` → `- [x]`); on
rework the plan gets a "Rework N" section and the test plan is reset for a
new run. `finalize` writes a traceability matrix criterion ↔ steps ↔ test
cases ↔ result.

**A plan before every step.** Every node leaves `steps/NN-<node>.md` — a
"Plan" section (goal, inputs, scope, DoD) written *before* execution and an
"Execution" section (result, artifacts, DoD, decision) added *after* it;
`index.md` and `STATUS.md` are refreshed after every step. The run folder
is `runs/<YYYY-MM-DD_HH-MM-SS>_<ticket>/` (`RUNS_DIR`).

**Human-in-the-Loop** (`HITL_GATES`; prod: `plan,publish,escalation`,
demo: `plan,escalation`; empty = no gates):

| Gate | Where | Decisions |
|------|-------|-----------|
| `plan` | after the plan, before the Developer | approve → Developer; changes (with guidance) → re-plan (up to `HITL_MAX_REVISIONS`); abort → `ABORTED` |
| `publish` | after QA APPROVED, before publishing (prod) | approve → commit/push/PR; changes → Developer; abort → no publishing |
| `escalation` | on a DoD failure / exhausted rework limit | retry (with guidance, counters reset) → the same node; abort → `ESCALATED` |

The gates are separate nodes with LangGraph `interrupt()` (they need a
checkpointer — without `CHECKPOINT_SQLITE_PATH` an `InMemorySaver` is used).
The console asks `[a]/[r: text]/[q]`; without an interactive console
`HITL_AUTO_APPROVE=1` approves (and records it). The UI shows the artifact
with Approve / Request changes / Abort buttons. All decisions are in
`hitl-decisions.md`.

## Tests

The project has a pytest suite (`tests/`) covering the full workflow
**without a single real LLM call** — the supervisor and the workers are
replaced with scripted test doubles, so the tests are fast, free and
deterministic:

- **unit tests** — the tools, i18n, config, the modes, the plans, the
  tracing (in `tmp_path`), the DoD policies, the HITL mechanism, the
  graph's building blocks;
- **integration** — the real ReAct agents (`create_agent`) + the real
  tools (incl. `update_plan_step` via `ToolRuntime`), driven by a fake
  tool-calling model;
- **E2E workflow** — the real graph: the happy path, the rework loop
  and its limit, Definition of Done (retry + escalation), the triage
  entries, the on-disk artifacts, the Human-in-the-Loop gates (interrupt
  → approve / revise / abort → resume), the streaming contract and the
  bilingual texts;
- **evals** (`tests/evals/`) — the quality of the REAL LLM decisions:
  a golden dataset (versioned YAML in the repo) + LLM-as-judge through
  the same `get_llm()`. Excluded from the default run (real costs!) —
  run explicitly.

```bash
python -m pytest              # the deterministic suite (~5 s, no LLM)
ruff check .                  # linter (the same one CI runs)
python -m pytest -m eval      # golden evals (real LLM, real costs)
```

## Bilingual support (bg/en)

All user-facing texts live in `src/i18n.py`, keyed by identifier and
language — the code asks for text via `t("key")`. The language comes
from `APP_LANG` (or the UI switch):

- **console and UI texts** switch instantly;
- **agent prompts** (in `agents.py`, `graph.py`) and **mock tool data**
  (in `tools.py`) exist in both languages — the prompt language also
  controls the language the agents respond in;
- an import-time validation guarantees no key is left half-translated.

## Workflow (branching)

**`main` is the production branch** — no direct commits or pushes
(both git hooks guard it). New functionality always goes through a
feature branch and a Pull Request:

```bash
git switch -c feature/feature-name       # 1. new feature branch
# ... work, commit freely ...
git push -u origin feature/feature-name  # 2. push the branch
                                         #    (the hook updates docs
                                         #     and the CHANGELOG here)
gh pr create --fill                      # 3. Pull Request to main
gh pr merge --squash --delete-branch     # 4. merge into main
```

Release notes for every completed feature live in
[CHANGELOG.md](CHANGELOG.md) (Keep a Changelog format): the pre-push
hook adds entries under **[Unreleased]** automatically; on release
they move under a version number.

## Automatic documentation sync (git hooks)

Two hooks live in `.githooks/`:

- **pre-commit** — blocks direct commits on `main`.
- **pre-push** — blocks direct pushes to `main` and runs **Claude Code
  headless** (`claude -p`): it reviews all commits about to be pushed
  and, if they made CLAUDE.md, README.md, README.en.md, requirements.txt
  or CHANGELOG.md inaccurate, updates them in a **separate docs-sync
  commit** (adding a release-notes entry for completed functionality).
  It also watches the bilingual invariants: the two READMEs must stay
  mirrored, and new user-facing texts must have both bg AND en versions
  in `src/i18n.py`.
  The push is then aborted with a clear message — run `git push` again
  and it passes immediately. The check runs once per series of commits,
  and the remote always receives up-to-date documentation.

```bash
# Activation (one-time after cloning — git does not run hooks from the
# repo by default, for security reasons)
git config core.hooksPath .githooks

# Skipping when needed
git push --no-verify              # skips all hooks
SKIP_DOCS_UPDATE=1 git push ...   # skips only the docs sync
SKIP_MAIN_GUARD=1 git push ...    # skips only the main guard (hotfix)
```

## Key concepts this project demonstrates

1. **Supervisor pattern** — a central agent manages specialized
   workers. The most widespread multi-agent architecture.
2. **Tools** — functions with `@tool` that the LLM calls; the
   docstring is the "documentation" the model uses to decide when to
   use them.
3. **ReAct loop** — think → tool → result → think again → answer
   (`create_agent` implements it out of the box).
4. **Shared state** — a common message history that every agent reads
   and appends to; this is how agents "hand work over" to each other.
5. **Structured output** — the supervisor's decision is a Pydantic
   model (`with_structured_output`), not free text → reliable routing.
6. **Conditional edges** — the graph branches based on the LLM's
   decision, including a rework loop (QA → Developer).
7. **Safeguards** — `recursion_limit` against infinite loops; tools
   return error messages instead of raising exceptions; code is checked
   with `ast.parse` **without being executed** (security!).
8. **Planning before acting** — the plan is structured output, the
   progress is code (JSON → markdown with checkboxes), a traceability matrix.
9. **Human-in-the-Loop** — `interrupt()` in separate nodes where a
   decision is expensive or irreversible; resume with `Command(resume=...)`.
10. **Audit trail** — every step leaves a plan and a result on disk;
    after the run its folder is the full story of what happened and why.

## Ideas for extending

- Real Jira via the Atlassian MCP server and draft PRs in git repositories
  (the prod mode — in progress, see [docs/prod-mode-plan.md](docs/prod-mode-plan.md)).
- Add a "DevOps" agent that simulates a deploy after QA approval.
- Give QA a tool that actually runs `pytest` in an isolated environment.
- Persistent memory across runs (a Postgres checkpointer) and context compression.
