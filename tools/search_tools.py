"""
新闻搜索工具。
搜索策略优先级：
1. Tavily API（通用搜索，推荐）
2. Bing Web Search API（通用搜索，需要 API Key）
3. RSS 订阅源（内置是 AI / 科技媒体，可用 .env 的 RSS_SOURCES 追加或替换）
4. 模拟新闻库（兜底，保证功能不中断）

时效约束（本次新增的核心能力）：
所有来源的新闻都必须带上「真实的发布时间」，并统一按时间窗口过滤，
只把窗口内的新闻交给模型。任何来源拿不到日期时，不再用当前时间顶替，
而是标记 date_verified=false，由上层决定是否采用。

来源范围（默认就是 AI / 科技为主）：
内置 RSS 源清一色是 AI 垂直媒体，这是有意为之，不是缺陷。
想补充别的来源时，把对应 RSS 加进 .env 的 RSS_SOURCES（例如再多加几家 AI 媒体）。
如果关注方向长期偏离 AI，可以打开 RSS_FILTER_BY_QUERY，
让 RSS 条目先命中查询词才进候选池 —— 默认关闭，因为在 AI 话题下
它会把约一半「没写全关键词但确实相关」的 AI 新闻误伤掉。
"""

import calendar
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import feedparser
import requests

from clock import LOCAL_TZ as _LOCAL_TZ
from clock import local_today

from llm import chat


# 低质量/视频平台域名黑名单：这些来源的搜索结果会被过滤掉
BLOCKED_DOMAINS = [
    "youtube.com",
    "youtu.be",
    "bilibili.com",
    "b23.tv",
    "douyin.com",
    "tiktok.com",
    "kuaishou.com",
    "ixigua.com",
    # 社交平台的帖子/转载页，标题党与碎片信息多，不作为新闻来源
    "facebook.com",
    "threads.net",
    "threads.com",
    "pinterest.com",
    "quora.com",
]

# 最近一次搜索中各来源的失败原因，用于排查「为什么没搜到新闻」。
# 各来源的异常以前被静默吞掉，出问题时只能看到"没有结果"，分不清是
# 未配置、被限流还是网络不通；这里把原因留下来，供日志与兜底提示使用。
LAST_ERRORS: List[str] = []


def _record_error(source: str, exc: BaseException) -> None:
    """记录来源失败原因（保留最近 10 条，避免无界增长）。"""
    LAST_ERRORS.append(f"{source}: {type(exc).__name__}: {exc}")
    del LAST_ERRORS[:-10]

# 相关性评分阈值：低于此分数的文章会被丢弃
RELEVANCE_THRESHOLD = int(os.getenv("RELEVANCE_THRESHOLD", "6"))

# 参与「关键词命中」判定的最短词长。
# 单字词（如「男」「女」）区分度太低，在中文正文里几乎必然命中，
# 拿它做过滤等于没过滤，反而会放行大量无关条目，所以默认不参与判定。
MIN_TERM_LEN = int(os.getenv("MIN_TERM_LEN", "2"))

# 单次批量相关性打分最多送多少条候选给模型（控制 token 与耗时）
_LLM_SCORING_LIMIT = int(os.getenv("LLM_SCORING_LIMIT", "12"))

# 搜索查询增强后缀：自动在查询词后追加，帮助搜到新闻而非百科
NEWS_QUERY_SUFFIX = os.getenv("NEWS_QUERY_SUFFIX", "新闻 最新")

# ---------------------------------------------------------------------------
# 时效配置：决定"什么算今天的新闻"
# ---------------------------------------------------------------------------
# 只保留最近 N 天内发布的新闻：1 = 只看今天；2 = 今天+昨天；7 = 近一周；0 = 不做时间过滤
NEWS_MAX_AGE_DAYS = int(os.getenv("NEWS_MAX_AGE_DAYS", "1"))

# 发布时间无法确认的条目如何处理：
#   false（默认）= 保留，但标 date_verified=false，供模型判断与标注
#   true        = 直接丢弃，只保留能确认发布时间的新闻（最严格的"只看今天"）
NEWS_STRICT_DATE = os.getenv("NEWS_STRICT_DATE", "false").strip().lower() in ("1", "true", "yes")

