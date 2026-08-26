"""
Unit тестове за src/run_tracker.py и src/step_plans.py - проследяването на
диска: run директория с дата и час, журнал, стъпки (план преди /
изпълнение след), STATUS.md, HITL решения, идемпотентност.
"""

import json
from datetime import datetime

from src import step_plans
from src.run_tracker import NullTracker, RunTracker, extract_ticket_key, make_run_id


class TestTicketKeyAndRunId:
    def test_extracts_first_ticket_key(self):
        assert extract_ticket_key("Имплементирай тикет DEV-101 и AIRD-2045") == "DEV-101"
        assert extract_ticket_key("без тикет") == ""
        assert extract_ticket_key("") == ""

    def test_run_id_starts_with_datetime_then_key(self):
        now = datetime(2026, 8, 26, 9, 58, 33)
        assert make_run_id("DEV-101", "задача", now=now) == "2026-08-26_09-58-33_DEV-101"

    def test_run_id_without_ticket_uses_task_hash(self):
        now = datetime(2026, 8, 26, 9, 58, 33)
        run_id = make_run_id("", "обърни низ", now=now)
        assert run_id.startswith("2026-08-26_09-58-33_task-") and len(run_id.split("task-")[1]) == 8

    def test_run_id_appends_thread_prefix(self):
        now = datetime(2026, 8, 26, 9, 58, 33)
        assert make_run_id("DEV-101", "x", now=now, thread_id="abcdef123456") == (
            "2026-08-26_09-58-33_DEV-101_abcdef12"
        )


class TestRunTrackerFiles:
    def test_start_creates_directory_and_run_json(self, tmp_path):
        tr = RunTracker.start(tmp_path, "r1", mode="demo", ticket_key="DEV-101", task="t", lang="bg")
        assert tr.run_dir == tmp_path / "r1" and tr.run_id == "r1"
        meta = tr.read_json("run.json")
        assert meta["mode"] == "demo" and meta["ticket_key"] == "DEV-101" and "started" in meta

    def test_start_is_idempotent(self, tmp_path):
        tr = RunTracker.start(tmp_path, "r1", mode="demo", ticket_key="", task="t", lang="bg")
        first = tr.read_json("run.json")
        RunTracker.start(tmp_path, "r1", mode="prod", ticket_key="X-1", task="t2", lang="en")
        assert tr.read_json("run.json") == first  # не презаписва метаданните

    def test_from_state(self, tmp_path):
        assert isinstance(RunTracker.from_state({}), NullTracker)
        assert isinstance(RunTracker.from_state({"run_dir": str(tmp_path)}), RunTracker)

    def test_atomic_write_leaves_no_tmp(self, tmp_path):
        tr = RunTracker(tmp_path / "r")
        tr.write_text("a.md", "текст")
        tr.write_json("b.json", {"x": 1})
        assert tr.read_text("a.md") == "текст" and tr.read_json("b.json") == {"x": 1}
        assert tr.list_artifacts() == ["a.md", "b.json"]
        assert tr.read_text("missing.md", "default") == "default"
        assert tr.read_json("missing.json", default=[]) == []


class TestJournal:
    def test_events_append_with_timestamp(self, tmp_path):
        tr = RunTracker(tmp_path)
        tr.event(seq=1, node="init_run", next="supervisor")
        tr.event(seq=2, node="supervisor", next="analyst")
        events = tr.events()
        assert [e["node"] for e in events] == ["init_run", "supervisor"]
        assert all("ts" in e for e in events)

    def test_duplicate_seq_node_is_skipped(self, tmp_path):
        # Resume от checkpoint изпълнява възела повторно - журналът не се дублира
        tr = RunTracker(tmp_path)
        tr.event(seq=1, node="analyst", next="dev_plan")
        tr.event(seq=1, node="analyst", next="dev_plan")
        assert len(tr.events()) == 1
        tr.event(seq=1, node="developer")  # друг възел със същия seq (не се случва, но е различен)
        assert len(tr.events()) == 2


