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

# Ролите в системата - всяка може да получи СОБСТВЕН модел (model routing):
# супервайзорът взима тривиално routing решение и не се нуждае от най-силния
# (и най-скъп) модел, докато Developer върши най-тежката когнитивна работа.
# Override: MODEL_SUPERVISOR / MODEL_ANALYST / MODEL_DEVELOPER / MODEL_QA
# (за Ollama: OLLAMA_MODEL_SUPERVISOR и т.н.). Без override всички роли
# ползват общия MODEL_NAME / OLLAMA_MODEL.
LLM_ROLES = ("supervisor", "analyst", "developer", "qa")

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


def get_llm(role: str = "default", model_override: str | None = None):
    """
    Създава и връща LLM клиент според избрания доставчик и РОЛЯ.

    Изнасяме създаването във функция ("factory"), за да:
      1. Не повтаряме една и съща конфигурация на 4 места.
      2. Можем лесно да сменим модела/доставчика за ЦЯЛАТА система
         от едно място (LLM_PROVIDER в .env, без промяна по кода).
      3. Всяка роля да може да ползва РАЗЛИЧЕН модел (model routing):
         get_llm("supervisor") чете MODEL_SUPERVISOR, ако е зададен.

    model_override заобикаля резолюцията по роля - ползва се от
    fallback веригата (виж fallback_model_name), която строи втори
    клиент с ИЗРИЧНО зададен резервен модел.

    Четем средата при ВСЯКО извикване (не веднъж на import), за да
    може UI-ят (app.py) да превключва доставчика по време на работа.
    """
    provider = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()

    # Устойчивост (виж improvement.md §2.7): всеки клиент получава
    # timeout (увиснало извикване не бива да виси вечно) и retry с
    # exponential backoff (вграден в Anthropic SDK-то) за преходни
    # грешки като 429/529. Стойностите са конфигурируеми от средата.
    timeout_s = float(os.getenv("LLM_TIMEOUT_SECONDS", "120"))
    max_retries = int(os.getenv("LLM_MAX_RETRIES", "3"))

    if provider == "ollama":
        # Локален модел - без интернет, без API ключ. Изисква пуснат
        # Ollama и модел с поддръжка на tool calling (напр. qwen3).
        # Import-ът е тук ("lazy"), за да не е задължителен пакетът,
        # когато се ползва само anthropic.
        from langchain_ollama import ChatOllama

        model = model_override or os.getenv(f"OLLAMA_MODEL_{role.upper()}", "") or OLLAMA_MODEL

        return ChatOllama(
            model=model,
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

        model = model_override or os.getenv(f"MODEL_{role.upper()}", "") or MODEL_NAME

        # Забележка: най-новите Claude модели (Opus 5 и нагоре) не приемат
        # параметъра temperature - поведението се управлява чрез промпта.
        return ChatAnthropic(
            model=model,
            # max_tokens ограничава дължината на отговора (защита от разходи)
            max_tokens=4096,
            max_retries=max_retries,
            default_request_timeout=timeout_s,
        )

    raise ValueError(
        f"Непознат LLM_PROVIDER: {provider!r}. Валидни: 'anthropic', 'ollama'."
    )


def fallback_model_name() -> str:
    """
    Името на РЕЗЕРВНИЯ модел за fallback веригата (improvement.md §2.7).

    Празен низ = fallback изключен (по подразбиране). Задава се с
    MODEL_FALLBACK (anthropic) / OLLAMA_MODEL_FALLBACK (ollama) - при
    неуспех на основния модел (изчерпани retries, timeout) критичните
    структурирани решения (triage, QA присъда) се опитват с резервния.
    """
    provider = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
    key = "OLLAMA_MODEL_FALLBACK" if provider == "ollama" else "MODEL_FALLBACK"
    return os.getenv(key, "").strip()


# ---------------------------------------------------------------------------
# Checkpointer - устойчивост на състоянието (improvement.md §2.2)
# ---------------------------------------------------------------------------
# С включен checkpointer всяка стъпка на графа се записва в SQLite файл:
# крашнал run се възобновява от последния завършен възел (същият
# thread_id), вместо да започва отначало (и да плаща токъните повторно).
# Checkpoint историята е и одитна следа - кой възел какво е решил.
# В продукция файлът се заменя с PostgresSaver - интерфейсът е същият.


def get_checkpointer():
    """
    SQLite checkpointer, ако е конфигуриран (CHECKPOINT_SQLITE_PATH).

    Връща None при липсваща настройка - графът тогава работи както
    досега, изцяло в паметта. Import-ът е lazy: пакетът
    langgraph-checkpoint-sqlite не е нужен, докато не се включи.
    """
    path = os.getenv("CHECKPOINT_SQLITE_PATH", "").strip()
    if not path:
        return None

    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    # check_same_thread=False: Streamlit/LangGraph могат да пипат
    # връзката от различни нишки; SqliteSaver си слага собствен lock.
    return SqliteSaver(sqlite3.connect(path, check_same_thread=False))


# ---------------------------------------------------------------------------
# Бюджетен контрол на изпълнението (improvement.md §5.3)
# ---------------------------------------------------------------------------
# Твърд лимит на разхода за ЕДИН run: визуализациите (main.py/app.py)
# проверяват натрупаната цена след всяка стъпка от стрийма и прекратяват
# изпълнението, ако лимитът е надвишен. Проверката е в консуматора (не в
# графа), защото usage метаданните се събират от callback-а там.


def run_budget_usd() -> float:
    """Лимитът в долари за един run (MAX_COST_USD_PER_RUN). 0 = изключен."""
    try:
        return float(os.getenv("MAX_COST_USD_PER_RUN", "0") or 0)
    except ValueError:
        return 0.0


def total_cost_usd(usage_metadata: dict) -> float:
    """Общата цена на run-а до момента - сума по всички използвани модели.
    Модели без известна цена (локални) се броят като $0."""
    return sum(
        estimate_cost_usd(model, usage) or 0.0
        for model, usage in usage_metadata.items()
    )


def budget_exceeded(usage_metadata: dict) -> bool:
    """True, ако има зададен бюджет и натрупаната цена го надвишава."""
    budget = run_budget_usd()
    return budget > 0 and total_cost_usd(usage_metadata) > budget
