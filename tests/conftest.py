"""
Обща тестова инфраструктура (fixtures) за тестовете на Multi-Bot.

Стратегията на тестване (тестова пирамида):

    1. UNIT тестове        - инструменти, i18n, config, режими, планове,
                             проследяване, DoD политики, HITL механизъм.
    2. ИНТЕГРАЦИОННИ       - истинските ReAct агенти (create_agent) +
                             истинските инструменти, но със СКРИПТИРАН
                             фалшив LLM (без мрежа).
    3. E2E WORKFLOW тестове - целият граф от build_graph() с
                             детерминистични дубльори (вкл. HITL портите
                             с InMemorySaver и Command(resume=...)).
    4. EVAL тестове         - качеството на РЕАЛНИТЕ LLM решения
                             (tests/evals/, маркер 'eval', пускат се
                             отделно: pytest -m eval).

Ключов принцип: нива 1-3 НЕ викат реален LLM API и НЕ пипат мрежа/диск
извън tmp_path. Всичко LLM-зависимо се подменя с "дубльори" (test
doubles), които връщат предварително скриптирани отговори.

Архитектурата под тест (виж src/graph.py): супервайзорът прави
еднократен triage, после потокът е детерминистичен - analyst -> dev_plan
-> approve_plan -> developer -> qa_plan -> qa -> finalize, с DoD проверки,
rework цикъл по QAVerdict и Human-in-the-Loop порти. Дубльорите тук са:
triage решения, планове, тест-планове, QA присъди и работни агенти.
"""

import pytest
from langchain_core.messages import AIMessage

from src import graph as graph_module
from src import plans as plans_module
from src.graph import QAVerdict, SupervisorDecision
from src.plans import AcceptanceCriterion, ImplementationPlan, PlanStep


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Праща всеки тест към стойностите по подразбиране (език bg, demo режим,
    без HITL порти, run артефакти в tmp_path, стандартен rework лимит, без
    fallback модел и без checkpointer), независимо от средата на разработчика.
    Тестове, които проверяват друго, сами си задават стойностите с monkeypatch."""
    monkeypatch.delenv("APP_LANG", raising=False)
    monkeypatch.delenv("MAX_REWORK", raising=False)
    monkeypatch.delenv("MODEL_FALLBACK", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL_FALLBACK", raising=False)
    monkeypatch.delenv("CHECKPOINT_SQLITE_PATH", raising=False)
    monkeypatch.delenv("HITL_MAX_REVISIONS", raising=False)
    monkeypatch.delenv("HITL_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("PROD_REPOS", raising=False)
    for var in ("JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_MCP_AUTH"):
        monkeypatch.delenv(var, raising=False)
    # Demo режим, без порти, артефактите - в временна директория на теста
    monkeypatch.setenv("APP_MODE", "demo")
    monkeypatch.setenv("HITL_GATES", "")
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspace"))
    # Git изолация: никакви глобални/системни настройки на разработчика (gpg sign,
    # hooks, credential helpers) не бива да влияят на тестовете с реален git
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig-empty"))
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    for var in ("PROD_BASE_BRANCH", "PROD_GIT_AUTHOR_NAME", "PROD_GIT_AUTHOR_EMAIL", "JIRA_WRITE_BACK"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Дубльори (test doubles) за LLM-зависимите обекти
# ---------------------------------------------------------------------------


def decide(next_: str, reason: str = "тестово решение") -> SupervisorDecision:
    """Кратък помощник за построяване на triage решение на супервайзора."""
    return SupervisorDecision(next=next_, reason=reason)


def verdict(status: str, issues: list[str] | None = None) -> QAVerdict:
    """Кратък помощник за построяване на QA присъда."""
    return QAVerdict(status=status, issues=issues or [])


def sample_plan(steps: int = 2, criteria: int = 2, summary: str = "План: функция validate_email.") -> ImplementationPlan:
    """План за имплементация с N стъпки, всяка покрива по един критерий."""
    acs = [AcceptanceCriterion(id=f"AC-{i}", text=f"критерий {i}") for i in range(1, criteria + 1)]
    return ImplementationPlan(
        summary=summary,
        acceptance_criteria=acs,
        steps=[
            PlanStep(
                id=f"S{i}",
                title=f"стъпка {i}",
                files=["validators.py"],
                acceptance_criteria_refs=[acs[(i - 1) % len(acs)].id] if acs else [],
            )
            for i in range(1, steps + 1)
        ],
    )


def sample_test_plan(cases: int = 2, summary: str = "Тест-план: проверка по критерии.") -> plans_module.TestPlan:
    """Тест-план с N случая, сочещи AC-1, AC-2, ..."""
    return plans_module.TestPlan(
        summary=summary,
        cases=[
            plans_module.TestCase(
                id=f"T{i}", title=f"тест {i}", criterion_ref=f"AC-{i}",
                method="manual_review", expected="очакван резултат",
            )
            for i in range(1, cases + 1)
        ],
    )


EMPTY_PLAN = ImplementationPlan(summary="празен", acceptance_criteria=[], steps=[])
EMPTY_TEST_PLAN = plans_module.TestPlan(summary="празен", cases=[])


class _ScriptedLLM:
    """
    Базов скриптиран "LLM": връща отговорите един по един; когато остане
    само един, го ПОВТАРЯ до безкрай (нужно за тестовете на лимитите,
    при които агентът "циклира" един и същи отговор).

    Записва всяко получено повикване в self.calls, за да могат тестовете
    да проверят какво точно е видял моделът (system prompt, история).
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def invoke(self, payload):
        self.calls.append(payload)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


