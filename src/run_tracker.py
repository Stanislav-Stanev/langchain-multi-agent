"""
RunTracker - проследяването на един run на диска (runs/<дата_час>_<ключ>/).

Защо файлове, а не само state: state-ът живее в паметта (или в
checkpointer-а) и е удобен за кода, но човекът иска да отвори папка и да
види какво е станало - плана, спецификацията, кода, доклада на QA,
решенията си, хронологията. Директорията на run-а е одитната следа на
процеса и се пише от ЧИСТ код (никакъв LLM) като страничен ефект на
възлите - същата категория ефект като checkpointer-а.

Съдържание (виж docs/prod-mode-plan.md §3):
    run.json                  метаданни на run-а
    run.jsonl                 append-only журнал: едно събитие на възел
    steps.json + steps/*.md   план преди / изпълнение след всяка стъпка
    index.md                  хронологичната таблица на стъпките
    STATUS.md                 „живият" статус (пре-рендира се всеки път)
    spec.md, code.py|code.diff, qa-report.md   артефактите на фазите
    implementation-plan.*, qa-plan.*           плановете (src/plans.py)
    hitl-decisions.md         човешките решения (src/hitl.py)
    traceability.md, summary.json              финалът

Идемпотентност: всичко се ПРЕ-РЕНДИРА от JSON източник на истина
(steps.json, плановете) или се презаписва изцяло; журналът дедупира по
(seq, node). Така повторно изпълнение на възел при resume от checkpoint
не дублира нищо. Записите са атомарни (tmp файл + os.replace), за да не
остане половин файл при crash.
"""

import hashlib
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from src import step_plans

# Ключ на тикет: букви/цифри, тире, число (DEV-101, AIRD-2045)
_TICKET_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")

RUN_JSON = "run.json"
RUN_JSONL = "run.jsonl"
STEPS_JSON = "steps.json"
STEPS_DIR = "steps"
INDEX_MD = "index.md"
STATUS_MD = "STATUS.md"
SUMMARY_JSON = "summary.json"
HITL_JSON = "hitl-decisions.json"
HITL_MD = "hitl-decisions.md"


def now_iso() -> str:
    """Текущият момент като ISO низ (секунди) - един формат за всички файлове."""
    return datetime.now().isoformat(timespec="seconds")


def extract_ticket_key(text: str) -> str:
    """Първият ключ на тикет в текста (напр. 'DEV-101') или празен низ."""
    match = _TICKET_KEY_RE.search(text or "")
    return match.group(0) if match else ""


