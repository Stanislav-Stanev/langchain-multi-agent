🇧🇬 **Български** | 🇬🇧 [English](README.en.md)

# Multi-Bot — мултиагентна SDLC система с LangChain + LangGraph

Учебен проект: **Multi-Bot** е мултиагентна система, която автоматизира
част от **SDLC процеса** (Software Development Life Cycle) — от
изискване в тикет до прегледан и одобрен код.

## Архитектура — шаблон "Supervisor"

```
 потребител -> init_run -> SUPERVISOR (еднократен triage)
                              |
   ANALYST -> dev_plan -> [approve_plan] -> DEVELOPER -> qa_plan -> QA
  изисквания   план          човек          пише код    тест-план  преглед
                 ^ "промени"                    ^                    |
                 +------------------------------+---- NEEDS_WORK ----+
                                                                     v
                                          [approve_publish] -> finalize -> END
   DoD провал / rework лимит -> [escalation_gate] -> повторен опит с указания | край
   [ ... ] = Human-in-the-Loop порта (спира и чака човек)
```

| Възел / агент       | SDLC фаза      | Какво прави                                                          |
|---------------------|----------------|----------------------------------------------------------------------|
| `init_run`          | —              | код: папка `runs/<дата_час>_<тикет>/`, ключ на тикета                |
| **Supervisor**      | управление     | LLM: еднократен triage — откъде влиза задачата (или FINISH)          |
| **Analyst**         | Requirements   | агент: спецификация; `get_ticket_details` (mock Jira / Jira в prod)  |
| `dev_plan`          | Planning       | LLM (структурирано): план за имплементация с чекбоксове              |
| `approve_plan`      | —              | **човек** одобрява плана / иска промени / прекратява                 |
| **Developer**       | Implementation | агент: код; `get_coding_standards`, `update_plan_step` (прогрес)     |
| `qa_plan`           | Test planning  | LLM (структурирано): тест-план по критериите за приемане             |
| **QA**              | Testing/Review | агент: `check_code_syntax`, `run_test_checklist`, `update_test_case` |
| `approve_publish`   | —              | **човек** одобрява публикуването (prod: commit / push / draft PR)    |
| `escalation_gate`   | —              | **човек** при ескалация: повторен опит с указания или край           |
| `finalize`          | —              | код: матрица на проследимост, `summary.json`, публикуване (prod)     |

Supervisor (LLM) прави само **еднократен triage** — откъде да влезе
задачата; `dev_plan`/`qa_plan` правят по едно **структурирано** LLM
извикване (планът е съдържание — решава го моделът; форматът и прогресът
са код). Всичко останало е **детерминистичен код** върху типизирани
артефакти (спецификация, план, код, тест-план, QA присъда):

- всяка фаза има **Definition of Done**, проверяван от кода (непразна
  спецификация, план със стъпки, извлечен и синтактично валиден код) —
  непокрит DoD дава на агента един повторен опит, после **ескалира към
  човек** (`escalation_gate`);
