"""
Публикуване в prod режим: commit -> push -> draft Pull Request (+ Jira коментар).

Изпълнява се САМО от възела finalize (src/graph.py), само при QA APPROVED и
само след като човек е одобрил на портата approve_publish (или тя е
изключена изрично). Draft PR е съзнателен избор: промените стигат до хора за
ревю, но никой не ги влива автоматично.

Нищо тук не хвърля към графа: резултатът е речник с publish_status
(PUBLISHED | PUBLISH_FAILED | SKIPPED_BY_HUMAN), pr_urls, publish_errors и
човешка причина - run-ът винаги приключва с summary.json, дори публикуването
да се е провалило наполовина.

`gh` (GitHub CLI) е зад инжектируем runner - тестовете проверяват argv и
получават фалшив URL; реалният `gh pr create --draft` изисква `gh auth login`.
"""

import re
from pathlib import Path

from src import plans
from src.i18n import pick, t
from src.repo_workspace import Runner, WorkspaceError, default_runner

_PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


class PublishError(Exception):
    """Грешка при създаване на PR - publish() я превръща в текст."""


class GhPublisher:
    """Draft PR през GitHub CLI (`gh`)."""

    def __init__(self, runner: Runner = default_runner):
        self.runner = runner

    def auth_status(self) -> tuple[bool, str]:
        result = self.runner(["gh", "auth", "status"])
        text = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, text.strip()

    def create_draft_pr(self, repo_display: str, cwd: Path, base: str, head: str, title: str, body_file: Path) -> str:
        """`gh pr create --draft`; ако PR за branch-а вече съществува (resume) - връща неговия URL."""
        result = self.runner(
            ["gh", "pr", "create", "--draft", "--repo", repo_display, "--base", base, "--head", head,
             "--title", title, "--body-file", str(body_file)],
            cwd=cwd,
        )
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        match = _PR_URL_RE.search(output)
        if result.returncode == 0 and match:
            return match.group(0)
        if "already exists" in output:
            existing = self.runner(["gh", "pr", "view", head, "--repo", repo_display, "--json", "url", "-q", ".url"], cwd=cwd)
            match = _PR_URL_RE.search(existing.stdout or "")
            if existing.returncode == 0 and match:
                return match.group(0)
        raise PublishError(output.strip() or f"gh pr create failed (rc={result.returncode})")


# ---------------------------------------------------------------------------
# Текстове на PR тялото / Jira коментара (двуезични, като промптовете)
# ---------------------------------------------------------------------------

_MD = {
    "bg": {
        "title": "## Multi-Bot · {ticket}",
        "ticket": "**Тикет:** {ticket}",
        "run": "**Run:** `{run_id}` · режим {mode} · QA: {qa}",
        "spec": "### Спецификация (Analyst)",
        "plan": "### План за имплементация",
        "test_plan": "### Тест-план и резултати (QA)",
        "trace": "### Матрица на проследимост",
        "files": "### Променени файлове",
        "hitl": "### Human-in-the-Loop решения",
        "hitl_row": "- порта `{gate}`: **{action}** ({by}) {feedback}",
        "footer": "_Създадено автоматично от Multi-Bot (draft PR - изисква човешко ревю)._",
        "pr_title": "{ticket}: {summary}",
        "commit": "{ticket}: {summary}\n\nMulti-Bot run {run_id}",
        "comment": (
            "Multi-Bot приключи run `{run_id}` за {ticket} със статус **{status}**.\n\n"
            "Draft Pull Request{plural}:\n{links}\n\n"
            "План за имплементация: {plan_counts} · Тест-план: {test_counts}"
        ),
        "no_summary": "промени по тикета",
    },
    "en": {
        "title": "## Multi-Bot · {ticket}",
        "ticket": "**Ticket:** {ticket}",
        "run": "**Run:** `{run_id}` · mode {mode} · QA: {qa}",
        "spec": "### Specification (Analyst)",
        "plan": "### Implementation plan",
        "test_plan": "### Test plan and results (QA)",
        "trace": "### Traceability matrix",
        "files": "### Changed files",
        "hitl": "### Human-in-the-Loop decisions",
        "hitl_row": "- gate `{gate}`: **{action}** ({by}) {feedback}",
        "footer": "_Generated automatically by Multi-Bot (draft PR - requires human review)._",
        "pr_title": "{ticket}: {summary}",
        "commit": "{ticket}: {summary}\n\nMulti-Bot run {run_id}",
        "comment": (
            "Multi-Bot finished run `{run_id}` for {ticket} with status **{status}**.\n\n"
            "Draft Pull Request{plural}:\n{links}\n\n"
            "Implementation plan: {plan_counts} · Test plan: {test_counts}"
        ),
        "no_summary": "changes for the ticket",
    },
}


def _md(key: str, **kwargs) -> str:
    text = pick(_MD)[key]
    return text.format(**kwargs) if kwargs else text


def _details(title: str, body: str) -> str:
    body = (body or "").strip()
    if not body:
        return ""
    return f"<details><summary>{title}</summary>\n\n{body}\n\n</details>\n"


def _counts_text(doc: dict | None, kind: str) -> str:
    if not doc:
        return "-"
    counts = plans.counts(doc, kind)
    return " · ".join(f"{k}: {v}" for k, v in counts.items() if v)


