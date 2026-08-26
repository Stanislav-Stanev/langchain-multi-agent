"""
Unit тестове за src/toolsets.py - инструментите и промпт добавките по режим.

Контрактът: demo и prod дават на агентите инструменти с ЕДНИ И СЪЩИ имена
за общите функции, а prod модулите се импортират lazy (demo работи без mcp).
"""

import sys

import pytest

from src.toolsets import PROMPT_ADDENDA, DemoContext, make_mode_context, make_tools


class TestModeContext:
    def test_demo_context_needs_nothing(self):
        ctx = make_mode_context("demo")
        assert isinstance(ctx, DemoContext) and ctx.publisher is None

    def test_demo_does_not_import_prod_modules(self):
        make_mode_context("demo")
        assert "src.jira_mcp" not in sys.modules and "src.repo_workspace" not in sys.modules

    def test_prod_without_config_raises_with_problems(self, monkeypatch):
        monkeypatch.delenv("PROD_REPOS", raising=False)
        with pytest.raises(RuntimeError, match="PROD_REPOS"):
            make_mode_context("prod")

    def test_prod_with_config_is_not_available_yet(self, monkeypatch):
        # PR 2 / PR 3 закачат Jira MCP и git workspace - дотогава ясна грешка
        monkeypatch.setenv("PROD_REPOS", "a/b")
        monkeypatch.setenv("JIRA_EMAIL", "e@x.com")
        monkeypatch.setenv("JIRA_API_TOKEN", "t")
        with pytest.raises(RuntimeError, match="не са налични"):
            make_mode_context("prod")


class TestMakeTools:
    def test_demo_tools_per_role(self):
        ts = make_tools("demo", DemoContext())
        names = {role: [t.name for t in tools] for role, tools in ts.tools.items()}
        assert names == {
            "analyst": ["get_ticket_details"],
            "developer": ["get_coding_standards", "update_plan_step"],
            "qa": ["check_code_syntax", "run_test_checklist", "update_test_case"],
        }

    def test_prod_tools_include_context_tools(self):
        class Ctx:
            jira_tools = ["JIRA"]
            repo_read_tools = ["READ"]
            repo_write_tools = ["WRITE"]
            repo_diff_tools = ["DIFF"]

        ts = make_tools("prod", Ctx())
        assert ts.tools["analyst"] == ["JIRA", "READ"]
        assert ts.tools["developer"][2:] == ["READ", "WRITE", "DIFF"]
        assert ts.tools["qa"][3:] == ["READ", "DIFF"]

    def test_prompt_addenda_by_mode_and_language(self, monkeypatch):
        demo = make_tools("demo", DemoContext()).prompt_addendum
        assert "```python" in demo["developer"] and demo["analyst"] == ""
        monkeypatch.setenv("APP_LANG", "en")
        prod = make_tools("prod", DemoContext()).prompt_addendum
        assert "write_repo_file" in prod["developer"] and "Do NOT return the code" in prod["developer"]

    def test_addenda_have_same_shape_in_both_languages(self):
        for mode in ("demo", "prod"):
            assert set(PROMPT_ADDENDA["bg"][mode]) == set(PROMPT_ADDENDA["en"][mode]) == {"analyst", "developer", "qa"}
