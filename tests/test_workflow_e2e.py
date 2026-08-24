"""
E2E тестове на ЦЕЛИЯ Multi-Bot workflow (реалният граф от build_graph).

Тук се изпълнява истинският LangGraph state machine - възли, DoD
проверки, детерминистичен routing, rework цикъл - а LLM-зависимите
части (triage, QA присъди, работници) са скриптирани дубльори (виж
conftest.py). Покрити сценарии:

  1. Happy path:      triage -> analyst -> developer -> qa -> APPROVED
  2. Rework цикълът:  QAVerdict NEEDS_WORK -> developer -> qa -> APPROVED
  3. Rework ЛИМИТЪТ:  изчерпан брой поправки -> ESCALATED (не вечен цикъл)
  4. Definition of Done: непокрит DoD -> един повторен опит -> ескалация
  5. Гъвкав вход:     triage може да влезе и направо при developer/qa
  6. Незабавен FINISH за несофтуерна задача (final_status=NO_ACTION)
  7. Streaming контрактът, който main.py/app.py консумират
  8. Езикът на triage промпта и детерминистичните reasons (bg/en)
"""

import pytest
from langchain_core.messages import HumanMessage
from langgraph.errors import GraphRecursionError

from src.graph import SUPERVISOR_PROMPTS
from tests.conftest import DEFAULT_DEV_OUTPUT, verdict

TASK = "Имплементирай тикет DEV-101 и се увери, че кодът е качествен."


def run(harness, task=TASK, recursion_limit=25):
    """Изпълнява графа както main.py: една задача, ограничен брой стъпки."""
    return harness.graph.invoke(
        {"messages": [HumanMessage(content=task)]},
        config={"recursion_limit": recursion_limit},
    )


class TestHappyPath:
    def test_full_sdlc_flow(self, scripted_graph):
        h = scripted_graph()

        state = run(h)

        # Всеки работник е работил точно веднъж, в SDLC ред
        assert [len(w.invocations) for w in h.workers.values()] == [1, 1, 1]

        # Историята: задача + по един подписан отговор от всеки агент
        names = [m.name for m in state["messages"]]
        assert names == [None, "analyst", "developer", "qa"]

        # Финалният статус идва от СТРУКТУРИРАНАТА присъда, не от текст
        assert state["final_status"] == "APPROVED"
        assert state["qa_verdict"]["status"] == "APPROVED"
        assert state["next"] == "FINISH"

        # Типизираните артефакти на фазите са попълнени
        assert state["spec"].startswith("Спецификация")
        assert "def validate_email" in state["code"]

        # Супервайзорът е викан точно ВЕДНЪЖ - само triage, останалото
        # е детерминистичен код (improvement.md §2.1)
        assert len(h.supervisor.calls) == 1

    def test_workers_see_previous_workers_output(self, scripted_graph):
        dev_output = DEFAULT_DEV_OUTPUT.replace("Ето кода:", "КОД-МАРКЕР")
        h = scripted_graph(
            analyst_outputs=["СПЕЦИФИКАЦИЯ-МАРКЕР"],
            developer_outputs=[dev_output],
        )

        run(h)

        # Developer вижда спецификацията на Analyst...
        dev_history = " ".join(str(m.content) for m in h.workers["developer"].invocations[0])
        assert "СПЕЦИФИКАЦИЯ-МАРКЕР" in dev_history

        # ...а QA вижда и спецификацията, и кода
        qa_history = " ".join(str(m.content) for m in h.workers["qa"].invocations[0])
        assert "СПЕЦИФИКАЦИЯ-МАРКЕР" in qa_history and "КОД-МАРКЕР" in qa_history

    def test_worker_messages_are_named_human_messages(self, scripted_graph):
        # Контрактът от гочата с thinking блоковете: резултатите на
        # работниците се реинжектират като HumanMessage с name=агента.
        h = scripted_graph()
        state = run(h)

        analyst_msg = state["messages"][1]
        assert isinstance(analyst_msg, HumanMessage)
        assert analyst_msg.name == "analyst"

    def test_verdict_llm_receives_qa_report(self, scripted_graph):
        # Присъдата се извлича от ДОКЛАДА на QA агента - проверяваме,
        # че екстракторът получава точно него.
        h = scripted_graph(qa_outputs=["ДОКЛАД-МАРКЕР: всичко е наред"])
        run(h)
        assert "ДОКЛАД-МАРКЕР" in str(h.verdicts.calls[0])


