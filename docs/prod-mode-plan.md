# План: Demo / Prod режим на Multi-Bot

> Jira през официалния Atlassian Remote MCP Server, реални git репозитории, draft PR след успешен flow,
> планове (.md) на Developer и QA, проследяване на целия run в `runs/<run_id>/`.
>
> Стъпка 0 от изпълнението копира този файл в репото като `docs/prod-mode-plan.md`; чекбоксовете в §9
> се отмятат при всяка стъпка — същият принцип, който въвеждаме и за агентите.

---

## 1. Контекст

Днес Multi-Bot е учебна система: Analyst → Developer → QA върху **статични mock тикети**
(`src/tools.py:_FAKE_TICKETS`, DEV-101/DEV-102). Кодът живее само като ```python блок в state-а, нищо не
се записва на диск, няма връзка с Jira или git. Целта е системата да стане използваема в реална среда:

- **Demo режим** — запазва днешното поведение (mock Jira, код в state, без git).
- **Prod режим (по подразбиране)** — тикетите идват от **реална Jira** през официалния Atlassian
  Remote MCP Server; Developer работи в **един или няколко git репозитория** (клонирани от `main`); при
  QA APPROVED се създава **draft PR** за всеки променен репозиторий.
- В **двата** режима: преди Developer се създава **план за имплементация (.md)**, преди QA — **тест-план
  (.md)**; и двата се обновяват по време на работа и в края на run-а; целият flow се проследява в
  `runs/<run_id>/` (журнал, статус, артефакти, обобщение).

Ограничения, които пазим (CLAUDE.md / improvement.md §8): Supervisor шаблон; LLM решава само това, което
кодът не може; всичко LLM-/мрежово-зависимо се създава **вътре** в `build_graph()`; инструментите връщат
грешки като текст (никога не хвърлят към LLM); нула LLM/мрежови извиквания в тестовата пирамида; всички
user-facing низове през `src/i18n.py` (bg+en); README.md ↔ README.en.md огледални; коментари на български.

### Проверени факти

- Среда: Node v24 + npx, `gh` 2.97 (логнат: Stanislav-Stanev), git 2.55, Python 3.12 venv, Windows 11.
- `mcp==1.29.1` (официалният Python MCP SDK) се инсталира чисто до `langchain-core 1.6.0`
  (алтернатива `langchain-mcp-adapters==0.3.2` — не е нужна, виж D2).
- Atlassian Remote MCP Server: `https://mcp.atlassian.com/v1/mcp`; неинтерактивна автентикация
  `Authorization: Basic base64(email:api_token)`; интерактивен OAuth 2.1 през
  `npx -y mcp-remote@latest https://mcp.atlassian.com/v1/mcp/authv2` (stdio; на Windows `npx.cmd`).
  Сайт: `myposgroup.atlassian.net` (`cloudId` приема и hostname — проверено).
