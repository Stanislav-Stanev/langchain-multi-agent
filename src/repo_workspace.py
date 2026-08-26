"""
Git workspace за prod режима - Developer и QA работят в клонирани репозитории.

Идеята: вместо кодът да живее само в отговора на Developer (demo), агентът
ЧЕТЕ реалния репозиторий и ПИШЕ в него през инструменти, а артефактът на
фазата е реалният git diff. Всичко минава през обикновени git команди
(subprocess, без GitPython) с инжектируем runner - тестовете ползват реален
локален bare репозиторий и никога мрежа.

Безопасност (агентът е LLM - третираме входа му като недоверен):
  - всеки път се резолвира и трябва да остане ВЪТРЕ в workspace-а
    (без абсолютни пътища, без "..", без symlink-ове, без .git);
  - deny-list за тайни (.env*, ключове, сертификати);
  - лимит на размера на файловете и на броя резултати;
  - бинарни файлове не се четат;
  - GIT_TERMINAL_PROMPT=0 - липсващи креденшъли водят до бърза грешка, не до
    висящ процес.

Един run наведнъж на workspace директория (Streamlit има една script нишка).
"""

import ast
import fnmatch
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from langchain_core.tools import tool

from src.i18n import pick
from src.modes import RepoRef

Runner = Callable[..., subprocess.CompletedProcess]

DENY_GLOBS = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "*secret*", "*credential*")
DEFAULT_MAX_FILE_BYTES = 200_000
DEFAULT_MAX_LIST = 500
DEFAULT_MAX_SEARCH_LINES = 200


class WorkspaceError(Exception):
    """Грешка от git/файловата система - инструментите я връщат като текст."""


