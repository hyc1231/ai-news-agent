"""
邮件发送工具。使用 SMTP 发送每日简报。
"""

import os
import re
import html as html_lib
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from typing import Set

from dotenv import load_dotenv

load_dotenv()

SMTP_SERVER = os.getenv("SMTP_SERVER", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USERNAME)

# 收件人兜底：模型偶尔会漏传 to_email，或用户没在前端填过邮箱。
# 直接在工具这一层兜住，不依赖模型每次都把参数带对。
DEFAULT_RECIPIENT = os.getenv("DEFAULT_RECIPIENT", "").strip()


def allowed_recipients() -> Set[str]:
    """
    允许发送的收件人白名单：preferences.email ∪ DEFAULT_RECIPIENT。

    为什么要加这一道：收件人原先完全由模型给出，而新闻正文是**不可信输入**、
    会被塞进模型上下文 —— 抓到的网页里只要藏一段提示词，就可能诱导 Agent
    把简报发到任意第三方地址。那既是「向未同意者发信」，也是一条把项目内容
    带出去的外泄通道。

    读库失败时**保守退化**为只允许 DEFAULT_RECIPIENT，而不是放开白名单。
    """
    allowed: Set[str] = set()
    if DEFAULT_RECIPIENT:
        allowed.add(DEFAULT_RECIPIENT.strip().lower())
    try:
        # 延迟导入：tools 层不必在模块导入期就绑定数据库配置
        import db

        email = (db.load_preferences() or {}).get("email", "") or ""
        if email.strip():
            allowed.add(email.strip().lower())
    except Exception:
        pass
    return allowed


def send_email(subject: str, content: str, to_email: str) -> str:
    """
    发送一封邮件。
    参数:
        subject: 邮件主题
        content: 邮件正文（Markdown 格式，会转成 HTML 发送）
        to_email: 收件人邮箱，留空时回落到环境变量 DEFAULT_RECIPIENT
    返回:
        发送结果描述

    只允许发往白名单内的地址（见 allowed_recipients），其余一律拒绝。
    """
    to_email = (to_email or "").strip()
    used_fallback = False
    if not to_email and DEFAULT_RECIPIENT:
        to_email = DEFAULT_RECIPIENT
        used_fallback = True

    if not to_email:
        return (
            "邮件发送失败：未指定收件人，且 .env 里也没配置 DEFAULT_RECIPIENT。"
            "请在前端「设置」里填写收件人，或补上 DEFAULT_RECIPIENT。"
        )
    if not all([SMTP_SERVER, SMTP_USERNAME, SMTP_PASSWORD]):
        # 分开报错：原来统一报「缺少 SMTP 配置或收件人邮箱」，
        # 排查时分不清到底是哪一项没配。
        return "邮件发送失败：SMTP 配置不完整（需要 SMTP_SERVER / SMTP_USERNAME / SMTP_PASSWORD）"

    # 白名单检查放在 SMTP 配置检查之后：SMTP 都没配时，
    # 报「收件人非法」会把排查方向带偏。
    if to_email.lower() not in allowed_recipients():
        return (
            f"邮件发送失败：收件人 {to_email} 不在允许列表内。"
            "为避免简报被发到非订阅地址，只允许发送到用户在「设置」里保存的邮箱"
            "（或 .env 的 DEFAULT_RECIPIENT）。"
        )

    html_content = _markdown_to_html(content)

    # multipart/alternative：同时提供纯文本与 HTML 两个版本，
    # 既兼容不渲染 HTML 的客户端，也更不容易被判为垃圾邮件。
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText(content, "plain", "utf-8"))
    msg.attach(MIMEText(html_content, "html", "utf-8"))
    msg["From"] = formataddr(("AI 新闻助手", FROM_EMAIL))
    msg["To"] = to_email
    msg["Subject"] = Header(subject, "utf-8")

    try:
        # 465 端口通常需要 SSL（QQ 邮箱等）；587 端口用 STARTTLS
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.sendmail(FROM_EMAIL, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
                server.starttls()
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.sendmail(FROM_EMAIL, [to_email], msg.as_string())
        note = "（收件人来自 DEFAULT_RECIPIENT 兜底）" if used_fallback else ""
        return f"邮件已发送至 {to_email}{note}"
    except Exception as e:
        return f"邮件发送失败: {e}"


def _inline(text: str) -> str:
    """
    处理一行文本里的行内元素。

    关键点：先把整行做 HTML 转义，再拼 `<a>` / `<strong>` 标签。
    顺序不能反——否则简报正文里出现 <script>alert(1)</script> 或
    <img src=x onerror=...> 时会被邮件客户端当成真实标签执行。
    """
    escaped = html_lib.escape(text, quote=True)
    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
        escaped,
    )
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)


def _markdown_to_html(md: str) -> str:
    """极简 Markdown 转 HTML：标题、列表、引用、段落、链接、加粗。"""
    html_lines = []
    in_list = False

    for line in md.splitlines():
        stripped = line.strip()

        # 连续的 - / * 开头行要合并成一个 <ul>，不能每行单独 <li>
        if stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            html_lines.append(f"<li>{_inline(stripped[2:])}</li>")
            continue
        if in_list:
            html_lines.append("</ul>")
            in_list = False

        if stripped.startswith("### "):
            html_lines.append(f"<h3>{_inline(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            html_lines.append(f"<h2>{_inline(stripped[3:])}</h2>")
        elif stripped.startswith("# "):
            html_lines.append(f"<h1>{_inline(stripped[2:])}</h1>")
        elif stripped.startswith("> "):
            html_lines.append(f"<blockquote>{_inline(stripped[2:])}</blockquote>")
        elif not stripped:
            html_lines.append("")
        else:
            html_lines.append(f"<p>{_inline(stripped)}</p>")

    if in_list:
        html_lines.append("</ul>")

    body = "\n".join(html_lines)
    return (
        '<html><head><meta charset="utf-8"></head>'
        '<body style="font-family:-apple-system,Segoe UI,Microsoft YaHei,sans-serif;'
        'line-height:1.7;color:#222;">'
        f"{body}</body></html>"
    )
