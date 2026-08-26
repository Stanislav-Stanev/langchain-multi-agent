"""
Планове на Developer и QA: структурирани схеми, рендер до markdown, прогрес.

Принципът (improvement.md §2.1, приложен към планирането): LLM решава
СЪДЪРЖАНИЕТО на плана (кои стъпки, кои файлове, кой критерий покриват),
а кодът пази ФОРМАТА и ПРОГРЕСА. Затова:

  - планът се получава като Pydantic обект (with_structured_output), не
    като свободен markdown, който после да парсваме;
  - източникът на истина е JSON файл (implementation-plan.json), а
    markdown-ът с чекбоксовете се ПРЕ-РЕНДИРА от него при всяка промяна -
    никакво regex редактиране на md текст (чупливо и неидемпотентно);
  - агентите отчитат прогреса с инструменти (update_plan_step,
    update_test_case), които мутират JSON-а и пре-рендират md-то; така
    прогресът се вижда на живо (в UI-я като tool call) и остава на диска;
  - history записите са с ключ (вид, възел, опит, поправка) -> повторно
    изпълнение при resume ЗАМЕНЯ записа, не го дублира.

Рендираният markdown е двуезичен (per-language речници, като промптовете).
"""

from typing import Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from src.i18n import pick, t
from src.run_context import RunCtx
from src.run_tracker import RunTracker

STEP_STATUSES = ("todo", "in_progress", "done", "blocked")
CASE_STATUSES = ("planned", "running", "passed", "failed")

PLAN_JSON = "implementation-plan.json"
PLAN_MD = "implementation-plan.md"
TEST_PLAN_JSON = "qa-plan.json"
TEST_PLAN_MD = "qa-plan.md"
TRACEABILITY_MD = "traceability.md"


# ---------------------------------------------------------------------------
# Схеми (структурираният изход на dev_plan / qa_plan възлите)
# ---------------------------------------------------------------------------


class AcceptanceCriterion(BaseModel):
    """Един критерий за приемане от спецификацията, с кратък идентификатор."""

    id: str = Field(description="Кратък идентификатор, напр. AC-1, AC-2.")
    text: str = Field(description="Критерият, преразказан с една фраза.")


class PlanStep(BaseModel):
    """Една стъпка от плана за имплементация."""

    id: str = Field(description="Кратък идентификатор, напр. S1, S2.")
    title: str = Field(description="Какво се прави в стъпката (1 изречение).")
    files: list[str] = Field(default_factory=list, description="Файлове, които стъпката създава/променя.")
    acceptance_criteria_refs: list[str] = Field(
        default_factory=list, description="ID-та на критериите (AC-x), които стъпката покрива."
    )
    status: Literal["todo", "in_progress", "done", "blocked"] = "todo"
    note: str = ""


class ImplementationPlan(BaseModel):
    """Планът на Developer - произведен от dev_plan възела."""

    summary: str = Field(description="Резюме на подхода в 1-2 изречения.")
    acceptance_criteria: list[AcceptanceCriterion] = Field(
        default_factory=list, description="Критериите за приемане от спецификацията."
    )
    steps: list[PlanStep] = Field(default_factory=list, description="Стъпките в ред на изпълнение.")


class TestCase(BaseModel):
    """Един тест-случай от тест-плана на QA."""

    id: str = Field(description="Кратък идентификатор, напр. T1, T2.")
    title: str = Field(description="Какво се проверява (1 изречение).")
    criterion_ref: str = Field(description="ID на критерия за приемане (AC-x), който случаят проверява.")
    method: Literal["static", "checklist", "manual_review", "unit_test"] = Field(
        description="Как: static (check_code_syntax), checklist (run_test_checklist), manual_review, unit_test."
    )
    expected: str = Field(description="Очакваният резултат.")
    status: Literal["planned", "running", "passed", "failed"] = "planned"
    note: str = ""


class TestPlan(BaseModel):
    """Тест-планът на QA - произведен от qa_plan възела."""

    summary: str = Field(description="Стратегията на проверката в 1-2 изречения.")
    cases: list[TestCase] = Field(default_factory=list, description="Тест-случаите.")


# ---------------------------------------------------------------------------
# Промптове за структурираните извиквания
# ---------------------------------------------------------------------------

