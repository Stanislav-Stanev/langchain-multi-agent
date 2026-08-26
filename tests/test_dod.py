"""
Unit тестове за src/dod.py - Definition of Done политиките по режим.

DemoDoD трябва да възпроизвежда ТОЧНО досегашното поведение на Developer
възела (```python блок + ast.parse); ProdDoD работи върху git workspace
(тук - фалшив), без реални git операции.
"""

import pytest

from src.dod import DemoDoD, ProdDoD, extract_python_code, make_dod_policy

VALID = "```python\ndef f() -> int:\n    return 1\n```"


class TestExtractPythonCode:
    def test_extracts_single_block(self):
        text = "Ето кода:\n```python\ndef f():\n    return 1\n```\nГотово."
        assert extract_python_code(text) == "def f():\n    return 1"

    def test_joins_multiple_blocks(self):
        text = "```python\ndef f(): ...\n```\nи тестът:\n```python\ndef test_f(): ...\n```"
        code = extract_python_code(text)
        assert "def f()" in code and "def test_f()" in code

    def test_no_block_returns_empty(self):
        assert extract_python_code("тук няма код, само обяснение") == ""

    def test_plain_fence_without_language_is_not_matched(self):
        assert extract_python_code("```\nx = 1\n```") == ""

    def test_empty_input(self):
        assert extract_python_code("") == ""


class TestCommonChecks:
    def test_spec_problem(self):
        dod = DemoDoD()
        assert dod.spec_problem("") is not None
        assert dod.spec_problem("   ") is not None
        assert dod.spec_problem("спецификация") is None

    def test_plan_and_test_plan_problems(self):
        dod = DemoDoD()
        assert dod.plan_problem({"steps": []}) is not None
        assert dod.plan_problem({"steps": [{"id": "S1"}]}) is None
        assert dod.test_plan_problem({"cases": []}) is not None
        assert dod.test_plan_problem({"cases": [{"id": "T1"}]}) is None


class TestDemoDoD:
    def test_valid_block_passes_and_returns_code(self):
        problem, code = DemoDoD().developer_result(VALID, None)
        assert problem is None
        assert code == "def f() -> int:\n    return 1"

    def test_missing_block_is_a_problem(self):
        problem, code = DemoDoD().developer_result("само обяснение", None)
        assert "```python" in problem and code == ""

    def test_syntax_error_is_a_problem_with_message(self):
        problem, code = DemoDoD().developer_result("```python\ndef f(:\n```", None)
        assert "синтактична" in problem and code == ""

    def test_ready_reason(self):
        assert "QA" in DemoDoD().ready_reason("x")


class FakeWorkspace:
    """Дубльор на git workspace-а: списък променени файлове + съдържание."""

    def __init__(self, files: dict[str, str]):
        self.files = files

    def changed_files(self):
        return list(self.files)

    def syntax_error(self, path):
        import ast

        try:
            ast.parse(self.files[path])
        except SyntaxError as exc:
            return exc.msg
        return None

    def diff(self):
        return "".join(f"diff --git a/{p} b/{p}\n+{c}\n" for p, c in self.files.items())


PLAN_DONE = {"steps": [{"id": "S1", "status": "done"}, {"id": "S2", "status": "todo"}]}
PLAN_TODO = {"steps": [{"id": "S1", "status": "todo"}]}


class TestProdDoD:
    def test_no_changes_is_a_problem(self):
        problem, code = ProdDoD(FakeWorkspace({})).developer_result("описание", PLAN_DONE)
        assert problem is not None and code == ""

    def test_broken_python_file_is_a_problem_naming_the_file(self):
        ws = FakeWorkspace({"pkg/bad.py": "def f(:", "README.md": "ok"})
        problem, _ = ProdDoD(ws).developer_result("описание", PLAN_DONE)
        assert "pkg/bad.py" in problem

    def test_no_done_step_is_a_problem(self):
        ws = FakeWorkspace({"a.py": "x = 1\n"})
        problem, _ = ProdDoD(ws).developer_result("описание", PLAN_TODO)
        assert "update_plan_step" in problem

    def test_happy_path_returns_diff(self):
        ws = FakeWorkspace({"a.py": "x = 1\n", "b.py": "y = 2\n"})
        problem, code = ProdDoD(ws).developer_result("описание", PLAN_DONE)
        assert problem is None
        assert code.startswith("diff --git")
        assert "2" in ProdDoD(ws).ready_reason(code)  # 2 файла


class TestFactory:
    def test_demo(self):
        assert isinstance(make_dod_policy("demo"), DemoDoD)

    def test_prod_requires_workspace(self):
        with pytest.raises(ValueError):
            make_dod_policy("prod")
        assert isinstance(make_dod_policy("prod", FakeWorkspace({})), ProdDoD)
