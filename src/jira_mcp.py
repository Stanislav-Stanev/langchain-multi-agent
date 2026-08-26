"""
Jira през официалния Atlassian Remote MCP Server (prod режим).

Какво е MCP (Model Context Protocol)? Стандарт, по който един сървър излага
"инструменти" (tools), а клиентите ги извикват с JSON аргументи. Atlassian
поддържа официален такъв сървър (https://mcp.atlassian.com/v1/mcp) с
инструменти за Jira: getJiraIssue, searchJiraIssuesUsingJql,
addCommentToJiraIssue... Тук НЕ даваме тези инструменти директно на агента -
обвиваме ги в СЪЩИЯ интерфейс, който Analyst ползва и в demo режим
(get_ticket_details(ticket_id) с картата Тикет/Заглавие/Приоритет/Описание/
Критерии за приемане). Така промптовете и тестовете на агентите не знаят за
режими (improvement.md §2.6: "реални инструменти зад същия интерфейс").

Автентикация (docs/prod-mode-plan.md D1):
    api_token (по подразбиране) - Authorization: Basic base64(email:api_token),
        неинтерактивна, подходяща за бекенд (Streamlit/CLI);
    oauth - през `npx -y mcp-remote@latest .../v1/mcp/authv2` (stdio):
        отваря браузър за OAuth 2.1 и кешира токена локално.

Sync мост: графът е синхронен, а MCP SDK-то е async. Всяко извикване се
изпълнява в ОТДЕЛНА нишка със собствен event loop (asyncio.run) - работи
еднакво добре от CLI без loop и от Streamlit (който има свой loop).
Сесия на извикване (initialize + call_tool): 2-3 мрежови кръга, приемливо за
2-4 извиквания на run.

Устойчивост: retry с backoff САМО при транспортни грешки (timeout, мрежа,
5xx/429); грешка, върната от сървъра (isError: несъществуващ тикет, невалиден
JQL), е детерминистична и не се повтаря. Инструментите към агента никога не
хвърлят - връщат текст (конвенция от src/tools.py).

`mcp` пакетът се импортира lazy - demo режимът и `import src.graph` работят и
без него (виж make_mode_context в src/toolsets.py).
"""

import asyncio
import base64
import json
import os
import re
import shlex
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from langchain_core.tools import tool

from src.i18n import get_lang, pick
from src.tools import _TICKET_TEXTS
from src.tools import get_ticket_details as _demo_ticket_tool

DEFAULT_MCP_URL = "https://mcp.atlassian.com/v1/mcp"
DEFAULT_OAUTH_COMMAND = "npx -y mcp-remote@latest https://mcp.atlassian.com/v1/mcp/authv2"
DEFAULT_CLOUD_ID = "myposgroup.atlassian.net"

ISSUE_FIELDS = ["summary", "description", "priority", "status", "issuetype", "labels"]
SEARCH_FIELDS = ["summary", "status", "priority", "issuetype"]

# Псевдо-име: вместо call_tool сесията прави list_tools (диагностика на връзката)
LIST_TOOLS = "__list_tools__"
# Инструментите на Jira, които клиентът ползва - проверяваме ги при test_connection
REQUIRED_TOOLS = ("getJiraIssue", "searchJiraIssuesUsingJql", "addCommentToJiraIssue")