DEV_PLAN_PROMPTS = {
    "bg": """Ти си Tech Lead. По спецификацията на Analyst (в разговора по-горе) направи
ПЛАН ЗА ИМПЛЕМЕНТАЦИЯ, преди Developer да пише код.

Правила:
- Извлечи критериите за приемане като списък с ID-та AC-1, AC-2, ...
- Раздели работата на 2-6 малки стъпки S1, S2, ... в ред на изпълнение.
- За всяка стъпка посочи файловете, които пипа, и кои AC покрива.
- Всеки критерий трябва да е покрит от поне една стъпка.
- Не пиши код - само планът, по схемата.""",
    "en": """You are a Tech Lead. From the Analyst's specification (earlier in the conversation)
produce an IMPLEMENTATION PLAN before the Developer writes code.

Rules:
- Extract the acceptance criteria as a list with IDs AC-1, AC-2, ...
- Split the work into 2-6 small steps S1, S2, ... in execution order.
- For every step list the files it touches and which ACs it covers.
- Every criterion must be covered by at least one step.
- Do not write code - only the plan, per the schema.""",
}

QA_PLAN_PROMPTS = {
    "bg": """Ти си QA Lead. По спецификацията, плана за имплементация и кода (в разговора
по-горе) направи ТЕСТ-ПЛАН, преди QA да проверява.

Критериите за приемане с техните ID-та:
{criteria}

Правила:
- За всеки критерий - поне един тест-случай T1, T2, ... с criterion_ref = ID-то му.
- method: static (синтаксис), checklist (QA чеклист), manual_review (ръчен преглед на
  логиката), unit_test (описан тест).
- expected описва конкретния очакван резултат.
- Не изпълнявай проверки - само планът, по схемата.""",
    "en": """You are a QA Lead. From the specification, the implementation plan and the code
(earlier in the conversation) produce a TEST PLAN before QA verifies.

The acceptance criteria with their IDs:
{criteria}

Rules:
- For every criterion at least one test case T1, T2, ... with criterion_ref = its ID.
- method: static (syntax), checklist (QA checklist), manual_review (manual logic review),
  unit_test (a described test).
- expected states the concrete expected result.
- Do not run checks - only the plan, per the schema.""",
}


# ---------------------------------------------------------------------------
# Документи (dict) - model_dump() + history
# ---------------------------------------------------------------------------


def new_plan_doc(plan: ImplementationPlan) -> dict:
    return {**plan.model_dump(), "history": []}


def new_test_plan_doc(test_plan: TestPlan) -> dict:
    return {**test_plan.model_dump(), "history": []}


def counts(doc: dict, kind: str = "plan") -> dict:
    """Броячи по статус - за STATUS.md, секциите „Прогрес" и DoD проверките."""
    items_key, statuses = ("steps", STEP_STATUSES) if kind == "plan" else ("cases", CASE_STATUSES)
    result = dict.fromkeys(statuses, 0)
    for item in (doc or {}).get(items_key, []):
        status = item.get("status")
        if status in result:
            result[status] += 1
    return result


def _upsert_history(doc: dict, entry: dict, key_fields: tuple[str, ...]) -> dict:
    """Записът се ЗАМЕНЯ, ако вече има такъв със същия ключ (идемпотентност)."""
    key = tuple(entry.get(k) for k in key_fields)
    history = [h for h in doc.get("history", []) if tuple(h.get(k) for k in key_fields) != key]
    history.append(entry)
    return {**doc, "history": history}


def apply_step_update(doc: dict, step_id: str, status: str, note: str = "") -> dict:
    """Нов статус/бележка на стъпка; непознато ID -> KeyError, статус -> ValueError."""
    if status not in STEP_STATUSES:
        raise ValueError(status)
    steps = [dict(s) for s in doc.get("steps", [])]
    for step in steps:
        if step["id"].strip().upper() == step_id.strip().upper():
            step["status"] = status
            if note:
                step["note"] = note
            return {**doc, "steps": steps}
    raise KeyError(step_id)


def apply_case_update(doc: dict, case_id: str, status: str, note: str = "") -> dict:
    if status not in CASE_STATUSES:
        raise ValueError(status)
    cases = [dict(c) for c in doc.get("cases", [])]
    for case in cases:
        if case["id"].strip().upper() == case_id.strip().upper():
            case["status"] = status
            if note:
                case["note"] = note
            return {**doc, "cases": cases}
    raise KeyError(case_id)


def record_phase_end(doc: dict, *, node: str, attempt: int, rework: int, kind: str = "plan") -> dict:
    """„Прогрес в края на фазата" - броячите по статус, с ключ (възел, опит, поправка)."""
    entry = {
        "kind": "phase_end",
        "node": node,
        "attempt": attempt,
        "rework": rework,
        "counts": counts(doc, kind),
    }
    return _upsert_history(doc, entry, ("kind", "node", "attempt", "rework"))


