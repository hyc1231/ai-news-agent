"""
Shell 命令工具。为了安全，只允许执行白名单内的只读/低风险命令，
禁止删除、覆写、管道组合等危险操作。

安全策略说明：
1. 白名单前缀优先：不在白名单内的命令直接拒绝；
2. 危险 token 按“单词边界”匹配（而不是子串），避免把 term、format_table 等
   正常词汇误判为 rm / format；
3. 尽量不经过 shell 执行；确需 shell 的内建命令（dir / echo / type 等）
   才交给 cmd /d /c，且此时已禁止所有管道、重定向与变量符号；
4. 命令中不得出现敏感文件（.env / *.key / *.pem 等）——
   否则 cat .env 就能绕过 file_tools 的敏感文件保护，把密钥读进模型上下文。
   敏感文件名单与 file_tools 共用 tools/security.py，避免两处不一致；
5. 命令里的路径参数必须落在项目目录内，复用同一个 security.safe_path()。
   这一条是补出来的：只挡敏感文件名并不能阻止 `cat C:\\Windows\\win.ini`
   把项目外的任意文件读进上下文，而新闻正文是**不可信输入**，
   一段藏在网页里的提示词就足以诱导模型去读它。
   判定跨平台一致：绝对路径（盘符 / UNC / 前导 `/`）一律拒绝，项目内引用写相对路径；
   切词前先把反斜杠归一化成正斜杠，否则 Linux 下 `..\\..\\..` 会被 shlex 吃掉反斜杠而失效。
6. 白名单里**不含任何解释器**（python / pip / uvicorn / pytest 等）。
   这是本模块安全模型的关键一环：只要还能执行任意代码，第 4、5 条就会被绕过——
   write_file 写一个脚本，再用 bash 运行它，就能读到 .env 全文或项目外文件。
   因此这里只开放只读的查看类命令，简报流程也不依赖解释器。
"""

import locale
import os
import shlex
import subprocess
from pathlib import Path

from tools.security import PROJECT_ROOT, find_sensitive_reference, safe_path

# 允许执行的命令白名单（第一个 token 必须命中）
#
# 刻意不含 python / python3 / py / pip / uvicorn / pytest 等解释器类命令：
# 它们能执行任意代码，与 write_file 组合起来就是一条完整的任意代码执行链路
# （写脚本 -> 运行脚本），敏感文件名单会被整体绕过。只保留只读查看类命令。
ALLOWED_COMMANDS = (
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


def _looks_like_path(token: str) -> bool:
    """
    判断一个参数是不是在「指定路径」，而不是开关或普通关键词。

    只看形状、不做业务判断：不先筛一遍的话，`-la`、`--oneline`、`大模型`
    这类参数都会被当成路径丢进 resolve()。
    """
    if not token or token.startswith("-"):
        return False
    if token.startswith(("http://", "https://")):
        return False
    if len(token) > 1 and token[1] == ":":  # Windows 盘符：C:\... 或 C:foo
        return True
    if token in (".", ".."):
        return True
    return any(ch in token for ch in ("/", "\\"))


def _path_tokens(cmd: str) -> list[str]:
    """
    按「路径检查」的视角切分命令：先把反斜杠统一成正斜杠，再按 POSIX 规则切词。

    为什么要先归一化 —— 两个宿主差异会让判定结果不一致：
    1. Windows 上 shlex 保留 `\\`，Linux（posix=True）把 `\\` 当转义符吃掉：
       `dir ..\\..\\..` 在 Linux 上被切成 `......`，这条命令**连「像路径」都不成立**，
       路径沙箱被整体绕开（CI 就是挂在这里，实测复现过）；
    2. POSIX 下 `C:/Windows/win.ini` 会被当相对路径 join 进项目根，
       从而被误判成「项目内」，而它在 Windows 上是绝对路径。
    归一化后两端切出同一串 token，判定才能跨平台一致。
    security.find_sensitive_reference() 出于同样的原因也先做 replace("\\\\", "/")。
    """
    try:
        return shlex.split(cmd.replace("\\", "/"), posix=True)
    except ValueError:
        return []


def _is_absolute_like(token: str) -> bool:
    """
    判断是不是「绝对路径写法」：POSIX 的 `/xxx`、UNC 的 `\\\\srv\\share`、Windows 盘符 `C:...`。

    不依赖 os.name，也不要求路径真实存在 —— 项目内引用一律写相对路径就够用了。
    """
    if token.startswith("/"):
        return True
    return len(token) > 1 and token[1] == ":"  # 盘符：C:\... / C:/... / C:foo


def _check_paths(tokens) -> str:
    """
    路径沙箱：命令里出现的路径参数必须落在项目目录内。

    为什么必须有这一条：只挡敏感文件名是不够的 —— `cat C:\\Windows\\win.ini`
    能把宿主机上任意文件读进模型上下文（实测确认过）。而新闻正文是**不可信输入**
    且会进模型上下文，一段藏在网页里的提示词就足以诱导模型去读它。
    file_tools 一直有这层边界（走 security.safe_path），shell 侧漏了，现在复用同一份实现。

    两条规则，都不依赖宿主系统（Windows 与 Linux 结论一致）：
    1. 绝对路径一律拒绝（盘符 / UNC / 前导 `/`）。想读项目内文件就写相对路径——
       这样判定不需要知道宿主是什么系统，也回避了 `C:` 盘符相对路径的歧义；
    2. 其余路径交给 security.safe_path() 做真实祖先目录判断，
       `..\\..\\` 这类上跳会被算出来并拒绝（前缀相同的同级目录也挡得住）。

    tokens 由 _path_tokens() 产出（反斜杠已归一化）。
    """
    for token in tokens[1:]:
        if not _looks_like_path(token):
            continue
        if _is_absolute_like(token):
            return f"命令访问了项目目录之外的路径（路径越界），已被拒绝: {token}"
        try:
            safe_path(token)
        except ValueError:
            return f"命令访问了项目目录之外的路径（路径越界），已被拒绝: {token}"
        except OSError:
            # 路径本身非法（含非法字符等）时同样拒绝，这里宁可保守
            return f"命令包含无法解析的路径，已被拒绝: {token}"
    return ""


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

    # 命令中不得引用敏感文件（cat .env / type .env* / python -c "open('.env')"）
    sensitive = find_sensitive_reference(cmd)
    if sensitive:
        return (
            f"命令引用了敏感文件（含密钥/口令），已被拒绝: {sensitive}。"
            "如需了解配置项，请读取 .env.example 模板文件。"
        )

    # 路径参数必须在项目目录内（cat C:\Windows\win.ini 这类要挡住）。
    # 用归一化后的 token 做判定：反斜杠先转正斜杠，避免 Windows / Linux 切词不一致
    # 导致 Linux 上 `..\..\..` 被吃掉反斜杠、整条规则失效。
    path_reason = _check_paths(_path_tokens(cmd) or tokens)
    if path_reason:
        return path_reason

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
