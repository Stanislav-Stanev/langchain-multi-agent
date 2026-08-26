"""
Тестове за src/publish.py - draft PR публикуването с фалшив `gh` (FakeGhRunner
в conftest.py) и реален git срещу bare репозиторий; Jira коментарът - с
фалшивата Jira. Нищо не пипа мрежата.
"""

import pytest

from src import plans
from src.publish import (
    GhPublisher,
    Publisher,
    PublishError,
    build_jira_comment,
    build_pr_body,
    spec_summary,
)
from src.repo_workspace import RepoWorkspace
from src.run_tracker import RunTracker
from tests.conftest import FakeGhRunner, fake_jira_client, git, sample_plan, sample_test_plan


def make_state(tracker, **extra) -> dict:
    plan_doc = plans.apply_step_update(plans.new_plan_doc(sample_plan()), "S1", "done", "готово")
    test_doc = plans.apply_case_update(plans.new_test_plan_doc(sample_test_plan()), "T1", "passed")
    plans.save_plan(tracker, plan_doc, "DEV-101")
    plans.save_test_plan(tracker, test_doc, "DEV-101")
    return {
        "run_id": tracker.run_id, "run_dir": str(tracker.run_dir), "mode": "prod", "ticket_key": "DEV-101",
        "spec": "# Спецификация\n\nФункция validate_email(s) -> bool.\n- критерий 1",
        "plan": plan_doc, "test_plan": test_doc, "final_status": "APPROVED",
        "qa_verdict": {"status": "APPROVED", "issues": []},
        "hitl_decisions": [{"gate": "plan", "action": "approve", "by": "тест", "feedback": ""}],
        **extra,
    }


@pytest.fixture
def tracker(tmp_path):
    return RunTracker.start(tmp_path / "runs", "2026-08-26_10-00-00_DEV-101", mode="prod", ticket_key="DEV-101", task="t", lang="bg")


class TestPrBody:
    def test_body_contains_everything(self, tracker):
        state = make_state(tracker)
        body = build_pr_body(state, tracker, ["src/app.py"], "https://site/browse/DEV-101")
        assert body.startswith("## Multi-Bot · DEV-101")
        assert "[DEV-101](https://site/browse/DEV-101)" in body
        assert "- [x] **S1**" in body and "- [x] **T1**" in body  # плановете със статуси
        assert "| AC-1 |" in body and "✅ покрит" in body           # матрицата
        assert "- `src/app.py`" in body and "порта `plan`: **approve**" in body
        assert "draft PR" in body

    def test_spec_summary_and_fallback(self):
        assert spec_summary({"spec": "# Заглавие\n\nФункция validate_email(s) -> bool."}) == "Функция validate_email(s) -> bool."
        assert spec_summary({"spec": ""}) == "промени по тикета"

    def test_jira_comment(self, tracker):
        state = make_state(tracker)
        text = build_jira_comment(state, {"acme/demo": "https://github.com/acme/demo/pull/7"})
        assert "run `2026-08-26_10-00-00_DEV-101`" in text and "**APPROVED**" in text
        assert "- acme/demo: https://github.com/acme/demo/pull/7" in text and "done: 1" in text


class TestGhPublisher:
    def test_create_draft_pr_argv_and_url(self, tmp_path):
        runner = FakeGhRunner()
        url = GhPublisher(runner).create_draft_pr("acme/demo", tmp_path, "main", "multibot/x", "DEV-101: t", tmp_path / "body.md")
        assert url == "https://github.com/acme/demo/pull/7"
        argv = runner.gh_calls[0]
        assert argv[:4] == ["gh", "pr", "create", "--draft"]
        assert "--repo" in argv and "acme/demo" in argv and "--base" in argv and "--head" in argv and "--body-file" in argv

    def test_existing_pr_falls_back_to_view(self, tmp_path):
        runner = FakeGhRunner(existing=True)
        url = GhPublisher(runner).create_draft_pr("acme/demo", tmp_path, "main", "multibot/x", "t", tmp_path / "b.md")
        assert url == "https://github.com/acme/demo/pull/7"
        assert runner.gh_calls[1][1:3] == ["pr", "view"]

    def test_failure_raises_publish_error(self, tmp_path):
        with pytest.raises(PublishError, match="GraphQL"):
            GhPublisher(FakeGhRunner(fail=True)).create_draft_pr("acme/demo", tmp_path, "main", "h", "t", tmp_path / "b.md")

    def test_auth_status(self):
        ok, text = GhPublisher(FakeGhRunner()).auth_status()
        assert ok and "Logged in" in text