class FakeLLMFactory:
    """
    Дубльор на get_llm(role): графът вика .with_structured_output(схема)
    върху него - диспечираме по схемата към правилния скриптиран LLM
    (SupervisorDecision -> triage, QAVerdict -> присъди, ImplementationPlan
    -> планове, TestPlan -> тест-планове). Пазим схемите, за да проверим,
    че графът иска точно тях.
    """

    def __init__(self, supervisor_llm, verdict_llm, plan_llm=None, test_plan_llm=None):
        self.supervisor_llm = supervisor_llm
        self.verdict_llm = verdict_llm
        self.plan_llm = plan_llm or _ScriptedLLM([sample_plan()])
        self.test_plan_llm = test_plan_llm or _ScriptedLLM([sample_test_plan()])

    def with_structured_output(self, schema):
        by_schema = {
            SupervisorDecision: self.supervisor_llm,
            QAVerdict: self.verdict_llm,
            ImplementationPlan: self.plan_llm,
            plans_module.TestPlan: self.test_plan_llm,
        }
        llm = by_schema.get(schema)
        if llm is None:
            raise AssertionError(f"Неочаквана структурирана схема: {schema}")
        llm.requested_schema = schema
        return llm


class FakeWorkerAgent:
    """
    Дубльор на ReAct агент (резултата от create_agent).

    Графът вика agent.invoke({"messages": [...]}, context=RunCtx) и чете
    последното съобщение - имитираме точно този контракт. Отговорите се
    скриптират (списък), а последният се повтаря при нужда. side_effect(ctx)
    симулира работата на инструментите (напр. отмятане на стъпка от плана).
    """

    def __init__(self, outputs, side_effect=None):
        self.outputs = list(outputs)
        self.invocations = []  # историята, която агентът е получил
        self.contexts = []     # RunCtx, подаден при всяко извикване
        self.side_effect = side_effect

    def invoke(self, payload, **kwargs):
        self.invocations.append(list(payload["messages"]))
        ctx = kwargs.get("context")
        self.contexts.append(ctx)
        if self.side_effect is not None:
            self.side_effect(ctx)
        content = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        return {"messages": [AIMessage(content=content)]}


class GraphHarness:
    """Всичко нужно на един E2E тест, събрано на едно място."""

    def __init__(self, graph, supervisor, verdicts, workers, plans, test_plans, runs_dir):
        self.graph = graph
        self.supervisor = supervisor
        self.verdicts = verdicts
        self.workers = workers
        self.plans = plans
        self.test_plans = test_plans
        self.runs_dir = runs_dir

    def run_dirs(self):
        """Създадените run директории (обикновено една)."""
        return sorted(p for p in self.runs_dir.iterdir() if p.is_dir()) if self.runs_dir.exists() else []


# Валиден Developer отговор по подразбиране - покрива DoD проверката
# (```python блок + валиден синтаксис).
DEFAULT_DEV_OUTPUT = (
    "Ето кода:\n```python\n"
    'def validate_email(email: str) -> bool:\n'
    '    """Валидира имейл."""\n'
    '    if not email:\n'
    "        return False\n"
    '    return email.count("@") == 1\n'
    "```"
)


