"""
简报语言设置是否真的生效。

回归背景：前端「简报语言」选了 English，但 run_agent 收尾固定调用
ensure_chinese()，英文占比远超阈值又被翻译回中文，配置形同虚设。
现在收尾校验改为 ensure_language(text, language)，方向跟随用户偏好。
"""

import pytest

import agent
import tools.digest_tools as digest_tools


@pytest.fixture
def news():
    return [
        {
            "title": "OpenAI releases a new model",
            "source": "test",
            "summary": "summary",
            "url": "https://example.com/1",
            "tags": [],
            "date": "2026-09-18",
            "date_verified": True,
        }
    ]


def _forbid_model(*_args, **_kwargs):
    pytest.fail("这条路径不应调用模型做翻译")


def test_zh_target_translates_english(monkeypatch):
    captured = {}

    def fake_chat(messages, **kwargs):
        captured["prompt"] = messages[0]["content"]
        return {"content": "中文结果"}

    monkeypatch.setattr(agent, "chat", fake_chat)

    out = agent.ensure_language("This is a long English paragraph about AI news.", "zh")

    assert out == "中文结果"
    assert "翻译成中文" in captured["prompt"]


def test_zh_target_keeps_chinese(monkeypatch):
    monkeypatch.setattr(agent, "chat", _forbid_model)
    text = "今日人工智能简报：某公司发布了大模型新版本，性能显著提升。"

    assert agent.ensure_language(text, "zh") == text


def test_en_target_translates_chinese(monkeypatch):
    captured = {}

    def fake_chat(messages, **kwargs):
        captured["prompt"] = messages[0]["content"]
        return {"content": "English result"}

    monkeypatch.setattr(agent, "chat", fake_chat)

    out = agent.ensure_language("今日人工智能简报：某公司发布了大模型新版本。", "en")

    assert out == "English result"
    assert "into English" in captured["prompt"]


def test_en_target_keeps_english(monkeypatch):
    monkeypatch.setattr(agent, "chat", _forbid_model)
    text = "Daily AI news: a new large language model was released today."

    assert agent.ensure_language(text, "en") == text


def test_digest_prompt_requires_english_when_configured(monkeypatch, news):
    captured = {}

    def fake_chat(messages, **kwargs):
        captured["messages"] = messages
        return {"content": "# digest"}

    monkeypatch.setattr(digest_tools, "chat", fake_chat)
    digest_tools.generate_digest(news, {"language": "en", "topics": [], "keywords": [], "max_articles": 5})

    prompt = captured["messages"][1]["content"]
    assert "期望语言：English" in prompt


def test_digest_prompt_requires_chinese_by_default(monkeypatch, news):
    captured = {}

    def fake_chat(messages, **kwargs):
        captured["messages"] = messages
        return {"content": "# digest"}

    monkeypatch.setattr(digest_tools, "chat", fake_chat)
    digest_tools.generate_digest(news, {"language": "zh", "topics": [], "keywords": [], "max_articles": 5})

    prompt = captured["messages"][1]["content"]
    assert "期望语言：中文" in prompt


def test_generate_and_send_digest_passes_language_through(monkeypatch):
    """偏好里是 en 时，任务描述和 run_agent 的 language 参数都要跟着变成英文。"""
    seen = {}

    monkeypatch.setattr(agent, "load_preferences", lambda: {"language": "en"})

    def fake_run_agent(task, max_iterations=10, trace_id="", language="zh"):
        seen["task"] = task
        seen["language"] = language
        return {"success": True, "answer": "ok", "trace": [], "trace_id": trace_id}

    monkeypatch.setattr(agent, "run_agent", fake_run_agent)

    result = agent.generate_and_send_digest()

    assert seen["language"] == "en"
    assert "用英文回报" in seen["task"]
    assert result["language"] == "en"


def test_generate_and_send_digest_defaults_to_chinese(monkeypatch):
    seen = {}

    monkeypatch.setattr(agent, "load_preferences", lambda: {"language": "zh"})

    def fake_run_agent(task, max_iterations=10, trace_id="", language="zh"):
        seen["task"] = task
        seen["language"] = language
        return {"success": True, "answer": "ok", "trace": []}

    monkeypatch.setattr(agent, "run_agent", fake_run_agent)

    agent.generate_and_send_digest()

    assert seen["language"] == "zh"
    assert "用中文回报" in seen["task"]


def test_preferences_error_falls_back_to_chinese(monkeypatch):
    """偏好读不出来时（数据库故障）回落中文，不能让整条链路失败。"""
    seen = {}
    monkeypatch.setattr(agent, "load_preferences", lambda: {"error": "db down"})

    def fake_run_agent(task, max_iterations=10, trace_id="", language="zh"):
        seen["language"] = language
        return {"success": True, "answer": "ok", "trace": []}

    monkeypatch.setattr(agent, "run_agent", fake_run_agent)

    agent.generate_and_send_digest()

    assert seen["language"] == "zh"
