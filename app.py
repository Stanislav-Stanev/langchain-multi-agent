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

Режим (prod / demo) и Human-in-the-Loop порти: също от страничната лента.
При порта графът спира; UI-ят показва артефакта (напр. плана) с бутони
Одобри / Поискай промени / Прекрати и продължава със Command(resume=...).
Streamlit изпълнява скрипта наново при всеки клик, затова ЦЯЛОТО състояние
на run-а (събития, конфигурация, чакаща порта) живее в st.session_state.
"""

import hashlib
import os
import time
import uuid
from pathlib import Path

import streamlit as st
from dotenv import dotenv_values, load_dotenv
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from src.config import (
    budget_exceeded,
    estimate_cost_usd,
    get_checkpointer,
    run_budget_usd,
    total_cost_usd,
)
from src.graph import build_graph, extract_text
from src.hitl import GATES, default_gates
from src.i18n import SUPPORTED_LANGS, t
from src.modes import MODES, validate_prod_config
from src.run_tracker import RunTracker

load_dotenv()

# Икони на възлите - чисто визуална украса за по-лесно сканиране с очи
ICONS = {
    "supervisor": "🧭", "analyst": "📋", "developer": "💻", "qa": "🔎",
    "init_run": "📁", "dev_plan": "📝", "qa_plan": "🧪", "finalize": "🏁",
    "approve_plan": "🙋", "approve_publish": "🙋", "escalation_gate": "🙋",
}
PLAN_TOOLS = {"update_plan_step", "update_test_case"}

st.set_page_config(page_title="Multi-Bot", page_icon="🤖", layout="wide")


# ---------------------------------------------------------------------------
# Странична лента: настройки
# ---------------------------------------------------------------------------
# Стойностите по подразбиране се четат ДИРЕКТНО от .env файла
# (dotenv_values), а не от os.environ - процесът може да е наследил
# променливи от обвивката, която го е пуснала, и това би подвело UI-я.
_env_file = dotenv_values()
_default_provider = (_env_file.get("LLM_PROVIDER") or "anthropic").strip().lower()
_default_lang = (_env_file.get("APP_LANG") or "bg").strip().lower()
_default_mode = (_env_file.get("APP_MODE") or "prod").strip().lower()
_env_gates = _env_file.get("HITL_GATES")

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

    # Режим: prod по подразбиране (реалната работа), demo - за обучение
    mode = st.selectbox(
        t("ui_mode"),
        list(MODES)[::-1],  # prod първи
        index=0 if _default_mode != "demo" else 1,
        format_func=lambda m: {"prod": t("ui_mode_prod"), "demo": t("ui_mode_demo")}[m],
        help=t("ui_mode_help"),
    )
    os.environ["APP_MODE"] = mode

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

    # Human-in-the-Loop порти: по подразбиране според режима (или HITL_GATES от .env)
    st.markdown(f"**{t('ui_hitl_gates')}**", help=t("ui_hitl_help"))
    if _env_gates is not None:
        _gate_defaults = tuple(g.strip() for g in _env_gates.split(",") if g.strip())
    else:
        _gate_defaults = default_gates(mode)
    selected_gates = tuple(
        gate
        for gate in GATES
        if (gate != "publish" or mode == "prod")
        and st.checkbox(t(f"ui_hitl_gate_{gate}"), value=gate in _gate_defaults, key=f"gate_{gate}_{mode}")
    )
    os.environ["HITL_GATES"] = ",".join(selected_gates)

    # Prod предварителни условия / demo подсказка
    problems = validate_prod_config(mode)
    if mode == "prod" and problems:
        st.error(t("ui_prod_config_error", problems="\n".join(f"- {p}" for p in problems)))
    if mode == "demo":
        st.info(t("ui_demo_hint"))

    st.divider()
    st.markdown(t("ui_legend"))

# Превключвателят действа чрез средата - get_llm() я чете при всяко
# извикване на build_graph() (виж src/config.py).
os.environ["LLM_PROVIDER"] = provider


@st.cache_resource(show_spinner="⏳")
def get_graph(provider_key: str, lang_key: str, mode_key: str, gates_key: tuple):
    """Кешира по един компилиран граф на (доставчик, език, режим, порти) -
    промптовете на агентите се фиксират при сглобяване, затова езикът и
    режимът са част от ключа. С CHECKPOINT_SQLITE_PATH графът пази всяка
    стъпка в SQLite (resume); иначе при включени порти - InMemorySaver."""
    os.environ["LLM_PROVIDER"] = provider_key
    os.environ["APP_LANG"] = lang_key
    os.environ["APP_MODE"] = mode_key
    return build_graph(checkpointer=get_checkpointer(), mode=mode_key, hitl_gates=gates_key)


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
    elif kind == "route":
        # Детерминистичен преход (DoD / rework / одобрение) - само причината
        st.caption(f"🧭 {e['reason']}")
    elif kind == "run_dir":
        st.caption(t("ui_run_dir", path=e["path"]))
    elif kind == "final":
        icon = ICONS.get(e["agent"], "🤖")
        with st.chat_message("assistant"):
            st.markdown(t("ui_step_final", icon=icon, n=e["step"], agent=e["agent"].upper()))
            st.markdown(e["text"])
    elif kind == "plan":
        with st.expander(t("ui_plan_expander", title=e["title"], path=e["path"]), expanded=True):
            st.markdown(e["text"])
    elif kind == "plan_update":
        st.caption(t("ui_plan_update", agent=e["agent"], id=e["id"], status=e["status"]))
    elif kind == "tool_call":
        st.caption(t("ui_tool_call", agent=e["agent"], tool=e["tool"], args=e["args"]))
    elif kind == "tool_result":
        with st.expander(t("ui_tool_result", agent=e["agent"], tool=e["tool"])):
            st.text(e["text"])
    elif kind == "hitl":
        render_hitl_gate(e)
    elif kind == "hitl_decision":
        st.caption(t("ui_hitl_decided", gate=e["gate"], action=e["action"], by=e["by"], feedback=e["feedback"]))
    elif kind == "artifacts":
        with st.expander(t("ui_artifacts", run_dir=e["run_dir"])):
            st.markdown("\n".join(f"- `{f}`" for f in e["files"]))
    elif kind == "summary":
        st.success(e["text"])
    elif kind == "error":
        st.error(e["text"])


def render_hitl_gate(e: dict) -> None:
    """Панелът на Human-in-the-Loop портата: преглед + решение (само ако още чака)."""
    payload = e["payload"]
    gate = payload.get("gate", "?")
    with st.container(border=True):
        st.markdown(t("ui_hitl_title", gate=gate))
        if payload.get("artifact"):
            st.caption(f"`{payload['artifact']}`")
        if payload.get("preview"):
            with st.expander(payload.get("title", gate), expanded=True):
                st.markdown(str(payload["preview"]))

        run_state = st.session_state.get("run")
        pending = run_state and run_state.get("pending_gate")
        if not (pending and pending["id"] == e["id"]):
            return  # решението вече е взето - панелът е само история

        feedback = st.text_area(t("ui_hitl_feedback"), key=f"hitl_feedback_{e['id']}")
        options = payload.get("options", ["approve", "revise", "abort"])
        cols = st.columns(len(options))
        for col, action in zip(cols, options, strict=False):
            label = {"approve": t("ui_hitl_approve"), "revise": t("ui_hitl_revise"), "abort": t("ui_hitl_abort")}[action]
            if col.button(label, key=f"hitl_{action}_{e['id']}", use_container_width=True):
                if action == "revise" and not feedback.strip():
                    st.warning(t("ui_hitl_feedback_required"))
                    return
                decide_gate(action, feedback.strip())
                st.rerun()


def decide_gate(action: str, feedback: str) -> None:
    """Записва човешкото решение и подготвя продължението на графа."""
    run_state = st.session_state.run
    gate = run_state["pending_gate"]["payload"].get("gate", "?")
    decision = {"action": action, "feedback": feedback, "by": "streamlit"}
    run_state["pending_gate"] = None
    run_state["pending_input"] = Command(resume=decision)
    st.session_state.events.append(
        {"kind": "hitl_decision", "gate": gate, "action": action, "by": "streamlit", "feedback": feedback}
    )


if "events" not in st.session_state:
    st.session_state.events = []
if "run" not in st.session_state:
    st.session_state.run = None


# ---------------------------------------------------------------------------
# Основна страница: задача + изпълнение на живо
# ---------------------------------------------------------------------------
st.title(t("ui_page_title"))
st.caption(t("ui_mode_banner", mode=mode, gates=", ".join(selected_gates) or "-"))

task = st.text_area(t("task_label"), t("default_task"), height=80)
can_run = bool(task.strip()) and not (mode == "prod" and problems)
run_clicked = st.button(t("ui_run"), type="primary", disabled=not can_run)

if run_clicked:
    # Нов run: чисто състояние. С SQLite checkpointer нишката = задачата
    # (resume семантика); с InMemorySaver (само за HITL) - уникална нишка.
    st.session_state.events = []
    run_config: dict = {"recursion_limit": 40}
    if os.getenv("CHECKPOINT_SQLITE_PATH", "").strip():
        run_config["configurable"] = {"thread_id": hashlib.sha1(task.encode()).hexdigest()[:12]}
    elif selected_gates:
        run_config["configurable"] = {"thread_id": uuid.uuid4().hex[:12]}
    st.session_state.run = {
        "graph_key": (provider, lang, mode, selected_gates),
        "config": run_config,
        "pending_input": {"messages": [HumanMessage(content=task)]},
        "pending_gate": None,
        "route": [],
        "step_no": 0,
        "tool_calls": 0,
        "final_status": "",
        "run_dir": "",
        "t0": time.time(),
        # Callback-ът улавя usage_metadata от ВСЯКО LLM извикване в графа
        # (вкл. супервайзора, чиито съобщения не се виждат в стрийма).
        "usage_cb": UsageMetadataCallbackHandler(),
        "gate_seq": 0,
    }

run_state = st.session_state.run

# Показваме натрупаните събития (нов run, продължение след порта или rerun)
for e in st.session_state.events:
    render_event(e)

if run_state and run_state.get("pending_input") is not None:
    events = st.session_state.events

    def emit(e: dict) -> None:
        events.append(e)
        render_event(e)

    def tick(label: str) -> None:
        status.update(label=t("ui_status_seconds", label=label, sec=int(time.time() - run_state["t0"])))

    graph = get_graph(*run_state["graph_key"])
    usage_cb = run_state["usage_cb"]
    pending_input = run_state["pending_input"]
    run_state["pending_input"] = None
    interrupt_payload = None

    # Живата статус лента показва КАКВО точно се случва в момента -
    # иначе при бавен модел изглежда, че нищо не работи.
    status = st.status(t("ui_status_supervisor_first"), expanded=False)

    try:
        # Същата стрийминг логика като main.py: subgraphs=True ни дава и
        # ВЪТРЕШНИТЕ стъпки на агентите (инструменти), не само възлите.
        for namespace, step in graph.stream(
            pending_input,
            config={**run_state["config"], "callbacks": [usage_cb]},
            stream_mode="updates",
            subgraphs=True,
        ):
            if not namespace:
                # --- Събитие от главния граф ------------------------------
                if "__interrupt__" in step:
                    interrupt_payload = step["__interrupt__"][0].value
                    continue
                for node_name, update in step.items():
                    run_state["step_no"] += 1
                    step_no = run_state["step_no"]
                    run_state["final_status"] = update.get("final_status") or run_state["final_status"]
                    if update.get("run_dir"):
                        run_state["run_dir"] = update["run_dir"]
                        emit({"kind": "run_dir", "path": update["run_dir"]})
                    if node_name == "supervisor":
                        nxt = update["next"]
                        run_state["route"].append(nxt)
                        emit({
                            "kind": "supervisor",
                            "step": step_no,
                            "next": nxt,
                            "reason": update["reason"],
                        })
                        if nxt != "FINISH":
                            tick(t("ui_status_working", icon=ICONS.get(nxt, "🤖"), agent=nxt.upper()))
                    else:
                        if node_name in ("dev_plan", "qa_plan") and update.get("messages"):
                            name = "implementation-plan.md" if node_name == "dev_plan" else "qa-plan.md"
                            emit({
                                "kind": "plan",
                                "title": t("ui_plan_title_implementation" if node_name == "dev_plan" else "ui_plan_title_test"),
                                "path": str(Path(run_state["run_dir"]) / name) if run_state["run_dir"] else name,
                                "text": extract_text(update["messages"][-1].content),
                            })
                        elif update.get("messages") and node_name in ("analyst", "developer", "qa"):
                            emit({
                                "kind": "final",
                                "step": step_no,
                                "agent": node_name,
                                "text": extract_text(update["messages"][-1].content),
                            })
                        # Детерминистичният преход на възела (DoD, rework,
                        # одобрение, порта) - показваме причината като route събитие.
                        if update.get("next"):
                            run_state["route"].append(update["next"])
                            emit({"kind": "route", "reason": update.get("reason", "")})
                            nxt = update["next"]
                            if nxt != "FINISH":
                                tick(t("ui_status_working", icon=ICONS.get(nxt, "🤖"), agent=nxt.upper()))

                # Бюджетна спирачка (improvement.md §5.3)
                if budget_exceeded(usage_cb.usage_metadata):
                    spent = total_cost_usd(usage_cb.usage_metadata)
                    emit({"kind": "error", "text": t(
                        "budget_stop",
                        limit=f"{run_budget_usd():.2f}",
                        spent=f"{spent:.4f}",
                    )})
                    run_state["final_status"] = "BUDGET_EXCEEDED"
                    interrupt_payload = None
                    break
            else:
                # --- Събитие отвътре в агент (ReAct цикъл) ----------------
                worker = namespace[0].split(":")[0]
                for update in step.values():
                    for msg in (update or {}).get("messages", []):
                        if isinstance(msg, AIMessage) and msg.tool_calls:
                            for tc in msg.tool_calls:
                                run_state["tool_calls"] += 1
                                if tc["name"] in PLAN_TOOLS:
                                    emit({
                                        "kind": "plan_update",
                                        "agent": worker,
                                        "id": tc["args"].get("step_id") or tc["args"].get("case_id", "?"),
                                        "status": tc["args"].get("status", "?"),
                                    })
                                else:
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

        if interrupt_payload is not None:
            # Human-in-the-Loop: графът чака решение - панелът с бутоните
            run_state["gate_seq"] += 1
            gate_event = {"kind": "hitl", "id": run_state["gate_seq"], "payload": interrupt_payload}
            run_state["pending_gate"] = {"id": gate_event["id"], "payload": interrupt_payload}
            status.update(label=t("ui_hitl_status", gate=interrupt_payload.get("gate", "?")), state="running")
            emit(gate_event)
        else:
            elapsed = int(time.time() - run_state["t0"])
            status.update(label=t("ui_status_done", sec=elapsed), state="complete")

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

            summary_text = t(
                "ui_summary",
                route="START → " + " → ".join(run_state["route"]),
                steps=run_state["step_no"],
                tools=run_state["tool_calls"],
                sec=elapsed,
                token_lines="\n\n".join(token_lines),
            )
            if run_state["final_status"]:
                summary_text = t("final_status_line", status=run_state["final_status"]) + "\n\n" + summary_text
            emit({"kind": "summary", "text": summary_text})

            if run_state["run_dir"]:
                tracker = RunTracker(run_state["run_dir"])
                tracker.write_usage(dict(usage_cb.usage_metadata), total_cost)
                if run_state["final_status"] == "BUDGET_EXCEEDED":
                    tracker.event(node="consumer", final_status="BUDGET_EXCEEDED")
                emit({"kind": "artifacts", "run_dir": run_state["run_dir"], "files": tracker.list_artifacts()})
    except Exception as exc:  # показваме грешката в UI-я, не само в терминала
        status.update(label=t("ui_status_error"), state="error")
        emit({"kind": "error", "text": t("ui_error", error=exc)})
