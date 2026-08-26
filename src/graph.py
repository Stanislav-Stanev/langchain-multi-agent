"""
Графът на Multi-Bot - шаблон "Supervisor" с детерминистичен SDLC поток,
планове преди работа и Human-in-the-Loop порти.

Топологията (docs/prod-mode-plan.md §3):

  START -> init_run -> supervisor (triage, 1 LLM)
             |- analyst -> dev_plan -> [approve_plan] -> developer -> qa_plan -> qa -+-> [approve_publish] -> finalize -> END
             |- dev_plan (готов spec)      ^ "промени"        ^                      |  APPROVED
             |- qa_plan  (готов код)       +------------------+ NEEDS_WORK ----------+  (<= MAX_REWORK)
             '- finalize (несофтуерна задача)
  DoD ескалация / rework лимит -> [escalation_gate] -> retry (обратно, с указания) | abort -> finalize
  [ ... ] = Human-in-the-Loop порта (interrupt), включва се с HITL_GATES

Ключовият принцип (improvement.md §2.1): LLM решава САМО това, което
кодът не може. Супервайзорът (LLM) прави еднократен triage - откъде да
влезе задачата (или FINISH). dev_plan / qa_plan правят по ЕДНО структурирано
извикване (планът е съдържание - решава го LLM; форматът и прогресът са
код - src/plans.py). Всичко останало е детерминистичен код:

  - analyst -> dev_plan: щом спецификацията покрива Definition of Done;
  - dev_plan -> approve_plan -> developer: планът има стъпки; човек го одобрява;
  - developer -> qa_plan / qa: щом кодът покрива DoD (политика по режим, src/dod.py);
  - qa -> approve_publish / developer: по СТРУКТУРИРАНАТА присъда (QAVerdict);
  - finalize: обобщение, матрица на проследимост, публикуване (prod).

Предпазители (improvement.md §2.4, §6.1):
  - Definition of Done на всяка фаза, проверяван ОТ КОДА: непокрит DoD
    дава на агента ЕДИН повторен опит с конкретната липса, после ескалира
    към човек (escalation_gate: повторен опит с указания или край).
  - MAX_REWORK лимит на цикъла qa -> developer.

Проследяване (src/run_tracker.py, src/step_plans.py): всеки възел е обвит в
_traced - ПРЕДИ изпълнението записва плана на стъпката (steps/NN-<възел>.md),
СЛЕД него - фактите, журнала (run.jsonl), артефактите и STATUS.md.

Ключови понятия от LangGraph:
- State (състояние)  - данните, които "текат" през графа: историята от
  съобщения + типизираните артефакти (spec, code, plan, qa_verdict...).
- Node (възел)       - функция, която получава състоянието и връща
  промени по него.
- Edge (ребро)       - преход между възли. Всеки възел записва в 'next'
  КЪДЕ трябва да продължи изпълнението, а едно общо условно ребро чете
  'next' и маршрутизира.
- interrupt()        - спира графа и чака човек; изисква checkpointer.
"""

import inspect
import time
from pathlib import Path
from typing import Literal

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, MessagesState, StateGraph
from pydantic import BaseModel, Field

from src import plans, step_plans
from src import publish as publish_module
from src.agents import create_analyst, create_developer, create_qa
from src.config import fallback_model_name, get_llm
from src.dod import (  # noqa: F401 (extract_python_code - публичен API)
    DemoDoD,
    extract_python_code,
    make_dod_policy,
)
from src.hitl import enabled_gates, make_gate_node, max_revisions
from src.i18n import get_lang, t
from src.modes import get_mode, max_rework  # noqa: F401 (max_rework - публичен API)
from src.modes import runs_dir as default_runs_dir
from src.plans import ImplementationPlan, TestPlan
from src.run_context import run_ctx_from_state
from src.run_tracker import RunTracker, extract_ticket_key, make_run_id
from src.toolsets import make_mode_context, make_tools

# Имената на работните агенти - изнесени като константа, за да ги
# ползваме и в промпта, и в routing логиката, без разминаване.
WORKERS = ["analyst", "developer", "qa"]

# Всички възли след triage-а - целите на общото условно ребро.
PIPELINE = [
    "analyst",
    "dev_plan",
    "approve_plan",
    "developer",
    "qa_plan",
    "qa",
    "approve_publish",
    "escalation_gate",
    "finalize",
]

# Решението на супервайзора -> реалната входна точка: планът се прави ПРЕДИ
# Developer при всеки вход, а тест-планът - преди QA.
ENTRY_NODE = {"analyst": "analyst", "developer": "dev_plan", "qa": "qa_plan", "FINISH": "finalize"}


