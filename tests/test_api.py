"""
REST 接口的鉴权与故障呈现。

两条回归：
1. 数据库读失败必须返回 500 —— 以前 GET /api/preferences 把故障包装成
   「默认值」、GET /api/history 把故障包装成「空列表」，页面上看起来
   和「真的没有数据」完全一样，只有写操作才报错，排查时极具误导性。
2. 未匹配的 /api 路由必须返回 404 JSON，不能被前端回退路由吞成 200 HTML。
"""

import pytest
from fastapi.testclient import TestClient

import app as app_module


@pytest.fixture
def client():
    # 不进入 with 上下文：不触发 lifespan，测试进程里不会启动调度器
    return TestClient(app_module.app)


@pytest.fixture
def auth_enabled(monkeypatch):
    monkeypatch.setattr(app_module, "API_KEY", "secret-key")
    return "secret-key"


def test_health_does_not_require_auth(client):
    res = client.get("/api/health")

    assert res.status_code == 200
    assert res.json()["auth_required"] is False


def test_health_reports_auth_required_when_key_set(client, auth_enabled):
    assert client.get("/api/health").json()["auth_required"] is True


def test_api_open_when_no_key_configured(client):
    assert client.get("/api/preferences").status_code == 200
    assert client.get("/api/history").status_code == 200


def test_missing_key_is_rejected(client, auth_enabled):
    res = client.get("/api/preferences")

    assert res.status_code == 401
    assert "X-API-Key" in res.json()["detail"]


def test_wrong_key_is_rejected(client, auth_enabled):
    res = client.get("/api/preferences", headers={"X-API-Key": "wrong"})

    assert res.status_code == 401


def test_correct_key_is_accepted(client, auth_enabled):
    res = client.get("/api/preferences", headers={"X-API-Key": auth_enabled})

    assert res.status_code == 200


def test_write_endpoint_also_requires_key(client, auth_enabled):
    res = client.post("/api/preferences", json={"name": "x"})

    assert res.status_code == 401


def test_unknown_api_route_returns_json_404(client):
    res = client.get("/api/definitely-not-a-route")

    assert res.status_code == 404
    assert "detail" in res.json(), "不能被前端回退路由吞成 200 HTML"


def test_preferences_failure_returns_500(client, monkeypatch):
    monkeypatch.setattr(
        app_module, "load_preferences", lambda: {"error": "读取偏好失败: db down"}
    )

    res = client.get("/api/preferences")

    assert res.status_code == 500
    assert "db down" in res.json()["detail"]


def test_history_failure_returns_500(client, monkeypatch):
    def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(app_module, "load_history", boom)

    res = client.get("/api/history")

    assert res.status_code == 500
    assert "connection refused" in res.json()["detail"]


def test_history_ok_returns_list(client, monkeypatch):
    monkeypatch.setattr(app_module, "load_history", lambda: [{"id": "1", "summary": "摘要"}])

    res = client.get("/api/history")

    assert res.status_code == 200
    assert res.json()["history"][0]["summary"] == "摘要"


def test_preview_rejects_concurrency(client):
    """预览的单飞锁：已有预览在跑时返回 409，而不是再跑一遍完整检索。"""
    acquired = app_module._preview_lock.acquire(blocking=False)
    assert acquired
    try:
        res = client.post("/api/preview", json={"query": ""})
        assert res.status_code == 409
        assert "预览" in res.json()["detail"]
    finally:
        app_module._preview_lock.release()


def test_preview_releases_lock_after_failure(client, monkeypatch):
    monkeypatch.setattr(app_module, "load_preferences", lambda: {"error": "db down"})

    first = client.post("/api/preview", json={"query": ""})
    second = client.post("/api/preview", json={"query": ""})

    assert first.status_code == 500
    # 第二次仍然是 500 而不是 409，说明失败路径也把锁释放了
    assert second.status_code == 500


def test_preview_succeeds_and_releases_lock(client, monkeypatch):
    monkeypatch.setattr(app_module, "load_preferences", lambda: {"topics": ["AI"], "keywords": []})
    monkeypatch.setattr(app_module, "search_news", lambda query, max_results=5: [{"title": "t"}])
    monkeypatch.setattr(app_module, "generate_digest", lambda news, prefs: "# 简报")

    first = client.post("/api/preview", json={"query": ""})
    second = client.post("/api/preview", json={"query": ""})

    assert first.status_code == 200
    assert first.json()["digest"] == "# 简报"
    assert second.status_code == 200, "成功路径必须释放预览锁，否则第二次会被 409"


def test_generate_conflict_when_already_running(client):
    acquired = app_module._generation_lock.acquire(blocking=False)
    assert acquired
    try:
        res = client.post("/api/generate")
        assert res.status_code == 409
    finally:
        app_module._generation_lock.release()