def make_run_id(ticket_key: str, task: str, *, now: datetime | None = None, thread_id: str = "") -> str:
    """
    Име на run директорията: <YYYY-MM-DD_HH-MM-SS>_<KEY|task-hash>[_<thread8>].

    Датата и часът са ПЪРВИ, за да се подреждат run-овете хронологично в
    файловата система; ключът на тикета прави папката разпознаваема; при
    checkpointer добавяме и началото на thread_id, за да е ясна връзката
    run <-> нишка.
    """
    stamp = (now or datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")
    label = ticket_key or f"task-{hashlib.sha1((task or '').encode()).hexdigest()[:8]}"
    run_id = f"{stamp}_{label}"
    if thread_id:
        run_id += f"_{thread_id[:8]}"
    return run_id


# Един lock на run директория: LangGraph изпълнява няколко tool извиквания
# ПАРАЛЕЛНО (нишки) - два update_plan_step в един ход на агента пишат в един
# и същи файл. Lock-ът пази read-modify-write последователностите и записите.
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(run_dir: Path) -> threading.RLock:
    key = str(run_dir.resolve()) if run_dir.exists() else str(run_dir.absolute())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _atomic_write(path: Path, text: str) -> None:
    """Запис през временен файл + os.replace - никога половин файл при crash.

    Временният файл има уникално име (паралелни записи не се блъскат), а
    целият запис се повтаря няколко пъти при PermissionError - Windows
    особеност: антивирус/индексатор може за миг да държи току-що записан файл."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    for attempt in range(5):
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05 * (attempt + 1))


class RunTracker:
    """Всички записи по диска за един run - виж docstring-а на модула."""

    def __init__(self, run_dir: Path | str):
        self.run_dir = Path(run_dir)

    @contextmanager
    def locked(self):
        """Критична секция за read-modify-write върху файловете на run-а
        (напр. зареди план -> промени -> запиши) - виж _LOCKS."""
        with _lock_for(self.run_dir):
            yield

    # -- създаване / намиране ------------------------------------------------

    @classmethod
    def start(cls, root: Path | str, run_id: str, *, mode: str, ticket_key: str, task: str, lang: str) -> "RunTracker":
        """Създава директорията на run-а (ако липсва) и записва run.json."""
        tracker = cls(Path(root) / run_id)
        tracker.run_dir.mkdir(parents=True, exist_ok=True)
        (tracker.run_dir / STEPS_DIR).mkdir(exist_ok=True)
        if not (tracker.run_dir / RUN_JSON).exists():
            tracker.write_json(
                RUN_JSON,
                {
                    "run_id": run_id,
                    "mode": mode,
                    "ticket_key": ticket_key,
                    "task": task,
                    "lang": lang,
                    "started": now_iso(),
                    "provider": os.getenv("LLM_PROVIDER", "anthropic"),
                },
            )
        return tracker

    @classmethod
    def from_state(cls, state: dict) -> "RunTracker | NullTracker":
        """Tracker за run-а от state-а; без run_dir (преди init_run) - no-op двойник."""
        run_dir = (state or {}).get("run_dir")
        return cls(run_dir) if run_dir else NullTracker()

    @property
    def run_id(self) -> str:
        return self.run_dir.name

    # -- ниско ниво: файлове -------------------------------------------------

    def write_text(self, name: str, text: str) -> Path:
        path = self.run_dir / name
        with self.locked():
            _atomic_write(path, text)
        return path

    def read_text(self, name: str, default: str = "") -> str:
        path = self.run_dir / name
        return path.read_text(encoding="utf-8") if path.exists() else default

    def write_json(self, name: str, obj) -> Path:
        return self.write_text(name, json.dumps(obj, ensure_ascii=False, indent=2))

    def read_json(self, name: str, default=None):
        path = self.run_dir / name
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def list_artifacts(self) -> list[str]:
        """Относителните пътища на всички файлове в run директорията (без .tmp)."""
        if not self.run_dir.exists():
            return []
        return sorted(
            str(p.relative_to(self.run_dir)).replace("\\", "/")
            for p in self.run_dir.rglob("*")
            if p.is_file() and not p.name.endswith(".tmp")
        )

    # -- журнал --------------------------------------------------------------

    def event(self, **fields) -> None:
        """Едно събитие в run.jsonl; дублиран (seq, node) се пропуска (resume)."""
        path = self.run_dir / RUN_JSONL
        seq, node = fields.get("seq"), fields.get("node")
        if seq is not None and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                existing = json.loads(line)
                if existing.get("seq") == seq and existing.get("node") == node:
                    return
        record = {"ts": now_iso(), **fields}
        with self.locked(), path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def events(self) -> list[dict]:
        path = self.run_dir / RUN_JSONL
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # -- стъпки: план преди, изпълнение след -------------------------------

    def steps(self) -> list[dict]:
        return self.read_json(STEPS_JSON, default=[]) or []

    def _save_steps(self, records: list[dict], changed: dict | None = None) -> None:
        """steps.json + index.md винаги; md файлът - само на променената стъпка."""
        self.write_json(STEPS_JSON, records)
        for rec in records if changed is None else [changed]:
            self.write_text(
                f"{STEPS_DIR}/{step_plans.step_filename(rec['seq'], rec['node'])}",
                step_plans.render_step_file(rec),
            )
        self.write_text(INDEX_MD, step_plans.render_index(self.run_id, records))

    def begin_step(self, seq: int, node: str, plan_md: str) -> None:
        """Записва плана на стъпката ПРЕДИ изпълнението ѝ (статус IN_PROGRESS)."""
        records = [r for r in self.steps() if not (r["seq"] == seq and r["node"] == node)]
        record = {
            "seq": seq,
            "node": node,
            "kind": step_plans.NODE_KIND.get(node, "code"),
            "status": "IN_PROGRESS",
            "started": now_iso(),
            "finished": None,
            "duration_s": None,
            "next": None,
            "plan_md": plan_md,
            "exec_md": None,
        }
        records.append(record)
        records.sort(key=lambda r: r["seq"])
        self._save_steps(records, changed=record)

    def end_step(self, seq: int, node: str, exec_md: str, *, status: str, next_node: str | None, duration_s: float) -> None:
        """Допълва стъпката СЛЕД изпълнението: факти, статус, продължителност."""
        records = self.steps()
        for rec in records:
            if rec["seq"] == seq and rec["node"] == node:
                rec.update(
                    status=status,
                    finished=now_iso(),
                    duration_s=round(duration_s, 3),
                    next=next_node,
                    exec_md=exec_md,
                )
                changed = rec
                break
        else:
            # end без begin (не бива да се случва) - записваме поне резултата
            changed = {
                "seq": seq, "node": node, "kind": step_plans.NODE_KIND.get(node, "code"),
                "status": status, "started": None, "finished": now_iso(),
                "duration_s": round(duration_s, 3), "next": next_node,
                "plan_md": "", "exec_md": exec_md,
            }
            records.append(changed)
            records.sort(key=lambda r: r["seq"])
        self._save_steps(records, changed=changed)

    # -- артефакти на фазите ------------------------------------------------

    def persist_artifacts(self, update: dict, *, mode: str) -> None:
        """Записва типизираните артефакти от update-а на възел като файлове."""
        if update.get("spec"):
            self.write_text("spec.md", update["spec"])
        if update.get("code"):
            self.write_text("code.diff" if mode == "prod" else "code.py", update["code"])
        if update.get("qa_verdict"):
            report = ""
            for msg in update.get("messages") or []:
                if getattr(msg, "name", None) == "qa":
                    report = str(getattr(msg, "content", ""))
            verdict = json.dumps(update["qa_verdict"], ensure_ascii=False, indent=2)
            self.write_text("qa-report.md", f"{report}\n\n```json\n{verdict}\n```\n")

    # -- статус, HITL, обобщение -------------------------------------------

    def write_status(self, view: dict, *, plan_counts: dict | None = None, test_counts: dict | None = None) -> None:
        run_meta = self.read_json(RUN_JSON, default={}) or {}
        full_view = {**run_meta, **{k: v for k, v in view.items() if v is not None}}
        self.write_text(
            STATUS_MD,
            step_plans.render_status(
                full_view,
                plan_counts=plan_counts or {},
                test_counts=test_counts or {},
                artifacts=[a for a in self.list_artifacts() if "/" not in a and not a.endswith(".json")],
                ts=now_iso(),
            ),
        )

    def hitl_decisions(self) -> list[dict]:
        return self.read_json(HITL_JSON, default=[]) or []

    def record_hitl(self, decision: dict) -> None:
        """Едно човешко решение -> hitl-decisions.json + пре-рендиран .md."""
        decisions = self.hitl_decisions()
        decisions.append({"ts": now_iso(), **decision})
        self.write_json(HITL_JSON, decisions)
        self.write_text(HITL_MD, step_plans.render_hitl_decisions(decisions))

    def write_summary(self, summary: dict) -> None:
        existing = self.read_json(SUMMARY_JSON, default={}) or {}
        self.write_json(SUMMARY_JSON, {**existing, **summary})

    def write_usage(self, usage_metadata: dict, cost_usd: float) -> None:
        """Токъни/цена - знае ги само консуматорът (callback-ът), не графът."""
        self.write_summary({"usage": usage_metadata, "cost_usd": round(cost_usd, 6)})


class NullTracker:
    """No-op двойник: същият интерфейс, никакви файлове (преди init_run / в unit тестове)."""

    run_dir = None
    run_id = ""

    @contextmanager
    def locked(self):
        yield

    def write_text(self, name, text):
        return None

    def read_text(self, name, default=""):
        return default

    def write_json(self, name, obj):
        return None

    def read_json(self, name, default=None):
        return default

    def list_artifacts(self):
        return []

    def event(self, **fields):
        return None

    def events(self):
        return []

    def steps(self):
        return []

    def begin_step(self, seq, node, plan_md):
        return None

    def end_step(self, seq, node, exec_md, *, status, next_node, duration_s):
        return None

    def persist_artifacts(self, update, *, mode):
        return None

    def write_status(self, view, *, plan_counts=None, test_counts=None):
        return None

    def hitl_decisions(self):
        return []

    def record_hitl(self, decision):
        return None

    def write_summary(self, summary):
        return None

    def write_usage(self, usage_metadata, cost_usd):
        return None
