# -*- coding: utf-8 -*-
"""T15 单用户认证测试：关闭态空操作 / 开启态会话守卫 / fail-fast。

设计要点（与 conftest 风格一致）：
- 认证开关/凭据/密钥全部在请求时从 src.config 模块常量惰性读取，故这里只
  monkeypatch config 常量，复用同一个 app 实例即可在"关闭/开启"间切换，
  无需重建 app / 重新 import。
- TestClient 用 httpx cookie 保持登录态；每个用例新建 client 并在结束时清 cookie，
  避免跨用例串味。
- DB 隔离由 conftest._isolated_repository autouse fixture 负责（临时 SQLite）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.api import app
from src import auth as auth_mod
from src import config


@pytest.fixture
def client():
    """每个用例独立 TestClient；结束清 cookie，避免登录态串味。"""
    c = TestClient(app)
    yield c
    c.cookies.clear()
    c.close()


@pytest.fixture
def enable_auth(monkeypatch):
    """开启认证并装配固定凭据/签名密钥（请求时惰性读取，即时生效）。"""
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    monkeypatch.setattr(config, "AUTH_USERNAME", "hr")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "s3cret-pass")
    monkeypatch.setattr(config, "AUTH_SECRET_KEY", "test-secret-key-please-change")


# ===========================================================================
# 关闭态（默认）：业务路由不拦截；auth 三端点空操作
# ===========================================================================
class TestDisabled:
    def test_health_public(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_me_disabled(self, client):
        r = client.get("/api/auth/me")
        assert r.status_code == 200
        assert r.json() == {
            "authenticated": True,
            "username": None,
            "auth_enabled": False,
        }

    def test_login_disabled_no_credential_check(self, client):
        # 任意 body（含错误/空凭据）都不校验，直接空操作成功
        r = client.post("/api/auth/login", json={"username": "nobody", "password": "wrong"})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "auth_enabled": False}
        # 关闭态不发会话 cookie
        assert "rs_session" not in r.headers.get("set-cookie", "")

    def test_logout_disabled(self, client):
        r = client.post("/api/auth/logout")
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    def test_business_routes_not_blocked(self, client):
        # 关闭态受保护业务路由直接 200（不要求 cookie）
        assert client.get("/api/dashboard").status_code == 200
        assert client.get("/api/resumes").status_code == 200
        assert client.get("/api/traces").status_code == 200


# ===========================================================================
# 开启态：会话守卫
# ===========================================================================
class TestEnabled:
    def test_business_route_requires_auth(self, client, enable_auth):
        r = client.get("/api/dashboard")
        assert r.status_code == 401
        assert r.json() == {"detail": "未认证"}

    def test_me_unauthenticated_still_200(self, client, enable_auth):
        r = client.get("/api/auth/me")
        assert r.status_code == 200
        assert r.json() == {
            "authenticated": False,
            "username": None,
            "auth_enabled": True,
        }

    def test_login_wrong_password_401(self, client, enable_auth):
        r = client.post("/api/auth/login", json={"username": "hr", "password": "bad"})
        assert r.status_code == 401
        assert r.json() == {"detail": "用户名或密码错误"}

    def test_login_wrong_username_401(self, client, enable_auth):
        r = client.post("/api/auth/login", json={"username": "root", "password": "s3cret-pass"})
        assert r.status_code == 401
        assert r.json() == {"detail": "用户名或密码错误"}

    def test_login_ok_sets_cookie(self, client, enable_auth):
        r = client.post("/api/auth/login", json={"username": "hr", "password": "s3cret-pass"})
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "username": "hr"}
        set_cookie = r.headers.get("set-cookie", "")
        assert "rs_session=" in set_cookie
        assert "HttpOnly" in set_cookie
        # httpx 已把 cookie 存入 client.cookies
        assert "rs_session" in client.cookies

    def test_authenticated_access_business(self, client, enable_auth):
        client.post("/api/auth/login", json={"username": "hr", "password": "s3cret-pass"})
        # 带 cookie 访问业务路由 → 200
        assert client.get("/api/dashboard").status_code == 200
        assert client.get("/api/resumes").status_code == 200

    def test_me_after_login(self, client, enable_auth):
        client.post("/api/auth/login", json={"username": "hr", "password": "s3cret-pass"})
        r = client.get("/api/auth/me")
        assert r.status_code == 200
        body = r.json()
        assert body["authenticated"] is True
        assert body["username"] == "hr"
        assert body["auth_enabled"] is True

    def test_logout_then_blocked(self, client, enable_auth):
        client.post("/api/auth/login", json={"username": "hr", "password": "s3cret-pass"})
        assert client.get("/api/dashboard").status_code == 200
        # 登出
        r = client.post("/api/auth/logout")
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        # 登出后再访问业务路由 → 401（cookie 被清）
        assert client.get("/api/dashboard").status_code == 401
        # me 回到未认证
        me = client.get("/api/auth/me").json()
        assert me["authenticated"] is False
        assert me["username"] is None

    def test_tampered_cookie_rejected(self, client, enable_auth):
        # 伪造一个 cookie 值 → 未认证
        client.cookies.set("rs_session", "forged.value.here")
        r = client.get("/api/dashboard")
        assert r.status_code == 401


# ===========================================================================
# fail-fast：开启态但缺凭据
# ===========================================================================
class TestFailFast:
    def test_enabled_without_credentials_raises(self, monkeypatch):
        monkeypatch.setattr(config, "AUTH_ENABLED", True)
        monkeypatch.setattr(config, "AUTH_USERNAME", "")
        monkeypatch.setattr(config, "AUTH_PASSWORD", "")
        with pytest.raises(RuntimeError, match="AUTH_ENABLED=1"):
            auth_mod.ensure_auth_configured()

    def test_enabled_with_credentials_ok(self, monkeypatch):
        monkeypatch.setattr(config, "AUTH_ENABLED", True)
        monkeypatch.setattr(config, "AUTH_USERNAME", "hr")
        monkeypatch.setattr(config, "AUTH_PASSWORD", "x")
        auth_mod.ensure_auth_configured()  # 不抛错

    def test_disabled_never_raises(self, monkeypatch):
        monkeypatch.setattr(config, "AUTH_ENABLED", False)
        monkeypatch.setattr(config, "AUTH_USERNAME", "")
        monkeypatch.setattr(config, "AUTH_PASSWORD", "")
        auth_mod.ensure_auth_configured()  # 关闭态不校验


# ===========================================================================
# 密码哈希 / 会话纯函数
# ===========================================================================
class TestCrypto:
    def test_verify_password_roundtrip(self):
        assert auth_mod.verify_password("hunter2", "hunter2") is True
        assert auth_mod.verify_password("hunter2", "hunter3") is False
        assert auth_mod.verify_password("", "hunter2") is False
        assert auth_mod.verify_password("hunter2", "") is False

    def test_session_roundtrip(self):
        tok = auth_mod.create_session("hr")
        assert auth_mod.read_session(tok) == "hr"
        assert auth_mod.read_session("garbage") is None
        assert auth_mod.read_session("") is None


# ===========================================================================
# org_id 数据模型预留：新库带列 + 老库幂等迁移
# ===========================================================================
class TestOrgIdReservation:
    def test_new_db_has_org_columns(self, _isolated_repository):
        repo = _isolated_repository
        for table in ("documents", "assessments", "runs", "batches"):
            cols = {r["name"] for r in repo._conn.execute(f"PRAGMA table_info({table})")}
            assert "org_id" in cols, f"{table} 缺 org_id 列"

    def test_org_id_default_local(self, _isolated_repository):
        repo = _isolated_repository
        repo.create_batch("b_org", "v1")
        row = repo._conn.execute("SELECT org_id FROM batches WHERE id='b_org'").fetchone()
        assert row["org_id"] == "local"

    def test_old_db_migrates_org_columns(self, tmp_path):
        """老库（无 org_id）经 PRAGMA 探测后 ALTER 补列，幂等。"""
        import sqlite3

        old_db = tmp_path / "old_org.db"
        c = sqlite3.connect(old_db)
        c.execute(
            "CREATE TABLE documents (id TEXT PRIMARY KEY, sha256 TEXT UNIQUE, "
            "storage_path TEXT NOT NULL, mime TEXT NOT NULL DEFAULT '', "
            "parse_status TEXT NOT NULL DEFAULT 'pending', "
            "parser_version TEXT NOT NULL DEFAULT '', "
            "pii_name TEXT NOT NULL DEFAULT '', pii_phone TEXT NOT NULL DEFAULT '', "
            "pii_email TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)"
        )
        c.execute(
            "CREATE TABLE batches (id TEXT PRIMARY KEY, job_version TEXT NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, "
            "frozen_at TEXT)"
        )
        from datetime import UTC, datetime

        c.execute(
            "INSERT INTO documents VALUES ('d1','sha1','/tmp/d.pdf','pdf','p','v','','','',?)",
            (datetime.now(UTC).isoformat(),),
        )
        c.commit()
        c.close()

        from src.repository import Repository

        repo = Repository(old_db)  # 触发迁移
        doc_cols = {r["name"] for r in repo._conn.execute("PRAGMA table_info(documents)")}
        batch_cols = {r["name"] for r in repo._conn.execute("PRAGMA table_info(batches)")}
        assert "org_id" in doc_cols
        assert "org_id" in batch_cols
        # 老行回填 'local'
        assert repo.get_document("d1")["org_id"] == "local"
        repo.close()
