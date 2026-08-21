"""
Локален уеб UI (Streamlit) за визуализация на мултиагентния workflow.

Стартиране:
    streamlit run app.py
    -> отваря се в браузъра на http://localhost:8501

Работи изцяло локално - никакви външни UI услуги (LangSmith/Studio).
С LLM_PROVIDER=ollama (или превключвателя в страничната лента) целият
стек е офлайн: локален модел + локален UI.

Езикът (български/английски) се сменя от страничната лента - тя
превключва APP_LANG, който i18n модулът чете при всеки текст, а
build_graph() взима при сглобяване (за промптовете на агентите).
"""

import os
import time

import streamlit as st
from dotenv import dotenv_values, load_dotenv
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.config import estimate_cost_usd
from src.graph import build_graph, extract_text
from src.i18n import SUPPORTED_LANGS, t

load_dotenv()

# Икони на агентите - чисто визуална украса за по-лесно сканиране с очи
ICONS = {"supervisor": "🧭", "analyst": "📋", "developer": "💻", "qa": "🔎"}

st.set_page_config(page_title="SDLC Multi-Agent", page_icon="🤖", layout="wide")


# ---------------------------------------------------------------------------
# Странична лента: настройки
# ---------------------------------------------------------------------------
# Стойностите по подразбиране се четат ДИРЕКТНО от .env файла
# (dotenv_values), а не от os.environ - процесът може да е наследил
# променливи от обвивката, която го е пуснала, и това би подвело UI-я.
_env_file = dotenv_values()
_default_provider = (_env_file.get("LLM_PROVIDER") or "anthropic").strip().lower()
_default_lang = (_env_file.get("APP_LANG") or "bg").strip().lower()

with st.sidebar:
    # Езикът се избира ПЪРВИ, защото всички останали надписи зависят от него
    lang = st.radio(
        "Език / Language",
        list(SUPPORTED_LANGS),
        index=list(SUPPORTED_LANGS).index(_default_lang)
        if _default_lang in SUPPORTED_LANGS
        else 0,
        format_func=lambda code: {"bg": "🇧🇬 Български", "en": "🇬🇧 English"}[code],
        horizontal=True,
    )
    # i18n.get_lang() чете APP_LANG при всяко t() - смяната действа веднага
    os.environ["APP_LANG"] = lang

    st.title(t("ui_sidebar_title"))
    st.caption(t("ui_sidebar_caption"))

    provider = st.radio(
        t("ui_provider"),
        ["anthropic", "ollama"],
        index=0 if _default_provider == "anthropic" else 1,
        format_func=lambda p: {
            "anthropic": t("ui_provider_anthropic"),
            "ollama": t("ui_provider_ollama"),
        }[p],
        help=t("ui_provider_help"),
    )
    model_label = (
        os.getenv("MODEL_NAME", "claude-opus-5")
        if provider == "anthropic"
        else os.getenv("OLLAMA_MODEL", "qwen3:8b")
    )
    st.caption(t("ui_model", model=model_label))
    if provider == "ollama":
        st.warning(t("ui_ollama_warning"))

    st.divider()
    st.markdown(t("ui_legend"))

# Превключвателят действа чрез средата - get_llm() я чете при всяко
# извикване на build_graph() (виж src/config.py).
os.environ["LLM_PROVIDER"] = provider


@st.cache_resource(show_spinner="⏳")
def get_graph(provider_key: str, lang_key: str):
    """Кешира по един компилиран граф на (доставчик, език) - промптовете
    на агентите се фиксират при сглобяване, затова езикът е част от ключа."""
    os.environ["LLM_PROVIDER"] = provider_key
    os.environ["APP_LANG"] = lang_key
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
        st.info(t("ui_step_supervisor", n=e["step"], next=e["next"].upper(), reason=e["reason"]))
    elif kind == "final":
        icon = ICONS.get(e["agent"], "🤖")
        with st.chat_message("assistant"):
            st.markdown(t("ui_step_final", icon=icon, n=e["step"], agent=e["agent"].upper()))
            st.markdown(e["text"])
    elif kind == "tool_call":
        st.caption(t("ui_tool_call", agent=e["agent"], tool=e["tool"], args=e["args"]))
    elif kind == "tool_result":
        with st.expander(t("ui_tool_result", agent=e["agent"], tool=e["tool"])):
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
st.title(t("ui_page_title"))

task = st.text_area(t("task_label"), t("default_task"), height=80)
run = st.button(t("ui_run"), type="primary", disabled=not task.strip())

if run:
    st.session_state.events = []
    events = st.session_state.events

    def emit(e: dict) -> None:
        events.append(e)
        render_event(e)

    graph = get_graph(provider, lang)
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
    status = st.status(t("ui_status_supervisor_first"), expanded=False)

    def tick(label: str) -> None:
        status.update(label=t("ui_status_seconds", label=label, sec=int(time.time() - t0)))

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
                            tick(t("ui_status_working", icon=ICONS.get(nxt, "🤖"), agent=nxt.upper()))
                    elif update.get("messages"):
                        emit({
                            "kind": "final",
                            "step": step_no,
                            "agent": node_name,
                            "text": extract_text(update["messages"][-1].content),
                        })
                        tick(t("ui_status_supervisor_next"))
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
                                tick(t("ui_status_tool", agent=worker, tool=tc["name"]))
                        elif isinstance(msg, ToolMessage):
                            emit({
                                "kind": "tool_result",
                                "agent": worker,
                                "tool": msg.name,
                                "text": extract_text(msg.content),
                            })

        status.update(label=t("ui_status_done", sec=int(time.time() - t0)), state="complete")

        # Консумирани токъни и цена в долари, сумирани по модел
        token_lines = []
        total_cost = 0.0
        for model, u in usage_cb.usage_metadata.items():
            cost = estimate_cost_usd(model, u)
            if cost is None:
                cost_label = t("local_model_cost")
            else:
                total_cost += cost
                cost_label = f"~${cost:.4f}"
            token_lines.append(t(
                "ui_token_line",
                model=model,
                inp=f"{u.get('input_tokens', 0):,}",
                out=f"{u.get('output_tokens', 0):,}",
                total=f"{u.get('total_tokens', 0):,}",
                cost=cost_label,
            ))
        if not token_lines:
            token_lines = [t("no_token_data").strip()]
        elif total_cost > 0:
            token_lines.append(t("ui_total_cost", cost=f"{total_cost:.4f}"))

        emit({
            "kind": "summary",
            "text": t(
                "ui_summary",
                route="START → " + " → ".join(route),
                steps=step_no,
                tools=tool_calls_count,
                sec=int(time.time() - t0),
                token_lines="\n\n".join(token_lines),
            ),
        })
    except Exception as exc:  # показваме грешката в UI-я, не само в терминала
        status.update(label=t("ui_status_error"), state="error")
        emit({"kind": "error", "text": t("ui_error", error=exc)})
else:
    # Rerun на скрипта (клик по друг елемент) - показваме последния резултат.
    for e in st.session_state.events:
        render_event(e)