def spec_summary(state: dict) -> str:
    """Първият съдържателен ред от спецификацията - за заглавие на PR/commit.

    Markdown заглавията ("# Спецификация") са само fallback: обикновено първият
    обикновен ред казва КАКВО се прави, а заглавието - че това е спецификация."""
    heading = ""
    for line in (state.get("spec") or "").splitlines():
        raw = line.strip()
        text = raw.lstrip("#*- ").strip()
        if len(text) <= 8:
            continue
        if raw.startswith("#"):
            heading = heading or text[:72]
            continue
        return text[:72]
    return heading or _md("no_summary")


def build_pr_body(state: dict, tracker, changed_files: list[str] | None = None, ticket_url: str = "") -> str:
    """Тялото на PR-а: тикет, spec, планове със статуси, матрица, файлове, HITL, run_id."""
    ticket = state.get("ticket_key") or "task"
    ticket_label = f"[{ticket}]({ticket_url})" if ticket_url else ticket
    qa = (state.get("qa_verdict") or {}).get("status", "-")
    plan_md = tracker.read_text(plans.PLAN_MD) or plans.render_implementation_plan(state.get("plan") or {}, ticket)
    test_md = tracker.read_text(plans.TEST_PLAN_MD) or plans.render_test_plan(state.get("test_plan") or {}, ticket)
    trace_md = plans.render_traceability(state.get("plan"), state.get("test_plan"))

    parts = [
        _md("title", ticket=ticket),
        "",
        _md("ticket", ticket=ticket_label),
        _md("run", run_id=state.get("run_id", ""), mode=state.get("mode", ""), qa=qa),
        "",
        _details(_md("spec"), state.get("spec", "")),
        _details(_md("plan"), plan_md),
        _details(_md("test_plan"), test_md),
        _details(_md("trace"), trace_md),
    ]
    if changed_files:
        parts += [_md("files"), "", *[f"- `{f}`" for f in changed_files], ""]
    decisions = state.get("hitl_decisions") or []
    if decisions:
        parts += [_md("hitl"), "", *[
            _md("hitl_row", gate=d.get("gate"), action=d.get("action"), by=d.get("by"), feedback=d.get("feedback") or "")
            for d in decisions
        ], ""]
    parts += ["", _md("footer"), ""]
    return "\n".join(parts)


def build_jira_comment(state: dict, pr_urls: dict) -> str:
    links = "\n".join(f"- {repo}: {url}" for repo, url in pr_urls.items()) or "-"
    return _md(
        "comment",
        run_id=state.get("run_id", ""),
        ticket=state.get("ticket_key") or "-",
        status=state.get("final_status") or "-",
        plural="s" if len(pr_urls) > 1 and pick(_MD) is _MD["en"] else ("-и" if len(pr_urls) > 1 else ""),
        links=links,
        plan_counts=_counts_text(state.get("plan"), "plan"),
        test_counts=_counts_text(state.get("test_plan"), "test"),
    )


class Publisher:
    """
    Оркестрира публикуването за всички репозитории с промени.

    workspaces: name -> RepoWorkspace (със start_run вече извикан за run-а);
    gh: GhPublisher; jira: JiraMcpClient или None; write_back: дали да
    коментира в тикета; author: (име, имейл) за commit-а.
    """

    def __init__(self, workspaces: dict, gh: GhPublisher, *, jira=None, write_back: bool = False,
                 author: tuple[str, str] = ("Multi-Bot", "noreply@multibot.local"), base_branch: str = "main",
                 ticket_url_for=None):
        self.workspaces = workspaces
        self.gh = gh
        self.jira = jira
        self.write_back = write_back
        self.author = author
        self.base_branch = base_branch
        self.ticket_url_for = ticket_url_for or (lambda key: "")

    def publish(self, state: dict, tracker) -> dict:
        """commit -> push -> draft PR за всеки репозиторий с промени; нищо не хвърля."""
        ticket = state.get("ticket_key") or "task"
        summary = spec_summary(state)
        pr_urls: dict[str, str] = {}
        errors: list[str] = []

        for name, ws in self.workspaces.items():
            try:
                if not ws.has_changes():
                    continue
                changed = ws.changed_files()
                ws.commit(_md("commit", ticket=ticket, summary=summary, run_id=state.get("run_id", "")), *self.author)
                ws.push()
                body_path = tracker.write_text(
                    f"pr-body-{ws.repo.slug}.md",
                    build_pr_body(state, tracker, changed, self.ticket_url_for(ticket)),
                ) or (Path(tracker.run_dir or ".") / f"pr-body-{ws.repo.slug}.md")
                pr_urls[name] = self.gh.create_draft_pr(
                    ws.repo.display, ws.path, self.base_branch, ws.branch or "", _md("pr_title", ticket=ticket, summary=summary), Path(body_path),
                )
            except (WorkspaceError, PublishError, OSError) as exc:
                errors.append(f"{name}: {exc}")

        if self.write_back and self.jira is not None and pr_urls and state.get("ticket_key"):
            try:
                self.jira.add_comment(state["ticket_key"], build_jira_comment({**state, "pr_urls": pr_urls}, pr_urls))
            except Exception as exc:  # noqa: BLE001 - коментарът е допълнение, не бива да проваля run-а
                errors.append(f"jira: {exc}")

        if not pr_urls and not errors:
            status, reason = "SKIPPED", t("route_publish_skipped")
        elif errors:
            status, reason = "PUBLISH_FAILED", t("route_publish_failed", errors="; ".join(errors))
        else:
            status, reason = "PUBLISHED", t("route_published", n=len(pr_urls))
        return {"pr_urls": pr_urls, "publish_errors": errors, "publish_status": status, "reason": reason}
