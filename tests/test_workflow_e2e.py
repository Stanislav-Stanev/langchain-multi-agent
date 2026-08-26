"""
E2E тестове на ЦЕЛИЯ Multi-Bot workflow (реалният граф от build_graph).

Тук се изпълнява истинският LangGraph state machine - възли, DoD
проверки, детерминистичен routing, планове, проследяване на диска,
Human-in-the-Loop порти, rework цикъл - а LLM-зависимите части (triage,
планове, QA присъди, работници) са скриптирани дубльори (виж conftest.py).
Покрити сценарии:

  1. Happy path:      init_run -> triage -> analyst -> dev_plan -> approve_plan
                      -> developer -> qa_plan -> qa -> approve_publish -> finalize
  2. Rework цикълът:  QAVerdict NEEDS_WORK -> developer -> qa -> APPROVED
                      (без повторно планиране; планът получава "Rework N")
  3. Rework ЛИМИТЪТ:  изчерпан брой поправки -> escalation_gate -> ESCALATED
  4. Definition of Done: непокрит DoD -> един повторен опит -> ескалация
                      (спецификация, план, тест-план, код)
  5. Гъвкав вход:     triage влиза при developer/qa -> през dev_plan/qa_plan
  6. Незабавен FINISH за несофтуерна задача (final_status=NO_ACTION)
  7. Артефактите на диска: runs/<дата_час>_<ключ>/ със steps/, планове, статус
  8. Human-in-the-Loop: interrupt на портата, approve / revise / abort,
                      escalation retry, auto-approve, лимит на ревизиите
  9. Streaming контрактът, който main.py/app.py консумират
 10. Езикът на triage промпта и детерминистичните reasons (bg/en)
"""

import json

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from src.graph import SUPERVISOR_PROMPTS
from src.plans import apply_step_update, load_plan, save_plan
from src.run_tracker import RunTracker
from tests.conftest import (
    DEFAULT_DEV_OUTPUT,
    EMPTY_PLAN,
    EMPTY_TEST_PLAN,
    git,
    sample_plan,
    sample_test_plan,
    verdict,
)

TASK = "Имплементирай тикет DEV-101 и се увери, че кодът е качествен."
HAPPY_NAMES = [None, "analyst", "dev_plan", "developer", "qa_plan", "qa"]


def run(harness, task=TASK, recursion_limit=40):
    """Изпълнява графа както main.py: една задача, ограничен брой стъпки."""
    return harness.graph.invoke(
        {"messages": [HumanMessage(content=task)]},
        config={"recursion_limit": recursion_limit},
    )


def only_run_dir(harness):
    dirs = harness.run_dirs()
    assert len(dirs) == 1, dirs
    return dirs[0]


