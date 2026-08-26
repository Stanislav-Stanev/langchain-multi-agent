# План · PR 3 — git workspace, draft PR и порта `approve_publish` (prod: Developer/QA/finalize)

Статус: DONE (реалният prod run - за потребителя, виж стъпка 8) · Начало: 2026-08-26 11:25 · Край: 2026-08-26 11:35 · Главен план: [prod-mode-plan.md](../prod-mode-plan.md) §4.1 `repo_workspace.py`/`publish.py`, §3a `approve_publish`, §9 PR 3

## Цел

В prod режим Developer работи в **клонирани git репозитории** (`PROD_REPOS`, `main` като база): чете,
търси и променя файлове с инструменти под строги ограничения; артефактът му е реалният diff (ProdDoD);
QA чете diff-а и файловете; след QA APPROVED **човек одобрява** на портата `approve_publish` (вижда diff,
тест-план, PR тяло) и `finalize` прави commit → push → `gh pr create --draft` за всеки репозиторий с
промени (идемпотентно по име на branch); при `JIRA_WRITE_BACK=1` — коментар в тикета с PR линковете.

## Контекст / входове

- PR 1 (планове, HITL, проследяване) и PR 2 (Jira MCP) са в `main`; `ProdContext` има полета
  `workspaces`, `workspace_view`, `publisher`, `repo_*_tools`, а `make_tools`/`build_graph` вече ги
  очакват (fallback към demo поведение, когато липсват).
- `ProdDoD(workspace_view)` е написан (PR 1) срещу протокол `changed_files()/syntax_error()/diff()`.
- `gh` 2.97 е логнат (account Stanislav-Stanev); git 2.55; `GIT_TERMINAL_PROMPT=0` спира висене при
  липсващи креденшъли.
- Тестовете НЕ пипат мрежа: реален локален **bare** репозиторий в `tmp_path` (`git init --bare` + seed)
  покрива clone/fetch/branch/write/diff/commit/push; `gh` е зад инжектируем `runner`.

## Обхват

`src/repo_workspace.py`, `src/publish.py`, `src/ui_helpers.py`, prod клон в `toolsets.py` (workspaces,
repo инструменти, publisher, `prepare_workspaces`), `graph.py` (`init_run` prod подготовка,
`approve_publish` payload с реален diff, `finalize` публикуване), `app.py` (мултиселект репозитории,
`publish` събитие, gh статус), `main.py` (PR линкове), i18n, `.env.example` (git секция), README bg/en,
CHANGELOG, CLAUDE.md, тестове. **Не-обхват:** изпълнение на код/тестове в sandbox (остава idea).

## Стъпки (критерий за готовност в скоби)

