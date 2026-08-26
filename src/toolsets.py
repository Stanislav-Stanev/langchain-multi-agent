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

import os
from dataclasses import dataclass, field

from src.i18n import pick, t
from src.modes import prod_repos, validate_prod_config, workspace_dir
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
    """Prod: Jira клиент (+ инструментите му), git workspace-и и publisher (PR 3)."""

    mode: str = "prod"
    repos: list = field(default_factory=list)
    jira: object = None
    jira_settings: object = None
    jira_tools: list = field(default_factory=list)
    workspaces: dict = field(default_factory=dict)
    workspace_view: object = None
    publisher: object = None
    repo_read_tools: list = field(default_factory=list)
    repo_write_tools: list = field(default_factory=list)
    repo_diff_tools: list = field(default_factory=list)
    prepare_workspaces: object = None  # (ticket_key, run_id) -> None; вика се от init_run


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

    # Lazy import: mcp пакетът (и git слоят) са нужни само тук, никога в demo режим.
    from src.jira_mcp import JiraMcpClient, JiraMcpSettings, make_jira_tools
    from src.publish import GhPublisher, Publisher
    from src.repo_workspace import MultiWorkspace, RepoWorkspace, branch_name, make_repo_tools

    settings = JiraMcpSettings.from_env()
    client = JiraMcpClient(settings)

    # Репозиториите: всички конфигурирани или само избраните в UI-я (по display име)
    all_refs = prod_repos()
    selected = [ref for ref in all_refs if repos is None or ref.display in set(repos)]
    base_branch = os.getenv("PROD_BASE_BRANCH", "main").strip() or "main"
    workspaces = {ref.display: RepoWorkspace(ref, workspace_dir(), base_branch=base_branch) for ref in selected}
    repo_tools = make_repo_tools(workspaces)
    author = (
        os.getenv("PROD_GIT_AUTHOR_NAME", "Multi-Bot").strip() or "Multi-Bot",
        os.getenv("PROD_GIT_AUTHOR_EMAIL", "noreply@multibot.local").strip() or "noreply@multibot.local",
    )
    publisher = Publisher(
        workspaces, GhPublisher(), jira=client, write_back=settings.write_back,
        author=author, base_branch=base_branch, ticket_url_for=settings.browse_url,
    )

    def prepare_workspaces(ticket_key: str, run_id: str) -> None:
        """Clone/fetch + чист branch за run-а във всеки избран репозиторий (вика се от init_run)."""
        branch = branch_name(ticket_key, run_id)
        for ws in workspaces.values():
            ws.prepare()
            ws.start_run(branch)

    return ProdContext(
        repos=[ref.display for ref in selected],
        jira=client,
        jira_settings=settings,
        jira_tools=make_jira_tools(client),
        workspaces=workspaces,
        workspace_view=MultiWorkspace(workspaces),
        publisher=publisher,
        repo_read_tools=repo_tools.read,
        repo_write_tools=repo_tools.write,
        repo_diff_tools=repo_tools.diff,
        prepare_workspaces=prepare_workspaces,
    )


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

    addenda = dict(pick(PROMPT_ADDENDA)[mode])
    if mode == "prod" and not (repo_write or repo_diff):
        # Преходен случай (до PR 3): Jira е реална, но git workspace още няма -
        # Developer/QA работят както в demo (код в ```python блок), за да не им
        # обещаваме инструменти, които не съществуват.
        demo_addenda = pick(PROMPT_ADDENDA)["demo"]
        addenda["developer"] = demo_addenda["developer"]
        addenda["qa"] = demo_addenda["qa"]
    return RoleToolset(tools=tools, prompt_addendum={role: addenda.get(role, "") for role in ROLES})