# ---------------------------------------------------------------------------
# Фабрика за "скриптиран" граф - целият workflow без нито едно LLM извикване
# ---------------------------------------------------------------------------


@pytest.fixture
def scripted_graph(monkeypatch, tmp_path):
    """
    Фабрика-fixture: строи ИСТИНСКИЯ граф (build_graph, реалните възли,
    DoD проверки, планове, проследяване и routing), но с подменени
    LLM-зависими съставки.

    Подмяната става върху имената В src.graph (а не в src.agents),
    защото build_graph() ползва точно тях. По подразбиране: demo режим,
    без HITL порти (hitl_gates=()), артефакти в tmp_path/runs.
    """

    def _build(
        decisions=("analyst",),
        verdicts=("APPROVED",),
        analyst_outputs=None,
        developer_outputs=None,
        qa_outputs=None,
        plans=None,
        test_plans=None,
        checkpointer=None,
        hitl_gates=(),
        mode="demo",
        developer_side_effect=None,
    ):
        supervisor = _ScriptedLLM(
            [decide(d) if isinstance(d, str) else d for d in decisions]
        )
        verdict_llm = _ScriptedLLM(
            [verdict(v) if isinstance(v, str) else v for v in verdicts]
        )
        plan_llm = _ScriptedLLM(list(plans) if plans else [sample_plan()])
        test_plan_llm = _ScriptedLLM(list(test_plans) if test_plans else [sample_test_plan()])
        workers = {
            "analyst": FakeWorkerAgent(
                analyst_outputs or ["Спецификация: функция validate_email(s) -> bool."]
            ),
            "developer": FakeWorkerAgent(
                developer_outputs or [DEFAULT_DEV_OUTPUT], side_effect=developer_side_effect
            ),
            "qa": FakeWorkerAgent(qa_outputs or ["Доклад: кодът покрива критериите."]),
        }

        factory = FakeLLMFactory(supervisor, verdict_llm, plan_llm, test_plan_llm)
        monkeypatch.setattr(
            graph_module,
            "get_llm",
            lambda role="default", model_override=None: factory,
        )
        monkeypatch.setattr(graph_module, "create_analyst", lambda *a, **k: workers["analyst"])
        monkeypatch.setattr(graph_module, "create_developer", lambda *a, **k: workers["developer"])
        monkeypatch.setattr(graph_module, "create_qa", lambda *a, **k: workers["qa"])

        runs_dir = tmp_path / "runs"
        return GraphHarness(
            graph_module.build_graph(
                checkpointer=checkpointer, mode=mode, runs_dir=runs_dir, hitl_gates=hitl_gates
            ),
            supervisor,
            verdict_llm,
            workers,
            plan_llm,
            test_plan_llm,
            runs_dir,
        )

    return _build


# ---------------------------------------------------------------------------
# Prod дубльори: локален bare git репозиторий, фалшив gh, фалшива Jira
# ---------------------------------------------------------------------------
# Git операциите са РЕАЛНИ (clone/branch/diff/commit/push срещу bare repo в
# tmp_path) - само мрежата (GitHub, Jira) е подменена.