# 时区基准统一由 clock.LOCAL_TZ 提供，本模块不再自己解析 TIMEZONE。
# 以前这里另存一份 NEWS_TIMEZONE，导致「今天」的判定散成多套基准（详见 clock.py）。
# Tavily news 主题要求 days 至少为 1
_TAVILY_MIN_DAYS = 1

# 日期解析支持的非标准格式（很多中文源会输出这些）
_DATE_PATTERNS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日", "%Y%m%d")


# Tavily API 配置（从环境变量读取）
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
TAVILY_ENDPOINT = os.getenv(
    "TAVILY_ENDPOINT",
    "https://api.tavily.com/search",
)

# Bing API 配置（从环境变量读取）
BING_SEARCH_KEY = os.getenv("BING_SEARCH_KEY", "")
BING_SEARCH_ENDPOINT = os.getenv(
    "BING_SEARCH_ENDPOINT",
    "https://api.bing.microsoft.com/v7.0/news/search",
)

# 内置 RSS 源：AI / 科技方向，作为 Tavily 之外的第二条通道。
# 这些源都实测过：能正常返回条目，且每条都带真实发布时间。
DEFAULT_RSS_SOURCES = [
    {"name": "量子位", "url": "https://www.qbitai.com/feed"},
    {"name": "InfoQ", "url": "https://www.infoq.cn/feed"},
    {"name": "雷峰网", "url": "https://www.leiphone.com/feed"},
    {"name": "钛媒体", "url": "https://www.tmtpost.com/rss.xml"},
]


def _parse_rss_sources(raw: str) -> List[Dict[str, str]]:
    """
    解析 RSS_SOURCES 配置，格式 `名称|地址`，多个用英文逗号分隔。

    容错优先：名称缺省时用域名兜底，地址不是 http(s) 的条目直接跳过。
    一个写错的配置项只该让那个源失效，不该把整条搜索链路带崩。
    """
    sources: List[Dict[str, str]] = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "|" in chunk:
            name, url = chunk.split("|", 1)
        else:
            name, url = "", chunk
        name, url = name.strip(), url.strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        if not name:
            name = urlparse(url).netloc or "自定义源"
        sources.append({"name": name, "url": url})
    return sources


# 自定义 RSS 源（.env 的 RSS_SOURCES）。默认「追加」在内置的 4 个 AI 源之后，
# 用来补充更多来源。格式 `名称|地址`，多个用英文逗号分隔。示例（均已实测可用）：
#   RSS_SOURCES=IT之家|https://www.ithome.com/rss/,中新网|http://www.chinanews.com.cn/rss/scroll-news.xml
# 想把内置的 AI 源整个换掉（例如改做财经方向），把 RSS_INCLUDE_DEFAULTS 设为 false。
RSS_SOURCES_EXTRA = _parse_rss_sources(os.getenv("RSS_SOURCES", ""))

# 是否要求 RSS 条目先命中查询词才能进候选池。**默认关闭**。
# 打开能挡掉与关注方向无关的条目，代价是 AI 话题下会误伤约一半
# 「意思相关但标题没写全关键词」的新闻（实测：70 条 AI 新闻里会被砍到 36 条），
# 所以只在关注方向明显远离 AI 时才建议打开。
RSS_FILTER_BY_QUERY = os.getenv("RSS_FILTER_BY_QUERY", "false").strip().lower() in (
    "1",
    "true",
    "yes",
)

RSS_INCLUDE_DEFAULTS = os.getenv("RSS_INCLUDE_DEFAULTS", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)

RSS_SOURCES = (DEFAULT_RSS_SOURCES if RSS_INCLUDE_DEFAULTS else []) + RSS_SOURCES_EXTRA
if not RSS_SOURCES:
    # 自定义源写了但全被解析丢掉（例如格式写错），此时回退到内置源。
    # 宁可多抓几个不相关的源，也不要让 RSS 这条通道静默消失。
    RSS_SOURCES = list(DEFAULT_RSS_SOURCES)

