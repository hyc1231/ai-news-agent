"""
大模型客户端封装。
当前使用 DeepSeek 的 OpenAI 兼容接口，支持普通对话和 tool calling。
内置网络重试与指数退避：一次偶发 429/超时不应该让整轮 Agent 直接终止。
"""

import os
import time
import random
from typing import List, Dict, Any, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# 请求超时、失败重试次数与退避基数（均可通过环境变量调整）
REQUEST_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "60"))
MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))          # 额外重试次数，总尝试 = 1 + MAX_RETRIES
RETRY_BACKOFF = float(os.getenv("LLM_RETRY_BACKOFF", "1.5"))  # 退避基数：1.5^n 秒
LLM_MAX_TOKENS = os.getenv("LLM_MAX_TOKENS", "").strip()      # 为空则用服务端默认值

# 这些状态码属于「稍后重试可能成功」，其余 4xx 是请求本身的问题，重试无意义
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def _error(message: str) -> Dict[str, Any]:
    """构造统一的错误返回结构。"""
    return {"error": message, "content": None, "tool_calls": None, "finish_reason": None, "truncated": False}


def chat(
    messages: List[Dict[str, str]],
    tools: Optional[List[Dict[str, Any]]] = None,
    temperature: float = 0.7,
) -> Dict[str, Any]:
    """
    调用大模型对话接口。
    返回原始响应字典，包含 content / tool_calls / finish_reason / truncated 等字段。

    失败时最多重试 MAX_RETRIES 次（指数退避），只有网络错误与
    408/429/5xx 这类可恢复错误才会重试；其他 4xx 立即返回错误。
    finish_reason 为 "length" 时 truncated=True，调用方不应把这种被截断的
    内容当成最终答案。
    """
    if not API_KEY:
        return _error("未配置 DEEPSEEK_API_KEY，请在 .env 文件中设置")

    url = f"{BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if LLM_MAX_TOKENS:
        try:
            payload["max_tokens"] = int(LLM_MAX_TOKENS)
        except ValueError:
            pass

    last_error = ""
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)

            if resp.status_code >= 400:
                if resp.status_code not in RETRYABLE_STATUS:
                    return _error(f"API 请求失败: {resp.status_code} - {resp.text[:500]}")
                last_error = f"HTTP {resp.status_code} - {resp.text[:200]}"
            else:
                data = resp.json()
                choice = data.get("choices", [{}])[0]
                message = choice.get("message", {})
                finish_reason = choice.get("finish_reason")
                return {
                    "content": message.get("content"),
                    "tool_calls": message.get("tool_calls"),
                    "finish_reason": finish_reason,
                    "truncated": finish_reason == "length",
                    "raw": data,
                }
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = f"网络错误: {e}"
        except Exception as e:
            # JSON 解析失败等属于不可恢复错误，直接返回
            return _error(f"调用模型时出错: {e}")

        if attempt < MAX_RETRIES:
            # 指数退避 + 抖动，避免多个并发请求同时重试形成尖峰
            time.sleep(RETRY_BACKOFF ** attempt + random.uniform(0, 0.5))

    return _error(f"调用模型失败（已尝试 {MAX_RETRIES + 1} 次）: {last_error}")
