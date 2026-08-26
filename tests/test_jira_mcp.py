"""
Unit тестове за src/jira_mcp.py - Jira през Atlassian MCP, БЕЗ мрежа.

MCP извикването е инжектируемо (call_tool): дубльорът връща фикстури във
формата, проверена на живо срещу mcp.atlassian.com (JSON текст в първия
TextContent блок; description е markdown текст; search връща issues.nodes;
грешките са {"error": true, "message": ...}).
"""

import json
from types import SimpleNamespace

import pytest

from src import jira_mcp
from src.jira_mcp import (
    JiraMcpClient,
    JiraMcpError,
    JiraMcpSettings,
    adf_to_text,
    extract_acceptance_criteria,
    format_ticket_card,
    make_jira_tools,
    normalize_issue,
    parse_tool_result,
)
from src.tools import get_ticket_details as demo_ticket_tool

# Фикстура във верифицираната форма на getJiraIssue (responseContentFormat=markdown)
ISSUE = {
    "key": "AA-42",
    "webUrl": "https://myposgroup.atlassian.net/browse/AA-42",
    "fields": {
        "summary": "Валидация на имейл при регистрация",
        "description": (
            "Като потребител искам системата да валидира имейл адреси.\n\n"
            "## Критерии за приемане\n"
            "- Функцията приема string и връща True/False\n"
            "- Точно един символ '@'\n"
            "3. Празен string връща False\n\n"
            "## Бележки\n"
            "Нищо друго."
        ),
        "priority": {"name": "High"},
        "status": {"name": "To Do"},
        "issuetype": {"name": "Story"},
        "labels": ["backend"],
    },
}

SEARCH = {
    "issues": {
        "nodes": [
            {"key": "AA-1", "fields": {"summary": "първи", "status": {"name": "Done"}, "priority": {"name": "Low"}}},
            {"key": "AA-2", "fields": {"summary": "втори", "status": {"name": "To Do"}, "priority": {"name": "High"}}},
        ],
        "pageInfo": {"hasNextPage": False},
    }
}


def text_result(payload, is_error=False):
    """CallToolResult дубльор: JSON текст в първия TextContent блок."""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], structuredContent=None, isError=is_error)


class FakeCallTool:
    """Дубльор на MCP call_tool: записва извикванията, връща по име на инструмент."""

    def __init__(self, responses: dict, failures: list | None = None):
        self.responses = responses
        self.failures = list(failures or [])  # изключения, хвърляни ПРЕДИ успешния отговор
        self.calls = []

    def __call__(self, name, args):
        self.calls.append((name, args))
        if self.failures:
            raise self.failures.pop(0)
        response = self.responses[name]
        if isinstance(response, Exception):
            raise response
        return response


def client(responses, failures=None, **settings) -> tuple[JiraMcpClient, FakeCallTool, list]:
    fake = FakeCallTool(responses, failures)
    sleeps = []
    c = JiraMcpClient(
        JiraMcpSettings(email="e@x.com", api_token="tok", **settings),
        call_tool=fake,
        sleep=sleeps.append,
    )
    return c, fake, sleeps


