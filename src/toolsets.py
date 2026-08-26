"""
Инструментите на агентите по режим (demo / prod) - „реални инструменти зад
същия интерфейс" (improvement.md §2.6).

Агентите и промптовете им НЕ знаят за режими: Analyst винаги вика
get_ticket_details(ticket_id), но в demo това е mock речник, а в prod -
Jira през Atlassian MCP. Тук е единственото място, което избира
имплементацията. Освен инструментите, режимът носи и кратък „addendum"
към промпта на всяка роля (как точно се предава кодът: като ```python блок
в demo или като записани файлове в prod).

Контекстът (ModeContext) държи външните обекти на prod режима - Jira
клиент, git workspace-и, publisher. Създава се ВЪТРЕ в build_graph()
(като LLM клиентите) и prod модулите се импортират lazy: demo режимът и
`import src.graph` работят без mcp пакета и без каквито и да е ключове.
"""

from dataclasses import dataclass, field

from src.i18n import pick, t
from src.modes import validate_prod_config
from src.plans import make_plan_tools
from src.tools import (
    check_code_syntax,
    get_coding_standards,
    get_ticket_details,
    run_test_checklist,
)

ROLES = ("analyst", "developer", "qa")


@dataclass
class DemoContext:
    """Demo: нищо външно."""

    mode: str = "demo"
    workspace_view = None
    publisher = None
    jira = None


@dataclass
class ProdContext:
    """Prod: Jira клиент, git workspace-и и publisher (PR 2 / PR 3)."""

    mode: str = "prod"
    repos: list = field(default_factory=list)
    jira: object = None
    workspaces: dict = field(default_factory=dict)
    workspace_view: object = None
    publisher: object = None


@dataclass
class RoleToolset:
    """Инструменти + добавка към промпта за всяка роля."""

    tools: dict[str, list]
    prompt_addendum: dict[str, str]


# Какво се добавя към базовия промпт на ролята според режима.
PROMPT_ADDENDA = {
    "bg": {
        "demo": {
            "analyst": "",
            "developer": (
                "Предаване на кода (demo режим): върни ЦЕЛИЯ код в един markdown блок "
                "```python ... ``` с кратко обяснение - оттам се извлича артефактът."
            ),
            "qa": (
                "Кодът за преглед (demo режим) е в разговора - в ```python блока на Developer."
            ),
        },
        "prod": {
            "analyst": (
                "Prod режим: тикетът се чете от реалната Jira. Ако задачата няма ключ на тикет, "
                "намери го със search_tickets (винаги ограничавай JQL с project/text/updated). "
                "Прегледай съответните файлове в репозитория (list_repo_files / read_repo_file / "
                "search_repo), за да е спецификацията вярна спрямо реалния код."
            ),
            "developer": (
                "Prod режим: работиш в клониран git репозиторий. Първо ЧЕТИ (list_repo_files, "
                "read_repo_file, search_repo), после променяй САМО нужните файлове с "
                "write_repo_file / delete_repo_file и провери резултата с get_change_diff. "
                "НЕ връщай кода в отговора - върни кратко описание на промените по файлове."
            ),
            "qa": (
                "Prod режим: промените са в git workspace-а - вземи ги с get_change_diff и "
                "чети засегнатите файлове с read_repo_file; проверявай синтаксиса на всеки "
                "променен .py файл."
            ),
        },
    },
    "en": {
        "demo": {
            "analyst": "",
            "developer": (
                "Delivering the code (demo mode): return ALL the code in a single markdown block "
                "```python ... ``` with a short explanation - the artifact is extracted from it."
            ),
            "qa": "The code under review (demo mode) is in the conversation - the Developer's ```python block.",
        },
        "prod": {
            "analyst": (
                "Prod mode: the ticket is read from the real Jira. If the task has no ticket key, "
                "find it with search_tickets (always bound the JQL with project/text/updated). "
                "Review the relevant repository files (list_repo_files / read_repo_file / "
                "search_repo) so the specification matches the real code."
            ),
            "developer": (
                "Prod mode: you work in a cloned git repository. READ first (list_repo_files, "
                "read_repo_file, search_repo), then change ONLY the necessary files with "
                "write_repo_file / delete_repo_file and verify with get_change_diff. "
                "Do NOT return the code in your answer - return a short per-file summary of the changes."
            ),
            "qa": (
                "Prod mode: the changes live in the git workspace - fetch them with get_change_diff and "
                "read the affected files with read_repo_file; syntax-check every changed .py file."
            ),
        },
    },
}


def make_mode_context(mode: str, repos=None):
    """
    Контекстът на режима - външните обекти, от които инструментите зависят.

    demo: нищо. prod: първо валидация на конфигурацията (ясна грешка, ако
    липсва), после lazy import на Jira/git модулите (идват в PR 2 / PR 3).
    """
    if mode == "demo":
        return DemoContext()

    problems = validate_prod_config(mode, None if repos is None else list(repos))
    if problems:
        raise RuntimeError(t("prod_problems_title") + "\n- " + "\n- ".join(problems))

    # PR 2 (Jira MCP) и PR 3 (git workspace, draft PR) закачат реалните обекти тук.
    raise RuntimeError(t("prod_problem_not_available"))


def make_tools(mode: str, ctx) -> RoleToolset:
    """Инструментите за всяка роля според режима + добавките към промптовете."""
    plan_tool, test_tool = make_plan_tools()

    if mode == "demo":
        tools = {
            "analyst": [get_ticket_details],
            "developer": [get_coding_standards, plan_tool],
            "qa": [check_code_syntax, run_test_checklist, test_tool],
        }
    else:
        # PR 2 / PR 3: Jira инструменти за analyst, repo инструменти за developer/qa
        jira_tools = list(getattr(ctx, "jira_tools", []) or [])
        repo_read = list(getattr(ctx, "repo_read_tools", []) or [])
        repo_write = list(getattr(ctx, "repo_write_tools", []) or [])
        repo_diff = list(getattr(ctx, "repo_diff_tools", []) or [])
        tools = {
            "analyst": [*jira_tools, *repo_read],
            "developer": [get_coding_standards, plan_tool, *repo_read, *repo_write, *repo_diff],
            "qa": [check_code_syntax, run_test_checklist, test_tool, *repo_read, *repo_diff],
        }

    addenda = pick(PROMPT_ADDENDA)[mode]
    return RoleToolset(tools=tools, prompt_addendum={role: addenda.get(role, "") for role in ROLES})