def add_rework(doc: dict, n: int, issues: list[str]) -> dict:
    """Секция „Rework N" със забележките на QA - планът не се пренаписва."""
    return _upsert_history(doc, {"kind": "rework", "n": n, "issues": list(issues)}, ("kind", "n"))


def reset_for_rerun(doc: dict, n: int) -> dict:
    """Нулира тест-случаите за нов run (след поправка), пазейки предишния резултат в бележка."""
    cases = []
    for case in doc.get("cases", []):
        previous = case.get("status", "planned")
        note = case.get("note", "")
        if previous in ("passed", "failed"):
            note = f"run {n - 1}: {previous}" + (f" - {note}" if note else "")
        cases.append({**case, "status": "planned", "note": note})
    return _upsert_history({**doc, "cases": cases}, {"kind": "rerun", "n": n}, ("kind", "n"))


def validate_case_refs(test_doc: dict, plan_doc: dict | None) -> dict:
    """Непознат criterion_ref -> '?' (матрицата го флагва); без план - без промяна."""
    if not plan_doc:
        return test_doc
    known = {c["id"].strip().upper() for c in plan_doc.get("acceptance_criteria", [])}
    cases = []
    for case in test_doc.get("cases", []):
        ref = (case.get("criterion_ref") or "").strip().upper()
        cases.append({**case, "criterion_ref": ref if ref in known else "?"})
    return {**test_doc, "cases": cases}


# ---------------------------------------------------------------------------
# Рендер до markdown
# ---------------------------------------------------------------------------

_MD = {
    "bg": {
        "plan_title": "# План за имплементация{ticket}",
        "test_title": "# Тест-план{ticket}",
        "for_ticket": " · {ticket}",
        "summary": "**Резюме:** {summary}",
        "criteria_heading": "## Критерии за приемане",
        "steps_heading": "## Стъпки",
        "cases_heading": "## Тест-случаи",
        "files": "файлове: {files}",
        "covers": "покрива: {refs}",
        "checks": "проверява: {ref}",
        "method": "метод: {method}",
        "expected": "очаквано: {expected}",
        "note": "бележка: {note}",
        "progress_heading": "## Прогрес в края на фаза `{node}` (опит {attempt}, поправка {rework})",
        "plan_counts": "- готови: {done} · в ход: {in_progress} · чакат: {todo} · блокирани: {blocked}",
        "test_counts": "- минали: {passed} · провалени: {failed} · в ход: {running} · чакат: {planned}",
        "rework_heading": "## Rework {n} - забележки от QA",
        "rerun_heading": "## Run {n} - тест-случаите са нулирани след поправка",
        "history_heading": "## История",
        "trace_title": "# Матрица на проследимост",
        "trace_header": "| Критерий | Текст | Стъпки (статус) | Тест-случаи (статус) | Резултат |",
        "trace_sep": "|----------|-------|-----------------|----------------------|----------|",
        "trace_pass": "✅ покрит",
        "trace_fail": "❌ провален",
        "trace_untested": "⚠️ непроверен",
        "trace_unknown_heading": "## Тест-случаи с непознат критерий (`?`)",
        "none": "—",
    },
    "en": {
        "plan_title": "# Implementation plan{ticket}",
        "test_title": "# Test plan{ticket}",
        "for_ticket": " · {ticket}",
        "summary": "**Summary:** {summary}",
        "criteria_heading": "## Acceptance criteria",
        "steps_heading": "## Steps",
        "cases_heading": "## Test cases",
        "files": "files: {files}",
        "covers": "covers: {refs}",
        "checks": "checks: {ref}",
        "method": "method: {method}",
        "expected": "expected: {expected}",
        "note": "note: {note}",
        "progress_heading": "## Progress at the end of phase `{node}` (attempt {attempt}, rework {rework})",
        "plan_counts": "- done: {done} · in progress: {in_progress} · todo: {todo} · blocked: {blocked}",
        "test_counts": "- passed: {passed} · failed: {failed} · running: {running} · planned: {planned}",
        "rework_heading": "## Rework {n} - QA issues",
        "rerun_heading": "## Run {n} - test cases reset after rework",
        "history_heading": "## History",
        "trace_title": "# Traceability matrix",
        "trace_header": "| Criterion | Text | Steps (status) | Test cases (status) | Result |",
        "trace_sep": "|-----------|------|----------------|---------------------|--------|",
        "trace_pass": "✅ covered",
        "trace_fail": "❌ failed",
        "trace_untested": "⚠️ untested",
        "trace_unknown_heading": "## Test cases with an unknown criterion (`?`)",
        "none": "—",
    },
}

