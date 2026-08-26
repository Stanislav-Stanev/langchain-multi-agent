"""
Входна точка на Multi-Bot - мултиагентната SDLC система.

Стартиране:
    python main.py                      -> демо задача (тикет DEV-101)
    python main.py "твоя задача тук"    -> собствена задача

Език на интерфейса и агентите: APP_LANG=bg|en в .env (по подразбиране bg).
Режим: APP_MODE=prod|demo в .env (по подразбиране prod; в PowerShell за
еднократна смяна: $env:APP_MODE="demo"; python main.py).

Примери за задачи:
    python main.py "Имплементирай тикет DEV-102"
    python main.py "Напиши функция, която обръща string наобратно"

Визуализация: скриптът показва ЦЕЛИЯ процес на живо -
  1. всяко решение на супервайзора (кой е следващ и ЗАЩО);
  2. вътрешната работа на всеки агент (кой инструмент вика,
     с какви аргументи и какво връща инструментът);
  3. финалния отговор на всеки агент, плановете и стъпките;
  4. Human-in-the-Loop портите: графът спира и чака решение от конзолата
     ([a] одобри / [r: указания] промени / [q] прекрати);
  5. накрая - обобщение на маршрута, токъните, цената и папката с артефактите.
"""

import hashlib
import os
import sys
import uuid
from pathlib import Path

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
from src.hitl import auto_approve, enabled_gates
from src.i18n import t
from src.modes import get_mode, validate_prod_config
from src.run_tracker import RunTracker

LINE = "=" * 70
THIN = "-" * 70

# ASCII схема на архитектурата - показваме я в началото, за да е ясно
# какво ще наблюдаваме по време на изпълнението.
ARCHITECTURE = r"""
   user -> init_run -> SUPERVISOR (triage)
                          |
     ANALYST -> dev_plan -> [approve_plan] -> DEVELOPER -> qa_plan -> QA
                                                  ^                   |
                                                  +--- NEEDS_WORK ----+
                                                                      v
                                              [approve_publish] -> finalize
   [ ... ] = Human-in-the-Loop порта (HITL_GATES)
"""

# Инструментите, с които агентите отчитат прогреса по плановете
PLAN_TOOLS = {"update_plan_step", "update_test_case"}


def shorten(text, limit: int = 250) -> str:
    """Съкращава дълъг текст, за да не задръства конзолата."""
    text = str(text).strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[:limit] + t("shortened", total=len(text))


def indent(text: str, prefix: str = "  ") -> str:
    """Отмества блок текст навътре - визуално отделя отговорите."""
    return "\n".join(prefix + line for line in str(text).splitlines())


def ask_human(payload: dict) -> str:
    """
    Human-in-the-Loop решение от конзолата.

    Показва какво чака одобрение (порта + преглед на артефакта) и чете
    решението от stdin. Без интерактивна конзола: HITL_AUTO_APPROVE=1
    одобрява автоматично (и това се записва в журнала), иначе безопасният
    изход е прекратяване - системата не продължава „на сляпо".
    """
    print(t("hitl_prompt", gate=payload.get("gate", "?")))
    if payload.get("artifact"):
        print(t("plan_file_line", name=payload.get("title", "artifact"), path=payload["artifact"]))
    preview = str(payload.get("preview", "")).strip()
    if preview:
        print(indent(preview, "  | "))

    if not sys.stdin.isatty():
        if auto_approve():
            return "a"
        print(t("hitl_no_tty"))
        return "q"

    answer = input(t("hitl_options")).strip()
    return answer or "q"


