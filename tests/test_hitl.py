"""
Unit тестове за src/hitl.py - конфигурацията на портите, разборът на
решението и gate възелът (без граф: pass-through и auto-approve пътищата;
interrupt пътят се покрива E2E в test_workflow_e2e.py).
"""

import pytest

from src.hitl import (
    GATE_NODES,
    GATES,
    HitlDecision,
    auto_approve,
    default_gates,
    enabled_gates,
    make_gate_node,
    max_revisions,
    parse_decision,
)


class TestGateConfig:
    def test_defaults_by_mode(self):
        assert default_gates("prod") == ("plan", "publish", "escalation")
        assert default_gates("demo") == ("plan", "escalation")
        assert default_gates("other") == default_gates("prod")

    def test_env_unset_uses_default(self, monkeypatch):
        monkeypatch.delenv("HITL_GATES", raising=False)
        assert enabled_gates("prod") == ("plan", "publish", "escalation")

    def test_env_empty_disables_all(self, monkeypatch):
        monkeypatch.setenv("HITL_GATES", "")
        assert enabled_gates("prod") == ()

    def test_env_list(self, monkeypatch):
        monkeypatch.setenv("HITL_GATES", " Plan , publish ")
        assert enabled_gates("demo") == ("plan", "publish")

    def test_unknown_gate_raises(self, monkeypatch):
        monkeypatch.setenv("HITL_GATES", "plan,coffee")
        with pytest.raises(ValueError, match="coffee"):
            enabled_gates("prod")

    def test_gate_nodes_cover_all_gates(self):
        assert set(GATE_NODES.values()) == set(GATES)

    def test_max_revisions_and_auto_approve(self, monkeypatch):
        assert max_revisions() == 2
        monkeypatch.setenv("HITL_MAX_REVISIONS", "5")
        assert max_revisions() == 5
        monkeypatch.setenv("HITL_MAX_REVISIONS", "x")
        assert max_revisions() == 2
        assert auto_approve() is False
        monkeypatch.setenv("HITL_AUTO_APPROVE", "true")
        assert auto_approve() is True


class TestParseDecision:
    @pytest.mark.parametrize(
        "raw,action,feedback",
        [
            ("a", "approve", ""),
            ("approve", "approve", ""),
            ("r: добави тестове", "revise", "добави тестове"),
            ("q", "abort", ""),
            ("reject", "abort", ""),
            ({"action": "revise", "feedback": "x", "by": "ivan"}, "revise", "x"),
        ],
    )
    def test_valid_forms(self, raw, action, feedback):
        d = parse_decision(raw)
        assert (d.action, d.feedback) == (action, feedback)

    @pytest.mark.parametrize("raw", ["maybe", {"action": "later"}, 42, None, {"feedback": "x"}])
    def test_invalid_is_abort_with_explanation(self, raw):
        d = parse_decision(raw)
        assert d.action == "abort" and "invalid decision" in d.feedback

    def test_passthrough_of_decision_object(self):
        d = HitlDecision(action="approve", by="me")
        assert parse_decision(d) is d


class TestGateNodeWithoutInterrupt:
    def _node(self, enabled, on_disabled=None, **kw):
        calls = []
        return make_gate_node(
            "plan",
            enabled=enabled,
            build_payload=lambda s: {"preview": "план"},
            on_approve=lambda s, d: calls.append(("approve", d)) or {"next": "developer"},
            on_revise=lambda s, d: {"next": "dev_plan"},
            on_abort=lambda s, d: {"next": "finalize", "final_status": "ABORTED"},
            on_disabled=on_disabled,
            **kw,
        ), calls

    def test_disabled_gate_is_pass_through_without_a_record(self):
        node, calls = self._node(enabled=False)
        update = node({"hitl_decisions": []})
        assert update["next"] == "developer"
        assert "автоматично" in update["reason"]
        # Изключена порта не е човешко решение - нищо в одита
        assert "hitl_decisions" not in update

    def test_disabled_gate_uses_on_disabled_when_given(self):
        node, calls = self._node(enabled=False, on_disabled=lambda s, d: {"next": "finalize"})
        assert node({})["next"] == "finalize" and calls == []

    def test_auto_approve_env(self, monkeypatch):
        monkeypatch.setenv("HITL_AUTO_APPROVE", "1")
        node, _ = self._node(enabled=True)
        update = node({"hitl_decisions": [{"gate": "x"}]})
        assert update["next"] == "developer"
        assert update["hitl_decisions"][-1]["by"] == "auto" and len(update["hitl_decisions"]) == 2

    def test_auto_decision_is_recorded_on_disk(self, tmp_path, monkeypatch):
        from src.run_tracker import RunTracker

        monkeypatch.setenv("HITL_AUTO_APPROVE", "1")
        node, _ = self._node(enabled=True)
        node({"run_dir": str(tmp_path)})
        assert RunTracker(tmp_path).hitl_decisions()[0]["by"] == "auto"
        assert "| plan | approve | auto |" in (tmp_path / "hitl-decisions.md").read_text(encoding="utf-8")
