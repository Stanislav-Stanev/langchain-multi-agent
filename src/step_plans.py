"""
"План преди всяка стъпка" - шаблоните за steps/NN-<възел>.md, index.md и STATUS.md.

Добра практика за проследимост на агентни системи: всяка стъпка оставя
ЕДИН документ в две части - какво е планирано (написано ПРЕДИ
изпълнението) и какво реално се случи (допълнено СЛЕД него). Така след
края на flow-а директорията runs/<run>/steps/ е пълният одит: кой възел,
кога, с каква цел, какво е произвел, дали е покрил своя Definition of Done
и къде е предал управлението.

Шаблонът следва структурата на добрите планове:
    цел -> контекст/входове -> обхват и не-обхват -> стъпки с критерий за
    готовност -> рискове/допускания -> Definition of Done -> статус/време,
после фактите от изпълнението (резултат, артефакти, DoD, решение).

Кой пише: обвивката _traced в src/graph.py вика plan_section() преди възела
и execution_section() след него; RunTracker пази записите в steps.json и
пре-рендира md файловете (идемпотентно - повторно изпълнение при resume
презаписва същия файл, а не добавя втори).

Текстовете тук са ГЕНЕРИРАН markdown на двата езика - държим ги в
per-language речници като промптовете (виж src/agents.py), не в i18n.STRINGS.
"""

from src.i18n import pick
from src.modes import max_rework

# Вид на всеки възел - определя иконата и шаблона на плана
NODE_KIND = {
    "init_run": "code",
    "supervisor": "llm",
    "analyst": "agent",
    "dev_plan": "llm",
    "approve_plan": "hitl",
    "developer": "agent",
    "qa_plan": "llm",
    "qa": "agent",
    "approve_publish": "hitl",
    "escalation_gate": "hitl",
    "finalize": "code",
}

KIND_LABELS = {
    "bg": {"code": "код (детерминистично)", "llm": "LLM (структурирано решение)",
           "agent": "агент (ReAct)", "hitl": "Human-in-the-Loop порта"},
    "en": {"code": "code (deterministic)", "llm": "LLM (structured decision)",
           "agent": "agent (ReAct)", "hitl": "Human-in-the-Loop gate"},
}