def main() -> None:
    # Windows конзолата (и пренасочването към файл) често не е UTF-8 -
    # без този ред кирилицата може да счупи print() с UnicodeEncodeError.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # Взимаме задачата от командния ред или ползваме демото
    task = sys.argv[1] if len(sys.argv) > 1 else t("default_task")
    mode = get_mode()
    gates = enabled_gates(mode)

    print(LINE)
    print(t("app_title"))
    print(LINE)
    print(ARCHITECTURE)
    print(f"{t('task_label')}: {task}")
    print(t("mode_line", mode=mode))
    print(t("hitl_line", gates=", ".join(gates) or "-"))

    # Prod режимът има предварителни условия (репозитории, Jira) - ясна
    # грешка ПРЕДИ да се сглоби графът, вместо срив по средата.
    problems = validate_prod_config(mode)
    if problems:
        print("\n" + t("prod_problems_title"))
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    if mode == "prod":
        from src.jira_mcp import JiraMcpSettings  # lazy: само prod ползва mcp
        from src.modes import prod_repos

        jira = JiraMcpSettings.from_env()
        print(t("jira_line", cloud=jira.cloud_id, auth=jira.auth))
        print(t("repos_line", repos=", ".join(r.display for r in prod_repos())))

    # Сглобяваме графа (виж src/graph.py за архитектурата).
    # С CHECKPOINT_SQLITE_PATH в .env всяка стъпка се записва в SQLite:
    # прекъснат run със СЪЩИЯ thread_id продължава оттам, докъдето е
    # стигнал (improvement.md §2.2). HITL портите изискват checkpointer -
    # без SQLite build_graph слага InMemorySaver (валиден за този процес).
    checkpointer = get_checkpointer()
    graph = build_graph(checkpointer=checkpointer, mode=mode, hitl_gates=gates)

    run_config: dict = {"recursion_limit": 40}
    if checkpointer is not None:
        # Нишката = задачата: повторен старт със същата задача (или с
        # изричен THREAD_ID) възобновява същия разговор.
        thread_id = os.getenv("THREAD_ID") or hashlib.sha1(task.encode()).hexdigest()[:12]
        run_config["configurable"] = {"thread_id": thread_id}
        print(t("thread_line", thread=thread_id))
    elif graph.checkpointer is not None:
        # InMemorySaver (само за HITL) - уникална нишка за този run
        run_config["configurable"] = {"thread_id": uuid.uuid4().hex[:12]}

    step_no = 0    # пореден номер на стъпка в главния граф (за четимост)
    route = []     # маршрутът през възлите - за финалното обобщение
    tool_calls_count = 0
    final_status = ""  # APPROVED | ESCALATED | NO_ACTION | ABORTED (от графа)
    run_dir = ""       # директорията с артефактите на run-а
    hitl_count = 0
    publish_status = ""  # PUBLISHED | PUBLISH_FAILED | SKIPPED* (prod, от finalize)
    pr_urls: dict = {}
    publish_errors: list = []

    # Callback-ът улавя usage_metadata от ВСЯКО LLM извикване в графа
    # (вкл. супервайзора) и ги сумира по модел - за отчета накрая.
    usage_cb = UsageMetadataCallbackHandler()

    # .stream() изпълнява графа стъпка по стъпка и ни дава резултата
    # на ВСЕКИ възел веднага щом приключи - идеално за наблюдение.
    #
    # subgraphs=True е ключът към пълната визуализация: дава ни събития
    # и от ВЪТРЕШНИТЕ графи на агентите (ReAct цикъла: извикване на
    # инструмент -> резултат), не само от възлите на главния граф.
    # Всяко събитие тогава е двойка (namespace, update):
    #   namespace == ()               -> възел от главния граф
    #   namespace == ("analyst:...",) -> стъпка ВЪТРЕ в analyst агента
    #
    # Human-in-the-Loop: когда порта извика interrupt(), стриймът връща
    # {"__interrupt__": ...} и спира. Питаме човека и продължаваме със
    # Command(resume=решение) - цикълът по-долу се върти, докато има порти.
    pending_input = {"messages": [HumanMessage(content=task)]}
    while pending_input is not None:
        interrupt_payload = None
        stop = False

        for namespace, step in graph.stream(
            pending_input,
            config={**run_config, "callbacks": [usage_cb]},
            stream_mode="updates",
            subgraphs=True,
        ):
            if not namespace:
                # --- Събитие от ГЛАВНИЯ граф ---------------------------------
                if "__interrupt__" in step:
                    interrupt_payload = step["__interrupt__"][0].value
                    continue
                for node_name, update in step.items():
                    step_no += 1
                    final_status = update.get("final_status") or final_status
                    if update.get("run_dir"):
                        run_dir = update["run_dir"]
                        print("\n" + t("run_dir_line", path=run_dir))
                    if node_name == "supervisor":
                        nxt = update["next"]
                        route.append(nxt)
                        print(f"\n{THIN}")
                        print(t("step_supervisor", n=step_no, next=nxt.upper()))
                        print(t("reason", reason=update["reason"]))
                        if nxt != "FINISH":
                            print(t("handoff", agent=nxt.upper()))
                    else:
                        if update.get("messages") and node_name in ("analyst", "developer", "qa", "dev_plan", "qa_plan"):
                            print("\n" + t("final_answer", n=step_no, agent=node_name.upper()))
                            print(indent(extract_text(update["messages"][-1].content), "  | "))
                        if node_name in ("dev_plan", "qa_plan") and run_dir:
                            name = "implementation-plan.md" if node_name == "dev_plan" else "qa-plan.md"
                            print(t("plan_file_line", name=name, path=str(Path(run_dir) / name)))
                        if update.get("hitl_decisions"):
                            hitl_count = len(update["hitl_decisions"])
                        if update.get("publish_status"):
                            publish_status = update["publish_status"]
                            pr_urls = update.get("pr_urls") or {}
                            publish_errors = update.get("publish_errors") or []
                        # Детерминистичният преход на възела (следваща стъпка + защо):
                        # всеки възел записва next/reason в състоянието.
                        if update.get("next"):
                            route.append(update["next"])
                            print(t("reason", reason=update.get("reason", "")))

                # Бюджетна спирачка (improvement.md §5.3): проверяваме
                # натрупаната цена след всяка стъпка от главния граф.
                if budget_exceeded(usage_cb.usage_metadata):
                    spent = total_cost_usd(usage_cb.usage_metadata)
                    print("\n" + t(
                        "budget_stop",
                        limit=f"{run_budget_usd():.2f}",
                        spent=f"{spent:.4f}",
                    ))
                    final_status = "BUDGET_EXCEEDED"
                    stop = True
                    break
            else:
                # --- Събитие ОТВЪТРЕ в агент (неговият ReAct цикъл) ----------
                # namespace[0] е например "analyst:<uuid>" - взимаме името.
                worker = namespace[0].split(":")[0]
                for update in step.values():
                    for msg in (update or {}).get("messages", []):
                        if isinstance(msg, AIMessage) and msg.tool_calls:
                            for tc in msg.tool_calls:
                                tool_calls_count += 1
                                print(t("tool_call", agent=worker, tool=tc["name"], args=tc["args"]))
                        elif isinstance(msg, ToolMessage):
                            print(t(
                                "tool_result",
                                agent=worker,
                                tool=msg.name,
                                result=shorten(extract_text(msg.content)),
                            ))

        if stop or interrupt_payload is None:
            pending_input = None
        else:
            pending_input = Command(resume=ask_human(interrupt_payload))

    # --- Финално обобщение на процеса ------------------------------------
    print(f"\n{LINE}")
    print(t("summary_title"))
    print(LINE)
    print(t("route", route="START -> " + " -> ".join(route)))
    if final_status:
        print(t("final_status_line", status=final_status))
    print(t("steps_line", steps=step_no, tools=tool_calls_count))
    if hitl_count:
        print(t("hitl_summary_line", n=hitl_count))
    if publish_status:
        print(t("publish_status_line", status=publish_status))
        for repo, url in pr_urls.items():
            print(t("pr_line", repo=repo, url=url))
        if publish_errors:
            print(t("publish_errors_line", errors="; ".join(publish_errors)))
    print(t("tokens_title"))
    total_cost = 0.0
    if usage_cb.usage_metadata:
        for model, u in usage_cb.usage_metadata.items():
            cost = estimate_cost_usd(model, u)
            if cost is None:
                cost_label = t("local_model_cost")
            else:
                total_cost += cost
                cost_label = f"~${cost:.4f}"
            print(t(
                "token_line",
                model=model,
                inp=f"{u.get('input_tokens', 0):,}",
                out=f"{u.get('output_tokens', 0):,}",
                total=f"{u.get('total_tokens', 0):,}",
                cost=cost_label,
            ))
        if total_cost > 0:
            print(t("total_cost", cost=f"{total_cost:.4f}"))
    else:
        print(t("no_token_data"))
    if run_dir:
        # Токъните/цената ги знае само консуматорът - дописваме ги в summary.json
        tracker = RunTracker(run_dir)
        tracker.write_usage(dict(usage_cb.usage_metadata), total_cost)
        if final_status == "BUDGET_EXCEEDED":
            tracker.event(node="consumer", final_status=final_status)
        print(t("artifacts_line", run_dir=run_dir))
    print(t("done"))
    print(LINE)


if __name__ == "__main__":
    main()
