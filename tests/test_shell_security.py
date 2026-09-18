"""
shell 工具与路径沙箱的安全边界。

重点回归一条真实存在的绕过链路：只用 file_tools 的敏感文件名单是不够的——
如果 shell 白名单里还有解释器，就可以 write_file 写一个脚本、再用 bash 运行它，
把 .env 全文读进模型上下文。所以「白名单不含解释器」是必须长期守住的约束。
"""

import pytest

from tools.file_tools import list_dir, read_file, write_file
from tools.security import PROJECT_ROOT
from tools.shell_tools import ALLOWED_COMMANDS, bash


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
