"""
Definition of Done политики по режим (improvement.md §6.1).

Всяка фаза има DoD, проверяван ОТ КОДА, не от LLM. Проверката зависи от
режима само за Developer: в demo артефактът е ```python блок в отговора,
в prod - реални промени в git workspace. Останалите проверки (спецификация,
план, тест-план) са еднакви. Политиката е обект, който build_graph() избира
веднъж според режима и подава на възлите - възлите не знаят за режими.
"""

import ast

# Регулярен израз за ```python ... ``` блок - кодът артефакт на Developer
# се извлича оттук, а не от целия свободен текст на отговора.
import re
from typing import Protocol

from src.i18n import t

_PYTHON_BLOCK_RE = re.compile(r"```python\s*\n(.*?)```", re.DOTALL)


def extract_python_code(text: str) -> str:
    """
    Извлича Python кода от markdown отговора на Developer агента.

    Взимат се ВСИЧКИ ```python блокове (агентът може да върне функцията
    и тестовете ѝ отделно). Ако няма нито един блок - връща празен низ,
    което проваля Definition of Done проверката и връща задачата на
    Developer с конкретно указание.
    """
    blocks = _PYTHON_BLOCK_RE.findall(text or "")
    return "\n\n".join(block.strip() for block in blocks).strip()


class DoDPolicy(Protocol):
    """Интерфейсът, който възлите ползват (виж DemoDoD за семантиката)."""

    def spec_problem(self, spec: str) -> str | None: ...
    def plan_problem(self, plan_doc: dict) -> str | None: ...
    def test_plan_problem(self, test_doc: dict) -> str | None: ...
    def developer_result(self, answer: str, plan_doc: dict | None) -> tuple[str | None, str]: ...
    def ready_reason(self, code: str) -> str: ...


class _CommonDoD:
    """Общите проверки за двата режима."""

    def spec_problem(self, spec: str) -> str | None:
        return None if (spec or "").strip() else t("dod_missing_spec")

    def plan_problem(self, plan_doc: dict) -> str | None:
        return None if (plan_doc or {}).get("steps") else t("dod_empty_plan")

    def test_plan_problem(self, test_doc: dict) -> str | None:
        return None if (test_doc or {}).get("cases") else t("dod_empty_test_plan")


class DemoDoD(_CommonDoD):
    """Demo: артефактът на Developer е ```python блок с валиден синтаксис."""

    def developer_result(self, answer: str, plan_doc: dict | None) -> tuple[str | None, str]:
        # 1. отговорът съдържа ```python блок с код;
        code = extract_python_code(answer)
        if not code:
            return t("dod_missing_code"), ""
        # 2. кодът е синтактично валиден (ast.parse НЕ изпълнява кода).
        try:
            ast.parse(code)
        except SyntaxError as exc:
            return t("dod_syntax_error", error=exc.msg), ""
        return None, code

    def ready_reason(self, code: str) -> str:
        return t("route_code_ready")


class WorkspaceView(Protocol):
    """Каквото ProdDoD иска от git workspace-а (реализация: src/repo_workspace.py, PR 3)."""

    def changed_files(self) -> list[str]: ...
    def syntax_error(self, path: str) -> str | None: ...
    def diff(self) -> str: ...


class ProdDoD(_CommonDoD):
    """
    Prod: артефактът е реалният diff в workspace-а.

    DoD: (1) има променен файл; (2) всеки променен .py файл се парсва;
    (3) поне една стъпка от плана е отметната done - Developer отчита
    какво е свършил, иначе планът е декорация.
    """

    def __init__(self, workspace: WorkspaceView):
        self.workspace = workspace

    def developer_result(self, answer: str, plan_doc: dict | None) -> tuple[str | None, str]:
        changed = self.workspace.changed_files()
        if not changed:
            return t("dod_no_diff"), ""
        for path in changed:
            if path.endswith(".py"):
                error = self.workspace.syntax_error(path)
                if error:
                    return t("dod_changed_file_syntax_error", file=path, error=error), ""
        done = sum(1 for s in (plan_doc or {}).get("steps", []) if s.get("status") == "done")
        if done == 0:
            return t("dod_no_step_done"), ""
        return None, self.workspace.diff()

    def ready_reason(self, code: str) -> str:
        files = sum(1 for line in (code or "").splitlines() if line.startswith("diff --git"))
        return t("route_diff_ready", files=files)


def make_dod_policy(mode: str, workspace: WorkspaceView | None = None) -> DoDPolicy:
    """Политиката за режима; prod изисква workspace."""
    if mode == "prod":
        if workspace is None:
            raise ValueError("ProdDoD изисква workspace (git репозиторий).")
        return ProdDoD(workspace)
    return DemoDoD()