# План по подразбиране за всеки възел: цел, входове, обхват, DoD.
# {task}, {ticket}, {n_messages}, {rework}, {max_rework} се попълват от state.
STEP_PLANS = {
    "bg": {
        "init_run": {
            "goal": "Създаване на run директорията и начална регистрация на задачата.",
            "inputs": "Текстът на задачата: {task}",
            "scope": "Извличане на ключ на тикет, run_id, папка runs/<дата_час>_<ключ>/; в prod - подготовка на git workspace.",
            "dod": "run.json съществува; state съдържа run_id и run_dir.",
        },
        "supervisor": {
            "goal": "Еднократен triage: откъде да влезе задачата (analyst / developer / qa) или FINISH.",
            "inputs": "Задачата ({n_messages} съобщения в историята).",
            "scope": "Само входната точка - всичко след това е детерминистичен код.",
            "dod": "Структурирано решение SupervisorDecision с причина.",
        },
        "analyst": {
            "goal": "Техническа спецификация по изискванията (тикет {ticket}).",
            "inputs": "Задачата; детайлите на тикета през get_ticket_details.",
            "scope": "Изисквания, входове/изходи, гранични случаи, критерии за приемане. Без код, без тестове.",
            "dod": "Непразна спецификация (spec.md).",
        },
        "dev_plan": {
            "goal": "План за имплементация: стъпки с файлове и покрити критерии за приемане.",
            "inputs": "Спецификацията на Analyst.",
            "scope": "Един структуриран LLM изход (ImplementationPlan) -> implementation-plan.md с чекбоксове.",
            "dod": "Поне една стъпка; всяка стъпка има критерий, който покрива.",
        },
        "approve_plan": {
            "goal": "Човек одобрява плана за имплементация, преди Developer да пише код.",
            "inputs": "implementation-plan.md, spec.md.",
            "scope": "Решение: approve / revise (с указания -> ново планиране) / abort.",
            "dod": "Записано човешко решение в hitl-decisions.md.",
        },
        "developer": {
            "goal": "Имплементация по плана (поправка {rework}/{max_rework}).",
            "inputs": "Спецификация, план за имплементация, забележки от QA (ако има).",
            "scope": "Кодът по стъпките от плана; всяка стъпка се маркира с update_plan_step.",
            "dod": "Demo: ```python блок с валиден синтаксис. Prod: непразен diff, валидни .py файлове, поне една стъпка done.",
        },
        "qa_plan": {
            "goal": "Тест-план: какво и как се проверява за всеки критерий за приемане.",
            "inputs": "Спецификация, план за имплементация, кодът.",
            "scope": "Един структуриран LLM изход (TestPlan) -> qa-plan.md.",
            "dod": "Поне един тест-случай; всеки сочи критерий за приемане.",
        },
        "qa": {
            "goal": "Преглед на кода по тест-плана и структурирана присъда (QAVerdict).",
            "inputs": "Кодът, спецификацията, qa-plan.md.",
            "scope": "Проверки с инструментите; всеки тест-случай се маркира с update_test_case; доклад -> присъда.",
            "dod": "QAVerdict (APPROVED / NEEDS_WORK) с конкретни забележки.",
        },
        "approve_publish": {
            "goal": "Човек одобрява публикуването (commit, push, draft PR) на промените.",
            "inputs": "code.diff, qa-plan.md с резултати, преглед на PR тялото.",
            "scope": "Решение: approve / revise (обратно към Developer) / abort (без публикуване).",
            "dod": "Записано човешко решение.",
        },
        "escalation_gate": {
            "goal": "Системата не може да продължи сама - човек решава: повторен опит с указания или край.",
            "inputs": "Причината за ескалация, последният артефакт/доклад.",
            "scope": "retry (с указания към провалилия се възел) / abort.",
            "dod": "Записано човешко решение.",
        },
        "finalize": {
            "goal": "Приключване на run-а: обобщение, матрица на проследимост; в prod - публикуване.",
            "inputs": "Цялото състояние на run-а.",
            "scope": "traceability.md, summary.json; commit/push/PR при APPROVED в prod.",
            "dod": "summary.json записан; next=FINISH.",
        },
    },
    "en": {
        "init_run": {
            "goal": "Create the run directory and register the task.",
            "inputs": "The task text: {task}",
            "scope": "Extract the ticket key, run_id, folder runs/<date_time>_<key>/; in prod - prepare the git workspace.",
            "dod": "run.json exists; state has run_id and run_dir.",
        },
        "supervisor": {
            "goal": "One-time triage: where the task enters (analyst / developer / qa) or FINISH.",
            "inputs": "The task ({n_messages} messages in the history).",
            "scope": "Only the entry point - everything after is deterministic code.",
            "dod": "A structured SupervisorDecision with a reason.",
        },
        "analyst": {
            "goal": "Technical specification from the requirements (ticket {ticket}).",
            "inputs": "The task; ticket details via get_ticket_details.",
            "scope": "Requirements, inputs/outputs, edge cases, acceptance criteria. No code, no tests.",
            "dod": "A non-empty specification (spec.md).",
        },
        "dev_plan": {
            "goal": "Implementation plan: steps with files and covered acceptance criteria.",
            "inputs": "The Analyst's specification.",
            "scope": "One structured LLM output (ImplementationPlan) -> implementation-plan.md with checkboxes.",
            "dod": "At least one step; every step covers a criterion.",
        },
        "approve_plan": {
            "goal": "A human approves the implementation plan before the Developer writes code.",
            "inputs": "implementation-plan.md, spec.md.",
            "scope": "Decision: approve / revise (with guidance -> re-plan) / abort.",
            "dod": "A recorded human decision in hitl-decisions.md.",
        },
        "developer": {
            "goal": "Implementation following the plan (rework {rework}/{max_rework}).",
            "inputs": "Specification, implementation plan, QA issues (if any).",
            "scope": "Code per the plan steps; every step is marked via update_plan_step.",
            "dod": "Demo: a ```python block with valid syntax. Prod: non-empty diff, valid .py files, at least one step done.",
        },
        "qa_plan": {
            "goal": "Test plan: what and how to verify for every acceptance criterion.",
            "inputs": "Specification, implementation plan, the code.",
            "scope": "One structured LLM output (TestPlan) -> qa-plan.md.",
            "dod": "At least one test case; every case points to an acceptance criterion.",
        },
        "qa": {
            "goal": "Code review per the test plan and a structured verdict (QAVerdict).",
            "inputs": "The code, the specification, qa-plan.md.",
            "scope": "Tool checks; every test case is marked via update_test_case; report -> verdict.",
            "dod": "QAVerdict (APPROVED / NEEDS_WORK) with concrete issues.",
        },
        "approve_publish": {
            "goal": "A human approves publishing (commit, push, draft PR) the changes.",
            "inputs": "code.diff, qa-plan.md with results, PR body preview.",
            "scope": "Decision: approve / revise (back to Developer) / abort (no publishing).",
            "dod": "A recorded human decision.",
        },
        "escalation_gate": {
            "goal": "The system cannot continue on its own - a human decides: retry with guidance or stop.",
            "inputs": "The escalation reason, the last artifact/report.",
            "scope": "retry (with guidance to the failed node) / abort.",
            "dod": "A recorded human decision.",
        },
        "finalize": {
            "goal": "Close the run: summary, traceability matrix; in prod - publishing.",
            "inputs": "The whole run state.",
            "scope": "traceability.md, summary.json; commit/push/PR on APPROVED in prod.",
            "dod": "summary.json written; next=FINISH.",
        },
    },
}