# Чекбокс маркери по статус: класическият "- [ ]"/"- [x]" + разширения
STEP_MARKERS = {"todo": "[ ]", "in_progress": "[~]", "done": "[x]", "blocked": "[!]"}
CASE_MARKERS = {"planned": "[ ]", "running": "[~]", "passed": "[x]", "failed": "[!]"}


def _md(key: str, **kwargs) -> str:
    text = pick(_MD)[key]
    return text.format(**kwargs) if kwargs else text


def _ticket_suffix(ticket_key: str) -> str:
    return _md("for_ticket", ticket=ticket_key) if ticket_key else ""


def _history_sections(doc: dict, kind: str) -> list[str]:
    lines: list[str] = []
    for entry in doc.get("history", []):
        if entry.get("kind") == "phase_end":
            lines += ["", _md("progress_heading", node=entry["node"], attempt=entry["attempt"], rework=entry["rework"])]
            lines.append(_md("plan_counts" if kind == "plan" else "test_counts", **entry["counts"]))
        elif entry.get("kind") == "rework":
            lines += ["", _md("rework_heading", n=entry["n"])]
            lines += [f"- {issue}" for issue in entry.get("issues", [])] or [f"- {_md('none')}"]
        elif entry.get("kind") == "rerun":
            lines += ["", _md("rerun_heading", n=entry["n"])]
    return lines


def render_implementation_plan(doc: dict, ticket_key: str = "") -> str:
    lines = [_md("plan_title", ticket=_ticket_suffix(ticket_key)), "", _md("summary", summary=doc.get("summary", ""))]
    criteria = doc.get("acceptance_criteria", [])
    if criteria:
        lines += ["", _md("criteria_heading")]
        lines += [f"- **{c['id']}** — {c['text']}" for c in criteria]
    lines += ["", _md("steps_heading")]
    for step in doc.get("steps", []):
        marker = STEP_MARKERS.get(step.get("status", "todo"), "[ ]")
        details = []
        if step.get("files"):
            details.append(_md("files", files=", ".join(f"`{f}`" for f in step["files"])))
        if step.get("acceptance_criteria_refs"):
            details.append(_md("covers", refs=", ".join(step["acceptance_criteria_refs"])))
        if step.get("note"):
            details.append(_md("note", note=step["note"]))
        suffix = f" _({'; '.join(details)})_" if details else ""
        lines.append(f"- {marker} **{step['id']}** {step['title']}{suffix}")
    lines += _history_sections(doc, "plan")
    lines.append("")
    return "\n".join(lines)


def render_test_plan(doc: dict, ticket_key: str = "") -> str:
    lines = [_md("test_title", ticket=_ticket_suffix(ticket_key)), "", _md("summary", summary=doc.get("summary", ""))]
    lines += ["", _md("cases_heading")]
    for case in doc.get("cases", []):
        marker = CASE_MARKERS.get(case.get("status", "planned"), "[ ]")
        details = [
            _md("checks", ref=case.get("criterion_ref", "?")),
            _md("method", method=case.get("method", "")),
            _md("expected", expected=case.get("expected", "")),
        ]
        if case.get("note"):
            details.append(_md("note", note=case["note"]))
        lines.append(f"- {marker} **{case['id']}** {case['title']} _({'; '.join(details)})_")
    lines += _history_sections(doc, "test")
    lines.append("")
    return "\n".join(lines)


def render_traceability(plan_doc: dict | None, test_doc: dict | None) -> str:
    """Матрица критерий <-> стъпки <-> тест-случаи <-> резултат (чист код)."""
    plan_doc = plan_doc or {}
    test_doc = test_doc or {}
    lines = [_md("trace_title"), "", _md("trace_header"), _md("trace_sep")]
    none = _md("none")
    for crit in plan_doc.get("acceptance_criteria", []):
        cid = crit["id"].strip().upper()
        steps = [s for s in plan_doc.get("steps", []) if cid in {r.strip().upper() for r in s.get("acceptance_criteria_refs", [])}]
        cases = [c for c in test_doc.get("cases", []) if (c.get("criterion_ref") or "").strip().upper() == cid]
        step_txt = ", ".join(f"{s['id']} ({s.get('status')})" for s in steps) or none
        case_txt = ", ".join(f"{c['id']} ({c.get('status')})" for c in cases) or none
        statuses = {c.get("status") for c in cases}
        if "failed" in statuses:
            result = _md("trace_fail")
        elif cases and statuses == {"passed"}:
            result = _md("trace_pass")
        else:
            result = _md("trace_untested")
        lines.append(f"| {crit['id']} | {crit['text']} | {step_txt} | {case_txt} | {result} |")
    unknown = [c for c in test_doc.get("cases", []) if (c.get("criterion_ref") or "").strip() in ("", "?")]
    if unknown:
        lines += ["", _md("trace_unknown_heading")]
        lines += [f"- {c['id']} {c['title']}" for c in unknown]
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Диск: JSON източник на истина + пре-рендиран markdown
# ---------------------------------------------------------------------------


