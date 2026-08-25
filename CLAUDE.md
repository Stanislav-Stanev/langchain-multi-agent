# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Multi-Bot** — a Bulgarian-language **teaching project**: a multi-agent SDLC system (Supervisor + Analyst + Developer + QA) built on LangChain 1.x + LangGraph 1.x. "Multi-Bot" is the system's name; use it in all user-facing titles and docs. Code comments and docstrings are in Bulgarian and deliberately verbose/explanatory — keep that style when editing (tests included). The linter is ruff (`ruff.toml`), configured to tolerate that style (E501 ignored — long Bulgarian comments/prompts are fine).

**Tests:** `tests/` is a pytest suite covering the whole workflow with **zero real LLM calls** — triage decisions, QA verdicts and workers are scripted test doubles (see `tests/conftest.py`). Four layers: unit (tools/i18n/config/graph building blocks), integration (real `create_agent` agents + real mock tools driven by a fake tool-calling model), E2E (the real graph: happy path, rework loop + limit, DoD retry/escalation, triage entries, streaming contract, bilingual reasons), and **evals** (`tests/evals/`, marker `eval`, golden YAML cases + LLM-as-judge through `get_llm()` — real LLM costs, excluded by default via `pytest.ini` `-m "not eval"`, run explicitly with `pytest -m eval`). An autouse fixture clears `APP_LANG`/`MAX_REWORK` so every test starts at defaults. Lint: `ruff check .` (config `ruff.toml`); CI (`.github/workflows/ci.yml`) gates PRs on ruff + pytest + 85% coverage of `src/`. Run: `.\.venv\Scripts\python.exe -m pytest`. When changing graph/agents/tools behavior, keep the suite green and extend it.

**Bilingual (bg/en):** all user-facing strings live in `src/i18n.py` (`t("key")`, language from `APP_LANG` env, `bg` default) — never add user-facing literals directly in `main.py`/`app.py`; add a key to `STRINGS` with **both** languages (import-time validation fails on half-translated keys). Agent prompts (`agents.py`, `graph.py`) and mock tool data (`tools.py`) are per-language dicts. Prompts are fixed at `build_graph()` time (language is part of the Streamlit graph cache key); tool texts resolve at call time. Docs are mirrored: `README.md` (bg) ↔ `README.en.md` (en) — edits to one must be mirrored in the other. Code comments stay Bulgarian-only by design.

## Commands

Windows + PowerShell project. Use the venv executables directly (no activation needed):

```powershell
.\.venv\Scripts\pip.exe install -r requirements.txt

# Console run with live step-by-step visualization (default task = ticket DEV-101)
.\.venv\Scripts\python.exe main.py
.\.venv\Scripts\python.exe main.py "своя задача"

# Streamlit web UI (http://localhost:8501)
.\.venv\Scripts\streamlit.exe run app.py

# Mermaid diagram of the graph structure (local, no LLM calls)
.\.venv\Scripts\python.exe draw_graph.py

# Tests (fast, offline — no LLM calls) + lint
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .

# Evals — golden cases with REAL LLM calls (real cost; excluded from the default run)
.\.venv\Scripts\python.exe -m pytest -m eval

# Offline model (one-time, ~5 GB)
ollama pull qwen3:8b

# Activate the docs-sync pre-push hook (one-time after cloning)
git config core.hooksPath .githooks
```

## Branching workflow (enforced by hooks)

**`main` is production.** Never commit or push to it directly — `.githooks/pre-commit` blocks commits on main and `.githooks/pre-push` blocks pushes to main. All work goes on a `feature/<name>` branch, pushed, then merged via PR (`gh pr create --fill` → `gh pr merge --squash --delete-branch`). Emergency escape hatches: `SKIP_MAIN_GUARD=1` or `--no-verify`.

Release notes live in `CHANGELOG.md` (Keep a Changelog format, Bulgarian). Completed user-facing functionality gets an entry under `[Unreleased]` — the pre-push hook adds these automatically; on release, entries move under a version number.

## Git hooks

