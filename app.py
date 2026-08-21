"""
Локален уеб UI (Streamlit) за визуализация на мултиагентния workflow.

Стартиране:
    streamlit run app.py
    -> отваря се в браузъра на http://localhost:8501

Работи изцяло локално - никакви външни UI услуги (LangSmith/Studio).
С LLM_PROVIDER=ollama (или превключвателя в страничната лента) целият
стек е офлайн: локален модел + локален UI.
"""

import os
import time

import streamlit as st
from dotenv import dotenv_values, load_dotenv
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.config import estimate_cost_usd
from src.graph import build_graph, extract_text

load_dotenv()

DEFAULT_TASK = "Имплементирай тикет DEV-101 и се увери, че кодът е качествен."

# Икони на агентите - чисто визуална украса за по-лесно сканиране с очи
ICONS = {"supervisor": "🧭", "analyst": "📋", "developer": "💻", "qa": "🔎"}

st.set_page_config(page_title="SDLC Мултиагентна система", page_icon="🤖", layout="wide")


# ---------------------------------------------------------------------------
# Странична лента: настройки
# ---------------------------------------------------------------------------
# Кой доставчик да е избран по подразбиране: четем ДИРЕКТНО от .env файла
# (dotenv_values), а не от os.environ - процесът може да е наследил
# LLM_PROVIDER от обвивката, която го е пуснала, и това би подвело UI-я.
_default_provider = (dotenv_values().get("LLM_PROVIDER") or "anthropic").strip().lower()

with st.sidebar:
    st.title("🤖 SDLC екип")
    st.caption("Supervisor + Analyst + Developer + QA (LangGraph)")

    provider = st.radio(
        "LLM доставчик",
        ["anthropic", "ollama"],
        index=0 if _default_provider == "anthropic" else 1,
        format_func=lambda p: {
            "anthropic": "Anthropic Claude (облак)",
            "ollama": "Ollama (локално, офлайн)",
        }[p],
        help="Ollama работи без интернет, но е по-бавен и качеството е под Claude.",
    )
    model_label = (
        os.getenv("MODEL_NAME", "claude-opus-5")
        if provider == "anthropic"
        else os.getenv("OLLAMA_MODEL", "qwen3:8b")
    )
    st.caption(f"Модел: `{model_label}`")
    if provider == "ollama":
        st.warning("Локалният модел е бавен: пълен цикъл отнема няколко минути.")

    st.divider()
    st.markdown(
        "**Как се чете визуализацията:**\n"
        "- 🧭 решение на супервайзора (кой е следващ и защо)\n"
        "- 🔧 агент извиква инструмент\n"
        "- 📥 резултат от инструмента\n"
        "- 💬 финален отговор на агента"
    )

# Превключвателят действа чрез средата - get_llm() я чете при всяко
# извикване на build_graph() (виж src/config.py).
os.environ["LLM_PROVIDER"] = provider


@st.cache_resource(show_spinner="Сглобяване на графа...")
def get_graph(provider_key: str):
    """Кешира по един компилиран граф на доставчик (сглобяването е евтино,
    но кешът пази UI-я от повторна работа при всеки rerun на Streamlit)."""
    os.environ["LLM_PROVIDER"] = provider_key
    return build_graph()


# ---------------------------------------------------------------------------
# Рендиране на събития
# ---------------------------------------------------------------------------
# Всяко събитие от графа се пази в session_state и се рендира от ЕДНА
# функция. Така: (1) виждаш стъпките на живо, докато графът работи;
# (2) резултатът НЕ изчезва при клик по който и да е елемент на UI-я
# (Streamlit изпълнява целия скрипт наново при всяко взаимодействие).


def render_event(e: dict) -> None:
    kind = e["kind"]
    if kind == "supervisor":
        st.info(
            f"{ICONS['supervisor']} **Стъпка {e['step']} · SUPERVISOR** "
            f"→ **{e['next'].upper()}**\n\n{e['reason']}"
        )
    elif kind == "final":
        icon = ICONS.get(e["agent"], "🤖")
        with st.chat_message("assistant"):
            st.markdown(f"{icon} **Стъпка {e['step']} · {e['agent'].upper()} - финален отговор**")
            st.markdown(e["text"])
    elif kind == "tool_call":
        st.caption(f"🔧 `{e['agent']}` извиква **{e['tool']}** `{e['args']}`")
    elif kind == "tool_result":
        with st.expander(f"📥 `{e['agent']}` ← резултат от **{e['tool']}**"):
            st.text(e["text"])
    elif kind == "summary":
        st.success(e["text"])
    elif kind == "error":
        st.error(e["text"])


if "events" not in st.session_state:
    st.session_state.events = []


