"""
Графът на мултиагентната система - шаблон "Supervisor" (надзорник).

Как работи архитектурата:

                        +--------------+
        потребител ---> |  SUPERVISOR  | <--- връща се след всеки агент
                        +--------------+
                         /     |      \\
                        v      v       v
                  +--------+ +-----------+ +------+
                  |ANALYST | | DEVELOPER | |  QA  |
                  +--------+ +-----------+ +------+

1. Потребителят задава задача (напр. "Имплементирай тикет DEV-101").
2. SUPERVISOR (самият той LLM) решава кой специалист да работи пръв.
3. Избраният агент върши своята част и резултатът му се добавя
   към общата история на разговора (споделеното "състояние").
4. Управлението се връща на SUPERVISOR, който решава следващата стъпка.
5. Когато прецени, че задачата е готова, SUPERVISOR връща FINISH
   и графът приключва.

Ключови понятия от LangGraph:
- State (състояние)  - данните, които "текат" през графа. Тук: списък
  от съобщения, който всеки възел чете и допълва.
- Node (възел)       - функция, която получава състоянието и връща
  промени по него. Всеки агент е един възел.
- Edge (ребро)       - преход между възли. "Условно ребро" избира
  следващия възел според резултата (тук: решението на супервайзора).
"""

from typing import Literal

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from pydantic import BaseModel, Field

from src.agents import create_analyst, create_developer, create_qa
from src.config import get_llm

# Имената на работните агенти - изнесени като константа, за да ги
# ползваме и в промпта, и в routing логиката, без разминаване.
WORKERS = ["analyst", "developer", "qa"]


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
# Структурирано решение на супервайзора
# ---------------------------------------------------------------------------
# Вместо да парсваме свободен текст ("мисля, че Analyst трябва..."),
# караме модела да върне СТРОГО структуриран отговор по тази Pydantic
# схема. Това е ключова добра практика: routing-ът става надежден,
# защото "next" може да е САМО една от изброените стойности.


class SupervisorDecision(BaseModel):
    """Решение на супервайзора: кой работи следващ или край."""

    next: Literal["analyst", "developer", "qa", "FINISH"] = Field(
        description="Следващият агент, който да поеме работата, или FINISH при готова задача."
    )
    reason: str = Field(
        description="Кратко обяснение (1 изречение) защо е избран този агент."
    )


SUPERVISOR_PROMPT = f"""Ти си Team Lead (супервайзор) на софтуерен екип от агенти.

Твоят екип (типичен SDLC процес):
- analyst:   анализира изисквания и тикети, пише спецификация. Работи ПЪРВИ.
- developer: пише Python код по спецификацията на analyst.
- qa:        проверява кода на developer спрямо изискванията.

Твоята задача: според историята на разговора реши КОЙ агент да работи
следващ. Стандартният поток е analyst -> developer -> qa -> FINISH.

Правила:
- Не пропускай фази: не пускай developer без спецификация от analyst,
  не пускай qa без код от developer.
- Ако qa върне NEEDS_WORK, върни задачата на developer за поправка.
- Когато qa одобри кода (APPROVED), избери FINISH.
- Ако задачата изобщо не е софтуерна, избери FINISH веднага.

Отговори само със структурираното решение."""


# ---------------------------------------------------------------------------
# Възли (nodes) на графа
# ---------------------------------------------------------------------------
# Агентите се създават ВЪТРЕ в build_graph() (не на ниво модул), за да
# може всяко извикване на build_graph() да вземе АКТУАЛНИЯ LLM_PROVIDER
# от средата - така UI-ят (app.py) превключва anthropic/ollama без
# рестарт на процеса.


