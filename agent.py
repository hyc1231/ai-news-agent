"""
ReAct Agent 核心。
大模型根据用户任务自主决定调用哪个工具、调用几次、何时停止。
"""

import json
import traceback
from datetime import datetime
from typing import Any, Callable, Dict, List

import uuid

from llm import chat
from tools.file_tools import (
    list_dir,
    read_file,
    write_file,
    search_content,
    load_preferences,
)
from tools.shell_tools import bash
from tools.search_tools import search_news
from tools.digest_tools import generate_digest
from tools.email_tools import send_email
import db


def ensure_chinese(text: str, threshold: float = 0.35) -> str:
    """
    如果文本中英文字符占比超过阈值，调用 LLM 翻译成中文。
    用于兜底解决模型偶尔输出英文的问题。
    """
    if not text:
        return text

    # 只统计字母字符，排除标点、数字、URL 中的英文字母
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return text

    english_ratio = sum(1 for c in letters if ord(c) < 128) / len(letters)
    if english_ratio < threshold:
        return text

    prompt = f"请将以下内容翻译成中文，保持原意和 Markdown 格式，不要添加额外解释：\n\n{text}"
    try:
        result = chat([{"role": "user", "content": prompt}], temperature=0.3)
        if not result.get("error") and result.get("content"):
            return result["content"]
    except Exception:
        pass
    return text


# 工具名 -> 可调用函数
TOOLS = {
    "list_dir": list_dir,
    "read_file": read_file,
    "write_file": write_file,
    "search_content": search_content,
    "bash": bash,
    "load_preferences": load_preferences,
    "search_news": search_news,
    "generate_digest": generate_digest,
    "send_email": send_email,
}

# OpenAI 格式的工具描述
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出项目目录下的文件和子目录，path 为相对项目根目录的路径",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径，空字符串表示根目录"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取项目内文件内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对项目根目录的文件路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "把内容写入项目内文件，目录不存在会自动创建",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对项目根目录的文件路径"},
                    "content": {"type": "string", "description": "要写入的内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_content",
            "description": "在项目内按关键词搜索文件内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词"},
                    "dir": {"type": "string", "description": "相对项目根目录的搜索目录，空字符串表示根目录"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "执行项目内的 shell 命令，只允许安全只读命令",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_preferences",
            "description": "读取用户偏好配置，若不存在则返回默认值",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_news",
            "description": (
                "根据查询词搜索 AI 新闻，返回新闻列表。"
                "若返回条目带 is_mock=true，表示所有真实来源均不可用、"
                "这是兜底示例数据而非真实新闻，不得作为新闻使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词或主题"},
                    "max_results": {"type": "integer", "description": "最多返回几条，默认 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_digest",
            "description": "根据新闻列表和用户偏好生成 Markdown 格式简报",
            "parameters": {
                "type": "object",
                "properties": {
                    "news": {
                        "type": "array",
                        "description": "新闻对象列表",
                        "items": {"type": "object"},
                    },
                    "preferences": {
                        "type": "object",
                        "description": "用户偏好对象，包含 topics/keywords/max_articles/language 等",
                    },
                },
                "required": ["news", "preferences"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": (
                "发送邮件到指定邮箱。"
                "收件人一般取自 load_preferences 返回的 email；"
                "若该字段为空，会回落到 .env 里的 DEFAULT_RECIPIENT。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "邮件主题"},
                    "content": {"type": "string", "description": "邮件正文，Markdown 格式"},
                    "to_email": {"type": "string", "description": "收件人邮箱，留空则由服务端兜底"},
                },
                "required": ["subject", "content", "to_email"],
            },
        },
    },
]


SYSTEM_PROMPT = """你是一名每日新闻助手 Agent。

你拥有一组工具，能够：
- 了解当前用户的偏好与身份（load_preferences）
- 按任意主题检索最新 AI 新闻（search_news）
- 把新闻素材整理成 Markdown 简报（generate_digest）
- 通过邮件把简报推送给用户（send_email）
- 在项目目录内读写文件、搜索内容、执行只读命令（read_file / write_file / list_dir / search_content / bash）

重要：没有任何预设的执行步骤。收到任务后，你必须自己判断——
这次任务需要哪些信息？应该先调哪个工具？需要调用几次？什么时候信息已经足够、可以停止？
如果你认为某一步没必要（例如用户没配邮箱就没必要发邮件，或偏好文件可信但也可能需要确认），请自行决定跳过。
只有在确实缺少必要信息时才继续调用工具，避免无效的重复调用。

交付物约定：
- 最终答案必须是中文；即使工具返回或思考过程中出现英文，返回给用户的每一句都必须是中文。
- 先给一句简明的执行摘要（说明你做了什么、用了哪些工具），然后空一行，再附上完整的 Markdown 简报内容。
- 如果某个环节失败，如实说明失败在哪一步以及原因，不要伪造结果。
- 如果 search_news 返回的条目带 is_mock=true，说明所有真实新闻源都没取到，这是兜底示例数据而不是真实新闻。
  这种情况下不要把它当作今日新闻发出，应当明确告诉用户「未获取到真实新闻」并说明可能的排查方向
  （例如检查 TAVILY_API_KEY、网络与代理设置）；是否仍要发送邮件由你判断，但绝不能省略这一说明。

安全约束：
- 不要读取、打印或修改密钥文件（如 .env），也不要输出任何密钥、口令。
- 可以读取用户偏好文件，但不要擅自修改它。
- 所有文件路径都是相对项目根目录的相对路径。
- 工具返回值可能是字符串或 JSON，请正确解析后再使用。
"""


