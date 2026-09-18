"""
简报生成工具：把原始新闻和用户偏好交给大模型，生成一份结构化的每日简报。
"""

import json
from datetime import datetime
from typing import List, Dict, Any

from llm import chat


MOCK_BANNER = (
    "> ⚠️ **本次未获取到真实新闻**：以下内容来自兜底示例数据，"
    "仅用于验证流程是否跑通，**不是真实新闻，请勿转发或引用**。\n\n"
)


def _is_mock_news(news: List[Dict[str, Any]]) -> bool:
    """判断新闻素材是否全部/部分来自兜底示例数据。"""
    return any(bool(item.get("is_mock")) for item in news)


def generate_digest(news: List[Dict[str, Any]], preferences: Dict[str, Any]) -> str:
    """
    根据新闻列表和用户偏好生成 Markdown 格式简报。
    参数:
        news: search_news 返回的新闻列表
        preferences: 用户偏好（topics/keywords/max_articles/language）
    返回:
        Markdown 格式的简报文本。若素材是兜底示例数据，会在开头加醒目声明。
    """
    if not news:
        return "今日未找到相关新闻。"

    has_mock = _is_mock_news(news)

    topics = ", ".join(preferences.get("topics", []))
    keywords = ", ".join(preferences.get("keywords", []))
    language = preferences.get("language", "zh")
    max_articles = preferences.get("max_articles", 5)

    lang_hint = "中文" if language == "zh" else "English"

    system_prompt = (
        "你是一位专业的新闻编辑。请根据提供的新闻素材，"
        "生成一份简洁、有重点的每日新闻简报。"
        "只输出 Markdown 格式，不要多余解释。"
        + (
            "注意：本次素材被标记为 is_mock（兜底示例数据），"
            "它不是真实新闻，你必须在简报开头用醒目的引用块声明这一点，"
            "并且不得把它描述成今天发生的事实。"
            if has_mock
            else ""
        )
    )

    user_prompt = f"""
今天是：{datetime.now().strftime("%Y-%m-%d")}
用户关注话题：{topics}
用户关注关键词：{keywords}
期望语言：{lang_hint}
最多包含：{max_articles} 条（上限，不是目标）

新闻素材：
{json.dumps(news, ensure_ascii=False, indent=2)}

请生成简报，要求：
1. 开头用一句话概述今日最重要动态。
2. 每条新闻用 ### 标题，下面写 2-3 句摘要，并附原文链接。
3. 条数上限为 {max_articles} 条 —— 这是**上限而不是目标**：素材不足或不够相关时
   宁可少写几条（两条就是两条），也不要用不相关的新闻凑够数量。
4. 语言为{lang_hint}。
5. 如果新闻素材与用户关注话题不相关，请明确指出并只挑选最相关的内容。
6. 每条新闻都必须标注发布日期与来源（格式如「2026-09-18 · 量子位」）。
7. 素材里 date_verified 为 false 的条目，发布时间未经来源确认，必须注明「发布时间未确认」。
8. 这是「今日简报」，只采用发布日期为今天的素材；如果素材里没有今天的新闻，
   直接说明「今天未检索到符合条件的新闻」，不要用旧闻或编造内容填充。
"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    result = chat(messages, temperature=0.5)
    if "error" in result and result["error"]:
        # 如果模型调用失败，用模板兜底，保证功能不中断
        return _fallback_digest(news, preferences)

    digest = result.get("content") or _fallback_digest(news, preferences)
    if has_mock:
        # 不依赖模型自觉：无论它怎么写，兜底声明都由代码保证出现在简报开头
        digest = MOCK_BANNER + digest
    return digest


def _fallback_digest(news: List[Dict[str, Any]], preferences: Dict[str, Any]) -> str:
    """模型不可用时的兜底简报生成器。"""
    lines = []
    if _is_mock_news(news):
        lines.append(MOCK_BANNER.rstrip("\n"))
    lines.append("# 每日新闻简报\n")
    lines.append(f"## 关注话题：{', '.join(preferences.get('topics', []))}\n")
    for idx, item in enumerate(news[: preferences.get("max_articles", 5)], start=1):
        published = item.get("date")
        if published and item.get("date_verified") is False:
            date_text = f"{published}（未确认）"
        elif published:
            date_text = published
        else:
            date_text = "发布时间未确认"
        lines.append(f"### {idx}. {item['title']}")
        lines.append(f"- 来源：{item['source']} | 发布日期：{date_text}")
        lines.append(f"- 摘要：{item['summary']}")
        lines.append(f"- 链接：{item['url']}\n")
    return "\n".join(lines)
