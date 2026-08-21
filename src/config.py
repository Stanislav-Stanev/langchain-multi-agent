"""
Конфигурация на приложението.

Добра практика: всички настройки (API ключове, имена на модели) се държат
на ЕДНО място и се четат от environment променливи, а не се пишат
директно в кода ("hardcode"). Така кодът може да се качи в git без тайни.

Поддържани LLM доставчици (избира се с LLM_PROVIDER в .env):
    anthropic -> Claude през API (изисква интернет + ANTHROPIC_API_KEY)
    ollama    -> локален модел през Ollama (работи НАПЪЛНО ОФЛАЙН)
"""

import os

from dotenv import load_dotenv

# Зарежда променливите от .env файла в средата (os.environ).
# Ако .env не съществува, просто не прави нищо - тогава разчитаме
# променливите вече да са зададени в системата.
load_dotenv()

# Имена на моделите по доставчик - четем от средата, с разумни default-и.
MODEL_NAME = os.getenv("MODEL_NAME", "claude-opus-5")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")

# Цени в USD за 1 милион токъна (вход, изход) - Claude API, август 2026.
# Източник: https://claude.com/pricing (цените се променят - проверявай!)
MODEL_PRICES_USD_PER_MTOK = {
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def estimate_cost_usd(model_name: str, usage: dict) -> float | None:
    """
    Приблизителна цена в долари за usage_metadata на един модел.

    Отчита и кеширането на промптове: прочетените от кеша токъни струват
    ~10% от входната цена, а записът в кеша - ~125%. Връща None за
    непознат модел (напр. локален Ollama модел - той е безплатен).
    """
    prices = None
    for known, p in MODEL_PRICES_USD_PER_MTOK.items():
        if model_name.startswith(known):
            prices = p
            break
    if prices is None:
        return None

    in_price = prices[0] / 1_000_000   # цена за 1 входен токън
    out_price = prices[1] / 1_000_000  # цена за 1 изходен токън

    details = usage.get("input_token_details") or {}
    cache_read = details.get("cache_read") or 0
    cache_write = details.get("cache_creation") or 0
    # input_tokens включва и кешираните - вадим ги, за да не ги броим двойно
    plain_input = max(usage.get("input_tokens", 0) - cache_read - cache_write, 0)

    return (
        plain_input * in_price
        + cache_read * in_price * 0.1
        + cache_write * in_price * 1.25
        + usage.get("output_tokens", 0) * out_price
    )


def get_llm():
    """
    Създава и връща LLM клиент според избрания доставчик.

    Изнасяме създаването във функция ("factory"), за да:
      1. Не повтаряме една и съща конфигурация на 4 места.
      2. Можем лесно да сменим модела/доставчика за ЦЯЛАТА система
         от едно място (LLM_PROVIDER в .env, без промяна по кода).

    Четем LLM_PROVIDER при ВСЯКО извикване (не веднъж на import), за да
    може UI-ят (app.py) да превключва доставчика по време на работа.
    """
    provider = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()

    if provider == "ollama":
        # Локален модел - без интернет, без API ключ. Изисква пуснат
        # Ollama и модел с поддръжка на tool calling (напр. qwen3).
        # Import-ът е тук ("lazy"), за да не е задължителен пакетът,
        # когато се ползва само anthropic.
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=OLLAMA_MODEL,
            # num_predict е еквивалентът на max_tokens при Ollama
            num_predict=4096,
        )

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        if not os.getenv("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "Липсва ANTHROPIC_API_KEY! Копирай .env.example като .env "
                "и попълни ключа си от https://platform.claude.com/"
            )

        # Забележка: най-новите Claude модели (Opus 5 и нагоре) не приемат
        # параметъра temperature - поведението се управлява чрез промпта.
        return ChatAnthropic(
            model=MODEL_NAME,
            # max_tokens ограничава дължината на отговора (защита от разходи)
            max_tokens=4096,
        )

    raise ValueError(
        f"Непознат LLM_PROVIDER: {provider!r}. Валидни: 'anthropic', 'ollama'."
    )
