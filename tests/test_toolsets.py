"""
Unit тестове за src/toolsets.py - инструментите и промпт добавките по режим.

Контрактът: demo и prod дават на агентите инструменти с ЕДНИ И СЪЩИ имена
за общите функции, а prod модулите се импортират lazy (demo работи без mcp).
"""

import sys

import pytest

from src.toolsets import PROMPT_ADDENDA, DemoContext, ProdContext, make_mode_context, make_tools


class TestModeContext:
    def test_demo_context_needs_nothing(self):
        ctx = make_mode_context("demo")
        assert isinstance(ctx, DemoContext) and ctx.publisher is None

    def test_prod_modules_are_imported_lazily(self):
        # Статична проверка (не зависи от реда на тестовете): toolsets/graph НЕ
        # импортират jira_mcp на ниво модул - demo режимът работи без mcp пакета.
        import ast
        import inspect

        from src import graph, toolsets

        for module in (toolsets, graph):
            tree = ast.parse(inspect.getsource(module))
            top_level_imports = {
                getattr(node, "module", None) or alias.name
                for node in tree.body
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names
            }
            assert not any("jira_mcp" in str(name) or "repo_workspace" in str(name) for name in top_level_imports), (
                f"{module.__name__} импортира prod модул на ниво модул"
            )

    def test_prod_without_config_raises_with_problems(self, monkeypatch):
        monkeypatch.delenv("PROD_REPOS", raising=False)
        with pytest.raises(RuntimeError, match="PROD_REPOS"):
            make_mode_context("prod")

    def test_prod_with_config_builds_jira_client_lazily(self, monkeypatch):
        monkeypatch.setenv("PROD_REPOS", "a/b")
        monkeypatch.setenv("JIRA_EMAIL", "e@x.com")
        monkeypatch.setenv("JIRA_API_TOKEN", "t")
        ctx = make_mode_context("prod", repos=["a/b"])
        assert isinstance(ctx, ProdContext) and ctx.repos == ["a/b"]
        assert ctx.jira is not None and ctx.jira_settings.email == "e@x.com"
        assert [t.name for t in ctx.jira_tools] == ["get_ticket_details", "search_tickets"]
        # Git интеграцията идва в PR 3 - още няма workspace/publisher
        assert ctx.workspace_view is None and ctx.publisher is None
        assert "src.jira_mcp" in sys.modules  # импортиран lazy, само за prod


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

        class WithGit:  # prod контекст С git workspace (PR 3) -> пълните prod добавки
            jira_tools = []
            repo_read_tools = ["READ"]
            repo_write_tools = ["WRITE"]
            repo_diff_tools = ["DIFF"]

        prod = make_tools("prod", WithGit()).prompt_addendum
        assert "write_repo_file" in prod["developer"] and "Do NOT return the code" in prod["developer"]

    def test_prod_without_git_uses_demo_delivery_for_developer_and_qa(self, monkeypatch):
        # Преходен случай (до PR 3): Jira е реална, git workspace още няма
        monkeypatch.setenv("PROD_REPOS", "a/b")
        monkeypatch.setenv("JIRA_EMAIL", "e@x.com")
        monkeypatch.setenv("JIRA_API_TOKEN", "t")
        ts = make_tools("prod", make_mode_context("prod"))
        assert [t.name for t in ts.tools["analyst"]] == ["get_ticket_details", "search_tickets"]
        assert "search_tickets" in ts.prompt_addendum["analyst"]
        assert "```python" in ts.prompt_addendum["developer"]  # не обещаваме write_repo_file
        assert "write_repo_file" not in ts.prompt_addendum["developer"]

    def test_prod_and_demo_ticket_tools_share_the_contract(self, monkeypatch):
        monkeypatch.setenv("PROD_REPOS", "a/b")
        monkeypatch.setenv("JIRA_EMAIL", "e@x.com")
        monkeypatch.setenv("JIRA_API_TOKEN", "t")
        demo_tool = make_tools("demo", DemoContext()).tools["analyst"][0]
        prod_tool = make_tools("prod", make_mode_context("prod")).tools["analyst"][0]
        assert (demo_tool.name, demo_tool.args, demo_tool.description) == (
            prod_tool.name, prod_tool.args, prod_tool.description
        )

    def test_addenda_have_same_shape_in_both_languages(self):
        for mode in ("demo", "prod"):
            assert set(PROMPT_ADDENDA["bg"][mode]) == set(PROMPT_ADDENDA["en"][mode]) == {"analyst", "developer", "qa"}
