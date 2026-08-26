"""
Unit тестове за src/plans.py - схемите на плановете, рендерът до markdown
с чекбоксове, прогресът (JSON е източник на истина) и инструментите
update_plan_step / update_test_case през ToolRuntime[RunCtx].
"""

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from src import plans
from src.plans import (
    AcceptanceCriterion,
    ImplementationPlan,
    PlanStep,
    add_rework,
    apply_case_update,
    apply_step_update,
    counts,
    make_plan_tools,
    new_plan_doc,
    new_test_plan_doc,
    record_phase_end,
    render_implementation_plan,
    render_test_plan,
    render_traceability,
    reset_for_rerun,
    validate_case_refs,
)
from src.run_context import RunCtx
from src.run_tracker import RunTracker
from tests.conftest import sample_plan, sample_test_plan
from tests.test_agents_integration import ScriptedToolCallingModel, tool_call


class TestSchemas:
    def test_status_literals_are_enforced(self):
        with pytest.raises(ValidationError):
            PlanStep(id="S1", title="x", status="почти")
        with pytest.raises(ValidationError):
            plans.TestCase(id="T1", title="x", criterion_ref="AC-1", method="vibes", expected="e")

    def test_defaults(self):
        step = PlanStep(id="S1", title="x")
        assert step.status == "todo" and step.files == [] and step.note == ""
        assert ImplementationPlan(summary="s").steps == []
        assert plans.TestPlan(summary="s").cases == []


class TestDocsAndProgress:
    def test_new_doc_has_history(self):
        doc = new_plan_doc(sample_plan())
        assert doc["history"] == [] and [s["id"] for s in doc["steps"]] == ["S1", "S2"]

    def test_counts(self):
        doc = new_plan_doc(sample_plan(steps=3))
        doc = apply_step_update(doc, "s1", "done")  # ID-тата не са case sensitive
        assert counts(doc) == {"todo": 2, "in_progress": 0, "done": 1, "blocked": 0}
        tdoc = new_test_plan_doc(sample_test_plan())
        assert counts(tdoc, "test") == {"planned": 2, "running": 0, "passed": 0, "failed": 0}

    def test_apply_step_update_is_pure_and_idempotent(self):
        doc = new_plan_doc(sample_plan())
        once = apply_step_update(doc, "S1", "done", "готово")
        twice = apply_step_update(once, "S1", "done", "готово")
        assert once == twice
        assert doc["steps"][0]["status"] == "todo"  # оригиналът не е мутиран
        assert once["steps"][0]["note"] == "готово"

    def test_apply_step_update_errors(self):
        doc = new_plan_doc(sample_plan())
        with pytest.raises(KeyError):
            apply_step_update(doc, "S9", "done")
        with pytest.raises(ValueError):
            apply_step_update(doc, "S1", "finished")

    def test_apply_case_update(self):
        doc = new_test_plan_doc(sample_test_plan())
        doc = apply_case_update(doc, "T2", "failed", "грешен резултат")
        assert doc["cases"][1]["status"] == "failed"
        with pytest.raises(KeyError):
            apply_case_update(doc, "T9", "passed")
        with pytest.raises(ValueError):
            apply_case_update(doc, "T1", "ok")

    def test_record_phase_end_replaces_same_key(self):
        doc = new_plan_doc(sample_plan())
        doc = record_phase_end(doc, node="developer", attempt=1, rework=0)
        doc = apply_step_update(doc, "S1", "done")
        doc = record_phase_end(doc, node="developer", attempt=1, rework=0)  # resume: същият ключ
        entries = [e for e in doc["history"] if e["kind"] == "phase_end"]
        assert len(entries) == 1 and entries[0]["counts"]["done"] == 1

    def test_add_rework_and_reset_for_rerun(self):
        doc = add_rework(new_plan_doc(sample_plan()), 1, ["баг 1", "баг 2"])
        assert doc["history"] == [{"kind": "rework", "n": 1, "issues": ["баг 1", "баг 2"]}]

        tdoc = apply_case_update(new_test_plan_doc(sample_test_plan()), "T1", "failed", "лошо")
        tdoc = reset_for_rerun(tdoc, 2)
        assert all(c["status"] == "planned" for c in tdoc["cases"])
        assert tdoc["cases"][0]["note"] == "run 1: failed - лошо"
        assert {"kind": "rerun", "n": 2} in tdoc["history"]

    def test_validate_case_refs_flags_unknown(self):
        pdoc = new_plan_doc(sample_plan(criteria=1))
        tdoc = new_test_plan_doc(sample_test_plan(cases=2))  # T2 сочи AC-2, който не съществува
        checked = validate_case_refs(tdoc, pdoc)
        assert [c["criterion_ref"] for c in checked["cases"]] == ["AC-1", "?"]
        assert validate_case_refs(tdoc, None) == tdoc