# 模拟新闻库：所有真实源都失败时的兜底数据。
# 注意：这里的内容是编造的示例，不是真实新闻。每条都带 is_mock 标记，
# search_news 会把它一路带到模型上下文，让模型知道「这是兜底示例，不能当新闻发」，
# 而不是像以前那样伪装成当天真实新闻直接发到用户邮箱。
_MOCK_MARKER = "⚠️ 兜底示例数据，非真实新闻，不得作为新闻呈现或推送"
_MOCK_NEWS_DB = [
    {
        "title": "（示例）大模型多模态能力升级",
        "source": "示例数据源",
        "summary": "这是一条用于保证流程不中断的占位示例，不代表任何真实事件。",
        "url": "https://example.com/news/1",
        "tags": ["OpenAI", "大模型", "多模态"],
        "is_mock": True,
        "note": _MOCK_MARKER,
    },
    {
        "title": "（示例）代码生成模型基准表现提升",
        "source": "示例数据源",
        "summary": "这是一条用于保证流程不中断的占位示例，不代表任何真实事件。",
        "url": "https://example.com/news/2",
        "tags": ["Google", "Gemini", "代码生成"],
        "is_mock": True,
        "note": _MOCK_MARKER,
    },
    {
        "title": "（示例）开源模型推理成本下降",
        "source": "示例数据源",
        "summary": "这是一条用于保证流程不中断的占位示例，不代表任何真实事件。",
        "url": "https://example.com/news/3",
        "tags": ["DeepSeek", "开源", "大模型"],
        "is_mock": True,
        "note": _MOCK_MARKER,
    },
]


def _clean_html(raw: str) -> str:
    """简单去除 HTML 标签，把连续空白压缩成单个空格。"""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", "", raw)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _truncate(text: str, max_len: int = 200) -> str:
    """截断长文本并在末尾加省略号。"""
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0] + "..."


def _is_blocked_url(url: str) -> bool:
    """判断 URL 是否来自黑名单域名。"""
    url_lower = url.lower()
    for domain in BLOCKED_DOMAINS:
        if domain in url_lower:
            return True
    return False


# ---------------------------------------------------------------------------
# 发布时间解析与时窗过滤
# ---------------------------------------------------------------------------
# 「今天」与目标时区统一由 clock 提供：local_today 直接来自 clock，
# _LOCAL_TZ 就是 clock.LOCAL_TZ，本模块不再自持一份时区解析。


def window_start() -> Optional[date]:
    """
    时间窗口的起始日期（含）。返回 None 表示不做时间过滤。

    NEWS_MAX_AGE_DAYS=1 时起始日就是今天，即「只看今天」。
    """
    if NEWS_MAX_AGE_DAYS <= 0:
        return None
    return local_today() - timedelta(days=NEWS_MAX_AGE_DAYS - 1)


def parse_published(raw: Any) -> Optional[str]:
    """
    把各种来源的发布时间统一解析成「本地时区」的日期字符串（YYYY-MM-DD）。

    支持的格式：
    - RFC 2822：`Thu, 17 Sep 2026 16:15:27 GMT`（Tavily 与多数 RSS 用这个）
    - ISO 8601：`2026-09-17T16:15:27Z` / `2026-09-17 16:15:27+08:00`
    - 纯日期：`2026-09-17` / `2026/09/17` / `2026年09月17日`
    - feedparser 的 struct_time

    解析不出来时返回 None —— 绝不返回当天日期来"补"一个不存在的发布时间。
    """
    if raw is None:
        return None

    # feedparser 解析好的 struct_time（UTC）
    if isinstance(raw, (tuple, list)) and len(raw) >= 6:
        try:
            ts = calendar.timegm(tuple(raw[:9]) + (0,) * max(0, 9 - len(raw[:9])))
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_LOCAL_TZ).strftime("%Y-%m-%d")
        except Exception:
            return None

    if isinstance(raw, datetime):
        dt = raw if raw.tzinfo else raw.replace(tzinfo=_LOCAL_TZ)
        return dt.astimezone(_LOCAL_TZ).strftime("%Y-%m-%d")

    text = str(raw).strip()
    if not text:
        return None

    # 1) RFC 2822（Tavily published_date、RSS pubDate）
    try:
        dt = parsedate_to_datetime(text)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(_LOCAL_TZ).strftime("%Y-%m-%d")
    except Exception:
        pass

    # 2) ISO 8601
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_LOCAL_TZ)
        return dt.astimezone(_LOCAL_TZ).strftime("%Y-%m-%d")
    except Exception:
        pass

    # 3) 纯日期格式
    head = text[:10]
    for pattern in _DATE_PATTERNS:
        for probe in (text, head):
            try:
                return datetime.strptime(probe, pattern).strftime("%Y-%m-%d")
            except Exception:
                continue

    return None


