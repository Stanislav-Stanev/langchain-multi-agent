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
}

# Валидация при import: всеки ключ трябва да има ВСИЧКИ поддържани езици.
# Хваща "полупреведени" ключове още при стартиране, не по време на работа.
for _key, _entry in STRINGS.items():
    _missing = [lang for lang in SUPPORTED_LANGS if lang not in _entry]
    if _missing:
        raise ValueError(f"i18n ключ '{_key}' няма превод за: {_missing}")
