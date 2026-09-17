"""
Shell 命令工具。为了安全，只允许执行白名单内的只读/低风险命令，
禁止删除、覆写、管道组合等危险操作。

安全策略说明：
1. 白名单前缀优先：不在白名单内的命令直接拒绝；
2. 危险 token 按“单词边界”匹配（而不是子串），避免把 term、format_table 等
   正常词汇误判为 rm / format；
3. 尽量不经过 shell 执行；确需 shell 的内建命令（dir / echo / type 等）
   才交给 cmd /d /c，且此时已禁止所有管道、重定向与变量符号。
"""

import locale
import os
import shlex
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 允许执行的命令白名单（第一个 token 必须命中）
ALLOWED_COMMANDS = (
    "python", "python3", "py", "pip", "pip3",
    "uvicorn", "pytest",
    "git", "ls", "dir", "pwd", "echo", "cat", "type",
    "head", "tail", "find", "where",
)

# 明确禁止的危险 token（按单词边界匹配）
FORBIDDEN_TOKENS = (
    "rm", "rmdir", "del", "erase", "format", "mkfs", "rd",
    "shutdown", "reboot", "kill", "taskkill",
    "curl", "wget", "ssh", "scp", "chmod", "chown",
)

# 禁止出现的 shell 元字符：管道/重定向/命令连接/变量展开
FORBIDDEN_CHARS = (">", "<", "|", "&", ";", "$", "`", "%", "!", "\n")

# 只能由 shell 提供的内建命令（无法作为可执行文件直接 spawn）
SHELL_BUILTINS = {"dir", "echo", "type", "where", "cls", "cd"}

# 常见 Unix 命令在 Windows 下的等价写法，方便模型直接用习惯命令
WIN_ALIASES = {"ls": "dir", "cat": "type", "pwd": "cd"}


def _check_safety(cmd: str) -> str:
    """返回空字符串表示通过检查，否则返回拒绝原因。"""
    try:
        tokens = shlex.split(cmd, posix=(os.name != "nt"))
    except ValueError:
        return f"命令无法解析（引号不匹配）: {cmd}"

    if not tokens:
        return "命令不能为空"

    # 检查 shell 元字符（在原始字符串中判断，防止绕过 shlex 拆分）
    if any(ch in cmd for ch in FORBIDDEN_CHARS):
        return f"命令包含管道/重定向/变量等危险符号，已被拒绝: {cmd}"

    program = Path(tokens[0]).name.lower()
    if program not in ALLOWED_COMMANDS:
        return f"命令不在允许列表内，已被拒绝: {cmd}"

    # 按单词边界匹配危险 token（形如 rm -rf 的第一个参数）
    for token in tokens[1:]:
        word = token.lower()
        if word in FORBIDDEN_TOKENS:
            return f"命令包含危险子命令，已被拒绝: {cmd}"

    # git 只允许只读子命令
    if program == "git" and len(tokens) > 1:
        if tokens[1].lower() not in ("status", "log", "diff", "branch", "show"):
            return f"git 子命令不在只读白名单内，已被拒绝: {tokens[1]}"

    return ""


def _build_argv(cmd: str):
    """
    把命令字符串转成 argv 列表。
    - Unix 常见命令在 Windows 下自动映射为等价的 cmd 内建命令；
    - 内建命令通过 cmd /d /c 调用（此时已排除所有元字符）；
    - 其余命令直接 spawn 可执行文件，完全不经过 shell。
    """
    tokens = shlex.split(cmd, posix=(os.name != "nt"))
    program = Path(tokens[0]).name.lower()
    program = WIN_ALIASES.get(program, program)
    tokens = [program] + tokens[1:]

    if os.name == "nt" and program in SHELL_BUILTINS:
        # /d 跳过 AutoRun，避免被注册表里的脚本劫持
        return ["cmd", "/d", "/c", *tokens]
    return tokens


def _decode(data: bytes) -> str:
    """
    解码子进程输出。依次尝试 UTF-8 和系统本地编码（Windows 中文环境为 GBK），
    避免因编码不一致导致中文乱码。
    """
    if not data:
        return ""
    candidates = ["utf-8", locale.getpreferredencoding(False), "gbk", "mbcs"]
    seen = set()
    for enc in candidates:
        if not enc or enc.lower() in seen:
            continue
        seen.add(enc.lower())
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def bash(command: str) -> str:
    """
    执行 shell 命令并返回输出。
    仅用于项目目录内的辅助操作，如查看目录、运行测试等。
    """
    cmd = command.strip()
    if not cmd:
        return "命令不能为空"

    reject_reason = _check_safety(cmd)
    if reject_reason:
        return reject_reason

    try:
        result = subprocess.run(
            _build_argv(cmd),
            shell=False,                     # 不经整串 shell，避免注入
            capture_output=True,             # 以字节捕获，交给 _decode 选择编码
            timeout=30,
            cwd=str(PROJECT_ROOT),           # 限制在项目根目录内执行
        )
        # Windows 的 cmd 输出通常是 GBK/macOS 是 UTF-8，逐个尝试避免中文乱码
        output = _decode(result.stdout).strip()
        err = _decode(result.stderr).strip()
        if result.returncode != 0:
            return f"命令执行失败 (code={result.returncode}):\n{err or output}"
        return output or "（命令执行成功，无输出）"
    except subprocess.TimeoutExpired:
        return "命令执行超时（超过 30 秒）"
    except FileNotFoundError:
        return f"命令不存在或不可用: {cmd}"
    except Exception as e:
        return f"执行命令时出错: {e}"
