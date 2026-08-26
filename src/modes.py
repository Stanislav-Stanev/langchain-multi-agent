"""
Режими на работа на Multi-Bot: demo и prod.

    demo -> учебният режим: mock тикети (src/tools.py), кодът живее само в
            отговора на Developer, нищо не се пипа извън runs/.
    prod -> реалният режим: тикетите идват от Jira (официалния Atlassian MCP
            сървър), Developer работи в клонирани git репозитории, а след
            одобрение от QA и от човек се отваря draft Pull Request.

Режимът се чете от средата (APP_MODE) при ВСЯКО извикване - като
LLM_PROVIDER и APP_LANG - за да може UI-ят да го сменя без рестарт.
По подразбиране е prod: системата е предназначена за реална работа, а
demo е изричен избор за обучение/демонстрация.

Невалидна стойност е ГРЕШКА, не тиха подмяна: ако при typo паднем към
prod, ще пуснем реални странични ефекти (MCP извиквания, push към
репозиторий), които никой не е искал.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from src.i18n import t

MODES = ("demo", "prod")
DEFAULT_MODE = "prod"


def get_mode() -> str:
    """Текущият режим (APP_MODE), по подразбиране prod; невалидно -> ValueError."""
    raw = os.getenv("APP_MODE", DEFAULT_MODE).strip().lower()
    if raw not in MODES:
        raise ValueError(f"Непознат APP_MODE: {raw!r}. Валидни стойности: {', '.join(MODES)}.")
    return raw


# Колко пъти QA може да върне задачата на Developer, преди системата да
# ескалира към човек. Чете се от средата при всяко решение (не при
# import), за да е конфигурируемо без рестарт - като LLM_PROVIDER.
DEFAULT_MAX_REWORK = 3


def max_rework() -> int:
    """Лимитът на поправките (MAX_REWORK от средата, по подразбиране 3)."""
    try:
        return int(os.getenv("MAX_REWORK", DEFAULT_MAX_REWORK))
    except ValueError:
        return DEFAULT_MAX_REWORK


def runs_dir() -> Path:
    """Коренът на run артефактите (RUNS_DIR, по подразбиране 'runs')."""
    return Path(os.getenv("RUNS_DIR", "runs").strip() or "runs")


def workspace_dir() -> Path:
    """Коренът на локалните клонове на репозиториите (WORKSPACE_DIR)."""
    return Path(os.getenv("WORKSPACE_DIR", "workspace").strip() or "workspace")


# ---------------------------------------------------------------------------
# Репозитории за prod режима
# ---------------------------------------------------------------------------

# "owner/name" или пълен https URL към GitHub (с или без .git накрая)
_GITHUB_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$"
)
_SHORT_RE = re.compile(r"^(?P<owner>[\w.-]+)/(?P<name>[\w.-]+)$")


@dataclass(frozen=True)
class RepoRef:
    """Референция към един git репозиторий (owner/name + URL за клониране)."""

    owner: str
    name: str
    url: str

    @property
    def display(self) -> str:
        """Кратко човешко име: owner/name."""
        return f"{self.owner}/{self.name}"

    @property
    def slug(self) -> str:
        """Име, безопасно за директория: owner__name."""
        return f"{self.owner}__{self.name}"

    @classmethod
    def parse(cls, spec: str) -> "RepoRef":
        """Приема 'owner/name' или https://github.com/owner/name[.git]."""
        spec = spec.strip()
        match = _GITHUB_URL_RE.match(spec) or _SHORT_RE.match(spec)
        if not match:
            raise ValueError(
                f"Невалиден репозиторий: {spec!r}. Очаква се 'owner/name' или GitHub URL."
            )
        owner, name = match.group("owner"), match.group("name")
        return cls(owner=owner, name=name, url=f"https://github.com/{owner}/{name}.git")


def parse_prod_repos(value: str) -> list[RepoRef]:
    """Списък репозитории от PROD_REPOS (със запетаи); празни и дубликати се махат."""
    refs: list[RepoRef] = []
    seen: set[str] = set()
    for item in (value or "").split(","):
        if not item.strip():
            continue
        ref = RepoRef.parse(item)
        if ref.display not in seen:
            seen.add(ref.display)
            refs.append(ref)
    return refs


def prod_repos() -> list[RepoRef]:
    """Конфигурираните репозитории от средата (PROD_REPOS)."""
    return parse_prod_repos(os.getenv("PROD_REPOS", ""))


# ---------------------------------------------------------------------------
# Валидация на prod конфигурацията - за UI-я (Run бутонът) и конзолата
# ---------------------------------------------------------------------------

# Кои променливи са нужни за всеки вид Jira автентикация (виж src/jira_mcp.py)
_JIRA_REQUIRED_BY_AUTH = {
    "api_token": ("JIRA_EMAIL", "JIRA_API_TOKEN"),
    "oauth": (),
}


def missing_jira_settings() -> list[str]:
    """Липсващите Jira променливи за избраната автентикация (JIRA_MCP_AUTH)."""
    auth = os.getenv("JIRA_MCP_AUTH", "api_token").strip().lower()
    required = _JIRA_REQUIRED_BY_AUTH.get(auth)
    if required is None:
        return ["JIRA_MCP_AUTH"]
    return [var for var in required if not os.getenv(var, "").strip()]


def validate_prod_config(mode: str, selected_repos: list[str] | None = None) -> list[str]:
    """
    Човешки описания на проблемите, които пречат на prod run.

    Празен списък = всичко е наред. В demo режим няма какво да се проверява.
    Ползва се от app.py (показва грешките и забранява Run) и main.py.
    """
    if mode != "prod":
        return []

    problems: list[str] = []
    repos = prod_repos()
    if not repos:
        problems.append(t("prod_problem_no_repos"))
    elif selected_repos is not None and not selected_repos:
        problems.append(t("prod_problem_no_repos_selected"))

    missing = missing_jira_settings()
    if missing:
        problems.append(t("prod_problem_jira_missing", vars=", ".join(missing)))

    return problems