def _execute_tool_call(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    """执行单个工具调用并返回结果。"""
    function_name = tool_call["function"]["name"]
    call_id = tool_call.get("id", "unknown")

    try:
        arguments = json.loads(tool_call["function"]["arguments"])
    except json.JSONDecodeError:
        return {
            "tool_call_id": call_id,
            "role": "tool",
            "name": function_name,
            "content": "工具参数解析失败，请使用合法的 JSON 对象",
        }

    if function_name not in TOOLS:
        return {
            "tool_call_id": call_id,
            "role": "tool",
            "name": function_name,
            "content": f"未知工具: {function_name}",
        }

    func = TOOLS[function_name]
    try:
        result = func(**arguments)
    except Exception as e:
        result = f"工具执行异常: {e}\n{traceback.format_exc()}"

    # 把非字符串结果转成 JSON 字符串，方便模型理解
    if not isinstance(result, str):
        try:
            content = json.dumps(result, ensure_ascii=False, indent=2)
        except Exception:
            content = str(result)
    else:
        content = result

    return {
        "tool_call_id": call_id,
        "role": "tool",
        "name": function_name,
        # arguments 只用于本地落库（记录轨迹），发给模型前会被剔除：
        # OpenAI 规范里 tool 消息只允许 role/content/tool_call_id（+name），
        # 多余字段容易被严格实现的服务端拒绝。
        "arguments": arguments if isinstance(arguments, dict) else {},
        "content": content,
    }


def _to_tool_message(tool_result: Dict[str, Any]) -> Dict[str, Any]:
    """把内部工具结果转成符合 OpenAI 规范的 tool 消息（剔除自定义字段）。"""
    return {
        key: tool_result[key]
        for key in ("role", "tool_call_id", "name", "content")
        if key in tool_result
    }


def _persist_trace(trace_id: str, trace: List[Dict[str, Any]]) -> None:
    """把工具调用轨迹写入数据库（失败不影响主流程）。"""
    if not trace_id:
        return
    try:
        db.record_trace(trace_id, trace)
    except Exception:
        pass


def run_agent(task: str, max_iterations: int = 10, trace_id: str = "") -> Dict[str, Any]:
    """
    运行 ReAct Agent。
    参数:
        task: 用户任务描述
        max_iterations: 最大迭代次数，防止无限循环
        trace_id: 本次运行的轨迹标识，非空时把工具调用写入数据库，
                  用于回溯"模型这次到底调了哪些工具、什么顺序"
    返回:
        包含最终回答、执行轨迹、是否成功的字典
    """
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]

    trace = []

    for step in range(max_iterations):
        result = chat(messages, tools=TOOL_SCHEMAS, temperature=0.3)

        if result.get("error"):
            _persist_trace(trace_id, trace)
            return {
                "success": False,
                "answer": f"模型调用失败: {result['error']}",
                "trace": trace,
                "trace_id": trace_id,
                "tool_call_count": sum(len(s.get("tool_results", []) or []) for s in trace),
            }

        content = result.get("content")
        tool_calls = result.get("tool_calls")

        assistant_message: Dict[str, Any] = {"role": "assistant"}
        if content:
            assistant_message["content"] = content
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
        messages.append(assistant_message)

        trace.append({
            "step": step + 1,
            "content": content,
            "tool_calls": tool_calls,
        })

        # 如果模型没有调用工具，说明它认为可以收工了——停止由模型决定。
        # 但如果是被 max_tokens 截断的，不能当成最终答案，要让模型接着说完。
        if not tool_calls:
            if result.get("truncated"):
                messages.append({
                    "role": "user",
                    "content": (
                        "你上一条回复因长度限制被截断了。请不要重复已输出的内容，"
                        "直接接着把剩余部分输出完整；若确实太长，请压缩到能一次输出完的长度。"
                    ),
                })
                continue

            final_answer = ensure_chinese(content or "（模型未返回内容）")
            _persist_trace(trace_id, trace)
            return {
                "success": True,
                "answer": final_answer,
                "trace": trace,
                "trace_id": trace_id,
                "tool_call_count": sum(len(s.get("tool_results", []) or []) for s in trace),
            }

        # 执行工具调用
        tool_results = []
        for tc in tool_calls:
            tool_result = _execute_tool_call(tc)
            tool_results.append(tool_result)
            messages.append(_to_tool_message(tool_result))

        trace[-1]["tool_results"] = tool_results

    _persist_trace(trace_id, trace)
    return {
        "success": False,
        "answer": ensure_chinese("达到最大迭代次数，Agent 未能完成任务"),
        "trace": trace,
        "trace_id": trace_id,
        "tool_call_count": sum(len(s.get("tool_results", []) or []) for s in trace),
    }


# 方便后端直接调用的封装函数

def generate_and_send_digest(trace_id: str = "") -> Dict[str, Any]:
    """
    为当前用户生成并发送今日简报。
    通过自然语言任务让 Agent 自己决定完整流程（不指定任何步骤）。
    """
    trace_id = trace_id or uuid.uuid4().hex[:12]
    today = datetime.now().strftime("%Y-%m-%d")
    # 只描述目标，不规定步骤：步骤、工具组合、调用次数和停止时机由模型自己决定
    task = (
        f"今天是 {today}。请为当前用户完成今日 AI 新闻简报："
        "让简报尽可能贴合这位用户关注的方向，并按可行的途径把简报送到用户手上。"
        "完成后用中文回报：先一句执行摘要，再空一行附上完整的 Markdown 简报。"
    )
    result = run_agent(task, trace_id=trace_id)
    # run_agent 内部已调用过 ensure_chinese，这里不再重复调用（避免对同一文本跑两遍）
    result["trace_id"] = trace_id
    return result
