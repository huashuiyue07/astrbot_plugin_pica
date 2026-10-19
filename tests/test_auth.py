"""auth 登录态管理测试（使用假 client，不触网）"""

import asyncio
import base64
import json
import stat
import sys
import time
from pathlib import Path

import pytest

from core.auth import PicaAuthManager
from core.client import PicaError


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


def _manager(tmp_path, store_password=False) -> tuple[PicaAuthManager, FakeClient]:
    client = FakeClient()
    am = PicaAuthManager(
        client,
        Path(tmp_path),
        default_email="admin@pica.com",
        default_password="adminpw",
        allow_default_account=True,
        store_password=store_password,
    )
    return am, client


def test_parse_expire(tmp_path):
    am, _ = _manager(tmp_path)
    assert am._parse_expire(_jwt(1700000000)) == 1700000000.0


def test_login_saves_creds_when_enabled(tmp_path):
    """store_password=True 时才保存密码，用于 token 过期后自动重登"""
    am, _ = _manager(tmp_path, store_password=True)
    asyncio.run(am.login("u1", "a@b.c", "pw1"))
    creds = am._read_creds(am._token_path("u1"))
    assert creds == {"email": "a@b.c", "password": "pw1"}


def test_login_does_not_store_password_by_default(tmp_path):
    """默认只落盘 token + 邮箱，不保存密码"""
    am, _ = _manager(tmp_path)
    asyncio.run(am.login("u1", "a@b.c", "pw1"))

    data = json.loads(am._token_path("u1").read_text())
    assert data["token"]
    assert data.get("email") == "a@b.c"
    assert "password" not in data
    assert am._read_creds(am._token_path("u1")) is None


def test_force_relogin_without_stored_password_raises(tmp_path):
    am, client = _manager(tmp_path)
    asyncio.run(am.login("u1", "a@b.c", "pw1"))

    with pytest.raises(PicaError):
        asyncio.run(am.force_relogin("u1"))
    assert client.calls == 1  # 只有首次登录


def test_force_relogin_bound_default_still_falls_back(tmp_path):
    """用 /picalogin（无参数）绑的默认账号没有 email：仍可回落到默认账号重登"""
    am, client = _manager(tmp_path)
    asyncio.run(am.bind_default("u2"))

    asyncio.run(am.force_relogin("u2"))

    assert client.calls == 2
    assert client.last_email == "admin@pica.com"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 上 chmod 语义不同")
def test_token_files_are_private(tmp_path):
    """token 文件 600、tokens 目录 700"""
    am, _ = _manager(tmp_path)
    asyncio.run(am.login("u1", "a@b.c", "pw1"))

    assert stat.S_IMODE(am._token_path("u1").stat().st_mode) == 0o600
    assert stat.S_IMODE(am.tokens_dir.stat().st_mode) == 0o700


def _write_legacy_token(tmp_path, password="pw1"):
    tokens_dir = Path(tmp_path) / "tokens"
    tokens_dir.mkdir(parents=True, exist_ok=True)
    path = tokens_dir / "u9.json"
    path.write_text(
        json.dumps(
            {
                "token": _jwt(time.time() + 3600),
                "expire_at": time.time() + 3600,
                "email": "old@b.c",
                "password": password,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_legacy_password_is_scrubbed_when_disabled(tmp_path):
    """默认不存密码：启动时清理历史文件里的明文密码，但保留 token 与邮箱"""
    path = _write_legacy_token(tmp_path)

    am, _ = _manager(tmp_path)

    data = json.loads(path.read_text())
    assert "password" not in data
    assert data.get("email") == "old@b.c"
    assert am._get_valid_token("u9") is not None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_legacy_password_is_kept_when_enabled(tmp_path):
    path = _write_legacy_token(tmp_path)

    _manager(tmp_path, store_password=True)

    assert json.loads(path.read_text())["password"] == "pw1"


def test_force_relogin_uses_stored_creds(tmp_path):
    am, client = _manager(tmp_path, store_password=True)
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