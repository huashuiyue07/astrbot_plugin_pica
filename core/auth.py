"""
Pica 账号/token 管理（按用户隔离）

- 每个 QQ 用户可通过 /picalogin 绑定自己的哔咔账号，token 单独持久化
  （data_dir/tokens/{user_id}.json），互不干扰、不会共用他人账号。
- 未绑定用户：allow_default_account=True 时使用配置的默认账号兜底；
  为 False 时提示先绑定，避免群成员全走管理员的个人账号。
- token 失效（JWT 过期 / 服务器端失效）自动重新登录：
  - 用户自绑账号：绑定/登录时把 email/password 一并持久化，用于重登
  - 默认账号兜底：直接用配置里的默认账号密码重登
"""

import base64
import json
import time
from pathlib import Path

from astrbot.api import logger

from .client import PicaError


class PicaAuthManager:
    """哔咔登录状态管理器（按用户隔离）"""

    def __init__(
        self,
        client,
        data_dir: Path,
        default_email: str = "",
        default_password: str = "",
        allow_default_account: bool = True,
    ):
        self.client = client
        self.data_dir = data_dir
        self.tokens_dir = data_dir / "tokens"
        self.tokens_dir.mkdir(parents=True, exist_ok=True)
        self.default_email = default_email
        self.default_password = default_password
        self.allow_default_account = allow_default_account

        # 内存缓存: str(file_path) -> (token, expire_at)
        self._cache: dict[str, tuple[str, float]] = {}
        # 默认账号 token 文件（全局一个）
        self._default_token_file = data_dir / "token.json"

    # ---------- 内部 ----------

    def _token_path(self, user_id: str) -> Path:
        return self.tokens_dir / f"{user_id}.json"

    def _read_token(self, path: Path) -> tuple[str, float] | None:
        """读取 token 文件，返回 (token, expire_at) 或 None"""
        data = self._load_token_file(path)
        if not data:
            return None
        token = data.get("token")
        expire_at = data.get("expire_at", 0)
        if token and expire_at > time.time():
            return token, expire_at
        return None

    def _load_token_file(self, path: Path) -> dict | None:
        """读取 token 文件原始内容（含 email/password），失败返回 None"""
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.debug(f"读取 token 文件失败 {path.name}: {e}")
        return None

    def _read_creds(self, path: Path) -> dict | None:
        """读取绑定账号的凭证（email/password），未存储返回 None"""
        data = self._load_token_file(path)
        if not data:
            return None
        email = data.get("email")
        password = data.get("password")
        if email and password:
            return {"email": email, "password": password}
        return None

    def _save_token(
        self,
        path: Path,
        token: str,
        email: str = None,
        password: str = None,
    ) -> None:
        expire_at = self._parse_expire(token)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"token": token, "expire_at": expire_at}
            if email:
                payload["email"] = email
            if password:
                payload["password"] = password
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
        except Exception as e:
            logger.warning(f"保存 token 文件失败 {path.name}: {e}")
        self._cache[str(path)] = (token, expire_at)

    @staticmethod
    def _parse_expire(token: str) -> float:
        """从 JWT 解析过期时间，失败则给 7 天兜底"""
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload))
            exp = data.get("exp")
            if exp:
                return float(exp)
        except Exception:
            pass
        return time.time() + 7 * 86400

    # ---------- 用户绑定 ----------

    def is_bound(self, user_id: str) -> bool:
        """用户是否已绑定自己的账号"""
        return self._get_valid_token(user_id) is not None

    def _get_valid_token(self, user_id: str) -> str | None:
        """获取用户已绑定且未过期的 token"""
        cache_key = str(self._token_path(user_id))
        cached = self._cache.get(cache_key)
        if cached and cached[1] > time.time():
            return cached[0]
        found = self._read_token(self._token_path(user_id))
        if found:
            self._cache[cache_key] = found
            return found[0]
        return None

    async def login(self, user_id: str, email: str, password: str) -> str:
        """当前用户绑定自己的哔咔账号，返回 token"""
        if not email or not password:
            raise PicaError("请提供哔咔账号密码：/picalogin <邮箱> <密码>")
        token = await self.client.login(email, password)
        # 一并持久化凭证，保证 token 失效时可自动重登
        self._save_token(
            self._token_path(user_id), token, email=email, password=password
        )
        return token

    async def bind_default(self, user_id: str) -> str:
        """将配置的默认账号绑定给当前用户（管理员自用）。"""
        if not self.default_email or not self.default_password:
            raise PicaError("插件配置中未填写默认账号")
        token = await self.client.login(self.default_email, self.default_password)
        # 不存用户级凭证：重登时统一走默认账号分支即可
        self._save_token(self._token_path(user_id), token)
        return token

    def logout(self, user_id: str) -> None:
        """当前用户解绑自己的账号"""
        path = self._token_path(user_id)
        try:
            if path.exists():
                path.unlink()
        except Exception as e:
            logger.debug(f"删除 token 文件失败: {e}")
        self._cache.pop(str(path), None)

    def status(self, user_id: str) -> dict:
        """当前用户登录状态"""
        bound_token = self._get_valid_token(user_id)
        if bound_token:
            creds = self._read_creds(self._token_path(user_id))
            return {
                "bound": True,
                "source": "self",
                "email": (creds or {}).get("email"),
                "expire": self._parse_expire(bound_token),
            }
        # 检查默认账号是否可用
        if self.allow_default_account and self.default_email:
            found = self._read_token(self._default_token_file)
            if found:
                return {
                    "bound": False,
                    "source": "default",
                    "email": self.default_email,
                    "expire": found[1],
                }
        return {"bound": False, "source": None, "email": None, "expire": None}

    # ---------- 取 token ----------

    async def ensure_login(self, user_id: str) -> str:
        """
        确保有可用 token（当前用户绑定 > 默认账号兜底），未绑定且未开兜底则抛错
        """
        # 1. 用户自己绑定的账号
        bound = self._get_valid_token(user_id)
        if bound:
            return bound

        # 2. 默认账号兜底
        if self.allow_default_account and self.default_email and self.default_password:
            default_token = self._read_token(self._default_token_file)
            if default_token:
                return default_token[0]
            # 登录默认账号并持久化
            token = await self.client.login(self.default_email, self.default_password)
            self._save_token(self._default_token_file, token)
            return token

        # 3. 无可用账号
        raise PicaError(
            "你的 QQ 还未绑定哔咔账号，为避免共用他人账号，请先发送 "
            "「/picalogin <邮箱> <密码>」绑定自己的哔咔账号"
        )

    async def force_relogin(self, user_id: str) -> str:
        """
        强制重新登录（用于 token 服务器端失效但 JWT 未过期）。
        清除本地 token 缓存后用绑定的账号密码重新登录。
        仅适用于 /picalogin 绑定过的用户或默认账号兜底。
        """
        # 1. 用户自绑账号：用绑定/登录时持久化的 email/password 重登
        if user_id and user_id != "__default__":
            path = self._token_path(user_id)
            creds = self._read_creds(path)
            self.logout(user_id)
            if creds:
                token = await self.client.login(
                    creds["email"], creds["password"]
                )
                self._save_token(path, token, email=creds["email"], password=creds["password"])
                logger.info(f"用户 {user_id} 的哔咔 token 失效，已自动重新登录")
                return token

        # 2. 默认账号
        self.logout_default()
        if self.default_email and self.default_password:
            token = await self.client.login(self.default_email, self.default_password)
            self._save_token(self._default_token_file, token)
            logger.info("默认哔咔账号 token 失效，已自动重新登录")
            return token

        raise PicaError("token 失效且未保存账号密码，请重新 /picalogin 绑定")

    def logout_default(self) -> None:
        """清除默认账号 token"""
        try:
            if self._default_token_file.exists():
                self._default_token_file.unlink()
        except Exception as e:
            logger.debug(f"删除默认 token 失败: {e}")
        self._cache.pop(str(self._default_token_file), None)