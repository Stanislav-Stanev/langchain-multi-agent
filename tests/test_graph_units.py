"""
Unit тестове за градивните елементи на src/graph.py.

Покриваме поотделно: extract_text (защитата срещу thinking блокове),
routing функцията, Pydantic схемата на решението, фабриките за възли и
структурата на компилирания граф (възли и ребра).
"""

from typing import get_args

import pytest
from langchain_core.messages import HumanMessage
from langgraph.graph import END
from pydantic import ValidationError

from src.graph import (
    SUPERVISOR_PROMPTS,
    WORKERS,
    SupervisorDecision,
    extract_text,
    make_supervisor_node,
    make_worker_node,
    route_after_supervisor,
)
from tests.conftest import FakeWorkerAgent, ScriptedSupervisorLLM, decide


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


class TestRouting:
    @pytest.mark.parametrize("worker", WORKERS)
    def test_worker_names_route_to_worker(self, worker):
        assert route_after_supervisor({"next": worker}) == worker

    def test_finish_routes_to_end(self):
        assert route_after_supervisor({"next": "FINISH"}) is END


class TestSupervisorDecision:
    @pytest.mark.parametrize("value", [*WORKERS, "FINISH"])
    def test_accepts_all_valid_destinations(self, value):
        assert SupervisorDecision(next=value, reason="r").next == value

    def test_rejects_unknown_destination(self):
        # Структурираният изход е защитата срещу "халюциниран" routing
        with pytest.raises(ValidationError):
            SupervisorDecision(next="manager", reason="r")

    def test_literal_matches_workers_constant(self):
        # Контракт: списъкът WORKERS и Literal-ът в схемата не се разминават
        allowed = set(get_args(SupervisorDecision.model_fields["next"].annotation))
        assert allowed == set(WORKERS) | {"FINISH"}


class TestMakeSupervisorNode:
    def test_returns_next_and_reason(self):
        node = make_supervisor_node(ScriptedSupervisorLLM([decide("analyst", "първи е")]))
        update = node({"messages": [HumanMessage(content="задача")]})
        assert update == {"next": "analyst", "reason": "първи е"}

    def test_prepends_system_prompt_in_current_language(self, monkeypatch):
        llm = ScriptedSupervisorLLM([decide("FINISH")])
        node = make_supervisor_node(llm)

        node({"messages": [HumanMessage(content="задача")]})
        assert llm.calls[0][0] == {"role": "system", "content": SUPERVISOR_PROMPTS["bg"]}

        monkeypatch.setenv("APP_LANG", "en")
        node({"messages": [HumanMessage(content="task")]})
        assert llm.calls[1][0] == {"role": "system", "content": SUPERVISOR_PROMPTS["en"]}

    def test_passes_full_history_to_llm(self):
        llm = ScriptedSupervisorLLM([decide("FINISH")])
        node = make_supervisor_node(llm)
        history = [HumanMessage(content="а"), HumanMessage(content="б", name="analyst")]
        node({"messages": history})
        # system + двете съобщения, в същия ред
        assert llm.calls[0][1:] == history


class TestMakeWorkerNode:
    def test_wraps_answer_as_named_human_message(self):
        agent = FakeWorkerAgent(["Спецификация готова."])
        node = make_worker_node(agent, "analyst")

        update = node({"messages": [HumanMessage(content="задача")]})
        (msg,) = update["messages"]

        # Контрактът към супервайзора: HumanMessage, ПОДПИСАН с името
        assert isinstance(msg, HumanMessage)
        assert msg.name == "analyst"
        assert msg.content == "Спецификация готова."

    def test_strips_thinking_blocks_from_agent_answer(self):
        agent = FakeWorkerAgent(
            [[
                {"type": "thinking", "thinking": "да помисля..."},
                {"type": "text", "text": "чист отговор"},
            ]]
        )
        node = make_worker_node(agent, "developer")
        (msg,) = node({"messages": []})["messages"]
        assert msg.content == "чист отговор"

    def test_agent_receives_the_whole_history(self):
        agent = FakeWorkerAgent(["ок"])
        node = make_worker_node(agent, "qa")
        history = [HumanMessage(content="1"), HumanMessage(content="2", name="developer")]
        node({"messages": history})
        assert agent.invocations[0] == history


class TestGraphStructure:
    def test_nodes_and_edges(self, scripted_graph):
        compiled, _, _ = scripted_graph(["FINISH"])
        g = compiled.get_graph()

        assert set(g.nodes) == {"__start__", "supervisor", "analyst", "developer", "qa", "__end__"}

        edges = {(e.source, e.target) for e in g.edges}
        assert ("__start__", "supervisor") in edges          # входна точка
        for worker in WORKERS:
            assert (worker, "supervisor") in edges           # връщане към шефа
            assert ("supervisor", worker) in edges           # условното ребро
        assert ("supervisor", "__end__") in edges            # FINISH -> END

    def test_supervisor_uses_structured_output_schema(self, scripted_graph):
        _, supervisor, _ = scripted_graph(["FINISH"])
        # build_graph() трябва да е поискал точно SupervisorDecision
        assert supervisor.requested_schema is SupervisorDecision

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