- [x] 1. `RepoWorkspace(repo, root, base_branch, runner)`: `prepare()` (clone/fetch), `start_run(branch)` (`checkout -B` от `origin/<base>`, `reset --hard`, `clean -fdx`), `list_files`, `read_file`, `search`, `write_file`, `delete_file`, `stage_all`, `diff`, `changed_files`, `has_changes`, `syntax_error`, `commit`, `push`; `_resolve` guard (не абсолютен, без `..`, вътре в workspace, без `.git`, без symlink, deny-globs) — тестове с bare repo + traversal матрица.
- [x] 2. `MultiWorkspace` (view над няколко репозитория за `ProdDoD`: `changed_files()` = `repo:path`, `syntax_error`, `diff()` с `# repo:` заглавия) + `make_repo_tools(workspaces)` → `list_repo_files`, `read_repo_file`, `search_repo`, `write_repo_file`, `delete_repo_file`, `get_change_diff` (грешки като текст, bg/en) — тестове.
- [x] 3. `publish.py`: `GhPublisher(runner)` (`create_draft_pr`, existing-PR fallback, `auth_status`), `build_pr_body` (тикет, spec резюме, план със статуси, тест-план с резултати, матрица, run_id, footer), `build_jira_comment`, `Publisher.publish(state, tracker)` → `pr_urls`/`publish_status`/`publish_errors`/`reason` — fake runner тестове (argv, 2 репозитория с 1 чист → 1 PR, rc≠0 → `PUBLISH_FAILED`, write-back само при `JIRA_WRITE_BACK=1`, Jira грешка не проваля публикуването).
- [x] 4. `toolsets.py`: `make_mode_context("prod", repos)` строи workspaces (без мрежа при конструкция), `MultiWorkspace`, repo инструменти, `Publisher`, `prepare_workspaces(ticket_key, run_id)`; `make_tools` дава пълните prod добавки.
- [x] 5. `graph.py`: `init_run` вика `prepare_workspaces` и записва `repos` (грешка → `escalation_gate`), `_publish_gate_payload` с реалния diff (```diff) и PR preview, `finalize` вика `publisher.publish` при APPROVED (+ `SKIPPED_BY_HUMAN`), `ProdDoD` активен; E2E prod: happy path (fake Jira + bare repo + fake gh → `code.diff`, `pr_urls`, branch `multibot/dev-101-*` в bare repo, PR body), DoD без промени / счупен .py → ескалация, abort на `approve_publish` → без push, approve → PR, publish грешка → `PUBLISH_FAILED` с `final_status` APPROVED, Jira write-back, грешка в workspace → ескалация.
- [x] 6. UI/CLI: мултиселект на репозиториите (default всички; празен избор → грешка + Run забранен), gh статус в „Тест на връзката", `publish` събитие с PR линкове / грешки; `main.py` печата publish статус, PR линкове, грешки; i18n ключове bg+en. (`ui_helpers.py` не се оказа нужен — логиката остана в 10 реда в app.py/main.py.)
- [x] 7. `.env.example` (git секция), README bg/en (таблица: кодът в prod = diff → draft PR; безопасност; `gh auth login` + `gh auth setup-git`), CHANGELOG, CLAUDE.md (repo_workspace/publish гочи).
- [x] 8. `ruff` + `pytest` зелени (~400 теста, покритие 97%); Streamlit AppTest в prod (мултиселект, Jira/gh проверка). **Остава за потребителя**: реален prod run срещу `Stanislav-Stanev/langchain-multi-agent` с реален Jira тикет → порта `publish` → draft PR (изисква личния Jira API token; git/gh частта е доказана с реален локален git и фалшив `gh`).

## Рискове / допускания

- Push към реален репозиторий е изходящо действие → винаги зад `approve_publish` (default включена в prod).
- `gh pr create` изисква `gh auth login`; клонирането по https - credential helper (`gh auth setup-git`).
- Един run в даден момент на workspace директория (Streamlit има една script нишка).
- Windows пътища: нормализираме `\` → `/`; `core.autocrlf=false` при clone, за да не „променяме" всички файлове.

## Definition of Done

Всички чекбоксове; prod E2E зелен; ръчна проверка на git частта (bare repo → draft PR симулация с fake gh
или реален PR, ако Jira token е наличен); README/CHANGELOG/CLAUDE.md актуални.

## Изпълнение (допълва се по време на работа)

- 11:25 — планът е записан; PR 2 отворен и чака CI.
- 11:27 — PR 2 влят (#7); клон `feature/prod-mode-3`; `repo_workspace.py`, `publish.py`, i18n, prod клон в `toolsets.py`, `graph.py` (repos в state, publish payload), conftest дубльори (`bare_repo`, `FakeGhRunner`, `fake_jira_client`, `prod_context`), тестове.
- 11:30 — отклонения: `spec_summary` взимаше markdown заглавието („Спецификация") вместо съдържателния ред → пропускаме заглавията; тестовете на toolsets за „преходния" случай заменени с реален prod контекст; `repos` не влизаше в state → `init_run` го записва; E2E тест проверяваше `reason` на finalize вместо на init_run → четем `run.jsonl`.
- 11:35 — всичко зелено (97% покритие); AppTest prod: мултиселект, празен избор блокира Run. PR 3 готов за push.
