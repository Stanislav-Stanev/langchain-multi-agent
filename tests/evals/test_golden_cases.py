"""
Eval suite: качеството на РЕАЛНИТЕ LLM решения (improvement.md §3.2).

За разлика от детерминистичните тестове (които пазят оркестрацията с
дубльори), тук се пуска ИСТИНСКИЯТ граф с истински LLM - проверява се
дали triage решенията, спецификациите и присъдите са смислени.

Пускане (нарочно отделено от бързия пакет - виж pytest.ini):

    pytest -m eval          # всички golden случаи (истински LLM разходи!)
    pytest -m eval -k dev101

Изцяло локален инструментариум: golden dataset-ът е версиониран YAML в
tests/evals/cases/ (преглед в PR, blame, синхрон с кода), runner-ът е
обикновен pytest, а съдията (LLM-as-judge) минава през същото get_llm()
- т.е. без нито един външен eval доставчик. За напълно офлайн eval:
LLM_PROVIDER=ollama.
"""

import os
from pathlib import Path
from typing import Literal

import pytest
import yaml
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from src.config import get_llm
from src.graph import build_graph

CASES_DIR = Path(__file__).parent / "cases"
CASES = sorted(CASES_DIR.glob("*.yaml"))

# Всички тестове тук са evals: изключени от стандартния pytest run
# (pytest.ini: -m "not eval") и пускани изрично с: pytest -m eval
pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        os.getenv("LLM_PROVIDER", "anthropic").strip().lower() == "anthropic"
        and not os.getenv("ANTHROPIC_API_KEY"),
        reason="Evals изискват реален LLM (ANTHROPIC_API_KEY или LLM_PROVIDER=ollama).",
    ),
]


class JudgeScore(BaseModel):
    """Структурираната оценка на LLM съдията (рубрика 1-5)."""

    score: Literal[1, 2, 3, 4, 5] = Field(
        description="1 = изобщо не покрива рубриката, 5 = покрива я напълно."
    )
    justification: str = Field(description="Кратка обосновка на оценката.")


def judge(question: str, artifact: str) -> JudgeScore:
    """LLM-as-judge: втори модел оценява артефакт по рубрика - през
    същото get_llm(), без външни eval услуги."""
    judge_llm = get_llm("qa").with_structured_output(JudgeScore)
    return judge_llm.invoke(
        "Ти си строг оценител. Оцени по рубриката от 1 до 5.\n\n"
        f"Рубрика: {question}\n\nОценяван артефакт:\n{artifact}"
    )


@pytest.mark.parametrize("case_path", CASES, ids=lambda p: p.stem)
def test_golden_case(case_path):
    case = yaml.safe_load(case_path.read_text(encoding="utf-8"))

    graph = build_graph(mode="demo")  # evals ползват mock тикетите, никога реална Jira
    state = graph.invoke(
        {"messages": [HumanMessage(content=case["task"])]},
        config={"recursion_limit": 25},
    )

    # 1. Финалният статус е сред очакваните за случая
    assert state.get("final_status") in case["expected_final_status"], (
        f"final_status={state.get('final_status')!r}, "
        f"очаквано едно от {case['expected_final_status']}"
    )

    # 2. Triage решението: кой работник е влязъл пръв (или никой)
    expected_first = case.get("expected_first_worker")
    worker_names = [m.name for m in state["messages"] if getattr(m, "name", None)]
    if expected_first is None:
        assert not worker_names, f"очаквах отказ без работници, а работиха: {worker_names}"
    else:
        assert worker_names and worker_names[0] == expected_first, (
            f"пръв работи {worker_names[:1]}, очакван: {expected_first}"
        )

    # 3. LLM-as-judge върху артефакт (ако случаят дефинира рубрика)
    rubric = case.get("judge")
    if rubric:
        artifact = state.get(rubric["target"], "")
        assert artifact, f"артефактът '{rubric['target']}' е празен - няма какво да се оцени"
        result = judge(rubric["question"], artifact)
        assert result.score >= rubric["min_score"], (
            f"съдията даде {result.score} < {rubric['min_score']}: {result.justification}"
        )
