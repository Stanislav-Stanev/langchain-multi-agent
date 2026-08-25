"""
Обща тестова инфраструктура (fixtures) за тестовете на Multi-Bot.

Стратегията на тестване (тестова пирамида):

    1. UNIT тестове        - инструменти, i18n, config, помощни функции.
    2. ИНТЕГРАЦИОННИ       - истинските ReAct агенти (create_agent) +
                             истинските mock инструменти, но със
                             СКРИПТИРАН фалшив LLM (без мрежа).
    3. E2E WORKFLOW тестове - целият граф от build_graph() с
                             детерминистични дубльори.
    4. EVAL тестове         - качеството на РЕАЛНИТЕ LLM решения
                             (tests/evals/, маркер 'eval', пускат се
                             отделно: pytest -m eval).

Ключов принцип: нива 1-3 НЕ викат реален LLM API. Всичко LLM-зависимо
се подменя с "дубльори" (test doubles), които връщат предварително
скриптирани отговори. Така тестовете са бързи, безплатни и стабилни.

Архитектурата под тест (виж src/graph.py): супервайзорът прави
еднократен triage, после потокът е детерминистичен - analyst ->
developer -> qa, с DoD проверки и rework цикъл по QAVerdict. Затова
дубльорите тук са ТРИ: triage решения, QA присъди и работни агенти.
"""

import pytest
from langchain_core.messages import AIMessage

from src import graph as graph_module
from src.graph import QAVerdict, SupervisorDecision


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Праща всеки тест към стойностите по подразбиране (език bg,
    стандартен rework лимит, без fallback модел и без checkpointer),
    независимо от средата на разработчика. Тестове, които проверяват
    друго, сами си задават стойностите с monkeypatch."""
    monkeypatch.delenv("APP_LANG", raising=False)
    monkeypatch.delenv("MAX_REWORK", raising=False)
    monkeypatch.delenv("MODEL_FALLBACK", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL_FALLBACK", raising=False)
    monkeypatch.delenv("CHECKPOINT_SQLITE_PATH", raising=False)


# ---------------------------------------------------------------------------
# Дубльори (test doubles) за LLM-зависимите обекти
# ---------------------------------------------------------------------------


def decide(next_: str, reason: str = "тестово решение") -> SupervisorDecision:
    """Кратък помощник за построяване на triage решение на супервайзора."""
    return SupervisorDecision(next=next_, reason=reason)


def verdict(status: str, issues: list[str] | None = None) -> QAVerdict:
    """Кратък помощник за построяване на QA присъда."""
    return QAVerdict(status=status, issues=issues or [])


class _ScriptedLLM:
    """
    Базов скриптиран "LLM": връща отговорите един по един; когато остане
    само един, го ПОВТАРЯ до безкрай (нужно за тестовете на лимитите,
    при които агентът "циклира" един и същи отговор).

    Записва всяко получено повикване в self.calls, за да могат тестовете
    да проверят какво точно е видял моделът (system prompt, история).
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def invoke(self, payload):
        self.calls.append(payload)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


class FakeLLMFactory:
    """
    Дубльор на get_llm(role): графът вика .with_structured_output(схема)
    върху него - диспечираме по схемата към правилния скриптиран LLM
    (SupervisorDecision -> triage, QAVerdict -> присъди). Пазим схемите,
    за да проверим, че графът иска точно тях.
    """

    def __init__(self, supervisor_llm, verdict_llm):
        self.supervisor_llm = supervisor_llm
        self.verdict_llm = verdict_llm

    def with_structured_output(self, schema):
        if schema is SupervisorDecision:
            self.supervisor_llm.requested_schema = schema
            return self.supervisor_llm
        if schema is QAVerdict:
            self.verdict_llm.requested_schema = schema
            return self.verdict_llm
        raise AssertionError(f"Неочаквана структурирана схема: {schema}")


class FakeWorkerAgent:
    """
    Дубльор на ReAct агент (резултата от create_agent).

    Графът вика agent.invoke({"messages": [...]}) и чете последното
    съобщение - имитираме точно този контракт. Отговорите се скриптират
    (списък), а последният се повтаря при нужда (за rework/DoD циклите
    са нужни различни отговори на всяко извикване).
    """

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.invocations = []  # историята, която агентът е получил

    def invoke(self, payload):
        self.invocations.append(list(payload["messages"]))
        content = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        return {"messages": [AIMessage(content=content)]}


class GraphHarness:
    """Всичко нужно на един E2E тест, събрано на едно място."""

    def __init__(self, graph, supervisor, verdicts, workers):
        self.graph = graph
        self.supervisor = supervisor
        self.verdicts = verdicts
        self.workers = workers


# Валиден Developer отговор по подразбиране - покрива DoD проверката
# (```python блок + валиден синтаксис).
DEFAULT_DEV_OUTPUT = (
    "Ето кода:\n```python\n"
    'def validate_email(email: str) -> bool:\n'
    '    """Валидира имейл."""\n'
    '    if not email:\n'
    "        return False\n"
    '    return email.count("@") == 1\n'
    "```"
)


# ---------------------------------------------------------------------------
# Фабрика за "скриптиран" граф - целият workflow без нито едно LLM извикване
# ---------------------------------------------------------------------------


@pytest.fixture
def scripted_graph(monkeypatch):
    """
    Фабрика-fixture: строи ИСТИНСКИЯ граф (build_graph, реалните възли,
    DoD проверки и routing), но с подменени LLM-зависими съставки.

    Подмяната става върху имената В src.graph (а не в src.agents),
    защото build_graph() ползва точно тях.
    """

    def _build(
        decisions=("analyst",),
        verdicts=("APPROVED",),
        analyst_outputs=None,
        developer_outputs=None,
        qa_outputs=None,
        checkpointer=None,
    ):
        supervisor = _ScriptedLLM(
            [decide(d) if isinstance(d, str) else d for d in decisions]
        )
        verdict_llm = _ScriptedLLM(
            [verdict(v) if isinstance(v, str) else v for v in verdicts]
        )
        workers = {
            "analyst": FakeWorkerAgent(
                analyst_outputs or ["Спецификация: функция validate_email(s) -> bool."]
            ),
            "developer": FakeWorkerAgent(developer_outputs or [DEFAULT_DEV_OUTPUT]),
            "qa": FakeWorkerAgent(qa_outputs or ["Доклад: кодът покрива критериите."]),
        }

        factory = FakeLLMFactory(supervisor, verdict_llm)
        monkeypatch.setattr(
            graph_module,
            "get_llm",
            lambda role="default", model_override=None: factory,
        )
        monkeypatch.setattr(graph_module, "create_analyst", lambda: workers["analyst"])
        monkeypatch.setattr(graph_module, "create_developer", lambda: workers["developer"])
        monkeypatch.setattr(graph_module, "create_qa", lambda: workers["qa"])

        return GraphHarness(
            graph_module.build_graph(checkpointer=checkpointer),
            supervisor,
            verdict_llm,
            workers,
        )

    return _build