_TXT = {
    "bg": {
        "step_title": "# Стъпка {seq:02d} · {node} · {kind}",
        "status_line": "Статус: **{status}** · Начало: {started} · Край: {finished} · Продължителност: {duration}",
        "plan_heading": "## План (написан преди изпълнението)",
        "goal": "- Цел: {goal}",
        "inputs": "- Входове: {inputs}",
        "scope": "- Обхват: {scope}",
        "dod": "- Definition of Done: {dod}",
        "exec_heading": "## Изпълнение (допълнено след стъпката)",
        "pending": "_(стъпката още се изпълнява)_",
        "result": "- Резултат: {result}",
        "artifacts": "- Артефакти: {artifacts}",
        "dod_ok": "- DoD: покрит",
        "dod_fail": "- DoD: непокрит -> {problem}",
        "decision": "- Решение: next=`{next}` · причина: {reason}",
        "final_status": "- Финален статус: {status}",
        "error": "- Грешка: {error}",
        "index_title": "# Стъпки на run {run_id}",
        "index_header": "| # | Възел | Вид | Статус | Продължителност | Следващ | Файл |",
        "index_sep": "|---|-------|-----|--------|-----------------|---------|------|",
        "index_row": "| {seq:02d} | {node} | {kind} | {status} | {duration} | {next} | [{file}](steps/{file}) |",
        "none": "—",
        "status_title": "# Статус на run {run_id}",
        "status_mode": "- Режим: **{mode}**",
        "status_ticket": "- Тикет: {ticket}",
        "status_task": "- Задача: {task}",
        "status_started": "- Старт: {started}",
        "status_phase": "- Текуща фаза: **{phase}** (стъпка {seq})",
        "status_next": "- Следващ възел: {next}",
        "status_rework": "- Поправки от QA: {rework}/{max_rework}",
        "status_dod": "- DoD повторни опити: {dod}",
        "status_plan": "- План за имплементация: {done} готови · {in_progress} в ход · {todo} чакат · {blocked} блокирани",
        "status_tests": "- Тест-план: {passed} минали · {failed} провалени · {running} в ход · {planned} чакат",
        "status_hitl": "- Human-in-the-Loop: {hitl}",
        "status_hitl_waiting": "чака решение на порта **{gate}**",
        "status_hitl_decisions": "{n} решения (виж hitl-decisions.md)",
        "status_final": "- Финален статус: **{status}**",
        "status_publish": "- Публикуване: {status}",
        "status_prs": "- Pull Requests: {prs}",
        "status_artifacts": "- Артефакти: {artifacts}",
        "status_updated": "_Обновено: {ts}_",
        "hitl_title": "# Human-in-the-Loop решения",
        "hitl_header": "| Кога | Порта | Решение | От | Коментар |",
        "hitl_sep": "|------|-------|---------|----|----------|",
        "hitl_row": "| {ts} | {gate} | {action} | {by} | {feedback} |",
    },
    "en": {
        "step_title": "# Step {seq:02d} · {node} · {kind}",
        "status_line": "Status: **{status}** · Started: {started} · Finished: {finished} · Duration: {duration}",
        "plan_heading": "## Plan (written before execution)",
        "goal": "- Goal: {goal}",
        "inputs": "- Inputs: {inputs}",
        "scope": "- Scope: {scope}",
        "dod": "- Definition of Done: {dod}",
        "exec_heading": "## Execution (added after the step)",
        "pending": "_(the step is still running)_",
        "result": "- Result: {result}",
        "artifacts": "- Artifacts: {artifacts}",
        "dod_ok": "- DoD: met",
        "dod_fail": "- DoD: not met -> {problem}",
        "decision": "- Decision: next=`{next}` · reason: {reason}",
        "final_status": "- Final status: {status}",
        "error": "- Error: {error}",
        "index_title": "# Steps of run {run_id}",
        "index_header": "| # | Node | Kind | Status | Duration | Next | File |",
        "index_sep": "|---|------|------|--------|----------|------|------|",
        "index_row": "| {seq:02d} | {node} | {kind} | {status} | {duration} | {next} | [{file}](steps/{file}) |",
        "none": "—",
        "status_title": "# Status of run {run_id}",
        "status_mode": "- Mode: **{mode}**",
        "status_ticket": "- Ticket: {ticket}",
        "status_task": "- Task: {task}",
        "status_started": "- Started: {started}",
        "status_phase": "- Current phase: **{phase}** (step {seq})",
        "status_next": "- Next node: {next}",
        "status_rework": "- QA rework cycles: {rework}/{max_rework}",
        "status_dod": "- DoD retries: {dod}",
        "status_plan": "- Implementation plan: {done} done · {in_progress} in progress · {todo} todo · {blocked} blocked",
        "status_tests": "- Test plan: {passed} passed · {failed} failed · {running} running · {planned} planned",
        "status_hitl": "- Human-in-the-Loop: {hitl}",
        "status_hitl_waiting": "waiting for a decision at gate **{gate}**",
        "status_hitl_decisions": "{n} decisions (see hitl-decisions.md)",
        "status_final": "- Final status: **{status}**",
        "status_publish": "- Publishing: {status}",
        "status_prs": "- Pull Requests: {prs}",
        "status_artifacts": "- Artifacts: {artifacts}",
        "status_updated": "_Updated: {ts}_",
        "hitl_title": "# Human-in-the-Loop decisions",
        "hitl_header": "| When | Gate | Decision | By | Comment |",
        "hitl_sep": "|------|------|----------|----|---------|",
        "hitl_row": "| {ts} | {gate} | {action} | {by} | {feedback} |",
    },
}