def git(argv, cwd):
    """Помощник за тестовете: реален git с изолирана идентичност; гърми при грешка."""
    import subprocess

    result = subprocess.run(
        ["git", "-c", "user.name=Тест", "-c", "user.email=test@example.com", *argv],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, f"git {' '.join(argv)} failed: {result.stderr}"
    return result.stdout


SEED_FILES = {
    "README.md": "# Demo repo\n",
    "src/app.py": "def hello() -> str:\n    return 'hi'\n",
    ".env": "SECRET=1\n",
}


@pytest.fixture
def bare_repo(tmp_path):
    """Bare репозиторий с начална ревизия в main - играе ролята на GitHub origin."""
    from src.modes import RepoRef

    bare = tmp_path / "origin.git"
    git(["init", "--bare", "-b", "main", str(bare)], cwd=tmp_path)
    seed = tmp_path / "seed"
    git(["clone", "-q", str(bare), str(seed)], cwd=tmp_path)
    for rel, content in SEED_FILES.items():
        path = seed / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(["add", "-A"], cwd=seed)
    git(["commit", "-q", "-m", "seed"], cwd=seed)
    git(["push", "-q", "origin", "HEAD:main"], cwd=seed)
    return RepoRef(owner="acme", name="demo", url=str(bare))


class FakeGhRunner:
    """Runner, който пуска реалния git, а `gh` командите симулира (записва argv)."""

    def __init__(self, pr_url="https://github.com/acme/demo/pull/7", fail=False, existing=False):
        self.pr_url = pr_url
        self.fail = fail
        self.existing = existing
        self.gh_calls = []

    def __call__(self, argv, cwd=None, timeout=600, env_extra=None):
        import subprocess

        from src.repo_workspace import default_runner

        if argv[0] != "gh":
            return default_runner(argv, cwd=cwd, timeout=timeout, env_extra=env_extra)
        self.gh_calls.append(list(argv))
        if argv[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(argv, 0, "Logged in to github.com account tester\n", "")
        if argv[1:3] == ["pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, self.pr_url + "\n", "")
        if self.fail:
            return subprocess.CompletedProcess(argv, 1, "", "GraphQL: something went wrong")
        if self.existing:
            return subprocess.CompletedProcess(argv, 1, "", "a pull request for branch already exists")
        return subprocess.CompletedProcess(argv, 0, f"Creating draft pull request\n{self.pr_url}\n", "")


def fake_jira_client(issues=None):
    """JiraMcpClient с фалшив call_tool: речник ключ -> суров MCP отговор."""
    from src.jira_mcp import JiraMcpClient, JiraMcpError, JiraMcpSettings

    issues = issues or {
        "DEV-101": {
            "key": "DEV-101",
            "fields": {
                "summary": "Валидация на имейл",
                "description": "Описание.\n\n## Критерии за приемане\n- приема string\n- празен -> False",
                "priority": {"name": "High"}, "status": {"name": "To Do"}, "issuetype": {"name": "Story"},
            },
        }
    }
    comments = []

    def call_tool(name, args):
        if name == "getJiraIssue":
            key = args["issueIdOrKey"]
            if key not in issues:
                raise JiraMcpError(f"Issue {key} does not exist")
            return issues[key]
        if name == "addCommentToJiraIssue":
            comments.append(args)
            return {"id": str(len(comments))}
        if name == "searchJiraIssuesUsingJql":
            return {"issues": {"nodes": list(issues.values())}}
        raise JiraMcpError(f"unexpected tool {name}")

    client = JiraMcpClient(JiraMcpSettings(email="e@x.com", api_token="t", cloud_id="site.atlassian.net"), call_tool=call_tool)
    client.comments = comments  # за проверки в тестовете
    return client


@pytest.fixture
def prod_context(bare_repo, tmp_path, monkeypatch):
    """
    Фабрика за ProdContext от дубльорите: реален workspace над bare_repo,
    фалшива Jira, фалшив gh. Патчва graph_module.make_mode_context, така че
    scripted_graph(mode="prod") получава точно този контекст.
    """

    def _build(gh_runner=None, write_back=False, jira=None):
        from src.publish import GhPublisher, Publisher
        from src.repo_workspace import MultiWorkspace, RepoWorkspace, branch_name, make_repo_tools
        from src.toolsets import ProdContext

        runner = gh_runner or FakeGhRunner()
        ws = RepoWorkspace(bare_repo, tmp_path / "workspace", base_branch="main", runner=runner)
        workspaces = {bare_repo.display: ws}
        jira_client = jira or fake_jira_client()
        from src.jira_mcp import make_jira_tools

        repo_tools = make_repo_tools(workspaces)
        publisher = Publisher(workspaces, GhPublisher(runner), jira=jira_client, write_back=write_back)

        def prepare_workspaces(ticket_key, run_id):
            for w in workspaces.values():
                w.prepare()
                w.start_run(branch_name(ticket_key, run_id))

        ctx = ProdContext(
            repos=[bare_repo.display], jira=jira_client, jira_settings=jira_client.settings,
            jira_tools=make_jira_tools(jira_client), workspaces=workspaces,
            workspace_view=MultiWorkspace(workspaces), publisher=publisher,
            repo_read_tools=repo_tools.read, repo_write_tools=repo_tools.write, repo_diff_tools=repo_tools.diff,
            prepare_workspaces=prepare_workspaces,
        )
        ctx.runner = runner
        monkeypatch.setattr(graph_module, "make_mode_context", lambda mode, repos=None: ctx)
        return ctx

    return _build