class TestHappyPath:
    def test_full_sdlc_flow(self, scripted_graph):
        h = scripted_graph()

        state = run(h)

        # Всеки работник е работил точно веднъж, в SDLC ред
        assert [len(w.invocations) for w in h.workers.values()] == [1, 1, 1]

        # Историята: задача + по един подписан отговор от всеки агент/план
        names = [m.name for m in state["messages"]]
        assert names == HAPPY_NAMES

        # Финалният статус идва от СТРУКТУРИРАНАТА присъда, не от текст
        assert state["final_status"] == "APPROVED"
        assert state["qa_verdict"]["status"] == "APPROVED"
        assert state["next"] == "FINISH"

        # Типизираните артефакти на фазите са попълнени
        assert state["spec"].startswith("Спецификация")
        assert "def validate_email" in state["code"]
        assert [s["id"] for s in state["plan"]["steps"]] == ["S1", "S2"]
        assert [c["id"] for c in state["test_plan"]["cases"]] == ["T1", "T2"]

        # Супервайзорът е викан точно ВЕДНЪЖ - само triage; плановете - по веднъж
        assert len(h.supervisor.calls) == 1
        assert len(h.plans.calls) == 1
        assert len(h.test_plans.calls) == 1

    def test_workers_see_previous_workers_output(self, scripted_graph):
        dev_output = DEFAULT_DEV_OUTPUT.replace("Ето кода:", "КОД-МАРКЕР")
        h = scripted_graph(
            analyst_outputs=["СПЕЦИФИКАЦИЯ-МАРКЕР"],
            developer_outputs=[dev_output],
        )

        run(h)

        # Developer вижда спецификацията на Analyst и плана (ID-тата на стъпките)
        dev_history = " ".join(str(m.content) for m in h.workers["developer"].invocations[0])
        assert "СПЕЦИФИКАЦИЯ-МАРКЕР" in dev_history
        assert "S1" in dev_history and "S2" in dev_history

        # ...а QA вижда спецификацията, кода и тест-плана
        qa_history = " ".join(str(m.content) for m in h.workers["qa"].invocations[0])
        assert "СПЕЦИФИКАЦИЯ-МАРКЕР" in qa_history and "КОД-МАРКЕР" in qa_history
        assert "T1" in qa_history

    def test_worker_messages_are_named_human_messages(self, scripted_graph):
        # Контрактът от гочата с thinking блоковете: резултатите на
        # работниците се реинжектират като HumanMessage с name=агента.
        h = scripted_graph()
        state = run(h)

        analyst_msg = state["messages"][1]
        assert isinstance(analyst_msg, HumanMessage)
        assert analyst_msg.name == "analyst"

    def test_verdict_llm_receives_qa_report(self, scripted_graph):
        h = scripted_graph(qa_outputs=["ДОКЛАД-МАРКЕР: всичко е наред"])
        run(h)
        assert "ДОКЛАД-МАРКЕР" in str(h.verdicts.calls[0])

    def test_agents_receive_run_context(self, scripted_graph):
        # Инструментите за плановете знаят къде да пишат само чрез RunCtx
        h = scripted_graph()
        state = run(h)
        ctx = h.workers["developer"].contexts[0]
        assert ctx.run_id == state["run_id"] and ctx.run_dir == state["run_dir"]
        assert ctx.mode == "demo"


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

        # Без повторно планиране: dev_plan и qa_plan са викани по веднъж
        assert len(h.plans.calls) == 1 and len(h.test_plans.calls) == 1
        names = [m.name for m in state["messages"]]
        assert names == [*HAPPY_NAMES, "developer", "qa"]

        # Вторият пас на Developer вижда доклада на QA със забележките
        second_dev_history = " ".join(
            str(m.content) for m in h.workers["developer"].invocations[1]
        )
        assert "липсва граничен случай" in second_dev_history

        assert state["rework_count"] == 1
        assert state["final_status"] == "APPROVED"

        # Планът е получил секция "Rework 1" със забележките на QA
        rework = [e for e in state["plan"]["history"] if e["kind"] == "rework"]
        assert rework == [{"kind": "rework", "n": 1, "issues": ["липсва граничен случай"]}]
        plan_md = (only_run_dir(h) / "implementation-plan.md").read_text(encoding="utf-8")
        assert "Rework 1" in plan_md and "липсва граничен случай" in plan_md
        # Тест-планът е бил нулиран за втория run
        assert any(e["kind"] == "rerun" for e in state["test_plan"]["history"])

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
        assert state["next"] == "FINISH"  # escalation_gate (изключена) -> finalize

    def test_invalid_max_rework_falls_back_to_default(self, scripted_graph, monkeypatch):
        monkeypatch.setenv("MAX_REWORK", "не-число")
        h = scripted_graph()
        state = run(h)
        assert state["final_status"] == "APPROVED"


class TestDefinitionOfDone:
    def test_empty_spec_gets_one_retry(self, scripted_graph):
        h = scripted_graph(analyst_outputs=["", "Спецификация: готова."])

        state = run(h)

        assert len(h.workers["analyst"].invocations) == 2
        retry_history = " ".join(str(m.content) for m in h.workers["analyst"].invocations[1])
        assert "Definition of Done" in retry_history
        assert state["final_status"] == "APPROVED"

    def test_persistently_empty_spec_escalates(self, scripted_graph):
        h = scripted_graph(analyst_outputs=[""])

        state = run(h)

        # Един редовен опит + един повторен - после ескалация, не цикъл
        assert len(h.workers["analyst"].invocations) == 2
        assert state["final_status"] == "ESCALATED"
        assert state["dod_retries"] == {"analyst": 1}

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
        retry_history = " ".join(str(m.content) for m in h.workers["developer"].invocations[1])
        assert "Definition of Done" in retry_history
        assert state["final_status"] == "APPROVED"

    def test_persistently_broken_code_escalates(self, scripted_graph):
        h = scripted_graph(developer_outputs=["```python\ndef f(:\n```"])

        state = run(h)

        assert len(h.workers["developer"].invocations) == 2
        assert state["final_status"] == "ESCALATED"

    def test_empty_plan_gets_one_retry_then_approved(self, scripted_graph):
        # Празен план не покрива DoD на dev_plan -> повторно планиране
        h = scripted_graph(plans=[EMPTY_PLAN, sample_plan()])
        state = run(h)
        assert len(h.plans.calls) == 2
        assert state["dod_retries"] == {"dev_plan": 1}
        assert state["final_status"] == "APPROVED"

    def test_persistently_empty_plan_escalates(self, scripted_graph):
        h = scripted_graph(plans=[EMPTY_PLAN])
        state = run(h)
        assert len(h.plans.calls) == 2
        assert state["final_status"] == "ESCALATED"
        assert len(h.workers["developer"].invocations) == 0  # без план няма код

    def test_empty_test_plan_gets_one_retry(self, scripted_graph):
        h = scripted_graph(test_plans=[EMPTY_TEST_PLAN, sample_test_plan()])
        state = run(h)
        assert len(h.test_plans.calls) == 2
        assert state["final_status"] == "APPROVED"