class TestSettings:
    def test_defaults(self, monkeypatch):
        for var in ("JIRA_MCP_URL", "JIRA_MCP_AUTH", "JIRA_CLOUD_ID", "JIRA_MCP_TIMEOUT_SECONDS", "JIRA_WRITE_BACK"):
            monkeypatch.delenv(var, raising=False)
        s = JiraMcpSettings.from_env()
        assert s.url == jira_mcp.DEFAULT_MCP_URL and s.auth == "api_token"
        assert s.cloud_id == "myposgroup.atlassian.net" and s.timeout_s == 60.0 and s.write_back is False

    def test_from_env_reads_everything(self, monkeypatch):
        monkeypatch.setenv("JIRA_MCP_AUTH", "OAuth")
        monkeypatch.setenv("JIRA_MCP_TIMEOUT_SECONDS", "5")
        monkeypatch.setenv("JIRA_MCP_MAX_RETRIES", "0")
        monkeypatch.setenv("JIRA_AC_FIELD", "customfield_1")
        monkeypatch.setenv("JIRA_WRITE_BACK", "1")
        s = JiraMcpSettings.from_env()
        assert (s.auth, s.timeout_s, s.max_retries, s.ac_field, s.write_back) == ("oauth", 5.0, 0, "customfield_1", True)

    def test_invalid_numbers_fall_back(self, monkeypatch):
        monkeypatch.setenv("JIRA_MCP_TIMEOUT_SECONDS", "бавно")
        monkeypatch.setenv("JIRA_MCP_MAX_RETRIES", "много")
        s = JiraMcpSettings.from_env()
        assert s.timeout_s == 60.0 and s.max_retries == 2

    def test_missing_per_auth(self):
        assert JiraMcpSettings(auth="api_token").missing() == ["JIRA_EMAIL", "JIRA_API_TOKEN"]
        assert JiraMcpSettings(auth="api_token", email="e", api_token="t").missing() == []
        assert JiraMcpSettings(auth="oauth").missing() == []
        assert JiraMcpSettings(auth="magic").missing() == ["JIRA_MCP_AUTH"]

    def test_basic_auth_header(self):
        s = JiraMcpSettings(email="me@x.com", api_token="secret")
        assert s.auth_headers() == {"Authorization": "Basic bWVAeC5jb206c2VjcmV0"}
        assert JiraMcpSettings(auth="oauth").auth_headers() == {}

    def test_browse_url_only_for_hostnames(self):
        assert JiraMcpSettings(cloud_id="site.atlassian.net").browse_url("AA-1") == "https://site.atlassian.net/browse/AA-1"
        assert JiraMcpSettings(cloud_id="fb47470f-f5c2-44bc-8182-f2a22f059adb").browse_url("AA-1") == ""


class TestParseToolResult:
    def test_json_text_is_parsed(self):
        assert parse_tool_result(text_result({"a": 1})) == {"a": 1}

    def test_plain_text_passes_through(self):
        assert parse_tool_result(text_result("просто текст")) == "просто текст"

    def test_structured_content_wins(self):
        result = SimpleNamespace(content=[], structuredContent={"key": "AA-1"}, isError=False)
        assert parse_tool_result(result) == {"key": "AA-1"}

    def test_is_error_flag_raises(self):
        with pytest.raises(JiraMcpError, match="Issue does not exist"):
            parse_tool_result(text_result({"error": True, "message": "Issue does not exist"}, is_error=True))

    def test_error_payload_without_flag_raises(self):
        with pytest.raises(JiraMcpError, match="Unbounded JQL"):
            parse_tool_result(text_result({"error": True, "message": "Unbounded JQL queries are not allowed here"}))