class TestSteps:
    def test_begin_then_end_renders_file_and_index(self, tmp_path):
        tr = RunTracker.start(tmp_path, "r1", mode="demo", ticket_key="DEV-101", task="t", lang="bg")
        tr.begin_step(3, "analyst", "## План\n- Цел: спецификация")
        step_file = tr.run_dir / "steps" / "03-analyst.md"
        text = step_file.read_text(encoding="utf-8")
        assert "IN_PROGRESS" in text and "## План" in text and "още се изпълнява" in text

        tr.end_step(3, "analyst", "## Изпълнение\n- Резултат: ок", status="DONE", next_node="dev_plan", duration_s=1.234)
        text = step_file.read_text(encoding="utf-8")
        assert "DONE" in text and "1.2s" in text and "## Изпълнение" in text
        index = (tr.run_dir / "index.md").read_text(encoding="utf-8")
        assert "| 03 | analyst |" in index and "dev_plan" in index

    def test_begin_step_twice_replaces_record(self, tmp_path):
        tr = RunTracker.start(tmp_path, "r1", mode="demo", ticket_key="", task="t", lang="bg")
        tr.begin_step(1, "init_run", "план 1")
        tr.begin_step(1, "init_run", "план 2")
        assert len(tr.steps()) == 1 and tr.steps()[0]["plan_md"] == "план 2"

    def test_end_without_begin_still_records(self, tmp_path):
        tr = RunTracker.start(tmp_path, "r1", mode="demo", ticket_key="", task="t", lang="bg")
        tr.end_step(2, "qa", "изп", status="DONE", next_node="finalize", duration_s=0.5)
        assert tr.steps()[0]["node"] == "qa" and tr.steps()[0]["started"] is None


class TestArtifactsAndStatus:
    def test_persist_artifacts_demo_and_prod(self, tmp_path):
        tr = RunTracker(tmp_path)
        tr.persist_artifacts({"spec": "спец", "code": "x = 1"}, mode="demo")
        assert tr.read_text("spec.md") == "спец" and tr.read_text("code.py") == "x = 1"
        tr.persist_artifacts({"code": "diff --git"}, mode="prod")
        assert tr.read_text("code.diff") == "diff --git"

    def test_qa_report_includes_verdict_json(self, tmp_path):
        from langchain_core.messages import HumanMessage

        tr = RunTracker(tmp_path)
        tr.persist_artifacts(
            {"qa_verdict": {"status": "APPROVED", "issues": []},
             "messages": [HumanMessage(content="Доклад OK", name="qa")]},
            mode="demo",
        )
        report = tr.read_text("qa-report.md")
        assert "Доклад OK" in report and '"status": "APPROVED"' in report

    def test_status_md_content(self, tmp_path):
        tr = RunTracker.start(tmp_path, "r1", mode="demo", ticket_key="DEV-101", task="t", lang="bg")
        tr.write_status(
            {"phase": "developer", "step_seq": 6, "next": "qa_plan", "rework_count": 1,
             "final_status": "", "pending_gate": None},
            plan_counts={"done": 1, "in_progress": 0, "todo": 1, "blocked": 0},
            test_counts={"passed": 0, "failed": 0, "running": 0, "planned": 2},
        )
        status = tr.read_text("STATUS.md")
        assert "DEV-101" in status and "**developer**" in status
        assert "1/3" in status and "1 готови" in status and "2 чакат" in status

    def test_status_shows_pending_gate(self, tmp_path):
        tr = RunTracker.start(tmp_path, "r1", mode="demo", ticket_key="", task="t", lang="bg")
        tr.write_status({"phase": "approve_plan", "pending_gate": "plan"})
        assert "чака решение на порта **plan**" in tr.read_text("STATUS.md")

    def test_hitl_decisions_rendered(self, tmp_path):
        tr = RunTracker(tmp_path)
        tr.record_hitl({"gate": "plan", "action": "revise", "feedback": "добави | тест", "by": "ivan"})
        assert len(tr.hitl_decisions()) == 1
        md = tr.read_text("hitl-decisions.md")
        assert "| plan | revise | ivan | добави / тест |" in md  # '|' в коментара не чупи таблицата

    def test_summary_and_usage_merge(self, tmp_path):
        tr = RunTracker(tmp_path)
        tr.write_summary({"final_status": "APPROVED"})
        tr.write_usage({"claude": {"input_tokens": 10}}, 0.1234567)
        summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
        assert summary["final_status"] == "APPROVED" and summary["cost_usd"] == 0.123457
        assert summary["usage"]["claude"]["input_tokens"] == 10


