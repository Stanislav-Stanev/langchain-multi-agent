"""
Unit тестове за градивните елементи на src/graph.py.

Покриваме поотделно: extract_text (защитата срещу thinking блокове),
extract_python_code (артефактът на Developer), routing функцията,
Pydantic схемите на решенията, фабриките за възли (вкл. DoD логиката)
и структурата на компилирания граф (възли и ребра).
"""

from typing import get_args

import pytest
from langchain_core.messages import HumanMessage
from langgraph.graph import END
from pydantic import ValidationError

from src.graph import (
    SUPERVISOR_PROMPTS,
    WORKERS,
    QAVerdict,
    SupervisorDecision,
    extract_python_code,
    extract_text,
    make_analyst_node,
    make_developer_node,
    make_qa_node,
    make_structured_llm,
    make_supervisor_node,
    max_rework,
    route_next,
)
from tests.conftest import FakeWorkerAgent, _ScriptedLLM, decide, verdict


class TestExtractText:
    def test_plain_string_passes_through(self):
        assert extract_text("готово") == "готово"

    def test_filters_thinking_blocks(self):
        # Гочата от CLAUDE.md: thinking блоковете НЕ бива да стигат до
        # HumanMessage - API-то ги отхвърля извън assistant роля.
        content = [
            {"type": "thinking", "thinking": "тайни разсъждения"},
            {"type": "text", "text": "видим отговор"},
        ]
        result = extract_text(content)
        assert result == "видим отговор"
        assert "тайни" not in result

    def test_joins_multiple_text_blocks(self):
        content = [
            {"type": "text", "text": "ред 1"},
            {"type": "thinking", "thinking": "x"},
            {"type": "text", "text": "ред 2"},
        ]
        assert extract_text(content) == "ред 1\nред 2"

    def test_empty_list_gives_empty_string(self):
        assert extract_text([]) == ""

    def test_non_dict_blocks_are_ignored(self):
        assert extract_text(["суров низ", {"type": "text", "text": "ок"}]) == "ок"


class TestExtractPythonCode:
    def test_extracts_single_block(self):
        text = "Ето кода:\n```python\ndef f():\n    return 1\n```\nГотово."
        assert extract_python_code(text) == "def f():\n    return 1"

    def test_joins_multiple_blocks(self):
        text = "```python\ndef f(): ...\n```\nи тестът:\n```python\ndef test_f(): ...\n```"
        code = extract_python_code(text)
        assert "def f()" in code and "def test_f()" in code

    def test_no_block_returns_empty(self):
        # Липсващ блок проваля Definition of Done -> връщане към Developer
        assert extract_python_code("тук няма код, само обяснение") == ""

    def test_plain_fence_without_language_is_not_matched(self):
        # Изискваме ЕЗИКОВО обозначен блок - това е и стандартът от промпта
        assert extract_python_code("```\nx = 1\n```") == ""

    def test_empty_input(self):
        assert extract_python_code("") == ""


class TestRouting:
    @pytest.mark.parametrize("worker", WORKERS)
    def test_worker_names_route_to_worker(self, worker):
        assert route_next({"next": worker}) == worker

    def test_finish_routes_to_end(self):
        assert route_next({"next": "FINISH"}) is END


class TestDecisionSchemas:
    @pytest.mark.parametrize("value", [*WORKERS, "FINISH"])
    def test_supervisor_accepts_all_valid_destinations(self, value):
        assert SupervisorDecision(next=value, reason="r").next == value

    def test_supervisor_rejects_unknown_destination(self):
        # Структурираният изход е защитата срещу "халюциниран" routing
        with pytest.raises(ValidationError):
            SupervisorDecision(next="manager", reason="r")

    def test_supervisor_literal_matches_workers_constant(self):
        # Контракт: списъкът WORKERS и Literal-ът в схемата не се разминават
        allowed = set(get_args(SupervisorDecision.model_fields["next"].annotation))
        assert allowed == set(WORKERS) | {"FINISH"}

    @pytest.mark.parametrize("status", ["APPROVED", "NEEDS_WORK"])
    def test_qa_verdict_statuses(self, status):
        assert QAVerdict(status=status).status == status

    def test_qa_verdict_rejects_free_text_status(self):
        # Точно това пази routing-а: "кодът е одобрен" не е валиден статус
        with pytest.raises(ValidationError):
            QAVerdict(status="кодът е одобрен")

    def test_qa_verdict_issues_default_to_empty(self):
        assert QAVerdict(status="APPROVED").issues == []


