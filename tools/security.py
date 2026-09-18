"""
工具层统一的安全边界：路径沙箱 + 敏感文件识别。

file_tools 与 shell_tools 共用这一份定义。之前敏感文件黑名单只写在 file_tools 里，
结果 read_file(".env") 被拦住、但 bash("cat .env") 照样把密钥读进模型上下文——
两边各维护一份黑名单迟早会不一致，所以这里收敛成唯一来源。
"""

import glob
import re
from pathlib import Path

# 项目根目录：ai-news-agent 文件夹
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 含敏感信息的文件：不可读、不可写、搜索时跳过、shell 命令里出现即拒绝
SENSITIVE_FILE_NAMES = {
    ".env", ".env.local", ".env.production", ".env.development", ".env.test",
    ".npmrc", ".pypirc", ".netrc", "credentials", "credentials.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
}
SENSITIVE_SUFFIXES = {".env", ".key", ".pem", ".pfx", ".p12", ".keystore", ".jks"}

# 这些后缀是模板/示例文件（.env.example），本身不含真实密钥，允许访问
TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".example.local")

# 搜索时要跳过的目录：依赖、缓存、版本库等，避免耗时且返回一堆无关命中。
# 实测项目里 3161 个文件中 3057 个位于 .venv，不剪枝会让搜索基本失效。
SEARCH_SKIP_DIRS = {
    ".venv", "venv", "env", "__pycache__", ".git", ".hg", ".svn",
    "node_modules", ".idea", ".vscode", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "dist", "build", ".tox", ".next", "site-packages",
}

# 在任意文本（含命令行）中识别敏感文件引用。
# 既覆盖 cat .env 这种直接引用，也覆盖 python -c "open('.env')" 这类字符串内引用。
# (?<![\w.-]) / (?![\w-]) 保证按边界匹配，不会把 mycompany.env.example 之类误伤。
_SENSITIVE_NAME_RE = re.compile(
    r"(?<![\w.\-/])"
    r"(?:"
    r"\.env(?:\.[\w-]+)?"
    r"|[\w-]+\.(?:key|pem|pfx|p12|jks|keystore)"
    r"|credentials(?:\.json)?"
    r"|id_(?:rsa|dsa|ecdsa|ed25519)"
    r"|\.npmrc|\.pypirc|\.netrc"
    r")"
    r"(?![\w-])",
    re.IGNORECASE,
)


def is_sensitive_name(name: str) -> bool:
    """按文件名判断是否为敏感文件（模板文件除外）。"""
    lowered = Path(name).name.lower()
    if any(lowered.endswith(suffix) for suffix in TEMPLATE_SUFFIXES):
        return False  # .env.example 这类模板允许访问，README 里就是让人照着复制
    if lowered in SENSITIVE_FILE_NAMES:
        return True
    return any(lowered.endswith(suffix) for suffix in SENSITIVE_SUFFIXES)


def is_sensitive(target: Path) -> bool:
    """按路径判断是否为敏感文件。"""
    return is_sensitive_name(target.name)


def find_sensitive_reference(text: str) -> str:
    """
    在命令文本中查找对敏感文件的引用，返回命中的文件名；未命中返回空串。

    两步走：
    1. 通配符展开：拦截 `type .env*` 这种用通配符绕过直接名字检查的写法；
    2. 正则边界匹配：拦截直接引用与字符串内嵌引用。
    """
    normalized = text.replace("\\", "/")
    for raw in normalized.split():
        token = raw.strip("\"'`()[],")
        if not token or token.startswith("-") or not any(ch in token for ch in "*?"):
            continue
        try:
            hits = glob.glob(token, root_dir=str(PROJECT_ROOT))
        except Exception:
            hits = []
        if any(is_sensitive_name(Path(hit).name) for hit in hits):
            return token

    for match in _SENSITIVE_NAME_RE.finditer(text):
        name = match.group(0)
        if name.lower().endswith(TEMPLATE_SUFFIXES):
            continue
        return name
    return ""


def safe_path(path: str) -> Path:
    """
    把相对路径解析为项目根目录下的绝对路径，防止 .. 跳出项目。

    用 is_relative_to 做真实的祖先目录判断，而不是字符串前缀比较——
    前缀比较存在绕过：项目目录是 ai-news-agent 时，同级目录
    ai-news-agent-BACKUP 也以该前缀开头，`..\\ai-news-agent-BACKUP\\x.txt`
    这类路径会被误判为“在项目内”。
    """
    target = (PROJECT_ROOT / path).resolve()
    if target != PROJECT_ROOT and not target.is_relative_to(PROJECT_ROOT):
        raise ValueError(f"路径越界，不允许访问项目根目录之外: {path}")
    return target
