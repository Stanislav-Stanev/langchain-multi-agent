"""
Входна точка на Multi-Bot - мултиагентната SDLC система.

Стартиране:
    python main.py                      -> демо задача (тикет DEV-101)
    python main.py "твоя задача тук"    -> собствена задача

Език на интерфейса и агентите: APP_LANG=bg|en в .env (по подразбиране bg).

Примери за задачи:
    python main.py "Имплементирай тикет DEV-102"
    python main.py "Напиши функция, която обръща string наобратно"

Визуализация: скриптът показва ЦЕЛИЯ процес на живо -
  1. всяко решение на супервайзора (кой е следващ и ЗАЩО);
  2. вътрешната работа на всеки агент (кой инструмент вика,
     с какви аргументи и какво връща инструментът);
  3. финалния отговор на всеки агент;
  4. накрая - обобщение на маршрута, токъните и цената.
"""

import sys

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.config import (
    budget_exceeded,
    estimate_cost_usd,
    run_budget_usd,
    total_cost_usd,
)
from src.graph import build_graph, extract_text
from src.i18n import t

LINE = "=" * 70
THIN = "-" * 70

# ASCII схема на архитектурата - показваме я в началото, за да е ясно
# какво ще наблюдаваме по време на изпълнението.
ARCHITECTURE = r"""
                        +--------------+
             user --->  |  SUPERVISOR  | <---+
                        +--------------+     |
                         /     |      \      |
                        v      v       v     |
                  +--------+ +-----------+ +------+
                  |ANALYST | | DEVELOPER | |  QA  |
                  +--------+ +-----------+ +------+
"""


def shorten(text, limit: int = 250) -> str:
    """Съкращава дълъг текст, за да не задръства конзолата."""
    text = str(text).strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[:limit] + t("shortened", total=len(text))


def indent(text: str, prefix: str = "  ") -> str:
    """Отмества блок текст навътре - визуално отделя отговорите."""
    return "\n".join(prefix + line for line in str(text).splitlines())


def main() -> None:
    # Windows конзолата (и пренасочването към файл) често не е UTF-8 -
    # без този ред кирилицата може да счупи print() с UnicodeEncodeError.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # Взимаме задачата от командния ред или ползваме демото
    task = sys.argv[1] if len(sys.argv) > 1 else t("default_task")

    print(LINE)
    print(t("app_title"))
    print(LINE)
    print(ARCHITECTURE)
    print(f"{t('task_label')}: {task}")

    # Сглобяваме графа (виж src/graph.py за архитектурата)
    graph = build_graph()

    step_no = 0    # пореден номер на стъпка в главния граф (за четимост)
    route = []     # маршрутът през възлите - за финалното обобщение
    tool_calls_count = 0
    final_status = ""  # APPROVED | ESCALATED | NO_ACTION (от графа)

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
    # recursion_limit е защита срещу безкраен цикъл: ако супервайзорът
    # никога не каже FINISH, графът спира принудително след N стъпки.
    for namespace, step in graph.stream(
        {"messages": [HumanMessage(content=task)]},
        config={"recursion_limit": 25, "callbacks": [usage_cb]},
        stream_mode="updates",
        subgraphs=True,
    ):
        if not namespace:
            # --- Събитие от ГЛАВНИЯ граф ---------------------------------
            for node_name, update in step.items():
                step_no += 1
                final_status = update.get("final_status") or final_status
                if node_name == "supervisor":
                    nxt = update["next"]
                    route.append(nxt)
                    print(f"\n{THIN}")
                    print(t("step_supervisor", n=step_no, next=nxt.upper()))
                    print(t("reason", reason=update["reason"]))
                    if nxt != "FINISH":
                        print(t("handoff", agent=nxt.upper()))
                else:
                    if update.get("messages"):
                        print("\n" + t("final_answer", n=step_no, agent=node_name.upper()))
                        print(indent(extract_text(update["messages"][-1].content), "  | "))
                    # Детерминистичният преход на възела (следваща стъпка + защо):
                    # всеки работник записва next/reason в състоянието.
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

    # --- Финално обобщение на процеса ------------------------------------
    print(f"\n{LINE}")
    print(t("summary_title"))
    print(LINE)
    print(t("route", route="START -> " + " -> ".join(route)))
    if final_status:
        print(t("final_status_line", status=final_status))
    print(t("steps_line", steps=step_no, tools=tool_calls_count))
    print(t("tokens_title"))
    if usage_cb.usage_metadata:
        total_cost = 0.0
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
    print(t("done"))
    print(LINE)


if __name__ == "__main__":
    main()