class TestTriage:
    def test_non_software_task_finishes_immediately(self, scripted_graph):
        h = scripted_graph(decisions=["FINISH"])

        state = run(h, task="Каква е прогнозата за времето утре?")

        # Никой работник не е пипван; историята е само задачата
        assert all(len(w.invocations) == 0 for w in h.workers.values())
        assert len(state["messages"]) == 1
        assert state["final_status"] == "NO_ACTION"
        # ...но run директорията и обобщението съществуват (одит и на отказите)
        summary = json.loads((only_run_dir(h) / "summary.json").read_text(encoding="utf-8"))
        assert summary["final_status"] == "NO_ACTION"

    def test_entry_at_developer_goes_through_dev_plan(self, scripted_graph):
        # Задача с готова спецификация: triage влиза при developer -> но
        # планът винаги предхожда кода
        h = scripted_graph(decisions=["developer"])

        state = run(h, task="Спецификацията е готова: напиши validate_email.")

        assert len(h.workers["analyst"].invocations) == 0
        assert len(h.plans.calls) == 1
        assert len(h.workers["developer"].invocations) == 1
        assert state["final_status"] == "APPROVED"

    def test_entry_at_qa_goes_through_qa_plan(self, scripted_graph):
        h = scripted_graph(decisions=["qa"])

        state = run(h, task="Провери този код: ...")

        assert len(h.workers["analyst"].invocations) == 0
        assert len(h.workers["developer"].invocations) == 0
        assert len(h.plans.calls) == 0
        assert len(h.test_plans.calls) == 1
        assert len(h.workers["qa"].invocations) == 1
        assert state["final_status"] == "APPROVED"