def make_supervisor_node(supervisor_llm):
    """
    Фабрика за възела-надзорник.

    Възелът чете цялата история и решава следващата стъпка. Връща dict
    с промени по състоянието: 'next' се чете от routing функцията, а
    'reason' се пази, за да може UI-ят да визуализира ЗАЩО е взето
    решението. Самата визуализация е в main.py/app.py - графът само
    произвежда данни (separation of concerns).
    """

    def supervisor_node(state: MessagesState) -> dict:
        decision = supervisor_llm.invoke(
            [{"role": "system", "content": SUPERVISOR_PROMPT}, *state["messages"]]
        )

        return {"next": decision.next, "reason": decision.reason}

    return supervisor_node


def make_worker_node(agent, name: str):
    """
    Фабрика за възли-работници.

    Защо фабрика? Тримата работници правят едно и също "опаковане":
    1. подават цялата история на своя агент;
    2. взимат финалния му отговор;
    3. добавят го обратно в общата история, ПОДПИСАН с името на агента
       (name=...), за да знае супервайзорът кой какво е казал.
    Вместо да копираме този код 3 пъти, го пишем веднъж.
    """

    def worker_node(state: MessagesState) -> dict:
        # Агентът получава цялата дотукашна история и пуска своя
        # вътрешен ReAct цикъл (мислене + инструменти).
        result = agent.invoke({"messages": state["messages"]})

        # Последното съобщение е финалният отговор на агента -
        # взимаме само текста му (виж extract_text защо).
        final_answer = extract_text(result["messages"][-1].content)

        # Връщаме го като ново съобщение в ОБЩАТА история.
        # MessagesState автоматично ДОБАВЯ (append) новите съобщения,
        # вместо да замества списъка - това е неговата "магия".
        return {
            "messages": [HumanMessage(content=final_answer, name=name)]
        }

    return worker_node


# ---------------------------------------------------------------------------
# Разширено състояние: MessagesState + полето 'next' за routing
# ---------------------------------------------------------------------------


class TeamState(MessagesState):
    """Състоянието на графа: историята + решението кой е следващ и защо."""

    next: str
    reason: str


def route_after_supervisor(state: TeamState) -> str:
    """
    Routing функция за условното ребро след супервайзора.

    Чете решението от състоянието и връща името на следващия възел.
    'FINISH' се превежда до END - специалния краен възел на LangGraph.
    """
    if state["next"] == "FINISH":
        return END
    return state["next"]


# ---------------------------------------------------------------------------
# Сглобяване на графа
# ---------------------------------------------------------------------------


def build_graph():
    """
    Сглобява и компилира мултиагентния граф.

    Всичко LLM-зависимо (агенти, супервайзор) се създава ТУК, при всяко
    извикване - така графът отразява текущия LLM_PROVIDER от средата.
    """
    # LLM клиент на супервайзора, "закотвен" към Pydantic схемата:
    # with_structured_output гарантира, че отговорът ще е SupervisorDecision.
    supervisor_llm = get_llm().with_structured_output(SupervisorDecision)

    builder = StateGraph(TeamState)

    # 1. Регистрираме възлите (име -> функция)
    builder.add_node("supervisor", make_supervisor_node(supervisor_llm))
    builder.add_node("analyst", make_worker_node(create_analyst(), "analyst"))
    builder.add_node("developer", make_worker_node(create_developer(), "developer"))
    builder.add_node("qa", make_worker_node(create_qa(), "qa"))

    # 2. Входна точка: разговорът винаги започва при супервайзора
    builder.add_edge(START, "supervisor")

    # 3. Условно ребро: след супервайзора отиваме там, където той реши
    builder.add_conditional_edges(
        "supervisor",
        route_after_supervisor,
        # Изброяваме възможните дестинации (за яснота и за визуализация)
        ["analyst", "developer", "qa", END],
    )

    # 4. След всеки работник управлението се ВРЪЩА на супервайзора
    for worker in WORKERS:
        builder.add_edge(worker, "supervisor")

    # compile() превръща описанието в изпълним обект с .invoke()/.stream()
    return builder.compile()


# Готов инстанциран граф на ниво модул - това е "входната точка", която
# LangGraph Studio очаква (виж langgraph.json: "./src/graph.py:graph").
graph = build_graph()
