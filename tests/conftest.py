"""
Обща тестова инфраструктура (fixtures) за тестовете на Multi-Bot.

Стратегията на тестване (тестова пирамида):

    1. UNIT тестове        - инструменти, i18n, config, режими, планове,
                             проследяване, DoD политики, HITL механизъм.
    2. ИНТЕГРАЦИОННИ       - истинските ReAct агенти (create_agent) +
                             истинските инструменти, но със СКРИПТИРАН
                             фалшив LLM (без мрежа).
    3. E2E WORKFLOW тестове - целият граф от build_graph() с
                             детерминистични дубльори (вкл. HITL портите
                             с InMemorySaver и Command(resume=...)).
    4. EVAL тестове         - качеството на РЕАЛНИТЕ LLM решения
                             (tests/evals/, маркер 'eval', пускат се
                             отделно: pytest -m eval).

Ключов принцип: нива 1-3 НЕ викат реален LLM API и НЕ пипат мрежа/диск
извън tmp_path. Всичко LLM-зависимо се подменя с "дубльори" (test
doubles), които връщат предварително скриптирани отговори.

Архитектурата под тест (виж src/graph.py): супервайзорът прави
еднократен triage, после потокът е детерминистичен - analyst -> dev_plan
-> approve_plan -> developer -> qa_plan -> qa -> finalize, с DoD проверки,
rework цикъл по QAVerdict и Human-in-the-Loop порти. Дубльорите тук са:
triage решения, планове, тест-планове, QA присъди и работни агенти.
"""

import pytest
from langchain_core.messages import AIMessage

from src import graph as graph_module
from src import plans as plans_module
from src.graph import QAVerdict, SupervisorDecision
from src.plans import AcceptanceCriterion, ImplementationPlan, PlanStep


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Праща всеки тест към стойностите по подразбиране (език bg, demo режим,
    без HITL порти, run артефакти в tmp_path, стандартен rework лимит, без
    fallback модел и без checkpointer), независимо от средата на разработчика.
    Тестове, които проверяват друго, сами си задават стойностите с monkeypatch."""
    monkeypatch.delenv("APP_LANG", raising=False)
    monkeypatch.delenv("MAX_REWORK", raising=False)
    monkeypatch.delenv("MODEL_FALLBACK", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL_FALLBACK", raising=False)
    monkeypatch.delenv("CHECKPOINT_SQLITE_PATH", raising=False)
    monkeypatch.delenv("HITL_MAX_REVISIONS", raising=False)
    monkeypatch.delenv("HITL_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("PROD_REPOS", raising=False)
    for var in ("JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_MCP_AUTH"):
        monkeypatch.delenv(var, raising=False)
    # Demo режим, без порти, артефактите - в временна директория на теста
    monkeypatch.setenv("APP_MODE", "demo")
    monkeypatch.setenv("HITL_GATES", "")
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspace"))


# ---------------------------------------------------------------------------
# Дубльори (test doubles) за LLM-зависимите обекти
# ---------------------------------------------------------------------------


def decide(next_: str, reason: str = "тестово решение") -> SupervisorDecision:
    """Кратък помощник за построяване на triage решение на супервайзора."""
    return SupervisorDecision(next=next_, reason=reason)


def verdict(status: str, issues: list[str] | None = None) -> QAVerdict:
    """Кратък помощник за построяване на QA присъда."""
    return QAVerdict(status=status, issues=issues or [])


def sample_plan(steps: int = 2, criteria: int = 2, summary: str = "План: функция validate_email.") -> ImplementationPlan:
    """План за имплементация с N стъпки, всяка покрива по един критерий."""
    acs = [AcceptanceCriterion(id=f"AC-{i}", text=f"критерий {i}") for i in range(1, criteria + 1)]
    return ImplementationPlan(
        summary=summary,
        acceptance_criteria=acs,
        steps=[
            PlanStep(
                id=f"S{i}",
                title=f"стъпка {i}",
                files=["validators.py"],
                acceptance_criteria_refs=[acs[(i - 1) % len(acs)].id] if acs else [],
            )
            for i in range(1, steps + 1)
        ],
    )


def sample_test_plan(cases: int = 2, summary: str = "Тест-план: проверка по критерии.") -> plans_module.TestPlan:
    """Тест-план с N случая, сочещи AC-1, AC-2, ..."""
    return plans_module.TestPlan(
        summary=summary,
        cases=[
            plans_module.TestCase(
                id=f"T{i}", title=f"тест {i}", criterion_ref=f"AC-{i}",
                method="manual_review", expected="очакван резултат",
            )
            for i in range(1, cases + 1)
        ],
    )


EMPTY_PLAN = ImplementationPlan(summary="празен", acceptance_criteria=[], steps=[])
EMPTY_TEST_PLAN = plans_module.TestPlan(summary="празен", cases=[])


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
    (SupervisorDecision -> triage, QAVerdict -> присъди, ImplementationPlan
    -> планове, TestPlan -> тест-планове). Пазим схемите, за да проверим,
    че графът иска точно тях.
    """

    def __init__(self, supervisor_llm, verdict_llm, plan_llm=None, test_plan_llm=None):
        self.supervisor_llm = supervisor_llm
        self.verdict_llm = verdict_llm
        self.plan_llm = plan_llm or _ScriptedLLM([sample_plan()])
        self.test_plan_llm = test_plan_llm or _ScriptedLLM([sample_test_plan()])

    def with_structured_output(self, schema):
        by_schema = {
            SupervisorDecision: self.supervisor_llm,
            QAVerdict: self.verdict_llm,
            ImplementationPlan: self.plan_llm,
            plans_module.TestPlan: self.test_plan_llm,
        }
        llm = by_schema.get(schema)
        if llm is None:
            raise AssertionError(f"Неочаквана структурирана схема: {schema}")
        llm.requested_schema = schema
        return llm