def _t(key: str, **kwargs) -> str:
    text = pick(_TXT)[key]
    return text.format(**kwargs) if kwargs else text


def _fmt_duration(seconds) -> str:
    if seconds is None:
        return _t("none")
    return f"{seconds:.1f}s"


def _task_text(state: dict) -> str:
    messages = state.get("messages") or []
    if not messages:
        return ""
    first = messages[0]
    content = getattr(first, "content", first)
    if isinstance(content, list):
        content = " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    text = str(content).strip().replace("\n", " ")
    return text if len(text) <= 200 else text[:200] + "…"


def plan_section(node: str, state: dict) -> str:
    """Секцията „План" за възел - от детерминистичния шаблон + данни от state."""
    template = pick(STEP_PLANS).get(node)
    if template is None:
        template = {"goal": node, "inputs": "", "scope": "", "dod": ""}
    values = {
        "task": _task_text(state),
        "ticket": state.get("ticket_key") or _t("none"),
        "n_messages": len(state.get("messages") or []),
        "rework": state.get("rework_count", 0),
        "max_rework": max_rework(),
    }
    lines = [_t("plan_heading")]
    for key in ("goal", "inputs", "scope", "dod"):
        text = template.get(key, "")
        if text:
            lines.append(_t(key, **{key: text.format(**values)}))
    return "\n".join(lines)