def extract_text(content) -> str:
    """
    Извлича само текста от content на съобщение.

    Моделите с "extended thinking" (Opus 5+) връщат content като СПИСЪК
    от блокове (thinking + text). Thinking блоковете не могат да се
    подават обратно в human съобщение (API-то ги позволява само в
    assistant роля), затова взимаме само текстовите блокове.
    """
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return content


# ---------------------------------------------------------------------------
# Структурирани решения - никакво парсване на свободен текст
# ---------------------------------------------------------------------------
# Както решението на супервайзора, така и присъдата на QA са Pydantic
# схеми: моделът е ПРИНУДЕН да върне една от изброените стойности.
# Това е гръбнакът на надеждния routing (improvement.md §2.1).


class SupervisorDecision(BaseModel):
    """Triage решение на супервайзора: откъде влиза задачата (или FINISH)."""

    next: Literal["analyst", "developer", "qa", "FINISH"] = Field(
        description="Агентът, от който да започне работата, или FINISH ако задачата не е софтуерна."
    )
    reason: str = Field(
        description="Кратко обяснение (1 изречение) защо е избран този вход."
    )


class QAVerdict(BaseModel):
    """Структурираната присъда на QA - routing-ът чете НЕЯ, не текста."""

    status: Literal["APPROVED", "NEEDS_WORK"] = Field(
        description="APPROVED ако кодът покрива всички критерии, иначе NEEDS_WORK."
    )
    issues: list[str] = Field(
        default_factory=list,
        description="Конкретните забележки (празен списък при APPROVED).",
    )


# Промптът на супервайзора - на двата езика (избира се с APP_LANG при
# сглобяването на графа; езикът на промпта определя и езика на 'reason').
SUPERVISOR_PROMPTS = {
    "bg": """Ти си Team Lead (супервайзор) на софтуерен екип от агенти.

Твоят екип (типичен SDLC процес):
- analyst:   анализира изисквания и тикети, пише спецификация.
- developer: пише Python код по спецификация.
- qa:        проверява код спрямо изисквания.

Твоята задача е ЕДНОКРАТЕН triage: реши ОТКЪДЕ да влезе задачата.
След твоето решение потокът е автоматичен: analyst -> developer -> qa.

Правила:
- Нова задача/тикет без готова спецификация -> analyst (стандартният случай).
- Задачата съдържа готова спецификация, но не и код -> developer.
- Задачата съдържа готов код, който само трябва да се провери -> qa.
- Задачата изобщо не е софтуерна -> FINISH.

Отговори само със структурираното решение.""",
    "en": """You are the Team Lead (supervisor) of a team of software agents.

Your team (a typical SDLC process):
- analyst:   analyzes requirements and tickets, writes a specification.
- developer: writes Python code from a specification.
- qa:        verifies code against requirements.

Your job is a ONE-TIME triage: decide WHERE the task enters.
After your decision the flow is automatic: analyst -> developer -> qa.

Rules:
- A new task/ticket without a spec -> analyst (the standard case).
- The task contains a ready spec but no code -> developer.
- The task contains ready code that only needs review -> qa.
- The task is not a software task at all -> FINISH.

Answer only with the structured decision.""",
}

# Промпт за извличането на структурираната присъда от QA доклада.
# Отделно (евтино) LLM извикване след ReAct цикъла на QA агента:
# доклад в свободен текст -> QAVerdict по схема.
QA_VERDICT_PROMPTS = {
    "bg": (
        "По-долу е доклад от QA преглед на код. Извлечи присъдата "
        "СТРИКТНО по схемата: status е APPROVED само ако докладът ясно "
        "одобрява кода; при каквито и да е забележки - NEEDS_WORK, а "
        "issues изброява конкретните проблеми.\n\nДоклад:\n{report}"
    ),
    "en": (
        "Below is a QA code-review report. Extract the verdict STRICTLY "
        "per the schema: status is APPROVED only if the report clearly "
        "approves the code; with any issues present - NEEDS_WORK, and "
        "issues lists the concrete problems.\n\nReport:\n{report}"
    ),
}


# ---------------------------------------------------------------------------
# Разширено състояние: MessagesState + типизираните артефакти на процеса
# ---------------------------------------------------------------------------