class TestRendering:
    def test_implementation_plan_markdown(self):
        doc = apply_step_update(new_plan_doc(sample_plan()), "S1", "in_progress")
        doc = add_rework(record_phase_end(doc, node="developer", attempt=1, rework=0), 1, ["баг"])
        md = render_implementation_plan(doc, "DEV-101")
        assert md.startswith("# План за имплементация · DEV-101")
        assert "- **AC-1** — критерий 1" in md
        assert "- [~] **S1** стъпка 1" in md and "- [ ] **S2**" in md
        assert "`validators.py`" in md and "покрива: AC-1" in md
        assert "## Прогрес в края на фаза `developer`" in md and "## Rework 1" in md and "- баг" in md

    def test_test_plan_markdown(self):
        doc = apply_case_update(new_test_plan_doc(sample_test_plan()), "T1", "passed")
        md = render_test_plan(doc)
        assert md.startswith("# Тест-план\n")
        assert "- [x] **T1** тест 1" in md and "проверява: AC-1" in md and "метод: manual_review" in md

    def test_traceability_matrix(self):
        pdoc = new_plan_doc(sample_plan(steps=2, criteria=2))
        tdoc = new_test_plan_doc(sample_test_plan(cases=3))
        tdoc = apply_case_update(tdoc, "T1", "passed")
        tdoc = apply_case_update(tdoc, "T2", "failed")
        tdoc = validate_case_refs(tdoc, pdoc)
        md = render_traceability(pdoc, tdoc)
        assert "| AC-1 | критерий 1 | S1 (todo) | T1 (passed) | ✅ покрит |" in md
        assert "| AC-2 | критерий 2 | S2 (todo) | T2 (failed) | ❌ провален |" in md
        assert "непознат критерий" in md and "T3" in md

    def test_untested_criterion(self):
        md = render_traceability(new_plan_doc(sample_plan(criteria=1)), None)
        assert "⚠️ непроверен" in md

    def test_english_rendering(self, monkeypatch):
        monkeypatch.setenv("APP_LANG", "en")
        assert render_implementation_plan(new_plan_doc(sample_plan())).startswith("# Implementation plan")
        assert "# Test plan" in render_test_plan(new_test_plan_doc(sample_test_plan()))
        assert "Traceability" in render_traceability({}, {})

    def test_md_texts_have_same_keys_in_both_languages(self):
        assert set(plans._MD["bg"]) == set(plans._MD["en"])
        assert set(plans.DEV_PLAN_PROMPTS) == set(plans.QA_PLAN_PROMPTS) == {"bg", "en"}


class TestDiskRoundTrip:
    def test_save_and_load(self, tmp_path):
        tr = RunTracker(tmp_path)
        plans.save_plan(tr, new_plan_doc(sample_plan()), "DEV-101")
        assert plans.load_plan(tr)["steps"][0]["id"] == "S1"
        assert "DEV-101" in tr.read_text(plans.PLAN_MD)
        plans.save_test_plan(tr, new_test_plan_doc(sample_test_plan()))
        assert plans.load_test_plan(tr)["cases"][0]["id"] == "T1"
        assert plans.load_plan(RunTracker(tmp_path / "empty")) is None