def _artifact_names(update: dict, mode: str) -> list[str]:
    names = []
    if update.get("spec"):
        names.append("spec.md")
    if update.get("code"):
        names.append("code.diff" if mode == "prod" else "code.py")
    if update.get("qa_verdict"):
        names.append("qa-report.md")
    if update.get("plan"):
        names.append("implementation-plan.md")
    if update.get("test_plan"):
        names.append("qa-plan.md")
    if update.get("pr_urls"):
        names.append("pr-body-*.md")
    return names


def execution_section(node: str, state: dict, update: dict, *, error: str | None = None) -> str:
    """Секцията „Изпълнение" - фактите от update-а на възела."""
    lines = [_t("exec_heading")]
    if error:
        lines.append(_t("error", error=error))
        return "\n".join(lines)

    result = _result_summary(node, update)
    if result:
        lines.append(_t("result", result=result))

    artifacts = _artifact_names(update, state.get("mode") or update.get("mode") or "demo")
    if artifacts:
        lines.append(_t("artifacts", artifacts=", ".join(artifacts)))

    # DoD: повторен опит към същия възел или ескалация = непокрит DoD
    nxt = update.get("next", "")
    retried = nxt == node and update.get("dod_retries") is not None
    escalated = update.get("final_status") == "ESCALATED" and nxt in ("escalation_gate", "finalize")
    if node in ("analyst", "developer", "dev_plan", "qa_plan"):
        if retried or escalated:
            lines.append(_t("dod_fail", problem=update.get("reason", "")))
        else:
            lines.append(_t("dod_ok"))

    lines.append(_t("decision", next=nxt or _t("none"), reason=update.get("reason", "")))
    if update.get("final_status"):
        lines.append(_t("final_status", status=update["final_status"]))
    return "\n".join(lines)


def _result_summary(node: str, update: dict) -> str:
    if node == "supervisor":
        return f"triage -> {update.get('next', '')}"
    if node == "dev_plan" and update.get("plan"):
        return f"{len(update['plan'].get('steps', []))} steps"
    if node == "qa_plan" and update.get("test_plan"):
        return f"{len(update['test_plan'].get('cases', []))} test cases"
    if node == "qa" and update.get("qa_verdict"):
        verdict = update["qa_verdict"]
        issues = verdict.get("issues") or []
        return f"QAVerdict {verdict.get('status')} ({len(issues)} issues)"
    if node in ("approve_plan", "approve_publish", "escalation_gate"):
        decisions = update.get("hitl_decisions") or []
        if decisions:
            last = decisions[-1]
            return f"{last.get('action')} ({last.get('by')}) {last.get('feedback', '')}".strip()
    if node == "finalize":
        return update.get("publish_status") or update.get("final_status") or ""
    messages = update.get("messages") or []
    if messages:
        content = getattr(messages[-1], "content", "")
        text = str(content).strip().replace("\n", " ")
        return text if len(text) <= 160 else text[:160] + "…"
    return ""


