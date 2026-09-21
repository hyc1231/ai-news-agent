"""
候选池的组成，以及「谁有资格被送进模型打分」。

回归背景：候选池是多来源拼接的（Tavily -> Bing -> RSS），而打分池有上限
（_LLM_SCORING_LIMIT，默认 12）。以前是「先按 (日期, 关键词命中数) 排完直接截断」，
同日期同分数时完全靠插入顺序决胜 —— 于是 RSS 的中文新闻被系统性地挤到 12 条之外，
从未被模型评估过就丢了。而 RSS 恰恰是唯一免费、且能覆盖中文垂类内容的通道。

另外两件事也在这里钉住：
- 单字关键词（如「男」「女」）区分度太低，不参与命中判定，避免噪声条目拿虚高分；
- RSS 是否按查询词过滤是**可配置**的，默认关闭（内置源本身就是 AI 垂媒，
  再按字面词筛会把「意思相关但没写全关键词」的新闻误伤掉）。
"""

import pytest

import tools.search_tools as st


def _article(title, date, source="test", url=None):
    return {
        "title": title,
        "source": source,
        "summary": title,
        "url": url or f"https://example.com/{title}",
        "tags": [],
        "date": date,
    }


@pytest.fixture
def today_str():
    return st.local_today().strftime("%Y-%m-%d")


# --- 有效查询词 -------------------------------------------------------------

def test_single_char_terms_are_dropped():
    assert st._effective_terms(["宠物", "结婚", "男", "女"]) == ["宠物", "结婚"]


def test_terms_fall_back_when_all_too_short():
    """全是单字时退回原列表 —— 不能让过滤逻辑变成「全部丢弃」。"""
    assert st._effective_terms(["男", "女"]) == ["男", "女"]


def test_keyword_score_ignores_single_char_hits():
    """「男」「女」在正文里几乎必然出现，计进来只会让噪声条目拿到虚高的分。"""
    article = _article("男女那点事", "2026-09-21")

    st._score_by_query([article], ["宠物", "结婚", "男", "女"])

    assert article["keyword_score"] == 0


def test_keyword_score_counts_effective_terms():
    article = _article("宠物行业观察", "2026-09-21")

    st._score_by_query([article], ["宠物", "结婚", "男"])

    assert article["keyword_score"] == 1


# --- 打分子集的挑选 ---------------------------------------------------------

def test_preselect_prefers_query_hits_over_insertion_order(today_str):
    """命中查询词的条目必须进打分池，哪怕它排在候选列表的最后。

    这正是修复前失败的那个场景：12 条同日期、同分数的条目先占满打分池，
    真正相关的那条排在最后，从来没被模型看到过。
    """
    pool = [_article(f"无关{i}", today_str) for i in range(12)]
    pool.append(_article("大模型新进展", today_str))
    st._score_by_query(pool, ["大模型"])

    picked = st._preselect_pool(pool, 3)

    assert "大模型新进展" in [a["title"] for a in picked]


def test_preselect_keeps_everything_when_under_limit(today_str):
    few = [_article("a", today_str), _article("b", today_str)]

    assert len(st._preselect_pool(few, 12)) == 2


def test_preselect_fills_with_newest_when_nothing_hits(today_str):
    """一条都没命中时按发布时间取最新的 —— 不是按来源顺序。"""
    articles = [_article("旧的", "2025-01-01"), _article("新的", today_str)]
    st._score_by_query(articles, ["宠物"])

    picked = st._preselect_pool(articles, 1)

    assert picked[0]["title"] == "新的"


def test_finalize_actually_sends_query_hits_to_scoring(monkeypatch, today_str):
    """端到端确认：命中查询词的 RSS 条目真的出现在送进模型的那批候选里。"""
    seen = {}

    def fake_score(query, pool):
        seen["titles"] = [a["title"] for a in pool]
        return [9 if a["title"] == "大模型新进展" else 1 for a in pool]

    monkeypatch.setattr(st, "_llm_score_articles", fake_score)
    monkeypatch.setattr(st, "_LLM_SCORING_LIMIT", 3)
    monkeypatch.setattr(st, "NEWS_MAX_AGE_DAYS", 1)

    candidates = [
        _article(f"Tavily 无关{i}", today_str, source="Tavily") for i in range(5)
    ]
    candidates.append(_article("大模型新进展", today_str, source="量子位"))

    out = st._finalize(candidates, ["大模型"], 5, query="大模型")

    assert "大模型新进展" in seen["titles"]
    assert [a["title"] for a in out] == ["大模型新进展"]


# --- RSS 源配置解析 ---------------------------------------------------------

def test_parse_rss_sources_uses_name_and_url():
    parsed = st._parse_rss_sources("量子位|https://www.qbitai.com/feed")

    assert parsed == [{"name": "量子位", "url": "https://www.qbitai.com/feed"}]


def test_parse_rss_sources_falls_back_to_hostname():
    parsed = st._parse_rss_sources("https://www.ithome.com/rss/")

    assert parsed[0]["name"] == "www.ithome.com"


def test_parse_rss_sources_skips_invalid_entries():
    """写错一条只该让那个源失效，不该把整条搜索链路带崩。"""
    parsed = st._parse_rss_sources("坏源|ftp://x,好的|https://ok.example/feed,,")

    assert parsed == [{"name": "好的", "url": "https://ok.example/feed"}]


def test_parse_rss_sources_handles_empty():
    assert st._parse_rss_sources("") == []


def test_default_rss_sources_are_intact():
    """默认配置不该被上面这些改动波及。"""
    names = [s["name"] for s in st.DEFAULT_RSS_SOURCES]

    assert names == ["量子位", "InfoQ", "雷峰网", "钛媒体"]


# --- RSS 查询词过滤（默认关闭） ---------------------------------------------

def _stub_rss(monkeypatch, articles):
    monkeypatch.setattr(
        st, "RSS_SOURCES", [{"name": "测试源", "url": "https://example.com/feed"}]
    )
    monkeypatch.setattr(
        st, "_fetch_rss", lambda source, timeout=10: ([dict(a) for a in articles], True)
    )


def test_rss_not_filtered_by_default(monkeypatch):
    """默认不过滤：内置源本身就是 AI 垂媒，再按字面词筛会误伤相关新闻。"""
    _stub_rss(monkeypatch, [_article("一篇讲大模型的稿子", "2026-09-21")])
    monkeypatch.setattr(st, "RSS_FILTER_BY_QUERY", False)

    articles, ok = st._search_rss(["宠物", "结婚"], 5)

    assert ok is True
    assert len(articles) == 1


def test_rss_filter_can_be_enabled(monkeypatch):
    """打开 RSS_FILTER_BY_QUERY 后，只有命中查询词的条目留下。"""
    _stub_rss(
        monkeypatch,
        [
            _article("一篇讲大模型的稿子", "2026-09-21"),
            _article("宠物智能项圈评测", "2026-09-21"),
        ],
    )
    monkeypatch.setattr(st, "RSS_FILTER_BY_QUERY", True)

    articles, _ok = st._search_rss(["宠物", "结婚"], 5)

    assert [a["title"] for a in articles] == ["宠物智能项圈评测"]


def test_rss_reachable_flag_survives_filtering(monkeypatch):
    """条目被过滤掉不等于源挂了 —— 外层靠这个区分「源失败」和「今天没相关内容」。"""
    _stub_rss(monkeypatch, [_article("一篇讲大模型的稿子", "2026-09-21")])
    monkeypatch.setattr(st, "RSS_FILTER_BY_QUERY", True)

    articles, ok = st._search_rss(["宠物"], 5)

    assert articles == []
    assert ok is True
