"""
新闻搜索工具。
搜索策略优先级：
1. Tavily API（通用 AI 搜索，推荐）
2. Bing Web Search API（通用搜索，需要 API Key）
3. RSS 订阅源（中文科技媒体，无需 Key）
4. 模拟新闻库（兜底，保证功能不中断）
"""

import os
import re
from datetime import datetime
from typing import List, Dict, Any

import feedparser
import requests

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
]

# 相关性评分阈值：低于此分数的文章会被丢弃
RELEVANCE_THRESHOLD = int(os.getenv("RELEVANCE_THRESHOLD", "6"))

# 搜索查询增强后缀：自动在查询词后追加，帮助搜到新闻而非百科
NEWS_QUERY_SUFFIX = os.getenv("NEWS_QUERY_SUFFIX", "新闻 最新")


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

# 中文科技媒体 RSS 源列表（Tavily/Bing 未配置或失败时作为备用）
RSS_SOURCES = [
    {"name": "机器之心", "url": "https://www.jiqizhixin.com/rss"},
    {"name": "量子位", "url": "https://www.qbitai.com/feed"},
    {"name": "InfoQ", "url": "https://www.infoq.cn/feed"},
]

# 模拟新闻库：所有真实源都失败时的兜底数据
_MOCK_NEWS_DB = [
    {
        "title": "OpenAI 发布 GPT-5 预览版，多模态能力再升级",
        "source": "机器之心",
        "summary": "OpenAI 今日公布 GPT-5 预览版，支持更长的上下文窗口和原生图像理解。",
        "url": "https://example.com/news/1",
        "tags": ["OpenAI", "大模型", "多模态"],
    },
    {
        "title": "Google Gemini 2.0 实测：代码生成准确率提升 15%",
        "source": "InfoQ",
        "summary": "Gemini 2.0 在多项代码基准测试中超过前代，开发者工具链集成更紧密。",
        "url": "https://example.com/news/2",
        "tags": ["Google", "Gemini", "代码生成"],
    },
    {
        "title": "DeepSeek 开源新版 67B 模型，推理成本下降 30%",
        "source": "量子位",
        "summary": "DeepSeek 开源 67B 参数模型，在数学推理和代码任务上表现优异。",
        "url": "https://example.com/news/3",
        "tags": ["DeepSeek", "开源", "大模型"],
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


def _llm_judge_relevance(query: str, article: Dict[str, Any]) -> int:
    """
    用 LLM 判断文章与查询词的相关性，返回 0-10 的整数分数。
    失败时返回中间值 5，避免误删。
    """
    title = article.get("title", "")
    summary = article.get("summary", "")

    prompt = f"""请判断下面这篇新闻与用户查询的相关程度。

用户查询：{query}

新闻标题：{title}
新闻摘要：{summary}

请只返回一个 0-10 的整数分数：
- 10：非常相关，完全匹配用户查询主题
- 5：部分相关，提到了相关概念但不是核心内容
- 0：完全不相关

只返回数字，不要任何解释。"""

    try:
        result = chat([{"role": "user", "content": prompt}], temperature=0.1)
        if result.get("error"):
            return 5

        content = result.get("content", "5") or "5"
        match = re.search(r"\b(\d{1,2})\b", content)
        if match:
            score = int(match.group(1))
            return min(max(score, 0), 10)
    except Exception:
        pass
    return 5


def _search_tavily(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """
    使用 Tavily API 搜索网页。
    若未配置 TAVILY_API_KEY，直接返回空列表，让外层切换到 Bing/RSS。
    """
    if not TAVILY_API_KEY:
        return []

    # 增强查询词，引导搜索引擎优先返回新闻而非百科
    enhanced_query = query.strip()
    if NEWS_QUERY_SUFFIX and NEWS_QUERY_SUFFIX not in enhanced_query:
        enhanced_query = f"{enhanced_query} {NEWS_QUERY_SUFFIX}"

    payload = {
        "api_key": TAVILY_API_KEY,
        "query": enhanced_query,
        "search_depth": "advanced",      # advanced 模式更深入地搜索新闻页面
        "max_results": max_results * 3,  # 多取一些，给过滤留空间
        "include_answer": False,
        "include_images": False,
        "include_raw_content": False,
        "exclude_domains": BLOCKED_DOMAINS,  # 从源头排除视频平台
    }

    try:
        resp = requests.post(TAVILY_ENDPOINT, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        # 第一步：解析并做黑名单兜底过滤
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
                "date": datetime.now().strftime("%Y-%m-%d"),
            })

        if not articles:
            return []

        # 第二步：用 LLM 给前 10 条打分，保留相关性 >= 阈值的文章
        scored_articles = []
        for article in articles[:10]:
            score = _llm_judge_relevance(query, article)
            article["relevance_score"] = score
            if score >= RELEVANCE_THRESHOLD:
                scored_articles.append(article)

        # 如果筛选后不够，用分数最高的补齐
        if len(scored_articles) < max_results:
            sorted_by_score = sorted(articles, key=lambda x: x.get("relevance_score", 0), reverse=True)
            seen_urls = {a["url"] for a in scored_articles}
            for article in sorted_by_score:
                if article["url"] not in seen_urls:
                    scored_articles.append(article)
                if len(scored_articles) >= max_results:
                    break

        return scored_articles[:max_results]
    except Exception:
        # Tavily 失败时返回空列表，让外层切换
        return []


def _search_bing(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """
    使用 Bing Web Search API 搜索新闻。
    若未配置 BING_SEARCH_KEY，直接返回空列表，让外层切换到 RSS。
    """
    if not BING_SEARCH_KEY:
        return []

    headers = {"Ocp-Apim-Subscription-Key": BING_SEARCH_KEY}
    params = {
        "q": query,
        "count": max_results * 2,
        "mkt": "zh-CN",
        "setLang": "zh",
    }

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
            date = item.get("datePublished", "")[:10] or datetime.now().strftime("%Y-%m-%d")

            if not title or not url:
                continue

            articles.append({
                "title": title,
                "source": source,
                "summary": summary or title,
                "url": url,
                "tags": [],
                "date": date,
            })
        return articles
    except Exception:
        return []


def _fetch_rss(source: Dict[str, str], timeout: int = 10) -> List[Dict[str, Any]]:
    """抓取单个 RSS 源，返回标准化的新闻条目列表。"""
    articles = []
    try:
        feed = feedparser.parse(source["url"], request_headers={"User-Agent": "Mozilla/5.0"})
        if feed.get("bozo") and not feed.get("entries"):
            return articles

        for entry in feed.entries[:10]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary_raw = entry.get("summary", "") or entry.get("description", "")
            summary = _truncate(_clean_html(summary_raw))

            if not title or not link:
                continue

            articles.append({
                "title": title,
                "source": source["name"],
                "summary": summary or title,
                "url": link,
                "tags": [],
                "date": datetime.now().strftime("%Y-%m-%d"),
            })
    except Exception:
        pass
    return articles


def _filter_by_query(articles: List[Dict[str, Any]], query_terms: List[str], max_results: int) -> List[Dict[str, Any]]:
    """按查询关键词给文章打分、排序、截断。"""
    if not query_terms:
        return articles[:max_results]

    scored = []
    for article in articles:
        text = f"{article['title']} {article['summary']}".lower()
        score = sum(1 for term in query_terms if term in text)
        if score > 0:
            scored.append((score, article))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = [article for _, article in scored[:max_results]]

    if len(results) < max_results:
        seen_urls = {r["url"] for r in results}
        for article in articles:
            if article["url"] not in seen_urls:
                results.append(article)
            if len(results) >= max_results:
                break

    return results


def _mock_search(query_terms: List[str], max_results: int) -> List[Dict[str, Any]]:
    """兜底：从模拟新闻库中按关键词筛选。"""
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

    today = datetime.now().strftime("%Y-%m-%d")
    for r in results:
        r["date"] = today
    return results


def search_news(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """
    根据查询词搜索新闻。
    优先使用 Tavily，未配置或失败则依次回退 Bing、RSS、模拟数据。
    参数:
        query: 搜索关键词或主题，多个词用空格分隔
        max_results: 最多返回几条
    返回:
        匹配的新闻列表，每条包含 title/source/summary/url/tags/date
    """
    query_terms = [q.strip().lower() for q in query.split() if q.strip()]
    if not query_terms:
        query_terms = ["ai"]

    # 1. 尝试 Tavily API
    tavily_articles = _search_tavily(query, max_results)
    if tavily_articles:
        return _filter_by_query(tavily_articles, query_terms, max_results)

    # 2. 尝试 Bing API
    bing_articles = _search_bing(query, max_results)
    if bing_articles:
        return _filter_by_query(bing_articles, query_terms, max_results)

    # 3. 尝试 RSS 源
    all_articles = []
    for source in RSS_SOURCES:
        all_articles.extend(_fetch_rss(source))
    if all_articles:
        return _filter_by_query(all_articles, query_terms, max_results)

    # 4. 全部失败，回退模拟数据
    return _mock_search(query_terms, max_results)
