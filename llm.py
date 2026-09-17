"""
大模型客户端封装。
当前使用 DeepSeek 的 OpenAI 兼容接口，支持普通对话和 tool calling。
"""

import os
import json
from typing import List, Dict, Any, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")


def chat(
    messages: List[Dict[str, str]],
    tools: Optional[List[Dict[str, Any]]] = None,
    temperature: float = 0.7,
) -> Dict[str, Any]:
    """
    调用大模型对话接口。
    返回原始响应字典，包含 content 和 tool_calls 等字段。
    """
    if not API_KEY:
        return {
            "error": "未配置 DEEPSEEK_API_KEY，请在 .env 文件中设置",
            "content": None,
            "tool_calls": None,
        }

    url = f"{BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        return {
            "content": message.get("content"),
            "tool_calls": message.get("tool_calls"),
            "finish_reason": choice.get("finish_reason"),
            "raw": data,
        }
    except requests.exceptions.HTTPError as e:
        return {
            "error": f"API 请求失败: {e.response.status_code} - {e.response.text}",
            "content": None,
            "tool_calls": None,
        }
    except Exception as e:
        return {
            "error": f"调用模型时出错: {e}",
            "content": None,
            "tool_calls": None,
        }
