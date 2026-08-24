"""
Unit тестове за src/config.py - LLM фабриката и ценовия калкулатор.

Тук пазим два контракта:
  1. get_llm() чете LLM_PROVIDER при ВСЯКО извикване (UI-ят разчита на
     това за смяна на доставчика без рестарт) и гърми ясно при грешна
     конфигурация.
  2. estimate_cost_usd() смята правилно и с кеширане на промптове.
"""

import pytest

from src import config
from src.config import estimate_cost_usd, get_llm


class TestEstimateCostUsd:
    def test_input_only(self):
        # 1M входни токъна при $5/MTok вход -> точно $5
        usage = {"input_tokens": 1_000_000, "output_tokens": 0}
        assert estimate_cost_usd("claude-opus-5", usage) == pytest.approx(5.00)

    def test_output_only(self):
        usage = {"input_tokens": 0, "output_tokens": 1_000_000}
        assert estimate_cost_usd("claude-opus-5", usage) == pytest.approx(25.00)

    def test_cache_read_costs_10_percent(self):
        # 500k обикновени + 500k от кеша: 500k*$5 + 500k*$0.5 (на MTok)
        usage = {
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "input_token_details": {"cache_read": 500_000},
        }
        assert estimate_cost_usd("claude-opus-5", usage) == pytest.approx(2.50 + 0.25)

    def test_cache_write_costs_125_percent(self):
        usage = {
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "input_token_details": {"cache_creation": 1_000_000},
        }
        assert estimate_cost_usd("claude-opus-5", usage) == pytest.approx(6.25)

    def test_unknown_model_returns_none(self):
        # Ollama/локални модели нямат цена - None означава "безплатно"
        assert estimate_cost_usd("qwen3:8b", {"input_tokens": 100}) is None

    def test_matches_model_by_prefix(self):
        # Датирани версии (claude-opus-5-20260814) взимат цената на базовия модел
        usage = {"input_tokens": 1_000_000, "output_tokens": 0}
        assert estimate_cost_usd("claude-opus-5-20260814", usage) == pytest.approx(5.00)

    def test_negative_plain_input_is_clamped_to_zero(self):
        # Кешираните токъни не бива да направят входа "отрицателен"
        usage = {
            "input_tokens": 100,
            "output_tokens": 0,
            "input_token_details": {"cache_read": 500},
        }
        cost = estimate_cost_usd("claude-opus-5", usage)
        # само cache_read компонентата: 500 * $5/M * 0.1
        assert cost == pytest.approx(500 * 5 / 1_000_000 * 0.1)

    def test_missing_usage_keys_default_to_zero(self):
        assert estimate_cost_usd("claude-opus-5", {}) == pytest.approx(0.0)


class TestGetLlm:
    def test_anthropic_is_default_provider(self, monkeypatch):
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        from langchain_anthropic import ChatAnthropic

        llm = get_llm()
        assert isinstance(llm, ChatAnthropic)
        assert llm.model == config.MODEL_NAME

    def test_anthropic_without_api_key_raises(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            get_llm()

    def test_ollama_provider(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        from langchain_ollama import ChatOllama

        llm = get_llm()
        assert isinstance(llm, ChatOllama)
        assert llm.model == config.OLLAMA_MODEL

    def test_provider_value_is_normalized(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "  OLLAMA  ")
        from langchain_ollama import ChatOllama

        assert isinstance(get_llm(), ChatOllama)

    def test_unknown_provider_raises_value_error(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        with pytest.raises(ValueError, match="openai"):
            get_llm()

    def test_provider_is_read_on_every_call(self, monkeypatch):
        # Контракт: смяната на доставчика важи БЕЗ рестарт/реимпорт
        from langchain_anthropic import ChatAnthropic
        from langchain_ollama import ChatOllama

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        assert isinstance(get_llm(), ChatAnthropic)
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        assert isinstance(get_llm(), ChatOllama)


class TestModelRouting:
    """Model routing по роля (improvement.md §2.5): всяка роля може да
    получи собствен модел през среда, с fallback към общия MODEL_NAME."""

    def test_role_specific_model_override(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("MODEL_SUPERVISOR", "claude-haiku-4-5")

        assert get_llm("supervisor").model == "claude-haiku-4-5"
        # Роля БЕЗ override пада към общия модел
        assert get_llm("developer").model == config.MODEL_NAME

    def test_ollama_role_override(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("OLLAMA_MODEL_QA", "llama3.1")

        assert get_llm("qa").model == "llama3.1"
        assert get_llm("analyst").model == config.OLLAMA_MODEL

    def test_default_role_uses_default_model(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        assert get_llm().model == config.MODEL_NAME


class TestResilienceSettings:
    """Retry/timeout на LLM клиентите (improvement.md §2.7)."""

    def test_default_retries_and_timeout(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")

        llm = get_llm()
        assert llm.max_retries == 3
        assert llm.default_request_timeout == 120.0

    def test_settings_are_configurable_from_env(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("LLM_MAX_RETRIES", "5")
        monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "60")

        llm = get_llm()
        assert llm.max_retries == 5
        assert llm.default_request_timeout == 60.0


class TestBudgetGuard:
    """Бюджетният лимит на run (improvement.md §5.3)."""

    def test_budget_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("MAX_COST_USD_PER_RUN", raising=False)
        assert config.run_budget_usd() == 0.0
        assert config.budget_exceeded({"claude-opus-5": {"input_tokens": 10**9}}) is False

    def test_invalid_budget_value_disables_the_guard(self, monkeypatch):
        monkeypatch.setenv("MAX_COST_USD_PER_RUN", "безплатно")
        assert config.run_budget_usd() == 0.0

    def test_total_cost_sums_models_and_skips_free_ones(self):
        usage = {
            "claude-opus-5": {"input_tokens": 1_000_000, "output_tokens": 0},  # $5
            "qwen3:8b": {"input_tokens": 10**9, "output_tokens": 10**9},       # локален -> $0
        }
        assert config.total_cost_usd(usage) == pytest.approx(5.00)

    def test_budget_exceeded_when_cost_is_above_limit(self, monkeypatch):
        monkeypatch.setenv("MAX_COST_USD_PER_RUN", "1.00")
        over = {"claude-opus-5": {"input_tokens": 1_000_000, "output_tokens": 0}}   # $5
        under = {"claude-opus-5": {"input_tokens": 100_000, "output_tokens": 0}}    # $0.50
        assert config.budget_exceeded(over) is True
        assert config.budget_exceeded(under) is False
