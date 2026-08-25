"""
Графът на Multi-Bot - шаблон "Supervisor" с детерминистичен SDLC поток.

Как работи архитектурата (след продукционизирането, improvement.md §2.1):

                        +--------------+
        потребител ---> |  SUPERVISOR  |  <- LLM решава САМО входа (triage)
                        +--------------+
                          |         \\
                          v          v
                    +---------+   FINISH (несофтуерна задача)
                    | ANALYST |
                    +---------+
                          | (детерминистично, след DoD проверка)
                          v
                    +-----------+
                    | DEVELOPER | <---+
                    +-----------+     | NEEDS_WORK (до MAX_REWORK пъти)
                          |           |
                          v           |
                       +------+       |
                       |  QA  | ------+
                       +------+
                          | APPROVED / ESCALATED
                          v
                         END

Ключовият принцип (improvement.md §2.1): LLM решава САМО това, което
кодът не може. Супервайзорът (LLM) прави еднократен triage - откъде да
влезе задачата (или FINISH, ако изобщо не е софтуерна). Всичко след
това е детерминистичен код:

  - analyst -> developer: щом спецификацията покрива Definition of Done;
  - developer -> qa: щом кодът е извлечен и синтактично валиден;
  - qa -> developer/END: по СТРУКТУРИРАНАТА присъда (QAVerdict), не по
    парсване на свободен текст - "APPROVED" в перифраза не може да
    подведе маршрутизацията.

Предпазители (improvement.md §2.4, §6.1):
  - Definition of Done на всяка фаза, проверяван ОТ КОДА: непокрит DoD
    дава на агента ЕДИН повторен опит с конкретната липса, после ескалира.
  - MAX_REWORK лимит на цикъла qa -> developer: след изчерпването му
    задачата ескалира към човек (final_status="ESCALATED") вместо да
    гори токъни до recursion_limit.

Ключови понятия от LangGraph:
- State (състояние)  - данните, които "текат" през графа: историята от
  съобщения + типизираните артефакти (spec, code, qa_verdict...).
- Node (възел)       - функция, която получава състоянието и връща
  промени по него. Всеки агент е един възел.
- Edge (ребро)       - преход между възли. Всеки възел записва в 'next'
  КЪДЕ трябва да продължи изпълнението, а едно общо условно ребро чете
  'next' и маршрутизира.
"""

import ast
import os
import re
from typing import Literal

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from pydantic import BaseModel, Field

from src.agents import create_analyst, create_developer, create_qa
from src.config import fallback_model_name, get_llm
from src.i18n import get_lang, t

# Имената на работните агенти - изнесени като константа, за да ги
# ползваме и в промпта, и в routing логиката, без разминаване.
WORKERS = ["analyst", "developer", "qa"]

# Колко пъти QA може да върне задачата на Developer, преди системата да
# ескалира към човек. Чете се от средата при всяко решение (не при
# import), за да е конфигурируемо без рестарт - като LLM_PROVIDER.
DEFAULT_MAX_REWORK = 3


def max_rework() -> int:
    """Лимитът на поправките (MAX_REWORK от средата, по подразбиране 3)."""
    try:
        return int(os.getenv("MAX_REWORK", DEFAULT_MAX_REWORK))
    except ValueError:
        return DEFAULT_MAX_REWORK


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


# Регулярен израз за ```python ... ``` блок - кодът артефакт на Developer
# се извлича оттук, а не от целия свободен текст на отговора.
_PYTHON_BLOCK_RE = re.compile(r"```python\s*\n(.*?)```", re.DOTALL)


