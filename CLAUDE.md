# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Bulgarian-language **teaching project**: a multi-agent SDLC system (Supervisor + Analyst + Developer + QA) built on LangChain 1.x + LangGraph 1.x. Code comments and docstrings are in Bulgarian and deliberately verbose/explanatory — keep that style when editing. There are no tests and no linter.

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

# LangGraph Studio dev server (visual debugger)
.\.venv\Scripts\langgraph.exe dev
# Studio UI must be opened at the EU host (account is EU-region):
# https://eu.smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024

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

Supervisor pattern on one LangGraph `StateGraph`: user task → `supervisor` (LLM with Pydantic structured output `SupervisorDecision`) routes via conditional edge to `analyst` / `developer` / `qa` / END; every worker edge returns to `supervisor`. QA returning `NEEDS_WORK` sends the loop back to `developer`. Workers are `create_agent` ReAct agents with mock tools (`src/tools.py`: fake Jira, coding standards, `ast.parse` syntax check — code is never executed).

- `src/config.py` — `get_llm()` factory. Reads `LLM_PROVIDER` from env **on every call**: `anthropic` (default, `claude-opus-5`) or `ollama` (local `qwen3:8b`, fully offline). Also holds `MODEL_PRICES_USD_PER_MTOK` + `estimate_cost_usd()` for the cost report.
- `src/graph.py` — the graph. Two contracts to preserve:
  - All LLM-dependent objects (agents, supervisor LLM) are created **inside** `build_graph()`, not at module level — this is what lets the UI switch providers at runtime. Don't hoist them out.
  - Module-level `graph = build_graph()` at the bottom is the entry point `langgraph.json` points at (LangGraph Studio). Keep it.
- `main.py` / `app.py` — visualization only; the graph itself never prints. Both stream with `stream_mode="updates", subgraphs=True` (subgraphs=True exposes the workers' inner tool calls) and attach a `UsageMetadataCallbackHandler` for the token/cost summary (it also captures the supervisor's calls, which never appear in the stream).

## Gotchas (each one cost a debugging session)

- **Thinking blocks:** Opus 5 returns content as a list of blocks (thinking + text). Worker results are re-injected as `HumanMessage`s, and the API rejects thinking blocks in non-assistant messages — always route agent output through `extract_text()` (`src/graph.py`).
- **`.env` must stay ASCII-only.** `langgraph dev` reads it with the Windows ANSI codepage (cp1251) and crashes on UTF-8 Cyrillic. Bulgarian docs for the settings live in `.env.example` instead.
- **LangSmith account is EU-region:** `LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com` is required in `.env`; without it every LangSmith call returns 403 Forbidden.
- **Ollama models must support tool calling** (qwen3, llama3.1). The locally available BgGPT is Gemma-2-based and does not — agents' tools and the supervisor's structured output fail on it.
- **Console encoding:** `main.py` calls `sys.stdout.reconfigure(encoding="utf-8")` — without it Cyrillic breaks when output is redirected on Windows.
- **Streamlit reruns wipe output:** `app.py` stores every graph event in `st.session_state.events` and replays them on rerun. New UI output must go through `emit()`/`render_event()`, not bare `st.*` calls inside the run loop.
- Streamlit can inherit `LLM_PROVIDER` from the launching shell; `load_dotenv()` does not override it. `app.py` reads the sidebar default from `dotenv_values()` for this reason.
