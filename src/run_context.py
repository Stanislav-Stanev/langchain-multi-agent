"""
Контекст на текущия run, подаван на агентите и техните инструменти.

Инструментите за плановете (update_plan_step, update_test_case) трябва да
знаят В КОЯ директория runs/<run>/ да пишат. Вместо глобална променлива,
LangChain 1.x предлага "runtime context": агентът се създава с
context_schema=RunCtx, при извикване получава context=RunCtx(...), а
инструмент с параметър runtime: ToolRuntime[RunCtx] получава същия обект.
Параметърът runtime НЕ се вижда в JSON схемата към модела (проверено) -
моделът подава само своите аргументи.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RunCtx:
    """Минималният контекст, който един инструмент трябва да знае за run-а."""

    run_id: str
    run_dir: str
    mode: str


def run_ctx_from_state(state: dict) -> RunCtx:
    """Сглобява контекста от полетата на TeamState (виж src/graph.py)."""
    return RunCtx(
        run_id=state.get("run_id", "") or "",
        run_dir=state.get("run_dir", "") or "",
        mode=state.get("mode", "") or "",
    )