- присъдата на QA е **структурирана** (`QAVerdict`, Pydantic) — при
  `NEEDS_WORK` задачата се връща на Developer (планът получава секция
  „Rework N" със забележките, без ново планиране), но най-много
  `MAX_REWORK` пъти (по подразбиране 3), после ескалация вместо вечен цикъл;
- **Human-in-the-Loop** — портите `plan`, `publish`, `escalation`
  (`HITL_GATES`) спират графа с `interrupt()` и чакат решение: одобри /
  поискай промени (с указания) / прекрати; всяко решение се записва;
- **проследяване** — всеки run оставя папка `runs/<дата_час>_<тикет>/` с
  план преди и изпълнение след **всяка стъпка** (`steps/NN-<възел>.md`),
  плановете с чекбоксове, `STATUS.md`, журнал, матрица на проследимост и
  `summary.json` (виж [Режими, планове и Human-in-the-Loop](#режими-планове-и-human-in-the-loop));
- по избор: различен модел за всяка роля (`MODEL_SUPERVISOR`, ...),
  твърд бюджетен лимит на изпълнение (`MAX_COST_USD_PER_RUN`), резервен
  модел при срив на основния (`MODEL_FALLBACK`) и **checkpointing** в
  SQLite (`CHECKPOINT_SQLITE_PATH`) — прекъснат run се възобновява от
  последната записана стъпка (thread per задача).

## Структура на проекта

```
langchain-multi-agent/
├── main.py              # входна точка — стартира графа (конзола)
├── app.py               # Streamlit уеб UI с визуализация на живо
├── draw_graph.py        # Mermaid диаграма на графа (локално, без LLM)
├── requirements.txt     # зависимости (pinned версии)
├── pytest.ini           # конфигурация на pytest (тестовете в tests/)
├── ruff.toml            # конфигурация на линтера (ruff)
├── improvement.md       # планът за продукционизиране (какво и защо)
├── .env.example         # шаблон за настройките (копирай като .env)
├── CHANGELOG.md         # release notes (Keep a Changelog)
├── README.en.md         # английско огледало на този файл
├── .github/workflows/
│   └── ci.yml           # CI: ruff + pytest + 85% покритие на src/
├── .githooks/
│   ├── pre-commit       # hook: пази main от директни комити
│   └── pre-push         # hook: пази main + Claude синхронизира docs/changelog
├── docs/
│   ├── prod-mode-plan.md    # планът за Demo/Prod режима, HITL и проследяването
│   └── plans/               # план преди всяка стъпка от имплементацията (дата_час)
├── src/
│   ├── config.py        # настройки + фабрика за LLM клиента (anthropic/ollama)
│   ├── modes.py         # режими demo/prod (APP_MODE), репозитории, валидация на prod
│   ├── i18n.py          # двуезичните текстове (bg/en) + t() функция
│   ├── tools.py         # инструментите (mock Jira, линтер, QA чеклист)
│   ├── toolsets.py      # кои инструменти получава всяка роля според режима
│   ├── plans.py         # планове на Developer/QA: схеми, чекбоксове, прогрес
│   ├── dod.py           # Definition of Done политики (demo / prod)
│   ├── hitl.py          # Human-in-the-Loop порти (interrupt + решение)
│   ├── run_tracker.py   # runs/<дата_час>_<тикет>/ - журнал, стъпки, статус
│   ├── step_plans.py    # шаблоните "план преди / изпълнение след" всяка стъпка
│   ├── run_context.py   # RunCtx - контекстът на run-а за инструментите
│   ├── jira_mcp.py      # prod: Jira през официалния Atlassian MCP сървър
│   ├── repo_workspace.py# prod: git workspace + инструменти за четене/писане
│   ├── publish.py       # prod: commit -> push -> draft PR (+ Jira коментар)
│   ├── agents.py        # тримата работни агенти (ReAct)
│   └── graph.py         # supervisor + планове + порти + сглобяване на графа
├── runs/                # (git-ignored) артефактите на всеки run
├── workspace/           # (git-ignored) локалните клонове на репозиториите (prod)
└── tests/               # pytest пакет — целият workflow, без реални LLM извиквания
    └── evals/           # golden evals с РЕАЛЕН LLM (пускат се изрично)
```

Препоръчителен ред на четене за учене:
`tools.py` → `agents.py` → `plans.py` → `hitl.py` → `graph.py` → `main.py`

## Инсталация и стартиране

```bash
# 1. Виртуална среда (добра практика — изолира зависимостите)
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/macOS

# 2. Зависимости
pip install -r requirements.txt

# 3. API ключ и режим
copy .env.example .env        # Windows (cp на Linux/macOS)
# редактирай .env и сложи ключа си от https://platform.claude.com/
# APP_MODE=prod е по подразбиране; за учебния режим сложи APP_MODE=demo

# 4. Старт с демо задачата (тикет DEV-101) - в demo режим
python main.py                # (PowerShell: $env:APP_MODE="demo"; python main.py)
# графът спира на портата approve_plan и чака: [a] одобри / [r: указания] / [q]

# ...или със собствена задача
python main.py "Имплементирай тикет DEV-102"
python main.py "Напиши функция, която обръща string наобратно"

# Уеб UI (език, доставчик, режим и Human-in-the-Loop порти в страничната лента)
streamlit run app.py

# Артефактите на run-а: runs/<дата_час>_DEV-101/ (планове, стъпки, STATUS.md)
```

## Режими, планове и Human-in-the-Loop

**Режим** (`APP_MODE`, дропдаун в UI-я; по подразбиране **prod**):

| | demo | prod |
|---|------|------|
| Тикети | примерните DEV-101 / DEV-102 (`src/tools.py`) | **реална Jira** през официалния Atlassian Remote MCP Server (`src/jira_mcp.py`) |
| Код | ```python блок в отговора на Developer | **реални промени в клонирани git репозитории** (`src/repo_workspace.py`) → **draft Pull Request** (`src/publish.py`) |
| Планове, стъпки, HITL | да | да (+ порта `publish` преди push) |

**Jira през MCP (prod).** Analyst ползва същия инструмент `get_ticket_details`,
но зад него стои официалният Atlassian MCP сървър (`https://mcp.atlassian.com/v1/mcp`):
`JIRA_MCP_AUTH=api_token` (Basic: `JIRA_EMAIL` + `JIRA_API_TOKEN` от
<https://id.atlassian.com/manage-profile/security/api-tokens>, неинтерактивно) или
`oauth` (`npx -y mcp-remote@latest …/authv2`, отваря браузър). `JIRA_CLOUD_ID` е
hostname-ът на сайта. Картата на тикета е в същия формат като mock-а (+ статус,
тип, линк); критериите за приемане се четат от custom field (`JIRA_AC_FIELD`)
или от секцията „Acceptance criteria / Критерии за приемане" в описанието.
Analyst има и `search_tickets(jql)` за задачи без ключ (JQL винаги ограничен).
Грешките се връщат като текст, транспортните се повтарят с backoff; `mcp`
пакетът се импортира само в prod. В UI-я има бутон „Тест на връзката с Jira".

**Git workspace и draft PR (prod).** `PROD_REPOS` (owner/name или GitHub URL, със
запетаи; мултиселект в UI-я) се клонират от `PROD_BASE_BRANCH` в `WORKSPACE_DIR`
и всеки run започва на чист branch `multibot/<тикет>-<run>`. Analyst/Developer/QA
четат с `list_repo_files` / `read_repo_file` / `search_repo`; Developer пише с
`write_repo_file` / `delete_repo_file` и проверява с `get_change_diff`; QA
преглежда diff-а. Артефактът на Developer е реалният `git diff` (DoD: непразен
diff, всеки променен `.py` се парсва, ≥1 стъпка от плана е `done`). След QA
APPROVED и одобрение на портата `publish` `finalize` прави commit → push →
`gh pr create --draft` за всеки репозиторий с промени (PR тялото съдържа
тикета, спецификацията, плановете със статуси, матрицата, `run_id`); при
`JIRA_WRITE_BACK=1` — коментар в тикета с PR линковете. Безопасност: пътищата
остават в workspace-а (без `..`, абсолютни пътища, symlink-ове, `.git`),
deny-list за тайни (`.env*`, ключове, сертификати), лимит на размера, бинарни
файлове не се четат, кодът никога не се изпълнява. Изисква `gh auth login` (и
`gh auth setup-git`). Без конфигурация UI-ят показва какво липсва и не пуска run.

**Планове преди работа.** Преди Developer възелът `dev_plan` прави план
за имплементация (критерии за приемане AC-x, стъпки S-x с файлове и
покрити критерии); преди QA възелът `qa_plan` прави тест-план (случаи T-x
↔ критерии). Планът е Pydantic обект (структуриран изход), JSON е
източникът на истина, а markdown-ът с чекбоксове се пре-рендира при всяка
промяна. Агентите отчитат прогреса с инструментите `update_plan_step` /
`update_test_case` (`- [ ]` → `- [~]` → `- [x]`); при rework планът получава
секция „Rework N", а тест-планът се нулира за нов run. `finalize` пише
матрица на проследимост критерий ↔ стъпки ↔ тест-случаи ↔ резултат.

**План преди всяка стъпка.** Всеки възел оставя `steps/NN-<възел>.md` —
секция „План" (цел, входове, обхват, DoD), написана *преди* изпълнението,
и „Изпълнение" (резултат, артефакти, DoD, решение), допълнена *след* него;
`index.md` и `STATUS.md` се обновяват след всяка стъпка. Папката на run-а е
`runs/<YYYY-MM-DD_HH-MM-SS>_<тикет>/` (`RUNS_DIR`).

**Human-in-the-Loop** (`HITL_GATES`; prod: `plan,publish,escalation`,
demo: `plan,escalation`; празно = без порти):

| Порта | Къде | Решения |
|-------|------|---------|
| `plan` | след плана, преди Developer | одобри → Developer; промени (с указания) → ново планиране (до `HITL_MAX_REVISIONS`); прекрати → `ABORTED` |
| `publish` | след QA APPROVED, преди публикуване (prod) | одобри → commit/push/PR; промени → Developer; прекрати → без публикуване |
| `escalation` | при DoD провал / изчерпан rework лимит | повторен опит (с указания, нулирани броячи) → същият възел; прекрати → `ESCALATED` |

Портите са отделни възли с LangGraph `interrupt()` (изискват checkpointer —
без `CHECKPOINT_SQLITE_PATH` се ползва `InMemorySaver`). Конзолата пита
`[a]/[r: текст]/[q]`; без интерактивна конзола `HITL_AUTO_APPROVE=1`
одобрява (и го записва). UI-ят показва артефакта с бутони Одобри /
Поискай промени / Прекрати. Всички решения са в `hitl-decisions.md`.

## Тестове

Проектът има pytest пакет (`tests/`), който покрива целия workflow **без
нито едно реално LLM извикване** — супервайзорът и работниците се
подменят със скриптирани дубльори (test doubles), затова тестовете са
бързи, безплатни и детерминистични:

- **unit тестове** — инструментите, i18n, config, режимите, плановете,
  проследяването (в `tmp_path`), DoD политиките, HITL механизмът,
  градивните елементи на графа;
- **интеграционни** — истинските ReAct агенти (`create_agent`) +
  истинските инструменти (вкл. `update_plan_step` през `ToolRuntime`),
  задвижвани от фалшив tool-calling модел;
- **E2E workflow** — реалният граф: happy path, rework цикълът и лимитът
  му, Definition of Done (повторен опит + ескалация), triage входовете,
  артефактите на диска, Human-in-the-Loop портите (interrupt →
  approve / revise / abort → resume), streaming контрактът и двуезичните текстове;
- **evals** (`tests/evals/`) — качеството на РЕАЛНИТЕ LLM решения:
  golden dataset (версиониран YAML в репото) + LLM-as-judge през същото
  `get_llm()`. Изключени от стандартния run (реални разходи!) — пускат
  се изрично.

```bash
python -m pytest              # детерминистичният пакет (~5 сек, без LLM)
ruff check .                  # линтер (същият, който пуска и CI)
python -m pytest -m eval      # golden evals (реален LLM, реални разходи)
```

## Двуезична поддръжка (bg/en)

Всички потребителски текстове живеят в `src/i18n.py`, индексирани по
ключ и език — кодът иска текст чрез `t("ключ")`. Езикът идва от
`APP_LANG` в `.env` (или превключвателя в уеб UI-я):

- **текстовете в конзолата и UI-я** се сменят мигновено;
- **промптовете на агентите** (`agents.py`, `graph.py`) и **mock
  данните на инструментите** (`tools.py`) съществуват на двата езика —
  езикът на промпта определя и езика, на който агентите отговарят;
- валидация при import гарантира, че никой ключ не е останал
  полупреведен.

Документацията е огледална: [README.md](README.md) (български) ↔
[README.en.md](README.en.md) (английски). Коментарите в кода са
умишлено само на български — това е учебният език на проекта.

## Работен процес (branching)

**`main` е продукционен клон** — по него не се комитва и не се push-ва
директно (двата git hook-а го пазят). Нова функционалност винаги минава
през feature клон и Pull Request:

```bash
git switch -c feature/име-на-функционалността   # 1. нов feature клон
# ... работиш, комитваш свободно ...
git push -u origin feature/име                  # 2. качваш клона
                                                #    (тук hook-ът обновява
                                                #     документацията и CHANGELOG)
gh pr create --fill                             # 3. Pull Request към main
gh pr merge --squash --delete-branch            # 4. вливане в main
```

Release notes за всяка завършена функционалност се пазят в
[CHANGELOG.md](CHANGELOG.md) (формат Keep a Changelog): pre-push
hook-ът добавя записите в секцията **[Unreleased]** автоматично, а при
release те се преместват под нов номер на версия.

## Автоматична синхронизация на документацията (git hooks)

В `.githooks/` живеят два hook-а:

- **pre-commit** — блокира директни комити върху `main`.
- **pre-push** — блокира директни push-ове към `main` и пуска
  **Claude Code в headless режим** (`claude -p`): той преглежда всички
  комити, които предстои да се качат, и ако са направили CLAUDE.md,
  README.md, README.en.md, requirements.txt или CHANGELOG.md неточни,
  ги обновява в **отделен docs-sync комит** (за нова завършена
  функционалност добавя и запис в release notes). Следи и двуезичността:
  двете README-та да са огледални, а новите потребителски текстове да
  имат bg И en версии в `src/i18n.py`. Push-ът тогава се прекъсва с ясно
  съобщение — пускаш `git push` втори път и той минава веднага. Така
  проверката се случва веднъж за цяла серия комити, а в remote винаги
  отива актуална документация.

```bash
# Активиране (еднократно след клониране — git не изпълнява hook-ове
# от репото по подразбиране, от съображения за сигурност)
git config core.hooksPath .githooks

# Прескачане при нужда
git push --no-verify              # прескача всички hook-ове
SKIP_DOCS_UPDATE=1 git push ...   # прескача само синхронизацията
SKIP_MAIN_GUARD=1 git push ...    # прескача само защитата на main (hotfix)
```

## Ключови концепции, които проектът демонстрира

1. **Supervisor шаблон** — централен агент управлява специализирани
   работници. Най-разпространената мултиагентна архитектура.
2. **Инструменти (tools)** — функции с `@tool`, които LLM извиква;
   docstring-ът е "документацията", по която моделът решава кога да ги ползва.
3. **ReAct цикъл** — мислене → инструмент → резултат → мислене → отговор
   (`create_agent` го имплементира наготово).
4. **Споделено състояние (State)** — обща история от съобщения, която
   всеки агент чете и допълва; така агентите си "предават" работата.
5. **Структуриран изход** — решението на супервайзора е Pydantic модел
   (`with_structured_output`), не свободен текст → надежден routing.
6. **Условни ребра (conditional edges)** — графът се разклонява според
   решението на LLM, включително цикъл за поправки (QA → Developer).
7. **Защити** — `recursion_limit` срещу безкраен цикъл; инструментите
   връщат съобщения за грешки вместо да хвърлят изключения; кодът се
   проверява с `ast.parse` **без да се изпълнява** (сигурност!).
8. **Планиране преди действие** — планът е структуриран изход, прогресът
   е код (JSON → markdown с чекбоксове), матрица на проследимост.
9. **Human-in-the-Loop** — `interrupt()` в отделни възли там, където
   решението е скъпо или необратимо; resume с `Command(resume=...)`.
10. **Одитна следа** — всяка стъпка оставя план и резултат на диска;
    след края на run-а папката му е пълният разказ какво и защо се е случило.

## Идеи за надграждане

- Добави агент "DevOps", който симулира deploy след одобрение от QA.
- Дай на QA инструмент, който наистина пуска `pytest` в изолирана среда.
- Постоянна памет между run-ове (Postgres checkpointer) и контекстна компресия.