class TestMaxRework:
    def test_default_is_three(self):
        assert max_rework() == 3

    def test_reads_env(self, monkeypatch):
        monkeypatch.setenv("MAX_REWORK", "5")
        assert max_rework() == 5

    def test_invalid_value_falls_back(self, monkeypatch):
        monkeypatch.setenv("MAX_REWORK", "много")
        assert max_rework() == 3


class TestSupervisorNode:
    def test_returns_next_and_reason(self):
        node = make_supervisor_node(_ScriptedLLM([decide("analyst", "първи е")]))
        update = node({"messages": [HumanMessage(content="задача")]})
        assert update == {"next": "analyst", "reason": "първи е"}

    def test_finish_sets_no_action_status(self):
        node = make_supervisor_node(_ScriptedLLM([decide("FINISH")]))
        update = node({"messages": [HumanMessage(content="времето утре?")]})
        assert update["final_status"] == "NO_ACTION"

    def test_prepends_system_prompt_in_current_language(self, monkeypatch):
        llm = _ScriptedLLM([decide("FINISH")])
        node = make_supervisor_node(llm)

        node({"messages": [HumanMessage(content="задача")]})
        assert llm.calls[0][0] == {"role": "system", "content": SUPERVISOR_PROMPTS["bg"]}

        monkeypatch.setenv("APP_LANG", "en")
        node({"messages": [HumanMessage(content="task")]})
        assert llm.calls[1][0] == {"role": "system", "content": SUPERVISOR_PROMPTS["en"]}


class TestAnalystNode:
    def test_stores_spec_and_routes_to_developer(self):
        node = make_analyst_node(FakeWorkerAgent(["Спецификация готова."]))
        update = node({"messages": [HumanMessage(content="задача")]})

        assert update["spec"] == "Спецификация готова."
        assert update["next"] == "developer"
        (msg,) = update["messages"]
        assert isinstance(msg, HumanMessage) and msg.name == "analyst"

    def test_strips_thinking_blocks_from_agent_answer(self):
        agent = FakeWorkerAgent(
            [[
                {"type": "thinking", "thinking": "да помисля..."},
                {"type": "text", "text": "чист отговор"},
            ]]
        )
        update = make_analyst_node(agent)({"messages": []})
        assert update["spec"] == "чист отговор"

    def test_empty_spec_triggers_dod_retry_then_escalation(self):
        node = make_analyst_node(FakeWorkerAgent([""]))

        first = node({"messages": []})
        assert first["next"] == "analyst"           # един повторен опит
        assert first["dod_retries"] == {"analyst": 1}

        second = node({"messages": [], "dod_retries": first["dod_retries"]})
        assert second["next"] == "FINISH"           # после ескалация
        assert second["final_status"] == "ESCALATED"


class TestDeveloperNode:
    def test_stores_extracted_code_and_routes_to_qa(self):
        answer = "```python\ndef f() -> int:\n    return 1\n```"
        update = make_developer_node(FakeWorkerAgent([answer]))({"messages": []})

        assert update["code"] == "def f() -> int:\n    return 1"
        assert update["next"] == "qa"

    def test_missing_code_block_fails_dod(self):
        update = make_developer_node(FakeWorkerAgent(["само обяснение"]))({"messages": []})
        assert update["next"] == "developer"

    def test_syntax_error_fails_dod_with_error_in_prompt(self):
        update = make_developer_node(
            FakeWorkerAgent(["```python\ndef f(:\n```"])
        )({"messages": []})
        assert update["next"] == "developer"
        # Коригиращото съобщение съдържа конкретния проблем
        assert "Definition of Done" in update["messages"][-1].content


class TestQaNode:
    def _node(self, verdicts, report="Доклад."):
        agent = FakeWorkerAgent([report])
        verdict_llm = _ScriptedLLM(verdicts)
        return make_qa_node(agent, verdict_llm), verdict_llm

    def test_approved_finishes_the_run(self):
        node, _ = self._node([verdict("APPROVED")])
        update = node({"messages": []})

        assert update["next"] == "FINISH"
        assert update["final_status"] == "APPROVED"
        assert update["qa_verdict"] == {"status": "APPROVED", "issues": []}

    def test_needs_work_returns_to_developer_and_counts(self):
        node, _ = self._node([verdict("NEEDS_WORK", ["баг"])])
        update = node({"messages": [], "rework_count": 0})

        assert update["next"] == "developer"
        assert update["rework_count"] == 1
        assert update["qa_verdict"]["issues"] == ["баг"]

    def test_rework_limit_escalates(self, monkeypatch):
        monkeypatch.setenv("MAX_REWORK", "2")
        node, _ = self._node([verdict("NEEDS_WORK")])
        update = node({"messages": [], "rework_count": 2})  # лимитът е изчерпан

        assert update["next"] == "FINISH"
        assert update["final_status"] == "ESCALATED"

    def test_verdict_llm_gets_the_report_text(self):
        node, verdict_llm = self._node([verdict("APPROVED")], report="УНИКАЛЕН-ДОКЛАД")
        node({"messages": []})
        assert "УНИКАЛЕН-ДОКЛАД" in str(verdict_llm.calls[0])