class TestPublisher:
    def _workspace(self, bare_repo, tmp_path, runner):
        ws = RepoWorkspace(bare_repo, tmp_path / "workspace", runner=runner)
        ws.prepare()
        ws.start_run("multibot/dev-101-t")
        return ws

    def test_publishes_changed_repo_and_skips_clean_one(self, bare_repo, tmp_path, tracker):
        runner = FakeGhRunner()
        changed = self._workspace(bare_repo, tmp_path, runner)
        changed.write_file("src/feature.py", "X = 1\n")
        clean = RepoWorkspace(bare_repo, tmp_path / "workspace2", runner=runner)
        clean.prepare()
        clean.start_run("multibot/dev-101-t2")
        pub = Publisher({"acme/demo": changed, "acme/clean": clean}, GhPublisher(runner), author=("Bot", "bot@x.com"))

        result = pub.publish(make_state(tracker), tracker)

        assert result["publish_status"] == "PUBLISHED" and result["publish_errors"] == []
        assert result["pr_urls"] == {"acme/demo": "https://github.com/acme/demo/pull/7"}
        assert "1 draft Pull Request" in result["reason"]
        assert len([c for c in runner.gh_calls if c[1:3] == ["pr", "create"]]) == 1
        # commit-ът е push-нат в origin, PR тялото е записано в run директорията
        log = git(["--git-dir", bare_repo.url, "log", "-1", "--format=%s%n%an", "multibot/dev-101-t"], cwd=tmp_path)
        assert log.startswith("DEV-101: Функция validate_email") and "Bot" in log
        assert (tracker.run_dir / "pr-body-acme__demo.md").exists()

    def test_gh_failure_is_reported_not_raised(self, bare_repo, tmp_path, tracker):
        runner = FakeGhRunner(fail=True)
        ws = self._workspace(bare_repo, tmp_path, runner)
        ws.write_file("src/feature.py", "X = 1\n")
        result = Publisher({"acme/demo": ws}, GhPublisher(runner)).publish(make_state(tracker), tracker)
        assert result["publish_status"] == "PUBLISH_FAILED" and result["pr_urls"] == {}
        assert result["publish_errors"][0].startswith("acme/demo: ") and "GraphQL" in result["reason"]

    def test_no_changes_is_skipped(self, bare_repo, tmp_path, tracker):
        runner = FakeGhRunner()
        ws = self._workspace(bare_repo, tmp_path, runner)
        result = Publisher({"acme/demo": ws}, GhPublisher(runner)).publish(make_state(tracker), tracker)
        assert result["publish_status"] == "SKIPPED" and runner.gh_calls == []

    def test_write_back_comments_only_when_enabled(self, bare_repo, tmp_path, tracker):
        runner = FakeGhRunner()
        ws = self._workspace(bare_repo, tmp_path, runner)
        ws.write_file("src/feature.py", "X = 1\n")
        jira = fake_jira_client()
        Publisher({"acme/demo": ws}, GhPublisher(runner), jira=jira, write_back=False).publish(make_state(tracker), tracker)
        assert jira.comments == []

        ws.write_file("src/feature2.py", "Y = 2\n")
        Publisher({"acme/demo": ws}, GhPublisher(runner), jira=jira, write_back=True).publish(make_state(tracker), tracker)
        assert len(jira.comments) == 1 and "pull/7" in jira.comments[0]["commentBody"]
        assert jira.comments[0]["issueIdOrKey"] == "DEV-101"

    def test_jira_failure_does_not_fail_the_publish(self, bare_repo, tmp_path, tracker):
        from src.jira_mcp import JiraMcpClient, JiraMcpError, JiraMcpSettings

        def boom(name, args):
            raise JiraMcpError("no permission")

        runner = FakeGhRunner()
        ws = self._workspace(bare_repo, tmp_path, runner)
        ws.write_file("src/feature.py", "X = 1\n")
        jira = JiraMcpClient(JiraMcpSettings(email="e", api_token="t"), call_tool=boom)
        result = Publisher({"acme/demo": ws}, GhPublisher(runner), jira=jira, write_back=True).publish(make_state(tracker), tracker)
        assert result["pr_urls"] and result["publish_status"] == "PUBLISH_FAILED"
        assert result["publish_errors"] == ["jira: no permission"]