class TestClient:
    def test_get_issue_maps_arguments_and_normalizes(self):
        c, fake, _ = client({"getJiraIssue": ISSUE}, ac_field="customfield_9")
        issue = c.get_issue("aa-42")
        name, args = fake.calls[0]
        assert name == "getJiraIssue"
        assert args["cloudId"] == "myposgroup.atlassian.net" and args["issueIdOrKey"] == "AA-42"
        assert args["responseContentFormat"] == "markdown" and "customfield_9" in args["fields"]
        assert issue["key"] == "AA-42" and issue["priority"] == "High" and issue["status"] == "To Do"
        assert issue["acceptance_criteria"] == [
            "Функцията приема string и връща True/False", "Точно един символ '@'", "Празен string връща False",
        ]
        assert issue["url"] == "https://myposgroup.atlassian.net/browse/AA-42"

    def test_search_reads_issues_nodes(self):
        c, fake, _ = client({"searchJiraIssuesUsingJql": SEARCH})
        rows = c.search("project = AA", max_results=5)
        assert [r["key"] for r in rows] == ["AA-1", "AA-2"] and rows[1]["priority"] == "High"
        assert fake.calls[0][1]["maxResults"] == 5

    def test_search_accepts_plain_list_shape(self):
        c, _, _ = client({"searchJiraIssuesUsingJql": {"issues": [{"key": "AA-9", "fields": {"summary": "x"}}]}})
        assert c.search("project = AA")[0]["key"] == "AA-9"

    def test_add_comment_arguments(self):
        c, fake, _ = client({"addCommentToJiraIssue": {"id": "1"}})
        c.add_comment("aa-42", "**PR**: https://github.com/x/y/pull/1")
        assert fake.calls[0][1]["contentFormat"] == "markdown" and fake.calls[0][1]["issueIdOrKey"] == "AA-42"

    def test_test_connection_lists_tools_then_resources(self):
        resources = [{"id": "fb47", "url": "https://myposgroup.atlassian.net", "scopes": ["read:jira-work"]}]
        tools = [*jira_mcp.REQUIRED_TOOLS, "getAccessibleAtlassianResources", "getJiraIssueTypeMetaWithFields"]
        c, fake, _ = client({jira_mcp.LIST_TOOLS: tools, "getAccessibleAtlassianResources": resources})
        text = c.test_connection()
        assert fake.calls[0][0] == jira_mcp.LIST_TOOLS
        assert text.splitlines() == [
            "Свързан с Atlassian MCP: 5 инструмента, Jira инструментите са налични.",
            "https://myposgroup.atlassian.net · fb47 · jira",
        ]

    def test_test_connection_reports_missing_jira_tools(self):
        # Проверено на живо: с невалидни креденшъли сървърът приема сесията, но не излага инструменти
        c, fake, _ = client({jira_mcp.LIST_TOOLS: []})
        with pytest.raises(JiraMcpError, match="JIRA_EMAIL / JIRA_API_TOKEN"):
            c.test_connection()
        assert len(fake.calls) == 1  # не стигаме до getAccessibleAtlassianResources

    def test_transient_errors_are_retried_with_backoff(self):
        c, fake, sleeps = client({"getJiraIssue": ISSUE}, failures=[ConnectionError("reset"), TimeoutError()])
        assert c.get_issue("AA-42")["key"] == "AA-42"
        assert len(fake.calls) == 3 and sleeps == [1.0, 2.0]

    def test_transient_errors_give_up_after_max_retries(self):
        c, fake, _ = client({"getJiraIssue": ISSUE}, failures=[OSError("down")] * 5, max_retries=1)
        with pytest.raises(JiraMcpError, match="OSError"):
            c.get_issue("AA-42")
        assert len(fake.calls) == 2

    def test_server_errors_are_not_retried(self):
        c, fake, sleeps = client({"getJiraIssue": JiraMcpError("Issue does not exist")})
        with pytest.raises(JiraMcpError, match="does not exist"):
            c.get_issue("AA-404")
        assert len(fake.calls) == 1 and sleeps == []

    def test_missing_mcp_package_is_not_retried(self):
        c, fake, _ = client({"getJiraIssue": ImportError("No module named mcp")})
        with pytest.raises(JiraMcpError, match="ImportError"):
            c.get_issue("AA-1")
        assert len(fake.calls) == 1


class TestNormalization:
    def test_adf_to_text(self):
        adf = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Първи "}, {"type": "text", "text": "ред"}]},
                {"type": "bulletList", "content": [
                    {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "точка"}]}]},
                ]},
                {"type": "paragraph", "content": [{"type": "mention", "attrs": {"text": "@ivan"}}, {"type": "hardBreak"}]},
            ],
        }
        text = adf_to_text(adf)
        assert "Първи ред" in text and "- точка" in text and "@ivan" in text
        assert adf_to_text(None) == "" and adf_to_text("готово") == "готово" and adf_to_text(5) == "5"

    def test_criteria_from_english_heading_and_bold(self):
        desc = "Intro\n\n**Acceptance criteria:**\n* first\n* second\n\nOther section"
        assert extract_acceptance_criteria(desc) == ["first", "second"]

    def test_criteria_stop_at_next_heading(self):
        desc = "AC\nодин\nдве\n## Друго\nтри"
        assert extract_acceptance_criteria(desc) == ["один", "две"]

    def test_criteria_from_custom_field_first(self):
        fields = {"customfield_1": "- от полето\n- второ"}
        assert extract_acceptance_criteria("## Критерии за приемане\n- от описанието", fields, "customfield_1") == ["от полето", "второ"]

    def test_no_criteria_gives_empty_list(self):
        assert extract_acceptance_criteria("Просто описание без секции.") == []
        assert extract_acceptance_criteria("") == []

    def test_normalize_handles_missing_fields_and_adf(self):
        raw = {"key": "AA-7", "fields": {"summary": " x ", "description": {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "ADF описание"}]}]}}}
        issue = normalize_issue(raw, JiraMcpSettings(cloud_id="site.atlassian.net"))
        assert issue["summary"] == "x" and issue["description"] == "ADF описание"
        assert issue["priority"] == "-" and issue["labels"] == []
        assert issue["url"] == "https://site.atlassian.net/browse/AA-7"

    def test_normalize_rejects_non_dict(self):
        with pytest.raises(JiraMcpError):
            normalize_issue("текст", JiraMcpSettings())