class TeamState(MessagesState):
    """Състоянието на графа: историята + артефактите на всяка SDLC фаза.

    Артефактите (spec, plan, code, test_plan, qa_verdict) са "официалните"
    резултати на фазите - routing-ът и Definition of Done проверките четат
    ТЯХ, а не свободния текст в историята (improvement.md §2.1).
    Данните за run-а (run_id, run_dir, mode) също живеят тук, а не в
    closures: графът е кеширан (Streamlit) и се пази в checkpointer.
    """

    next: str            # къде продължава изпълнението (чете се от routing)
    reason: str          # защо - за визуализациите (main.py / app.py)
    spec: str            # артефакт на Analyst
    code: str            # артефакт на Developer (demo: Python код; prod: unified diff)
    qa_verdict: dict     # артефакт на QA (QAVerdict.model_dump())
    rework_count: int    # колко пъти QA е връщал задачата (лимит: max_rework)
    dod_retries: dict    # брой повторни опити по агент при непокрит DoD
    final_status: str    # APPROVED | ESCALATED | NO_ACTION | ABORTED (за отчета)

    run_id: str          # името на run директорията (дата_час_ключ)
    run_dir: str         # пълният път до runs/<run_id>/
    mode: str            # demo | prod
    ticket_key: str      # ключът на тикета от задачата (DEV-101), ако има
    repos: list          # избраните репозитории (prod)
    step_seq: int        # пореден номер на стъпката (дедупликация при resume)
    plan: dict           # планът за имплементация (src/plans.py документ)
    test_plan: dict      # тест-планът
    plan_revisions: int  # колко пъти човек е върнал плана за ревизия
    retry_target: str    # към кой възел връща escalation_gate при retry
    hitl_decisions: list # човешките решения на портите
    pr_urls: dict        # repo -> URL на draft PR (prod)
    publish_status: str  # PUBLISHED | PUBLISH_FAILED | SKIPPED_BY_HUMAN (prod)
    publish_errors: list


def route_next(state: TeamState) -> str:
    """
    Общата routing функция: всеки възел е записал в 'next' къде трябва
    да продължи изпълнението; 'FINISH' се превежда до END.
    """
    if state["next"] == "FINISH":
        return END
    return state["next"]


# ---------------------------------------------------------------------------
# Помощници за възлите
# ---------------------------------------------------------------------------


def _run_agent(agent, state: TeamState, name: str) -> tuple[str, HumanMessage]:
    """
    Пуска ReAct цикъла на агент върху историята и връща (текст, съобщение).

    Резултатът се "подписва" с името на агента (name=...) и се добавя
    в общата история като HumanMessage - виж extract_text защо текстът
    се филтрира от thinking блокове. context= носи RunCtx до инструментите
    за плановете (update_plan_step / update_test_case).
    """
    result = agent.invoke({"messages": state["messages"]}, context=run_ctx_from_state(state))
    final_answer = extract_text(result["messages"][-1].content)
    return final_answer, HumanMessage(content=final_answer, name=name)


def _dod_failure(state: TeamState, name: str, problem: str) -> dict:
    """
    Обработва непокрит Definition of Done (improvement.md §6.1).

    Първият пропуск дава на агента ЕДИН повторен опит: в историята се
    добавя конкретно указание какво липсва и изпълнението се връща към
    същия възел. Втори пропуск ескалира към човек (escalation_gate) -
    агент, който два пъти не покрива собствения си DoD, няма да го покрие
    и на третия, а токъните струват пари.
    """
    retries = dict(state.get("dod_retries") or {})
    attempts = retries.get(name, 0)

    if attempts < 1:
        retries[name] = attempts + 1
        return {
            "messages": [HumanMessage(content=t("dod_fix_request", problem=problem))],
            "dod_retries": retries,
            "next": name,  # повторен опит: обратно към същия агент
            "reason": t("route_dod_retry", agent=name, problem=problem),
        }

    return {
        "next": "escalation_gate",
        "retry_target": name,
        "final_status": "ESCALATED",
        "reason": t("route_dod_failed", agent=name),
    }


def _attempt(state: TeamState, name: str) -> int:
    """Пореден опит на възела (1 + DoD повторните опити)."""
    return (state.get("dod_retries") or {}).get(name, 0) + 1


def _criteria_text(plan_doc: dict | None) -> str:
    criteria = (plan_doc or {}).get("acceptance_criteria") or []
    if not criteria:
        return "-"
    return "\n".join(f"- {c['id']}: {c['text']}" for c in criteria)


# ---------------------------------------------------------------------------
# Възли (nodes) на графа
# ---------------------------------------------------------------------------
# Агентите се създават ВЪТРЕ в build_graph() (не на ниво модул), за да
# може всяко извикване на build_graph() да вземе АКТУАЛНИЯ LLM_PROVIDER
# от средата - така UI-ят (app.py) превключва anthropic/ollama без
# рестарт на процеса. Фабриките по-долу са чисти функции - обвивката
# _traced (проследяване на диска) се слага само в build_graph().


