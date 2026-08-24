"""
E2E тестове на ЦЕЛИЯ Multi-Bot workflow (реалният граф от build_graph).

Тук се изпълнява истинският LangGraph state machine - възли, условни
ребра, MessagesState натрупване - а LLM-зависимите части (супервайзор и
работници) са скриптирани дубльори (виж conftest.py). Това ни дава
детерминистична проверка на всички важни сценарии:

  1. Happy path:      analyst -> developer -> qa -> FINISH
  2. Rework цикълът:  qa връща NEEDS_WORK -> developer поправя -> qa -> FINISH
  3. Незабавен FINISH за несофтуерна задача
  4. Защитата recursion_limit при "зациклил" супервайзор
  5. Streaming контрактът, който main.py/app.py консумират
  6. Езикът на супервайзорския промпт (bg/en) при сглобяване
"""

import pytest
from langchain_core.messages import HumanMessage
from langgraph.errors import GraphRecursionError

from src.graph import SUPERVISOR_PROMPTS

TASK = "Имплементирай тикет DEV-101 и се увери, че кодът е качествен."


def run(compiled, task=TASK, recursion_limit=25):
    """Изпълнява графа както main.py: една задача, ограничен брой стъпки."""
    return compiled.invoke(
        {"messages": [HumanMessage(content=task)]},
        config={"recursion_limit": recursion_limit},
    )


class TestHappyPath:
    def test_full_sdlc_flow(self, scripted_graph):
        compiled, supervisor, workers = scripted_graph(
            ["analyst", "developer", "qa", "FINISH"]
        )

        state = run(compiled)

        # Всеки работник е работил точно веднъж, в SDLC ред
        assert [len(w.invocations) for w in workers.values()] == [1, 1, 1]

        # Историята: задача + по един подписан отговор от всеки агент
        names = [m.name for m in state["messages"]]
        assert names == [None, "analyst", "developer", "qa"]

        # Финалното решение е FINISH (routing-ът е стигнал END, не е забил)
        assert state["next"] == "FINISH"

        # Супервайзорът е викан 4 пъти и вижда РАСТЯЩАТА история:
        # (system + задача), после +1 съобщение след всеки работник
        assert [len(call) for call in supervisor.calls] == [2, 3, 4, 5]

    def test_workers_see_previous_workers_output(self, scripted_graph):
        compiled, _, workers = scripted_graph(
            ["analyst", "developer", "qa", "FINISH"],
            analyst_outputs=["СПЕЦИФИКАЦИЯ-МАРКЕР"],
            developer_outputs=["КОД-МАРКЕР"],
        )

        run(compiled)

        # Developer вижда спецификацията на Analyst...
        dev_history = " ".join(str(m.content) for m in workers["developer"].invocations[0])
        assert "СПЕЦИФИКАЦИЯ-МАРКЕР" in dev_history

        # ...а QA вижда и спецификацията, и кода
        qa_history = " ".join(str(m.content) for m in workers["qa"].invocations[0])
        assert "СПЕЦИФИКАЦИЯ-МАРКЕР" in qa_history and "КОД-МАРКЕР" in qa_history

    def test_worker_messages_are_named_human_messages(self, scripted_graph):
        # Контрактът от гочата с thinking блоковете: резултатите на
        # работниците се реинжектират като HumanMessage с name=агента.
        compiled, _, _ = scripted_graph(["analyst", "FINISH"])
        state = run(compiled)

        analyst_msg = state["messages"][1]
        assert isinstance(analyst_msg, HumanMessage)
        assert analyst_msg.name == "analyst"


class TestReworkLoop:
    def test_needs_work_sends_task_back_to_developer(self, scripted_graph):
        compiled, _, workers = scripted_graph(
            ["analyst", "developer", "qa", "developer", "qa", "FINISH"],
            developer_outputs=["КОД v1 (с бъг)", "КОД v2 (поправен)"],
            qa_outputs=["Статус: NEEDS_WORK - липсва граничен случай", "Статус: APPROVED"],
        )

        state = run(compiled)

        # Developer и QA са работили по 2 пъти; Analyst - само веднъж
        assert len(workers["analyst"].invocations) == 1
        assert len(workers["developer"].invocations) == 2
        assert len(workers["qa"].invocations) == 2

        # Историята показва пълния цикъл на поправката
        names = [m.name for m in state["messages"]]
        assert names == [None, "analyst", "developer", "qa", "developer", "qa"]

        # Вторият пас на Developer вижда забележките на QA
        second_dev_history = " ".join(
            str(m.content) for m in workers["developer"].invocations[1]
        )
        assert "NEEDS_WORK" in second_dev_history

        # Финалният QA доклад е одобрението
        assert "APPROVED" in state["messages"][-1].content


class TestEarlyFinish:
    def test_non_software_task_finishes_immediately(self, scripted_graph):
        compiled, supervisor, workers = scripted_graph(["FINISH"])

        state = run(compiled, task="Каква е прогнозата за времето утре?")

        # Никой работник не е пипван; историята е само задачата
        assert all(len(w.invocations) == 0 for w in workers.values())
        assert len(state["messages"]) == 1
        assert len(supervisor.calls) == 1


class TestRecursionGuard:
    def test_looping_supervisor_hits_recursion_limit(self, scripted_graph):
        # Супервайзор, който ВИНАГИ праща analyst - без защитата графът
        # би вървял вечно (и би горил токъни). recursion_limit го спира.
        compiled, _, _ = scripted_graph(["analyst"])

        with pytest.raises(GraphRecursionError):
            run(compiled, recursion_limit=10)


class TestStreamingContract:
    def test_updates_stream_matches_main_py_consumption(self, scripted_graph):
        # main.py и app.py консумират точно този формат: (namespace, step)
        # при stream_mode="updates", subgraphs=True. Пазим го от регресии.
        compiled, _, _ = scripted_graph(["analyst", "developer", "qa", "FINISH"])

        route = []
        worker_events = []
        for namespace, step in compiled.stream(
            {"messages": [HumanMessage(content=TASK)]},
            config={"recursion_limit": 25},
            stream_mode="updates",
            subgraphs=True,
        ):
            assert namespace == ()  # дубльорите нямат вътрешни подграфи
            for node_name, update in step.items():
                if node_name == "supervisor":
                    # Контрактът към UI-то: всяко решение носи next + reason
                    assert "next" in update and "reason" in update
                    route.append(update["next"])
                else:
                    worker_events.append(node_name)
                    assert update["messages"][-1].name == node_name

        assert route == ["analyst", "developer", "qa", "FINISH"]
        assert worker_events == ["analyst", "developer", "qa"]


class TestSupervisorLanguage:
    def test_bulgarian_prompt_by_default(self, scripted_graph):
        compiled, supervisor, _ = scripted_graph(["FINISH"])
        run(compiled)
        assert supervisor.calls[0][0]["content"] == SUPERVISOR_PROMPTS["bg"]

    def test_english_prompt_when_app_lang_is_en(self, scripted_graph, monkeypatch):
        monkeypatch.setenv("APP_LANG", "en")
        compiled, supervisor, _ = scripted_graph(["FINISH"])
        run(compiled, task="Implement ticket DEV-101.")
        assert supervisor.calls[0][0]["content"] == SUPERVISOR_PROMPTS["en"]
