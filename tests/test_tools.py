"""
Unit тестове за src/tools.py - инструментите на агентите.

Инструментите са ЕДИНСТВЕНАТА детерминистична част от agentic цикъла,
затова ги покриваме плътно: нормални случаи, гранични случаи, грешки
и двуезичното им поведение (езикът се избира ПРИ ИЗВИКВАНЕ).

Инструментите се викат през .invoke({...}) - същия интерфейс, който
LangChain ползва, когато агентът поиска инструмент.
"""

import pytest

from src.tools import (
    _FAKE_TICKETS,
    _STANDARDS,
    check_code_syntax,
    get_coding_standards,
    get_ticket_details,
    run_test_checklist,
)

# Примерен "идеален" код - покрива всички евристики на QA чеклиста
GOOD_CODE = '''
def validate_email(email: str) -> bool:
    """Валидира имейл адрес."""
    if not email:
        return False
    return email.count("@") == 1
'''


class TestGetTicketDetails:
    def test_known_ticket_returns_full_card(self):
        result = get_ticket_details.invoke({"ticket_id": "DEV-101"})
        assert "DEV-101" in result
        assert "Функция за валидация на имейл адреси" in result
        # Всички критерии за приемане присъстват в картата
        for criterion in _FAKE_TICKETS["bg"]["DEV-101"]["acceptance_criteria"]:
            assert criterion in result

    def test_ticket_id_is_normalized(self):
        # Агентът може да подаде " dev-101 " - инструментът трябва да прощава
        result = get_ticket_details.invoke({"ticket_id": "  dev-101  "})
        assert "Функция за валидация на имейл адреси" in result

    def test_unknown_ticket_reports_available(self):
        # Грешката е ЧЕСТНО съобщение (не изключение) + списък с наличните
        result = get_ticket_details.invoke({"ticket_id": "DEV-999"})
        assert "DEV-999" in result
        assert "DEV-101" in result and "DEV-102" in result

    def test_english_card_when_lang_is_en(self, monkeypatch):
        monkeypatch.setenv("APP_LANG", "en")
        result = get_ticket_details.invoke({"ticket_id": "DEV-101"})
        assert "Email address validation function" in result
        assert "Acceptance criteria" in result

    def test_language_resolves_at_call_time(self, monkeypatch):
        # Контракт: инструментите реагират на смяна на езика ВЕДНАГА
        bg = get_ticket_details.invoke({"ticket_id": "DEV-101"})
        monkeypatch.setenv("APP_LANG", "en")
        en = get_ticket_details.invoke({"ticket_id": "DEV-101"})
        assert bg != en

    def test_ticket_data_is_mirrored_between_languages(self):
        # Данновият контракт: еднакви тикети и еднакъв брой критерии bg/en
        assert _FAKE_TICKETS["bg"].keys() == _FAKE_TICKETS["en"].keys()
        for ticket_id in _FAKE_TICKETS["bg"]:
            assert len(_FAKE_TICKETS["bg"][ticket_id]["acceptance_criteria"]) == len(
                _FAKE_TICKETS["en"][ticket_id]["acceptance_criteria"]
            )


class TestGetCodingStandards:
    @pytest.mark.parametrize("topic", ["python", "naming", "testing"])
    def test_known_topics_return_standard(self, topic):
        result = get_coding_standards.invoke({"topic": topic})
        assert result == _STANDARDS["bg"][topic]

    def test_topic_is_normalized(self):
        result = get_coding_standards.invoke({"topic": "  PYTHON  "})
        assert result == _STANDARDS["bg"]["python"]

    def test_unknown_topic_reports_available(self):
        result = get_coding_standards.invoke({"topic": "security"})
        assert "security" in result
        for topic in ("python", "naming", "testing"):
            assert topic in result

    def test_english_standards(self, monkeypatch):
        monkeypatch.setenv("APP_LANG", "en")
        result = get_coding_standards.invoke({"topic": "python"})
        assert result == _STANDARDS["en"]["python"]

    def test_topics_are_mirrored_between_languages(self):
        assert _STANDARDS["bg"].keys() == _STANDARDS["en"].keys()


class TestCheckCodeSyntax:
    def test_valid_code_returns_ok(self):
        result = check_code_syntax.invoke({"code": GOOD_CODE})
        assert "OK" in result

    def test_syntax_error_reports_line_number(self):
        # Ред 2 е счупен нарочно - искаме номера на реда в доклада
        broken = "def f():\n    return ((\n"
        result = check_code_syntax.invoke({"code": broken})
        assert "OK" not in result
        assert "2" in result or "3" in result  # ast сочи реда на грешката

    def test_empty_code_is_valid_python(self):
        # Празен низ е валиден (празен модул) - не трябва да гърми
        assert "OK" in check_code_syntax.invoke({"code": ""})

    def test_error_message_is_english_when_lang_is_en(self, monkeypatch):
        monkeypatch.setenv("APP_LANG", "en")
        result = check_code_syntax.invoke({"code": "def f(:"})
        assert "Syntax error" in result


class TestRunTestChecklist:
    def test_good_code_passes_all_checks(self):
        result = run_test_checklist.invoke(
            {"code": GOOD_CODE, "requirements": "валидация на имейл"}
        )
        assert "FAIL" not in result
        assert "Всички проверки преминаха успешно." in result

    def test_bare_code_fails_all_checks(self):
        result = run_test_checklist.invoke({"code": "x = 1", "requirements": "нищо"})
        assert result.count("FAIL") == 4
        assert "4" in result  # броят пропаднали проверки се докладва

    @pytest.mark.parametrize(
        "code, expected_fail",
        [
            ('x = 1\n"""d"""\ny: int = 2  # -> \nraise ValueError', "def "),  # няма def
            ("def f() -> int:\n    raise ValueError", '"""'),                # няма docstring
            ('def f():\n    """d"""\n    raise ValueError', "->"),           # няма типове
            ('def f() -> int:\n    """d"""\n    return 1', "edge"),          # няма гранични случаи
        ],
    )
    def test_each_heuristic_fails_independently(self, code, expected_fail):
        # Всяка евристика се проваля сама за себе си - точно 1 FAIL
        result = run_test_checklist.invoke({"code": code, "requirements": "r"})
        assert result.count("FAIL") == 1, f"очаквах 1 FAIL за липсващо '{expected_fail}'"

    def test_english_verdict(self, monkeypatch):
        monkeypatch.setenv("APP_LANG", "en")
        result = run_test_checklist.invoke(
            {"code": GOOD_CODE, "requirements": "email validation"}
        )
        assert "All checks passed." in result