def make_init_run_node(mode: str, runs_root: Path, prepare_workspaces=None, repos: list | None = None):
    """
    init_run (чист код): run_id, run директория, ключ на тикета, избраните
    репозитории; в prod - подготовка на git workspace-ите. При resume (run_id
    вече е в state) само подготовката се повтаря.
    """

    def init_run(state: TeamState, config: RunnableConfig) -> dict:
        if state.get("run_id"):
            # Resume на същата нишка (или повторен опит след грешка в workspace):
            # директорията вече съществува - само подготовката на workspace-а се повтаря.
            run_id, ticket_key = state["run_id"], state.get("ticket_key", "")
            update = {"next": "supervisor", "reason": t("route_run_resumed", run_id=run_id)}
        else:
            task = extract_text(state["messages"][-1].content) if state.get("messages") else ""
            ticket_key = extract_ticket_key(task)
            thread_id = str((config.get("configurable") or {}).get("thread_id") or "")
            run_id = make_run_id(ticket_key, task, thread_id=thread_id)
            tracker = RunTracker.start(
                runs_root, run_id, mode=mode, ticket_key=ticket_key, task=task, lang=get_lang()
            )
            update = {
                "run_id": run_id,
                "run_dir": str(tracker.run_dir),
                "mode": mode,
                "ticket_key": ticket_key,
                "repos": list(repos or []),
                "next": "supervisor",
                "reason": t("route_run_started", run_id=run_id),
            }

        if prepare_workspaces is not None:
            try:
                prepare_workspaces(ticket_key, run_id)
            except Exception as exc:  # workspace грешката е ескалация, не crash
                update.update(
                    next="escalation_gate",
                    retry_target="init_run",
                    final_status="ESCALATED",
                    reason=t("route_workspace_failed", error=exc),
                )
        return update

    return init_run


def make_supervisor_node(supervisor_llm):
    """
    Фабрика за възела-надзорник (еднократният triage).

    Възелът чете задачата и решава откъде да влезе тя в конвейера.
    Решението се превежда през ENTRY_NODE (developer -> dev_plan, qa ->
    qa_plan): планът винаги предхожда работата. 'reason' се пази, за да
    може UI-ят да визуализира ЗАЩО е взето решението.
    """

    def supervisor_node(state: TeamState) -> dict:
        decision = supervisor_llm.invoke(
            [
                {"role": "system", "content": SUPERVISOR_PROMPTS[get_lang()]},
                *state["messages"],
            ]
        )

        update = {"next": ENTRY_NODE[decision.next], "reason": decision.reason}
        if decision.next == "FINISH":
            # Несофтуерна задача: приключваме без работа по нея.
            update["final_status"] = "NO_ACTION"
        return update

    return supervisor_node


def make_analyst_node(agent, dod=None):
    """Analyst: спецификация + DoD проверка, после детерминистично -> dev_plan."""
    dod = dod or DemoDoD()

    def analyst_node(state: TeamState) -> dict:
        spec, message = _run_agent(agent, state, "analyst")

        # Definition of Done на фазата: спецификацията не е празна.
        problem = dod.spec_problem(spec)
        if problem:
            failure = _dod_failure(state, "analyst", problem)
            failure.setdefault("messages", []).insert(0, message)
            return failure

        return {
            "messages": [message],
            "spec": spec,
            "next": "dev_plan",
            "reason": t("route_spec_ready"),
        }

    return analyst_node


def make_dev_plan_node(plan_llm, dod=None):
    """
    dev_plan: ЕДНО структурирано извикване -> ImplementationPlan -> markdown с
    чекбоксове (src/plans.py). Рендираният план влиза в историята като
    съобщение с name="dev_plan", за да вижда Developer ID-тата на стъпките.
    """
    dod = dod or DemoDoD()

    def dev_plan_node(state: TeamState) -> dict:
        plan = plan_llm.invoke(
            [{"role": "system", "content": plans.DEV_PLAN_PROMPTS[get_lang()]}, *state["messages"]]
        )
        doc = plans.new_plan_doc(plan)

        problem = dod.plan_problem(doc)
        if problem:
            return _dod_failure(state, "dev_plan", problem)

        tracker = RunTracker.from_state(state)
        plans.save_plan(tracker, doc, state.get("ticket_key", ""))
        rendered = plans.render_implementation_plan(doc, state.get("ticket_key", ""))
        return {
            "messages": [HumanMessage(content=rendered, name="dev_plan")],
            "plan": doc,
            "next": "approve_plan",
            "reason": t("route_plan_ready", steps=len(doc["steps"])),
        }

    return dev_plan_node


