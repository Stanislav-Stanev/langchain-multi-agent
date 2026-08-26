"""
Unit тестове за src/modes.py - режимите demo/prod и prod конфигурацията.

Ключовото правило: невалиден APP_MODE е грешка, не тиха подмяна към prod
(prod има реални странични ефекти - MCP извиквания, push към репозиторий).
"""

from pathlib import Path

import pytest

from src.modes import (
    RepoRef,
    get_mode,
    max_rework,
    missing_jira_settings,
    parse_prod_repos,
    prod_repos,
    runs_dir,
    validate_prod_config,
    workspace_dir,
)


class TestGetMode:
    def test_default_is_prod(self, monkeypatch):
        monkeypatch.delenv("APP_MODE", raising=False)
        assert get_mode() == "prod"

    @pytest.mark.parametrize("raw", ["demo", " DEMO ", "Demo"])
    def test_normalizes_case_and_whitespace(self, monkeypatch, raw):
        monkeypatch.setenv("APP_MODE", raw)
        assert get_mode() == "demo"

    def test_invalid_value_raises(self, monkeypatch):
        # Typo НЕ бива да пуска prod страничните ефекти - ясна грешка вместо това
        monkeypatch.setenv("APP_MODE", "production")
        with pytest.raises(ValueError, match="APP_MODE"):
            get_mode()


class TestDirectories:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("RUNS_DIR", raising=False)
        monkeypatch.delenv("WORKSPACE_DIR", raising=False)
        assert runs_dir() == Path("runs")
        assert workspace_dir() == Path("workspace")

    def test_env_overrides(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RUNS_DIR", str(tmp_path / "r"))
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "w"))
        assert runs_dir() == tmp_path / "r"
        assert workspace_dir() == tmp_path / "w"


class TestMaxRework:
    def test_default_is_three(self):
        assert max_rework() == 3

    def test_reads_env(self, monkeypatch):
        monkeypatch.setenv("MAX_REWORK", "5")
        assert max_rework() == 5

    def test_invalid_value_falls_back(self, monkeypatch):
        monkeypatch.setenv("MAX_REWORK", "много")
        assert max_rework() == 3


class TestRepoRef:
    @pytest.mark.parametrize(
        "spec",
        [
            "owner/name",
            "https://github.com/owner/name",
            "https://github.com/owner/name.git",
            "https://www.github.com/owner/name/",
        ],
    )
    def test_parses_short_and_url_forms(self, spec):
        ref = RepoRef.parse(spec)
        assert (ref.owner, ref.name) == ("owner", "name")
        assert ref.url == "https://github.com/owner/name.git"
        assert ref.display == "owner/name"
        assert ref.slug == "owner__name"

    @pytest.mark.parametrize("spec", ["", "just-a-name", "https://gitlab.com/a/b", "a/b/c"])
    def test_rejects_invalid(self, spec):
        with pytest.raises(ValueError):
            RepoRef.parse(spec)

    def test_parse_prod_repos_dedupes_and_skips_blanks(self):
        refs = parse_prod_repos("a/b, https://github.com/a/b.git ,, c/d")
        assert [r.display for r in refs] == ["a/b", "c/d"]

    def test_prod_repos_reads_env(self, monkeypatch):
        monkeypatch.setenv("PROD_REPOS", "x/y")
        assert [r.display for r in prod_repos()] == ["x/y"]
        monkeypatch.delenv("PROD_REPOS")
        assert prod_repos() == []


class TestProdConfigValidation:
    def test_demo_has_no_problems(self):
        assert validate_prod_config("demo") == []

    def test_reports_missing_repos_and_jira(self, monkeypatch):
        monkeypatch.delenv("PROD_REPOS", raising=False)
        problems = validate_prod_config("prod")
        assert any("PROD_REPOS" in p for p in problems)
        assert any("JIRA_EMAIL" in p and "JIRA_API_TOKEN" in p for p in problems)

    def test_no_selected_repo_is_a_problem(self, monkeypatch):
        monkeypatch.setenv("PROD_REPOS", "a/b")
        monkeypatch.setenv("JIRA_EMAIL", "e@x.com")
        monkeypatch.setenv("JIRA_API_TOKEN", "tok")
        assert validate_prod_config("prod", selected_repos=[]) != []
        assert validate_prod_config("prod", selected_repos=["a/b"]) == []

    def test_oauth_needs_no_credentials(self, monkeypatch):
        monkeypatch.setenv("JIRA_MCP_AUTH", "oauth")
        assert missing_jira_settings() == []

    def test_unknown_auth_is_reported(self, monkeypatch):
        monkeypatch.setenv("JIRA_MCP_AUTH", "magic")
        assert missing_jira_settings() == ["JIRA_MCP_AUTH"]

    def test_english_messages(self, monkeypatch):
        monkeypatch.setenv("APP_LANG", "en")
        assert any("No repositories" in p for p in validate_prod_config("prod"))
