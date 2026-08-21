"""
Входна точка на мултиагентната SDLC система.

Стартиране:
    python main.py                      -> демо задача (тикет DEV-101)
    python main.py "твоя задача тук"    -> собствена задача

Примери за задачи:
    python main.py "Имплементирай тикет DEV-102"
    python main.py "Напиши функция, която обръща string наобратно"

Визуализация: скриптът показва ЦЕЛИЯ процес на живо -
  1. всяко решение на супервайзора (кой е следващ и ЗАЩО);
  2. вътрешната работа на всеки агент (кой инструмент вика,
     с какви аргументи и какво връща инструментът);
  3. финалния отговор на всеки агент;
  4. накрая - обобщение на маршрута през графа.
"""

import sys

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.config import estimate_cost_usd
from src.graph import build_graph, extract_text

# Демо задача по подразбиране - реферира тикет от "тикет системата"
DEFAULT_TASK = "Имплементирай тикет DEV-101 и се увери, че кодът е качествен."

LINE = "=" * 70
THIN = "-" * 70

# ASCII схема на архитектурата - показваме я в началото, за да е ясно
# какво ще наблюдаваме по време на изпълнението.
ARCHITECTURE = r"""
                        +--------------+
        потребител ---> |  SUPERVISOR  | <--- връща се след всеки агент
                        +--------------+
                         /     |      \
                        v      v       v
                  +--------+ +-----------+ +------+
                  |ANALYST | | DEVELOPER | |  QA  |
                  +--------+ +-----------+ +------+
"""


def shorten(text, limit: int = 250) -> str:
    """Съкращава дълъг текст, за да не задръства конзолата."""
    text = str(text).strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [съкратено, общо {len(text)} символа]"


def indent(text: str, prefix: str = "  ") -> str:
    """Отмества блок текст навътре - визуално отделя отговорите."""
    return "\n".join(prefix + line for line in str(text).splitlines())


def main() -> None:
    # Windows конзолата (и пренасочването към файл) често не е UTF-8 -
    # без този ред кирилицата може да счупи print() с UnicodeEncodeError.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # Взимаме задачата от командния ред или ползваме демото
    task = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TASK

    print(LINE)
    print("МУЛТИАГЕНТНА SDLC СИСТЕМА (Supervisor + Analyst + Developer + QA)")
    print(LINE)
    print(ARCHITECTURE)
    print(f"Задача: {task}")

    # Сглобяваме графа (виж src/graph.py за архитектурата)
    graph = build_graph()

    step_no = 0    # пореден номер на стъпка в главния граф (за четимост)
    route = []     # решенията на супервайзора - за финалното обобщение
    tool_calls_count = 0

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
                if node_name == "supervisor":
                    nxt = update["next"]
                    route.append(nxt)
                    print(f"\n{THIN}")
                    print(f"СТЪПКА {step_no} | SUPERVISOR решава: -> {nxt.upper()}")
                    print(f"  Причина: {update['reason']}")
                    if nxt != "FINISH":
                        print(f"  Управлението преминава към [{nxt.upper()}]:")
                elif update.get("messages"):
                    print(f"\nСТЪПКА {step_no} | [{node_name.upper()}] финален отговор:")
                    print(indent(extract_text(update["messages"][-1].content), "  | "))
        else:
            # --- Събитие ОТВЪТРЕ в агент (неговият ReAct цикъл) ----------
            # namespace[0] е например "analyst:<uuid>" - взимаме името.
            worker = namespace[0].split(":")[0]
            for update in step.values():
                for msg in (update or {}).get("messages", []):
                    if isinstance(msg, AIMessage) and msg.tool_calls:
                        for tc in msg.tool_calls:
                            tool_calls_count += 1
                            print(f"    [{worker}] вика инструмент: {tc['name']}({tc['args']})")
                    elif isinstance(msg, ToolMessage):
                        print(f"    [{worker}] <- {msg.name}: {shorten(extract_text(msg.content))}")

    # --- Финално обобщение на процеса ------------------------------------
    print(f"\n{LINE}")
    print("ОБОБЩЕНИЕ НА ПРОЦЕСА")
    print(LINE)
    print("Маршрут: START -> " + " -> ".join(route))
    print(f"Стъпки в главния граф: {step_no} | Извиквания на инструменти: {tool_calls_count}")
    print("Консумирани токъни и цена:")
    if usage_cb.usage_metadata:
        total_cost = 0.0
        for model, u in usage_cb.usage_metadata.items():
            cost = estimate_cost_usd(model, u)
            if cost is None:
                cost_label = "$0.00 (локален модел)"
            else:
                total_cost += cost
                cost_label = f"~${cost:.4f}"
            print(
                f"  {model}: вход {u.get('input_tokens', 0):,} | "
                f"изход {u.get('output_tokens', 0):,} | "
                f"общо {u.get('total_tokens', 0):,} | {cost_label}"
            )
        if total_cost > 0:
            print(f"  Обща цена: ~${total_cost:.4f}")
    else:
        print("  (моделът не върна данни за токъни)")
    print("ГОТОВО - задачата премина през целия SDLC процес.")
    print(LINE)


if __name__ == "__main__":
    main()