def make_developer_node(agent, dod=None):
    """Developer: код + DoD по политиката на режима (src/dod.py) -> qa_plan / qa."""
    dod = dod or DemoDoD()

    def developer_node(state: TeamState) -> dict:
        answer, message = _run_agent(agent, state, "developer")

        # Планът може да е обновен от инструментите на агента - четем го от диска.
        tracker = RunTracker.from_state(state)
        plan_doc = plans.load_plan(tracker) or state.get("plan")

        # Definition of Done на фазата, проверяван ОТ КОДА (не от LLM).
        problem, code = dod.developer_result(answer, plan_doc)
        if problem:
            failure = _dod_failure(state, "developer", problem)
            failure.setdefault("messages", []).insert(0, message)
            return failure

        update = {
            "messages": [message],
            "code": code,
            # Тест-планът се прави веднъж; при rework отиваме направо към QA.
            "next": "qa" if state.get("test_plan") else "qa_plan",
            "reason": dod.ready_reason(code),
        }
        if plan_doc:
            plan_doc = plans.record_phase_end(
                plan_doc, node="developer", attempt=_attempt(state, "developer"),
                rework=state.get("rework_count", 0),
            )
            plans.save_plan(tracker, plan_doc, state.get("ticket_key", ""))
            update["plan"] = plan_doc
        return update

    return developer_node


def make_qa_plan_node(test_plan_llm, dod=None):
    """qa_plan: ЕДНО структурирано извикване -> TestPlan -> qa-plan.md."""
    dod = dod or DemoDoD()

    def qa_plan_node(state: TeamState) -> dict:
        prompt = plans.QA_PLAN_PROMPTS[get_lang()].format(criteria=_criteria_text(state.get("plan")))
        test_plan = test_plan_llm.invoke([{"role": "system", "content": prompt}, *state["messages"]])
        doc = plans.validate_case_refs(plans.new_test_plan_doc(test_plan), state.get("plan"))

        problem = dod.test_plan_problem(doc)
        if problem:
            return _dod_failure(state, "qa_plan", problem)

        tracker = RunTracker.from_state(state)
        plans.save_test_plan(tracker, doc, state.get("ticket_key", ""))
        rendered = plans.render_test_plan(doc, state.get("ticket_key", ""))
        return {
            "messages": [HumanMessage(content=rendered, name="qa_plan")],
            "test_plan": doc,
            "next": "qa",
            "reason": t("route_test_plan_ready", cases=len(doc["cases"])),
        }

    return qa_plan_node


def make_qa_node(agent, verdict_llm, on_approved: str = "approve_publish"):
    """
    QA: преглед + СТРУКТУРИРАНА присъда + детерминистичен routing.

    Двустъпков процес:
      1. QA агентът (ReAct) прави прегледа и пише доклад в свободен текст.
      2. Отделно structured-output извикване извлича QAVerdict от доклада.
    Routing-ът след това е чист код: APPROVED -> on_approved (портата преди
    публикуване, после finalize); NEEDS_WORK -> developer, но само до
    max_rework() пъти - после ескалация към човек (improvement.md §2.4).
    """

    def qa_node(state: TeamState) -> dict:
        report, message = _run_agent(agent, state, "qa")

        verdict = verdict_llm.invoke(
            QA_VERDICT_PROMPTS[get_lang()].format(report=report)
        )

        update = {"messages": [message], "qa_verdict": verdict.model_dump()}

        tracker = RunTracker.from_state(state)
        ticket = state.get("ticket_key", "")
        test_doc = plans.load_test_plan(tracker) or state.get("test_plan")
        plan_doc = plans.load_plan(tracker) or state.get("plan")
        if test_doc:
            test_doc = plans.record_phase_end(
                test_doc, node="qa", attempt=_attempt(state, "qa"),
                rework=state.get("rework_count", 0), kind="test",
            )

        if verdict.status == "APPROVED":
            update.update(next=on_approved, final_status="APPROVED", reason=t("route_qa_approved"))
        else:
            # NEEDS_WORK: връщане към Developer, докато лимитът позволява.
            rework = state.get("rework_count", 0) + 1
            limit = max_rework()
            update["rework_count"] = rework

            if rework > limit:
                update.update(
                    next="escalation_gate",
                    retry_target="developer",
                    final_status="ESCALATED",
                    reason=t("route_rework_limit", max=limit),
                )
            else:
                update.update(next="developer", reason=t("route_qa_needs_work", n=rework, max=limit))
                # Планът получава секция "Rework N"; тест-планът се нулира за нов run.
                if plan_doc:
                    plan_doc = plans.add_rework(plan_doc, rework, verdict.issues)
                if test_doc:
                    test_doc = plans.reset_for_rerun(test_doc, rework + 1)

        if plan_doc:
            plans.save_plan(tracker, plan_doc, ticket)
            update["plan"] = plan_doc
        if test_doc:
            plans.save_test_plan(tracker, test_doc, ticket)
            update["test_plan"] = test_doc
        return update

    return qa_node


# ---------------------------------------------------------------------------
# Human-in-the-Loop порти (src/hitl.py) - какво вижда човекът и какво следва
# ---------------------------------------------------------------------------


