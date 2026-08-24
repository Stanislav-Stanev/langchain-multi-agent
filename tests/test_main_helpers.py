"""
Unit тестове за помощните функции на main.py (конзолната визуализация).

main.py е "визуализация" - графът никога не печата сам. Тестваме само
чистите помощни функции; самото streaming поведение се покрива в
tests/test_workflow_e2e.py през реалния граф.
"""

from main import indent, shorten


class TestShorten:
    def test_short_text_is_unchanged(self):
        assert shorten("кратък текст") == "кратък текст"

    def test_long_text_is_truncated_with_total_length(self):
        text = "х" * 300
        result = shorten(text, limit=250)
        assert result.startswith("х" * 250)
        assert "300" in result  # суфиксът докладва пълната дължина

    def test_newlines_are_collapsed_to_spaces(self):
        # Конзолният ред не бива да се чупи от многоредов tool резултат
        assert shorten("ред 1\nред 2") == "ред 1 ред 2"

    def test_strips_surrounding_whitespace(self):
        assert shorten("  текст  ") == "текст"

    def test_non_string_input_is_coerced(self):
        assert shorten({"a": 1}) == "{'a': 1}"

    def test_exact_limit_is_not_truncated(self):
        text = "а" * 250
        assert shorten(text, limit=250) == text


class TestIndent:
    def test_prefixes_every_line(self):
        assert indent("ред 1\nред 2", "  | ") == "  | ред 1\n  | ред 2"

    def test_default_prefix_is_two_spaces(self):
        assert indent("х") == "  х"