class TestCard:
    def test_card_starts_like_the_mock_card(self):
        issue = normalize_issue(ISSUE, JiraMcpSettings())
        card = format_ticket_card(issue)
        mock_card = demo_ticket_tool.invoke({"ticket_id": "DEV-101"})
        # Същите първи етикети (Тикет/Заглавие/Приоритет/Описание/Критерии) като mock картата
        assert [line.split(":")[0] for line in card.splitlines()[:3]] == [line.split(":")[0] for line in mock_card.splitlines()[:3]]
        assert "Критерии за приемане:" in card and "  - Точно един символ '@'" in card
        assert "Статус: To Do" in card and "Тип: Story" in card and "browse/AA-42" in card

    def test_card_without_criteria_has_the_hint(self, monkeypatch):
        monkeypatch.setenv("APP_LANG", "en")
        issue = {"key": "AA-1", "summary": "s", "description": "d", "priority": "-", "status": "-", "issuetype": "-", "url": "", "acceptance_criteria": []}
        card = format_ticket_card(issue)
        assert "derive them from the description" in card and card.startswith("Ticket: AA-1")

    def test_texts_have_same_keys_in_both_languages(self):
        assert set(jira_mcp._TEXTS["bg"]) == set(jira_mcp._TEXTS["en"])


class TestTools:
    def test_get_ticket_details_keeps_the_demo_contract(self):
        c, _, _ = client({"getJiraIssue": ISSUE})
        prod_tool, search_tool = make_jira_tools(c)
        assert prod_tool.name == demo_ticket_tool.name == "get_ticket_details"
        assert prod_tool.args == demo_ticket_tool.args
        assert prod_tool.description == demo_ticket_tool.description
        assert search_tool.name == "search_tickets" and set(search_tool.args) == {"jql"}

    def test_get_ticket_details_returns_card_or_error_text(self):
        c, _, _ = client({"getJiraIssue": JiraMcpError("Issue does not exist or you do not have permission")})
        prod_tool, _ = make_jira_tools(c)
        text = prod_tool.invoke({"ticket_id": "aa-404"})
        assert text.startswith("Тикет 'AA-404': грешка от Jira") and "does not exist" in text

        c2, _, _ = client({"getJiraIssue": ISSUE})
        card = make_jira_tools(c2)[0].invoke({"ticket_id": "AA-42"})
        assert card.startswith("Тикет: AA-42\nЗаглавие: Валидация на имейл")

    def test_search_tickets_rows_empty_and_unbounded_hint(self):
        c, _, _ = client({"searchJiraIssuesUsingJql": SEARCH})
        _, search_tool = make_jira_tools(c)
        assert search_tool.invoke({"jql": "project = AA"}) == "AA-1 | Done | Low | първи\nAA-2 | To Do | High | втори"

        c_empty, _, _ = client({"searchJiraIssuesUsingJql": {"issues": {"nodes": []}}})
        assert make_jira_tools(c_empty)[1].invoke({"jql": "project = ZZ"}).startswith("Няма тикети")

        c_err, _, _ = client({"searchJiraIssuesUsingJql": JiraMcpError("Unbounded JQL queries are not allowed here")})
        text = make_jira_tools(c_err)[1].invoke({"jql": "text ~ x"})
        assert "Подсказка" in text and "project = KEY" in text


class TestSyncBridge:
    def test_run_sync_returns_result_and_propagates_errors(self):
        async def ok():
            return 42

        async def boom():
            raise ValueError("лошо")

        assert jira_mcp._run_sync(ok, timeout_s=5) == 42
        with pytest.raises(ValueError, match="лошо"):
            jira_mcp._run_sync(boom, timeout_s=5)

    def test_run_sync_times_out(self):
        import asyncio

        async def slow():
            await asyncio.sleep(5)

        with pytest.raises(TimeoutError):
            jira_mcp._run_sync(slow, timeout_s=0.05)

    def test_transient_classification(self):
        assert jira_mcp._is_transient(TimeoutError()) is True
        assert jira_mcp._is_transient(JiraMcpError("x")) is False
        assert jira_mcp._is_transient(ImportError("x")) is False