class FakeWorkerAgent:
    """
    Дубльор на ReAct агент (резултата от create_agent).

    Графът вика agent.invoke({"messages": [...]}, context=RunCtx) и чете
    последното съобщение - имитираме точно този контракт. Отговорите се
    скриптират (списък), а последният се повтаря при нужда. side_effect(ctx)
    симулира работата на инструментите (напр. отмятане на стъпка от плана).
    """

    def __init__(self, outputs, side_effect=None):
        self.outputs = list(outputs)
        self.invocations = []  # историята, която агентът е получил
        self.contexts = []     # RunCtx, подаден при всяко извикване
        self.side_effect = side_effect

    def invoke(self, payload, **kwargs):
        self.invocations.append(list(payload["messages"]))
        ctx = kwargs.get("context")
        self.contexts.append(ctx)
        if self.side_effect is not None:
            self.side_effect(ctx)
        content = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        return {"messages": [AIMessage(content=content)]}


class GraphHarness:
    """Всичко нужно на един E2E тест, събрано на едно място."""

    def __init__(self, graph, supervisor, verdicts, workers, plans, test_plans, runs_dir):
        self.graph = graph
        self.supervisor = supervisor
        self.verdicts = verdicts
        self.workers = workers
        self.plans = plans
        self.test_plans = test_plans
        self.runs_dir = runs_dir

    def run_dirs(self):
        """Създадените run директории (обикновено една)."""
        return sorted(p for p in self.runs_dir.iterdir() if p.is_dir()) if self.runs_dir.exists() else []


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
def scripted_graph(monkeypatch, tmp_path):
    """
    Фабрика-fixture: строи ИСТИНСКИЯ граф (build_graph, реалните възли,
    DoD проверки, планове, проследяване и routing), но с подменени
    LLM-зависими съставки.

    Подмяната става върху имената В src.graph (а не в src.agents),
    защото build_graph() ползва точно тях. По подразбиране: demo режим,
    без HITL порти (hitl_gates=()), артефакти в tmp_path/runs.
    """

    def _build(
        decisions=("analyst",),
        verdicts=("APPROVED",),
        analyst_outputs=None,
        developer_outputs=None,
        qa_outputs=None,
        plans=None,
        test_plans=None,
        checkpointer=None,
        hitl_gates=(),
        mode="demo",
        developer_side_effect=None,
    ):
        supervisor = _ScriptedLLM(
            [decide(d) if isinstance(d, str) else d for d in decisions]
        )
        verdict_llm = _ScriptedLLM(
            [verdict(v) if isinstance(v, str) else v for v in verdicts]
        )
        plan_llm = _ScriptedLLM(list(plans) if plans else [sample_plan()])
        test_plan_llm = _ScriptedLLM(list(test_plans) if test_plans else [sample_test_plan()])
        workers = {
            "analyst": FakeWorkerAgent(
                analyst_outputs or ["Спецификация: функция validate_email(s) -> bool."]
            ),
            "developer": FakeWorkerAgent(
                developer_outputs or [DEFAULT_DEV_OUTPUT], side_effect=developer_side_effect
            ),
            "qa": FakeWorkerAgent(qa_outputs or ["Доклад: кодът покрива критериите."]),
        }

        factory = FakeLLMFactory(supervisor, verdict_llm, plan_llm, test_plan_llm)
        monkeypatch.setattr(
            graph_module,
            "get_llm",
            lambda role="default", model_override=None: factory,
        )
        monkeypatch.setattr(graph_module, "create_analyst", lambda *a, **k: workers["analyst"])
        monkeypatch.setattr(graph_module, "create_developer", lambda *a, **k: workers["developer"])
        monkeypatch.setattr(graph_module, "create_qa", lambda *a, **k: workers["qa"])

        runs_dir = tmp_path / "runs"
        return GraphHarness(
            graph_module.build_graph(
                checkpointer=checkpointer, mode=mode, runs_dir=runs_dir, hitl_gates=hitl_gates
            ),
            supervisor,
            verdict_llm,
            workers,
            plan_llm,
            test_plan_llm,
            runs_dir,
        )

    return _build