# ---------------------------------------------------------------------------
# Основна страница: задача + изпълнение на живо
# ---------------------------------------------------------------------------
st.title("Мултиагентна SDLC система")

task = st.text_area("Задача", DEFAULT_TASK, height=80)
run = st.button("▶ Стартирай", type="primary", disabled=not task.strip())

if run:
    st.session_state.events = []
    events = st.session_state.events

    def emit(e: dict) -> None:
        events.append(e)
        render_event(e)

    graph = get_graph(provider)
    route: list[str] = []
    tool_calls_count = 0
    step_no = 0
    t0 = time.time()

    # Callback-ът улавя usage_metadata от ВСЯКО LLM извикване в графа
    # (вкл. супервайзора, чиито съобщения не се виждат в стрийма) и
    # ги сумира по модел - от него накрая четем консумираните токъни.
    usage_cb = UsageMetadataCallbackHandler()

    # Живата статус лента показва КАКВО точно се случва в момента -
    # иначе при бавен модел изглежда, че нищо не работи.
    status = st.status("🧭 Супервайзорът решава кой агент да работи пръв...", expanded=False)

    def tick(label: str) -> None:
        status.update(label=f"{label}  ·  {int(time.time() - t0)} сек")

    try:
        # Същата стрийминг логика като main.py: subgraphs=True ни дава и
        # ВЪТРЕШНИТЕ стъпки на агентите (инструменти), не само възлите.
        for namespace, step in graph.stream(
            {"messages": [HumanMessage(content=task)]},
            config={"recursion_limit": 25, "callbacks": [usage_cb]},
            stream_mode="updates",
            subgraphs=True,
        ):
            if not namespace:
                # --- Събитие от главния граф ------------------------------
                for node_name, update in step.items():
                    step_no += 1
                    if node_name == "supervisor":
                        nxt = update["next"]
                        route.append(nxt)
                        emit({
                            "kind": "supervisor",
                            "step": step_no,
                            "next": nxt,
                            "reason": update["reason"],
                        })
                        if nxt != "FINISH":
                            tick(f"{ICONS.get(nxt, '🤖')} {nxt.upper()} работи...")
                    elif update.get("messages"):
                        emit({
                            "kind": "final",
                            "step": step_no,
                            "agent": node_name,
                            "text": extract_text(update["messages"][-1].content),
                        })
                        tick("🧭 Супервайзорът преценява следващата стъпка...")
            else:
                # --- Събитие отвътре в агент (ReAct цикъл) ----------------
                worker = namespace[0].split(":")[0]
                for update in step.values():
                    for msg in (update or {}).get("messages", []):
                        if isinstance(msg, AIMessage) and msg.tool_calls:
                            for tc in msg.tool_calls:
                                tool_calls_count += 1
                                emit({
                                    "kind": "tool_call",
                                    "agent": worker,
                                    "tool": tc["name"],
                                    "args": tc["args"],
                                })
                                tick(f"🔧 {worker} изпълнява {tc['name']}...")
                        elif isinstance(msg, ToolMessage):
                            emit({
                                "kind": "tool_result",
                                "agent": worker,
                                "tool": msg.name,
                                "text": extract_text(msg.content),
                            })

        status.update(label=f"✅ Готово за {int(time.time() - t0)} сек", state="complete")

        # Консумирани токъни и цена в долари, сумирани по модел
        token_lines = []
        total_cost = 0.0
        for model, u in usage_cb.usage_metadata.items():
            cost = estimate_cost_usd(model, u)
            if cost is None:
                cost_label = "$0.00 (локален модел)"
            else:
                total_cost += cost
                cost_label = f"~${cost:.4f}"
            token_lines.append(
                f"`{model}`: вход {u.get('input_tokens', 0):,} · "
                f"изход {u.get('output_tokens', 0):,} · "
                f"общо {u.get('total_tokens', 0):,} · **{cost_label}**"
            )
        if not token_lines:
            token_lines = ["моделът не върна данни за токъни"]
        elif total_cost > 0:
            token_lines.append(f"**Обща цена: ~${total_cost:.4f}**")

        emit({
            "kind": "summary",
            "text": (
                f"**Готово!** Маршрут: START → {' → '.join(route)}\n\n"
                f"Стъпки в главния граф: {step_no} · "
                f"Извиквания на инструменти: {tool_calls_count} · "
                f"Време: {int(time.time() - t0)} сек\n\n"
                f"**Консумирани токъни:**\n\n" + "\n\n".join(token_lines)
            ),
        })
    except Exception as exc:  # показваме грешката в UI-я, не само в терминала
        status.update(label="❌ Грешка", state="error")
        emit({"kind": "error", "text": f"Изпълнението се провали: {exc}"})
else:
    # Rerun на скрипта (клик по друг елемент) - показваме последния резултат.
    for e in st.session_state.events:
        render_event(e)
