"""
Интернационализация (i18n) - двуезична поддръжка (български / английски).

Добра практика за многоезични приложения: текстовете НЕ се пишат като
литерали из кода, а живеят на ЕДНО място (тук), индексирани по ключ и
език. Кодът пита за текст чрез t("ключ") и получава версията за текущия
език. Така:
  1. добавянето на нов език е промяна само в този файл;
  2. никой текст не може да "изостане" преведен само наполовина -
     липсващ превод се вижда веднага (KeyError при добавяне на ключ
     само за единия език се хваща от валидацията по-долу);
  3. кодът остава четим - без дублирани if lang == ... навсякъде.

Езикът се избира с environment променливата APP_LANG (bg | en),
по подразбиране: bg. UI-ят (app.py) я сменя и по време на работа.
"""

import os

SUPPORTED_LANGS = ("bg", "en")
DEFAULT_LANG = "bg"


def get_lang() -> str:
    """Текущият език - чете се при ВСЯКО извикване, за да може UI-ят
    да го сменя по време на работа (както LLM_PROVIDER в config.py)."""
    lang = os.getenv("APP_LANG", DEFAULT_LANG).strip().lower()
    return lang if lang in SUPPORTED_LANGS else DEFAULT_LANG


def t(key: str, **kwargs) -> str:
    """Връща текста за 'key' на текущия език, с форматиране по избор."""
    entry = STRINGS[key]
    text = entry.get(get_lang(), entry[DEFAULT_LANG])
    return text.format(**kwargs) if kwargs else text


def pick(texts: dict):
    """Избира версията за текущия език от per-language речник ({"bg": ..., "en": ...}).

    Ползва се от модулите, които държат СВОИ двуезични текстове (промптове,
    mock данни, генериран markdown) вместо да ги слагат в STRINGS."""
    return texts.get(get_lang(), texts[DEFAULT_LANG])


# ---------------------------------------------------------------------------
# Всички потребителски текстове, по ключ и език
# ---------------------------------------------------------------------------