def extract_python_code(text: str) -> str:
    """
    Извлича Python кода от markdown отговора на Developer агента.

    Взимат се ВСИЧКИ ```python блокове (агентът може да върне функцията
    и тестовете ѝ отделно). Ако няма нито един блок - връща празен низ,
    което проваля Definition of Done проверката и връща задачата на
    Developer с конкретно указание.
    """
    blocks = _PYTHON_BLOCK_RE.findall(text or "")
    return "\n\n".join(block.strip() for block in blocks).strip()


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

    Артефактите (spec, code, qa_verdict) са "официалните" резултати на
    фазите - routing-ът и Definition of Done проверките четат ТЯХ, а не
    свободния текст в историята (improvement.md §2.1).
    """

    next: str            # къде продължава изпълнението (чете се от routing)
    reason: str          # защо - за визуализациите (main.py / app.py)
    spec: str            # артефакт на Analyst
    code: str            # артефакт на Developer (извлеченият Python код)
    qa_verdict: dict     # артефакт на QA (QAVerdict.model_dump())
    rework_count: int    # колко пъти QA е връщал задачата (лимит: max_rework)
    dod_retries: dict    # брой повторни опити по агент при непокрит DoD
    final_status: str    # APPROVED | ESCALATED | NO_ACTION (за отчета)


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
    се филтрира от thinking блокове.
    """
    result = agent.invoke({"messages": state["messages"]})
    final_answer = extract_text(result["messages"][-1].content)
    return final_answer, HumanMessage(content=final_answer, name=name)


