"""auth 登录态管理测试（使用假 client，不触网）"""

import asyncio
import base64
import json
import time
from pathlib import Path

from core.auth import PicaAuthManager


def _jwt(exp: float) -> str:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode())
        .decode()
        .rstrip("=")
    )
    return f"h.{payload}.s"


class FakeClient:
    """只记录 login 调用，返回带未来过期时间的 token"""

    def __init__(self):
        self.calls = 0
        self.last_email = None
        self.last_password = None

    async def login(self, email, password):
        self.calls += 1
        self.last_email = email
        self.last_password = password
        return _jwt(time.time() + 3600)


def _manager(tmp_path) -> tuple[PicaAuthManager, FakeClient]:
    client = FakeClient()
    am = PicaAuthManager(
        client,
        Path(tmp_path),
        default_email="admin@pica.com",
        default_password="adminpw",
        allow_default_account=True,
    )
    return am, client


def test_parse_expire(tmp_path):
    am, _ = _manager(tmp_path)
    assert am._parse_expire(_jwt(1700000000)) == 1700000000.0


def test_login_saves_creds(tmp_path):
    am, _ = _manager(tmp_path)
    asyncio.run(am.login("u1", "a@b.c", "pw1"))
    creds = am._read_creds(am._token_path("u1"))
    assert creds == {"email": "a@b.c", "password": "pw1"}


def test_force_relogin_uses_stored_creds(tmp_path):
    am, client = _manager(tmp_path)
    asyncio.run(am.login("u1", "a@b.c", "pw1"))
    asyncio.run(am.force_relogin("u1"))
    assert client.calls == 2
    assert client.last_email == "a@b.c"
    assert client.last_password == "pw1"
    # 重登后 token 文件恢复可用
    assert am._get_valid_token("u1") is not None


def test_force_relogin_default_account(tmp_path):
    am, client = _manager(tmp_path)
    asyncio.run(am.force_relogin("__default__"))
    assert client.calls == 1
    assert client.last_email == "admin@pica.com"


def test_logout_removes_file(tmp_path):
    am, _ = _manager(tmp_path)
    asyncio.run(am.login("u1", "a@b.c", "pw1"))
    am.logout("u1")
    assert not am._token_path("u1").exists()
    assert am._get_valid_token("u1") is None