import pytest

pytest.importorskip("pydantic_ai")

from openvarak.agents.pydantic_desktop import free_experiment_tools
from openvarak.tools.calculator import CalculatorTool
from openvarak.tools.web_search import WebSearchTool


def test_free_tools_ignore_paid_provider_and_exclude_unknown_tools(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    calculator = CalculatorTool()
    search, retained = free_experiment_tools(
        [WebSearchTool(engine="tavily"), object(), calculator]
    )
    assert retained is calculator
    assert search.spec.metadata["engine"] == "duckduckgo"
    monkeypatch.setattr(
        search, "_duckduckgo_search", lambda query, limit: "free result"
    )
    assert search.execute(query="prototype demand").success
