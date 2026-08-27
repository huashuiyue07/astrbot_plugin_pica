"""
图片下载模块

pica 的图片资源（封面、章节图）需要带 authorization 请求头才能访问，
所以必须自行下载到本地再通过 AstrBot 发送。支持并发下载与 MD5 修改
（修改图片字节防止被平台风控检测）。
"""

import asyncio
import random
import re
from pathlib import Path

from astrbot.api import logger

from .client import PicaError

# 文件名非法字符（跨平台）
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')


def _safe_name(name: str, max_len: int = 60) -> str:
    """清洗文件名"""
    name = _ILLEGAL.sub("-", name).strip()
    return name[:max_len] or "unknown"


def _modify_md5(data: bytes) -> bytes:
    """在文件末尾追加随机字节修改 MD5（JPEG/PNG 解码时忽略尾部数据）"""
    return data + bytes([random.randint(0, 255) & 0x7F])


def build_image_url(file_server: str, path: str) -> str:
    """拼接 pica 图片 URL"""
    if path.startswith("http"):
        return path
    return f"{file_server}/static/{path}"


def pick_image_path(media: dict) -> list:
    """
    从章节图片 media 中取出候选 path 列表。
    新版 API 返回 files 数组（files[0] 原图），旧版返回 path。
    """
    candidates = []
    files = media.get("files") or []
    for f in files:
        if isinstance(f, str) and f:
            candidates.append(f)
    if media.get("path") and media["path"] not in candidates:
        candidates.append(media["path"])
    return candidates


