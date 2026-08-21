"""
Инструменти (Tools) за SDLC агентите.

Какво е "инструмент"?
    Функция, която агентът (LLM) може да ИЗВИКА, когато прецени, че му
    трябва. Моделът не изпълнява кода сам - той връща "искам да извикам
    tool X с аргументи Y", LangChain изпълнява функцията и подава
    резултата обратно на модела. Този цикъл се нарича "agentic loop".

Добри практики за инструменти:
    1. Декораторът @tool превръща обикновена функция в инструмент.
    2. Docstring-ът е ЗАДЪЛЖИТЕЛЕН и МНОГО важен - моделът чете именно
       него, за да реши КОГА и КАК да ползва инструмента.
    3. Типовите анотации (ticket_id: str) стават JSON схема, която
       казва на модела какви аргументи да подаде.
    4. Инструментът винаги връща стойност (обикновено str), дори при
       грешка - така агентът може да реагира, вместо всичко да гръмне.

Забележка: тук инструментите са "mock" (симулирани), за да работи
примерът без външни системи. В реален проект get_ticket_details би
викал Jira REST API, run_tests би пускал pytest в CI и т.н. -
интерфейсът към агента остава СЪЩИЯТ, сменя се само имплементацията.

Двуезичност: mock данните и съобщенията съществуват на български и
английски; версията се избира С ТЕКУЩИЯ език (get_lang) при всяко
извикване - инструментите се изпълняват по време на разговора, затова
могат да реагират на смяна на езика веднага (за разлика от промптовете,
които се фиксират при създаването на агента).
"""

import ast

from langchain_core.tools import tool

from src.i18n import get_lang

# ---------------------------------------------------------------------------
# Симулирана тикет система (като Jira) - за Analyst агента
# ---------------------------------------------------------------------------

_FAKE_TICKETS = {
    "bg": {
        "DEV-101": {
            "title": "Функция за валидация на имейл адреси",
            "description": (
                "Като потребител искам системата да валидира имейл адреси при "
                "регистрация, за да не се създават акаунти с невалидни имейли."
            ),
            "acceptance_criteria": [
                "Функцията приема string и връща True/False",
                "Валиден имейл съдържа точно един символ '@'",
                "След '@' трябва да има домейн с поне една точка",
                "Празен string връща False",
            ],
            "priority": "High",
        },
        "DEV-102": {
            "title": "Функция за изчисляване на отстъпка",
            "description": (
                "Като клиент искам да получавам отстъпка според сумата на "
                "поръчката: над 100 лв. - 5%, над 500 лв. - 10%."
            ),
            "acceptance_criteria": [
                "Функцията приема сума (float) и връща крайна цена с отстъпка",
                "Отрицателна сума хвърля ValueError",
                "До 100 лв. включително няма отстъпка",
            ],
            "priority": "Medium",
        },
    },
    "en": {
        "DEV-101": {
            "title": "Email address validation function",
            "description": (
                "As a user I want the system to validate email addresses at "
                "registration, so accounts with invalid emails are not created."
            ),
            "acceptance_criteria": [
                "The function takes a string and returns True/False",
                "A valid email contains exactly one '@' character",
                "After '@' there must be a domain with at least one dot",
                "An empty string returns False",
            ],
            "priority": "High",
        },
        "DEV-102": {
            "title": "Discount calculation function",
            "description": (
                "As a customer I want a discount based on the order total: "
                "above 100 BGN - 5%, above 500 BGN - 10%."
            ),
            "acceptance_criteria": [
                "The function takes an amount (float) and returns the final discounted price",
                "A negative amount raises ValueError",
                "Up to and including 100 BGN there is no discount",
            ],
            "priority": "Medium",
        },
    },
}

_TICKET_TEXTS = {
    "bg": {
        "not_found": "Тикет '{ticket_id}' не е намерен. Налични тикети: {available}",
        "card": (
            "Тикет: {ticket_id}\nЗаглавие: {title}\nПриоритет: {priority}\n"
            "Описание: {description}\nКритерии за приемане:\n{criteria}"
        ),
    },
    "en": {
        "not_found": "Ticket '{ticket_id}' not found. Available tickets: {available}",
        "card": (
            "Ticket: {ticket_id}\nTitle: {title}\nPriority: {priority}\n"
            "Description: {description}\nAcceptance criteria:\n{criteria}"
        ),
    },
}


@tool
def get_ticket_details(ticket_id: str) -> str:
    """
    Извлича детайлите на тикет от тикет системата по неговия номер
    (например 'DEV-101'). Връща заглавие, описание, критерии за приемане
    и приоритет. Използвай ВИНАГИ, когато задачата споменава тикет.
    / Fetches ticket details by ticket number (e.g. 'DEV-101'): title,
    description, acceptance criteria, priority. ALWAYS use it when the
    task mentions a ticket.
    """
    lang = get_lang()
    tickets = _FAKE_TICKETS[lang]
    texts = _TICKET_TEXTS[lang]

    ticket = tickets.get(ticket_id.strip().upper())

    if ticket is None:
        # Честно съобщение за грешка вместо изключение -
        # агентът ще го прочете и ще каже на потребителя.
        return texts["not_found"].format(
            ticket_id=ticket_id, available=", ".join(tickets.keys())
        )

    criteria = "\n".join(f"  - {c}" for c in ticket["acceptance_criteria"])
    return texts["card"].format(
        ticket_id=ticket_id.strip().upper(),
        title=ticket["title"],
        priority=ticket["priority"],
        description=ticket["description"],
        criteria=criteria,
    )


# ---------------------------------------------------------------------------
# База знания с фирмени стандарти за кодиране - за Developer агента
# ---------------------------------------------------------------------------