def render_step_file(record: dict) -> str:
    """Целият steps/NN-<node>.md от един запис в steps.json."""
    kind = pick(KIND_LABELS).get(record.get("kind", "code"), record.get("kind", ""))
    lines = [
        _t("step_title", seq=record["seq"], node=record["node"], kind=kind),
        _t(
            "status_line",
            status=record.get("status", "PLANNED"),
            started=record.get("started") or _t("none"),
            finished=record.get("finished") or _t("none"),
            duration=_fmt_duration(record.get("duration_s")),
        ),
        "",
        record.get("plan_md", ""),
        "",
        record.get("exec_md") or (_t("exec_heading") + "\n" + _t("pending")),
        "",
    ]
    return "\n".join(lines)


def step_filename(seq: int, node: str) -> str:
    return f"{seq:02d}-{node}.md"


def render_index(run_id: str, records: list[dict]) -> str:
    """index.md - хронологичната таблица на стъпките."""
    lines = [_t("index_title", run_id=run_id), "", _t("index_header"), _t("index_sep")]
    for rec in records:
        lines.append(
            _t(
                "index_row",
                seq=rec["seq"],
                node=rec["node"],
                kind=rec.get("kind", ""),
                status=rec.get("status", ""),
                duration=_fmt_duration(rec.get("duration_s")),
                next=rec.get("next") or _t("none"),
                file=step_filename(rec["seq"], rec["node"]),
            )
        )
    lines.append("")
    return "\n".join(lines)


def render_status(view: dict, *, plan_counts: dict, test_counts: dict, artifacts: list[str], ts: str) -> str:
    """STATUS.md - „живият" статус на run-а, пре-рендиран след всяка стъпка."""
    none = _t("none")
    lines = [
        _t("status_title", run_id=view.get("run_id") or none),
        "",
        _t("status_mode", mode=view.get("mode") or none),
        _t("status_ticket", ticket=view.get("ticket_key") or none),
        # Задачата идва от run.json (view["task"]) - update-ът на възела носи
        # само НОВИТЕ съобщения, така че messages[0] тук не е оригиналната задача.
        _t("status_task", task=(view.get("task") or _task_text(view) or none)[:200]),
        _t("status_started", started=view.get("started") or none),
        _t("status_phase", phase=view.get("phase") or none, seq=view.get("step_seq", 0)),
        _t("status_next", next=view.get("next") or none),
        _t("status_rework", rework=view.get("rework_count", 0), max_rework=max_rework()),
        _t("status_dod", dod=view.get("dod_retries") or "{}"),
    ]
    if plan_counts:
        lines.append(_t("status_plan", **plan_counts))
    if test_counts:
        lines.append(_t("status_tests", **test_counts))

    pending_gate = view.get("pending_gate")
    decisions = view.get("hitl_decisions") or []
    if pending_gate:
        hitl = _t("status_hitl_waiting", gate=pending_gate)
    elif decisions:
        hitl = _t("status_hitl_decisions", n=len(decisions))
    else:
        hitl = none
    lines.append(_t("status_hitl", hitl=hitl))

    if view.get("final_status"):
        lines.append(_t("status_final", status=view["final_status"]))
    if view.get("publish_status"):
        lines.append(_t("status_publish", status=view["publish_status"]))
    prs = view.get("pr_urls") or {}
    if prs:
        lines.append(_t("status_prs", prs=", ".join(f"{k}: {v}" for k, v in prs.items())))
    lines.append(_t("status_artifacts", artifacts=", ".join(artifacts) if artifacts else none))
    lines += ["", _t("status_updated", ts=ts), ""]
    return "\n".join(lines)


def render_hitl_decisions(decisions: list[dict]) -> str:
    """hitl-decisions.md - всички човешки решения на едно място."""
    lines = [_t("hitl_title"), "", _t("hitl_header"), _t("hitl_sep")]
    for d in decisions:
        lines.append(
            _t(
                "hitl_row",
                ts=d.get("ts", ""),
                gate=d.get("gate", ""),
                action=d.get("action", ""),
                by=d.get("by", ""),
                feedback=(d.get("feedback") or "").replace("\n", " ").replace("|", "/"),
            )
        )
    lines.append("")
    return "\n".join(lines)