STRINGS = {
    # --- Общи -----------------------------------------------------------
    "default_task": {
        "bg": "Имплементирай тикет DEV-101 и се увери, че кодът е качествен.",
        "en": "Implement ticket DEV-101 and make sure the code is high quality.",
    },
    "app_title": {
        "bg": "MULTI-BOT - мултиагентна SDLC система (Supervisor + Analyst + Developer + QA)",
        "en": "MULTI-BOT - multi-agent SDLC system (Supervisor + Analyst + Developer + QA)",
    },
    "task_label": {"bg": "Задача", "en": "Task"},

    # --- Детерминистичен routing и Definition of Done ----------------------
    # Тези текстове ги произвежда самият граф (src/graph.py) като 'reason'
    # на детерминистичните преходи - езикът се решава при извикване.
    "route_spec_ready": {
        "bg": "Спецификацията е готова - следва Developer.",
        "en": "The specification is ready - Developer is next.",
    },
    "route_code_ready": {
        "bg": "Кодът е готов и синтактично валиден - следва QA.",
        "en": "The code is ready and syntactically valid - QA is next.",
    },
    "route_qa_approved": {
        "bg": "QA одобри кода - задачата е завършена.",
        "en": "QA approved the code - the task is complete.",
    },
    "route_qa_needs_work": {
        "bg": "QA върна забележки (поправка {n}/{max}) - обратно към Developer.",
        "en": "QA returned issues (rework {n}/{max}) - back to Developer.",
    },
    "route_rework_limit": {
        "bg": "Лимитът от {max} поправки е изчерпан - ескалация към човек.",
        "en": "The limit of {max} rework cycles is exhausted - escalating to a human.",
    },
    "route_dod_retry": {
        "bg": "{agent} не покри Definition of Done ({problem}) - един повторен опит.",
        "en": "{agent} did not meet the Definition of Done ({problem}) - one retry.",
    },
    "route_dod_failed": {
        "bg": "{agent} не покри Definition of Done и след повторния опит - ескалация към човек.",
        "en": "{agent} did not meet the Definition of Done even after the retry - escalating to a human.",
    },
    "dod_missing_spec": {
        "bg": "празна спецификация",
        "en": "empty specification",
    },
    "dod_missing_code": {
        "bg": "липсва ```python блок с код",
        "en": "missing ```python code block",
    },
    "dod_syntax_error": {
        "bg": "синтактична грешка: {error}",
        "en": "syntax error: {error}",
    },
    "dod_empty_plan": {
        "bg": "празен план за имплементация (нито една стъпка)",
        "en": "empty implementation plan (no steps)",
    },
    "dod_empty_test_plan": {
        "bg": "празен тест-план (нито един тест-случай)",
        "en": "empty test plan (no test cases)",
    },
    "dod_no_diff": {
        "bg": "няма променени файлове в нито един репозиторий",
        "en": "no changed files in any repository",
    },
    "dod_changed_file_syntax_error": {
        "bg": "синтактична грешка в {file}: {error}",
        "en": "syntax error in {file}: {error}",
    },
    "dod_no_step_done": {
        "bg": "нито една стъпка от плана не е отметната като done (update_plan_step)",
        "en": "no plan step is marked done (update_plan_step)",
    },
    "route_run_started": {
        "bg": "Run {run_id} започна - следва triage на супервайзора.",
        "en": "Run {run_id} started - the supervisor's triage is next.",
    },
    "route_run_resumed": {
        "bg": "Run {run_id} продължава (resume) - следва triage на супервайзора.",
        "en": "Run {run_id} resumes - the supervisor's triage is next.",
    },
    "route_workspace_failed": {
        "bg": "Подготовката на git workspace се провали: {error} - ескалация към човек.",
        "en": "Preparing the git workspace failed: {error} - escalating to a human.",
    },
    "route_plan_ready": {
        "bg": "Планът за имплементация е готов ({steps} стъпки) - следва одобрение / Developer.",
        "en": "The implementation plan is ready ({steps} steps) - approval / Developer is next.",
    },
    "route_test_plan_ready": {
        "bg": "Тест-планът е готов ({cases} тест-случаи) - следва QA.",
        "en": "The test plan is ready ({cases} test cases) - QA is next.",
    },
    "route_diff_ready": {
        "bg": "Промените са в workspace-а ({files} файла, валиден синтаксис) - следва QA.",
        "en": "The changes are in the workspace ({files} files, valid syntax) - QA is next.",
    },
    "route_run_finished": {
        "bg": "Run-ът приключи със статус {status}.",
        "en": "The run finished with status {status}.",
    },
    "route_gate_approved": {
        "bg": "Човек одобри на порта '{gate}' - продължаваме.",
        "en": "A human approved at gate '{gate}' - continuing.",
    },
    "route_gate_revise": {
        "bg": "Човек поиска промени на порта '{gate}': {feedback}",
        "en": "A human requested changes at gate '{gate}': {feedback}",
    },
    "route_gate_aborted": {
        "bg": "Човек прекрати на порта '{gate}'.",
        "en": "A human aborted at gate '{gate}'.",
    },
    "route_gate_auto": {
        "bg": "Порта '{gate}': автоматично одобрение (изключена или HITL_AUTO_APPROVE).",
        "en": "Gate '{gate}': automatic approval (disabled or HITL_AUTO_APPROVE).",
    },
    "route_gate_retry": {
        "bg": "Човек поиска повторен опит - обратно към {node} с указания.",
        "en": "A human requested a retry - back to {node} with guidance.",
    },
    "route_gate_revisions_exhausted": {
        "bg": "Лимитът от {max} ревизии е изчерпан - планът се приема както е.",
        "en": "The limit of {max} revisions is exhausted - the plan is accepted as is.",
    },
    "hitl_revise_request": {
        "bg": "Човек прегледа резултата на порта '{gate}' и иска промени: {feedback}. Отрази ги и върни ПЪЛНИЯ резултат отново.",
        "en": "A human reviewed the result at gate '{gate}' and requests changes: {feedback}. Apply them and return the FULL result again.",
    },
    "hitl_retry_request": {
        "bg": "Човек разгледа ескалацията и дава указания за повторен опит: {feedback}",
        "en": "A human reviewed the escalation and gives guidance for a retry: {feedback}",
    },
    "tool_plan_step_updated": {
        "bg": "Стъпка {step_id} -> {status}. Планът е обновен.",
        "en": "Step {step_id} -> {status}. The plan is updated.",
    },
    "tool_plan_step_unknown": {
        "bg": "Непозната стъпка '{step_id}'. Налични: {available}",
        "en": "Unknown step '{step_id}'. Available: {available}",
    },
    "tool_plan_status_invalid": {
        "bg": "Невалиден статус '{status}'. Позволени: {allowed}",
        "en": "Invalid status '{status}'. Allowed: {allowed}",
    },
    "tool_plan_missing": {
        "bg": "Няма план за този run - продължи без отчитане на прогреса.",
        "en": "There is no plan for this run - continue without progress tracking.",
    },
    "tool_test_case_updated": {
        "bg": "Тест-случай {case_id} -> {status}. Тест-планът е обновен.",
        "en": "Test case {case_id} -> {status}. The test plan is updated.",
    },
    "tool_test_case_unknown": {
        "bg": "Непознат тест-случай '{case_id}'. Налични: {available}",
        "en": "Unknown test case '{case_id}'. Available: {available}",
    },
    "prod_problem_no_repos": {
        "bg": "Няма конфигурирани репозитории (PROD_REPOS в .env).",
        "en": "No repositories configured (PROD_REPOS in .env).",
    },
    "prod_problem_no_repos_selected": {
        "bg": "Избери поне един репозиторий.",
        "en": "Select at least one repository.",
    },
    "prod_problem_jira_missing": {
        "bg": "Липсват Jira настройки: {vars} (виж .env.example).",
        "en": "Missing Jira settings: {vars} (see .env.example).",
    },
    "prod_problem_not_available": {
        "bg": "Git интеграцията на prod режима (workspace, draft PR) идва в следващата стъпка - Developer/QA работят както в demo.",
        "en": "The prod git integration (workspace, draft PR) arrives in the next step - Developer/QA work as in demo.",
    },
    "jira_line": {
        "bg": "Jira (Atlassian MCP): {cloud} · автентикация: {auth}",
        "en": "Jira (Atlassian MCP): {cloud} · auth: {auth}",
    },
    "ui_jira_status": {
        "bg": "🔗 Jira през Atlassian MCP: `{cloud}` · автентикация: `{auth}`",
        "en": "🔗 Jira via Atlassian MCP: `{cloud}` · auth: `{auth}`",
    },
    "ui_jira_test": {"bg": "Тест на връзката с Jira", "en": "Test the Jira connection"},
    "ui_jira_ok": {"bg": "Jira отговаря:\n\n{details}", "en": "Jira responds:\n\n{details}"},
    "ui_jira_error": {"bg": "Jira не отговаря: {error}", "en": "Jira does not respond: {error}"},
    "ui_prod_git_pending": {
        "bg": "Git интеграцията (workspace, draft PR) идва в следващата стъпка - в prod Developer/QA засега работят както в demo.",
        "en": "The git integration (workspace, draft PR) arrives in the next step - in prod Developer/QA work as in demo for now.",
    },
    "dod_fix_request": {
        "bg": (
            "Резултатът ти не покри Definition of Done на фазата: {problem}. "
            "Поправи проблема и върни ПЪЛНИЯ резултат отново."
        ),
        "en": (
            "Your result did not meet the phase's Definition of Done: {problem}. "
            "Fix the problem and return the FULL result again."
        ),
    },
    "final_status_line": {
        "bg": "Финален статус: {status}",
        "en": "Final status: {status}",
    },
    "thread_line": {
        "bg": "Checkpointing: включен | thread_id: {thread} (същият thread_id продължава run-а)",
        "en": "Checkpointing: on | thread_id: {thread} (the same thread_id resumes the run)",
    },
    "budget_stop": {
        "bg": (
            "СТОП: бюджетът за изпълнение (~${limit}) е надвишен "
            "(изразходвано ~${spent}) - изпълнението е прекратено."
        ),
        "en": (
            "STOP: the run budget (~${limit}) has been exceeded "
            "(~${spent} spent) - the run was aborted."
        ),
    },

    # --- Стъпки на изпълнението ------------------------------------------
    "step_supervisor": {
        "bg": "СТЪПКА {n} | SUPERVISOR решава: -> {next}",
        "en": "STEP {n} | SUPERVISOR decides: -> {next}",
    },
    "reason": {"bg": "  Причина: {reason}", "en": "  Reason: {reason}"},
    "handoff": {
        "bg": "  Управлението преминава към [{agent}]:",
        "en": "  Handing control to [{agent}]:",
    },
    "final_answer": {
        "bg": "СТЪПКА {n} | [{agent}] финален отговор:",
        "en": "STEP {n} | [{agent}] final answer:",
    },
    "tool_call": {
        "bg": "    [{agent}] вика инструмент: {tool}({args})",
        "en": "    [{agent}] calls tool: {tool}({args})",
    },
    "tool_result": {
        "bg": "    [{agent}] <- {tool}: {result}",
        "en": "    [{agent}] <- {tool}: {result}",
    },
    "shortened": {
        "bg": "... [съкратено, общо {total} символа]",
        "en": "... [truncated, {total} characters total]",
    },

    # --- Финално обобщение ------------------------------------------------
    "summary_title": {"bg": "ОБОБЩЕНИЕ НА ПРОЦЕСА", "en": "PROCESS SUMMARY"},
    "route": {"bg": "Маршрут: {route}", "en": "Route: {route}"},
    "steps_line": {
        "bg": "Стъпки в главния граф: {steps} | Извиквания на инструменти: {tools}",
        "en": "Main graph steps: {steps} | Tool calls: {tools}",
    },
    "tokens_title": {
        "bg": "Консумирани токъни и цена:",
        "en": "Tokens consumed and cost:",
    },
    "token_line": {
        "bg": "  {model}: вход {inp} | изход {out} | общо {total} | {cost}",
        "en": "  {model}: input {inp} | output {out} | total {total} | {cost}",
    },
    "local_model_cost": {
        "bg": "$0.00 (локален модел)",
        "en": "$0.00 (local model)",
    },
    "total_cost": {"bg": "  Обща цена: ~${cost}", "en": "  Total cost: ~${cost}"},
    "no_token_data": {
        "bg": "  (моделът не върна данни за токъни)",
        "en": "  (the model returned no token usage data)",
    },
    "done": {
        "bg": "ГОТОВО - задачата премина през целия SDLC процес.",
        "en": "DONE - the task went through the full SDLC process.",
    },
    "mode_line": {"bg": "Режим: {mode}", "en": "Mode: {mode}"},
    "hitl_line": {
        "bg": "Human-in-the-Loop порти: {gates}",
        "en": "Human-in-the-Loop gates: {gates}",
    },
    "run_dir_line": {
        "bg": "Директория на run-а: {path}",
        "en": "Run directory: {path}",
    },
    "plan_file_line": {
        "bg": "  {name}: {path}",
        "en": "  {name}: {path}",
    },
    "artifacts_line": {
        "bg": "Артефакти (планове, стъпки, статус, обобщение): {run_dir}",
        "en": "Artifacts (plans, steps, status, summary): {run_dir}",
    },
    "hitl_prompt": {
        "bg": "\n>>> Human-in-the-Loop: порта '{gate}' чака твоето решение.",
        "en": "\n>>> Human-in-the-Loop: gate '{gate}' is waiting for your decision.",
    },
    "hitl_options": {
        "bg": "    [a] одобри   [r] поискай промени (напиши: r: твоите указания)   [q] прекрати\n    Решение: ",
        "en": "    [a] approve   [r] request changes (type: r: your guidance)   [q] abort\n    Decision: ",
    },
    "hitl_no_tty": {
        "bg": "Няма интерактивна конзола за HITL решение. Задай HITL_AUTO_APPROVE=1 или HITL_GATES= (без порти).",
        "en": "No interactive console for the HITL decision. Set HITL_AUTO_APPROVE=1 or HITL_GATES= (no gates).",
    },
    "hitl_summary_line": {
        "bg": "Human-in-the-Loop решения: {n} (виж hitl-decisions.md)",
        "en": "Human-in-the-Loop decisions: {n} (see hitl-decisions.md)",
    },
    "prod_problems_title": {
        "bg": "Prod режимът не може да стартира:",
        "en": "Prod mode cannot start:",
    },

    # --- Streamlit UI ------------------------------------------------------
    "ui_sidebar_title": {"bg": "🤖 Multi-Bot", "en": "🤖 Multi-Bot"},
    "ui_sidebar_caption": {
        "bg": "Supervisor + Analyst + Developer + QA (LangGraph)",
        "en": "Supervisor + Analyst + Developer + QA (LangGraph)",
    },
    "ui_language": {"bg": "Език / Language", "en": "Език / Language"},
    "ui_provider": {"bg": "LLM доставчик", "en": "LLM provider"},
    "ui_provider_anthropic": {
        "bg": "Anthropic Claude (облак)",
        "en": "Anthropic Claude (cloud)",
    },
    "ui_provider_ollama": {
        "bg": "Ollama (локално, офлайн)",
        "en": "Ollama (local, offline)",
    },
    "ui_provider_help": {
        "bg": "Ollama работи без интернет, но е по-бавен и качеството е под Claude.",
        "en": "Ollama works without internet, but is slower and below Claude in quality.",
    },
    "ui_model": {"bg": "Модел: `{model}`", "en": "Model: `{model}`"},
    "ui_ollama_warning": {
        "bg": "Локалният модел е бавен: пълен цикъл отнема няколко минути.",
        "en": "The local model is slow: a full cycle takes several minutes.",
    },
    "ui_legend": {
        "bg": (
            "**Как се чете визуализацията:**\n"
            "- 🧭 решение на супервайзора (кой е следващ и защо)\n"
            "- 🔧 агент извиква инструмент\n"
            "- 📥 резултат от инструмента\n"
            "- 💬 финален отговор на агента"
        ),
        "en": (
            "**How to read the visualization:**\n"
            "- 🧭 supervisor decision (who is next and why)\n"
            "- 🔧 agent calls a tool\n"
            "- 📥 tool result\n"
            "- 💬 agent's final answer"
        ),
    },
    "ui_page_title": {
        "bg": "Multi-Bot — мултиагентна SDLC система",
        "en": "Multi-Bot — multi-agent SDLC system",
    },
    "ui_run": {"bg": "▶ Стартирай", "en": "▶ Run"},
    "ui_building_graph": {"bg": "Сглобяване на графа...", "en": "Building the graph..."},
    "ui_status_supervisor_first": {
        "bg": "🧭 Супервайзорът решава кой агент да работи пръв...",
        "en": "🧭 The supervisor is deciding which agent works first...",
    },
    "ui_status_supervisor_next": {
        "bg": "🧭 Супервайзорът преценява следващата стъпка...",
        "en": "🧭 The supervisor is deciding the next step...",
    },
    "ui_status_working": {"bg": "{icon} {agent} работи...", "en": "{icon} {agent} is working..."},
    "ui_status_tool": {
        "bg": "🔧 {agent} изпълнява {tool}...",
        "en": "🔧 {agent} is running {tool}...",
    },
    "ui_status_seconds": {"bg": "{label}  ·  {sec} сек", "en": "{label}  ·  {sec} s"},
    "ui_status_done": {
        "bg": "✅ Готово за {sec} сек",
        "en": "✅ Done in {sec} s",
    },
    "ui_status_error": {"bg": "❌ Грешка", "en": "❌ Error"},
    "ui_step_supervisor": {
        "bg": "🧭 **Стъпка {n} · SUPERVISOR** → **{next}**\n\n{reason}",
        "en": "🧭 **Step {n} · SUPERVISOR** → **{next}**\n\n{reason}",
    },
    "ui_step_final": {
        "bg": "{icon} **Стъпка {n} · {agent} - финален отговор**",
        "en": "{icon} **Step {n} · {agent} - final answer**",
    },
    "ui_tool_call": {
        "bg": "🔧 `{agent}` извиква **{tool}** `{args}`",
        "en": "🔧 `{agent}` calls **{tool}** `{args}`",
    },
    "ui_tool_result": {
        "bg": "📥 `{agent}` ← резултат от **{tool}**",
        "en": "📥 `{agent}` ← result from **{tool}**",
    },
    "ui_spinner": {
        "bg": "Екипът работи... (следи стъпките по-долу на живо)",
        "en": "The team is working... (watch the steps below live)",
    },
    "ui_summary": {
        "bg": (
            "**Готово!** Маршрут: {route}\n\n"
            "Стъпки в главния граф: {steps} · Извиквания на инструменти: {tools} · "
            "Време: {sec} сек\n\n**Консумирани токъни:**\n\n{token_lines}"
        ),
        "en": (
            "**Done!** Route: {route}\n\n"
            "Main graph steps: {steps} · Tool calls: {tools} · "
            "Time: {sec} s\n\n**Tokens consumed:**\n\n{token_lines}"
        ),
    },
    "ui_token_line": {
        "bg": "`{model}`: вход {inp} · изход {out} · общо {total} · **{cost}**",
        "en": "`{model}`: input {inp} · output {out} · total {total} · **{cost}**",
    },
    "ui_total_cost": {"bg": "**Обща цена: ~${cost}**", "en": "**Total cost: ~${cost}**"},
    "ui_error": {
        "bg": "Изпълнението се провали: {error}",
        "en": "The run failed: {error}",
    },
    "ui_mode": {"bg": "Режим", "en": "Mode"},
    "ui_mode_prod": {
        "bg": "Prod - реална Jira, git репозитории, draft PR",
        "en": "Prod - real Jira, git repositories, draft PR",
    },
    "ui_mode_demo": {
        "bg": "Demo - mock тикети (DEV-101, DEV-102), без git",
        "en": "Demo - mock tickets (DEV-101, DEV-102), no git",
    },
    "ui_mode_help": {
        "bg": "Prod чете тикетите от Jira през официалния Atlassian MCP сървър и работи в клонирани репозитории. Demo ползва статичните примерни тикети.",
        "en": "Prod reads tickets from Jira via the official Atlassian MCP server and works in cloned repositories. Demo uses the static sample tickets.",
    },
    "ui_mode_banner": {
        "bg": "Режим: **{mode}** · Human-in-the-Loop порти: {gates}",
        "en": "Mode: **{mode}** · Human-in-the-Loop gates: {gates}",
    },
    "ui_demo_hint": {
        "bg": "Demo режим: наличните примерни тикети са DEV-101 и DEV-102.",
        "en": "Demo mode: the available sample tickets are DEV-101 and DEV-102.",
    },
    "ui_prod_config_error": {
        "bg": "Prod режимът не може да стартира:\n\n{problems}",
        "en": "Prod mode cannot start:\n\n{problems}",
    },
    "ui_hitl_gates": {"bg": "Human-in-the-Loop порти", "en": "Human-in-the-Loop gates"},
    "ui_hitl_gate_plan": {
        "bg": "Одобрение на плана за имплементация",
        "en": "Approve the implementation plan",
    },
    "ui_hitl_gate_publish": {
        "bg": "Одобрение преди публикуване (push / draft PR)",
        "en": "Approve before publishing (push / draft PR)",
    },
    "ui_hitl_gate_escalation": {
        "bg": "Решение при ескалация (повторен опит или край)",
        "en": "Decide on escalation (retry or stop)",
    },
    "ui_hitl_help": {
        "bg": "Графът спира на избраните порти и чака твоето решение. Изключена порта = автоматично одобрение.",
        "en": "The graph pauses at the selected gates and waits for your decision. A disabled gate = automatic approval.",
    },
    "ui_hitl_title": {
        "bg": "🙋 **Human-in-the-Loop · порта `{gate}`** - графът чака твоето решение",
        "en": "🙋 **Human-in-the-Loop · gate `{gate}`** - the graph is waiting for your decision",
    },
    "ui_hitl_approve": {"bg": "✅ Одобри", "en": "✅ Approve"},
    "ui_hitl_revise": {"bg": "✏️ Поискай промени", "en": "✏️ Request changes"},
    "ui_hitl_abort": {"bg": "⛔ Прекрати", "en": "⛔ Abort"},
    "ui_hitl_feedback": {
        "bg": "Коментар / указания (задължителни при „Поискай промени“)",
        "en": "Comment / guidance (required for “Request changes”)",
    },
    "ui_hitl_feedback_required": {
        "bg": "Напиши какви промени искаш, преди да поискаш ревизия.",
        "en": "Describe the changes you want before requesting a revision.",
    },
    "ui_hitl_decided": {
        "bg": "🙋 Решение на порта `{gate}`: **{action}** ({by}) {feedback}",
        "en": "🙋 Decision at gate `{gate}`: **{action}** ({by}) {feedback}",
    },
    "ui_hitl_status": {
        "bg": "🙋 Чака човешко решение на порта {gate}...",
        "en": "🙋 Waiting for a human decision at gate {gate}...",
    },
    "ui_plan_expander": {
        "bg": "📝 {title} · `{path}`",
        "en": "📝 {title} · `{path}`",
    },
    "ui_plan_title_implementation": {"bg": "План за имплементация", "en": "Implementation plan"},
    "ui_plan_title_test": {"bg": "Тест-план", "en": "Test plan"},
    "ui_plan_update": {
        "bg": "📝 `{agent}` отбелязва **{id}** → {status}",
        "en": "📝 `{agent}` marks **{id}** → {status}",
    },
    "ui_artifacts": {
        "bg": "📁 Артефакти на run-а · `{run_dir}`",
        "en": "📁 Run artifacts · `{run_dir}`",
    },
    "ui_run_dir": {
        "bg": "📁 Run директория: `{path}`",
        "en": "📁 Run directory: `{path}`",
    },
}

# Валидация при import: всеки ключ трябва да има ВСИЧКИ поддържани езици.
# Хваща "полупреведени" ключове още при стартиране, не по време на работа.
for _key, _entry in STRINGS.items():
    _missing = [lang for lang in SUPPORTED_LANGS if lang not in _entry]
    if _missing:
        raise ValueError(f"i18n ключ '{_key}' няма превод за: {_missing}")