- **Проверени на живо форми на отговорите**: резултатът е JSON текст в първия `TextContent`; с
  `responseContentFormat: "markdown"` `fields.description` е **чист текст** (не ADF); налични
  `priority.name`, `status.name`, `issuetype.name`, `webUrl`; `searchJiraIssuesUsingJql` връща
  `{"issues": {"nodes": [...]}}`; грешките са `{"error": true, "message": ...}`; **необвързан JQL се
  отказва** („Unbounded JQL queries are not allowed").
- Графът е синхронен (`agent.invoke`, `graph.stream`) → MCP (async) изисква sync мост.
- `ToolRuntime[Ctx]` параметърът на инструмент е скрит от LLM схемата и `agent.invoke(..., context=)`
  работи в закачените версии (проверено) → чист начин да подадем run-контекст на инструментите.

---

## 2. Решения (по подразбиране — кажете, ако искате друго)

| # | Решение | Избор | Защо |
|---|---------|-------|------|
| D1 | Jira автентикация | `JIRA_MCP_AUTH=api_token` (Basic email:token) по подразбиране; `oauth` (mcp-remote, stdio) като опция | Streamlit/CLI са неинтерактивни; Atlassian препоръчва API token за бекенди |
| D2 | MCP клиент | Официалният `mcp` Python SDK (`ClientSession.call_tool`, `streamablehttp_client` / `stdio_client`), **lazy import** | Нужни са 4 извиквания с фиксиран наш контракт; адаптерите генерират инструменти по схемата на сървъра, които пак трябва да обвием |
| D3 | Стабилен интерфейс на инструментите | `get_ticket_details(ticket_id)` има demo и prod имплементация със **същото име, сигнатура, docstring и формат на картата** | Промптовете и integration тестовете не се променят (improvement.md §2.6) |
| D4 | Кои репозитории | `PROD_REPOS` (env: `owner/name` или https URL, със запетаи) → UI мултиселект (по подразбиране всички); Developer сам решава кои да промени | Не гадаем URL; потребителят конфигурира |
| D5 | Къде живеят плановете | Локално в `runs/<run_id>/` + пълният им текст в **тялото на PR-а**; **не** се комитват в целевия репозиторий | Генерирани документи замърсяват репото; PR body дава пълна проследимост |
| D6 | Кога/къде се публикува | Детерминистичен възел `finalize` (единствен изход на графа); при QA APPROVED в prod: commit → push → `gh pr create --draft` за всеки репозиторий с diff; идемпотентно по име на branch | Draft PR е самата „човешка порта" (improvement.md §2.3); един проследим възел, логнат в `run.jsonl` |
| D7 | Jira write-back | `JIRA_WRITE_BACK=0`; при `1` — коментар в тикета с PR линковете и резюме на плановете (без transition) | Четене първо; писането е opt-in |
| D8 | Планове през структуриран изход | Възли `dev_plan`/`qa_plan` (по едно `with_structured_output` извикване → Pydantic → код рендира md с чекбоксове) + инструменти `update_plan_step`/`update_test_case`; JSON е източник на истина, md се пре-рендира | LLM решава *съдържанието*, кодът пази *формата* и прогреса; идемпотентно при resume |
| D9 | Режим в CLI | Само env `APP_MODE` (`$env:APP_MODE="demo"; python main.py`) | Консистентно с всички други knobs; без argparse |
| D10 | Developer DoD в prod | непразен diff в ≥1 репозиторий **и** всеки променен `.py` минава `ast.parse` **и** ≥1 стъпка от плана е `done` | Реален артефакт вместо ```python блок |
| D11 | Невалиден `APP_MODE` | `ValueError` (не тиха подмяна) | Подмяна към prod при typo би пуснала реални странични ефекти |
| D12 | Липсваща prod конфигурация | UI: `validate_prod_config()` → грешка + Run забранен; `build_graph()` в prod хвърля `RuntimeError` (както при липсващ API ключ днес) | Грешките изплуват рано и ясно |
| D13 | Статуси | `final_status` остава SDLC изходът (APPROVED/ESCALATED/NO_ACTION); нов `publish_status` (PUBLISHED / PUBLISH_FAILED / SKIPPED) | Не смесваме „QA одобри" с „PR се създаде"; съществуващите тестове за `final_status` остават |
| D14 | Rework | `qa → developer` без ре-планиране; планът получава секция „Rework N" с `QAVerdict.issues`; тест-планът се нулира за „Run N" | LLM извикване по-малко; ID-тата на стъпките, към които сочи тест-планът, не се пренаписват |
| D15 | Доставка | Една тема, **3 последователни PR-а** (§9); суитът е зелен след всеки | Ревюируемост; CI gate 85% |
| D16 | Human-in-the-Loop механизъм | LangGraph `interrupt()` в **отделни gate възли** + checkpointer (задължителен при включен HITL; без `CHECKPOINT_SQLITE_PATH` → автоматичен `InMemorySaver` за процеса) + `Command(resume=…)` | Отделният възел е видим в стрийма, resume изпълнява само него; interrupt без checkpointer е невъзможен |
| D17 | Кои порти и къде | `plan` (след `dev_plan`, преди Developer), `publish` (преди commit/push/PR), `escalation` (при ESCALATED вместо тих край) — виж §3a | Портите са там, където решението е скъпо, необратимо или изисква човешка преценка (best practice: „approve before irreversible/expensive work") |
| D18 | Портите по режим | `HITL_GATES` env; default prod = `plan,publish,escalation`, demo = `plan,escalation` (`publish` не съществува в demo); UI чекбоксове за всяка порта | Prod без човек пред push е недопустимо; demo показва концепцията, но без git |
| D19 | План преди ВСЯКА стъпка | Обвивката `_traced` пише `steps/NN-<node>.md` **преди** изпълнението (секция „План": цел, входове, очакван изход, DoD) и го **допълва след** него („Изпълнение": какво се случи, продължителност, инструменти, резултат на DoD, `next`/`reason`). За `dev_plan`/`qa_plan` планът е LLM-структуриран; за останалите — детерминистичен шаблон | Еднообразна следа „планирано → изпълнено → резултат" за всяка стъпка; след края на flow-а `runs/<run>/steps/` + `index.md` са пълният одит |
| D20 | Име на run директорията | `runs/<YYYY-MM-DD_HH-MM-SS>_<KEY или task>/` (дата и час първи → хронологична подредба); при checkpointer се добавя `_<thread_id[:8]>` | Изискване за папки с дата и час; сортиране в файловата система |

---

## 3. Целева архитектура (една топология за двата режима)

```text
START → init_run → supervisor (triage, 1 LLM)
                      ├→ analyst ─→ dev_plan ─→ [approve_plan] ─→ developer ─→ qa_plan ─→ qa ─┬→ [approve_publish] ─→ finalize → END
                      ├→ dev_plan (готов spec)      ↑  „промени"       ↑                     │  APPROVED (prod)
                      ├→ qa_plan  (готов код)       └──────────────────┘ NEEDS_WORK ─────────┘  (≤ MAX_REWORK)
                      └→ finalize (не е софтуерна задача)
   DoD ескалация / rework лимит / грешка в workspace → [escalation_gate] ─┬→ retry (обратно към възела, с указания)
   [ … ] = Human-in-the-Loop порта (interrupt), включва се с HITL_GATES     └→ abort → finalize (ESCALATED)
```

- **`init_run`** (чист код): извлича `ticket_key` (regex `\b[A-Z][A-Z0-9]+-\d+\b`), определя `run_id`
  (`thread_id` от config при checkpointer; иначе `<KEY|TASK>_<YYYYmmdd-HHMMSS>_<sha1[:6]>`), създава
  `runs/<run_id>/` + `run.json`. В prod: за всеки избран репозиторий `prepare()` (clone/fetch) +
  `start_run("multibot/<KEY|task>-<run_id[:8]>")` от `origin/<PROD_BASE_BRANCH>`; грешка →
  `next="finalize"`, `final_status="ESCALATED"`, `reason=t("route_workspace_failed")`. При resume
  (`run_id` вече в state) е no-op.
- **`supervisor`**: непроменен triage; целта се превежда през
  `ENTRY_NODE = {analyst→analyst, developer→dev_plan, qa→qa_plan, FINISH→finalize}` — планът се прави
  **преди** Developer при всеки вход.
- **`dev_plan` / `qa_plan`**: по едно структурирано извикване (`make_structured_llm("developer",
  ImplementationPlan)` / `("qa", TestPlan)`), DoD (празен план → 1 повторен опит → ESCALATED) чрез
  съществуващия `_dod_failure`; рендираният план влиза в историята като `HumanMessage(name="dev_plan")`,
  за да вижда Developer ID-тата на стъпките; `qa_plan` получава списъка AC-ID-та, за да съвпадат
  референциите (невалидни → `criterion_ref="?"`, флагнати в матрицата).
- **`developer`** → `qa_plan`, ако още няма `test_plan` в state; при rework → директно `qa`.
- **`finalize`** (нула LLM): `traceability.md`, `summary.json`; при `final_status=APPROVED` в prod вика
  publisher-а (за всеки репозиторий с промени: commit, push, `gh pr create --draft`; чист репозиторий се
  пропуска; при `JIRA_WRITE_BACK=1` коментар в тикета). Нищо не хвърля: `publish_status`,
  `pr_urls`, `publish_errors` → state + summary. Само той пише `next="FINISH"`; `route_next` не се пипа.
- `WORKERS` остава `["analyst","developer","qa"]` (обвързан със `SupervisorDecision`); нов
  `PIPELINE = ["analyst","dev_plan","developer","qa_plan","qa","finalize"]` за условните ребра;
  `_dod_failure` ескалира към `next="finalize"`.
- Обвивка **`_traced(name, fn)`**, поставена само в `build_graph()` (фабриките на възлите остават чисти за
  unit тестовете): мери време, увеличава `step_seq`, пише събитие в `run.jsonl`, записва артефактите от
  update-а (`spec`→`spec.md`, `code`→`code.py|code.diff`, `qa_verdict`→`qa-report.md`), пре-рендира
  `STATUS.md`.

### Нови полета в `TeamState` (`src/graph.py:217`)

`run_id`, `run_dir`, `mode`, `ticket_key`, `repos: list[str]`, `step_seq: int` (дедупликация в
`run.jsonl` при resume), `plan: dict` (`ImplementationPlan.model_dump()` + `history`), `test_plan: dict`,
`pr_urls: dict[repo → url]`, `publish_status: str`, `publish_errors: list[str]`, `hitl_decisions: list[dict]`,
`plan_revisions: int`, `retry_target: str` (към кой възел връща `escalation_gate`). `code` в prod = unified
diff по репозитории. `final_status` получава и `ABORTED` (човек прекрати на порта). Per-run данните са в **state** (checkpoint-friendly, видими в стрийма), не в closures —
графът е кеширан в Streamlit по `(provider, lang, mode, repos)`.

### Контекст за инструментите

Плановите инструменти получават `runtime: ToolRuntime[RunCtx]` (`RunCtx{run_id, run_dir, mode}`);
`_run_agent` подава `agent.invoke({...}, context=run_ctx_from_state(state))`; `create_agent(...,
context_schema=RunCtx)`. Repo/Jira инструментите са closures над обектите от `build_graph()`.

## 3a. Human-in-the-Loop (HITL)

### Принципи (AI Architect)

1. **Човекът решава там, където грешката е скъпа или необратима**, не на всяка стъпка — иначе
   системата става „кликай Approve" и вниманието се изчерпва (alert fatigue).
2. **Портата е отделен, детерминистичен възел** (`interrupt()` вътре), не код в агентски възел: видима е
   в стрийма като стъпка, resume изпълнява само нея, тестваема е изолирано, а LLM никога не „решава"
   дали да пита човека.
3. **Всичко, което човекът вижда, е артефакт на диска** (планът, diff-ът, причината за ескалация) — не
   само UI съобщение. Решението му (кой, кога, какво, коментар) се записва в `run.jsonl` и
   `hitl-decisions.md` → одит.
4. **Три изхода на всяка порта**: `approve` (продължи), `revise` (върни се назад с конкретни указания —
   инжектирани като `HumanMessage`), `abort` (приключи безопасно). Броят ревизии е ограничен
   (`HITL_MAX_REVISIONS`=2), после портата предлага само approve/abort.
5. **Fail-safe по подразбиране**: в prod портите са включени; изключването е изрично (`HITL_GATES=`).
   Неинтерактивна среда (CI, `HITL_AUTO_APPROVE=1`) записва в журнала, че решението е автоматично.

### Портите — къде точно и защо

| Порта (възел) | Позиция в графа | Какво вижда човекът | Решения | Защо тук |
|---------------|-----------------|---------------------|---------|----------|
| `approve_plan` | след `dev_plan`, преди `developer` (и при вход от triage направо в `dev_plan`) | `implementation-plan.md` (стъпки, файлове, AC покритие) + spec | approve → `developer`; revise(feedback) → `dev_plan` (ре-планиране с указанията, макс. 2); abort → `finalize` (`final_status=ABORTED`) | Планът е най-евтиният момент за корекция на посоката — преди Developer да изгори токъни и да пише в репозитория |
| `approve_publish` | след `qa` APPROVED, преди `finalize` публикува (само prod) | `code.diff` по репозитории, `qa-plan.md` с резултати, матрицата, `pr-body-*.md` (preview), целеви репозитории/branch | approve → publish; revise(feedback) → `developer` (брои се като rework, в лимита); abort/skip → `finalize` без publish (`publish_status=SKIPPED_BY_HUMAN`; промените остават локално в workspace) | Push към реален репозиторий и коментар в Jira са изходящи, видими за други хора действия — човек ги пуска |
| `escalation_gate` | вместо директен преход към `finalize` при: DoD провал след повторен опит, изчерпан `MAX_REWORK`, грешка при подготовка на workspace | причината (`reason`), последния артефакт/доклад, историята на опитите | retry(feedback) → връща се към провалилия се възел с нулиран DoD брояч / +1 rework, указанията са в историята; abort → `finalize` (`ESCALATED`) | Ескалацията означава „системата не може сама" — тихият край губи цялата свършена работа; човек често може да отблокира с едно изречение |

Неподходящи места (съзнателно **без** порта): след triage (грешен вход се вижда и коригира на
`approve_plan`), между `developer → qa_plan → qa` (QA е автоматичният ревюър — човекът гледа
резултата на `approve_publish`), преди всяко извикване на инструмент (шум).

### Механика

- `src/hitl.py`: `enabled_gates()` (env `HITL_GATES`, default по режим), `make_gate_node(name, payload_fn,
  on_approve, on_revise, on_abort)` → възел, който: пише `steps/NN-<gate>.md` (какво се иска),
  `decision = interrupt(payload)` (payload: gate, run_id, artifacts paths, markdown preview, options),
  валидира `decision` (`{"action": approve|revise|abort, "feedback": str, "by": str}`), записва го в
  `run.jsonl`/`hitl-decisions.md`, връща `next`+`reason`. Ако портата е изключена → възелът е
  pass-through (пише „auto-approved (gate disabled)").
- Портите се добавят в `PIPELINE`; `route_next` е непроменен (портата пише `next`).
- Checkpointer е задължителен за `interrupt()`: `build_graph()` при включен HITL и `checkpointer=None`
  създава `InMemorySaver` (работи в рамките на процеса — достатъчно за Streamlit сесия/CLI). Resume:
  `graph.stream(Command(resume=decision), config={"configurable": {"thread_id": …}})`.
- **UI (app.py)**: стриймът спира на `__interrupt__` събитие → `emit({"kind": "hitl", …})` рендира панел
  с преглед на артефакта, поле за коментар и бутони Approve / Request changes / Abort; кликът записва
  `st.session_state.pending_resume` и продължава стрийма със `Command(resume=…)` през същия `emit()`
  цикъл (events се пазят → rerun-safe). Sidebar: чекбоксове за портите (default по режим).
- **CLI (main.py)**: при interrupt печата прегледа и чете от stdin (`[a] approve / [r] revise: текст /
  [q] abort`); без TTY → `HITL_AUTO_APPROVE=1` приема (и го логва), иначе спира с ясно съобщение.
- **State**: `hitl_decisions: list[dict]`, `plan_revisions: int`, `final_status` получава `ABORTED`;
  `publish_status` получава `SKIPPED_BY_HUMAN`.

## 3b. План преди всяка стъпка (`steps/`)

Всяка стъпка от графа оставя **един md файл**, написан на две части — преди и след изпълнението:

```text
runs/2026-08-26_14-05-33_DEV-101/
  index.md                     хронологичен списък: NN | възел | статус | продължителност | next | линк
  steps/
    01-init_run.md
    02-supervisor.md
    03-analyst.md
    04-dev_plan.md             планът тук е LLM-структурираният ImplementationPlan (+ implementation-plan.md)
    05-approve_plan.md         HITL: какво е поискано, решението, коментарът, кой/кога
    06-developer.md
    07-qa_plan.md
    08-qa.md
    09-approve_publish.md
    10-finalize.md
  hitl-decisions.md            всички човешки решения на едно място
```

Шаблон на `steps/NN-<node>.md` (best practices за планове: цел → контекст → обхват/не-обхват →
стъпки с критерии → рискове → DoD → статус/време; после фактите от изпълнението):

```markdown
# Стъпка NN · <node> · <агент/код/HITL>
Статус: PLANNED | IN_PROGRESS | DONE | FAILED | SKIPPED   Начало: … Край: … Продължителност: …

## План (написан преди изпълнението)
- Цел: … (1 изречение)
- Входове: spec.md / implementation-plan.md / история (N съобщения)
- Обхват: …   Не-обхват: …
- Стъпки:  - [ ] … (критерий за готовност)
- Рискове/допускания: …
- Definition of Done: …

## Изпълнение (допълва се след стъпката)
- Резултат: … (кратко)   Артефакти: spec.md, code.diff …
- Инструменти: get_ticket_details ×1, write_repo_file ×3 …
- DoD: покрит / непокрит (проблем: …) → повторен опит N
- Решение: next=<...> · причина: <reason>
- Отклонения от плана: …
```

Кой пише какво: `_traced` пише „План" от детерминистичен шаблон за всеки възел (за `dev_plan`/`qa_plan`
вгражда LLM плана след генерирането), а „Изпълнение" — от update-а на възела, tool_calls от callback-а
и DoD резултата. `index.md` и `STATUS.md` се пре-рендират след всяка стъпка. При rework/повторен опит
същият възел получава нов файл (`11-developer.md`) — историята не се презаписва.

Същата дисциплина за **самата имплементация**: всяка стъпка от §9 получава `docs/plans/<YYYY-MM-DD_HH-MM>_<стъпка>.md`
(план преди, отметки след), а главният план (този файл) отмята §9.

### Run директория `runs/<run_id>/` (двата режима; `RUNS_DIR`, default `runs/`)

```text
run.json                  run_id, режим, тикет, задача, старт, език, доставчик/модели
run.jsonl                 append-only журнал: ts, seq, node, next, reason, duration_s, rework_count, dod_retries, final_status, hitl
index.md                  хронологичен списък на стъпките (виж §3b)
steps/NN-<node>.md        план преди + изпълнение след всяка стъпка (§3b)
hitl-decisions.md         човешките решения: порта, действие, коментар, кой, кога (§3a)
STATUS.md                 „жив" статус: режим, тикет, текуща фаза, rework n/max, DoD опити, броячи план/тестове, чакаща HITL порта, финален статус, PR линкове
spec.md                   артефакт на Analyst
implementation-plan.json  източник на истина (стъпки, статуси, бележки, history)
implementation-plan.md    рендиран: - [ ] todo / - [~] in_progress / - [x] done / - [!] blocked; „Прогрес в края на фаза"; „Rework N"
qa-plan.json / qa-plan.md тест-случаи ↔ критерии ↔ метод ↔ статус/резултат по run
code.py | code.diff       demo: извлеченият код; prod: unified diff
qa-report.md              QA докладът + структурираната присъда
traceability.md           матрица AC ↔ стъпки ↔ тест-случаи ↔ резултат
pr-body-<repo>.md         тялото на PR-а (prod)
summary.json              финал: статуси, маршрут, стъпки, токени/цена (добавя се от main.py/app.py), PR URL-и, продължителност
```

Всички записи са идемпотентни (пре-рендиране от JSON/state; атомарни `tmp + os.replace`; журналът
дедупира по `seq`; history записите са с ключ `(kind, node, attempt, rework)`).

---

## 4. Промени файл по файл

### 4.1 Нови файлове

| Файл | Съдържание |
|------|-----------|
| `src/modes.py` | `MODES=("demo","prod")`, `get_mode()` (`APP_MODE`, default `prod`, невалидно → `ValueError`), `runs_dir()`, `workspace_dir()`, `RepoRef{owner,name,url}` + `RepoRef.parse()` + `parse_prod_repos(value)` (дубликати/празни се махат), `ProdConfig.from_env()`, `validate_prod_config(mode, repos_selected) -> list[str]` (i18n ключове на проблемите: няма `PROD_REPOS`, няма избран репозиторий, липсват Jira креденшъли за избраната автентикация) |
| `src/run_context.py` | `RunCtx` dataclass + `run_ctx_from_state(state)` |
| `src/run_tracker.py` | `RunTracker(run_dir)`: `start(root, run_id, *, mode, ticket_key, task, lang)`, `from_state(state) -> RunTracker \| NullTracker`, `event(**fields)`, атомарни `write_text/write_json/read_json`, `load_plan/save_plan` (JSON **и** md), `load_test_plan/save_test_plan`, `persist_artifacts(update, mode)`, `write_status(view)`, `write_summary(dict)`, `write_usage(usage_metadata, cost_usd)` (от main.py/app.py след стрийма), `extract_ticket_key`, `make_run_id`, `list_artifacts()`. `NullTracker` = no-op двойник |
| `src/plans.py` | Pydantic: `AcceptanceCriterion{id,text}`, `PlanStep{id,title,files,acceptance_criteria_refs,status,note}`, `ImplementationPlan{summary,acceptance_criteria,steps}`, `TestCase{id,title,criterion_ref,method: static\|checklist\|manual_review\|unit_test,expected,status,note}`, `TestPlan{summary,cases}`. Чисти функции: `render_implementation_plan`, `render_test_plan`, `render_traceability`, `apply_step_update`, `apply_case_update`, `record_phase_end`, `add_rework`, `reset_for_rerun`, `counts`; `make_plan_tools()` → `update_plan_step(step_id, status, note)`, `update_test_case(case_id, status, note)` (зареждат JSON → мутират → `save_plan`; грешки като текст: непознат ID + наличните, невалиден статус, липсващ план); промптове `DEV_PLAN_PROMPTS`/`QA_PLAN_PROMPTS` (bg/en) |
| `src/dod.py` | `DoDPolicy` протокол (`spec_problem`, `plan_problem`, `test_plan_problem`, `developer_result(answer, plan) -> (problem \| None, code_artifact)`, `ready_reason(code)`); `DemoDoD` — днешната логика 1:1; `ProdDoD(workspaces)` — D10 (`reason=t("route_diff_ready", files=n)`); `make_dod_policy(mode, ctx)` |
| `src/toolsets.py` | `make_mode_context(mode, repos)` → `DemoContext` / `ProdContext{jira, workspaces, publisher, settings}` (lazy import на prod модулите; `import src.graph` работи без `mcp` и без ключове); `make_tools(mode, ctx) -> RoleToolset{tools: dict[role → list], prompt_addendum: dict[role → str]}`. Demo: analyst `get_ticket_details`; developer `get_coding_standards`+`update_plan_step`; qa `check_code_syntax`+`run_test_checklist`+`update_test_case`. Prod: analyst → MCP `get_ticket_details` + `search_tickets` + read инструментите; developer + `list_repo_files`/`read_repo_file`/`search_repo`/`write_repo_file`/`delete_repo_file`/`get_change_diff`; qa + `read_repo_file`/`list_repo_files`/`get_change_diff`. Prompt addenda: demo („върни кода в ```python блок"), prod („работиш в клониран репозиторий: чети → променяй само нужните файлове с `write_repo_file` → провери с `get_change_diff`; **не** връщай код в отговора, а кратко описание на промените") |
| `src/jira_mcp.py` | `JiraMcpSettings.from_env()` (`missing()`, `auth_headers()`, `browse_url()`); `JiraMcpError`; `JiraMcpClient(settings, call_tool=None)` с `call`, `get_issue(key)`, `search(jql, max_results=10)`, `add_comment(key, md)`, `test_connection()`. Мапинг: `getJiraIssue{cloudId, issueIdOrKey, fields:[summary,description,priority,status,issuetype,labels,+ac_field], responseContentFormat:"markdown"}`, `searchJiraIssuesUsingJql` (чете `issues.nodes`, fallback списък), `addCommentToJiraIssue{commentBody, contentFormat:"markdown"}`, `getAccessibleAtlassianResources`. Sync мост `_run_sync(coro_factory, timeout)` — отделна нишка + `asyncio.run(wait_for(...))` (безопасно с/без течащ loop); транспорт `streamablehttp_client(url, headers)` или `stdio_client(shlex.split(JIRA_MCP_COMMAND))`; сесия на извикване (init + call; ≤4 на run). Retry с backoff само при транспортни грешки/timeout/429/5xx (sleep инжектируем), **без** retry при `isError` (детерминистични). Нормализация (чиста, тестваема офлайн): `parse_tool_result`, `adf_to_text` (защитен fallback), `extract_acceptance_criteria` (custom field → заглавие „Acceptance criteria/Критерии за приемане" в описанието → празно + бележка „изведи ги от описанието"), `normalize_issue`, `format_ticket_card(issue, lang)` (първите 5 секции = `_TICKET_TEXTS[lang]["card"]` от `tools.py`, + Статус/Тип/Линк). Инструменти `make_jira_tools(client)`: `get_ticket_details(ticket_id)`, `search_tickets(jql)` (docstring: винаги ограничавай с `project =`/`text ~`/`updated >=`; при „Unbounded JQL" добавя подсказка). Никога не хвърлят |
| `src/repo_workspace.py` | `Runner = Callable[[argv, cwd, timeout], CompletedProcess]`, `default_runner` (`subprocess.run`, `shell=False`, utf-8, `GIT_TERMINAL_PROMPT=0`); `WorkspaceError`; `RepoWorkspace(repo, root, base_branch, runner, max_file_bytes=200_000, max_list=500)`: `prepare()` (clone с `core.autocrlf=false` или `fetch --prune`), `start_run(branch)` (`checkout -B <branch> origin/<base>`, `reset --hard`, `clean -fdx`), `list_files(glob)` (`git ls-files` + fnmatch), `read_file` (guard, лимит, бинарен → грешка), `search(pattern, glob)` (`git grep -n -I`), `write_file`, `delete_file`, `stage_all`, `diff()` (`--cached`), `changed_files()`, `has_changes()`, `syntax_error(path)`, `commit(msg, author)`, `push()`. `_resolve(rel)`: не празен/абсолютен (вкл. `C:\`, `\\srv`), без `..`, вътре в workspace, без `.git` компонент, без symlink, deny-globs (`.env*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`, `*secret*`, `*credential*`). `make_repo_tools(workspaces)` → read/write/diff инструменти (`repo` аргумент `owner/name` или уникално `name`; непознат → списък с наличните); текстове в `_WS_TEXTS[lang]` при извикване |
| `src/publish.py` | `PublishError`; `GhPublisher(runner)`: `create_draft_pr(repo, cwd, base, head, title, body_file) -> url` (`gh pr create --draft --repo owner/name --base --head --title --body-file`; URL = последният ред `https://github.com/.+/pull/\d+`; при „already exists" → `gh pr view <head> --json url -q .url`), `auth_status()`; `publish(state, ctx, tracker) -> dict` (логиката от `finalize`: за всеки workspace с промени commit/push/PR, write-back, събира `pr_urls`/`publish_errors`/`publish_status`); `build_pr_body(...)` (тикет линк, резюме на spec, план със статуси и тест-план с резултати в `<details>`, матрица, `run_id`, footer „generated by Multi-Bot"); `build_jira_comment(...)` |
| `src/ui_helpers.py` | чисти, тестваеми помощници за app.py/main.py: `mode_problems(...)`, `pr_links_markdown(pr_urls)`, `list_run_artifacts(run_dir)`, `format_repos(repos)`, `hitl_prompt_text(payload)` |
| `src/hitl.py` | `GATES = ("plan", "publish", "escalation")`, `default_gates(mode)`, `enabled_gates(mode)` (env `HITL_GATES`), `max_revisions()`, `HitlDecision` (Pydantic: `action: approve\|revise\|abort`, `feedback`, `by`), `parse_decision(raw)` (валидира resume payload; невалиден → abort с бележка), `make_gate_node(name, *, build_payload, on_approve, on_revise, on_abort, tracker_from_state)` — възел с `interrupt(payload)`; pass-through при изключена порта; записва решението в `run.jsonl`, `hitl-decisions.md`, `steps/NN-<gate>.md`; `auto_approve()` (env `HITL_AUTO_APPROVE`) |
| `src/step_plans.py` | шаблоните от §3b: `plan_section(node, state) -> str` (детерминистичен план по възел), `execution_section(update, tool_calls, duration, dod)`, `render_index(steps)`, `StepRecord` dataclass; ползва се от `_traced` |

### 4.2 Променени файлове

| Файл | Промяна |
|------|--------|
| `src/graph.py` | `TeamState` +полета (§3); `PIPELINE`, `ENTRY_NODE`; `build_graph(checkpointer=None, *, mode=None, repos=None, runs_dir=None)` — вътре `ctx = make_mode_context(mode, repos)`, `toolset = make_tools(...)`, `dod = make_dod_policy(...)`, `plan_llm`/`test_plan_llm`, `create_analyst(toolset.tools["analyst"], toolset.prompt_addendum["analyst"])` и т.н.; нови фабрики `make_init_run_node(mode, runs_root, workspaces)`, `make_dev_plan_node(plan_llm, dod)`, `make_qa_plan_node(test_plan_llm, dod)`, `make_finalize_node(ctx)`; `make_developer_node(agent, dod)` (инлайн DoD блокът L356-372 → `dod.developer_result`), след агента: `record_phase_end`, `next = "qa" if test_plan else "qa_plan"`; `make_qa_node` — `record_phase_end`, при NEEDS_WORK `add_rework` + `reset_for_rerun`, терминалните клони → `next="finalize"`; `make_supervisor_node` → `ENTRY_NODE`; `_dod_failure` → `escalation_gate` (ако е включена) иначе `finalize`; `_traced` (пише и `steps/NN-<node>.md` преди/след); HITL възли `approve_plan`, `approve_publish`, `escalation_gate` от `hitl.make_gate_node`; `dev_plan → approve_plan → developer`, `qa` APPROVED → `approve_publish` (prod) → `finalize`; `build_graph(..., hitl_gates=None)` → при включени порти и `checkpointer=None` → `InMemorySaver`; ребра: `START→init_run→supervisor`, `for node in ("supervisor", *PIPELINE): add_conditional_edges(node, route_next, [*PIPELINE, END])` |
| `src/agents.py` | `create_analyst(tools=None, prompt_addendum="")`, `create_developer(...)`, `create_qa(...)` — `None` → днешните demo списъци (обратна съвместимост); `create_agent(..., context_schema=RunCtx)`; базовите промптове получават режим-неутрален абзац за плана („маркирай стъпката `in_progress` преди да започнеш, `done` с бележка след като завършиш, `blocked` ако не можеш"; QA: `running` → `passed\|failed`); специфичното за режима идва от `prompt_addendum` |
| `src/tools.py` | без промяна на mock инструментите; `_TICKET_TEXTS` и docstring-ът на `get_ticket_details` се експортират за преизползване от prod |
| `src/config.py` | без промяна (режимът е в `modes.py`) |
| `src/i18n.py` | нови ключове (§6) |
| `app.py` | sidebar след доставчика: `mode = st.selectbox(t("ui_mode"), ["prod","demo"], index от `dotenv_values()["APP_MODE"]` (default prod), format_func, help)`; `os.environ["APP_MODE"] = mode`; prod блок: `st.multiselect(t("ui_repos"), all, default=all)`, caption `t("ui_jira_status", cloud, auth)`, бутон `t("ui_jira_test")` → `JiraMcpClient.test_connection()` + `GhPublisher.auth_status()` (извън run-цикъла → bare `st.*` е ОК), проблемите от `validate_prod_config` → `st.error` + Run `disabled`; demo блок: `st.info(t("ui_demo_hint"))`; `get_graph(provider, lang, mode, repos_tuple)`; `render_event` нови видове: `plan` (expander с md + път), `plan_update` (caption при `update_plan_step`/`update_test_case` tool_call), `publish` (success с PR линкове / error с грешките), `artifacts` (expander със списъка файлове в `runs/<run_id>/`), `hitl` (панел на портата: преглед на артефакта, textarea за коментар, бутони Approve / Request changes / Abort), `hitl_decision` (caption с решението); стриймът се изнася в `run_stream(graph, inp, config)` (извиква се и с `Command(resume=…)`), `__interrupt__` събитието спира цикъла и оставя `st.session_state.pending_gate`; sidebar чекбоксове `t("ui_hitl_gates")` (default по режим) → `os.environ["HITL_GATES"]`; кеш ключ включва и портите; след стрийма `RunTracker(run_dir).write_usage(...)`; всичко през `emit()` |
| `main.py` | печат `t("mode_line", mode, repos)` + `t("hitl_line", gates)` след задачата; `t("run_dir_line")` при `init_run`; `t("plan_file_line")` при `dev_plan`/`qa_plan`; при `__interrupt__` — печата прегледа и чете решение от stdin (`[a]/[r] текст/[q]`), без TTY → `HITL_AUTO_APPROVE=1` или ясно съобщение и изход; продължава с `Command(resume=…)`; в обобщението `t("pr_line")`/`t("publish_errors_line")`/`t("artifacts_line")`/`t("hitl_summary_line")`; `write_usage` след стрийма; при budget stop `tracker.event(final_status="BUDGET_EXCEEDED")` |
| `draw_graph.py` | `os.environ.setdefault("APP_MODE", "demo")` преди импорта |
| `requirements.txt` | `mcp==1.29.1` |
| `.gitignore` | `runs/`, `workspace/`, `.ruff_cache/` |
| `.env.example` | секция „Режим и Prod интеграции" (§5, bg коментари, бележка за `gh auth login` + `gh auth setup-git`) |
| `tests/conftest.py` | `_clean_env`: `APP_MODE=demo`, `RUNS_DIR=<tmp_path>/runs`, `WORKSPACE_DIR=<tmp_path>/ws`, чисти `PROD_REPOS`/`JIRA_*`; git изолация `GIT_CONFIG_GLOBAL=<празен файл>`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0`; `FakeLLMFactory` диспечира и `ImplementationPlan`/`TestPlan`; `FakeWorkerAgent.invoke(payload, **kwargs)` пази `context`, опционален `side_effect(ctx)` (симулира tool извикване — напр. маркира стъпка `done`, пише файл в workspace); `scripted_graph(..., plans=, test_plans=, mode="demo", ctx=None)`; patch lambdas → `lambda *a, **k: ...`; фикстури `bare_repo` (`git init --bare` + seed `main`), `fake_jira` (`call_tool` двойник с фикстурни JSON отговори във верифицираната форма), `fake_runner` (записва argv; `gh` → фалшив PR URL), `prod_ctx` (сглобен `ProdContext` от двойниците; `graph_module.make_mode_context` се патчва) |
| `tests/test_graph_units.py` | `TestGraphStructure` (нови възли/ребра), `test_llm_factories_receive_structured_schemas` (+2 схеми), терминалните `next` → `"finalize"`, developer → `qa_plan`/`qa`, `test_no_llm_objects_at_module_level` (+ `make_tools`, `make_mode_context`) |
| `tests/test_workflow_e2e.py` | имена на съобщенията `[None,"analyst","dev_plan","developer","qa_plan","qa"]`, маршрут със `init_run`/`finalize`; `TestStreamingContract` — `messages` проверката само когото има съобщения; нови: планове в state и на диск, DoD на празен план (retry → ESCALATED), rework → „Rework 1" + `qa_plan` LLM извикан веднъж, вход от developer/qa → `dev_plan`/`qa_plan`, resume пази `run_id`, **prod happy path** (fake Jira + локален bare repo + fake gh → `code.diff`, `pr_urls`, PR в bare repo branch), publish грешка → `publish_status=PUBLISH_FAILED`, `final_status` остава APPROVED |
| `tests/test_agents_integration.py` | без промяна на съществуващите; + подаване на `tools=`/`prompt_addendum`, prod агентите имат repo/plan инструментите, `ScriptedToolCallingModel` вика `write_repo_file`/`update_plan_step` и файловете реално се променят (`context=RunCtx`) |
| `tests/evals/test_golden_cases.py` | `build_graph(mode="demo")` (иначе evals ще ударят реална Jira) |
| нови тестове | `test_modes.py`, `test_run_tracker.py`, `test_plans.py`, `test_dod.py`, `test_toolsets.py` (вкл. контрактен паритет demo/prod), `test_jira_mcp.py`, `test_repo_workspace.py`, `test_publish.py`, `test_ui_helpers.py` |
| `README.md` / `README.en.md` | нова секция „Режими: Demo / Prod" / „Modes: Demo / Prod" (таблица инструменти по режим, Jira token или OAuth, `PROD_REPOS`, branch naming, draft PR, write-back, лимити за безопасност); диаграма с `init_run`/`dev_plan`/`qa_plan`/`finalize`; дърво с новите модули + `runs/`/`workspace/`; инсталация (Node/npx, `gh auth login`); „Идеи за надграждане" (маха се „замени mock тикетите с Jira") |
| `CHANGELOG.md` | `[Unreleased] / Добавено`: режими Demo/Prod (Jira през официалния Atlassian MCP, git репозитории, draft PR), планове на Developer/QA, проследяване в `runs/` |
| `CLAUDE.md` | абзац за режимите/новите модули; gotchas: „prod инструментите никога не хвърлят; `mcp` е lazy import; тестовете не пипат мрежа — fake `call_tool`/`runner`; никога не пиши извън `RUNS_DIR`/`WORKSPACE_DIR`" |

---

## 5. Env променливи (нови)

```dotenv
# --- Режим ---
APP_MODE=prod                     # prod | demo (UI дропдаунът го презаписва)
RUNS_DIR=runs                     # артефакти на run-овете (папка с дата и час на всеки run)
WORKSPACE_DIR=workspace           # локални клонове на репозиториите
# --- Human-in-the-Loop ---
# HITL_GATES=plan,publish,escalation   # default: prod = всички; demo = plan,escalation; празно = изключено
# HITL_MAX_REVISIONS=2             # колко пъти човек може да върне плана за ревизия
# HITL_AUTO_APPROVE=0              # 1 = неинтерактивно одобрение (CI); записва се в журнала
# --- Jira (Atlassian Remote MCP Server) ---
JIRA_MCP_URL=https://mcp.atlassian.com/v1/mcp
JIRA_MCP_AUTH=api_token           # api_token | oauth
JIRA_EMAIL=you@company.com        # за api_token (Basic)
JIRA_API_TOKEN=...                # https://id.atlassian.com/manage-profile/security/api-tokens
JIRA_CLOUD_ID=myposgroup.atlassian.net
# JIRA_MCP_COMMAND=npx -y mcp-remote@latest https://mcp.atlassian.com/v1/mcp/authv2   # само за oauth
JIRA_MCP_TIMEOUT_SECONDS=60
JIRA_MCP_MAX_RETRIES=2
# JIRA_AC_FIELD=customfield_XXXXX  # custom field за критерии за приемане (по избор)
JIRA_WRITE_BACK=0                 # 1 = коментар в тикета с PR линковете
# --- Git / PR ---
PROD_REPOS=Stanislav-Stanev/langchain-multi-agent   # със запетаи: owner/name или https URL
PROD_BASE_BRANCH=main
PROD_GIT_AUTHOR_NAME=Multi-Bot
PROD_GIT_AUTHOR_EMAIL=noreply@multibot.local
REPO_MAX_FILE_KB=200
```

---

## 6. i18n ключове (bg + en)

- Routing: `route_run_started {run_id}`, `route_run_resumed {run_id}`, `route_workspace_ready {repos, branch}`,
  `route_workspace_failed {error}`, `route_plan_ready {steps}`, `route_test_plan_ready {cases}`,
  `route_diff_ready {files}`, `route_run_finished {status}`, `route_published {n}`, `route_publish_failed {errors}`,
  `route_publish_skipped`.
- DoD: `dod_empty_plan`, `dod_empty_test_plan`, `dod_no_diff`, `dod_changed_file_syntax_error {file, error}`,
  `dod_no_step_done`.
- Инструменти за планове: `tool_plan_step_updated {step_id, status}`, `tool_plan_step_unknown {step_id, available}`,
  `tool_plan_status_invalid {status, allowed}`, `tool_plan_missing`, `tool_test_case_updated`, `tool_test_case_unknown`.
- Рендер (`md_*`): заглавия/секции на плана, тест-плана, матрицата, STATUS.md, PR body, Jira коментар;
  етикети на статусите `status_todo|in_progress|done|blocked|planned|running|passed|failed`.
- UI: `ui_mode`, `ui_mode_prod`, `ui_mode_demo`, `ui_mode_help`, `ui_mode_banner {mode, repos}`, `ui_repos`,
  `ui_jira_status {cloud, auth}`, `ui_jira_test`, `ui_jira_ok`, `ui_jira_error`, `ui_gh_status`,
  `ui_warn_no_repos`, `ui_warn_no_repos_selected`, `ui_warn_jira_missing {vars}`, `ui_demo_hint`,
  `ui_plan_expander {title, path}`, `ui_plan_update {agent, id, status}`, `ui_publish_done {links}`,
  `ui_publish_failed {errors}`, `ui_artifacts {run_dir}`.
- Конзола: `mode_line {mode, repos}`, `hitl_line {gates}`, `run_dir_line {path}`, `plan_file_line {name, path}`,
  `pr_line {repo, url}`, `publish_errors_line {errors}`, `artifacts_line {run_dir}`, `hitl_summary_line {n}`,
  `hitl_prompt {gate}`, `hitl_options`, `hitl_no_tty`.
- HITL: `route_gate_approved {gate}`, `route_gate_revise {gate, feedback}`, `route_gate_aborted {gate}`,
  `route_gate_auto {gate}` (изключена/auto), `route_gate_retry {node}`, `route_gate_revisions_exhausted {max}`,
  `ui_hitl_gates`, `ui_hitl_gate_plan|publish|escalation`, `ui_hitl_title {gate}`, `ui_hitl_approve`,
  `ui_hitl_revise`, `ui_hitl_abort`, `ui_hitl_feedback`, `ui_hitl_waiting`, `ui_hitl_decided {action, by}`,
  `md_step_*` (заглавия на секциите от шаблона в §3b), `md_hitl_*` (hitl-decisions.md).
- Текстовете на Jira/repo инструментите остават в per-language dict-ове в модулите им (конвенция от `tools.py`).

---

## 7. Тестова стратегия (офлайн, без LLM, без мрежа)

- **unit**: `test_modes.py` (default prod, нормализация, невалидно → `ValueError`, `parse_prod_repos`,
  `validate_prod_config`); `test_run_tracker.py` (директория, `run.json`, append + `seq` дедупликация,
  атомарен overwrite, STATUS.md, `extract_ticket_key`/`make_run_id`, `NullTracker`); `test_plans.py`
  (схеми, чекбокс маркери, history замяна, матрица с `?`, идемпотентен `apply_step_update`, инструментите
  през `create_agent(context_schema=RunCtx)` + `ScriptedToolCallingModel`); `test_dod.py` (Demo = днешните 3
  изхода; Prod: без diff / счупен `.py` / без `done` / happy → diff); `test_toolsets.py` (имена по роля и
  режим, **контрактен паритет** на общите инструменти, demo не импортира `src.jira_mcp`); `test_jira_mcp.py`
  (fake `call_tool`: мапинг на аргументите, нормализация върху фикстура във верифицираната форма, AC
  извличане bg/en/custom field/fallback, `adf_to_text`, `issues.nodes`, `isError` → текст без retry,
  транспортна грешка → retry с инжектиран sleep, timeout → текст, `sys.modules["mcp"]=None` → подсказка за
  инсталация, картата започва като mock картата); `test_repo_workspace.py` (реален локален bare repo:
  clone→fetch идемпотентно, `start_run` чисти, list/read/search, write→diff, delete, commit+push виден в bare,
  traversal матрица, лимит, бинарен отказ); `test_publish.py` (fake runner: `gh` argv, 2 workspace-а с 1 чист
  → 1 PR, rc≠0 → `PUBLISH_FAILED` без изключение, existing-PR fallback, `build_pr_body`, write-back само при
  `JIRA_WRITE_BACK=1` и грешката се поглъща); `test_ui_helpers.py`.
- **HITL** (`test_hitl.py` + E2E): `enabled_gates` по режим/env; `parse_decision` (невалиден → abort);
  gate възел с изключена порта = pass-through с „auto" причина; **с `InMemorySaver`**: стриймът спира с
  `__interrupt__` на `approve_plan` (payload съдържа пътя до плана и preview), `Command(resume={"action":
  "approve"})` продължава към developer; `revise` → `dev_plan` извикан втори път с feedback в историята,
  `plan_revisions == 1`; трета ревизия → само approve/abort (`route_gate_revisions_exhausted`); `abort` →
  `final_status == "ABORTED"`, `finalize` пише summary; `escalation_gate` при персистентно счупен код →
  `retry` с указания нулира `dod_retries["developer"]` и Developer бива извикан отново; `approve_publish`
  `abort` → `publish_status == "SKIPPED_BY_HUMAN"`, fake gh не е викан; `HITL_AUTO_APPROVE=1` → без
  interrupt, решението е логнато като auto; `hitl-decisions.md` и `steps/NN-approve_plan.md` съдържат
  решението/коментара; `build_graph(hitl_gates=("plan",))` без checkpointer → `InMemorySaver`.
- **Step plans** (`test_step_plans.py`): всяка стъпка има `steps/NN-<node>.md` със секции „План" и
  „Изпълнение"; повторно изпълнение на възел (rework) дава нов номер; `index.md` изброява всички;
  статусите/времената са попълнени; DoD провалът е описан в „Изпълнение".
- **integration**: агентите с prod инструментите реално четат/пишат в workspace и в плана.
- **E2E**: виж `tests/test_workflow_e2e.py` в §4.2.
- Покритие на `src/` ≥ 85% (всички нови модули са с инжектируеми граници; `# pragma: no cover` само за
  1-3 реда, които стартират реален процес/мрежа). `ruff check .` зелен.

---

## 8. Документация

README.md + README.en.md огледално (§4.2), CHANGELOG `[Unreleased]`, CLAUDE.md, `.env.example`,
`docs/prod-mode-plan.md` (този план с отметнат прогрес).

---

## 9. Последователност (3 PR-а)

- [x] **Стъпка 0** — `docs/prod-mode-plan.md` (този план); чекбоксовете тук се отмятат при всяка стъпка.
  Преди всеки PR: `docs/plans/<YYYY-MM-DD_HH-MM>_<стъпка>.md` (план → отметки след изпълнение).
- [x] **PR 1 — режими, планове, проследяване, HITL (пълен demo flow)** — виж `docs/plans/2026-08-26_09-58_pr1-modes-plans-tracking-hitl.md`: `modes.py`, `run_context.py`,
  `run_tracker.py`, `step_plans.py`, `plans.py`, `dod.py` (Demo), `hitl.py` (порти `plan`, `escalation`),
  `toolsets.py` (demo клон + prod интерфейси като Protocols), `graph.py` (state, `init_run`/`dev_plan`/
  `approve_plan`/`qa_plan`/`escalation_gate`/`finalize`, `_traced` със `steps/`, `ENTRY_NODE`,
  `build_graph(mode=, hitl_gates=)`), `agents.py`, i18n, `main.py` (HITL през stdin), `app.py` (дропдаун
  default prod, HITL чекбоксове + панел, `plan`/`plan_update`/`artifacts`/`hitl` събития, prod → „не е
  конфигуриран" + Run забранен), `draw_graph.py`, `.gitignore`, `.env.example` (режим, HITL), evals
  `mode="demo"`, conftest + всички засегнати/нови тестове за тази част, README/CHANGELOG/CLAUDE.md за частта.
- [x] **PR 2 — Jira през MCP** — виж `docs/plans/2026-08-26_11-02_pr2-jira-mcp.md`: `jira_mcp.py`,
  `requirements.txt` (`mcp`), prod клон на `toolsets.py` за analyst, UI Jira статус + тест на връзката,
  `JIRA_WRITE_BACK` (обвивка `add_comment`, ползвана в PR 3), `.env.example` (Jira), `test_jira_mcp.py`, docs;
  мрежов smoke срещу `mcp.atlassian.com` направен; **ръчната проверка с реален тикет изисква личния API
  token на потребителя** (бутон „Тест на връзката с Jira" + prod run).
- [ ] **PR 3 — git workspace + draft PR + порта `approve_publish`**: `repo_workspace.py`, `publish.py`,
  `ProdDoD`, `init_run` prod клон, `approve_publish` (preview на diff/PR body), `finalize` publish,
  `ui_helpers.py`, UI мултиселект/`publish` събитие, `.env.example` (git), тестове с bare repo + fake gh,
  prod E2E (вкл. abort на портата → без push), docs; **ръчна проверка**: реален run срещу конфигуриран
  репозиторий (напр. `Stanislav-Stanev/langchain-multi-agent`) → одобрение на портата → draft PR; при
  `JIRA_WRITE_BACK=1` — коментар в тикета.

---

## 10. Верификация (край на всеки PR)

1. `.\.venv\Scripts\ruff.exe check .` и `.\.venv\Scripts\python.exe -m pytest` — зелени, покритие ≥ 85%.
2. `.\.venv\Scripts\python.exe draw_graph.py` — диаграмата показва `init_run`, `dev_plan`, `qa_plan`, `finalize`.
3. `$env:APP_MODE="demo"; .\.venv\Scripts\python.exe main.py` — happy path DEV-101: спира на
   `approve_plan`, показва плана, `a` продължава; `runs/<дата_час>_DEV-101/` съдържа `steps/01..NN-*.md`
   (всеки с „План" и „Изпълнение"), `index.md`, `hitl-decisions.md`, `implementation-plan.md` с отметнати
   стъпки, `qa-plan.md` с резултати, `STATUS.md`, `run.jsonl`, `traceability.md`, `summary.json` (вкл.
   токени/цена). `HITL_GATES=` → без спиране; `r` с коментар → `dev_plan` се изпълнява повторно.
4. Streamlit: дропдаунът е на **Prod** по подразбиране; без конфигурация — грешка и Run забранен; в Demo —
   пълен run с панели „План", „Тест-план", „Артефакти" и HITL панел (Approve / Request changes / Abort),
   който преживява rerun; смяната на режим/порти не изисква рестарт.
5. PR 2: prod run с реален Jira ключ (напр. от проект `AA`) — картата идва от MCP; „Тест на връзката" показва
   сайта и `gh` статуса.
6. PR 3: prod run → draft PR в конфигурирания репозиторий; тялото съдържа тикет, spec, план със статуси,
   тест-план с резултати, матрица, `run_id`; повторен `finalize` (resume) не създава втори PR.