def _dod_failure(state: TeamState, name: str, problem: str) -> dict:
    """
    Обработва непокрит Definition of Done (improvement.md §6.1).

    Първият пропуск дава на агента ЕДИН повторен опит: в историята се
    добавя конкретно указание какво липсва и изпълнението се връща към
    същия възел. Втори пропуск ескалира към човек - агент, който два
    пъти не покрива собствения си DoD, няма да го покрие и на третия,
    а токъните струват пари.
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
        "next": "FINISH",
        "final_status": "ESCALATED",
        "reason": t("route_dod_failed", agent=name),
    }


# ---------------------------------------------------------------------------
# Възли (nodes) на графа
# ---------------------------------------------------------------------------
# Агентите се създават ВЪТРЕ в build_graph() (не на ниво модул), за да
# може всяко извикване на build_graph() да вземе АКТУАЛНИЯ LLM_PROVIDER
# от средата - така UI-ят (app.py) превключва anthropic/ollama без
# рестарт на процеса.


def make_supervisor_node(supervisor_llm):
    """
    Фабрика за възела-надзорник (еднократният triage).

    Възелът чете задачата и решава откъде да влезе тя в конвейера.
    'reason' се пази, за да може UI-ят да визуализира ЗАЩО е взето
    решението - самата визуализация е в main.py/app.py (separation
    of concerns: графът само произвежда данни).
    """

    def supervisor_node(state: TeamState) -> dict:
        decision = supervisor_llm.invoke(
            [
                {"role": "system", "content": SUPERVISOR_PROMPTS[get_lang()]},
                *state["messages"],
            ]
        )

        update = {"next": decision.next, "reason": decision.reason}
        if decision.next == "FINISH":
            # Несофтуерна задача: приключваме без работа по нея.
            update["final_status"] = "NO_ACTION"
        return update

    return supervisor_node


def make_analyst_node(agent):
    """Analyst: спецификация + DoD проверка, после детерминистично -> developer."""

    def analyst_node(state: TeamState) -> dict:
        spec, message = _run_agent(agent, state, "analyst")

        # Definition of Done на фазата: спецификацията не е празна.
        if not spec.strip():
            failure = _dod_failure(state, "analyst", t("dod_missing_spec"))
            failure.setdefault("messages", []).insert(0, message)
            return failure

        return {
            "messages": [message],
            "spec": spec,
            "next": "developer",
            "reason": t("route_spec_ready"),
        }

    return analyst_node


def make_developer_node(agent):
    """Developer: код + DoD проверка (извлечен блок, валиден синтаксис) -> qa."""

    def developer_node(state: TeamState) -> dict:
        answer, message = _run_agent(agent, state, "developer")

        # Definition of Done на фазата, проверяван ОТ КОДА (не от LLM):
        # 1. отговорът съдържа ```python блок с код;
        code = extract_python_code(answer)
        if not code:
            failure = _dod_failure(state, "developer", t("dod_missing_code"))
            failure.setdefault("messages", []).insert(0, message)
            return failure

        # 2. кодът е синтактично валиден (ast.parse НЕ изпълнява кода).
        try:
            ast.parse(code)
        except SyntaxError as exc:
            failure = _dod_failure(
                state, "developer", t("dod_syntax_error", error=exc.msg)
            )
            failure.setdefault("messages", []).insert(0, message)
            return failure

        return {
            "messages": [message],
            "code": code,
            "next": "qa",
            "reason": t("route_code_ready"),
        }

    return developer_node


def make_qa_node(agent, verdict_llm):
    """
    QA: преглед + СТРУКТУРИРАНА присъда + детерминистичен routing.

    Двустъпков процес:
      1. QA агентът (ReAct) прави прегледа и пише доклад в свободен текст.
      2. Отделно structured-output извикване извлича QAVerdict от доклада.
    Routing-ът след това е чист код: APPROVED -> END; NEEDS_WORK ->
    developer, но само до max_rework() пъти - после ескалация към човек
    (improvement.md §2.4: лимитът е бизнес правило, не recursion_limit).
    """

    def qa_node(state: TeamState) -> dict:
        report, message = _run_agent(agent, state, "qa")

        verdict = verdict_llm.invoke(
            QA_VERDICT_PROMPTS[get_lang()].format(report=report)
        )

        update = {"messages": [message], "qa_verdict": verdict.model_dump()}

        if verdict.status == "APPROVED":
            update.update(
                next="FINISH",
                final_status="APPROVED",
                reason=t("route_qa_approved"),
            )
            return update

        # NEEDS_WORK: връщане към Developer, докато лимитът позволява.
        rework = state.get("rework_count", 0) + 1
        limit = max_rework()
        update["rework_count"] = rework

        if rework > limit:
            update.update(
                next="FINISH",
                final_status="ESCALATED",
                reason=t("route_rework_limit", max=limit),
            )
        else:
            update.update(
                next="developer",
                reason=t("route_qa_needs_work", n=rework, max=limit),
            )
        return update

    return qa_node


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


def build_graph(checkpointer=None):
    """
    Сглобява и компилира мултиагентния граф.

    Всичко LLM-зависимо (агенти, супервайзор, екстрактор на присъдата)
    се създава ТУК, при всяко извикване - така графът отразява текущия
    LLM_PROVIDER (и per-role моделите) от средата.

    checkpointer (по избор, improvement.md §2.2): подаден SqliteSaver /
    PostgresSaver прави всяка стъпка устойчива - run със същия thread_id
    продължава от последния запазен checkpoint (resume). None = както
    досега, всичко в паметта. Създава се от config.get_checkpointer().
    """
    # LLM клиенти, "закотвени" към Pydantic схемите: with_structured_output
    # гарантира, че отговорът е точно SupervisorDecision / QAVerdict.
    supervisor_llm = make_structured_llm("supervisor", SupervisorDecision)
    verdict_llm = make_structured_llm("qa", QAVerdict)

    builder = StateGraph(TeamState)

    # 1. Регистрираме възлите (име -> функция)
    builder.add_node("supervisor", make_supervisor_node(supervisor_llm))
    builder.add_node("analyst", make_analyst_node(create_analyst()))
    builder.add_node("developer", make_developer_node(create_developer()))
    builder.add_node("qa", make_qa_node(create_qa(), verdict_llm))

    # 2. Входна точка: еднократният triage на супервайзора
    builder.add_edge(START, "supervisor")

    # 3. Едно общо условно ребро: ВСЕКИ възел записва в 'next' къде
    #    продължава изпълнението (triage, детерминистичните преходи,
    #    DoD повторните опити, rework цикълът) - route_next само го чете.
    for node in ("supervisor", *WORKERS):
        builder.add_conditional_edges(node, route_next, [*WORKERS, END])

    # compile() превръща описанието в изпълним обект с .invoke()/.stream()
    return builder.compile(checkpointer=checkpointer)
