"""
邮件发送工具。使用 SMTP 发送每日简报。
"""

import os
import smtplib
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

    # 简单把 Markdown 转成 HTML：标题加粗、换行转 <br>
    html_content = _markdown_to_html(content)

    msg = MIMEText(html_content, "html", "utf-8")
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


def _markdown_to_html(md: str) -> str:
    """极简 Markdown 转 HTML，仅处理标题和换行。"""
    lines = md.splitlines()
    html_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            html_lines.append(f"<h1>{stripped[2:]}</h1>")
        elif stripped.startswith("## "):
            html_lines.append(f"<h2>{stripped[3:]}</h2>")
        elif stripped.startswith("### "):
            html_lines.append(f"<h3>{stripped[4:]}</h3>")
        elif stripped.startswith("- "):
            html_lines.append(f"<li>{stripped[2:]}</li>")
        else:
            html_lines.append(f"<p>{stripped}</p>")
    return "\n".join(html_lines)