class TestNullTracker:
    def test_everything_is_a_noop(self):
        nt = NullTracker()
        nt.begin_step(1, "x", "p")
        nt.end_step(1, "x", "e", status="DONE", next_node=None, duration_s=0)
        nt.event(seq=1, node="x")
        nt.persist_artifacts({"spec": "s"}, mode="demo")
        nt.write_status({})
        nt.record_hitl({})
        nt.write_summary({})
        nt.write_usage({}, 0.0)
        assert nt.steps() == [] and nt.events() == [] and nt.list_artifacts() == []
        assert nt.read_json("x", default=5) == 5 and nt.read_text("x", "d") == "d"
        assert nt.run_dir is None


class TestStepPlans:
    def test_plan_section_uses_node_template_and_state(self):
        from langchain_core.messages import HumanMessage

        state = {"messages": [HumanMessage(content="Имплементирай DEV-101")], "ticket_key": "DEV-101"}
        md = step_plans.plan_section("analyst", state)
        assert "## План" in md and "DEV-101" in md and "Definition of Done" in md

    def test_unknown_node_falls_back_to_name(self):
        assert "mystery" in step_plans.plan_section("mystery", {"messages": []})

    def test_execution_section_reports_dod_failure(self):
        update = {"next": "developer", "dod_retries": {"developer": 1}, "reason": "липсва блок"}
        md = step_plans.execution_section("developer", {}, update)
        assert "непокрит" in md and "липсва блок" in md

    def test_execution_section_success_and_error(self):
        ok = step_plans.execution_section("developer", {}, {"next": "qa_plan", "code": "x", "reason": "готово"})
        assert "DoD: покрит" in ok and "code.py" in ok
        err = step_plans.execution_section("qa", {}, {}, error="boom")
        assert "boom" in err

    def test_english_templates(self, monkeypatch):
        monkeypatch.setenv("APP_LANG", "en")
        assert "## Plan" in step_plans.plan_section("qa", {"messages": []})
        assert step_plans.render_index("r", []).startswith("# Steps of run r")

    def test_all_nodes_have_templates_in_both_languages(self):
        for lang in ("bg", "en"):
            assert set(step_plans.STEP_PLANS[lang]) == set(step_plans.NODE_KIND)
        assert set(step_plans._TXT["bg"]) == set(step_plans._TXT["en"])


class TestConcurrentWrites:
    """LangGraph изпълнява няколко tool извиквания паралелно (нишки) - записите
    в един и същи файл не бива да се блъскат (Windows: PermissionError върху
    общ .tmp файл) и read-modify-write на плана не бива да губи обновления."""

    def test_parallel_writes_to_the_same_file_do_not_collide(self, tmp_path):
        import threading

        tr = RunTracker(tmp_path)
        errors = []

        def writer(i):
            try:
                for _ in range(20):
                    tr.write_json("shared.json", {"writer": i})
            except Exception as exc:  # noqa: BLE001 - тестът събира всичко
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert errors == []
        assert tr.read_json("shared.json")["writer"] in range(8)
        assert not list(tmp_path.glob("*.tmp"))

    def test_locked_serializes_read_modify_write(self, tmp_path):
        import threading

        tr = RunTracker(tmp_path)
        tr.write_json("counter.json", {"n": 0})

        def bump():
            for _ in range(25):
                with tr.locked():
                    tr.write_json("counter.json", {"n": tr.read_json("counter.json")["n"] + 1})

        threads = [threading.Thread(target=bump) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert tr.read_json("counter.json")["n"] == 100


def test_status_task_comes_from_run_json_not_last_message(tmp_path):
    # Регресия от реален run: update-ът на възела носи само новите съобщения,
    # затова STATUS.md показваше QA доклада като "задача".
    from langchain_core.messages import HumanMessage

    tr = RunTracker.start(tmp_path, "r1", mode="demo", ticket_key="DEV-101", task="Имплементирай DEV-101", lang="bg")
    tr.write_status({"phase": "qa", "messages": [HumanMessage(content="QA доклад: APPROVED", name="qa")]})
    status = tr.read_text("STATUS.md")
    assert "Задача: Имплементирай DEV-101" in status and "QA доклад" not in status
