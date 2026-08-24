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
