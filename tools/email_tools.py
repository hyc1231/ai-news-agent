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

from dotenv import load_dotenv

load_dotenv()

SMTP_SERVER = os.getenv("SMTP_SERVER", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USERNAME)


def send_email(subject: str, content: str, to_email: str) -> str:
    """
    发送一封邮件。
    参数:
        subject: 邮件主题
        content: 邮件正文（Markdown 格式，会转成 HTML 发送）
        to_email: 收件人邮箱
    返回:
        发送结果描述
    """
    if not all([SMTP_SERVER, SMTP_USERNAME, SMTP_PASSWORD, to_email]):
        return "邮件发送失败：缺少 SMTP 配置或收件人邮箱"

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
        return f"邮件已发送至 {to_email}"
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
