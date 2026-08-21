🇧🇬 [Български](README.md) | 🇬🇧 **English**

# Multi-Agent SDLC System with LangChain + LangGraph

A teaching project: a multi-agent system that automates part of the
**SDLC process** (Software Development Life Cycle) — from a requirement
in a ticket to reviewed and approved code.

> The interface, console output, and agents are bilingual: set
> `APP_LANG=bg|en` in `.env` (or use the language switch in the web UI).
> Code comments are intentionally in Bulgarian — this is a Bulgarian
> teaching project; this file is the English mirror of README.md.

## Architecture — the "Supervisor" pattern

```
                       +--------------+
             user ---> |  SUPERVISOR  | <--- returns after every agent
                       | (Team Lead)  |
                       +--------------+
                        /      |      \
                       v       v       v
                 +---------+ +-----------+ +--------+
                 | ANALYST | | DEVELOPER | |   QA   |
                 +---------+ +-----------+ +--------+
                requirements  writes code   review
```

| Agent          | SDLC phase     | Tools                                     |
|----------------|----------------|-------------------------------------------|
| **Supervisor** | management     | none — only decides who works next        |
| **Analyst**    | Requirements   | `get_ticket_details` (mock Jira)          |
| **Developer**  | Implementation | `get_coding_standards` (company rules)    |
| **QA**         | Testing/Review | `check_code_syntax`, `run_test_checklist` |

The flow is **analyst → developer → qa → FINISH**, but each step is
decided by the Supervisor (an LLM) itself. If QA returns `NEEDS_WORK`,
the Supervisor sends the task back to the Developer — i.e. the system
has a rework loop, just like a real team.

## Project structure

```
langchain-multi-agent/
├── main.py              # entry point — runs the graph (console)
├── app.py               # Streamlit web UI with live visualization
├── langgraph.json       # LangGraph Studio configuration
├── requirements.txt     # dependencies
├── .env.example         # settings template (copy as .env)
├── CHANGELOG.md         # release notes (Keep a Changelog)
├── README.en.md         # this file — English mirror of README.md
├── .githooks/
│   ├── pre-commit       # hook: protects main from direct commits
│   └── pre-push         # hook: protects main + Claude syncs docs/changelog
└── src/
    ├── config.py        # settings + LLM client factory (anthropic/ollama)
    ├── i18n.py          # bilingual texts (bg/en) + t() helper
    ├── tools.py         # the tools (mock Jira, linter, QA checklist)
    ├── agents.py        # the three worker agents (ReAct)
    └── graph.py         # supervisor + LangGraph graph assembly
```

Recommended reading order for learning:
`tools.py` → `agents.py` → `graph.py` → `main.py`

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

# 4. Run with the demo task (ticket DEV-101)
python main.py

# ...or with your own task
python main.py "Implement ticket DEV-102"
python main.py "Write a function that reverses a string"

# Web UI (language and provider switches in the sidebar)
streamlit run app.py
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
  and, if they made CLAUDE.md, README.md, requirements.txt or
  CHANGELOG.md inaccurate, updates them in a **separate docs-sync
  commit** (adding a release-notes entry for completed functionality).
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

## Ideas for extending

- Replace the mock tickets with a real Jira/GitHub API.
- Add a "DevOps" agent that simulates a deploy after QA approval.
- Give QA a tool that actually runs `pytest` in an isolated environment.
- Add memory (a LangGraph checkpointer) to continue conversations.
- Visualize the graph: `build_graph().get_graph().draw_mermaid()`.