class TestPlanTools:
    """Инструментите през РЕАЛЕН create_agent + ToolRuntime[RunCtx] (без LLM)."""

    def _agent_with(self, script, tool):
        model = ScriptedToolCallingModel(script=list(script), received=[], bound_tools=[])
        return create_agent(model=model, tools=[tool], system_prompt="x", context_schema=RunCtx), model

    def _ctx(self, tmp_path):
        tr = RunTracker.start(tmp_path, "r1", mode="demo", ticket_key="DEV-101", task="t", lang="bg")
        plans.save_plan(tr, new_plan_doc(sample_plan()), "DEV-101")
        plans.save_test_plan(tr, new_test_plan_doc(sample_test_plan()), "DEV-101")
        return RunCtx(run_id="r1", run_dir=str(tr.run_dir), mode="demo"), tr

    def test_runtime_parameter_is_hidden_from_the_model(self):
        update_plan_step, update_test_case = make_plan_tools()
        assert set(update_plan_step.args) == {"step_id", "status", "note"}
        assert set(update_test_case.args) == {"case_id", "status", "note"}

    def test_update_plan_step_writes_json_and_markdown(self, tmp_path):
        ctx, tr = self._ctx(tmp_path)
        update_plan_step, _ = make_plan_tools()
        agent, _ = self._agent_with(
            [tool_call("update_plan_step", {"step_id": "S1", "status": "done", "note": "готово"}),
             AIMessage(content="край")],
            update_plan_step,
        )
        result = agent.invoke({"messages": [HumanMessage(content="работи")]}, context=ctx)
        tool_msg = [m for m in result["messages"] if isinstance(m, ToolMessage)][0]
        assert "S1 -> done" in tool_msg.content
        assert plans.load_plan(tr)["steps"][0] == {
            "id": "S1", "title": "стъпка 1", "files": ["validators.py"],
            "acceptance_criteria_refs": ["AC-1"], "status": "done", "note": "готово",
        }
        assert "- [x] **S1**" in tr.read_text(plans.PLAN_MD)

    def test_update_test_case(self, tmp_path):
        ctx, tr = self._ctx(tmp_path)
        _, update_test_case = make_plan_tools()
        agent, _ = self._agent_with(
            [tool_call("update_test_case", {"case_id": "t2", "status": "failed", "note": "грешка"}),
             AIMessage(content="край")],
            update_test_case,
        )
        agent.invoke({"messages": [HumanMessage(content="провери")]}, context=ctx)
        assert plans.load_test_plan(tr)["cases"][1]["status"] == "failed"

    def test_tool_errors_are_text_not_exceptions(self, tmp_path):
        ctx, _ = self._ctx(tmp_path)
        update_plan_step, _ = make_plan_tools()
        unknown = tool_call("update_plan_step", {"step_id": "S9", "status": "done", "note": ""}, "c1")
        bad_status = tool_call("update_plan_step", {"step_id": "S1", "status": "finished", "note": ""}, "c2")
        agent, _ = self._agent_with([unknown, bad_status, AIMessage(content="край")], update_plan_step)
        result = agent.invoke({"messages": [HumanMessage(content="x")]}, context=ctx)
        texts = [m.content for m in result["messages"] if isinstance(m, ToolMessage)]
        assert "Непозната стъпка 'S9'" in texts[0] and "S1, S2" in texts[0]
        assert "Невалиден статус 'finished'" in texts[1]

    def test_missing_plan_is_reported(self, tmp_path):
        update_plan_step, _ = make_plan_tools()
        agent, _ = self._agent_with(
            [tool_call("update_plan_step", {"step_id": "S1", "status": "done", "note": ""}),
             AIMessage(content="край")],
            update_plan_step,
        )
        ctx = RunCtx(run_id="r", run_dir=str(tmp_path / "nowhere"), mode="demo")
        result = agent.invoke({"messages": [HumanMessage(content="x")]}, context=ctx)
        tool_msg = [m for m in result["messages"] if isinstance(m, ToolMessage)][0]
        assert "Няма план" in tool_msg.content


def test_acceptance_criterion_model():
    ac = AcceptanceCriterion(id="AC-1", text="t")
    assert ac.model_dump() == {"id": "AC-1", "text": "t"}