def default_runner(argv: list[str], cwd: Path | str | None = None, timeout: float = 600, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """subprocess.run с настройките, които ни трябват за git/gh (utf-8, без prompt-ове)."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env_extra or {})}
    return subprocess.run(  # noqa: S603 - argv е списък, shell=False
        argv,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        shell=False,
    )


class RepoWorkspace:
    """Един клониран репозиторий: lifecycle, четене/писане с guard, diff, commit, push."""

    def __init__(
        self,
        repo: RepoRef,
        root: Path | str,
        base_branch: str = "main",
        runner: Runner = default_runner,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_list: int = DEFAULT_MAX_LIST,
    ):
        self.repo = repo
        self.root = Path(root)
        self.path = self.root / repo.slug
        self.base_branch = base_branch
        self.runner = runner
        self.max_file_bytes = max_file_bytes
        self.max_list = max_list
        self.branch: str | None = None

    # -- git помощници ----------------------------------------------------------

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
        result = self.runner(["git", *args], cwd=cwd or self.path)
        if check and result.returncode != 0:
            raise WorkspaceError(f"git {' '.join(args[:2])} ({self.repo.display}): {(result.stderr or result.stdout).strip()}")
        return result

    # -- lifecycle (единствените операции с мрежа: clone / fetch / push) --------

    def prepare(self) -> None:
        """Клонира при първи път, иначе fetch - workspace-ът е кеш между run-овете."""
        if (self.path / ".git").exists():
            self._git("fetch", "origin", "--prune")
            return
        self.root.mkdir(parents=True, exist_ok=True)
        result = self.runner(["git", "clone", "-c", "core.autocrlf=false", self.repo.url, str(self.path)], cwd=self.root)
        if result.returncode != 0:
            raise WorkspaceError(f"git clone ({self.repo.display}): {(result.stderr or result.stdout).strip()}")

    def start_run(self, branch: str) -> None:
        """Чисто работно дърво на нов branch от origin/<base> - всеки run започва от нулата."""
        self._git("checkout", "-B", branch, f"origin/{self.base_branch}")
        self._git("reset", "--hard")
        self._git("clean", "-fdx")
        self.branch = branch

    # -- guard ------------------------------------------------------------------

    def _resolve(self, rel: str) -> Path:
        """Резолвира относителен път и отказва всичко, което излиза от workspace-а."""
        texts = pick(_WS_TEXTS)
        raw = (rel or "").strip().replace("\\", "/")
        if not raw:
            raise WorkspaceError(texts["path_empty"])
        if raw.startswith("/") or raw.startswith("//") or (len(raw) > 1 and raw[1] == ":"):
            raise WorkspaceError(texts["path_absolute"].format(path=rel))
        parts = PurePosixPath(raw).parts
        if ".." in parts or ".git" in parts:
            raise WorkspaceError(texts["path_forbidden"].format(path=rel))
        for part in parts:
            if any(fnmatch.fnmatch(part.lower(), pattern) for pattern in DENY_GLOBS):
                raise WorkspaceError(texts["path_denied"].format(path=rel))
        target = (self.path / raw)
        root = self.path.resolve()
        # symlink компонент може да сочи навън - проверяваме всяко съществуващо ниво
        current = self.path
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise WorkspaceError(texts["path_symlink"].format(path=rel))
        resolved = target.resolve() if target.exists() else (target.parent.resolve() / target.name)
        if resolved != root and root not in resolved.parents:
            raise WorkspaceError(texts["path_forbidden"].format(path=rel))
        return target

    # -- четене -----------------------------------------------------------------

    def list_files(self, pattern: str = "**/*") -> list[str]:
        result = self._git("ls-files")
        files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if pattern and pattern not in ("**/*", "*", ""):
            files = [f for f in files if fnmatch.fnmatch(f, pattern) or fnmatch.fnmatch(PurePosixPath(f).name, pattern)]
        return files[: self.max_list]

    def read_file(self, rel: str) -> str:
        path = self._resolve(rel)
        texts = pick(_WS_TEXTS)
        if not path.is_file():
            raise WorkspaceError(texts["not_found"].format(path=rel))
        if path.stat().st_size > self.max_file_bytes:
            raise WorkspaceError(texts["too_large"].format(path=rel, limit=self.max_file_bytes))
        head = path.read_bytes()[:8192]
        if b"\0" in head:
            raise WorkspaceError(texts["binary"].format(path=rel))
        return path.read_text(encoding="utf-8", errors="replace")

    def search(self, pattern: str, glob: str | None = None) -> list[str]:
        args = ["grep", "-n", "-I", "-e", pattern]
        if glob:
            args += ["--", glob]
        result = self._git(*args, check=False)
        if result.returncode not in (0, 1):  # 1 = няма съвпадения
            raise WorkspaceError(f"git grep ({self.repo.display}): {result.stderr.strip()}")
        return result.stdout.splitlines()[:DEFAULT_MAX_SEARCH_LINES]

    # -- писане (само Developer) ----------------------------------------------

    def write_file(self, rel: str, content: str) -> None:
        path = self._resolve(rel)
        if len(content.encode("utf-8")) > self.max_file_bytes:
            raise WorkspaceError(pick(_WS_TEXTS)["too_large"].format(path=rel, limit=self.max_file_bytes))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as fh:
            fh.write(content)

    def delete_file(self, rel: str) -> None:
        path = self._resolve(rel)
        if not path.is_file():
            raise WorkspaceError(pick(_WS_TEXTS)["not_found"].format(path=rel))
        path.unlink()

    # -- промени / публикуване ------------------------------------------------

    def stage_all(self) -> None:
        self._git("add", "-A")

    def diff(self) -> str:
        self.stage_all()
        return self._git("diff", "--cached").stdout

    def changed_files(self) -> list[str]:
        self.stage_all()
        result = self._git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def has_changes(self) -> bool:
        return bool(self._git("status", "--porcelain").stdout.strip())

    def syntax_error(self, rel: str) -> str | None:
        """ast.parse върху файл от workspace-а (кодът НЕ се изпълнява)."""
        try:
            ast.parse(self.read_file(rel))
        except SyntaxError as exc:
            return f"{exc.msg} (line {exc.lineno})"
        except WorkspaceError as exc:
            return str(exc)
        return None

    def commit(self, message: str, author_name: str, author_email: str) -> str:
        self.stage_all()
        self._git("-c", f"user.name={author_name}", "-c", f"user.email={author_email}", "commit", "-q", "-m", message)
        return self._git("rev-parse", "HEAD").stdout.strip()

    def push(self) -> None:
        if not self.branch:
            raise WorkspaceError(pick(_WS_TEXTS)["no_branch"])
        self._git("push", "-u", "origin", self.branch)


class MultiWorkspace:
    """Единен изглед над няколко репозитория - каквото ProdDoD и finalize искат."""

    def __init__(self, workspaces: dict[str, RepoWorkspace]):
        self.workspaces = workspaces

    def changed_files(self) -> list[str]:
        return [f"{name}:{path}" for name, ws in self.workspaces.items() for path in ws.changed_files()]

    def syntax_error(self, path: str) -> str | None:
        name, _, rel = path.partition(":")
        ws = self.workspaces.get(name)
        return ws.syntax_error(rel) if ws else None

    def diff(self) -> str:
        parts = []
        for name, ws in self.workspaces.items():
            text = ws.diff()
            if text.strip():
                parts.append(f"# repo: {name}\n{text}")
        return "\n\n".join(parts)

    def with_changes(self) -> dict[str, RepoWorkspace]:
        return {name: ws for name, ws in self.workspaces.items() if ws.has_changes()}


# ---------------------------------------------------------------------------
# Инструментите за агентите (bg/en текстове при извикване, като src/tools.py)
# ---------------------------------------------------------------------------

_WS_TEXTS = {
    "bg": {
        "unknown_repo": "Непознат репозиторий '{repo}'. Налични: {available}",
        "path_empty": "Празен път.",
        "path_absolute": "Абсолютни пътища не са позволени: {path}",
        "path_forbidden": "Пътят излиза от репозитория или сочи към .git: {path}",
        "path_denied": "Достъпът до този файл е забранен (тайни/ключове): {path}",
        "path_symlink": "Symlink-ове не са позволени: {path}",
        "not_found": "Файлът не съществува: {path}",
        "too_large": "Файлът е твърде голям (лимит {limit} байта): {path}",
        "binary": "Бинарен файл - не се чете като текст: {path}",
        "no_branch": "Няма активен branch за този run (start_run не е извикан).",
        "written": "Записан: {repo}:{path} ({bytes} байта)",
        "deleted": "Изтрит: {repo}:{path}",
        "no_files": "Няма файлове по шаблон '{pattern}' в {repo}.",
        "no_matches": "Няма съвпадения за '{pattern}' в {repo}.",
        "no_changes": "Няма промени в {repo}.",
        "error": "Грешка ({repo}): {message}",
    },
    "en": {
        "unknown_repo": "Unknown repository '{repo}'. Available: {available}",
        "path_empty": "Empty path.",
        "path_absolute": "Absolute paths are not allowed: {path}",
        "path_forbidden": "The path escapes the repository or points into .git: {path}",
        "path_denied": "Access to this file is denied (secrets/keys): {path}",
        "path_symlink": "Symlinks are not allowed: {path}",
        "not_found": "The file does not exist: {path}",
        "too_large": "The file is too large (limit {limit} bytes): {path}",
        "binary": "Binary file - not readable as text: {path}",
        "no_branch": "No active branch for this run (start_run was not called).",
        "written": "Written: {repo}:{path} ({bytes} bytes)",
        "deleted": "Deleted: {repo}:{path}",
        "no_files": "No files match '{pattern}' in {repo}.",
        "no_matches": "No matches for '{pattern}' in {repo}.",
        "no_changes": "No changes in {repo}.",
        "error": "Error ({repo}): {message}",
    },
}


@dataclass
class RepoTools:
    read: list
    write: list
    diff: list


def make_repo_tools(workspaces: dict[str, RepoWorkspace]) -> RepoTools:
    """Инструменти за четене (Analyst/Developer/QA), писане (Developer) и diff (Developer/QA)."""

    def _pick(repo: str) -> RepoWorkspace:
        key = (repo or "").strip()
        if key in workspaces:
            return workspaces[key]
        by_name = [ws for name, ws in workspaces.items() if name.split("/")[-1] == key]
        if len(by_name) == 1:
            return by_name[0]
        if len(workspaces) == 1 and not key:
            return next(iter(workspaces.values()))
        raise WorkspaceError(pick(_WS_TEXTS)["unknown_repo"].format(repo=repo, available=", ".join(workspaces)))

    def _guard(repo: str, fn):
        try:
            return fn(_pick(repo))
        except WorkspaceError as exc:
            return pick(_WS_TEXTS)["error"].format(repo=repo, message=exc)

    @tool
    def list_repo_files(repo: str, pattern: str = "**/*") -> str:
        """
        Списък с файловете в репозиторий (owner/name) по glob шаблон, напр. 'src/*.py'
        или '**/*.md'. Ползвай, за да се ориентираш в структурата, преди да четеш/пишеш.
        / Lists the files of a repository (owner/name) matching a glob pattern such as
        'src/*.py' or '**/*.md'. Use it to orient yourself before reading/writing.
        """
        def run(ws):
            files = ws.list_files(pattern)
            return "\n".join(files) if files else pick(_WS_TEXTS)["no_files"].format(pattern=pattern, repo=repo)
        return _guard(repo, run)

    @tool
    def read_repo_file(repo: str, path: str) -> str:
        """
        Връща съдържанието на текстов файл от репозитория (относителен път, напр.
        'src/app.py'). Винаги чети файла, преди да го променяш.
        / Returns the content of a text file in the repository (relative path such as
        'src/app.py'). Always read a file before changing it.
        """
        return _guard(repo, lambda ws: ws.read_file(path))

    @tool
    def search_repo(repo: str, pattern: str, glob: str = "") -> str:
        """
        Търси текст/регулярен израз в репозитория (git grep), по избор ограничен до
        glob (напр. '*.py'). Връща редове 'файл:ред:текст'.
        / Searches the repository for a text/regex (git grep), optionally limited to a
        glob (e.g. '*.py'). Returns 'file:line:text' rows.
        """
        def run(ws):
            rows = ws.search(pattern, glob or None)
            return "\n".join(rows) if rows else pick(_WS_TEXTS)["no_matches"].format(pattern=pattern, repo=repo)
        return _guard(repo, run)

    @tool
    def write_repo_file(repo: str, path: str, content: str) -> str:
        """
        Записва ЦЕЛОТО съдържание на файл в репозитория (създава го, ако липсва;
        презаписва го, ако съществува). Подавай пълния нов текст на файла, не diff.
        / Writes the FULL content of a file in the repository (creates it if missing,
        overwrites otherwise). Pass the complete new text of the file, not a diff.
        """
        def run(ws):
            ws.write_file(path, content)
            return pick(_WS_TEXTS)["written"].format(repo=repo, path=path, bytes=len(content.encode("utf-8")))
        return _guard(repo, run)

    @tool
    def delete_repo_file(repo: str, path: str) -> str:
        """
        Изтрива файл от репозитория. Ползвай само когато спецификацията изисква
        премахване. / Deletes a file from the repository. Use only when the
        specification requires removal.
        """
        def run(ws):
            ws.delete_file(path)
            return pick(_WS_TEXTS)["deleted"].format(repo=repo, path=path)
        return _guard(repo, run)

    @tool
    def get_change_diff(repo: str) -> str:
        """
        Връща git diff на всички текущи промени в репозитория (спрямо main). Developer:
        провери работата си с него; QA: това е кодът за преглед.
        / Returns the git diff of all current changes in the repository (against main).
        Developer: verify your work with it; QA: this is the code under review.
        """
        def run(ws):
            text = ws.diff()
            return text if text.strip() else pick(_WS_TEXTS)["no_changes"].format(repo=repo)
        return _guard(repo, run)

    return RepoTools(
        read=[list_repo_files, read_repo_file, search_repo],
        write=[write_repo_file, delete_repo_file],
        diff=[get_change_diff],
    )


def branch_name(ticket_key: str, run_id: str) -> str:
    """multibot/<KEY|task>-<run_id кратко> - разпознаваем и уникален на run."""
    label = (ticket_key or "task").lower()
    short = run_id.replace(":", "").replace(" ", "")[-12:] if run_id else "run"
    return f"multibot/{label}-{short}"