`.githooks/pre-push` also runs `claude -p` (headless) before every push to check whether the commits being pushed made CLAUDE.md / README.md / README.en.md / requirements.txt / CHANGELOG.md inaccurate, that the two READMEs stay mirrored, and that new user-facing strings have both bg+en versions in `src/i18n.py`. If so, it creates a separate `docs-sync:` commit and **aborts the push with exit 1** — the user re-runs `git push`, which passes immediately (a `docs-sync:` HEAD skips the check). Opt-in via `git config core.hooksPath .githooks`. Skip with `git push --no-verify` (all hooks) or `SKIP_DOCS_UPDATE=1 git push` (docs sync only). It also no-ops when the pushed commits touch only the doc files, when the doc files have uncommitted changes (never commits someone's WIP), and guards against nested invocation via `CLAUDE_DOCS_HOOK_RUNNING`.

## Architecture

Supervisor pattern on one LangGraph `StateGraph`, productionized per `improvement.md`: the LLM decides **only** what code cannot. `supervisor` (Pydantic structured output `SupervisorDecision`) does a **one-time triage** — entry point (`analyst`/`developer`/`qa`) or FINISH for non-software tasks. Everything after is deterministic code over typed state artifacts (`TeamState`: `spec`, `code`, `qa_verdict`, `rework_count`, `final_status`): analyst → developer → qa, each phase gated by a code-checked Definition of Done (empty spec / missing ```python block / `ast.parse` failure → **one** self-retry with the concrete problem injected, then `final_status="ESCALATED"`). QA's ReAct report is distilled into a structured `QAVerdict` by a second LLM call; routing reads the verdict, never free text: APPROVED → END, NEEDS_WORK → developer up to `MAX_REWORK` (env, default 3) times, then ESCALATED. Every node writes `next`+`reason` into state; one shared `route_next` conditional edge reads it. Workers are `create_agent` ReAct agents with mock tools (`src/tools.py`: fake Jira, coding standards, `ast.parse` syntax check — code is never executed).

Resilience/cost knobs (all env, read per call): per-role models `MODEL_SUPERVISOR`/`MODEL_ANALYST`/`MODEL_DEVELOPER`/`MODEL_QA` (fallback `MODEL_NAME`; Ollama analog `OLLAMA_MODEL_<ROLE>`), `LLM_MAX_RETRIES` (3) and `LLM_TIMEOUT_SECONDS` (120) on the Anthropic client, `MODEL_FALLBACK`/`OLLAMA_MODEL_FALLBACK` (empty=off) — a second-model fallback chain built by `make_structured_llm()` in `src/graph.py` for the two structured decisions only (triage + QA verdict; ReAct agents rely on client retries), `MAX_COST_USD_PER_RUN` (0=off) — checked by main.py/app.py between stream events via `budget_exceeded()` in `src/config.py`, and `CHECKPOINT_SQLITE_PATH` (empty=off) — `get_checkpointer()` builds a `SqliteSaver`, `build_graph(checkpointer=...)` compiles with it, main.py/app.py then pass `thread_id` (env `THREAD_ID` or sha1 of the task) so an interrupted run resumes on the same thread.

GitHub `main` also has server-side branch protection: the CI check `test` is required and force-pushes/deletions are blocked (set via `gh api .../branches/main/protection`).

- `src/config.py` — `get_llm()` factory. Reads `LLM_PROVIDER` from env **on every call**: `anthropic` (default, `claude-opus-5`) or `ollama` (local `qwen3:8b`, fully offline). Also holds `MODEL_PRICES_USD_PER_MTOK` + `estimate_cost_usd()` for the cost report.
- `src/graph.py` — the graph. Contract to preserve: all LLM-dependent objects (agents, supervisor LLM) are created **inside** `build_graph()`, not at module level — this is what lets the UI switch providers at runtime and lets the module import without any API key (a test guards this). Don't hoist them out.
- `main.py` / `app.py` — visualization only; the graph itself never prints. Both stream with `stream_mode="updates", subgraphs=True` (subgraphs=True exposes the workers' inner tool calls) and attach a `UsageMetadataCallbackHandler` for the token/cost summary (it also captures the supervisor's calls, which never appear in the stream).

## Gotchas (each one cost a debugging session)

- **Thinking blocks:** Opus 5 returns content as a list of blocks (thinking + text). Worker results are re-injected as `HumanMessage`s, and the API rejects thinking blocks in non-assistant messages — always route agent output through `extract_text()` (`src/graph.py`).
- **No LangSmith / LangGraph Studio.** The project deliberately has no external tracing/observability dependency — everything runs locally (Streamlit UI for live visualization, `draw_graph.py` for the graph diagram). The `langsmith` package remains only as a transitive dependency of langchain-core; never enable `LANGSMITH_TRACING` or add Studio configs back.
- **Ollama models must support tool calling** (qwen3, llama3.1). The locally available BgGPT is Gemma-2-based and does not — agents' tools and the supervisor's structured output fail on it.
- **Console encoding:** `main.py` calls `sys.stdout.reconfigure(encoding="utf-8")` — without it Cyrillic breaks when output is redirected on Windows.
- **Streamlit reruns wipe output:** `app.py` stores every graph event in `st.session_state.events` and replays them on rerun. New UI output must go through `emit()`/`render_event()`, not bare `st.*` calls inside the run loop.
- Streamlit can inherit `LLM_PROVIDER` from the launching shell; `load_dotenv()` does not override it. `app.py` reads the sidebar default from `dotenv_values()` for this reason.
