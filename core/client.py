"""
Pica API 客户端

实现哔咔官方 App 的请求签名（HMAC-SHA256）、登录、搜索、详情、章节、
排行榜、收藏、签到等全部常用接口，带自动重试与统一异常处理。
"""

import asyncio
import hashlib
import hmac
import json
import time

import aiohttp

from astrbot.api import logger

from .constants import (
    ACCEPT_JSON,
    API_KEY,
    APP_BUILD_VERSION,
    APP_CHANNEL,
    APP_PLATFORM,
    APP_UUID,
    APP_VERSION,
    BASE_URL,
    NONCE,
    SECRET_KEY,
    USER_AGENT,
)


class PicaError(Exception):
    """Pica 请求错误"""

    def __init__(self, message: str, code: int = None):
        super().__init__(message)
        self.code = code


class PicaAuthError(PicaError):
    """认证失败（token 失效 / 账号密码错误）"""


class PicaClient:
    """哔咔 API 客户端"""

    def __init__(
        self,
        use_proxy: bool = False,
        proxy_url: str = "",
        max_retry: int = 3,
        timeout: int = 30,
        on_token_invalid=None,
    ):
        self.use_proxy = use_proxy
        self.proxy_url = proxy_url
        self.max_retry = max_retry
        self.timeout = timeout
        # 当服务器返回 401/1005 时调用此回调，期望返回新 token（异步）。
        # main.py 中由 PicaAuthManager.force_relogin 实现，
        # 自动用绑定的账号密码重新登录。
        self.on_token_invalid = on_token_invalid
        # 当前请求的 user_id（main.py 在 ensure_login 后设置，供 callback 使用）
        self._current_user_id: str | None = None

    # ---------- 签名 ----------

    def _sign(self, method: str, path: str, ts: str) -> str:
        """计算 HMAC-SHA256 签名"""
        raw = f"{path}{ts}{NONCE}{method}{API_KEY}".lower()
        hc = hmac.new(SECRET_KEY.encode(), raw.encode(), hashlib.sha256)
        return hc.hexdigest()

    def _headers(self, method: str, path: str, token: str = None) -> dict:
        ts = str(int(time.time()))
        headers = {
            "api-key": API_KEY,
            "accept": ACCEPT_JSON,
            "app-channel": APP_CHANNEL,
            "time": ts,
            "nonce": NONCE,
            "signature": self._sign(method, path, ts),
            "app-version": APP_VERSION,
            "app-uuid": APP_UUID,
            "app-platform": APP_PLATFORM,
            "app-build-version": APP_BUILD_VERSION,
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": USER_AGENT,
            "image-quality": "original",
        }
        if token:
            headers["authorization"] = token
        return headers

    # ---------- 基础请求 ----------

    async def _request(
        self,
        method: str,
        path: str,
        data: dict = None,
        token: str = None,
        _retry: int = 0,
    ) -> dict:
        """发起 JSON API 请求，带重试"""
        url = BASE_URL + path
        # 注意：签名必须使用去掉前导斜杠的路径，否则 pica 返回假的
        # {"code":200,"message":"success"}（无 data）空响应
        headers = self._headers(method, path.lstrip("/"), token)
        body = json.dumps(data).encode() if data else None

        timeout = aiohttp.ClientTimeout(total=self.timeout)
        kwargs = dict(timeout=timeout, headers=headers)
        if self.use_proxy and self.proxy_url:
            kwargs["proxy"] = self.proxy_url

        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, data=body, **kwargs) as resp:
                text = await resp.text()
                try:
                    result = json.loads(text)
                except json.JSONDecodeError:
                    raise PicaError(f"响应解析失败 (HTTP {resp.status})", resp.status)

        code = result.get("code", -1)
        if code != 200:
            msg = result.get("message", "未知错误")
            error = result.get("error", "")
            # 认证失败：token 失效/错误，自动重新登录一次
            if error in ("1005", "1008") or resp.status == 401:
                if self.on_token_invalid and _retry == 0:
                    try:
                        new_token = await self.on_token_invalid(self._current_user_id)
                        if new_token:
                            return await self._request(
                                method, path, data, new_token, _retry=_retry + 1
                            )
                    except Exception:
                        pass
                raise PicaAuthError(f"认证失败: {msg} ({error})", code)
            # 触发风控/被封禁
            if resp.status == 400 and "banned" in str(result).lower():
                raise PicaError(f"内容已被平台封禁: {msg}", code)
            # 网络类错误重试
            if resp.status >= 500 and _retry < self.max_retry:
                await asyncio.sleep(1 + _retry)
                return await self._request(
                    method, path, data, token, _retry=_retry + 1
                )
            raise PicaError(f"请求失败: {msg} ({error})", code)

        return result

    async def _download(
        self, url: str, token: str = None, timeout: int = 60, _retry: int = 0
    ) -> bytes:
        """下载图片等二进制资源（需带 authorization）"""
        headers = {"User-Agent": USER_AGENT, "accept": "image/*"}
        if token:
            headers["authorization"] = token
        timeout_obj = aiohttp.ClientTimeout(total=timeout)
        kwargs = dict(timeout=timeout_obj, headers=headers)
        if self.use_proxy and self.proxy_url:
            kwargs["proxy"] = self.proxy_url

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, **kwargs) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    if resp.status == 401:
                        raise PicaAuthError("图片下载认证失败，token 可能已失效")
                    if resp.status >= 500 and _retry < self.max_retry:
                        await asyncio.sleep(1 + _retry)
                        return await self._download(
                            url, token, timeout, _retry=_retry + 1
                        )
                    raise PicaError(f"图片下载失败 (HTTP {resp.status})", resp.status)
        except asyncio.TimeoutError:
            if _retry < self.max_retry:
                await asyncio.sleep(1 + _retry)
                return await self._download(url, token, timeout, _retry=_retry + 1)
            raise PicaError("图片下载超时")

    # ---------- 认证 ----------

    async def login(self, email: str, password: str) -> str:
        """登录，返回 token"""
        if not email or not password:
            raise PicaError("请先在插件配置中填写哔咔账号密码")
        result = await self._request(
            "POST", "/auth/sign-in", data={"email": email, "password": password}
        )
        token = result.get("data", {}).get("token")
        if not token:
            raise PicaError("登录失败: 响应中没有 token")
        logger.info("Pica 登录成功")
        return token

    # ---------- 浏览/搜索 ----------

    async def categories(self) -> list:
        """获取全部分区"""
        result = await self._request("GET", "/categories")
        return result.get("data", {}).get("categories", [])

    async def comics(
        self, block: str = "", tag: str = "", order: str = "ua", page: int = 1,
        token: str = None,
    ) -> dict:
        """按分区/标签浏览漫画。

        注意：中文参数必须 URL 编码（签名使用编码后的 path），
        否则 pica 返回假的 {"code":200} 空响应。
        """
        from urllib.parse import urlencode

        params = []
        if block:
            params.append(("c", block))
        if tag:
            params.append(("t", tag))
        if order:
            params.append(("s", order))
        params.append(("page", str(page)))
        qs = urlencode(params)
        result = await self._request("GET", f"/comics?{qs}", token=token)
        return result.get("data", {}).get("comics", {})

    async def search(
        self, keyword: str, sort: str = "ua", page: int = 1, token: str = None
    ) -> dict:
        """高级搜索"""
        result = await self._request(
            "POST",
            f"/comics/advanced-search?page={page}",
            data={"categories": [], "keyword": keyword, "sort": sort},
            token=token,
        )
        return result.get("data", {}).get("comics", {})

    async def leaderboard(self, tt: str = "H24", token: str = None) -> list:
        """排行榜"""
        result = await self._request("GET", f"/comics/leaderboard?ct=VC&tt={tt}", token=token)
        return result.get("data", {}).get("comics", [])

    async def comic_info(self, comic_id: str, token: str = None) -> dict:
        """漫画详情"""
        result = await self._request("GET", f"/comics/{comic_id}", token=token)
        return result.get("data", {}).get("comic", {})

    async def episodes(self, comic_id: str, page: int = 1, token: str = None) -> dict:
        """章节列表"""
        result = await self._request("GET", f"/comics/{comic_id}/eps?page={page}", token=token)
        return result.get("data", {}).get("eps", {})

    async def episodes_all(self, comic_id: str, token: str = None) -> list[dict]:
        """拉取全部章节（自动翻页）"""
        all_docs = []
        page = 1
        while True:
            eps = await self.episodes(comic_id, page, token)
            docs = eps.get("docs", [])
            all_docs.extend(docs)
            total = eps.get("total", 0)
            pages = eps.get("pages", 1)
            if page >= pages or page >= (total // 40 + 1) or not docs:
                break
            page += 1
        return all_docs

    async def pages(self, comic_id: str, ep_order: int, page: int = 1, token: str = None) -> dict:
        """章节图片页"""
        result = await self._request(
            "GET", f"/comics/{comic_id}/order/{ep_order}/pages?page={page}", token=token
        )
        return result.get("data", {}).get("pages", {})

    async def recommendations(self, comic_id: str, token: str = None) -> list:
        """相关推荐"""
        result = await self._request("GET", f"/comics/{comic_id}/recommendation", token=token)
        return result.get("data", {}).get("comics", [])

    async def comments(self, comic_id: str, page: int = 1, token: str = None) -> dict:
        """评论"""
        result = await self._request("GET", f"/comics/{comic_id}/comments?page={page}", token=token)
        return result.get("data", {}).get("comments", {})

    # ---------- 用户操作 ----------

    async def favourite(self, comic_id: str, token: str = None) -> bool:
        """收藏/取消收藏，返回操作后是否已收藏"""
        result = await self._request("POST", f"/comics/{comic_id}/favourite", token=token)
        return result.get("data", {}).get("favourite", False)

    async def my_favourite(self, page: int = 1, sort: str = "ua", token: str = None) -> dict:
        """我的收藏"""
        result = await self._request(
            "GET", f"/users/favourite?s={sort}&page={page}", token=token
        )
        return result.get("data", {}).get("comics", {})

    async def like(self, comic_id: str, token: str = None) -> None:
        """爱心（喜欢）"""
        await self._request("POST", f"/comics/{comic_id}/like", token=token)

    async def punch_in(self, token: str = None) -> dict:
        """每日签到"""
        result = await self._request("GET", "/users/punch-in", token=token)
        return result.get("data", {})
