"""
Локална визуализация на графа - Mermaid диаграма, без външни услуги.

Стартиране:
    python draw_graph.py

Отпечатва структурата на мултиагентния граф (възли + ребра) като
Mermaid текст. Диаграмата се рендерира локално: постави я в markdown
файл (GitHub/VS Code я показват директно) или в https://mermaid.live
по избор - самото генериране е чисто локална операция, не праща нищо
никъде и не вика LLM.
"""

import os
import sys

# Диаграмата описва СТРУКТУРАТА на графа - LLM никога не се извиква.
# build_graph() обаче създава LLM клиентите (за да сглоби възлите),
# а Anthropic клиентът иска ключ при конструиране. Затова за целите
# на чертането подаваме фиктивен ключ, ако няма истински.
os.environ.setdefault("ANTHROPIC_API_KEY", "not-needed-for-drawing")

from src.graph import build_graph


def main() -> None:
    # Кирилицата в конзолата чупи redirect-нат изход на Windows (виж main.py)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print(build_graph().get_graph().draw_mermaid())


if __name__ == "__main__":
    main()