class PicaDownloader:
    """下载管理器"""

    def __init__(self, client, data_dir: Path, modify_md5: bool = True):
        self.client = client
        self.data_dir = data_dir
        self.modify_md5 = modify_md5

    def cache_dir(self) -> Path:
        d = self.data_dir / "cache"
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def download_cover(self, comic: dict, token: str = None) -> Path | None:
        """下载封面图，返回本地路径；失败返回 None"""
        try:
            thumb = comic.get("thumb") or {}
            fs = thumb.get("fileServer")
            path = thumb.get("path")
            if not fs or not path:
                return None
            url = build_image_url(fs, path)
            comic_id = str(comic.get("_id") or "unknown")
            save_path = self.cache_dir() / f"cover_{comic_id}.jpg"
            if save_path.exists():
                return save_path

            data = await self.client._download(url, token)
            if self.modify_md5:
                data = _modify_md5(data)
            save_path.write_bytes(data)
            return save_path
        except Exception as e:
            logger.debug(f"封面下载失败: {e}")
            return None

    async def download_episode(
        self,
        comic_id: str,
        ep_order: int,
        token: str = None,
        max_concurrent: int = 5,
        target_dir: Path = None,
    ) -> list[Path]:
        """
        下载整个章节的图片，返回本地文件列表。
        先获取章节图片分页信息，再并发下载。

        Args:
            target_dir: 指定下载目录（章节保存在 {target_dir}/ep{order}/ 下），
                缺省用缓存目录。
        """
        # 获取第一页，得到总页数
        pages_info = await self.client.pages(comic_id, ep_order, 1, token)
        total_pages = pages_info.get("pages", 1)
        all_docs = list(pages_info.get("docs", []))

        for p in range(2, total_pages + 1):
            try:
                pi = await self.client.pages(comic_id, ep_order, p, token)
                all_docs.extend(pi.get("docs", []))
            except PicaError as e:
                logger.warning(f"获取第 {p} 页图片列表失败: {e}")

        # 解析每张图的 URL 与文件名
        items = []
        for idx, doc in enumerate(all_docs, 1):
            media = doc.get("media") or {}
            paths = pick_image_path(media)
            if not paths:
                continue
            fs = media.get("fileServer")
            og_name = media.get("originalName") or f"{idx:03d}.jpg"
            items.append((fs, paths, og_name, idx))

        sem = asyncio.Semaphore(max_concurrent)

        async def fetch(fs, paths, og_name, idx):
            async with sem:
                last_err = None
                for p in paths:
                    url = build_image_url(fs, p)
                    try:
                        data = await self.client._download(url, token, timeout=60)
                        if self.modify_md5:
                            data = _modify_md5(data)
                        suffix = Path(og_name).suffix or ".jpg"
                        safe = _safe_name(f"{idx:03d}_{Path(og_name).stem}{suffix}")
                        return safe, data
                    except Exception as e:
                        last_err = e
                        continue
                raise last_err or PicaError(f"第{idx}张图片下载失败")

        results = await asyncio.gather(
            *(fetch(fs, paths, og, i) for fs, paths, og, i in items),
            return_exceptions=True,
        )

        if target_dir is not None:
            ep_dir = target_dir / f"ep{ep_order}"
        else:
            ep_dir = self.cache_dir() / f"{comic_id}_ep{ep_order}"
        ep_dir.mkdir(parents=True, exist_ok=True)
        saved = []
        for idx, res in enumerate(results, 1):
            if isinstance(res, Exception):
                logger.warning(f"图片 {idx} 下载失败: {res}")
                continue
            name, data = res
            p = ep_dir / name
            p.write_bytes(data)
            saved.append(p)
        return saved

    async def download_all(
        self,
        comic_id: str,
        episodes: list[dict],
        token: str = None,
        max_concurrent: int = 5,
        progress_cb=None,
    ) -> Path:
        """
        整本下载：逐章节下载到 {target}/ep{order}/ 子目录。

        Args:
            episodes: 章节列表（含 order / title）
            progress_cb: 进度回调 (done_eps, total_eps, ep_order) -> None

        Returns:
            整本下载根目录（含全部章节子目录）
        """
        root_dir = self.cache_dir() / f"{comic_id}_all"
        total = len(episodes)
        for i, ep in enumerate(episodes, 1):
            order = ep.get("order")
            if order is None:
                continue
            try:
                await self.download_episode(
                    comic_id,
                    order,
                    token=token,
                    max_concurrent=max_concurrent,
                    target_dir=root_dir,
                )
            except Exception as e:
                logger.warning(f"章节 {order} 下载失败: {e}")
            if progress_cb:
                try:
                    await progress_cb(i, total, order)
                except Exception:
                    pass
        return root_dir

    def cleanup_cache(
        self,
        max_age_days: int = 7,
        max_size_mb: int = 0,
        include_packs: bool = True,
    ) -> dict:
        """
        清理下载缓存（同步执行，建议放线程池调用）。

        策略：
        1. 超过 max_age_days 天的文件直接删除（0 或负数表示不按时间清理）
        2. 缓存总大小超过 max_size_mb 时，按修改时间从旧到新删除
           直到低于上限（0 或负数表示不按大小清理）
        3. 清理所有空目录

        Args:
            max_age_days: 保留天数
            max_size_mb: 大小上限(MB)
            include_packs: 是否同时清理打包产物目录 packs/

        Returns:
            {"deleted_files": 删除文件数, "freed_mb": 释放空间(MB), "current_mb": 剩余(MB)}
        """
        import time

        now = time.time()
        targets = [self.cache_dir()]
        if include_packs:
            targets.append(self.data_dir / "packs")

        def _total_size(dirs) -> int:
            total = 0
            for d in dirs:
                if d.exists():
                    for f in d.rglob("*"):
                        if f.is_file():
                            try:
                                total += f.stat().st_size
                            except OSError:
                                pass
            return total

        deleted = 0
        freed = 0

        # 1. 按时间清理
        if max_age_days and max_age_days > 0:
            cutoff = now - max_age_days * 86400
            for d in targets:
                if not d.exists():
                    continue
                for f in d.rglob("*"):
                    if not f.is_file():
                        continue
                    try:
                        if f.stat().st_mtime < cutoff:
                            size = f.stat().st_size
                            f.unlink()
                            deleted += 1
                            freed += size
                    except OSError:
                        pass

        # 2. 按大小清理（最旧优先）
        if max_size_mb and max_size_mb > 0:
            limit = max_size_mb * 1024 * 1024
            for d in targets:
                if not d.exists():
                    continue
                files = [f for f in d.rglob("*") if f.is_file()]
                if not files:
                    continue
                total = sum(f.stat().st_size for f in files if f.stat().st_size)
                if total <= limit:
                    continue
                files.sort(key=lambda p: p.stat().st_mtime)
                for f in files:
                    if total <= limit:
                        break
                    try:
                        size = f.stat().st_size
                        f.unlink()
                        total -= size
                        deleted += 1
                        freed += size
                    except OSError:
                        pass

        # 3. 清理空目录
        for d in targets:
            if not d.exists():
                continue
            for sub in sorted(
                d.rglob("*"), key=lambda p: len(str(p)), reverse=True
            ):
                if sub.is_dir():
                    try:
                        sub.rmdir()
                    except OSError:
                        pass

        return {
            "deleted_files": deleted,
            "freed_mb": round(freed / 1024 / 1024, 2),
            "current_mb": round(_total_size(targets) / 1024 / 1024, 2),
        }