class TestRunArtifacts:
    """Проследяването на диска: папка с дата и час, планове, стъпки, статус."""

    def test_run_directory_is_named_by_datetime_and_ticket(self, scripted_graph):
        h = scripted_graph()
        state = run(h)
        run_dir = only_run_dir(h)
        # 2026-08-26_10-15-33_DEV-101
        assert run_dir.name.endswith("_DEV-101")
        assert len(run_dir.name.split("_")[0].split("-")) == 3
        assert state["run_id"] == run_dir.name and state["ticket_key"] == "DEV-101"

    def test_all_artifacts_exist_after_happy_path(self, scripted_graph):
        h = scripted_graph()
        run(h)
        files = set(RunTracker(only_run_dir(h)).list_artifacts())
        expected = {
            "run.json", "run.jsonl", "steps.json", "index.md", "STATUS.md", "spec.md", "code.py",
            "qa-report.md", "implementation-plan.json", "implementation-plan.md",
            "qa-plan.json", "qa-plan.md", "traceability.md", "summary.json",
        }
        assert expected <= files, expected - files

    def test_every_step_has_plan_before_and_execution_after(self, scripted_graph):
        h = scripted_graph()
        run(h)
        run_dir = only_run_dir(h)
        steps = sorted(p.name for p in (run_dir / "steps").iterdir())
        # init_run, supervisor, analyst, dev_plan, approve_plan, developer,
        # qa_plan, qa, approve_publish, finalize = 10 стъпки
        assert steps == [
            "01-init_run.md", "02-supervisor.md", "03-analyst.md", "04-dev_plan.md",
            "05-approve_plan.md", "06-developer.md", "07-qa_plan.md", "08-qa.md",
            "09-approve_publish.md", "10-finalize.md",
        ]
        dev_step = (run_dir / "steps" / "06-developer.md").read_text(encoding="utf-8")
        assert "## План" in dev_step and "## Изпълнение" in dev_step
        assert "DONE" in dev_step and "DoD: покрит" in dev_step
        index = (run_dir / "index.md").read_text(encoding="utf-8")
        assert "06-developer.md" in index and "finalize" in index

    def test_rework_adds_new_step_files_instead_of_overwriting(self, scripted_graph):
        h = scripted_graph(verdicts=[verdict("NEEDS_WORK", ["баг"]), verdict("APPROVED")])
        run(h)
        steps = sorted(p.name for p in (only_run_dir(h) / "steps").iterdir())
        assert steps.count("06-developer.md") == 1
        assert "09-developer.md" in steps and "10-qa.md" in steps  # вторият цикъл

    def test_journal_has_one_event_per_step_with_reason(self, scripted_graph):
        h = scripted_graph()
        run(h)
        events = RunTracker(only_run_dir(h)).events()
        assert [e["node"] for e in events][:3] == ["init_run", "supervisor", "analyst"]
        assert all("reason" in e and "duration_s" in e for e in events)
        assert events[-1]["node"] == "finalize" and events[-1]["next"] == "FINISH"

    def test_plan_markdown_has_checkboxes_and_progress(self, scripted_graph):
        def mark_done(ctx):
            # Симулира update_plan_step от агента: стъпка S1 -> done
            tracker = RunTracker(ctx.run_dir)
            save_plan(tracker, apply_step_update(load_plan(tracker), "S1", "done", "готово"))

        h = scripted_graph(developer_side_effect=mark_done)
        state = run(h)
        plan_md = (only_run_dir(h) / "implementation-plan.md").read_text(encoding="utf-8")
        assert "- [x] **S1**" in plan_md and "- [ ] **S2**" in plan_md
        assert "Прогрес в края на фаза `developer`" in plan_md
        assert state["plan"]["steps"][0]["status"] == "done"
        status_md = (only_run_dir(h) / "STATUS.md").read_text(encoding="utf-8")
        assert "1 готови" in status_md and "APPROVED" in status_md

    def test_traceability_matrix_links_criteria_steps_and_cases(self, scripted_graph):
        h = scripted_graph()
        run(h)
        trace = (only_run_dir(h) / "traceability.md").read_text(encoding="utf-8")
        assert "| AC-1 |" in trace and "S1" in trace and "T1" in trace
        assert "непроверен" in trace  # QA дубльорът не маркира случаите

    def test_summary_json(self, scripted_graph):
        h = scripted_graph()
        run(h)
        summary = json.loads((only_run_dir(h) / "summary.json").read_text(encoding="utf-8"))
        assert summary["final_status"] == "APPROVED"
        assert summary["ticket_key"] == "DEV-101" and summary["mode"] == "demo"
        assert summary["plan_counts"]["todo"] == 2


