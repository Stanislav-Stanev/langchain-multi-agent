"""
Обща тестова инфраструктура (fixtures) за тестовете на Multi-Bot.

Стратегията на тестване (тестова пирамида):

    1. UNIT тестове        - инструменти, i18n, config, помощни функции.
    2. ИНТЕГРАЦИОННИ       - истинските ReAct агенти (create_agent) +
                             истинските mock инструменти, но със
                             СКРИПТИРАН фалшив LLM (без мрежа).
    3. E2E WORKFLOW тестове - целият Supervisor граф от build_graph(),
                             при който и супервайзорът, и работниците са
                             детерминистични дубльори.

Ключов принцип: НИКОЙ тест не вика реален LLM API. Всичко LLM-зависимо
се подменя с "дубльори" (test doubles), които връщат предварително
скриптирани отговори. Така тестовете са бързи, безплатни и стабилни.
"""

import pytest
from langchain_core.messages import AIMessage

from src import graph as graph_module
from src.graph import SupervisorDecision


@pytest.fixture(autouse=True)
def _clean_lang(monkeypatch):
    """Праща всеки тест към езика по подразбиране (bg), независимо какво
    има в средата на разработчика. Тестове, които проверяват английски,
    сами си задават APP_LANG=en с monkeypatch."""
    monkeypatch.delenv("APP_LANG", raising=False)


# ---------------------------------------------------------------------------
# Дубльори (test doubles) за LLM-зависимите обекти
# ---------------------------------------------------------------------------


def decide(next_: str, reason: str = "тестово решение") -> SupervisorDecision:
    """Кратък помощник за построяване на решение на супервайзора."""
    return SupervisorDecision(next=next_, reason=reason)


class ScriptedSupervisorLLM:
    """
    Фалшив "structured output" LLM за супервайзора.

    Връща предварително скриптирани SupervisorDecision обекти един по
    един; когато остане само едно решение, го ПОВТАРЯ до безкрай (нужно
    за теста на recursion_limit, при който супервайзорът "зацикля").

    Записва всяко получено повикване в self.calls, за да могат тестовете
    да проверят какво точно е видял супервайзорът (system prompt, история).
    """

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.calls = []

    def invoke(self, messages):
        self.calls.append(list(messages))
        if len(self.decisions) > 1:
            return self.decisions.pop(0)
        return self.decisions[0]


class FakeStructuredLLMFactory:
    """Дубльор на get_llm(): графът вика .with_structured_output(схема)
    върху него и очаква runnable - връщаме скриптирания супервайзор.
    Пазим схемата, за да проверим, че графът иска точно SupervisorDecision."""

    def __init__(self, supervisor_llm):
        self.supervisor_llm = supervisor_llm
        self.requested_schema = None

    def with_structured_output(self, schema):
        self.requested_schema = schema
        self.supervisor_llm.requested_schema = schema
        return self.supervisor_llm


class FakeWorkerAgent:
    """
    Дубльор на ReAct агент (резултата от create_agent).

    Графът вика agent.invoke({"messages": [...]}) и чете последното
    съобщение - имитираме точно този контракт. Отговорите се скриптират
    (списък), а последният се повтаря при нужда (за QA цикъла NEEDS_WORK
    -> APPROVED са нужни два различни отговора).
    """

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.invocations = []  # историята, която агентът е получил

    def invoke(self, payload):
        self.invocations.append(list(payload["messages"]))
        content = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        return {"messages": [AIMessage(content=content)]}


# ---------------------------------------------------------------------------
# Фабрика за "скриптиран" граф - целият workflow без нито едно LLM извикване
# ---------------------------------------------------------------------------


@pytest.fixture
def scripted_graph(monkeypatch):
    """
    Фабрика-fixture: строи ИСТИНСКИЯ граф (build_graph, реалните възли,
    ребра и routing), но с подменени LLM-зависими съставки.

    Подмяната става върху имената В src.graph (а не в src.agents),
    защото build_graph() ползва точно тях.
    """

    def _build(decisions, analyst_outputs=None, developer_outputs=None, qa_outputs=None):
        supervisor = ScriptedSupervisorLLM(
            [decide(d) if isinstance(d, str) else d for d in decisions]
        )
        workers = {
            "analyst": FakeWorkerAgent(
                analyst_outputs or ["Спецификация: функция validate_email(s) -> bool."]
            ),
            "developer": FakeWorkerAgent(
                developer_outputs or ["```python\ndef validate_email(s: str) -> bool: ...\n```"]
            ),
            "qa": FakeWorkerAgent(qa_outputs or ["Статус: APPROVED"]),
        }

        monkeypatch.setattr(
            graph_module, "get_llm", lambda: FakeStructuredLLMFactory(supervisor)
        )
        monkeypatch.setattr(graph_module, "create_analyst", lambda: workers["analyst"])
        monkeypatch.setattr(graph_module, "create_developer", lambda: workers["developer"])
        monkeypatch.setattr(graph_module, "create_qa", lambda: workers["qa"])

        return graph_module.build_graph(), supervisor, workers

    return _build
