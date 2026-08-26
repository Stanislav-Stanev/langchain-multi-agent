# Release Notes

Всички съществени промени по проекта се документират в този файл.
Форматът следва [Keep a Changelog](https://keepachangelog.com/), а
версиите — [Semantic Versioning](https://semver.org/).

Записите в **[Unreleased]** се добавят автоматично от pre-push hook-а
(Claude) при качване на feature клон. При release записите се
преместват под нов номер на версия с дата.

## [Unreleased]

### Добавено (Demo / Prod режим — стъпка 2: Jira през MCP)
- **Реална Jira в prod режим** през официалния Atlassian Remote MCP Server
  (`src/jira_mcp.py`, пакет `mcp`): Analyst ползва същия инструмент
  `get_ticket_details` (картата на тикета е в същия формат като mock-а, плюс
  статус/тип/линк; критериите за приемане от custom field или от секцията в
  описанието) и нов `search_tickets(jql)`. Автентикация с API token
  (`JIRA_EMAIL`/`JIRA_API_TOKEN`, Basic) или OAuth през `mcp-remote`;
  sync мост към async MCP сесията, retry с backoff при транспортни грешки,
  грешките към агента са текст. Бутон „Тест на връзката с Jira" в UI-я.
  Git интеграцията (workspace, draft PR) е следващата стъпка.

### Добавено (Demo / Prod режим — стъпка 1)
- **Режими** `APP_MODE=prod|demo` (по подразбиране prod; дропдаун в
  Streamlit): demo е учебният режим с примерните тикети; prod е избираем,
  а интеграциите му (Jira през Atlassian MCP, git репозитории, draft PR)
  идват в следващите стъпки от `docs/prod-mode-plan.md` — без конфигурация
  UI-ят показва какво липсва и не пуска run.
- **Планове преди работа**: възел `dev_plan` (план за имплементация —
  критерии за приемане, стъпки с файлове) преди Developer и `qa_plan`
  (тест-план по критериите) преди QA — структуриран LLM изход, JSON като
  източник на истина, markdown с чекбоксове; агентите отчитат прогреса с
  инструментите `update_plan_step` / `update_test_case`; при rework планът
  получава секция „Rework N"; матрица на проследимост в `traceability.md`.
- **Human-in-the-Loop порти** (`HITL_GATES`): `approve_plan` (одобрение
  на плана: одобри / поискай промени с указания / прекрати),
  `approve_publish` (преди публикуване в prod) и `escalation_gate`
  (при DoD провал или изчерпан rework лимит: повторен опит с указания или
  край) — LangGraph `interrupt()` + `Command(resume=...)`; конзолата пита
  интерактивно, UI-ят показва панел с бутони; `HITL_AUTO_APPROVE=1` за CI.
- **Проследяване на run-а** в `runs/<дата_час>_<тикет>/`: план преди и
  изпълнение след всяка стъпка (`steps/NN-<възел>.md`, `index.md`),
  `STATUS.md`, `run.jsonl`, артефактите на фазите, `hitl-decisions.md`,
  `summary.json` (вкл. токени/цена); нови модули `src/modes.py`,
  `src/plans.py`, `src/hitl.py`, `src/dod.py`, `src/toolsets.py`,
  `src/run_tracker.py`, `src/step_plans.py`, `src/run_context.py`.
- Тестове за всичко горе (унит, интеграционни, E2E с HITL през
  `InMemorySaver`); `docs/plans/` — план преди всяка стъпка от имплементацията.

### Променено
- Системата е преименувана на **Multi-Bot** (заглавия в UI и конзолата,
  документация).
- Архитектурата е детерминизирана (improvement.md): Supervisor (LLM)
  прави само еднократен triage — входна точка или FINISH; всичко след
  това е код върху типизирани артефакти (спецификация, код, QA присъда).
  Всяка фаза има Definition of Done, проверяван от кода (един повторен
  опит, после ескалация към човек); присъдата на QA е структурирана
  (`QAVerdict`) и rework цикълът е ограничен до `MAX_REWORK` пъти
  (по подразбиране 3), после `ESCALATED` вместо вечен цикъл.

### Добавено
- Настройки за устойчивост и разходи (всички през .env): различен модел
  за всяка роля (`MODEL_SUPERVISOR`, ...), резервен модел при срив на
  основния (`MODEL_FALLBACK`), retry/timeout на LLM извикванията, твърд
  бюджетен лимит на изпълнение (`MAX_COST_USD_PER_RUN`) със стоп на
  стрийма при надвишаване.
- Checkpointing в SQLite (`CHECKPOINT_SQLITE_PATH`): всяка стъпка на
  графа се записва и прекъснат run се възобновява от последната записана
  стъпка (thread per задача, `THREAD_ID` за конзолата).
- `draw_graph.py` — локална Mermaid диаграма на графа, без LLM извиквания
  и без външни услуги.
- Eval пакет (`tests/evals/`): golden dataset (YAML) + LLM-as-judge с
  реални LLM извиквания; изключен от стандартния тестов run, пуска се
  изрично с `pytest -m eval`.
- Линтер ruff (`ruff.toml`) и GitHub Actions CI (ruff + pytest + 85%
  покритие на `src/`) като задължителна проверка за PR; server-side
  branch protection на `main`.
- Двуезична поддръжка (български/английски): всички потребителски
  текстове, промптове на агентите и mock данни съществуват на двата
  езика (`src/i18n.py`, `APP_LANG=bg|en`); езиков превключвател в
  Streamlit UI; огледална английска документация (`README.en.md`).
- Работен процес с feature клонове: `main` е продукционен и защитен от
  директни комити/push-ове (git hooks); нова функционалност влиза през
  Pull Request.
- `CHANGELOG.md` (този файл) — release notes за всяка завършена
  функционалност, поддържан автоматично от pre-push hook-а.
- Пълен pytest пакет (`tests/`) — покрива целия workflow без реални LLM
  извиквания: unit, интеграционни и E2E тестове (happy path, rework
  цикъл и лимитът му, Definition of Done, triage входовете, streaming
  контракт, двуезични текстове). Стартиране: `python -m pytest`.

### Премахнато
- LangSmith tracing и LangGraph Studio — стекът е изцяло локален:
  визуализация чрез Streamlit UI и `draw_graph.py` (`langgraph.json`
  е изтрит).

## [0.1.0] - 2026-08-21

### Добавено
- Мултиагентна SDLC система (Supervisor + Analyst + Developer + QA) с
  LangChain 1.x + LangGraph 1.x — от тикет с изискване до прегледан и
  одобрен код, включително цикъл за поправки (QA → Developer).
- Конзолна визуализация на живо (`main.py`): решения на супервайзора с
  причини, извиквания на инструменти с аргументи и резултати, финални
  отговори на агентите.
- Streamlit уеб UI (`app.py`) с жива статус лента и превключвател на
  доставчика: Anthropic Claude (облак) / Ollama (локално, напълно офлайн).
- Поддръжка на LangGraph Studio (`langgraph.json`) и LangSmith tracing
  (EU регион).
- Отчет за консумирани токъни и приблизителна цена в USD по модели,
  с отчитане на prompt кеширането.
- Pre-push git hook: Claude Code (headless) автоматично синхронизира
  документацията преди качване в remote.
