"""
Тестове за src/repo_workspace.py - реален git срещу локален bare репозиторий
(fixture bare_repo в conftest.py), без мрежа.

Покриваме: clone/fetch идемпотентност, чист branch на run, четене/търсене,
писане -> diff -> commit -> push (виден в bare repo), path guard матрицата,
лимити, бинарни файлове, MultiWorkspace и инструментите (грешки като текст).
"""

import os

import pytest

from src.repo_workspace import (
    MultiWorkspace,
    RepoWorkspace,
    WorkspaceError,
    branch_name,
    make_repo_tools,
)
from tests.conftest import git


@pytest.fixture
def ws(bare_repo, tmp_path):
    w = RepoWorkspace(bare_repo, tmp_path / "workspace")
    w.prepare()
    w.start_run("multibot/dev-101-test")
    return w


class TestLifecycle:
    def test_prepare_clones_then_fetches(self, bare_repo, tmp_path):
        w = RepoWorkspace(bare_repo, tmp_path / "workspace")
        w.prepare()
        assert (w.path / ".git").exists() and (w.path / "README.md").exists()
        w.prepare()  # втори път -> fetch, не clone; не гърми
        assert w.path == tmp_path / "workspace" / "acme__demo"

    def test_start_run_gives_clean_branch_from_main(self, ws):
        (ws.path / "junk.txt").write_text("boklu", encoding="utf-8")
        ws.start_run("multibot/dev-101-again")
        assert not (ws.path / "junk.txt").exists()  # clean -fdx
        assert git(["branch", "--show-current"], cwd=ws.path).strip() == "multibot/dev-101-again"
        assert ws.branch == "multibot/dev-101-again"

    def test_branch_name(self):
        assert branch_name("DEV-101", "2026-08-26_10-44-12_DEV-101_f615573d") == "multibot/dev-101-101_f615573d"
        assert branch_name("", "").startswith("multibot/task-")


class TestReadOnly:
    def test_list_files_all_and_pattern(self, ws):
        assert ws.list_files() == [".env", "README.md", "src/app.py"]
        assert ws.list_files("*.py") == ["src/app.py"]
        assert ws.list_files("src/*.py") == ["src/app.py"]
        assert ws.list_files("*.rs") == []

    def test_read_file(self, ws):
        assert ws.read_file("src/app.py").startswith("def hello()")
        assert ws.read_file("src\\app.py").startswith("def hello()")  # Windows разделител

    def test_search(self, ws):
        rows = ws.search("hello")
        assert rows == ["src/app.py:1:def hello() -> str:"]
        assert ws.search("няма-такова") == []
        assert ws.search("hello", "*.md") == []

    def test_syntax_error(self, ws):
        assert ws.syntax_error("src/app.py") is None
        ws.write_file("src/bad.py", "def f(:\n")
        assert "line 1" in ws.syntax_error("src/bad.py")


