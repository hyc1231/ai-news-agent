"""
shell 工具与路径沙箱的安全边界。

重点回归一条真实存在的绕过链路：只用 file_tools 的敏感文件名单是不够的——
如果 shell 白名单里还有解释器，就可以 write_file 写一个脚本、再用 bash 运行它，
把 .env 全文读进模型上下文。所以「白名单不含解释器」是必须长期守住的约束。
"""

import pytest

from tools.file_tools import list_dir, read_file, write_file
from tools.security import PROJECT_ROOT
from tools.shell_tools import (
    ALLOWED_COMMANDS,
    _check_safety,
    _looks_like_path,
    _path_tokens,
    bash,
)


def _is_rejected(cmd: str) -> bool:
    out = bash(cmd)
    return any(word in out for word in ("拒绝", "不允许", "越界"))


@pytest.mark.parametrize(
    "cmd",
    [
        'python -c "print(open(\'.env\').read())"',
        "python run_probe.py",
        "python3 run_probe.py",
        "py run_probe.py",
        "pip install requests",
        "uvicorn app:app",
        "pytest",
    ],
)
def test_interpreters_are_rejected(cmd):
    """解释器类命令一律拒绝——它们是任意代码执行的入口。"""
    assert _is_rejected(cmd), f"这条命令本应被拒绝，但它通过了: {cmd}"


def test_whitelist_contains_no_interpreter():
    forbidden = {"python", "python3", "py", "pip", "pip3", "uvicorn", "pytest"}
    assert forbidden.isdisjoint(set(ALLOWED_COMMANDS))


def test_write_then_run_chain_is_blocked():
    """完整攻击链：写脚本（允许）-> 运行脚本（必须被拒绝）。"""
    target = "data/_security_probe.py"
    wrote = write_file(target, "print(open('.env').read())")
    assert wrote.startswith("写入成功")
    try:
        out = bash(f"python {target}")
        assert "拒绝" in out, f"写脚本再执行这条绕过链路必须被挡住，实际输出: {out}"
    finally:
        probe = PROJECT_ROOT / target
        if probe.exists():
            probe.unlink()


@pytest.mark.parametrize("cmd", ["cat .env", "type .env", "type .env*"])
def test_sensitive_file_reference_is_rejected(cmd):
    assert _is_rejected(cmd), f"{cmd} 会把 .env 内容读进模型上下文"


def test_env_example_is_still_readable():
    """模板文件不含真实密钥，应当仍可访问（否则模型连要配哪些项都不知道）。"""
    assert not _is_rejected("type .env.example")
    assert read_file(".env.example").startswith("#")


@pytest.mark.parametrize(
    "cmd",
    [
        "ls | grep x",       # 管道
        "cat a.txt > b.txt",  # 重定向
        "ls && ls",          # 命令连接
        "rm -rf data",       # 危险子命令
        "git push",          # git 只允许只读子命令
    ],
)
def test_dangerous_commands_are_rejected(cmd):
    assert _is_rejected(cmd), f"{cmd} 属于危险命令"


def test_readonly_command_is_allowed():
    assert not _is_rejected("ls")


def test_path_traversal_is_blocked():
    assert "越界" in read_file("../.env")
    assert "越界" in list_dir("../")


def test_sensitive_file_cannot_be_read_or_written():
    assert "敏感" in read_file(".env")
    assert "敏感" in write_file(".env", "HACKED=1")


# --- 路径沙箱 ---------------------------------------------------------------
#
# 回归背景：只挡敏感文件名是不够的 —— `cat C:\Windows\win.ini` 能把宿主机上
# 任意文件读进模型上下文（实测确认过），而新闻正文是**不可信输入**且会进
# 模型上下文，一段藏在网页里的提示词就足以诱导模型去读它。
# file_tools 一直有这层边界（走 security.safe_path），shell 侧以前漏了。