_STANDARDS = {
    "bg": {
        "python": (
            "Python стандарти: следвай PEP 8; всяка функция има docstring "
            "и типови анотации; без глобални променливи; грешките се "
            "обработват с конкретни изключения (не 'except Exception')."
        ),
        "naming": (
            "Именуване: функции и променливи - snake_case; класове - "
            "PascalCase; константи - UPPER_CASE; имената са описателни "
            "на английски (validate_email, а не 've' или 'proverka')."
        ),
        "testing": (
            "Тестване: всяка публична функция има unit тестове; покривай "
            "и граничните случаи (празни стойности, нули, отрицателни "
            "числа); тестовете следват шаблона Arrange-Act-Assert."
        ),
    },
    "en": {
        "python": (
            "Python standards: follow PEP 8; every function has a docstring "
            "and type annotations; no global variables; errors are handled "
            "with specific exceptions (not 'except Exception')."
        ),
        "naming": (
            "Naming: functions and variables - snake_case; classes - "
            "PascalCase; constants - UPPER_CASE; names are descriptive "
            "English (validate_email, not 've' or 'proverka')."
        ),
        "testing": (
            "Testing: every public function has unit tests; cover edge "
            "cases too (empty values, zeros, negative numbers); tests "
            "follow the Arrange-Act-Assert pattern."
        ),
    },
}

_STANDARDS_NOT_FOUND = {
    "bg": "Няма стандарт за '{topic}'. Налични теми: {available}",
    "en": "No standard for '{topic}'. Available topics: {available}",
}


@tool
def get_coding_standards(topic: str) -> str:
    """
    Връща фирмените стандарти за писане на код по дадена тема.
    Възможни теми: 'python', 'naming', 'testing'.
    Използвай, преди да напишеш код, за да спазиш стандартите на екипа.
    / Returns the company coding standards for a topic ('python',
    'naming', 'testing'). Use before writing code.
    """
    lang = get_lang()
    standards = _STANDARDS[lang]

    result = standards.get(topic.strip().lower())
    if result is None:
        return _STANDARDS_NOT_FOUND[lang].format(
            topic=topic, available=", ".join(standards.keys())
        )
    return result


# ---------------------------------------------------------------------------
# Статичен анализ на код (като линтер/CI проверка) - за QA агента
# ---------------------------------------------------------------------------

_SYNTAX_TEXTS = {
    "bg": {
        "error": "Синтактична грешка на ред {line}: {msg}",
        "ok": "OK - кодът е синтактично валиден.",
    },
    "en": {
        "error": "Syntax error on line {line}: {msg}",
        "ok": "OK - the code is syntactically valid.",
    },
}


@tool
def check_code_syntax(code: str) -> str:
    """
    Проверява дали подаденият Python код е синтактично валиден
    (като линтер в CI pipeline). Подай ЦЕЛИЯ код като string.
    Връща 'OK' или описание на синтактичната грешка с номер на ред.
    / Checks whether the given Python code is syntactically valid.
    Pass the WHOLE code as a string. Returns 'OK' or the syntax error.
    """
    texts = _SYNTAX_TEXTS[get_lang()]

    # Реална (не mock!) проверка: ast.parse компилира кода до
    # абстрактно синтактично дърво, БЕЗ да го изпълнява.
    # Важно за сигурността: никога не изпълнявай (exec/eval) код,
    # генериран от LLM, без изолирана среда (sandbox)!
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return texts["error"].format(line=exc.lineno, msg=exc.msg)

    return texts["ok"]


_CHECKLIST_TEXTS = {
    "bg": {
        "checks": {
            "has_def": "Има дефинирана функция (def)",
            "has_docstring": "Има docstring (описание)",
            "has_types": "Има типови анотации (->)",
            "has_edge_cases": "Обработва грешки или гранични случаи",
        },
        "all_passed": "Всички проверки преминаха успешно.",
        "some_failed": "{failed} проверки пропаднаха - кодът има нужда от доработка.",
        "header": "QA чеклист:",
        "verdict": "Заключение: {verdict}",
    },
    "en": {
        "checks": {
            "has_def": "Has a function definition (def)",
            "has_docstring": "Has a docstring (description)",
            "has_types": "Has type annotations (->)",
            "has_edge_cases": "Handles errors or edge cases",
        },
        "all_passed": "All checks passed.",
        "some_failed": "{failed} checks failed - the code needs rework.",
        "header": "QA checklist:",
        "verdict": "Verdict: {verdict}",
    },
}


@tool
def run_test_checklist(code: str, requirements: str) -> str:
    """
    Симулира QA чеклист: проверява дали кодът съдържа базови елементи
    за качество. Подай кода и кратко резюме на изискванията.
    Връща списък с преминали/пропаднали проверки.
    / Simulates a QA checklist over the code and a requirements summary.
    Returns the list of passed/failed checks.
    """
    texts = _CHECKLIST_TEXTS[get_lang()]

    # Прости евристични проверки - в реален проект тук би се пускал
    # pytest, coverage, mypy и т.н.
    results = {
        "has_def": "def " in code,
        "has_docstring": '"""' in code or "'''" in code,
        "has_types": "->" in code,
        "has_edge_cases": any(
            kw in code for kw in ("raise", "if not", "is None", "== 0", "except")
        ),
    }

    lines = [
        f"  [{'PASS' if passed else 'FAIL'}] {texts['checks'][key]}"
        for key, passed in results.items()
    ]
    failed = sum(1 for passed in results.values() if not passed)

    verdict = (
        texts["all_passed"]
        if failed == 0
        else texts["some_failed"].format(failed=failed)
    )
    return (
        texts["header"] + "\n" + "\n".join(lines)
        + "\n\n" + texts["verdict"].format(verdict=verdict)
    )