class TestReworkLoop:
    def test_needs_work_sends_task_back_to_developer(self, scripted_graph):
        h = scripted_graph(
            verdicts=[verdict("NEEDS_WORK", ["липсва граничен случай"]), verdict("APPROVED")],
            developer_outputs=[DEFAULT_DEV_OUTPUT, DEFAULT_DEV_OUTPUT],
            qa_outputs=["Доклад: липсва граничен случай.", "Доклад: всичко е покрито."],
        )

        state = run(h)

        # Developer и QA са работили по 2 пъти; Analyst - само веднъж
        assert len(h.workers["analyst"].invocations) == 1
        assert len(h.workers["developer"].invocations) == 2
        assert len(h.workers["qa"].invocations) == 2

        # Историята показва пълния цикъл на поправката
        names = [m.name for m in state["messages"]]
        assert names == [None, "analyst", "developer", "qa", "developer", "qa"]

        # Вторият пас на Developer вижда доклада на QA със забележките
        second_dev_history = " ".join(
            str(m.content) for m in h.workers["developer"].invocations[1]
        )
        assert "липсва граничен случай" in second_dev_history

        assert state["rework_count"] == 1
        assert state["final_status"] == "APPROVED"

    def test_rework_limit_escalates_instead_of_looping(self, scripted_graph, monkeypatch):
        # QA НИКОГА не одобрява -> след лимита системата ескалира към
        # човек, вместо да гори токъни до recursion_limit (§2.4).
        monkeypatch.setenv("MAX_REWORK", "1")
        h = scripted_graph(verdicts=[verdict("NEEDS_WORK", ["баг"])])

        state = run(h)

        # dev(1) -> qa NW (rework 1, в лимита) -> dev(2) -> qa NW (2 > 1) -> ESCALATED
        assert len(h.workers["developer"].invocations) == 2
        assert state["final_status"] == "ESCALATED"
        assert state["rework_count"] == 2

    def test_invalid_max_rework_falls_back_to_default(self, scripted_graph, monkeypatch):
        # Счупена конфигурация не бива да чупи run-а - пада към default
        monkeypatch.setenv("MAX_REWORK", "не-число")
        h = scripted_graph()
        state = run(h)
        assert state["final_status"] == "APPROVED"


class TestDefinitionOfDone:
    def test_empty_spec_gets_one_retry(self, scripted_graph):
        # Първи опит: празна спецификация -> DoD пропуск -> повторен опит
        h = scripted_graph(analyst_outputs=["", "Спецификация: готова."])

        state = run(h)

        assert len(h.workers["analyst"].invocations) == 2
        # Повторният опит вижда конкретното указание какво липсва
        retry_history = " ".join(str(m.content) for m in h.workers["analyst"].invocations[1])
        assert "Definition of Done" in retry_history
        assert state["final_status"] == "APPROVED"

    def test_persistently_empty_spec_escalates(self, scripted_graph):
        h = scripted_graph(analyst_outputs=[""])

        state = run(h)

        # Един редовен опит + един повторен - после ескалация, не цикъл
        assert len(h.workers["analyst"].invocations) == 2
        assert state["final_status"] == "ESCALATED"

    def test_missing_code_block_gets_one_retry(self, scripted_graph):
        h = scripted_graph(
            developer_outputs=["Ще напиша кода по-късно.", DEFAULT_DEV_OUTPUT]
        )

        state = run(h)

        assert len(h.workers["developer"].invocations) == 2
        assert state["final_status"] == "APPROVED"
        assert "def validate_email" in state["code"]

    def test_syntax_error_gets_one_retry(self, scripted_graph):
        broken = "```python\ndef f(:\n```"
        h = scripted_graph(developer_outputs=[broken, DEFAULT_DEV_OUTPUT])

        state = run(h)

        assert len(h.workers["developer"].invocations) == 2
        # Повторният опит вижда каква е синтактичната грешка
        retry_history = " ".join(str(m.content) for m in h.workers["developer"].invocations[1])
        assert "Definition of Done" in retry_history
        assert state["final_status"] == "APPROVED"

    def test_persistently_broken_code_escalates(self, scripted_graph):
        h = scripted_graph(developer_outputs=["```python\ndef f(:\n```"])

        state = run(h)

        assert len(h.workers["developer"].invocations) == 2
        assert state["final_status"] == "ESCALATED"