class TestStructuredLlmFallback:
    """Fallback веригата на критичните структурирани решения (§2.7)."""

    @staticmethod
    def _fake_get_llm(primary, fallback):
        """get_llm дубльор: без model_override връща основния, с - резервния."""

        class _Factory:
            def __init__(self, runnable):
                self.runnable = runnable

            def with_structured_output(self, schema):
                return self.runnable

        def fake(role="default", model_override=None):
            return _Factory(fallback if model_override else primary)

        return fake

    def test_without_fallback_returns_primary_untouched(self, monkeypatch):
        from src import graph as graph_module

        primary = object()  # маркер - никаква верига не бива да се строи
        monkeypatch.setattr(
            graph_module, "get_llm", self._fake_get_llm(primary, object())
        )
        assert make_structured_llm("supervisor", SupervisorDecision) is primary

    def test_fallback_recovers_when_primary_fails(self, monkeypatch):
        from langchain_core.runnables import RunnableLambda

        from src import graph as graph_module

        def _boom(_):
            raise RuntimeError("529 overloaded")

        primary = RunnableLambda(_boom)
        fallback = RunnableLambda(lambda _: decide("analyst", "резервен модел"))

        monkeypatch.setenv("MODEL_FALLBACK", "claude-sonnet-5")
        monkeypatch.setattr(
            graph_module, "get_llm", self._fake_get_llm(primary, fallback)
        )

        chain = make_structured_llm("supervisor", SupervisorDecision)
        decision = chain.invoke("задача")
        assert decision.next == "analyst"
        assert decision.reason == "резервен модел"

    def test_primary_error_propagates_without_fallback(self, monkeypatch):
        from langchain_core.runnables import RunnableLambda

        from src import graph as graph_module

        def _boom(_):
            raise RuntimeError("529 overloaded")

        monkeypatch.setattr(
            graph_module,
            "get_llm",
            self._fake_get_llm(RunnableLambda(_boom), None),
        )

        chain = make_structured_llm("supervisor", SupervisorDecision)
        with pytest.raises(RuntimeError, match="529"):
            chain.invoke("задача")


class TestGraphStructure:
    def test_nodes_and_edges(self, scripted_graph):
        h = scripted_graph()
        g = h.graph.get_graph()

        assert set(g.nodes) == {"__start__", "supervisor", "analyst", "developer", "qa", "__end__"}

        edges = {(e.source, e.target) for e in g.edges}
        assert ("__start__", "supervisor") in edges          # входна точка
        # Детерминистичният конвейер + rework цикълът
        assert ("supervisor", "analyst") in edges            # triage вход
        assert ("analyst", "developer") in edges             # spec -> код
        assert ("developer", "qa") in edges                  # код -> преглед
        assert ("qa", "developer") in edges                  # NEEDS_WORK цикълът
        assert ("qa", "__end__") in edges                    # APPROVED/ESCALATED

    def test_llm_factories_receive_structured_schemas(self, scripted_graph):
        h = scripted_graph()
        # build_graph() трябва да е поискал точно двете схеми
        assert h.supervisor.requested_schema is SupervisorDecision
        assert h.verdicts.requested_schema is QAVerdict

    def test_no_llm_objects_at_module_level(self):
        # Контракт: src.graph НЯМА LLM-зависим код на ниво модул -
        # импортът минава без какъвто и да е API ключ (build_graph()
        # е единственото място, което създава LLM клиенти и агенти).
        # Проверяваме статично (ast), без да изпълняваме нищо.
        import ast as ast_module
        import inspect

        import src.graph as graph_module

        tree = ast_module.parse(inspect.getsource(graph_module))
        forbidden = {"build_graph", "get_llm", "create_analyst", "create_developer", "create_qa"}
        module_level_calls = {
            node.value.func.id
            for node in tree.body
            if isinstance(node, (ast_module.Assign, ast_module.Expr))
            and isinstance(node.value, ast_module.Call)
            and isinstance(node.value.func, ast_module.Name)
        }
        assert not (module_level_calls & forbidden), (
            f"LLM-зависими извиквания на ниво модул: {module_level_calls & forbidden}"
        )