def _plan_gate_payload(state: TeamState) -> dict:
    tracker = RunTracker.from_state(state)
    return {
        "title": "implementation-plan.md",
        "artifact": str(Path(state.get("run_dir", "")) / plans.PLAN_MD) if state.get("run_dir") else "",
        "preview": tracker.read_text(plans.PLAN_MD) or plans.render_implementation_plan(
            state.get("plan") or {}, state.get("ticket_key", "")
        ),
        "spec": state.get("spec", ""),
        "revisions": state.get("plan_revisions", 0),
        "max_revisions": max_revisions(),
    }


def make_approve_plan_node(enabled: bool):
    """Порта 'plan': човек одобрява плана, преди Developer да пише код."""

    def on_approve(state, decision):
        return {"next": "developer"}

    def on_revise(state, decision):
        return {
            "next": "dev_plan",
            "plan_revisions": state.get("plan_revisions", 0) + 1,
            "messages": [HumanMessage(content=t("hitl_revise_request", gate="plan", feedback=decision.feedback))],
        }

    def on_abort(state, decision):
        return {"next": "finalize", "final_status": "ABORTED"}

    return make_gate_node(
        "plan",
        enabled=enabled,
        build_payload=_plan_gate_payload,
        on_approve=on_approve,
        on_revise=on_revise,
        on_abort=on_abort,
        can_revise=lambda state: state.get("plan_revisions", 0) < max_revisions(),
    )


def _publish_gate_payload(state: TeamState) -> dict:
    """Какво вижда човекът преди publish: diff-ът, присъдата на QA, тест-планът и PR тялото."""
    tracker = RunTracker.from_state(state)
    diff = state.get("code", "")
    return {
        "title": "code.diff",
        "artifact": str(Path(state.get("run_dir", "")) / "code.diff") if state.get("run_dir") else "",
        "preview": f"```diff\n{diff}\n```" if diff else "",
        "qa_verdict": state.get("qa_verdict", {}),
        "test_plan": tracker.read_text(plans.TEST_PLAN_MD),
        "pr_body": publish_module.build_pr_body(state, tracker),
        "repos": state.get("repos", []),
    }


def make_approve_publish_node(enabled: bool):
    """Порта 'publish': човек одобрява commit / push / draft PR (само prod)."""

    def on_approve(state, decision):
        return {"next": "finalize"}

    def on_revise(state, decision):
        return {
            "next": "developer",
            "rework_count": state.get("rework_count", 0) + 1,
            "messages": [HumanMessage(content=t("hitl_revise_request", gate="publish", feedback=decision.feedback))],
        }

    def on_abort(state, decision):
        return {"next": "finalize", "publish_status": "SKIPPED_BY_HUMAN"}

    return make_gate_node(
        "publish",
        enabled=enabled,
        build_payload=_publish_gate_payload,
        on_approve=on_approve,
        on_revise=on_revise,
        on_abort=on_abort,
    )


def _escalation_payload(state: TeamState) -> dict:
    return {
        "title": "escalation",
        "artifact": str(Path(state.get("run_dir", "")) / "STATUS.md") if state.get("run_dir") else "",
        "preview": state.get("reason", ""),
        "retry_target": state.get("retry_target", ""),
        "final_status": state.get("final_status", ""),
        "rework_count": state.get("rework_count", 0),
        "dod_retries": state.get("dod_retries", {}),
    }


def make_escalation_gate_node(enabled: bool):
    """
    Порта 'escalation': системата не може сама - човек решава.
    approve / revise = повторен опит (с указания) към провалилия се възел с
    нулирани броячи; abort = край (ESCALATED). Изключена порта -> finalize.
    """

    def retry(state, decision):
        target = state.get("retry_target") or "developer"
        retries = dict(state.get("dod_retries") or {})
        retries[target] = 0
        update = {
            "next": target,
            "retry_target": "",
            "final_status": "",
            "dod_retries": retries,
            "rework_count": 0 if target == "developer" else state.get("rework_count", 0),
            "reason": t("route_gate_retry", node=target),
        }
        if decision.feedback:
            update["messages"] = [HumanMessage(content=t("hitl_retry_request", feedback=decision.feedback))]
        return update

    def on_abort(state, decision):
        return {"next": "finalize", "final_status": state.get("final_status") or "ESCALATED"}

    return make_gate_node(
        "escalation",
        enabled=enabled,
        build_payload=_escalation_payload,
        on_approve=retry,
        on_revise=retry,
        on_abort=on_abort,
        on_disabled=on_abort,
    )


