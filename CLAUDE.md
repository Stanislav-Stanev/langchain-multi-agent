# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Bulgarian-language **teaching project**: a multi-agent SDLC system (Supervisor + Analyst + Developer + QA) built on LangChain 1.x + LangGraph 1.x. All code comments, docstrings, prompts, and console output are in Bulgarian and deliberately verbose/explanatory — keep that style when editing. There are no tests and no linter.

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

# Activate the docs-sync pre-commit hook (one-time after cloning)
git config core.hooksPath .githooks
```

## Git hooks

`.githooks/pre-commit` runs `claude -p` (headless) before every commit to check whether the staged changes made CLAUDE.md / README.md / requirements.txt inaccurate, and stages any updates into the same commit. It is opt-in via `git config core.hooksPath .githooks`. Skip it with `git commit --no-verify` (all hooks) or `SKIP_DOCS_UPDATE=1 git commit` (this hook only); it also no-ops when only the three doc files are staged, and guards against nested invocation via `CLAUDE_DOCS_HOOK_RUNNING`.

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