def filter_by_date(
    articles: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    按时间窗口过滤新闻，并给每条打上 date_verified 标记。

    返回 (保留的新闻列表, 统计信息)。统计信息用于日志与排查：
    dropped_old = 明确超期被丢弃的数量，undated = 发布时间无法确认的数量。
    """
    start = window_start()
    stats = {"dropped_old": 0, "undated": 0, "kept": 0}

    if start is None:
        for article in articles:
            article["date_verified"] = bool(article.get("date"))
        stats["kept"] = len(articles)
        return list(articles), stats

    kept: List[Dict[str, Any]] = []
    for article in articles:
        published = article.get("date")
        verified = bool(published)
        if verified:
            try:
                published_day = datetime.strptime(published, "%Y-%m-%d").date()
            except Exception:
                verified = False

        if not verified:
            # 发布时间无法确认
            stats["undated"] += 1
            if NEWS_STRICT_DATE:
                continue
            article["date_verified"] = False
            kept.append(article)
            continue

        if published_day >= start:
            article["date_verified"] = True
            kept.append(article)
        else:
            stats["dropped_old"] += 1

    stats["kept"] = len(kept)
    return kept, stats


def _effective_terms(query_terms: List[str]) -> List[str]:
    """
    过滤掉单字这类没有区分度的查询词。

    全被过滤掉时（用户只填了「男」「女」这种词）退回原列表 ——
    宁可放宽，也不要因为一个词都不可用而把过滤逻辑变成「全部丢弃」。
    """
    terms = [term for term in query_terms if len(term) >= MIN_TERM_LEN]
    return terms or list(query_terms)


def _matches_query(article: Dict[str, Any], terms: List[str]) -> bool:
    """标题或摘要里是否出现任一查询词。"""
    text = f"{article.get('title', '')} {article.get('summary', '')}".lower()
    return any(term in text for term in terms)


def _score_by_query(articles: List[Dict[str, Any]], query_terms: List[str]) -> None:
    """按查询关键词给文章打关键词分（就地写入 keyword_score 字段）。

    只统计有效查询词的命中数：单字词在中文正文里几乎必然命中，
    计进来只会让小作文式的噪声条目拿到虚高的分。
    """
    terms = _effective_terms(query_terms)
    for article in articles:
        text = f"{article.get('title', '')} {article.get('summary', '')}".lower()
        article["keyword_score"] = sum(1 for term in terms if term in text)


def _sort_articles(articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    最新发布的排在前面，同一天的按相关性/关键词得分降序。

    date 是 YYYY-MM-DD 字符串，字典序等于时间序；date 为空的排到最后。
    """
    return sorted(
        articles,
        key=lambda a: (
            a.get("date") or "",
            a.get("relevance_score", a.get("keyword_score", 0)),
        ),
        reverse=True,
    )


def _preselect_pool(articles: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """
    挑出送进 LLM 打分的候选（最多 limit 条）。

    这里的关键是**不要让来源顺序参与决胜**。以前的写法是先按
    (date, keyword_score) 排完直接截断，同日期同分数的条目谁在前完全取决于
    候选池的拼接顺序（Tavily -> Bing -> RSS），于是 RSS 的中文新闻被系统性地
    挤到 12 条之外，从未被模型评估过就丢了 —— 而 RSS 恰恰是唯一免费、
    且能覆盖中文垂类内容的通道。

    现在的规则：命中查询词的先入选，不够再用最近发布的补齐。
    被截断的永远是「既没命中关键词、又不算最新」的那些，这个取舍是说得清的。
    """
    by_time = _sort_articles(articles)
    if limit <= 0 or len(by_time) <= limit:
        return by_time

    hit = [a for a in by_time if a.get("keyword_score", 0) > 0]
    rest = [a for a in by_time if a.get("keyword_score", 0) <= 0]
    return (hit + rest)[:limit]


def _finalize(
    candidates: List[Dict[str, Any]],
    query_terms: List[str],
    max_results: int,
    query: str = "",
) -> List[Dict[str, Any]]:
    """
    统一收口：时间过滤 -> 相关性排序 -> 按需截断。

    **max_results 是上限，不是要凑满的目标条数**：达标几条就给几条，
    绝不为了把数量填满而塞进不相关的新闻（那会让"最多 N 条"变成"必须 N 条"）。

    具体规则：

    1. 先挑出送进打分的候选（见 _preselect_pool，最多 _LLM_SCORING_LIMIT 条），
       再用 LLM 给它们统一打相关性分。
       **所有来源共用同一把尺子**，这样 RSS 的中文新闻才有机会和 Tavily 的结果公平竞争。
    2. 分数达标（>= RELEVANCE_THRESHOLD）的按「发布时间倒序 + 相关性」返回，最多 max_results 条。
       达标条目不足 max_results 时就返回这几条 —— 当天贴合关注方向的新闻本来有多少就是多少。
    3. 只有当**一条都不达标**时，才退化为给分数最高的几条：宁可给出"相关性一般"的当日新闻，
       也不要谎报"今天没有新闻"（那样用户会以为当天真的什么都没有）。
    """
    kept, _stats = filter_by_date(candidates)
    if not kept:
        return []

    _score_by_query(kept, query_terms)
    ordered = _sort_articles(kept)

    if query:
        # 预筛选必须放在打分之前：打分池有上限，谁进谁出直接决定模型能看到什么
        pool = _preselect_pool(kept, _LLM_SCORING_LIMIT)
        scores = _llm_score_articles(query, pool)
        for article, score in zip(pool, scores):
            article["relevance_score"] = score

        strong = [a for a in pool if a.get("relevance_score", 0) >= RELEVANCE_THRESHOLD]
        if strong:
            # 达标几条给几条，不用低分条目补满
            return _sort_articles(strong)[:max_results]

        # 一条都不达标时的退化路径（仍然不超过上限）
        return _sort_articles(pool)[:max_results]

    # 没给查询词（不会发生在正常调用链上）时退化为关键词匹配
    matched = [a for a in ordered if a.get("keyword_score", 0) > 0]
    if matched:
        return _sort_articles(matched)[:max_results]
    return ordered[:max_results]


def _llm_score_articles(query: str, articles: List[Dict[str, Any]]) -> List[int]:
    """
    一次性给多篇文章打相关性分数，返回与 articles 等长的 0-10 分数列表。

    以前是「每篇文章单独调一次模型」，一次简报最多要额外 10 次调用，
    现在合并成 1 次批量调用，延迟和 token 成本都大幅下降。
    解析失败时返回全 5（等价于“不筛选”），宁可保留也不误删。
    """
    if not articles:
        return []

    numbered = []
    for idx, article in enumerate(articles, start=1):
        title = article.get("title", "")
        summary = article.get("summary", "")
        numbered.append(f'{idx}. 标题：{title}\n   摘要：{summary}')

    prompt = (
        "请判断下列新闻与用户查询的相关程度，逐条给出 0-10 的整数分数。\n\n"
        f"用户查询：{query}\n\n"
        + "\n".join(numbered)
        + "\n\n评分标准：10 非常相关；5 部分相关；0 完全不相关。\n"
        "只返回一个 JSON 数组，长度与条目数一致，元素为整数，不要任何解释。"
        "例如：[8, 3, 10]"
    )

    try:
        result = chat([{"role": "user", "content": prompt}], temperature=0.1)
        if result.get("error"):
            return [5] * len(articles)

        content = result.get("content") or ""
        match = re.search(r"\[[^\]]*\]", content)
        if not match:
            return [5] * len(articles)

        scores = json.loads(match.group(0))
        if not isinstance(scores, list) or len(scores) != len(articles):
            return [5] * len(articles)
        return [min(max(int(score), 0), 10) for score in scores]
    except Exception:
        return [5] * len(articles)


def _search_tavily(query: str, max_results: int = 5) -> Tuple[List[Dict[str, Any]], bool]:
    """
    使用 Tavily API 搜索新闻。

    返回 (新闻列表, 数据源是否可用)。

    关键点：必须用 topic="news"。Tavily 的 general 主题**不返回发布日期**，
    结果里混着年鉴、月报、百科等旧内容，而且没有日期就无法判断时效；
    只有 news 主题才提供 published_date，并支持用 days 限定回溯天数。

    若未配置 TAVILY_API_KEY，直接返回 ([], False)，让外层切换到 Bing/RSS。
    """
    if not TAVILY_API_KEY:
        return [], False

    # 增强查询词，引导搜索引擎优先返回新闻而非百科
    enhanced_query = query.strip()
    if NEWS_QUERY_SUFFIX and NEWS_QUERY_SUFFIX not in enhanced_query:
        enhanced_query = f"{enhanced_query} {NEWS_QUERY_SUFFIX}"

    days = NEWS_MAX_AGE_DAYS if NEWS_MAX_AGE_DAYS > 0 else 7
    days = max(days, _TAVILY_MIN_DAYS)

    payload = {
        "api_key": TAVILY_API_KEY,
        "query": enhanced_query,
        "search_depth": "advanced",      # advanced 模式更深入地搜索新闻页面
        "topic": "news",                 # 只有 news 主题才带 published_date
        "days": days,                    # 限定回溯天数，从源头挡掉旧闻
        "max_results": max(max_results * 3, 10),  # 多取一些，给时间过滤留空间
        "include_answer": False,
        "include_images": False,
        "include_raw_content": False,
        "exclude_domains": BLOCKED_DOMAINS,  # 从源头排除视频平台
    }

    try:
        resp = requests.post(TAVILY_ENDPOINT, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        # 第一步：解析并做黑名单兜底过滤，同时取真实发布日期
        articles = []
        for item in data.get("results", []):
            title = item.get("title", "").strip()
            url = item.get("url", "").strip()
            summary = _truncate(_clean_html(item.get("content", "")))
            source = item.get("source", "Tavily")

            if not title or not url:
                continue
            if _is_blocked_url(url):
                continue

            articles.append({
                "title": title,
                "source": source,
                "summary": summary or title,
                "url": url,
                "tags": [],
                # 解析真实发布时间；拿不到就是 None，不再用当前时间顶替
                "date": parse_published(item.get("published_date")),
            })

        if not articles:
            return [], True  # 源是通的，只是这次没有结果

        # 只保留时间窗口内的候选，随后统一交给 search_news 做相关性打分。
        # 这里不再自己打分，否则 Tavily 候选会带着 0-10 的 LLM 分数
        # 去和 RSS 候选的 0-3 关键词分比较，量纲不一致，
        # 结果就是优质的 RSS 中文新闻被无差别挤掉。
        windowed, _stats = filter_by_date(articles)
        if not windowed:
            return [], True

        return windowed[: max(max_results * 2, 10)], True
    except Exception as exc:
        # Tavily 失败时返回空列表，让外层切换
        _record_error("Tavily", exc)
        return [], False


def _search_bing(query: str, max_results: int = 5) -> Tuple[List[Dict[str, Any]], bool]:
    """
    使用 Bing News Search API 搜索新闻。

    返回 (新闻列表, 数据源是否可用)。
    通过 freshness 参数限定时间范围、sortBy=Date 让结果按发布时间倒序，
    避免拿回一堆陈年旧闻。

    若未配置 BING_SEARCH_KEY，直接返回 ([], False)，让外层切换到 RSS。
    """
    if not BING_SEARCH_KEY:
        return [], False

    headers = {"Ocp-Apim-Subscription-Key": BING_SEARCH_KEY}
    params = {
        "q": query,
        "count": max(max_results * 2, 10),
        "mkt": "zh-CN",
        "setLang": "zh",
        "sortBy": "Date",           # 按发布时间倒序
    }
    # freshness 只接受 Day / Week / Month
    if NEWS_MAX_AGE_DAYS <= 0:
        pass
    elif NEWS_MAX_AGE_DAYS <= 1:
        params["freshness"] = "Day"
    elif NEWS_MAX_AGE_DAYS <= 7:
        params["freshness"] = "Week"
    else:
        params["freshness"] = "Month"

    try:
        resp = requests.get(BING_SEARCH_ENDPOINT, headers=headers, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        articles = []
        for item in data.get("value", []):
            title = item.get("name", "").strip()
            url = item.get("url", "").strip()
            summary = _truncate(_clean_html(item.get("description", "")))
            source = item.get("provider", [{}])[0].get("name", "Bing")

            if not title or not url:
                continue

            articles.append({
                "title": title,
                "source": source,
                "summary": summary or title,
                "url": url,
                "tags": [],
                "date": parse_published(item.get("datePublished")),
            })
        return articles, True
    except Exception as exc:
        _record_error("Bing", exc)
        return [], False


def _fetch_rss(source: Dict[str, str], timeout: int = 10) -> Tuple[List[Dict[str, Any]], bool]:
    """
    抓取单个 RSS 源，返回 (标准化的新闻条目列表, 源是否可用)。

    这里先用 requests 带超时下载，再交给 feedparser 解析：
    feedparser.parse(url) 自身不接受超时参数，源站不响应时会一直挂住，
    定时任务和 HTTP 请求都可能被拖死。

    发布时间优先用 feedparser 解析好的 published_parsed（UTC struct_time），
    再换算成本地时区日期；拿不到就留空，由上层标记为 date_verified=false。
    """
    articles: List[Dict[str, Any]] = []
    try:
        resp = requests.get(
            source["url"],
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ai-news-agent/1.0)"},
        )
        if resp.status_code != 200:
            return [], False
        feed = feedparser.parse(resp.content)
        if feed.get("bozo") and not feed.get("entries"):
            return [], False

        for entry in feed.entries[:20]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()

            summary_raw = entry.get("summary", "") or entry.get("description", "")
            summary = _truncate(_clean_html(summary_raw))

            # 优先用结构化时间；退回原始字符串；都没有就是 None
            published = (
                entry.get("published_parsed")
                or entry.get("updated_parsed")
                or entry.get("published")
                or entry.get("updated")
            )
            published_date = parse_published(published)

            if not title or not link:
                continue

            articles.append({
                "title": title,
                "source": source["name"],
                "summary": summary or title,
                "url": link,
                "tags": [],
                "date": published_date,
            })
    except Exception as exc:
        _record_error(f"RSS {source['name']}", exc)
        return [], False

    # 有 items 但一条都解析不出来，视为源不可用
    return articles, bool(articles)


def _mock_search(query_terms: List[str], max_results: int) -> List[Dict[str, Any]]:
    """兜底：从模拟新闻库中按关键词筛选（返回结果均带 is_mock 标记）。"""
    scored = []
    for article in _MOCK_NEWS_DB:
        text = f"{article['title']} {article['summary']} {' '.join(article['tags'])}".lower()
        score = sum(1 for term in query_terms if term in text)
        if score > 0:
            scored.append((score, article))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = [article.copy() for _, article in scored[:max_results]]
    if not results:
        results = [article.copy() for article in _MOCK_NEWS_DB[:max_results]]

    today = local_today().strftime("%Y-%m-%d")
    # 把各来源的失败原因一并带出去，方便用户/模型判断是配置问题还是网络问题
    reasons = "；".join(LAST_ERRORS[:3])
    note = _MOCK_MARKER + (f"（来源失败原因：{reasons}）" if reasons else "")
    for r in results:
        r["date"] = today
        r["date_verified"] = False   # 示例数据的日期没有验证意义
        r["is_mock"] = True          # 明确标记，避免被当成当天真实新闻
        r["note"] = note
    return results


def _search_rss(
    query_terms: List[str], max_results: int
) -> Tuple[List[Dict[str, Any]], bool]:
    """
    抓取全部 RSS 源并合并，返回 (新闻列表, 是否有源可达)。

    默认**不做查询词过滤**：内置源本来就是 AI 垂直媒体，抓回来的内容天然在用户的
    关注范围内，再要求标题命中查询词，只会把「意思相关但没写全关键词」的新闻误伤掉。

    RSS_FILTER_BY_QUERY=true 时才按查询词过滤，给关注方向明显远离 AI 的场景用 ——
    否则那几十条无关的 AI 新闻会把候选池占满，打分池一截断，模型看到的就全是 AI 内容。

    max_results 目前不影响 RSS 的抓取量：每个源各取最近 20 条，之后统一在
    _finalize 里按时间与相关性收敛。参数保留是为了和其它来源的签名保持一致。

    返回的 ok 只表示「源是否可达」，与过滤掉多少条无关 ——
    区分「源挂了」和「源好好的只是没有相关内容」，是外层决定要不要兜底的前提。
    """
    merged: List[Dict[str, Any]] = []
    any_ok = False
    terms = _effective_terms(query_terms) if RSS_FILTER_BY_QUERY else None

    for source in RSS_SOURCES:
        articles, ok = _fetch_rss(source)
        any_ok = any_ok or ok
        if terms is None:
            merged.extend(articles)
        else:
            merged.extend(a for a in articles if _matches_query(a, terms))

    return merged, any_ok


def search_news(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """
    根据查询词搜索新闻，**只返回时间窗口内发布的新闻**。

    时间窗口由环境变量 NEWS_MAX_AGE_DAYS 控制（默认 1 = 只看今天），
    窗口边界按 TIMEZONE（默认 Asia/Shanghai）计算，避免 UTC 时差把当天的新闻算成昨天。

    参数:
        query: 搜索关键词或主题，多个词用空格分隔
        max_results: **上限**条数，不是要凑满的目标。当天贴合关注方向的新闻不足时，
            返回条数会少于该值，这是正常现象（不会用不相关的新闻补足）。
    返回:
        匹配的新闻列表，长度 <= max_results，每条包含 title/source/summary/url/tags/date/date_verified。
        - date 是本地时区的发布日期（YYYY-MM-DD），拿不到真实日期时为 None
        - date_verified=false 表示发布时间无法从来源确认，不得当作"今日新闻"陈述
        - 返回空列表表示：真实新闻源可用，但窗口内确实没有符合条件的新闻
        - 条目带 is_mock=true 表示所有真实来源都不可用，这是兜底示例数据而非真实新闻
    """
    query_terms = [q.strip().lower() for q in query.split() if q.strip()]
    if not query_terms:
        query_terms = ["ai"]

    LAST_ERRORS.clear()

    # 依次向各真实源取候选（不再"第一个源有结果就用它"，
    # 而是合并多源后统一做时间过滤，这样才有足够素材筛出"今天"的新闻）
    candidates: List[Dict[str, Any]] = []
    any_source_ok = False

    tavily_articles, tavily_ok = _search_tavily(query, max_results)
    any_source_ok = any_source_ok or tavily_ok
    candidates.extend(tavily_articles)

    bing_articles, bing_ok = _search_bing(query, max_results)
    any_source_ok = any_source_ok or bing_ok
    candidates.extend(bing_articles)

    # 查询词一并交给 RSS：默认不参与过滤（内置源本身就是 AI 垂媒），
    # 只有 RSS_FILTER_BY_QUERY 打开时才用它筛条目
    rss_articles, rss_ok = _search_rss(query_terms, max_results)
    any_source_ok = any_source_ok or rss_ok
    candidates.extend(rss_articles)

    # 跨源去重（按 URL），保留先出现的（Tavily/Bing 优先于 RSS）
    deduped: List[Dict[str, Any]] = []
    seen_urls = set()
    for article in candidates:
        url = article.get("url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        deduped.append(article)

    if deduped:
        results = _finalize(deduped, query_terms, max_results, query=query)
        if results:
            return results

    # 走到这里说明：没有任何"窗口内"的新闻
    if any_source_ok:
        # 真实源是通的，只是今天确实没有符合条件的新闻 —— 如实返回空，
        # 绝不用过期新闻或编造内容来"凑数"
        return []

    # 所有真实源都不可用，才用示例数据兜底（带 is_mock 标记）
    return _mock_search(query_terms, max_results)
