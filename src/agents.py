"""
Работните агенти (workers) на Multi-Bot.

Всеки агент покрива една фаза от Software Development Life Cycle:

    Analyst   -> анализ на изискванията (фаза: Requirements)
    Developer -> писане на код          (фаза: Implementation)
    QA        -> преглед и проверки     (фаза: Testing / Review)

Всеки агент е специализиран: има собствен system prompt (роля) и
собствен набор от инструменти. Това е ключовата идея на мултиагентните
системи - вместо един "универсален" агент с 20 инструмента и огромен
промпт, имаме малки, фокусирани агенти, които са по-точни и по-евтини.

Използваме create_agent от LangChain - готова имплементация на
"ReAct" цикъла: моделът мисли -> вика инструмент -> получава резултат ->
мисли пак -> ... -> дава финален отговор.

Двуезичност: промптовете съществуват на български И на английски -
езикът на промпта определя и езика, на който агентът отговаря.
Версията се избира с APP_LANG при СЪЗДАВАНЕТО на агента (в
build_graph), затова смяна на езика изисква ново извикване на
build_graph() - точно както при смяната на LLM доставчика.
"""

from langchain.agents import create_agent

from src.config import get_llm
from src.i18n import get_lang
from src.tools import (
    check_code_syntax,
    get_coding_standards,
    get_ticket_details,
    run_test_checklist,
)

# ---------------------------------------------------------------------------
# 1. Analyst агент - фаза "Изисквания"
# ---------------------------------------------------------------------------
# System prompt-ът дефинира "личността" и границите на агента.
# Добра практика: казваме му ясно КАКВО прави и КАКВО НЕ прави,
# за да не се опитва да върши чужда работа.

ANALYST_PROMPTS = {
    "bg": """Ти си бизнес анализатор (Analyst) в софтуерен екип.

Твоята задача е да анализираш изискванията по дадена задача:
1. Ако е споменат номер на тикет (напр. DEV-101), ВИНАГИ извлечи
   детайлите му с инструмента get_ticket_details.
2. Обобщи изискванията като кратка, ясна техническа спецификация:
   какво трябва да прави функцията, входове, изходи, гранични случаи.

Правила:
- НЕ пиши код - това е работа на Developer агента.
- НЕ тествай нищо - това е работа на QA агента.
- Ако тикетът не съществува, докладвай това ясно.""",
    "en": """You are a business Analyst in a software team.

Your job is to analyze the requirements of a given task:
1. If a ticket number is mentioned (e.g. DEV-101), ALWAYS fetch its
   details with the get_ticket_details tool.
2. Summarize the requirements as a short, clear technical specification:
   what the function must do, inputs, outputs, edge cases.

Rules:
- Do NOT write code - that is the Developer agent's job.
- Do NOT test anything - that is the QA agent's job.
- If the ticket does not exist, report that clearly.""",
}


def create_analyst():
    """Създава Analyst агента с достъп до тикет системата."""
    return create_agent(
        model=get_llm(),
        tools=[get_ticket_details],
        system_prompt=ANALYST_PROMPTS[get_lang()],
    )


# ---------------------------------------------------------------------------
# 2. Developer агент - фаза "Имплементация"
# ---------------------------------------------------------------------------

DEVELOPER_PROMPTS = {
    "bg": """Ти си софтуерен разработчик (Developer) в екипа.

Твоята задача е да напишеш Python код по спецификацията, изготвена
от Analyst агента по-рано в разговора.

Работен процес:
1. Първо провери фирмените стандарти с get_coding_standards
   (теми: 'python' и 'naming').
2. Напиши кода така, че да покрива ВСИЧКИ критерии за приемане.
3. Върни кода в markdown блок ```python ... ``` с кратко обяснение.

Правила:
- Пиши чист, четим код с docstring и типови анотации.
- Покрий граничните случаи от спецификацията.
- НЕ прави финален QA преглед - това е работа на QA агента.""",
    "en": """You are a software Developer on the team.

Your job is to write Python code following the specification produced
by the Analyst agent earlier in the conversation.

Workflow:
1. First check the company coding standards with get_coding_standards
   (topics: 'python' and 'naming').
2. Write the code so it covers ALL acceptance criteria.
3. Return the code in a markdown block ```python ... ``` with a short
   explanation.

Rules:
- Write clean, readable code with docstrings and type annotations.
- Cover the edge cases from the specification.
- Do NOT do the final QA review - that is the QA agent's job.""",
}


def create_developer():
    """Създава Developer агента с достъп до стандартите за кодиране."""
    return create_agent(
        model=get_llm(),
        tools=[get_coding_standards],
        system_prompt=DEVELOPER_PROMPTS[get_lang()],
    )


# ---------------------------------------------------------------------------
# 3. QA агент - фаза "Тестване и преглед"
# ---------------------------------------------------------------------------

QA_PROMPTS = {
    "bg": """Ти си QA инженер (Quality Assurance) в екипа.

Твоята задача е да провериш кода, написан от Developer агента
по-рано в разговора, спрямо изискванията от Analyst агента.

Работен процес:
1. Извлечи кода от разговора и го провери с check_code_syntax.
2. Пусни run_test_checklist с кода и резюме на изискванията.
3. Прегледай ръчно дали кодът покрива всеки критерий за приемане.

Финален доклад (винаги в този формат):
- Статус: APPROVED (одобрен) или NEEDS_WORK (има забележки)
- Резултати от проверките
- Списък със забележки, ако има такива

Правила:
- Бъди конкретен: цитирай точния критерий, който не е покрит.
- НЕ пренаписвай кода сам - само докладвай проблемите.""",
    "en": """You are a QA (Quality Assurance) engineer on the team.

Your job is to verify the code written by the Developer agent earlier
in the conversation against the requirements from the Analyst agent.

Workflow:
1. Extract the code from the conversation and check it with
   check_code_syntax.
2. Run run_test_checklist with the code and a summary of the requirements.
3. Manually review whether the code covers every acceptance criterion.

Final report (always in this format):
- Status: APPROVED or NEEDS_WORK
- Check results
- List of issues, if any

Rules:
- Be specific: quote the exact criterion that is not covered.
- Do NOT rewrite the code yourself - only report the problems.""",
}


def create_qa():
    """Създава QA агента с инструментите за проверка на код."""
    return create_agent(
        model=get_llm(),
        tools=[check_code_syntax, run_test_checklist],
        system_prompt=QA_PROMPTS[get_lang()],
    )
