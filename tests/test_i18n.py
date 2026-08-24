"""
Unit тестове за src/i18n.py - двуезичната поддръжка.

Какво пазим тук:
  1. get_lang() - избор на език от APP_LANG с безопасен default.
  2. t()        - връщане на правилния превод + форматиране.
  3. Данновия контракт: ВСЕКИ ключ има превод и на двата езика
     (същото, което import-валидацията пази, но като явен тест).
"""

import pytest

from src.i18n import DEFAULT_LANG, STRINGS, SUPPORTED_LANGS, get_lang, t


class TestGetLang:
    def test_default_is_bg(self):
        # autouse fixture-ът е изчистил APP_LANG -> връщаме default-а
        assert get_lang() == "bg"

    def test_reads_en_from_env(self, monkeypatch):
        monkeypatch.setenv("APP_LANG", "en")
        assert get_lang() == "en"

    def test_is_case_insensitive_and_strips(self, monkeypatch):
        monkeypatch.setenv("APP_LANG", "  EN  ")
        assert get_lang() == "en"

    def test_unknown_lang_falls_back_to_default(self, monkeypatch):
        # Непознат език НЕ трябва да чупи приложението, а да пада към bg
        monkeypatch.setenv("APP_LANG", "de")
        assert get_lang() == DEFAULT_LANG

    def test_reads_env_on_every_call(self, monkeypatch):
        # Контракт: езикът се чете при ВСЯКО извикване (UI-ят го сменя live)
        monkeypatch.setenv("APP_LANG", "bg")
        assert get_lang() == "bg"
        monkeypatch.setenv("APP_LANG", "en")
        assert get_lang() == "en"


class TestT:
    def test_returns_bulgarian_by_default(self):
        assert t("task_label") == "Задача"

    def test_returns_english_when_lang_is_en(self, monkeypatch):
        monkeypatch.setenv("APP_LANG", "en")
        assert t("task_label") == "Task"

    def test_formats_kwargs(self):
        text = t("step_supervisor", n=3, next="ANALYST")
        assert "3" in text and "ANALYST" in text

    def test_unknown_key_raises(self):
        # Правописна грешка в ключ трябва да гърми ШУМНО, не да мълчи
        with pytest.raises(KeyError):
            t("no_such_key_anywhere")


class TestStringsContract:
    def test_every_key_has_all_supported_languages(self):
        # Огледално на import-валидацията: никой ключ не е "полупреведен"
        for key, entry in STRINGS.items():
            missing = [lang for lang in SUPPORTED_LANGS if lang not in entry]
            assert not missing, f"Ключ '{key}' няма превод за: {missing}"

    def test_translations_are_nonempty_strings(self):
        for key, entry in STRINGS.items():
            for lang in SUPPORTED_LANGS:
                assert isinstance(entry[lang], str) and entry[lang].strip(), (
                    f"Ключ '{key}' има празен превод за '{lang}'"
                )

    def test_placeholders_match_between_languages(self):
        # Ако bg версията има {n} и {next}, en версията трябва да има
        # СЪЩИТЕ placeholder-и - иначе t(key, ...) гърми само на единия език.
        import string

        fmt = string.Formatter()
        for key, entry in STRINGS.items():
            fields_by_lang = {
                lang: {name for _, name, _, _ in fmt.parse(entry[lang]) if name}
                for lang in SUPPORTED_LANGS
            }
            assert fields_by_lang["bg"] == fields_by_lang["en"], (
                f"Ключ '{key}' има различни placeholder-и: {fields_by_lang}"
            )