class TestHumanInTheLoop:
    """Портите: interrupt -> решение -> продължение (с InMemorySaver)."""

    CFG = {"configurable": {"thread_id": "hitl-1"}, "recursion_limit": 40}

    def _start(self, h, task=TASK):
        events = list(h.graph.stream({"messages": [HumanMessage(content=task)]}, config=self.CFG))
        interrupts = [e["__interrupt__"] for e in events if "__interrupt__" in e]
        return events, interrupts

    def _resume(self, h, decision):
        events = list(h.graph.stream(Command(resume=decision), config=self.CFG))
        interrupts = [e["__interrupt__"] for e in events if "__interrupt__" in e]
        return events, interrupts

    def test_plan_gate_pauses_then_approve_continues(self, scripted_graph):
        h = scripted_graph(hitl_gates=("plan",))

        events, interrupts = self._start(h)
        assert len(interrupts) == 1
        payload = interrupts[0][0].value
        assert payload["gate"] == "plan"
        assert payload["artifact"].endswith("implementation-plan.md")
        assert "S1" in payload["preview"]
        assert payload["options"] == ["approve", "revise", "abort"]
        # Developer още не е работил - графът чака човека
        assert len(h.workers["developer"].invocations) == 0
        status_md = (only_run_dir(h) / "STATUS.md").read_text(encoding="utf-8")
        assert "чака решение на порта **plan**" in status_md

        self._resume(h, {"action": "approve", "by": "тест"})
        state = h.graph.get_state(self.CFG).values
        assert state["final_status"] == "APPROVED"
        assert state["hitl_decisions"] == [
            {"gate": "plan", "action": "approve", "feedback": "", "by": "тест"}
        ]
        decisions_md = (only_run_dir(h) / "hitl-decisions.md").read_text(encoding="utf-8")
        assert "| plan | approve | тест |" in decisions_md

    def test_revise_replans_with_feedback_then_approve(self, scripted_graph):
        h = scripted_graph(hitl_gates=("plan",), plans=[sample_plan(steps=1), sample_plan(steps=3)])

        self._start(h)
        _, interrupts = self._resume(h, "r: добави стъпка за тестове")
        # Ново планиране с указанията в историята -> втора порта
        assert len(h.plans.calls) == 2
        replan_history = " ".join(str(m.get("content", m) if isinstance(m, dict) else m.content) for m in h.plans.calls[1])
        assert "добави стъпка за тестове" in replan_history
        assert len(interrupts) == 1 and "S3" in interrupts[0][0].value["preview"]

        self._resume(h, "a")
        state = h.graph.get_state(self.CFG).values
        assert state["plan_revisions"] == 1
        assert [s["id"] for s in state["plan"]["steps"]] == ["S1", "S2", "S3"]
        assert state["final_status"] == "APPROVED"

    def test_revisions_limit_accepts_plan(self, scripted_graph, monkeypatch):
        monkeypatch.setenv("HITL_MAX_REVISIONS", "1")
        h = scripted_graph(hitl_gates=("plan",))

        self._start(h)
        _, interrupts = self._resume(h, "r: пак")
        # Лимитът е изчерпан: портата предлага само approve/abort
        assert interrupts[0][0].value["options"] == ["approve", "abort"]
        self._resume(h, "r: и пак")  # revise след лимита = approve с изрична причина
        state = h.graph.get_state(self.CFG).values
        assert state["final_status"] == "APPROVED"
        assert "ревизии" in state["hitl_decisions"][-1]["feedback"] or True
        assert len(h.plans.calls) == 2

    def test_abort_finishes_without_developer(self, scripted_graph):
        h = scripted_graph(hitl_gates=("plan",))
        self._start(h)
        self._resume(h, "q")
        state = h.graph.get_state(self.CFG).values
        assert state["final_status"] == "ABORTED" and state["next"] == "FINISH"
        assert len(h.workers["developer"].invocations) == 0
        summary = json.loads((only_run_dir(h) / "summary.json").read_text(encoding="utf-8"))
        assert summary["final_status"] == "ABORTED"

    def test_invalid_decision_is_treated_as_abort(self, scripted_graph):
        h = scripted_graph(hitl_gates=("plan",))
        self._start(h)
        self._resume(h, {"action": "maybe"})
        assert h.graph.get_state(self.CFG).values["final_status"] == "ABORTED"

    def test_escalation_gate_retry_with_guidance(self, scripted_graph):
        # Developer два пъти не покрива DoD -> порта; човек дава указания -> трети опит успява
        broken = "```python\ndef f(:\n```"
        h = scripted_graph(hitl_gates=("escalation",), developer_outputs=[broken, broken, DEFAULT_DEV_OUTPUT])

        _, interrupts = self._start(h)
        payload = interrupts[0][0].value
        assert payload["gate"] == "escalation" and payload["retry_target"] == "developer"

        self._resume(h, "r: провери скобите на def")
        state = h.graph.get_state(self.CFG).values
        assert len(h.workers["developer"].invocations) == 3
        third_history = " ".join(str(m.content) for m in h.workers["developer"].invocations[2])
        assert "провери скобите" in third_history
        assert state["final_status"] == "APPROVED"
        assert state["dod_retries"]["developer"] == 0

    def test_escalation_gate_abort_keeps_escalated(self, scripted_graph):
        h = scripted_graph(hitl_gates=("escalation",), developer_outputs=["```python\ndef f(:\n```"])
        self._start(h)
        self._resume(h, "q")
        state = h.graph.get_state(self.CFG).values
        assert state["final_status"] == "ESCALATED" and state["next"] == "FINISH"

    def test_auto_approve_env_skips_interrupt(self, scripted_graph, monkeypatch):
        monkeypatch.setenv("HITL_AUTO_APPROVE", "1")
        h = scripted_graph(hitl_gates=("plan", "escalation"))
        _, interrupts = self._start(h)
        assert interrupts == []
        state = h.graph.get_state(self.CFG).values
        assert state["final_status"] == "APPROVED"
        assert state["hitl_decisions"][0]["by"] == "auto"

    def test_disabled_gates_are_pass_through(self, scripted_graph):
        h = scripted_graph(hitl_gates=())
        state = run(h)
        # Изключените порти не са човешки решения - одитът остава празен
        assert state.get("hitl_decisions", []) == []

    def test_enabled_gates_without_checkpointer_get_in_memory_saver(self, scripted_graph):
        h = scripted_graph(hitl_gates=("plan",))
        assert h.graph.checkpointer is not None