class TestTriage:
    def test_non_software_task_finishes_immediately(self, scripted_graph):
        h = scripted_graph(decisions=["FINISH"])

        state = run(h, task="Каква е прогнозата за времето утре?")

        # Никой работник не е пипван; историята е само задачата
        assert all(len(w.invocations) == 0 for w in h.workers.values())
        assert len(state["messages"]) == 1
        assert state["final_status"] == "NO_ACTION"

    def test_entry_at_developer_skips_analyst(self, scripted_graph):
        # Задача с готова спецификация: triage влиза направо при developer
        h = scripted_graph(decisions=["developer"])

        state = run(h, task="Спецификацията е готова: напиши validate_email.")

        assert len(h.workers["analyst"].invocations) == 0
        assert len(h.workers["developer"].invocations) == 1
        assert state["final_status"] == "APPROVED"

    def test_entry_at_qa_only_reviews(self, scripted_graph):
        h = scripted_graph(decisions=["qa"])

        state = run(h, task="Провери този код: ...")

        assert len(h.workers["analyst"].invocations) == 0
        assert len(h.workers["developer"].invocations) == 0
        assert len(h.workers["qa"].invocations) == 1
        assert state["final_status"] == "APPROVED"


class TestRecursionGuard:
    def test_recursion_limit_is_still_the_emergency_brake(self, scripted_graph):
        # Бизнес лимитите (rework, DoD) спират циклите ЕЛЕГАНТНО, но
        # recursion_limit остава аварийната спирачка на самия LangGraph.
        h = scripted_graph()
        with pytest.raises(GraphRecursionError):
            run(h, recursion_limit=2)


class TestStreamingContract:
    def test_updates_stream_matches_main_py_consumption(self, scripted_graph):
        # main.py и app.py консумират точно този формат: (namespace, step)
        # при stream_mode="updates", subgraphs=True. Пазим го от регресии.
        h = scripted_graph()

        route = []
        for namespace, step in h.graph.stream(
            {"messages": [HumanMessage(content=TASK)]},
            config={"recursion_limit": 25},
            stream_mode="updates",
            subgraphs=True,
        ):
            assert namespace == ()  # дубльорите нямат вътрешни подграфи
            for node_name, update in step.items():
                # Контрактът към UI-то: ВСЕКИ възел носи next + reason
                assert "next" in update and "reason" in update
                route.append(update["next"])
                if node_name != "supervisor":
                    assert update["messages"][-1].name == node_name

        assert route == ["analyst", "developer", "qa", "FINISH"]


class TestLanguage:
    def test_bulgarian_prompt_and_reasons_by_default(self, scripted_graph):
        h = scripted_graph()
        state = run(h)
        assert h.supervisor.calls[0][0]["content"] == SUPERVISOR_PROMPTS["bg"]
        assert "задачата е завършена" in state["reason"]

    def test_english_prompt_and_reasons(self, scripted_graph, monkeypatch):
        monkeypatch.setenv("APP_LANG", "en")
        h = scripted_graph()
        state = run(h, task="Implement ticket DEV-101.")
        assert h.supervisor.calls[0][0]["content"] == SUPERVISOR_PROMPTS["en"]
        assert "the task is complete" in state["reason"]