def make_finalize_node(mode: str, publisher=None):
    """
    finalize (чист код): матрица на проследимост, summary.json; в prod при
    APPROVED - публикуване (publisher, PR 3). Единственият възел с next=FINISH.
    """

    def finalize(state: TeamState) -> dict:
        tracker = RunTracker.from_state(state)
        status = state.get("final_status") or ""
        update: dict = {"next": "FINISH", "reason": t("route_run_finished", status=status)}

        tracker.write_text(
            plans.TRACEABILITY_MD,
            plans.render_traceability(state.get("plan"), state.get("test_plan")),
        )

        if mode == "prod" and status == "APPROVED" and publisher is not None:
            if state.get("publish_status") == "SKIPPED_BY_HUMAN":
                update["publish_status"] = "SKIPPED_BY_HUMAN"
            else:
                update.update(publisher.publish(state, tracker))
                update["reason"] = update.get("reason") or t("route_run_finished", status=status)

        tracker.write_summary(
            {
                "run_id": state.get("run_id", ""),
                "mode": mode,
                "ticket_key": state.get("ticket_key", ""),
                "final_status": status,
                "publish_status": update.get("publish_status") or state.get("publish_status") or "",
                "pr_urls": update.get("pr_urls") or state.get("pr_urls") or {},
                "publish_errors": update.get("publish_errors") or state.get("publish_errors") or [],
                "rework_count": state.get("rework_count", 0),
                "dod_retries": state.get("dod_retries") or {},
                "plan_revisions": state.get("plan_revisions", 0),
                "hitl_decisions": state.get("hitl_decisions") or [],
                "plan_counts": plans.counts(state.get("plan") or {}) if state.get("plan") else {},
                "test_counts": plans.counts(state.get("test_plan") or {}, "test") if state.get("test_plan") else {},
                "steps": state.get("step_seq", 0) + 1,
            }
        )
        return update

    return finalize


# ---------------------------------------------------------------------------
# Проследяване: „план преди / изпълнение след" за всяка стъпка (§3b)
# ---------------------------------------------------------------------------


def _traced(name: str, fn):
    """
    Обвива възел с проследяването на диска (src/run_tracker.py):
      ПРЕДИ  - steps/NN-<възел>.md със секция „План" (шаблон по възел);
      СЛЕД   - „Изпълнение", run.jsonl събитие, артефакти, STATUS.md.
    interrupt() (HITL) минава през обвивката: стъпката остава WAITING_HUMAN,
    а при resume възелът се изпълнява отново със същия пореден номер.
    """
    takes_config = len(inspect.signature(fn).parameters) >= 2

    def node(state: TeamState, config: RunnableConfig) -> dict:
        seq = state.get("step_seq", 0) + 1
        pre_tracker = RunTracker.from_state(state)
        plan_md = step_plans.plan_section(name, state)
        pre_tracker.begin_step(seq, name, plan_md)
        started = time.perf_counter()

        try:
            update = fn(state, config) if takes_config else fn(state)
        except GraphInterrupt:
            gate = name.replace("approve_", "").replace("_gate", "")
            pre_tracker.write_status({**state, "phase": name, "pending_gate": gate, "step_seq": seq})
            raise
        except Exception as exc:
            pre_tracker.end_step(
                seq, name, step_plans.execution_section(name, state, {}, error=str(exc)),
                status="FAILED", next_node=None, duration_s=time.perf_counter() - started,
            )
            pre_tracker.event(seq=seq, node=name, error=str(exc))
            raise

        duration = time.perf_counter() - started
        update["step_seq"] = seq
        merged = {**state, **update}
        tracker = RunTracker.from_state(merged)
        if pre_tracker.run_dir is None and tracker.run_dir is not None:
            # init_run току-що създаде директорията - записваме и плана на стъпката
            tracker.begin_step(seq, name, plan_md)

        mode = merged.get("mode") or "demo"
        tracker.persist_artifacts(update, mode=mode)
        tracker.end_step(
            seq, name, step_plans.execution_section(name, state, update),
            status="DONE", next_node=update.get("next"), duration_s=duration,
        )
        decisions = update.get("hitl_decisions")
        tracker.event(
            seq=seq,
            node=name,
            next=update.get("next"),
            reason=update.get("reason"),
            duration_s=round(duration, 3),
            rework_count=merged.get("rework_count", 0),
            dod_retries=merged.get("dod_retries") or {},
            final_status=update.get("final_status"),
            hitl=decisions[-1] if decisions else None,
        )
        plan_doc, test_doc = merged.get("plan"), merged.get("test_plan")
        tracker.write_status(
            {**merged, "phase": name},
            plan_counts=plans.counts(plan_doc) if plan_doc else None,
            test_counts=plans.counts(test_doc, "test") if test_doc else None,
        )
        return update

    return node


# ---------------------------------------------------------------------------
# Сглобяване на графа
# ---------------------------------------------------------------------------