class TestPathGuard:
    @pytest.mark.parametrize(
        "rel",
        ["", "../outside.txt", "src/../../x", "/etc/passwd", "C:\\Windows\\x", "\\\\server\\share", ".git/config", "src/.git/HEAD"],
    )
    def test_rejects_escapes_and_git(self, ws, rel):
        with pytest.raises(WorkspaceError):
            ws.read_file(rel)

    @pytest.mark.parametrize("rel", [".env", "config/.env.prod", "certs/server.pem", "id_rsa", "my-secret.txt", "db_credentials.json"])
    def test_rejects_secrets(self, ws, rel):
        with pytest.raises(WorkspaceError, match="забранен"):
            ws.write_file(rel, "x")

    @pytest.mark.skipif(os.name == "nt", reason="symlink изисква права на Windows")
    def test_rejects_symlink(self, ws, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        (ws.path / "link.txt").symlink_to(outside)
        with pytest.raises(WorkspaceError, match="Symlink"):
            ws.read_file("link.txt")

    def test_missing_file_and_limits(self, ws):
        with pytest.raises(WorkspaceError, match="не съществува"):
            ws.read_file("nope.py")
        small = RepoWorkspace(ws.repo, ws.root, max_file_bytes=10)
        with pytest.raises(WorkspaceError, match="твърде голям"):
            small.read_file("src/app.py")
        with pytest.raises(WorkspaceError, match="твърде голям"):
            small.write_file("big.txt", "x" * 100)

    def test_binary_is_refused(self, ws):
        (ws.path / "img.bin").write_bytes(b"\x89PNG\x00\x00binary")
        with pytest.raises(WorkspaceError, match="Бинарен"):
            ws.read_file("img.bin")


class TestWriteDiffCommitPush:
    def test_write_diff_changed_files_commit_push(self, ws, bare_repo):
        assert not ws.has_changes()
        ws.write_file("src/new_module.py", "X = 1\n")
        ws.write_file("src/app.py", "def hello() -> str:\n    return 'hello'\n")
        assert ws.has_changes()
        assert ws.changed_files() == ["src/app.py", "src/new_module.py"]
        diff = ws.diff()
        assert "diff --git a/src/new_module.py" in diff and "+    return 'hello'" in diff

        sha = ws.commit("DEV-101: промени", "Multi-Bot", "bot@example.com")
        assert len(sha) == 40 and not ws.has_changes()
        ws.push()
        # Branch-ът е виден в origin (bare repo) със същия sha
        remote = git(["--git-dir", bare_repo.url, "rev-parse", "multibot/dev-101-test"], cwd=ws.root).strip()
        assert remote == sha
        log = git(["--git-dir", bare_repo.url, "log", "-1", "--format=%an <%ae>", "multibot/dev-101-test"], cwd=ws.root).strip()
        assert log == "Multi-Bot <bot@example.com>"

    def test_delete_file(self, ws):
        ws.delete_file("README.md")
        assert ws.changed_files() == []  # изтриване не е ACMR
        assert "deleted file" in ws.diff()
        with pytest.raises(WorkspaceError):
            ws.delete_file("README.md")

    def test_push_without_branch_fails_clearly(self, bare_repo, tmp_path):
        w = RepoWorkspace(bare_repo, tmp_path / "workspace")
        w.prepare()
        with pytest.raises(WorkspaceError, match="start_run"):
            w.push()

    def test_git_failure_is_workspace_error(self, ws):
        with pytest.raises(WorkspaceError, match="git checkout"):
            ws.start_run("bad name with spaces")


class TestMultiWorkspace:
    def test_aggregates_changes_across_repos(self, ws):
        multi = MultiWorkspace({"acme/demo": ws})
        assert multi.changed_files() == [] and multi.diff() == "" and multi.with_changes() == {}
        ws.write_file("src/bad.py", "def f(:\n")
        assert multi.changed_files() == ["acme/demo:src/bad.py"]
        assert "line 1" in multi.syntax_error("acme/demo:src/bad.py")
        assert multi.syntax_error("other/repo:x.py") is None
        assert multi.diff().startswith("# repo: acme/demo\n")
        assert list(multi.with_changes()) == ["acme/demo"]


class TestRepoTools:
    def test_tools_read_write_diff_and_errors_as_text(self, ws):
        tools = make_repo_tools({"acme/demo": ws})
        names = [t.name for t in [*tools.read, *tools.write, *tools.diff]]
        assert names == ["list_repo_files", "read_repo_file", "search_repo", "write_repo_file", "delete_repo_file", "get_change_diff"]
        list_files, read_file, search = tools.read
        write_file, delete_file = tools.write
        (get_diff,) = tools.diff

        assert "src/app.py" in list_files.invoke({"repo": "acme/demo", "pattern": "*.py"})
        assert read_file.invoke({"repo": "demo", "path": "src/app.py"}).startswith("def hello")  # уникално кратко име
        assert "src/app.py:1" in search.invoke({"repo": "acme/demo", "pattern": "hello", "glob": ""})
        assert get_diff.invoke({"repo": "acme/demo"}) == "Няма промени в acme/demo."

        assert write_file.invoke({"repo": "acme/demo", "path": "src/x.py", "content": "y = 2\n"}).startswith("Записан: acme/demo:src/x.py")
        assert "diff --git a/src/x.py" in get_diff.invoke({"repo": "acme/demo"})
        assert delete_file.invoke({"repo": "acme/demo", "path": "src/x.py"}) == "Изтрит: acme/demo:src/x.py"

        # Грешките са текст, не изключения
        assert read_file.invoke({"repo": "acme/demo", "path": "../secret"}).startswith("Грешка (acme/demo)")
        assert write_file.invoke({"repo": "acme/demo", "path": ".env", "content": "x"}).startswith("Грешка")
        assert "Непознат репозиторий" in read_file.invoke({"repo": "nope/nope", "path": "README.md"})
        assert list_files.invoke({"repo": "acme/demo", "pattern": "*.rs"}) == "Няма файлове по шаблон '*.rs' в acme/demo."

    def test_english_texts(self, ws, monkeypatch):
        monkeypatch.setenv("APP_LANG", "en")
        tools = make_repo_tools({"acme/demo": ws})
        assert tools.read[1].invoke({"repo": "acme/demo", "path": "nope"}).startswith("Error (acme/demo)")
