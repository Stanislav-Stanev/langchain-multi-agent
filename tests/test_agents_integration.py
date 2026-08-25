"""
Интеграционни тестове за src/agents.py - ИСТИНСКИТЕ ReAct агенти.

Тук НЕ подменяме агента (както в E2E тестовете), а само LLM-а вътре в
него: ScriptedToolCallingModel връща предварително скриптирани
AIMessage-и (с tool_calls), а create_agent + LangGraph изпълняват
реалния agentic цикъл: модел -> инструмент -> резултат -> модел.

Така проверяваме, че:
  1. create_agent наистина ИЗПЪЛНЯВА нашите инструменти и подава
     резултата им обратно (ToolMessage);
  2. агентите са свързани с правилните инструменти;
  3. system prompt-ът се избира според текущия език.
"""

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from src import agents as agents_module


class ScriptedToolCallingModel(BaseChatModel):
    """
    Фалшив chat модел с поддръжка на tool calling.

    Връща съобщенията от script едно по едно - първите обикновено
    съдържат tool_calls (агентът "иска" инструмент), а последното е
    финалният текстов отговор. Записва всичко получено в received,
    за да проверим какво е стигнало до "модела".
    """

    script: list[AIMessage]
    received: list[Any] = []
    bound_tools: list[Any] = []

    def bind_tools(self, tools, **kwargs):
        self.bound_tools = list(tools)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.received.append(list(messages))
        message = self.script.pop(0)
        return ChatResult(generations=[ChatGeneration(message=message)])

    @property
    def _llm_type(self) -> str:
        return "scripted-tool-calling"


def tool_call(name: str, args: dict, call_id: str = "call_1") -> AIMessage:
    """AIMessage, с което моделът 'иска' извикване на инструмент."""
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def install_model(monkeypatch, script) -> ScriptedToolCallingModel:
    """Подменя get_llm В src.agents, така че create_* да получи дубльора.
    Приема role аргумента (model routing) - дубльорът е един за всички роли."""
    model = ScriptedToolCallingModel(script=list(script), received=[], bound_tools=[])
    monkeypatch.setattr(agents_module, "get_llm", lambda role="default": model)
    return model


class TestAnalystAgent:
    def test_fetches_ticket_and_returns_spec(self, monkeypatch):
        install_model(
            monkeypatch,
            [
                tool_call("get_ticket_details", {"ticket_id": "DEV-101"}),
                AIMessage(content="Спецификация: validate_email(s: str) -> bool."),
            ],
        )
        agent = agents_module.create_analyst()

        result = agent.invoke(
            {"messages": [HumanMessage(content="Имплементирай тикет DEV-101")]}
        )
        messages = result["messages"]

        # Инструментът е РЕАЛНО изпълнен: има ToolMessage с данните на тикета
        tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
        assert len(tool_messages) == 1
        assert "Функция за валидация на имейл адреси" in tool_messages[0].content

        # Финалният отговор на агента е последното съобщение
        assert messages[-1].content == "Спецификация: validate_email(s: str) -> bool."

    def test_analyst_has_only_the_ticket_tool(self, monkeypatch):
        model = install_model(monkeypatch, [AIMessage(content="без инструменти")])
        agent = agents_module.create_analyst()
        agent.invoke({"messages": [HumanMessage(content="здравей")]})

        assert [t.name for t in model.bound_tools] == ["get_ticket_details"]

    def test_system_prompt_language_is_fixed_at_creation(self, monkeypatch):
        monkeypatch.setenv("APP_LANG", "en")
        model = install_model(monkeypatch, [AIMessage(content="done")])
        agent = agents_module.create_analyst()
        agent.invoke({"messages": [HumanMessage(content="hi")]})

        # Първото съобщение, което моделът вижда, е английският system prompt
        system_text = str(model.received[0][0].content)
        assert "business Analyst" in system_text


class TestDeveloperAgent:
    def test_checks_standards_before_writing_code(self, monkeypatch):
        model = install_model(
            monkeypatch,
            [
                tool_call("get_coding_standards", {"topic": "python"}),
                AIMessage(content="```python\ndef f() -> int:\n    return 1\n```"),
            ],
        )
        agent = agents_module.create_developer()
        result = agent.invoke({"messages": [HumanMessage(content="напиши кода")]})

        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert "PEP 8" in tool_messages[0].content
        assert [t.name for t in model.bound_tools] == ["get_coding_standards"]


class TestQaAgent:
    def test_runs_both_checks_and_reports_status(self, monkeypatch):
        code = 'def f(x: int) -> int:\n    """d"""\n    if not x:\n        raise ValueError\n    return x'
        install_model(
            monkeypatch,
            [
                tool_call("check_code_syntax", {"code": code}, "call_1"),
                tool_call(
                    "run_test_checklist",
                    {"code": code, "requirements": "функция f"},
                    "call_2",
                ),
                AIMessage(content="Статус: APPROVED"),
            ],
        )
        agent = agents_module.create_qa()
        result = agent.invoke({"messages": [HumanMessage(content="провери кода")]})

        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(tool_messages) == 2
        # check_code_syntax: кодът е валиден
        assert any("OK" in m.content for m in tool_messages)
        # run_test_checklist: всички евристики минават
        assert any("Всички проверки преминаха успешно." in m.content for m in tool_messages)
        assert result["messages"][-1].content == "Статус: APPROVED"

    def test_qa_has_both_verification_tools(self, monkeypatch):
        model = install_model(monkeypatch, [AIMessage(content="край")])
        agent = agents_module.create_qa()
        agent.invoke({"messages": [HumanMessage(content="х")]})

        assert {t.name for t in model.bound_tools} == {
            "check_code_syntax",
            "run_test_checklist",
        }
