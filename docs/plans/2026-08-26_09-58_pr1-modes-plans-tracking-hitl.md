# План · PR 1 — режими, планове, проследяване, Human-in-the-Loop (demo flow)

Статус: DONE · Начало: 2026-08-26 09:58 · Край: 2026-08-26 11:00 · Главен план: [prod-mode-plan.md](../prod-mode-plan.md) §9

## Цел

Целият demo flow работи с новата топология (`init_run → supervisor → analyst → dev_plan → approve_plan →
developer → qa_plan → qa → finalize`), всяка стъпка оставя план + изпълнение в `runs/<дата_час>_<KEY>/steps/`,
Developer и QA работят по структурирани планове с чекбоксове, човек одобрява плана и решава при ескалация;
Prod режимът е избираем (default), но без конфигурация е ясно блокиран. Суитът е зелен, покритие ≥ 85%.

## Контекст / входове

- Одобрен главен план (§1–§10), проверени API-та: `create_agent(context_schema=…)`, `ToolRuntime`,
  `langgraph.types.interrupt/Command`, `InMemorySaver`.
- Съществуващи тестове (`tests/`) — контрактите, които трябва да останат зелени или да се адаптират съзнателно.

## Обхват

Всичко от §9 „PR 1". **Не-обхват:** Jira MCP клиент (PR 2), git workspace/publish/`approve_publish` (PR 3).

## Стъпки (критерий за готовност в скоби)

- [x] 1. `src/modes.py` (+ `tests/test_modes.py`): `get_mode` default prod, невалидно → `ValueError`, `runs_dir`, `parse_prod_repos`, `validate_prod_config`.
- [x] 2. `src/run_context.py`, `src/run_tracker.py`, `src/step_plans.py` (+ тестове): run директория с дата/час, `run.jsonl` с дедупликация, `STATUS.md`, `steps/NN-<node>.md` преди/след, `index.md`, `NullTracker`.
- [x] 3. `src/plans.py` (+ `tests/test_plans.py`): Pydantic схеми, рендери с чекбоксове, `apply_*`, `record_phase_end`, `add_rework`, `reset_for_rerun`, `render_traceability`, инструменти `update_plan_step`/`update_test_case` през `ToolRuntime[RunCtx]`.
- [x] 4. `src/dod.py` (+ `tests/test_dod.py`): `DemoDoD` = днешната логика; `ProdDoD` интерфейс (реален в PR 3).
- [x] 5. `src/hitl.py` (+ `tests/test_hitl.py`): `enabled_gates`, `parse_decision`, `make_gate_node` с `interrupt`, pass-through при изключена порта, auto-approve.
- [x] 6. `src/toolsets.py` (+ `tests/test_toolsets.py`): `make_mode_context`, `make_tools` (demo пълен; prod → ясна грешка до PR 2/3), prompt addenda.
- [x] 7. `src/agents.py`: `tools=`/`prompt_addendum=` параметри, `context_schema=RunCtx`, абзаци за плановете; integration тестовете зелени.
- [x] 8. `src/i18n.py`: всички нови ключове (bg+en).
- [x] 9. `src/graph.py`: state, `PIPELINE`/`ENTRY_NODE`, `init_run`/`dev_plan`/`approve_plan`/`qa_plan`/`escalation_gate`/`finalize`, `_traced`, `build_graph(mode=, hitl_gates=)`; `draw_graph.py` demo.
- [x] 10. Тестове: conftest (`APP_MODE=demo`, `RUNS_DIR=tmp`, plan/test-plan LLM дубльори, `FakeWorkerAgent(**kwargs)`), `test_graph_units.py`, `test_workflow_e2e.py` (нови сценарии вкл. HITL), evals `mode="demo"`.
- [x] 11. `main.py` (HITL през stdin, run_dir/планове в изхода) и `app.py` (дропдаун Prod default, HITL чекбоксове + панел, `plan`/`artifacts`/`hitl` събития, prod блокиран без конфигурация).
- [x] 12. `.gitignore`, `.env.example`, README.md + README.en.md, CHANGELOG.md, CLAUDE.md.
- [x] 13. `ruff check .` + `pytest` зелени (305 теста, покритие 97%), `draw_graph.py` (12 възела), реален demo run през CLI (auto-approve) и Streamlit AppTest (prod default → грешка + Run забранен; demo → Run активен, `publish` портата скрита).

## Рискове / допускания

- `interrupt()` изисква checkpointer → без конфигуриран SQLite ползваме `InMemorySaver` (валидно в един процес).
- Streamlit: interrupt/resume през rerun-и — състоянието на портата живее в `st.session_state`.
- Съществуващите E2E тестове проверяват точен маршрут/имена на съобщения → адаптират се съзнателно (нови възли).

## Definition of Done

Всички чекбоксове горе; `pytest` + `ruff` зелени; demo run произвежда пълната `runs/<дата_час>_DEV-101/` структура;
PR отворен към `main` с описание и линк към този план.

## Изпълнение (допълва се по време на работа)

- 09:58 — планът е записан; започва стъпка 1.
- 10:05 — потвърдени на живо API-тата (`ToolRuntime[RunCtx]` скрит от схемата, `interrupt` → `__interrupt__`, `Command(resume)`).
- 10:20 — стъпки 1–9 написани (`modes`, `run_context`, `run_tracker`, `step_plans`, `plans`, `dod`, `hitl`, `toolsets`, `agents`, `i18n`, `graph`); `draw_graph.py` показва 12 възела.
- 10:30 — стъпка 10: conftest + всички тестове; 302 теста зелени, покритие 97%. Отклонения: Windows `PermissionError` при `os.replace` → retry; рендер само на променената стъпка; помощниците `plan()/test_plan()` преименувани (pytest ги събираше).
- 10:36 — стъпка 13 (реален demo run, auto-approve): **бъг**, който тестовете не хващат — LangGraph изпълнява няколко `update_plan_step` паралелно (нишки) и общият `implementation-plan.json.tmp` дава `PermissionError`; трасирането го записа в `steps/06-developer.md` (FAILED). Поправка: уникален tmp файл + lock на run директория около read-modify-write; regression тест `TestConcurrentWrites`.
- 10:44–10:56 — втори реален run: 10 стъпки, десетки паралелни `update_plan_step`/`update_test_case` без грешка; планът отметнат 4/5 (S5 blocked - агентът честно отчита, че няма среда за pytest), тест-планът 13 случая с резултати; QA върна NEEDS_WORK два пъти → спиране от бюджетната спирачка ($3.86 > $3). Наблюдение: пълен demo run с Opus 5 струва ~$4-5 при строг QA. Козметичен бъг: `STATUS.md` показваше последното съобщение като „Задача" (update-ът носи само новите messages) → четем задачата от `run.json`; regression тест.
- 11:00 — стъпка 13 завършена; PR 1 готов за push. Prod интеграциите (Jira MCP, git, draft PR) са следващите два PR-а.
