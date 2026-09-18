"""
新闻时效：日期解析与时窗过滤。

这里覆盖的是「只推今天的新闻」能否被代码强制保证。两个最容易出错的点：

1. 时区：Tavily 和 RSS 给的都是 UTC。`09-17 16:15 GMT` 其实是北京时间 09-18，
   直接取日期字符串会把当天的新闻误杀成昨天。
2. 拿不到日期时绝不能补一个 `datetime.now()` —— 那会把旧文标成今天。
"""

import pytest

import tools.search_tools as st


def _article(title, date, url=None):
    return {
        "title": title,
        "source": "test",
        "summary": title,
        "url": url or f"https://example.com/{title}",
        "tags": [],
        "date": date,
    }


@pytest.fixture
def today_str():
    return st.local_today().strftime("%Y-%m-%d")


def test_rfc2822_utc_is_converted_to_local_date():
    """UTC 16:15 已是北京时间次日 00:15，必须算成 18 号而不是 17 号。"""
    assert st.parse_published("Thu, 17 Sep 2026 16:15:27 GMT") == "2026-09-18"


def test_iso8601_utc_is_converted_to_local_date():
    assert st.parse_published("2026-09-17T16:15:27Z") == "2026-09-18"


def test_struct_time_from_feedparser_is_converted():
    assert st.parse_published((2026, 9, 17, 16, 15, 27, 0, 0, 0)) == "2026-09-18"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-09-17", "2026-09-17"),
        ("2026/09/17", "2026-09-17"),
        ("2026年09月17日", "2026-09-17"),
    ],
)
def test_plain_date_formats(raw, expected):
    assert st.parse_published(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "昨天", "not-a-date", "刚刚"])
def test_unparseable_date_returns_none(raw):
    """解析不出来就返回 None，绝不返回当天日期来伪造时效。"""
    assert st.parse_published(raw) is None


def test_window_start_follows_max_age_days(monkeypatch):
    monkeypatch.setattr(st, "NEWS_MAX_AGE_DAYS", 1)
    assert st.window_start() == st.local_today()

    monkeypatch.setattr(st, "NEWS_MAX_AGE_DAYS", 2)
    assert (st.local_today() - st.window_start()).days == 1

    monkeypatch.setattr(st, "NEWS_MAX_AGE_DAYS", 0)
    assert st.window_start() is None


def test_filter_keeps_only_in_window(monkeypatch, today_str):
    monkeypatch.setattr(st, "NEWS_MAX_AGE_DAYS", 1)
    monkeypatch.setattr(st, "NEWS_STRICT_DATE", False)

    articles = [
        _article("今天", today_str),
        _article("去年的旧闻", "2025-01-01"),
    ]
    kept, stats = st.filter_by_date(articles)

    assert [a["title"] for a in kept] == ["今天"]
    assert stats["dropped_old"] == 1
    assert kept[0]["date_verified"] is True


def test_undated_articles_kept_but_not_verified(monkeypatch, today_str):
    monkeypatch.setattr(st, "NEWS_MAX_AGE_DAYS", 1)
    monkeypatch.setattr(st, "NEWS_STRICT_DATE", False)

    kept, stats = st.filter_by_date([_article("无日期", None)])

    assert len(kept) == 1
    assert kept[0]["date_verified"] is False
    assert stats["undated"] == 1


def test_strict_mode_drops_undated(monkeypatch, today_str):
    monkeypatch.setattr(st, "NEWS_MAX_AGE_DAYS", 1)
    monkeypatch.setattr(st, "NEWS_STRICT_DATE", True)

    kept, _stats = st.filter_by_date([_article("无日期", None)])
    assert kept == []


def test_max_age_days_zero_disables_filtering(monkeypatch):
    monkeypatch.setattr(st, "NEWS_MAX_AGE_DAYS", 0)

    kept, _stats = st.filter_by_date([_article("很旧的新闻", "2020-01-01")])
    assert len(kept) == 1


@pytest.mark.parametrize(
    "url,blocked",
    [
        ("https://www.youtube.com/watch?v=1", True),
        ("https://www.bilibili.com/video/1", True),
        ("https://www.facebook.com/post/1", True),
        ("https://www.36kr.com/p/1", False),
        ("https://www.qbitai.com/2026/09/1.html", False),
    ],
)
def test_blocked_domains(url, blocked):
    assert st._is_blocked_url(url) is blocked


def test_all_sources_down_falls_back_to_mock(monkeypatch, today_str):
    """所有真实源都不可用时才用示例数据兜底，且必须带 is_mock 标记。"""
    monkeypatch.setattr(st, "_search_tavily", lambda q, m: ([], False))
    monkeypatch.setattr(st, "_search_bing", lambda q, m: ([], False))
    monkeypatch.setattr(st, "_search_rss", lambda m: ([], False))

    out = st.search_news("大模型", max_results=5)

    assert out, "所有源都不可用时应返回兜底示例数据"
    assert all(item.get("is_mock") is True for item in out)
    assert all("非真实新闻" in item.get("note", "") for item in out)


def test_live_sources_with_no_news_return_empty(monkeypatch):
    """源是通的、只是今天没有新闻 -> 必须返回空列表，而不是拿旧闻或示例数据凑数。"""
    monkeypatch.setattr(st, "_search_tavily", lambda q, m: ([], True))
    monkeypatch.setattr(st, "_search_bing", lambda q, m: ([], True))
    monkeypatch.setattr(st, "_search_rss", lambda m: ([], True))

    assert st.search_news("大模型", max_results=5) == []


def test_old_news_from_live_source_is_filtered_out(monkeypatch):
    """源返回了结果但全是旧闻 -> 同样返回空，不能推送给用户。"""
    old = [_article("2025 年 AI 大事件回顾", "2025-01-01")]
    monkeypatch.setattr(st, "NEWS_MAX_AGE_DAYS", 1)
    monkeypatch.setattr(st, "_search_tavily", lambda q, m: (list(old), True))
    monkeypatch.setattr(st, "_search_bing", lambda q, m: ([], True))
    monkeypatch.setattr(st, "_search_rss", lambda m: ([], True))

    assert st.search_news("大模型", max_results=5) == []