class JiraMcpError(Exception):
    """Грешка от Jira/MCP слоя - инструментите я превеждат в текст за агента."""


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JiraMcpSettings:
    """Всичко, което клиентът трябва да знае - чете се от средата при build_graph()."""

    url: str = DEFAULT_MCP_URL
    auth: str = "api_token"          # api_token | oauth
    email: str = ""
    api_token: str = ""
    cloud_id: str = DEFAULT_CLOUD_ID  # hostname или UUID - сървърът приема двете
    command: str = DEFAULT_OAUTH_COMMAND
    timeout_s: float = 60.0
    max_retries: int = 2
    ac_field: str = ""               # custom field за критерии за приемане (по избор)
    write_back: bool = False

    @classmethod
    def from_env(cls) -> "JiraMcpSettings":
        def _float(name, default):
            try:
                return float(os.getenv(name, default))
            except ValueError:
                return float(default)

        def _int(name, default):
            try:
                return int(os.getenv(name, default))
            except ValueError:
                return int(default)

        return cls(
            url=os.getenv("JIRA_MCP_URL", DEFAULT_MCP_URL).strip() or DEFAULT_MCP_URL,
            auth=os.getenv("JIRA_MCP_AUTH", "api_token").strip().lower() or "api_token",
            email=os.getenv("JIRA_EMAIL", "").strip(),
            api_token=os.getenv("JIRA_API_TOKEN", "").strip(),
            cloud_id=os.getenv("JIRA_CLOUD_ID", DEFAULT_CLOUD_ID).strip() or DEFAULT_CLOUD_ID,
            command=os.getenv("JIRA_MCP_COMMAND", DEFAULT_OAUTH_COMMAND).strip() or DEFAULT_OAUTH_COMMAND,
            timeout_s=_float("JIRA_MCP_TIMEOUT_SECONDS", "60"),
            max_retries=_int("JIRA_MCP_MAX_RETRIES", "2"),
            ac_field=os.getenv("JIRA_AC_FIELD", "").strip(),
            write_back=os.getenv("JIRA_WRITE_BACK", "0").strip().lower() in ("1", "true", "yes"),
        )

    def missing(self) -> list[str]:
        """Липсващите променливи за избраната автентикация."""
        if self.auth == "api_token":
            return [name for name, value in (("JIRA_EMAIL", self.email), ("JIRA_API_TOKEN", self.api_token)) if not value]
        if self.auth == "oauth":
            return []
        return ["JIRA_MCP_AUTH"]

    def auth_headers(self) -> dict[str, str]:
        """Basic автентикация за api_token режима (email:token в base64)."""
        if self.auth != "api_token":
            return {}
        raw = f"{self.email}:{self.api_token}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}

    def browse_url(self, key: str) -> str:
        """Линк към тикета в браузъра (само ако cloud_id е hostname)."""
        if "." in self.cloud_id and "-" not in self.cloud_id.split(".")[0][:8]:
            return f"https://{self.cloud_id}/browse/{key}"
        return ""


# ---------------------------------------------------------------------------
# Транспорт: async MCP сесия + sync мост
# ---------------------------------------------------------------------------