def make_structured_llm(role: str, schema):
    """
    Структуриран LLM за критичните решения, с опционална fallback верига
    (improvement.md §2.7).

    Без MODEL_FALLBACK: просто get_llm(role) + схемата. С MODEL_FALLBACK:
    при неуспех на основния модел (изчерпани retries, timeout, 5xx)
    същото решение се опитва ВТОРИ път с резервния модел - triage и QA
    присъдата са твърде важни, за да умре целият run от една грешка на
    доставчика. (Работните агенти разчитат на client-level retries -
    fallback на ниво ReAct агент би сменил модела по средата на цикъла.)
    """
    primary = get_llm(role).with_structured_output(schema)

    fallback_model = fallback_model_name()
    if not fallback_model:
        return primary

    fallback = get_llm(role, model_override=fallback_model).with_structured_output(schema)
    return primary.with_fallbacks([fallback])


def build_graph(checkpointer=None, *, mode: str | None = None, repos=None, runs_dir=None, hitl_gates=None):
    """
    Сглобява и компилира мултиагентния граф.

    Всичко LLM-зависимо (агенти, супервайзор, планиращи LLM-и, екстрактор
    на присъдата) и всичко външно (Jira, git - prod) се създава ТУК, при
    всяко извикване - така графът отразява текущия LLM_PROVIDER, APP_MODE,
    APP_LANG и HITL_GATES от средата.

    checkpointer (по избор, improvement.md §2.2): SqliteSaver прави всяка
    стъпка устойчива (resume). Human-in-the-Loop портите изискват
    checkpointer - ако има включени порти и не е подаден, се ползва
    InMemorySaver (валиден в рамките на процеса: Streamlit сесия, CLI).

    mode / repos / runs_dir / hitl_gates: изрични стойности за UI-я и
    тестовете; None = от средата (get_mode, PROD_REPOS, RUNS_DIR, HITL_GATES).
    """
    mode = mode or get_mode()
    runs_root = Path(runs_dir) if runs_dir else default_runs_dir()
    gates = tuple(hitl_gates) if hitl_gates is not None else enabled_gates(mode)

    ctx = make_mode_context(mode, repos)
    toolset = make_tools(mode, ctx)
    workspace_view = getattr(ctx, "workspace_view", None)
    # Преходен случай (до PR 3): prod без git workspace -> Developer предава
    # кода както в demo (```python блок), затова и DoD е demo политиката.
    dod = make_dod_policy(mode if workspace_view is not None else "demo", workspace_view)

    # LLM клиенти, "закотвени" към Pydantic схемите: with_structured_output
    # гарантира, че отговорът е точно SupervisorDecision / QAVerdict / план.
    supervisor_llm = make_structured_llm("supervisor", SupervisorDecision)
    verdict_llm = make_structured_llm("qa", QAVerdict)
    plan_llm = make_structured_llm("developer", ImplementationPlan)
    test_plan_llm = make_structured_llm("qa", TestPlan)

    def worker(role):
        factory = {"analyst": create_analyst, "developer": create_developer, "qa": create_qa}[role]
        return factory(toolset.tools[role], toolset.prompt_addendum[role])

    builder = StateGraph(TeamState)

    # 1. Регистрираме възлите (име -> функция), всеки обвит с проследяването
    nodes = {
        "init_run": make_init_run_node(
            mode, runs_root, getattr(ctx, "prepare_workspaces", None), getattr(ctx, "repos", None)
        ),
        "supervisor": make_supervisor_node(supervisor_llm),
        "analyst": make_analyst_node(worker("analyst"), dod),
        "dev_plan": make_dev_plan_node(plan_llm, dod),
        "approve_plan": make_approve_plan_node("plan" in gates),
        "developer": make_developer_node(worker("developer"), dod),
        "qa_plan": make_qa_plan_node(test_plan_llm, dod),
        "qa": make_qa_node(worker("qa"), verdict_llm),
        "approve_publish": make_approve_publish_node("publish" in gates and mode == "prod"),
        "escalation_gate": make_escalation_gate_node("escalation" in gates),
        "finalize": make_finalize_node(mode, getattr(ctx, "publisher", None)),
    }
    for name, fn in nodes.items():
        builder.add_node(name, _traced(name, fn))

    # 2. Входна точка: регистрация на run-а, после еднократният triage
    builder.add_edge(START, "init_run")

    # 3. Едно общо условно ребро: ВСЕКИ възел записва в 'next' къде
    #    продължава изпълнението (triage, детерминистичните преходи,
    #    DoD повторните опити, rework цикълът, портите) - route_next само го чете.
    for node in ("init_run", "supervisor", *PIPELINE):
        builder.add_conditional_edges(node, route_next, ["init_run", "supervisor", *PIPELINE, END])

    # interrupt() (HITL) изисква checkpointer - без подаден слагаме InMemorySaver.
    if gates and checkpointer is None:
        checkpointer = InMemorySaver()

    # compile() превръща описанието в изпълним обект с .invoke()/.stream()
    return builder.compile(checkpointer=checkpointer)