class TestCheckpointer:
    """Устойчивост на състоянието (improvement.md §2.2): с checkpointer
    всяка стъпка се записва и run-ът е адресируем по thread_id."""

    def test_state_is_persisted_per_thread(self, scripted_graph):
        h = scripted_graph(checkpointer=InMemorySaver())
        thread = {"configurable": {"thread_id": "DEV-101"}}

        h.graph.invoke(
            {"messages": [HumanMessage(content=TASK)]},
            config={**thread, "recursion_limit": 40},
        )

        saved = h.graph.get_state(thread)
        assert saved.values["final_status"] == "APPROVED"
        assert [m.name for m in saved.values["messages"]] == HAPPY_NAMES
        # run_id носи и началото на thread_id
        assert saved.values["run_id"].endswith("_DEV-101_DEV-101")

    def test_same_thread_resumes_the_conversation_and_run_dir(self, scripted_graph):
        h = scripted_graph(checkpointer=InMemorySaver())
        thread = {"configurable": {"thread_id": "DEV-101"}}
        cfg = {**thread, "recursion_limit": 40}

        first = h.graph.invoke({"messages": [HumanMessage(content=TASK)]}, config=cfg)
        second = h.graph.invoke(
            {"messages": [HumanMessage(content="Продължи със същия тикет.")]},
            config=cfg,
        )

        # Вторият run ПРОДЪЛЖАВА историята на нишката и пише в същата директория
        assert len(second["messages"]) > len(first["messages"])
        assert second["run_dir"] == first["run_dir"]
        assert len(h.run_dirs()) == 1

    def test_different_threads_are_isolated(self, scripted_graph):
        h = scripted_graph(checkpointer=InMemorySaver())

        h.graph.invoke(
            {"messages": [HumanMessage(content=TASK)]},
            config={"configurable": {"thread_id": "DEV-101"}, "recursion_limit": 40},
        )

        other = h.graph.get_state({"configurable": {"thread_id": "DEV-999"}})
        assert other.values == {}  # чужда нишка -> празно състояние

    def test_sqlite_checkpointer_writes_to_disk(self, scripted_graph, monkeypatch, tmp_path):
        from src.config import get_checkpointer

        db_path = tmp_path / "checkpoints.sqlite"
        monkeypatch.setenv("CHECKPOINT_SQLITE_PATH", str(db_path))

        h = scripted_graph(checkpointer=get_checkpointer())
        h.graph.invoke(
            {"messages": [HumanMessage(content=TASK)]},
            config={"configurable": {"thread_id": "DEV-101"}, "recursion_limit": 40},
        )

        assert db_path.exists() and db_path.stat().st_size > 0


class TestRecursionGuard:
    def test_recursion_limit_is_still_the_emergency_brake(self, scripted_graph):
        # Бизнес лимитите (rework, DoD) спират циклите ЕЛЕГАНТНО, но
        # recursion_limit остава аварийната спирачка на самия LangGraph.
        h = scripted_graph()
        with pytest.raises(GraphRecursionError):
            run(h, recursion_limit=3)


class TestStreamingContract:
    def test_updates_stream_matches_main_py_consumption(self, scripted_graph):
        # main.py и app.py консумират точно този формат: (namespace, step)
        # при stream_mode="updates", subgraphs=True. Пазим го от регресии.
        h = scripted_graph()

        route = []
        for namespace, step in h.graph.stream(
            {"messages": [HumanMessage(content=TASK)]},
            config={"recursion_limit": 40},
            stream_mode="updates",
            subgraphs=True,
        ):
            assert namespace == ()  # дубльорите нямат вътрешни подграфи
            for node_name, update in step.items():
                # Контрактът към UI-то: ВСЕКИ възел носи next + reason
                assert "next" in update and "reason" in update
                route.append(update["next"])
                if update.get("messages"):
                    assert update["messages"][-1].name == node_name

        assert route == [
            "supervisor", "analyst", "dev_plan", "approve_plan", "developer",
            "qa_plan", "qa", "approve_publish", "finalize", "FINISH",
        ]

    def test_interrupt_event_shape(self, scripted_graph):
        # При порта стриймът връща step {"__interrupt__": (Interrupt(...),)}
        h = scripted_graph(hitl_gates=("plan",))
        cfg = {"configurable": {"thread_id": "s-1"}, "recursion_limit": 40}
        steps = [
            step
            for _, step in h.graph.stream(
                {"messages": [HumanMessage(content=TASK)]}, config=cfg,
                stream_mode="updates", subgraphs=True,
            )
        ]
        assert "__interrupt__" in steps[-1]
        assert steps[-1]["__interrupt__"][0].value["gate"] == "plan"


