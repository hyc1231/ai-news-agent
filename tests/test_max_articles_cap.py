"""
简报条数语义：max_results 是**上限**，不是必须凑满的目标条数。

回归背景：曾经的实现把「达标条目 + 低分条目」拼起来统一截断，
而候选池几乎每天都多于 max_results（RSS 多源合并后有十几条），
于是「最多 5 条」实际变成了「一定有 5 条」，简报里被塞进不相关的新闻。
"""

import pytest

import tools.search_tools as st


@pytest.fixture
def today_str():
    return st.local_today().strftime("%Y-%m-%d")


@pytest.fixture(autouse=True)
def _fixed_window(monkeypatch):
    monkeypatch.setattr(st, "NEWS_MAX_AGE_DAYS", 1)
    monkeypatch.setattr(st, "NEWS_STRICT_DATE", False)
    monkeypatch.setattr(st, "RELEVANCE_THRESHOLD", 6)


def _article(title, date):
    return {
        "title": title,
        "source": "test",
        "summary": title,
        "url": f"https://example.com/{title}",
        "tags": [],
        "date": date,
    }


def _stub_scores(monkeypatch, scores):
    monkeypatch.setattr(st, "_llm_score_articles", lambda q, pool: scores[: len(pool)])


def test_returns_only_qualified_articles(monkeypatch, today_str):
    """6 条候选里只有 2 条达标 -> 就返回 2 条，不能补满 5 条。"""
    _stub_scores(monkeypatch, [9, 8, 5, 4, 3, 2])
    candidates = [_article(f"n{i}", today_str) for i in range(6)]

    out = st._finalize(candidates, ["ai"], 5, query="ai")

    assert len(out) == 2
    assert all(a["relevance_score"] >= st.RELEVANCE_THRESHOLD for a in out)


def test_fewer_than_cap_all_qualified(monkeypatch, today_str):
    _stub_scores(monkeypatch, [7, 7, 7])
    candidates = [_article(f"n{i}", today_str) for i in range(3)]

    assert len(st._finalize(candidates, ["ai"], 5, query="ai")) == 3


def test_cap_still_truncates(monkeypatch, today_str):
    """达标条目多于上限时才截断。"""
    _stub_scores(monkeypatch, [8] * 12)
    candidates = [_article(f"n{i}", today_str) for i in range(12)]

    assert len(st._finalize(candidates, ["ai"], 5, query="ai")) == 5


def test_respects_smaller_cap(monkeypatch, today_str):
    _stub_scores(monkeypatch, [9] * 8)
    candidates = [_article(f"n{i}", today_str) for i in range(8)]

    assert len(st._finalize(candidates, ["ai"], 3, query="ai")) == 3


def test_all_below_threshold_still_returns_something(monkeypatch, today_str):
    """一条都不达标时退化为给分最高的几条：宁可给「相关性一般」的当日新闻，
    也不要谎报「今天没有新闻」。"""
    _stub_scores(monkeypatch, [2, 1])
    candidates = [_article(f"n{i}", today_str) for i in range(2)]

    out = st._finalize(candidates, ["ai"], 5, query="ai")

    assert len(out) == 2, "全不达标时不应返回空列表"


def test_old_news_never_used_to_fill_quota(monkeypatch, today_str):
    """窗口外的旧闻既不能进入结果，也不能用来补足条数。"""
    _stub_scores(monkeypatch, [9] * 4)
    candidates = [_article("今天", today_str)] + [
        _article(f"旧闻{i}", "2025-01-01") for i in range(3)
    ]

    out = st._finalize(candidates, ["ai"], 5, query="ai")

    assert len(out) == 1
    assert out[0]["title"] == "今天"


def test_returns_empty_when_nothing_in_window(monkeypatch):
    _stub_scores(monkeypatch, [9])
    only_old = [_article("旧闻", "2025-01-01")]

    assert st._finalize(only_old, ["ai"], 5, query="ai") == []


def test_keyword_path_used_when_no_query(monkeypatch, today_str):
    """没有查询词（非正常调用链）时退化为关键词匹配。"""
    candidates = [
        _article("大模型新进展", today_str),
        _article("某明星八卦", today_str),
    ]

    out = st._finalize(candidates, ["大模型"], 5)

    assert out[0]["title"] == "大模型新进展"
