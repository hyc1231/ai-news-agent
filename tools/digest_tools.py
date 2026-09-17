"""
简报生成工具：把原始新闻和用户偏好交给大模型，生成一份结构化的每日简报。
"""

import json
from typing import List, Dict, Any

from llm import chat


def generate_digest(news: List[Dict[str, Any]], preferences: Dict[str, Any]) -> str:
    """
    根据新闻列表和用户偏好生成 Markdown 格式简报。
    参数:
        news: search_news 返回的新闻列表
        preferences: 用户偏好（topics/keywords/max_articles/language）
    返回:
        Markdown 格式的简报文本
    """
    if not news:
        return "今日未找到相关新闻。"

    topics = ", ".join(preferences.get("topics", []))
    keywords = ", ".join(preferences.get("keywords", []))
    language = preferences.get("language", "zh")
    max_articles = preferences.get("max_articles", 5)

    lang_hint = "中文" if language == "zh" else "English"

    system_prompt = (
        "你是一位专业的新闻编辑。请根据提供的新闻素材，"
        "生成一份简洁、有重点的每日新闻简报。"
        "只输出 Markdown 格式，不要多余解释。"
    )

    user_prompt = f"""
用户关注话题：{topics}
用户关注关键词：{keywords}
期望语言：{lang_hint}
最多包含：{max_articles} 条

新闻素材：
{json.dumps(news, ensure_ascii=False, indent=2)}

请生成简报，要求：
1. 开头用一句话概述今日最重要动态。
2. 每条新闻用 ### 标题，下面写 2-3 句摘要，并附原文链接。
3. 整体控制在 {max_articles} 条以内，按用户关注的重要性和时效性排序。
4. 语言为{lang_hint}。
5. 如果新闻素材与用户关注话题不相关，请明确指出并只挑选最相关的内容。
"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    result = chat(messages, temperature=0.5)
    if "error" in result and result["error"]:
        # 如果模型调用失败，用模板兜底，保证功能不中断
        return _fallback_digest(news, preferences)
    return result.get("content") or _fallback_digest(news, preferences)


def _fallback_digest(news: List[Dict[str, Any]], preferences: Dict[str, Any]) -> str:
    """模型不可用时的兜底简报生成器。"""
    lines = ["# 每日新闻简报\n", f"## 关注话题：{', '.join(preferences.get('topics', []))}\n"]
    for idx, item in enumerate(news[: preferences.get("max_articles", 5)], start=1):
        lines.append(f"### {idx}. {item['title']}")
        lines.append(f"- 来源：{item['source']} | 日期：{item.get('date', '')}")
        lines.append(f"- 摘要：{item['summary']}")
        lines.append(f"- 链接：{item['url']}\n")
    return "\n".join(lines)