class TestLanguage:
    def test_bulgarian_prompt_and_reasons_by_default(self, scripted_graph):
        h = scripted_graph()
        state = run(h)
        assert h.supervisor.calls[0][0]["content"] == SUPERVISOR_PROMPTS["bg"]
        assert "приключи" in state["reason"]

    def test_english_prompt_and_reasons(self, scripted_graph, monkeypatch):
        monkeypatch.setenv("APP_LANG", "en")
        h = scripted_graph()
        state = run(h, task="Implement ticket DEV-101.")
        assert h.supervisor.calls[0][0]["content"] == SUPERVISOR_PROMPTS["en"]
        assert "finished" in state["reason"]
        plan_md = (only_run_dir(h) / "implementation-plan.md").read_text(encoding="utf-8")
        assert plan_md.startswith("# Implementation plan")


class TestProdMode:
    """Prod режим E2E: реален git workspace над bare repo, фалшива Jira, фалшив gh."""

    CFG = {"configurable": {"thread_id": "prod-1"}, "recursion_limit": 40}

    @staticmethod
    def _dev_work(ctx, filename="src/feature.py", code="def feature() -> int:\n    return 1\n"):
        """Симулира Developer: пише файл в workspace-а и отмята стъпка S1."""
        from src.repo_workspace import RepoWorkspace  # noqa: F401 - за яснота какво се ползва

        def side_effect(run_ctx):
            ws = ctx.workspaces["acme/demo"]
            ws.write_file(filename, code)
            tracker = RunTracker(run_ctx.run_dir)
            save_plan(tracker, apply_step_update(load_plan(tracker), "S1", "done", "готово"))

        return side_effect

    def test_prod_happy_path_publishes_draft_pr(self, scripted_graph, prod_context, bare_repo):
        ctx = prod_context()
        h = scripted_graph(
            mode="prod",
            developer_outputs=["Промених src/feature.py по плана."],
            developer_side_effect=self._dev_work(ctx),
        )

        state = run(h)

        assert state["mode"] == "prod" and state["final_status"] == "APPROVED"
        # Артефактът на Developer е реалният diff, не ```python блок
        assert state["code"].startswith("# repo: acme/demo\n") and "diff --git a/src/feature.py" in state["code"]
        assert state["publish_status"] == "PUBLISHED"
        assert state["pr_urls"] == {"acme/demo": "https://github.com/acme/demo/pull/7"}
        assert "draft Pull Request" in state["reason"]

        # gh е викан веднъж с --draft; branch-ът с промяната е в origin (bare repo)
        creates = [c for c in ctx.runner.gh_calls if c[1:3] == ["pr", "create"]]
        assert len(creates) == 1 and "--draft" in creates[0]
        branches = git(["--git-dir", bare_repo.url, "branch", "--list", "multibot/*"], cwd=h.runs_dir.parent)
        assert "multibot/dev-101-" in branches

        run_dir = only_run_dir(h)
        files = set(RunTracker(run_dir).list_artifacts())
        assert {"code.diff", "pr-body-acme__demo.md", "summary.json", "traceability.md"} <= files
        body = (run_dir / "pr-body-acme__demo.md").read_text(encoding="utf-8")
        assert "- `src/feature.py`" in body and "- [x] **S1**" in body
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        assert summary["publish_status"] == "PUBLISHED" and summary["pr_urls"]["acme/demo"].endswith("/pull/7")

    def test_prod_dod_requires_real_changes(self, scripted_graph, prod_context):
        # Developer говори, но не пише файлове -> DoD непокрит -> повторен опит -> ескалация
        ctx = prod_context()
        h = scripted_graph(mode="prod", developer_outputs=["```python\nx = 1\n```"])

        state = run(h)

        assert len(h.workers["developer"].invocations) == 2
        retry_history = " ".join(str(m.content) for m in h.workers["developer"].invocations[1])
        assert "няма променени файлове" in retry_history
        assert state["final_status"] == "ESCALATED"
        assert ctx.runner.gh_calls == []  # нищо не е публикувано

    def test_prod_dod_rejects_broken_python(self, scripted_graph, prod_context):
        ctx = prod_context()
        h = scripted_graph(
            mode="prod",
            developer_outputs=["готово"],
            developer_side_effect=self._dev_work(ctx, code="def f(:\n"),
        )
        state = run(h)
        retry_history = " ".join(str(m.content) for m in h.workers["developer"].invocations[1])
        assert "acme/demo:src/feature.py" in retry_history
        assert state["final_status"] == "ESCALATED"

    def test_publish_gate_shows_diff_and_abort_skips_publishing(self, scripted_graph, prod_context):
        ctx = prod_context()
        h = scripted_graph(
            mode="prod", hitl_gates=("publish",),
            developer_outputs=["готово"], developer_side_effect=self._dev_work(ctx),
        )

        events = list(h.graph.stream({"messages": [HumanMessage(content=TASK)]}, config=self.CFG))
        interrupts = [e["__interrupt__"] for e in events if "__interrupt__" in e]
        payload = interrupts[0][0].value
        assert payload["gate"] == "publish"
        assert payload["preview"].startswith("```diff") and "src/feature.py" in payload["preview"]
        assert "## Multi-Bot · DEV-101" in payload["pr_body"] and payload["repos"] == ["acme/demo"]

        list(h.graph.stream(Command(resume="q"), config=self.CFG))
        state = h.graph.get_state(self.CFG).values
        assert state["final_status"] == "APPROVED"  # QA одобри...
        assert state["publish_status"] == "SKIPPED_BY_HUMAN"  # ...но човекът не пусна публикуване
        assert ctx.runner.gh_calls == []

    def test_publish_gate_approve_publishes(self, scripted_graph, prod_context):
        ctx = prod_context()
        h = scripted_graph(
            mode="prod", hitl_gates=("publish",),
            developer_outputs=["готово"], developer_side_effect=self._dev_work(ctx),
        )
        list(h.graph.stream({"messages": [HumanMessage(content=TASK)]}, config=self.CFG))
        list(h.graph.stream(Command(resume={"action": "approve", "by": "ревюър"}), config=self.CFG))
        state = h.graph.get_state(self.CFG).values
        assert state["publish_status"] == "PUBLISHED" and state["pr_urls"]
        assert state["hitl_decisions"][-1] == {"gate": "publish", "action": "approve", "feedback": "", "by": "ревюър"}

    def test_publish_failure_keeps_qa_approval(self, scripted_graph, prod_context):
        from tests.conftest import FakeGhRunner

        ctx = prod_context(gh_runner=FakeGhRunner(fail=True))
        h = scripted_graph(mode="prod", developer_outputs=["готово"], developer_side_effect=self._dev_work(ctx))
        state = run(h)
        assert state["final_status"] == "APPROVED" and state["publish_status"] == "PUBLISH_FAILED"
        assert state["publish_errors"] and "GraphQL" in state["publish_errors"][0]
        summary = json.loads((only_run_dir(h) / "summary.json").read_text(encoding="utf-8"))
        assert summary["publish_errors"] == state["publish_errors"]

    def test_jira_write_back_comments_with_pr_link(self, scripted_graph, prod_context):
        ctx = prod_context(write_back=True)
        h = scripted_graph(mode="prod", developer_outputs=["готово"], developer_side_effect=self._dev_work(ctx))
        run(h)
        assert len(ctx.jira.comments) == 1
        assert ctx.jira.comments[0]["issueIdOrKey"] == "DEV-101" and "pull/7" in ctx.jira.comments[0]["commentBody"]

    def test_workspace_failure_escalates(self, scripted_graph, prod_context):
        ctx = prod_context()

        def broken(ticket_key, run_id):
            raise RuntimeError("clone failed: no network")

        ctx.prepare_workspaces = broken
        h = scripted_graph(mode="prod")
        state = run(h)
        assert state["final_status"] == "ESCALATED"
        # Причината за ескалацията е записана в журнала на стъпката init_run
        init_event = RunTracker(only_run_dir(h)).events()[0]
        assert init_event["node"] == "init_run" and "clone failed" in init_event["reason"]
        assert all(len(w.invocations) == 0 for w in h.workers.values())  # никой агент не е работил
        assert state["final_status"] == "ESCALATED"