@pytest.mark.parametrize(
    "cmd",
    [
        r"cat C:\Windows\win.ini",  # Windows 绝对路径
        "cat C:/Windows/win.ini",  # 正斜杠写法
        r"dir C:\Users",
        r"dir ..\..\..",  # 逐级上跳
        r"type ..\..\..\Windows\win.ini",
        r"cat ..\..\ai-news-agent-BACKUP\x.txt",  # 同级目录：前缀相同但不该放行
        "cat /etc/passwd",  # Unix 绝对路径
    ],
)
def test_paths_outside_project_are_rejected(cmd):
    assert "越界" in bash(cmd), f"{cmd} 读到了项目目录之外"


@pytest.mark.parametrize(
    "cmd",
    [
        "ls",
        "dir",
        "dir db",
        "type README.md",
        "cat tools/security.py",
        "type .env.example",
        "git status",
        "git log --oneline -3",
    ],
)
def test_paths_inside_project_are_allowed(cmd):
    """
    直接问 _check_safety() 拿结论，而不是扫命令输出。

    扫输出会假阳性：README 正文里本身就写着「`.env` 里填了越界值」这类句子，
    `type README.md` 的输出里就带「越界」二字。
    """
    assert _check_safety(cmd) == "", f"{cmd} 是项目内的正常只读操作，被误拦了"


@pytest.mark.parametrize(
    "token",
    ["-la", "--oneline", "大模型", "README.md", "https://example.com/a", ""],
)
def test_plain_arguments_are_not_treated_as_paths(token):
    """开关与普通关键词不该被当成路径丢进 resolve()。"""
    assert not _looks_like_path(token)


@pytest.mark.parametrize("token", [".", "..", "a/b", r"a\b", r"C:\x", "C:x", "/etc"])
def test_path_like_arguments_are_detected(token):
    assert _looks_like_path(token)


# --- 跨平台一致性 -----------------------------------------------------------
#
# 回归背景：CI 第一版挂在 Linux 上（同一份代码在 Windows 本地 142 项全绿）。
# 两个宿主差异叠加，把路径沙箱整体绕开了：
#   1. shlex.split(posix=True) 把反斜杠当转义符吃掉 —— `dir ..\..\..` 被切成 `......`，
#      连「像路径」都不成立，_check_paths 直接跳过不判；
#   2. POSIX 下 `C:/Windows/win.ini` 被当**相对路径** join 进项目根 —— 误判成「项目内」。
# 下面这些用例都不依赖 os.name，所以 Windows 与 Linux 必须给出同一个结论。


@pytest.mark.parametrize(
    "cmd,expect",
    [
        (r"dir ..\..\..", ["dir", "../../.."]),
        (r"type ..\..\..\Windows\win.ini", ["type", "../../../Windows/win.ini"]),
        ("cat C:/Windows/win.ini", ["cat", "C:/Windows/win.ini"]),
        ("ls tools", ["ls", "tools"]),
    ],
)
def test_path_tokens_are_normalized(cmd, expect):
    """切词前先把反斜杠归一化 —— 这是两个平台判定一致的前提。"""
    assert _path_tokens(cmd) == expect


@pytest.mark.parametrize(
    "cmd",
    [
        r"dir ..\..\..",
        r"type ..\..\..\Windows\win.ini",
        "cat C:/Windows/win.ini",
        "cat //server/share/x.txt",  # UNC 写法
        "cat C:foo.txt",  # 盘符相对写法
        "head -c 100 C:/Windows/win.ini",
    ],
)
def test_windows_style_paths_are_rejected_on_every_platform(cmd):
    """Windows 写法在 Linux 宿主上同样要拦 —— CI 跑在 ubuntu-latest 上。"""
    assert "越界" in bash(cmd), f"{cmd} 在非 Windows 宿主上被放行了"


def test_absolute_path_inside_project_is_also_rejected():
    """
    项目内的绝对路径也一并拒绝：只放行相对路径，判定才不需要知道宿主系统。

    代价是模型写绝对路径会被挡（拒绝语里让它改用相对路径），换来的是 Windows 与 Linux
    结论完全一致 —— 否则「什么算项目内」会随部署环境变化而变，等于换机器就换一套边界。
    """
    absolute = (PROJECT_ROOT / "README.md").as_posix()
    assert "越界" in bash(f"cat {absolute}")