def load_plan(tracker) -> dict | None:
    return tracker.read_json(PLAN_JSON, default=None)


def save_plan(tracker, doc: dict, ticket_key: str = "") -> None:
    tracker.write_json(PLAN_JSON, doc)
    tracker.write_text(PLAN_MD, render_implementation_plan(doc, ticket_key))


def load_test_plan(tracker) -> dict | None:
    return tracker.read_json(TEST_PLAN_JSON, default=None)


def save_test_plan(tracker, doc: dict, ticket_key: str = "") -> None:
    tracker.write_json(TEST_PLAN_JSON, doc)
    tracker.write_text(TEST_PLAN_MD, render_test_plan(doc, ticket_key))


def _ticket_of(tracker) -> str:
    return (tracker.read_json("run.json", default={}) or {}).get("ticket_key", "")


# ---------------------------------------------------------------------------
# Инструменти за агентите: отчитане на прогреса
# ---------------------------------------------------------------------------
# runtime: ToolRuntime[RunCtx] е СКРИТ от модела (не влиза в JSON схемата) -
# LangChain го инжектира от context=RunCtx(...), подаден при agent.invoke.


def make_plan_tools() -> list:
    """Инструментите update_plan_step / update_test_case (за Developer / QA)."""

    @tool
    def update_plan_step(step_id: str, status: str, note: str, runtime: ToolRuntime[RunCtx]) -> str:
        """
        Отбелязва прогреса по стъпка от плана за имплементация. Извикай с
        status='in_progress' преди да започнеш стъпка, 'done' (с кратка бележка
        какво е направено) когато я завършиш, 'blocked' ако не можеш да я
        завършиш. Статуси: todo | in_progress | done | blocked.
        / Records progress on an implementation-plan step: 'in_progress' before
        you start it, 'done' (with a short note) when finished, 'blocked' if you
        cannot finish it. Statuses: todo | in_progress | done | blocked.
        """
        tracker = RunTracker(runtime.context.run_dir) if runtime.context.run_dir else None
        if tracker is None:
            return t("tool_plan_missing")
        # Агентът може да извика инструмента няколко пъти в ЕДИН ход - LangGraph
        # ги изпълнява паралелно, затова зареди -> промени -> запиши е под lock.
        with tracker.locked():
            doc = load_plan(tracker)
            if doc is None:
                return t("tool_plan_missing")
            try:
                doc = apply_step_update(doc, step_id, status.strip().lower(), note or "")
            except ValueError:
                return t("tool_plan_status_invalid", status=status, allowed=", ".join(STEP_STATUSES))
            except KeyError:
                return t("tool_plan_step_unknown", step_id=step_id, available=", ".join(s["id"] for s in doc["steps"]))
            save_plan(tracker, doc, _ticket_of(tracker))
        return t("tool_plan_step_updated", step_id=step_id.upper(), status=status.strip().lower())

    @tool
    def update_test_case(case_id: str, status: str, note: str, runtime: ToolRuntime[RunCtx]) -> str:
        """
        Отбелязва резултата от тест-случай в тест-плана. Извикай с
        status='running' преди проверката и 'passed' или 'failed' (с бележка
        какво точно е установено) след нея. Статуси: planned | running | passed | failed.
        / Records a test-plan case result: 'running' before the check, then
        'passed' or 'failed' (with a note on what was found).
        Statuses: planned | running | passed | failed.
        """
        tracker = RunTracker(runtime.context.run_dir) if runtime.context.run_dir else None
        if tracker is None:
            return t("tool_plan_missing")
        with tracker.locked():
            doc = load_test_plan(tracker)
            if doc is None:
                return t("tool_plan_missing")
            try:
                doc = apply_case_update(doc, case_id, status.strip().lower(), note or "")
            except ValueError:
                return t("tool_plan_status_invalid", status=status, allowed=", ".join(CASE_STATUSES))
            except KeyError:
                return t("tool_test_case_unknown", case_id=case_id, available=", ".join(c["id"] for c in doc["cases"]))
            save_test_plan(tracker, doc, _ticket_of(tracker))
        return t("tool_test_case_updated", case_id=case_id.upper(), status=status.strip().lower())

    return [update_plan_step, update_test_case]
