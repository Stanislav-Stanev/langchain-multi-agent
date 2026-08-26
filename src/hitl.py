"""
Human-in-the-Loop (HITL) порти - човекът решава там, където грешката е скъпа.

Принципи (docs/prod-mode-plan.md §3a):
  1. Портата е ОТДЕЛЕН детерминистичен възел с interrupt() вътре - видим е в
     стрийма, resume изпълнява само него, тества се изолирано. LLM никога не
     решава дали да пита човека.
  2. Три изхода: approve (продължи), revise (назад с указания), abort (край).
  3. Всяко решение се записва (run.jsonl, hitl-decisions.md) - одит.
  4. Изключена порта = pass-through с причина „auto" - графът има една и
     съща структура независимо от конфигурацията.

Механика (LangGraph): interrupt(payload) спира графа и излиза от stream()
със събитие "__interrupt__"; консуматорът (main.py / app.py) показва
payload-а, събира решението и продължава с
graph.stream(Command(resume=decision), config=<същият thread_id>).
Изисква checkpointer - build_graph() слага InMemorySaver, ако няма друг.

Кои порти има и къде стоят е решение на build_graph() (src/graph.py); тук
е само общият механизъм.
"""

import os
from collections.abc import Callable
from typing import Literal

from langgraph.types import interrupt
from pydantic import BaseModel, ValidationError

from src.i18n import t
from src.run_tracker import RunTracker

GATES = ("plan", "publish", "escalation")

# По подразбиране: prod - всички; demo - без publish (там няма публикуване)
DEFAULT_GATES = {"demo": ("plan", "escalation"), "prod": ("plan", "publish", "escalation")}

# Възел на графа <-> име на портата
GATE_NODES = {"approve_plan": "plan", "approve_publish": "publish", "escalation_gate": "escalation"}


def default_gates(mode: str) -> tuple[str, ...]:
    return DEFAULT_GATES.get(mode, DEFAULT_GATES["prod"])


def enabled_gates(mode: str) -> tuple[str, ...]:
    """HITL_GATES от средата: липсва -> default по режим; празно -> нищо; иначе списък."""
    raw = os.getenv("HITL_GATES")
    if raw is None:
        return default_gates(mode)
    gates = tuple(g.strip().lower() for g in raw.split(",") if g.strip())
    unknown = [g for g in gates if g not in GATES]
    if unknown:
        raise ValueError(f"Непознати HITL порти: {unknown}. Валидни: {', '.join(GATES)}.")
    return gates


def max_revisions() -> int:
    """Колко пъти човек може да върне плана за ревизия (HITL_MAX_REVISIONS, default 2)."""
    try:
        return int(os.getenv("HITL_MAX_REVISIONS", "2"))
    except ValueError:
        return 2


def auto_approve() -> bool:
    """HITL_AUTO_APPROVE=1 - неинтерактивна среда (CI): портите одобряват сами и го логват."""
    return os.getenv("HITL_AUTO_APPROVE", "0").strip().lower() in ("1", "true", "yes")


class HitlDecision(BaseModel):
    """Решението на човека, както го подава консуматорът през Command(resume=...)."""

    action: Literal["approve", "revise", "abort"]
    feedback: str = ""
    by: str = "human"


_SHORTCUTS = {"a": "approve", "approve": "approve", "r": "revise", "revise": "revise",
              "q": "abort", "abort": "abort", "reject": "abort"}


def parse_decision(raw) -> HitlDecision:
    """
    Приема dict ({"action": ..., "feedback": ...}), низ ("a", "revise: текст",
    "approve") или HitlDecision. Невалидно решение = abort с обяснение - при
    съмнение системата спира, не продължава.
    """
    if isinstance(raw, HitlDecision):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        head, _, tail = text.partition(":")
        action = _SHORTCUTS.get(head.strip().lower())
        if action:
            return HitlDecision(action=action, feedback=tail.strip())
        return HitlDecision(action="abort", feedback=f"invalid decision: {text!r}")
    if isinstance(raw, dict):
        try:
            return HitlDecision(**raw)
        except (ValidationError, TypeError):
            return HitlDecision(action="abort", feedback=f"invalid decision: {raw!r}")
    return HitlDecision(action="abort", feedback=f"invalid decision: {raw!r}")


def make_gate_node(
    gate: str,
    *,
    enabled: bool,
    build_payload: Callable[[dict], dict],
    on_approve: Callable[[dict, HitlDecision], dict],
    on_revise: Callable[[dict, HitlDecision], dict],
    on_abort: Callable[[dict, HitlDecision], dict],
    can_revise: Callable[[dict], bool] | None = None,
    on_disabled: Callable[[dict, HitlDecision], dict] | None = None,
):
    """
    Фабрика за HITL възел.

    build_payload(state) -> какво вижда човекът (пътища до артефакти, preview,
    контекст). on_* връщат update-а на графа за съответното решение (next,
    reason, промени по state). can_revise(state) ограничава ревизиите - при
    изчерпване 'revise' се третира като approve с изрична причина.
    on_disabled е пътят при изключена порта (по подразбиране = on_approve;
    escalation портата например при изключване приключва run-а, не го повтаря).
    """

    def gate_node(state: dict) -> dict:
        tracker = RunTracker.from_state(state)

        if not enabled or auto_approve():
            decision = HitlDecision(action="approve", by="auto" if enabled else "disabled")
            update = (on_disabled or on_approve)(state, decision) if not enabled else on_approve(state, decision)
            update["reason"] = t("route_gate_auto", gate=gate)
        else:
            payload = {
                **build_payload(state),
                "gate": gate,
                "run_id": state.get("run_id", ""),
                "run_dir": state.get("run_dir", ""),
                "options": ["approve", "revise", "abort"] if (can_revise is None or can_revise(state)) else ["approve", "abort"],
            }
            # Тук графът СПИРА; при resume възелът се изпълнява отново от
            # начало и interrupt() връща подаденото решение.
            decision = parse_decision(interrupt(payload))

            if decision.action == "revise" and can_revise is not None and not can_revise(state):
                update = on_approve(state, decision)
                update["reason"] = t("route_gate_revisions_exhausted", max=max_revisions())
            elif decision.action == "approve":
                update = on_approve(state, decision)
                update.setdefault("reason", t("route_gate_approved", gate=gate))
            elif decision.action == "revise":
                update = on_revise(state, decision)
                update.setdefault("reason", t("route_gate_revise", gate=gate, feedback=decision.feedback))
            else:
                update = on_abort(state, decision)
                update.setdefault("reason", t("route_gate_aborted", gate=gate))

        if enabled:
            # Само реални решения (човек или изричен HITL_AUTO_APPROVE) влизат в
            # одита - изключената порта не е решение, а липса на порта.
            record = {"gate": gate, "action": decision.action, "feedback": decision.feedback, "by": decision.by}
            tracker.record_hitl(record)
            update["hitl_decisions"] = [*(state.get("hitl_decisions") or []), record]
        return update

    return gate_node