def _run_sync(coro_factory: Callable[[], Any], timeout_s: float) -> Any:
    """Изпълнява async корутина в отделна нишка със собствен loop и връща резултата.

    Безопасно и когда викащият няма event loop (CLI), и когда има (Streamlit) -
    затова НЕ ползваме asyncio.run директно в текущата нишка."""
    box: dict[str, Any] = {}

    def runner():
        try:
            box["result"] = asyncio.run(asyncio.wait_for(coro_factory(), timeout_s))
        except BaseException as exc:  # noqa: BLE001 - пренасяме всичко към викащата нишка
            box["error"] = exc

    thread = threading.Thread(target=runner, name="jira-mcp", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


async def _call_over_mcp(settings: JiraMcpSettings, name: str, args: dict) -> Any:
    """Една MCP сесия: connect -> initialize -> call_tool -> close."""
    try:
        from mcp import ClientSession  # lazy: demo режимът не изисква пакета
    except ImportError as exc:  # pragma: no cover - зависи от средата
        raise JiraMcpError(pick(_TEXTS)["mcp_missing"]) from exc

    if settings.auth == "oauth":
        from mcp.client.stdio import StdioServerParameters, stdio_client

        command, *argv = shlex.split(settings.command)
        transport = stdio_client(StdioServerParameters(command=command, args=argv))
    else:
        from mcp.client.streamable_http import streamablehttp_client

        transport = streamablehttp_client(
            settings.url,
            headers=settings.auth_headers(),
            timeout=timedelta(seconds=settings.timeout_s),
        )

    async with transport as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            if name == LIST_TOOLS:
                listed = await session.list_tools()
                return sorted(tool.name for tool in getattr(listed, "tools", []) or [])
            return await session.call_tool(name, args)


def parse_tool_result(result: Any) -> Any:
    """CallToolResult -> Python данни. isError -> JiraMcpError с текста на сървъра.

    Проверено: Atlassian връща JSON като текст в първия TextContent блок;
    грешките са {"error": true, "message": "..."}."""
    content = getattr(result, "content", None) or []
    texts = [getattr(block, "text", "") for block in content if getattr(block, "text", None)]
    text = texts[0] if texts else ""

    parsed: Any = None
    if text:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = text

    if getattr(result, "isError", False) or (isinstance(parsed, dict) and parsed.get("error") is True):
        message = parsed.get("message") if isinstance(parsed, dict) else None
        raise JiraMcpError(str(message or text or "MCP tool error"))

    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        return structured
    return parsed if parsed is not None else text


def _is_transient(exc: BaseException) -> bool:
    """Транспортна грешка (повтаряме) или детерминистична (не повтаряме)?"""
    if isinstance(exc, JiraMcpError):
        return False
    if isinstance(exc, ImportError):
        return False
    return True


# ---------------------------------------------------------------------------
# Клиент
# ---------------------------------------------------------------------------


class JiraMcpClient:
    """
    Малък sync клиент над MCP инструментите на Jira.

    call_tool е инжектируем (тестове/дубльори): (name, args) -> данни или
    JiraMcpError. По подразбиране - реална MCP сесия (виж _call_over_mcp).
    """

    def __init__(self, settings: JiraMcpSettings, call_tool: Callable[[str, dict], Any] | None = None, sleep=time.sleep):
        self.settings = settings
        self._call_tool = call_tool or self._real_call
        self._sleep = sleep

    def _real_call(self, name: str, args: dict) -> Any:
        result = _run_sync(lambda: _call_over_mcp(self.settings, name, args), self.settings.timeout_s)
        if name == LIST_TOOLS:
            return result  # вече е списък с имена
        return parse_tool_result(result)

    def list_tool_names(self) -> list[str]:
        """Имената на инструментите, които сървърът излага за тези креденшъли."""
        names = self.call(LIST_TOOLS, {})
        return [str(n) for n in (names or [])]

    def call(self, name: str, args: dict) -> Any:
        """Извикване с retry/backoff при транспортни грешки."""
        attempts = max(0, self.settings.max_retries) + 1
        for attempt in range(attempts):
            try:
                return self._call_tool(name, args)
            except Exception as exc:  # noqa: BLE001 - класифицираме по-долу
                if not _is_transient(exc) or attempt == attempts - 1:
                    if isinstance(exc, JiraMcpError):
                        raise
                    raise JiraMcpError(f"{type(exc).__name__}: {exc}") from exc
                self._sleep(1.0 * 2**attempt)
        raise JiraMcpError("unreachable")  # pragma: no cover

    # -- Jira операции ------------------------------------------------------

    def get_issue(self, key: str) -> dict:
        fields = [*ISSUE_FIELDS, *([self.settings.ac_field] if self.settings.ac_field else [])]
        raw = self.call(
            "getJiraIssue",
            {
                "cloudId": self.settings.cloud_id,
                "issueIdOrKey": key.strip().upper(),
                "fields": fields,
                "responseContentFormat": "markdown",
            },
        )
        return normalize_issue(raw, self.settings)

    def search(self, jql: str, max_results: int = 10) -> list[dict]:
        raw = self.call(
            "searchJiraIssuesUsingJql",
            {
                "cloudId": self.settings.cloud_id,
                "jql": jql,
                "maxResults": max_results,
                "fields": SEARCH_FIELDS,
                "responseContentFormat": "markdown",
            },
        )
        return [normalize_issue(node, self.settings) for node in _search_nodes(raw)]

    def add_comment(self, key: str, body_markdown: str) -> Any:
        return self.call(
            "addCommentToJiraIssue",
            {
                "cloudId": self.settings.cloud_id,
                "issueIdOrKey": key.strip().upper(),
                "commentBody": body_markdown,
                "contentFormat": "markdown",
            },
        )

    def test_connection(self) -> str:
        """
        Диагностика за UI-я/конзолата: свързване + кои Jira инструменти сървърът
        излага + достъпните сайтове. Проверено: с невалидни креденшъли сървърът
        приема сесията, но НЕ излага инструменти ("Tool ... not found") - затова
        първо list_tools, после getAccessibleAtlassianResources.
        """
        texts = pick(_TEXTS)
        names = self.list_tool_names()
        missing = [tool for tool in REQUIRED_TOOLS if tool not in names]
        if missing:
            raise JiraMcpError(texts["tools_missing"].format(missing=", ".join(missing), count=len(names)))

        lines = [texts["tools_ok"].format(count=len(names))]
        if "getAccessibleAtlassianResources" in names:
            resources = self.call("getAccessibleAtlassianResources", {})
            if isinstance(resources, dict):
                resources = resources.get("resources") or resources.get("values") or [resources]
            for res in resources or []:
                if not isinstance(res, dict):
                    continue
                scopes = res.get("scopes") or []
                jira = "jira" if any("jira" in str(s) for s in scopes) else "-"
                lines.append(f"{res.get('url', '?')} · {res.get('id', '?')} · {jira}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Нормализация - чисти функции, тестваеми офлайн
# ---------------------------------------------------------------------------


def _search_nodes(raw: Any) -> list:
    if isinstance(raw, dict):
        issues = raw.get("issues", raw.get("nodes", []))
        if isinstance(issues, dict):
            return list(issues.get("nodes") or [])
        return list(issues or [])
    return list(raw or [])


def adf_to_text(node: Any) -> str:
    """Защитен fallback: Atlassian Document Format -> обикновен текст."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return str(node)

    ntype = node.get("type", "")
    if ntype == "text":
        return node.get("text", "")
    if ntype == "hardBreak":
        return "\n"
    if ntype in ("mention", "inlineCard", "emoji"):
        attrs = node.get("attrs", {})
        return str(attrs.get("text") or attrs.get("url") or attrs.get("shortName") or "")

    inner = "".join(adf_to_text(child) for child in node.get("content", []))
    if ntype in ("paragraph", "heading"):
        return inner + "\n"
    if ntype == "listItem":
        return "- " + inner.rstrip("\n") + "\n"
    if ntype in ("bulletList", "orderedList", "codeBlock", "blockquote", "panel", "doc"):
        return inner + ("\n" if ntype != "doc" else "")
    return inner


def description_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return adf_to_text(value).strip()


_AC_HEADING_RE = re.compile(
    r"^\W*(acceptance\s+criteria|критерии\s+за\s+приемане|критерии\s+за\s+приемливост|ac)\W*$",
    re.IGNORECASE,
)
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")
_HEADING_RE = re.compile(r"^\s*(#{1,6}\s|\*\*.+\*\*\s*:?\s*$)")


def extract_acceptance_criteria(description: str, fields: dict | None = None, ac_field: str = "") -> list[str]:
    """
    Критериите за приемане: (1) custom field, ако е конфигуриран и непразен;
    (2) секцията под заглавие "Acceptance criteria" / "Критерии за приемане" в
    описанието; (3) празен списък (картата казва да се изведат от описанието).
    """
    if ac_field and fields:
        raw = description_to_text(fields.get(ac_field))
        if raw:
            items = [m.group(1).strip() for m in map(_LIST_ITEM_RE.match, raw.splitlines()) if m]
            return items or [line.strip() for line in raw.splitlines() if line.strip()]

    lines = (description or "").splitlines()
    for idx, line in enumerate(lines):
        if not _AC_HEADING_RE.match(line.strip().strip("*#:")):
            continue
        items: list[str] = []
        for following in lines[idx + 1:]:
            stripped = following.strip()
            if not stripped:
                if items:
                    break
                continue
            if _HEADING_RE.match(following) or _AC_HEADING_RE.match(stripped.strip("*#:")):
                break
            match = _LIST_ITEM_RE.match(following)
            items.append(match.group(1).strip() if match else stripped)
        if items:
            return items
    return []


def _field(node: dict, name: str) -> Any:
    fields = node.get("fields") if isinstance(node.get("fields"), dict) else {}
    return fields.get(name, node.get(name))


def _named(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("value") or "-")
    return str(value) if value not in (None, "") else "-"


def normalize_issue(raw: Any, settings: JiraMcpSettings) -> dict:
    """Суров MCP отговор (или search node) -> плоският речник, който картата ползва."""
    if not isinstance(raw, dict):
        raise JiraMcpError(f"Unexpected Jira payload: {type(raw).__name__}")
    fields = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
    key = str(raw.get("key") or raw.get("issueKey") or "")
    description = description_to_text(_field(raw, "description"))
    labels = _field(raw, "labels") or []
    return {
        "key": key,
        "summary": str(_field(raw, "summary") or "").strip(),
        "description": description,
        "priority": _named(_field(raw, "priority")),
        "status": _named(_field(raw, "status")),
        "issuetype": _named(_field(raw, "issuetype")),
        "labels": [str(label) for label in labels] if isinstance(labels, list) else [],
        "url": str(raw.get("webUrl") or settings.browse_url(key)),
        "acceptance_criteria": extract_acceptance_criteria(description, fields, settings.ac_field),
    }


# Двуезични текстове на prod инструментите (конвенция от src/tools.py)
_TEXTS = {
    "bg": {
        "no_criteria": "  - (тикетът няма изрични критерии за приемане - изведи ги от описанието)",
        "extra": "Статус: {status}\nТип: {issuetype}\nЛинк: {url}",
        "error": "Тикет '{ticket_id}': грешка от Jira: {message}",
        "search_error": "Търсене в Jira се провали: {message}",
        "unbounded_hint": " Подсказка: ограничи JQL с project = KEY, text ~ \"дума\" или updated >= -30d.",
        "search_empty": "Няма тикети за JQL: {jql}",
        "search_row": "{key} | {status} | {priority} | {summary}",
        "mcp_missing": "Пакетът 'mcp' не е инсталиран: pip install -r requirements.txt",
        "tools_ok": "Свързан с Atlassian MCP: {count} инструмента, Jira инструментите са налични.",
        "tools_missing": (
            "Свързан с Atlassian MCP, но Jira инструментите липсват: {missing} "
            "(сървърът излага {count} инструмента) - провери JIRA_EMAIL / JIRA_API_TOKEN и достъпа до Jira."
        ),
    },
    "en": {
        "no_criteria": "  - (the ticket has no explicit acceptance criteria - derive them from the description)",
        "extra": "Status: {status}\nType: {issuetype}\nLink: {url}",
        "error": "Ticket '{ticket_id}': Jira error: {message}",
        "search_error": "Jira search failed: {message}",
        "unbounded_hint": " Hint: bound the JQL with project = KEY, text ~ \"word\" or updated >= -30d.",
        "search_empty": "No tickets for JQL: {jql}",
        "search_row": "{key} | {status} | {priority} | {summary}",
        "mcp_missing": "The 'mcp' package is not installed: pip install -r requirements.txt",
        "tools_ok": "Connected to Atlassian MCP: {count} tools, the Jira tools are available.",
        "tools_missing": (
            "Connected to Atlassian MCP, but the Jira tools are missing: {missing} "
            "(the server exposes {count} tools) - check JIRA_EMAIL / JIRA_API_TOKEN and your Jira access."
        ),
    },
}


def format_ticket_card(issue: dict) -> str:
    """Същата карта като mock инструмента (Тикет/Заглавие/Приоритет/Описание/
    Критерии) + Статус/Тип/Линк - агентът вижда един и същи формат в двата режима."""
    lang = get_lang()
    criteria = "\n".join(f"  - {c}" for c in issue.get("acceptance_criteria") or []) or _TEXTS[lang]["no_criteria"]
    card = _TICKET_TEXTS[lang]["card"].format(
        ticket_id=issue.get("key", ""),
        title=issue.get("summary", ""),
        priority=issue.get("priority", "-"),
        description=issue.get("description", ""),
        criteria=criteria,
    )
    extra = _TEXTS[lang]["extra"].format(
        status=issue.get("status", "-"),
        issuetype=issue.get("issuetype", "-"),
        url=issue.get("url") or "-",
    )
    return f"{card}\n{extra}"


# ---------------------------------------------------------------------------
# Инструментите за Analyst (prod)
# ---------------------------------------------------------------------------


def make_jira_tools(client: JiraMcpClient) -> list:
    """get_ticket_details (същия контракт като mock-а) + search_tickets."""

    @tool("get_ticket_details", description=_demo_ticket_tool.description)
    def get_ticket_details(ticket_id: str) -> str:
        key = ticket_id.strip().upper()
        try:
            return format_ticket_card(client.get_issue(key))
        except JiraMcpError as exc:
            return pick(_TEXTS)["error"].format(ticket_id=key, message=exc)

    @tool
    def search_tickets(jql: str) -> str:
        """
        Търси тикети в Jira по JQL, когато задачата няма ключ на тикет. ВИНАГИ
        ограничавай заявката (напр. project = KEY AND text ~ "дума", или
        updated >= -30d) - неограничен JQL се отказва от сървъра. Връща до 10
        реда: ключ | статус | приоритет | заглавие.
        / Searches Jira issues by JQL when the task has no ticket key. ALWAYS
        bound the query (e.g. project = KEY AND text ~ "word", or updated >= -30d)
        - unbounded JQL is rejected. Returns up to 10 rows: key | status | priority | summary.
        """
        texts = pick(_TEXTS)
        try:
            issues = client.search(jql)
        except JiraMcpError as exc:
            message = str(exc)
            if "unbounded" in message.lower():
                message += texts["unbounded_hint"]
            return texts["search_error"].format(message=message)
        if not issues:
            return texts["search_empty"].format(jql=jql)
        return "\n".join(
            texts["search_row"].format(
                key=i["key"], status=i["status"], priority=i["priority"], summary=i["summary"]
            )
            for i in issues
        )

    return [get_ticket_details, search_tickets]
