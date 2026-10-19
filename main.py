"""
Pica-Comics - AstrBot 哔咔漫画插件

搜索、查看、下载哔咔漫画（picacomic）本子，支持排行榜、收藏、每日签到。
- 支持整本/单章节下载，打包格式 zip/pdf/长图
- 账号按用户隔离：每个 QQ 用户可绑定自己的哔咔账号，互不共用
"""

import asyncio
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .core import (
    CATEGORIES,
    RANK_TYPES,
    MessageFormatter,
    PicaAuthManager,
    PicaClient,
    PicaDownloader,
    PicaError,
    PicaPacker,
    RankDiff,
    RankHistoryStore,
    RankSubscriptionStore,
    cron_matches,
    validate_cron,
)

PLUGIN_NAME = "astrbot_plugin_pica"

# 单次最多发送图片数（仅 images 模式）
MAX_SEND_IMAGES = 10

# 整本下载打包目录
PACKS_DIR = "packs"

# 发送前需要「上传本地文件到 QQ 服务器」的组件类型。
# 这类消息的发送耗时随文件体积增长，客户端等待超时**不代表消息未送达**
# （napcat 会把文件传完再投递），因此绝不能按「失败」重发，否则用户收到两份。
UPLOAD_COMPONENT_TYPES: tuple[type, ...] = (
    Comp.File,
    Comp.Image,
    Comp.Video,
    Comp.Record,
)

# 发送上传类消息前，把 OneBot API 调用超时抬到不低于该秒数。
# AstrBot 的 aiocqhttp 适配器把该值硬编码为 180s，而默认单包上限 500MB，
# 大文件上传很容易超过 180s 从而触发上面的「假失败」。0 表示不改动。
DEFAULT_API_TIMEOUT_SEC = 900

# 主动推送纯文本时，超过该字数就折叠成 QQ 的「合并转发」Node。
# 与 AstrBot 响应管线的 platform_settings.forward_threshold 语义一致，
# 读不到会话配置时用它兜底。
DEFAULT_FORWARD_THRESHOLD = 1500

# 排行榜定时推送的检查间隔（秒）。cron 精度为分钟，取 30s 保证不跳过整点那一分钟，
# 同一分钟内重复命中由 main 里的触发记录去重。
RANK_SUB_TICK_SEC = 30


@dataclass
class PushOutcome:
    """一次「按榜单类型推送」的结果。

    Attributes:
        attempted: 计划推送的会话数
        delivered: 榜单文本已提交给平台的会话数
            （封面 PDF 属附加内容，失败只记日志，不影响该计数）
        skipped: 整批被跳过的原因（榜单为空 / 无可用账号 / 拉取失败），
            非空时 ``delivered`` 必为 0
    """

    attempted: int = 0
    delivered: int = 0
    skipped: str = ""

    @property
    def failed(self) -> int:
        """确认失败的会话数（被跳过时算 0，因为没有真的尝试发送）"""
        if self.skipped:
            return 0
        return max(0, self.attempted - self.delivered)


@dataclass
class RankCoverPack:
    """一次排行榜封面 PDF 的构建结果，供多个会话复用。

    Attributes:
        path: 生成的 PDF 路径；构建失败时为 None
        cover_count: 成功下载的封面张数
        error: 构建失败的原因
    """

    path: Path | None = None
    cover_count: int = 0
    error: str = ""


@register(
    "astrbot_plugin_pica",
    "huashuiyue07",
    "哔咔漫画插件 - 搜索、查看、下载哔咔漫画本子，支持整本下载与打包",
    "1.4.7",
    "https://github.com/huashuiyue07/astrbot_plugin_pica",
)
class PicaPlugin(Star):
    """AstrBot 哔咔漫画插件"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config

        # 数据目录
        try:
            self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"获取数据目录失败: {e}")
            self.data_dir = Path(__file__).parent / "data"
            self.data_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Pica 数据目录: {self.data_dir}")

        # 客户端（注册 token 失效自动重新登录回调）
        self.client = PicaClient(
            use_proxy=bool(config.get("use_proxy", False)),
            proxy_url=config.get("proxy_url", ""),
            max_retry=int(config.get("max_retry", 3)),
            timeout=int(config.get("timeout", 30)),
        )
        self.client.on_token_invalid = self._on_pica_token_invalid

        # 认证管理（按用户隔离）
        self.auth = PicaAuthManager(
            self.client,
            self.data_dir,
            default_email=config.get("pica_account", ""),
            default_password=config.get("pica_password", ""),
            allow_default_account=bool(config.get("allow_default_account", True)),
            store_password=bool(config.get("store_password", False)),
        )

        # 下载器
        self.downloader = PicaDownloader(
            self.client,
            self.data_dir,
            modify_md5=bool(config.get("modify_md5", True)),
        )

        # 进行中的整本下载任务: (user_id, comic_id) -> task
        self._all_download_tasks: dict[tuple[str, str], asyncio.Task] = {}

        # 排行榜快照（用于「新上榜」对比）与定时推送订阅
        self.rank_history = RankHistoryStore(self.data_dir)
        self.subs = RankSubscriptionStore(self.data_dir)
        # 同一分钟内已触发过的榜单类型: tt -> "YYYY-MM-DD HH:MM"
        self._rank_fired: dict[str, str] = {}
        # 已告警过的非法 cron 表达式: tt -> expr（避免每 30 秒刷一次日志）
        self._cron_warned: dict[str, str] = {}
        # 各平台的机器人自身账号: platform_id -> (QQ 号, 昵称)，合并转发节点需要
        self._bot_accounts: dict[str, tuple[str, str]] = {}

        # 后台缓存自动清理任务
        self._clean_task = None
        clean_interval = int(config.get("cache_clean_interval_hours", 12) or 0)
        if clean_interval > 0:
            self._clean_task = self._spawn_task(self._cache_clean_loop, "缓存清理")

        # 排行榜定时推送任务（没有订阅时只是空转，开销可忽略）
        self._sub_task = self._spawn_task(self._rank_sub_loop, "排行榜订阅推送")

        logger.info("Pica-Comics 插件初始化完成")

    @staticmethod
    def _spawn_task(coro_factory, label: str):
        """启动后台任务；没有运行中的事件循环时记日志并返回 None"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(f"无法启动{label}后台任务：当前没有运行中的事件循环")
            return None
        return asyncio.create_task(coro_factory())

    async def terminate(self) -> None:
        """插件卸载/重载时取消后台任务并释放连接"""
        for task in (
            getattr(self, "_clean_task", None),
            getattr(self, "_sub_task", None),
        ):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        try:
            await self.client.aclose()
        except Exception as e:
            logger.debug(f"关闭 HTTP 会话失败: {e}")

    # ---------- 缓存自动清理 ----------

    async def _cache_clean_loop(self) -> None:
        """定时清理缓存（按配置的天数/大小上限）"""
        interval = max(1, int(self.config.get("cache_clean_interval_hours", 12) or 12)) * 3600
        while True:
            await asyncio.sleep(interval)
            try:
                await self._run_cache_cleanup()
            except Exception as e:
                logger.warning(f"自动清理缓存失败: {e}")

    async def _run_cache_cleanup(self) -> dict:
        max_age = int(self.config.get("cache_max_age_days", 7) or 0)
        max_size = int(self.config.get("cache_max_size_mb", 0) or 0)
        stats = await asyncio.to_thread(
            self.downloader.cleanup_cache, max_age, max_size, True
        )
        if stats["deleted_files"] > 0:
            logger.info(
                f"自动清理缓存: 删除 {stats['deleted_files']} 个文件, "
                f"释放 {stats['freed_mb']}MB, 当前 {stats['current_mb']}MB"
            )
        return stats

    # ---------- 工具 ----------

    async def _on_pica_token_invalid(self, user_id: str | None) -> str | None:
        """client 在收到 401/认证错误码时回调，自动重新登录返回新 token"""
        try:
            return await self.auth.force_relogin(user_id or "")
        except Exception as e:
            logger.warning(f"自动重新登录失败: {e}")
            return None

    async def _auth_token(self, user_id: str) -> str:
        """获取 token 并把当前 user_id 注入 client，供自动重登录使用"""
        token = await self.auth.ensure_login(user_id)
        self.client.set_current_user(user_id)
        return token

    @staticmethod
    def _safe_filename(name: str, max_len: int = 80) -> str:
        """清洗输出文件名"""
        import re

        name = re.sub(r'[\\/:*?"<>|\r\n\t]', "-", name).strip()
        return name[:max_len] or "pica"

    def _admin_ids(self) -> list[str]:
        """配置里的管理员 ID 列表（逗号分隔）"""
        return [
            item.strip()
            for item in str(self.config.get("admin_ids", "")).split(",")
            if item.strip()
        ]

    def _check_permission(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """检查权限：管理员白名单（可选，所有命令统一执行）"""
        if not self.config.get("admin_only", False):
            return True, ""
        admin_ids = self._admin_ids()
        if not admin_ids:
            return False, "❌ 已开启仅管理员模式，但未配置 admin_ids"
        user_id = str(event.get_sender_id())
        if user_id in admin_ids:
            return True, ""
        return False, "❌ 你没有权限使用此插件"

    def _uid(self, event: AstrMessageEvent) -> str:
        """当前用户 ID"""
        return str(event.get_sender_id())

    def _guard(self, event: AstrMessageEvent) -> str | None:
        """权限统一入口：无权限时返回提示文本，有权限返回 None"""
        ok, msg = self._check_permission(event)
        return msg if not ok else None

    # ---------- 帮助 ----------

    @filter.command("picahelp")
    async def help_command(self, event: AstrMessageEvent):
        denied = self._guard(event)
        if denied:
            yield event.plain_result(denied)
            return
        yield event.plain_result(MessageFormatter.help_text())

    @filter.command("pica")
    async def pica_command(self, event: AstrMessageEvent):
        denied = self._guard(event)
        if denied:
            yield event.plain_result(denied)
            return
        yield event.plain_result(MessageFormatter.help_text())

    # ---------- 登录（按用户绑定） ----------

    @staticmethod
    def _is_group_session(event: AstrMessageEvent) -> bool:
        """当前会话是否为群聊（OneBot 的 UMO 形如 default:GroupMessage:123）"""
        return ":GroupMessage:" in str(event.unified_msg_origin)

    @filter.command("picalogin")
    async def login_command(
        self, event: AstrMessageEvent, email: str = None, password: str = None
    ):
        """绑定当前 QQ 的哔咔账号：/picalogin <邮箱> <密码>（建议私聊）"""
        denied = self._guard(event)
        if denied:
            yield event.plain_result(denied)
            return
        user_id = self._uid(event)
        if bool(email) != bool(password):
            yield event.plain_result(
                "❌ 用法: /picalogin <邮箱> <密码>\n"
                "        或不带参数，直接绑定插件配置里的账号"
            )
            return
        if email and self._is_group_session(event):
            # 群聊里发密码会永久留在群消息、日志和会话库里
            yield event.plain_result(
                "🔒 请私聊我绑定账号：密码发在群里会留在聊天记录中\n"
                "💡 私聊发送 /picalogin <邮箱> <密码>"
            )
            return
        try:
            if email and password:
                yield event.plain_result("🔑 正在绑定你的哔咔账号...")
                await self.auth.login(user_id, str(email), str(password))
            else:
                # 未提供账号 → 用配置默认账号绑定
                yield event.plain_result("🔑 正在用配置账号绑定...")
                await self.auth.bind_default(user_id)
            yield event.plain_result("✅ 绑定成功！当前账号仅你自己使用")
        except PicaError as e:
            yield event.plain_result(f"❌ 绑定失败: {e}")

    @filter.command("picalogout")
    async def logout_command(self, event: AstrMessageEvent):
        """解绑当前 QQ 的哔咔账号"""
        denied = self._guard(event)
        if denied:
            yield event.plain_result(denied)
            return
        self.auth.logout(self._uid(event))
        yield event.plain_result("👋 已解绑你的哔咔账号")

    @filter.command("picastatus")
    async def status_command(self, event: AstrMessageEvent):
        """查看当前 QQ 的账号状态"""
        denied = self._guard(event)
        if denied:
            yield event.plain_result(denied)
            return
        st = self.auth.status(self._uid(event))
        if st["bound"]:
            email = st.get("email")
            email_line = f"（{email}）" if email else ""
            yield event.plain_result(
                f"✅ 已绑定自己的哔咔账号{email_line}\n🔑 有效期至: "
                f"{MessageFormatter.ts_str(st['expire'])}"
            )
        elif st["source"] == "default":
            yield event.plain_result(
                f"⚠️ 未绑定个人账号，当前使用插件默认账号（{st['email']}）\n"
                f"💡 建议 /picalogin <邮箱> <密码> 绑定自己的账号以隔离"
            )
        else:
            yield event.plain_result(
                "❌ 未绑定哔咔账号\n"
                "💡 发送 /picalogin <邮箱> <密码> 绑定你自己的哔咔账号"
            )

    # ---------- 搜索 ----------

    @filter.command("picasearch")
    async def search_command(
        self, event: AstrMessageEvent, keyword: str = None, page: int = 1
    ):
        """搜索：/picasearch <关键词> [页码]"""
        denied = self._guard(event)
        if denied:
            yield event.plain_result(denied)
            return
        if keyword is None or not str(keyword).strip():
            yield event.plain_result("❌ 用法: /picasearch <关键词> [页码]\n例: /picasearch 碧蓝航线")
            return
        keyword = str(keyword).strip()
        try:
            page = max(1, int(page))
        except (ValueError, TypeError):
            page = 1

        try:
            yield event.plain_result(f"🔍 正在搜索 [{keyword}] 第{page}页...")
            token = await self._auth_token(self._uid(event))
            result = await self.client.search(keyword, sort="ua", page=page, token=token)
            comics = result.get("docs", [])
            total = result.get("total", 0)
            page_size = int(self.config.get("page_size", 10))
            comics = comics[:page_size]
            yield event.plain_result(
                MessageFormatter.format_search_results(
                    comics, keyword, page, total=total, page_size=page_size
                )
            )
        except Exception as e:
            yield event.plain_result(f"❌ 搜索失败: {e}")

    # ---------- 详情 ----------

    @filter.command("picainfo")
    async def info_command(self, event: AstrMessageEvent, comic_id: str = None):
        """详情：/picainfo <ID>"""
        denied = self._guard(event)
        if denied:
            yield event.plain_result(denied)
            return
        if not comic_id:
            yield event.plain_result("❌ 用法: /picainfo <ID>")
            return
        comic_id = str(comic_id).strip()
        try:
            yield event.plain_result(f"📖 正在获取本子 {comic_id} 详情...")
            token = await self._auth_token(self._uid(event))
            comic = await self.client.comic_info(comic_id, token)
            if not comic:
                yield event.plain_result("❌ 未找到该本子，请检查 ID")
                return

            text = MessageFormatter.format_comic(comic, with_episodes=True)

            if self.config.get("send_cover", True):
                cover = await self.downloader.download_cover(comic, token)
                if cover and cover.exists():
                    chain = MessageChain([Comp.Image(file=str(cover)), Comp.Plain(text)])
                    yield event.chain_result(chain.chain)
                    return
            yield event.plain_result(text)
        except PicaError as e:
            yield event.plain_result(f"❌ 获取详情失败: {e}")
        except Exception as e:
            logger.error(f"获取详情异常: {e}")
            yield event.plain_result(f"❌ 获取详情失败: {e}")

    # ---------- 章节 ----------

    @filter.command("picaeps")
    async def episodes_command(self, event: AstrMessageEvent, comic_id: str = None):
        """章节列表：/picaeps <ID>"""
        denied = self._guard(event)
        if denied:
            yield event.plain_result(denied)
            return
        if not comic_id:
            yield event.plain_result("❌ 用法: /picaeps <ID>")
            return
        comic_id = str(comic_id).strip()
        try:
            token = await self._auth_token(self._uid(event))
            eps = await self.client.episodes_all(comic_id, token)
            yield event.plain_result(MessageFormatter.format_episodes({"docs": eps}, comic_id))
        except PicaError as e:
            yield event.plain_result(f"❌ 获取章节失败: {e}")

    # ---------- 下载 ----------

    @filter.command("picadl")
    async def download_command(
        self, event: AstrMessageEvent, comic_id: str = None, ep: str = None
    ):
        """
        下载：
        - /picadl <ID> <章节号>   单章节下载（同步）
        - /picadl <ID>            整本下载（后台任务，完成后通知）
        """
        denied = self._guard(event)
        if denied:
            yield event.plain_result(denied)
            return
        if not comic_id:
            yield event.plain_result(
                "❌ 用法:\n/picadl <ID> <章节号> 单章节\n/picadl <ID> 整本下载\n"
                "例: /picadl 5c4a17b19b7955ef19b0f7f5 1"
            )
            return
        comic_id = str(comic_id).strip()

        try:
            token = await self._auth_token(self._uid(event))
        except PicaError as e:
            yield event.plain_result(f"❌ {e}")
            return

        try:
            comic = await self.client.comic_info(comic_id, token)
            title = comic.get("title", comic_id) if comic else comic_id

            if ep is not None and str(ep).strip():
                # ---------- 单章节下载 ----------
                try:
                    ep_order = int(str(ep).strip())
                except ValueError:
                    yield event.plain_result("❌ 章节号必须是数字")
                    return
                yield event.plain_result(
                    f"⏬ 开始下载 [{title}] 第{ep_order}话，请稍候..."
                )
                try:
                    images = await self.downloader.download_episode(
                        comic_id, ep_order, token=token,
                        max_concurrent=int(self.config.get("max_concurrent", 5)),
                    )
                except PicaError as e:
                    yield event.plain_result(f"❌ 下载失败: {e}")
                    return
                if not images:
                    yield event.plain_result("❌ 下载失败，未获取到任何图片")
                    return
                yield event.plain_result(
                    f"✅ 第{ep_order}话下载完成，共 {len(images)} 张"
                )
                # 发送（按打包格式）
                chain = await self._build_download_result(
                    comic_id, title, f"第{ep_order}话", images[0].parent, token,
                )
                # 单章节同样可能要上传大文件，先抬高 OneBot API 超时
                self._ensure_api_timeout(event)
                yield event.chain_result(chain.chain)
                return

            # ---------- 整本下载（后台任务） ----------
            key = (self._uid(event), comic_id)
            if key in self._all_download_tasks and not self._all_download_tasks[key].done():
                yield event.plain_result(
                    "⏳ 该本子正在整本下载中，请勿重复请求"
                )
                return

            yield event.plain_result(
                f"📚 开始整本下载 [{title}]，完成后会自动通知你"
            )
            task = asyncio.create_task(
                self._download_all_task(event, comic_id, title, token)
            )
            self._all_download_tasks[key] = task
            task.add_done_callback(lambda t, k=key: self._all_download_tasks.pop(k, None))
        except PicaError as e:
            yield event.plain_result(f"❌ 下载失败: {e}")
        except Exception as e:
            logger.error(f"下载异常: {e}")
            yield event.plain_result(f"❌ 下载失败: {e}")

    async def _download_all_task(
        self, event: AstrMessageEvent, comic_id: str, title: str, token: str
    ) -> None:
        """整本下载后台任务"""
        umo = event.unified_msg_origin
        try:
            eps = await self.client.episodes_all(comic_id, token)
            if not eps:
                await self.context.send_message(
                    umo, MessageChain([Comp.Plain(f"❌ [{title}] 没有可用章节")])
                )
                return
            total = len(eps)

            # 按百分比提示进度（默认每 10% 一次，避免 100 章刷屏）
            step_pct = max(1, int(self.config.get("progress_step_pct", 10) or 10))
            next_pct = step_pct

            async def progress(done, total_eps, ep_order):
                nonlocal next_pct
                pct = int(done / total_eps * 100) if total_eps else 100
                if pct >= next_pct or done == total_eps:
                    next_pct = pct + step_pct
                    await self.context.send_message(
                        umo,
                        MessageChain(
                            [Comp.Plain(
                                f"⏳ [{title}] 整本下载中 {pct}% ({done}/{total_eps} 章)..."
                            )]
                        ),
                    )

            root_dir = await self.downloader.download_all(
                comic_id,
                eps,
                token=token,
                max_concurrent=int(self.config.get("max_concurrent", 5)),
                progress_cb=progress,
            )

            # 打包并发送
            pack_format = str(self.config.get("pack_format", "zip") or "zip").lower()
            if pack_format == "images":
                # 整本发图不现实，提示用户
                await self.context.send_message(
                    umo,
                    MessageChain(
                        [Comp.Plain(
                            f"✅ [{title}] 整本下载完成，共 {total} 章\n"
                            f"💡 当前打包格式为 images，无法发送整本，"
                            f"请把 pack_format 改为 zip/pdf/long_img 后重试"
                        )]
                    ),
                )
                return

            packer = PicaPacker(
                pack_format,
                password=self.config.get("pack_password", "") or "",
            )
            safe_name = self._safe_filename(title)
            packs_dir = self.data_dir / PACKS_DIR

            # 按大小分批打包发送，避免单文件过大被 QQ 拒绝（rich media transfer failed）
            batch_mb = int(self.config.get("send_batch_mb", 500) or 0)
            batches = self._batch_episode_dirs(root_dir, batch_mb)
            if not batches:
                await self.context.send_message(
                    umo, MessageChain([Comp.Plain(f"❌ [{title}] 没有可发送的章节")])
                )
                return

            total_batches = len(batches)
            sent_ok = 0
            uncertain = 0
            for idx, batch in enumerate(batches, 1):
                # 该批章节临时移入独立目录打包，避免混入其他批次
                tmp_batch = root_dir / ".batch_tmp"
                tmp_batch.mkdir(exist_ok=True)
                for d in batch:
                    shutil.move(str(d), str(tmp_batch / d.name))
                try:
                    batch_name = safe_name if total_batches == 1 else f"{safe_name}_part{idx}"
                    # 打包是 CPU/IO 密集操作，丢到线程池避免阻塞事件循环
                    result = await asyncio.to_thread(
                        packer.pack, tmp_batch, batch_name, packs_dir
                    )
                finally:
                    for d in batch:
                        shutil.move(str(tmp_batch / d.name), str(root_dir / d.name))
                    try:
                        tmp_batch.rmdir()
                    except OSError:
                        pass

                if not result.success or not result.output_path:
                    await self.context.send_message(
                        umo,
                        MessageChain(
                            [Comp.Plain(f"❌ [{title}] 第{idx}批打包失败: {result.error_message}")]
                        ),
                    )
                    continue

                out_path = result.output_path
                suffix = out_path.suffix.lower()
                ep_orders = sorted(
                    int(d.name.replace("ep", "")) for d in batch if d.name.startswith("ep")
                )
                ep_range = f"{ep_orders[0]}-{ep_orders[-1]}" if ep_orders else str(idx)
                texts = [f"✅ [{title}] 第{ep_range}章 打包完成"]
                if result.encrypted and self.config.get("pack_password", ""):
                    texts.append(f"🔒 密码: {self.config.get('pack_password', '')}")
                if total_batches > 1:
                    texts.append(f"📦 进度: {idx}/{total_batches} 批")
                text = "\n".join(texts)

                if pack_format == "long_img" and suffix == ".png":
                    chain = MessageChain(
                        [Comp.Image(file=str(out_path)), Comp.Plain(text)]
                    )
                elif suffix in (".zip", ".pdf", ".png"):
                    chain = MessageChain(
                        [
                            Comp.File(name=out_path.name, file=str(out_path)),
                            Comp.Plain(text),
                        ]
                    )
                else:
                    chain = MessageChain([Comp.Plain(f"{text}\n📁 {out_path}")])

                send_result = await self._send_with_retry(event, umo, chain)
                if send_result is True:
                    sent_ok += 1
                elif send_result is None:
                    # 上传超时：文件很可能已经送达，不再重发（重发=用户收到两份），
                    # 也不谎报失败，仅在最后统一提示一次。
                    uncertain += 1
                else:
                    await self.context.send_message(
                        umo,
                        MessageChain(
                            [Comp.Plain(
                                f"⚠️ [{title}] 第{ep_range}章 发送失败（文件过大或网络问题），"
                                f"已保存在本地: {out_path}"
                            )]
                        ),
                    )

            if sent_ok == 0 and uncertain == 0 and total_batches > 1:
                await self.context.send_message(
                    umo,
                    MessageChain(
                        [Comp.Plain(
                            f"⚠️ [{title}] 整本下载完成，共 {total} 章，"
                            f"但所有批次发送失败，文件保存在: {packs_dir}"
                        )]
                    ),
                )
            elif uncertain:
                await self.context.send_message(
                    umo,
                    MessageChain(
                        [Comp.Plain(
                            f"⚠️ [{title}] 有 {uncertain} 个分包含上传超时，无法确认是否已送达。"
                            f"请先确认是否收到，未收到再用 /picadl 重试，"
                            f"或直接取本地文件: {packs_dir}"
                        )]
                    ),
                )
        except Exception as e:
            logger.error(f"整本下载任务异常: {e}")
            try:
                await self.context.send_message(
                    umo,
                    MessageChain([Comp.Plain(f"❌ 整本下载失败: {e}")]),
                )
            except Exception:
                pass

    @staticmethod
    def _batch_episode_dirs(root_dir: Path, batch_mb: int) -> list[list[Path]]:
        """按大小把章节目录分批（连续章节合批，单批 ≤ batch_mb）。

        batch_mb <= 0 时全部合并为一批（整本单文件）。
        """
        ep_dirs = sorted(
            [d for d in root_dir.iterdir() if d.is_dir()],
            key=lambda p: p.name,
        )
        if not ep_dirs:
            return []
        if batch_mb <= 0:
            return [ep_dirs]

        limit = batch_mb * 1024 * 1024

        def _dir_size(d: Path) -> int:
            try:
                return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            except OSError:
                return 0

        batches: list[list[Path]] = []
        cur: list[Path] = []
        cur_size = 0
        for d in ep_dirs:
            size = _dir_size(d)
            if cur and cur_size + size > limit:
                batches.append(cur)
                cur, cur_size = [], 0
            cur.append(d)
            cur_size += size
        if cur:
            batches.append(cur)
        return batches

    @staticmethod
    def _has_upload_components(chain: MessageChain) -> bool:
        """消息链中是否含需要上传本地文件的组件（图片/文件/语音/视频）"""
        comps = getattr(chain, "chain", None) or []
        return any(isinstance(c, UPLOAD_COMPONENT_TYPES) for c in comps)

    @staticmethod
    def _is_api_timeout(err: BaseException) -> bool:
        """异常是否为 OneBot API 调用超时（消息可能已送达的不确定态）"""
        text = f"{type(err).__name__}: {err}".lower()
        return "timeout" in text or "超时" in text

    def _ensure_api_timeout(self, event: AstrMessageEvent | None) -> None:
        """把 OneBot API 调用超时抬到配置值以上，避免大文件上传被判超时。

        AstrBot 的 aiocqhttp 适配器把该值硬编码为 180s（见
        `aiocqhttp_platform_adapter.py` 的 `api_timeout_sec=180`），而默认单包上限
        500MB，上传常常超过 180s。这里在发送前原地抬高该值。

        - 只增不减，因而幂等，并发调用不会把别人的超时改小
        - 任何内部结构变动只会让本方法静默失效，不影响发送本身
        """
        want = int(self.config.get("send_api_timeout", DEFAULT_API_TIMEOUT_SEC) or 0)
        if want <= 0:
            return
        bot = getattr(event, "bot", None)
        api = getattr(bot, "_api", None)
        if api is None:
            return
        for holder in (api, getattr(api, "_wsr_api", None), getattr(api, "_http_api", None)):
            if holder is None:
                continue
            cur = getattr(holder, "_timeout_sec", None)
            if not isinstance(cur, (int, float)) or cur >= want:
                continue
            try:
                holder._timeout_sec = want
                logger.info(f"OneBot API 超时已由 {cur}s 提升至 {want}s（大文件上传）")
            except Exception as e:
                logger.debug(f"调整 OneBot API 超时失败: {e}")

    async def _send_with_retry(
        self,
        event: AstrMessageEvent | None,
        umo: str,
        chain: MessageChain,
        retries: int = 2,
    ) -> bool | None:
        """发送消息。

        返回 ``True`` 已送达 / ``False`` 确认失败 / ``None`` 结果不确定（上传超时）。

        ⚠️ 含上传类组件的消息链**只尝试一次**，不重试：
        上传大文件的耗时可以超过 OneBot API 调用超时，该超时只是客户端不再等待，
        napcat 侧仍会把文件传完并投递成功。此时若按「失败」重发，用户就会收到两份
        文件（v1.4.1 修复的重复发送问题）。
        """
        upload = self._has_upload_components(chain)
        if upload:
            self._ensure_api_timeout(event)

        attempts = 1 if upload else retries + 1
        for attempt in range(attempts):
            try:
                if await self.context.send_message(umo, chain):
                    return True
            except Exception as e:
                if upload and self._is_api_timeout(e):
                    logger.warning(
                        f"上传消息超时（不代表未送达，已放弃重发以免重复）: {e}"
                    )
                    return None
                logger.warning(f"发送消息失败(第{attempt + 1}次): {e}")
            if attempt < attempts - 1:
                await asyncio.sleep(2 * (attempt + 1))
        return False

    async def _build_download_result(
        self,
        comic_id: str,
        title: str,
        ep_label: str,
        ep_dir: Path,
        token: str,
    ) -> MessageChain:
        """单章节下载结果按打包格式构造消息链（返回 MessageChain 而非异步生成器）"""
        pack_format = str(self.config.get("pack_format", "zip") or "zip").lower()
        total = len(list(ep_dir.glob("*")))

        if pack_format == "images":
            images = sorted(ep_dir.glob("*"))[:MAX_SEND_IMAGES]
            comps = [Comp.Image(file=str(p)) for p in images]
            remaining = total - len(images)
            tail = f"\n……等共 {total} 张" if remaining > 0 else ""
            comps.append(Comp.Plain(f"✅ [{title}] - {ep_label} 下载完成，共 {total} 张{tail}"))
            return MessageChain(comps)

        packer = PicaPacker(
            pack_format,
            password=self.config.get("pack_password", "") or "",
        )
        safe_name = self._safe_filename(f"{title}_{ep_label}")
        packs_dir = self.data_dir / PACKS_DIR
        # 打包是 CPU/IO 密集操作，丢到线程池避免阻塞事件循环
        result = await asyncio.to_thread(packer.pack, ep_dir, safe_name, packs_dir)

        if not result.success or not result.output_path:
            return MessageChain(
                [Comp.Plain(f"❌ 打包失败 ({pack_format}): {result.error_message}")]
            )

        out_path = result.output_path
        suffix = out_path.suffix.lower()
        texts = [f"✅ [{title}] - {ep_label} 下载完成，共 {total} 张"]
        if result.encrypted and self.config.get("pack_password", ""):
            texts.append(f"🔒 密码: {self.config.get('pack_password', '')}")
        text = "\n".join(texts)

        if pack_format == "long_img" and suffix == ".png":
            return MessageChain([Comp.Image(file=str(out_path)), Comp.Plain(text)])
        if suffix in (".zip", ".pdf", ".png"):
            return MessageChain(
                [Comp.File(name=out_path.name, file=str(out_path)), Comp.Plain(text)]
            )
        return MessageChain([Comp.Plain(f"{text}\n📁 {out_path}")])

    # ---------- 排行榜 ----------

    @staticmethod
    def _parse_tt(value) -> str | None:
        """把用户输入解析成榜单类型（H24/D7/D30），非法返回 None"""
        tt = str(value or "").strip().upper()
        return tt if tt in RANK_TYPES else None

    def _page_size(self) -> int:
        try:
            return max(1, int(self.config.get("page_size", 10) or 10))
        except (TypeError, ValueError):
            return 10

    def _push_size(self) -> int:
        """定时推送的榜单条数；rank_push_size 为 0/非法时跟随 page_size"""
        try:
            size = int(self.config.get("rank_push_size", 0) or 0)
        except (TypeError, ValueError):
            size = 0
        return size if size > 0 else self._page_size()

    async def _rank_payload(
        self, tt: str, token: str, limit: int | None = None
    ) -> tuple[list[dict], RankDiff]:
        """拉取全量榜单并与上次快照对比。

        Args:
            limit: 展示条数；None 或非正数时用 page_size。
                定时推送传 rank_push_size，使「推送条数」与「命令条数」解耦。

        Returns:
            (按 limit 截断的展示用榜单, 与上次快照的差异)
        """
        all_comics = list(await self.client.leaderboard(tt, token) or [])
        if not all_comics:
            # 空结果可能只是接口抖动：不更新快照，
            # 否则下次会把整榜都当成「新上榜」
            return [], RankDiff()
        size = limit if limit is not None and limit > 0 else self._page_size()
        diff = self.rank_history.update(tt, all_comics, limit=size)
        return all_comics[:size], diff

    def _rank_texts(self, tt: str, comics: list, diff: RankDiff) -> list[str]:
        """本次要发送的榜单文本：榜单列表 [+ 新上榜高亮]"""
        texts = [MessageFormatter.format_rank(comics, tt)]
        if diff.new_entries and bool(self.config.get("rank_new_highlight", True)):
            texts.append(
                MessageFormatter.format_new_entries(
                    diff.new_entries, tt, diff.previous_updated_at
                )
            )
        return texts

    @filter.command("picarank")
    async def rank_command(self, event: AstrMessageEvent, tt: str = "H24"):
        """排行榜：/picarank [H24|D7|D30]"""
        denied = self._guard(event)
        if denied:
            yield event.plain_result(denied)
            return
        tt = self._parse_tt(tt) or "H24"
        try:
            yield event.plain_result("🏆 正在获取排行榜...")
            token = await self._auth_token(self._uid(event))
            comics, diff = await self._rank_payload(tt, token)
            for text in self._rank_texts(tt, comics, diff):
                yield event.plain_result(text)
            await self._maybe_send_rank_cover_pdf(
                event.unified_msg_origin, comics, token, tt, event
            )
        except PicaError as e:
            yield event.plain_result(f"❌ 获取排行榜失败: {e}")

    async def _notify(self, umo: str, text: str) -> bool:
        """尽力发送一条纯文本消息。

        Returns:
            是否已**提交给平台**。``Context.send_message`` 在找不到会话对应的
            平台实例时返回 ``False``（不抛异常），协议端内部的静默失败不在此保证内。
        """
        chain: MessageChain | None = None
        try:
            chain = await self._build_notify_chain(umo, text)
            ok = await self.context.send_message(umo, chain)
            if not ok:
                logger.debug(f"未找到会话 {umo} 对应的平台实例，消息未发送")
            return bool(ok)
        except Exception as e:
            if chain is not None and any(
                isinstance(comp, Comp.Node) for comp in chain.chain
            ):
                # 折叠成长消息后失败 = 用户什么都收不到，值得告警
                logger.warning(f"合并转发发送失败: {e}")
            else:
                logger.debug(f"发送提示消息失败: {e}")
            return False

    @staticmethod
    def _umo_platform_id(umo: str) -> str:
        """从 unified_msg_origin 取平台 ID（形如 default:group:123456）"""
        return str(umo).split(":", 1)[0]

    def _forward_threshold(self, umo: str) -> int:
        """该会话「超长文本折叠成合并转发」的字数阈值。

        读取会话自己的 platform_settings.forward_threshold，
        与管线里的 ResultDecorateStage 保持同一套配置。
        """
        raw = None
        try:
            conf = self.context.get_config(umo=umo) or {}
            raw = (conf.get("platform_settings") or {}).get("forward_threshold")
        except Exception as e:
            logger.debug(f"读取 forward_threshold 失败，使用默认值: {e}")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return DEFAULT_FORWARD_THRESHOLD

    def _find_platform(self, platform_id: str):
        """按平台 ID 找平台实例（取机器人自身账号用）"""
        try:
            platforms = self.context.platform_manager.platform_insts
        except Exception as e:
            logger.debug(f"获取平台实例失败: {e}")
            return None
        for platform in platforms:
            try:
                if str(platform.meta().id) == platform_id:
                    return platform
            except Exception:
                continue
        return None

    async def _bot_identity(self, umo: str) -> tuple[str, str]:
        """该会话所属平台机器人的 (QQ 号, 昵称)，带缓存。

        合并转发节点需要注明「发送者」，而主动推送没有 event 可取 self_id，
        只能向平台要。aiocqhttp 的 bot 通过 __getattr__ 动态暴露 OneBot API，
        因此 bot.get_login_info() 等价于调用 get_login_info 接口。
        任何一步失败都返回空串，由调用方退化成普通文本。
        """
        platform_id = self._umo_platform_id(umo)
        cached = self._bot_accounts.get(platform_id)
        if cached:
            return cached

        platform = self._find_platform(platform_id)
        bot = getattr(platform, "bot", None) if platform is not None else None
        if bot is None:
            return "", ""
        try:
            info = await bot.get_login_info()
        except Exception as e:
            logger.warning(f"获取机器人账号信息失败，长消息将不折叠: {e}")
            return "", ""

        self_id = str((info or {}).get("user_id") or "")
        nickname = str((info or {}).get("nickname") or "")
        if not self_id:
            return "", ""
        self._bot_accounts[platform_id] = (self_id, nickname)
        return self_id, nickname

    async def _build_notify_chain(self, umo: str, text: str) -> MessageChain:
        """构造主动推送的消息链：超长文本按响应管线的规则折叠成「合并转发」。

        响应管线（ResultDecorateStage）只对**经过管线**的消息做这层处理：
        aiocqhttp 平台下纯文本超过 forward_threshold 时，整条消息折叠成一个
        Node（QQ 的「合并转发」）。主动推送走 context.send_message，绕过了
        整条管线，超长纯文本在部分协议端会发送失败，因此这里复刻同样的规则，
        阈值取该会话自己的平台配置。
        """
        threshold = self._forward_threshold(umo)
        if len(text) <= threshold:
            return MessageChain([Comp.Plain(text)])

        self_id, nickname = await self._bot_identity(umo)
        if not self_id:
            logger.warning(
                f"文本 {len(text)} 字超过 forward_threshold({threshold})，"
                f"但取不到机器人账号，仍按普通文本发送（可能被平台拒收）"
            )
            return MessageChain([Comp.Plain(text)])

        node = Comp.Node(
            uin=self_id,
            name=nickname or "AstrBot",
            content=[Comp.Plain(text)],
        )
        return MessageChain([node])

    async def _maybe_send_rank_cover_pdf(
        self,
        umo: str,
        comics: list,
        token: str,
        tt: str,
        event: AstrMessageEvent | None = None,
    ) -> bool:
        """按配置决定是否附带排行榜封面 PDF（rank_cover_pdf，默认关闭）。"""
        if not comics or not self.config.get("rank_cover_pdf", False):
            return False
        await self._notify(
            umo, f"🖼️ 正在下载排行榜 {len(comics)} 张封面并生成 PDF..."
        )
        pack = await self._build_rank_cover_pdf(comics, token, tt)
        if pack.path is None:
            await self._notify(umo, f"❌ 封面 PDF 生成失败: {pack.error}")
            return False
        return await self._send_rank_cover_pdf_file(umo, pack, tt, event)

    async def _build_rank_cover_pdf(
        self, comics: list, token: str, tt: str
    ) -> RankCoverPack:
        """下载榜单全部封面并打包成 PDF（只构建、不发送）。

        与发送解耦，使定时推送能「构建一次、多群复用」；封面临时目录用完即清。
        """
        cover_dir: Path | None = None
        try:
            # 每次构建用独立临时目录：并发构建时封面不会互相污染
            cover_dir = Path(
                tempfile.mkdtemp(
                    prefix=f"rank_{tt}_", dir=self.downloader.cache_dir()
                )
            )
            covers = await self.downloader.download_covers(
                comics,
                token=token,
                max_concurrent=int(self.config.get("max_concurrent", 5)),
                target_dir=cover_dir,
            )
            if not covers:
                return RankCoverPack(error="未获取到任何封面")

            packer = PicaPacker("pdf")
            safe_name = self._safe_filename(f"排行榜封面_{tt}")
            packs_dir = self.data_dir / PACKS_DIR
            # 打包是 CPU/IO 密集操作，丢到线程池避免阻塞事件循环
            result = await asyncio.to_thread(
                packer.pack, cover_dir, safe_name, packs_dir
            )
            if not result.success or not result.output_path:
                return RankCoverPack(error=result.error_message or "PDF 生成失败")
            return RankCoverPack(path=result.output_path, cover_count=len(covers))
        except Exception as e:
            logger.error(f"排行榜封面 PDF 构建异常: {e}")
            return RankCoverPack(error=str(e))
        finally:
            # 封面只是 PDF 的中间产物，打包完即可清理
            if cover_dir is not None:
                shutil.rmtree(cover_dir, ignore_errors=True)

    async def _send_rank_cover_pdf_file(
        self,
        umo: str,
        pack: RankCoverPack,
        tt: str,
        event: AstrMessageEvent | None = None,
    ) -> bool:
        """把已构建好的封面 PDF 发给一个会话（失败只提示，不抛异常）。

        Returns:
            是否确认发送成功。
        """
        out_path = pack.path
        if out_path is None:
            return False
        rank_name = RANK_TYPES.get(tt, tt)
        chain = MessageChain(
            [
                Comp.File(name=out_path.name, file=str(out_path)),
                Comp.Plain(f"🖼️ 排行榜封面 {pack.cover_count} 张 ({rank_name})"),
            ]
        )
        try:
            send_result = await self._send_with_retry(event, umo, chain)
        except Exception as e:
            logger.warning(f"封面 PDF 发送异常: {e}")
            return False
        if send_result is True:
            return True
        if send_result is None:
            # 上传超时：文件很可能已送达，不再重发，也不谎报失败
            await self._notify(
                umo,
                f"⚠️ 封面 PDF 上传超时，无法确认是否已送达，请先确认是否收到；"
                f"本地文件: {out_path}",
            )
            return False
        await self._notify(umo, f"⚠️ 封面 PDF 发送失败，已保存在本地: {out_path}")
        return False

    # ---------- 排行榜订阅（定时推送） ----------

    def _cron_expr(self, tt: str) -> str:
        """该榜单类型的刷新时间（cron 表达式，空串表示不推送）"""
        return str(self.config.get(f"rank_cron_{tt.lower()}", "") or "").strip()

    def _sub_status_text(self, umo: str) -> str:
        """订阅状态文本（list 与订阅成功回复共用）"""
        targets = self.subs.targets(umo)
        lines = ["📋 本群排行榜订阅", "━━━━━━━━━━━━━━━━━━━"]
        for tt, name in RANK_TYPES.items():
            if tt not in targets:
                lines.append(f"➖ {name}")
                continue
            expr = self._cron_expr(tt)
            if not expr:
                schedule = "⚠️ 未配置刷新时间"
            elif validate_cron(expr):
                schedule = f"⚠️ 刷新时间非法: {expr}"
            else:
                schedule = f"⏰ {expr}"
            lines.append(f"✅ {name} {schedule} · 推送 {self._push_size()} 条")
        lines.append("━━━━━━━━━━━━━━━━━━━")
        lines.append("💡 /picasub <H24|D7|D30> 添加 | /picasub off 取消全部")
        return "\n".join(lines)

    def _sub_guard(self, event: AstrMessageEvent) -> str | None:
        """订阅写操作（添加/取消）的权限检查。

        默认 `sub_admin_only=True`：只有 `admin_ids` 里的用户能改订阅。
        定时推送会把榜单主动发到群里，若任何群成员都能订阅，等于给了一个
        「让机器人每天往群里推内容」的开关，因此写操作默认收紧；
        `/picasub list` 是只读的，不受此限制。
        """
        if not self.config.get("sub_admin_only", True):
            return None
        admin_ids = self._admin_ids()
        if not admin_ids:
            return (
                "❌ 已开启「仅管理员可订阅」，但未配置 admin_ids；"
                "请填写管理员 QQ，或把 sub_admin_only 设为 false"
            )
        if self._uid(event) not in admin_ids:
            return "❌ 只有管理员可以添加/取消排行榜订阅"
        return None

    @filter.command("picasub")
    async def subscribe_command(self, event: AstrMessageEvent, arg: str = None):
        """排行榜定时推送订阅：/picasub [H24|D7|D30] | list | off"""
        denied = self._guard(event)
        if denied:
            yield event.plain_result(denied)
            return
        umo = event.unified_msg_origin
        value = str(arg or "").strip()
        keyword = value.lower()

        if keyword in ("list", "ls", "status"):
            yield event.plain_result(self._sub_status_text(umo))
            return

        denied = self._sub_guard(event)
        if denied:
            yield event.plain_result(denied)
            return

        if keyword in ("off", "del", "unsub"):
            removed = self.subs.remove(umo)
            if not removed:
                yield event.plain_result("📭 本群当前没有排行榜订阅")
            else:
                names = "、".join(RANK_TYPES.get(tt, tt) for tt in removed)
                yield event.plain_result(f"🗑️ 已取消本群的排行榜订阅：{names}")
            return

        # /picarank 的回落是无副作用的展示行为，可以保留；这里会写持久化订阅，
        # 因此参数非法时明确报错，不替用户决定订阅哪个榜单。
        parsed_tt = self._parse_tt(value)
        if value and parsed_tt is None:
            yield event.plain_result(
                "❌ 用法: /picasub [H24|D7|D30]\n"
                "        /picasub list 查看本群订阅 | /picasub off 取消全部"
            )
            return
        tt = parsed_tt or "H24"
        name = RANK_TYPES[tt]
        added = self.subs.add(umo, tt, self._uid(event))
        head = (
            f"✅ 已为本群订阅「{name}」排行榜"
            if added
            else f"ℹ️ 本群早已订阅「{name}」排行榜"
        )

        expr = self._cron_expr(tt)
        if not expr:
            tip = f"⚠️ 未配置刷新时间（rank_cron_{tt.lower()}），订阅后不会推送"
        else:
            err = validate_cron(expr)
            if err:
                tip = f"⚠️ 刷新时间配置有误（{expr}）：{err}"
            else:
                tip = f"⏰ 定时推送: {expr}"
        yield event.plain_result(f"{head}\n{tip}\n\n{self._sub_status_text(umo)}")

    def _debug_guard(self, event: AstrMessageEvent) -> str | None:
        """调试命令权限：必须配置在 admin_ids 中。

        `/picadebug` 会立刻向所有订阅会话推送，等于一次全量广播，
        因此即使没开 admin_only，也只允许 admin_ids 里的用户使用。
        """
        denied = self._guard(event)
        if denied:
            return denied
        admin_ids = self._admin_ids()
        if not admin_ids:
            return (
                "❌ 调试命令需要先在插件配置里填写 admin_ids"
                "（逗号分隔的 QQ 号）才能使用"
            )
        if self._uid(event) not in admin_ids:
            return "❌ 你没有权限使用调试命令"
        return None

    async def _trigger_subscriptions(self, targets: list[str]) -> tuple[int, list[str]]:
        """无视时间触发订阅推送，返回 (已提交的会话数, 每个榜单的结果说明)"""
        sent = 0
        notes: list[str] = []
        for tt in targets:
            name = RANK_TYPES.get(tt, tt)
            groups = self.subs.groups_for(tt)
            if not groups:
                notes.append(f"➖ {name}: 无人订阅")
                continue
            outcome = await self._push_rank_to_groups(tt, groups)
            sent += outcome.delivered
            if outcome.skipped:
                notes.append(f"➖ {name}: 已跳过（{outcome.skipped}）")
            elif outcome.failed:
                notes.append(
                    f"⚠️ {name}: 已推送给 {outcome.delivered}/{outcome.attempted} 个会话"
                    f"（其余失败，请看日志）"
                )
            else:
                notes.append(f"✅ {name}: 已推送给 {outcome.delivered} 个会话")
        return sent, notes

    @filter.command("picadebug")
    async def debug_command(self, event: AstrMessageEvent, arg: str = None):
        """调试：无视 cron 时间立即触发订阅推送：/picadebug [H24|D7|D30|all]"""
        denied = self._debug_guard(event)
        if denied:
            yield event.plain_result(denied)
            return

        value = str(arg or "").strip()
        if value.lower() in ("", "all", "*", "全部"):
            targets = list(RANK_TYPES)
        else:
            tt = self._parse_tt(value)
            if tt is None:
                yield event.plain_result(
                    "❌ 用法: /picadebug [H24|D7|D30|all]\n"
                    "调试用：无视 rank_cron_* 的时间设置，立即触发一次订阅推送"
                )
                return
            targets = [tt]

        yield event.plain_result(
            "🔧 调试触发订阅推送（无视时间）: "
            + "、".join(RANK_TYPES.get(tt, tt) for tt in targets)
        )
        sent, notes = await self._trigger_subscriptions(targets)
        yield event.plain_result(
            "\n".join(
                [
                    "🔧 触发完成",
                    "━━━━━━━━━━━━━━━━━━━",
                    *notes,
                    "━━━━━━━━━━━━━━━━━━━",
                    f"共推送 {sent} 个会话",
                    "💡 本次榜单已写入快照，随后的定时推送不会重复提醒新上榜",
                ]
            )
        )

    async def _rank_sub_loop(self) -> None:
        """按配置的 cron 定时推送排行榜（每 RANK_SUB_TICK_SEC 秒检查一次）"""
        while True:
            await asyncio.sleep(RANK_SUB_TICK_SEC)
            try:
                await self._run_rank_subscriptions(datetime.now())
            except Exception as e:
                logger.warning(f"排行榜定时推送失败: {e}")

    async def _run_rank_subscriptions(self, now: datetime) -> int:
        """到点的榜单类型向订阅会话推送，返回本次推送的会话数（便于测试）"""
        stamp = now.strftime("%Y-%m-%d %H:%M")
        pushed = 0
        for tt in RANK_TYPES:
            expr = self._cron_expr(tt)
            if not expr:
                continue
            err = validate_cron(expr)
            if err:
                if self._cron_warned.get(tt) != expr:
                    self._cron_warned[tt] = expr
                    logger.warning(
                        f"rank_cron_{tt.lower()} 不是合法的 cron 表达式，已忽略: {err}"
                    )
                continue
            if not cron_matches(expr, now):
                continue
            if self._rank_fired.get(tt) == stamp:
                continue  # 同一分钟只触发一次
            groups = self.subs.groups_for(tt)
            if not groups:
                continue
            self._rank_fired[tt] = stamp
            outcome = await self._push_rank_to_groups(tt, groups)
            pushed += outcome.delivered
        return pushed

    async def _push_rank_to_groups(self, tt: str, groups: list[str]) -> PushOutcome:
        """抓取一次榜单并推送给全部订阅会话。

        Returns:
            推送结果；``skipped`` 非空表示整批跳过（榜单为空 / 无可用账号 / 拉取失败）。
        """
        outcome = PushOutcome(attempted=len(groups))
        identity = await self._subscription_identity(groups)
        if identity is None:
            outcome.skipped = "没有可用的哔咔账号"
            return outcome
        token, user_id = identity
        self.client.set_current_user(user_id)
        try:
            comics, diff = await self._rank_payload(
                tt, token, limit=self._push_size()
            )
        except Exception as e:
            logger.warning(f"定时推送 {tt} 排行榜失败: {e}")
            outcome.skipped = "获取榜单失败"
            return outcome

        if not comics:
            # 空榜单（接口抖动 / 风控 / 限流）不该广播给所有订阅群：
            # 手动 /picarank 保留「排行榜为空」提示，主动推送直接跳过。
            logger.info(
                f"{tt} 榜单为空，跳过本次推送（{len(groups)} 个订阅会话）"
            )
            outcome.skipped = "榜单为空"
            return outcome

        for umo in groups:
            try:
                delivered = True
                for text in self._rank_texts(tt, comics, diff):
                    if not await self._notify(umo, text):
                        delivered = False
                if delivered:
                    outcome.delivered += 1
            except Exception as e:
                logger.warning(f"向 {umo} 推送 {tt} 榜单文本失败: {e}")

        # 封面 PDF 属附加内容：一次构建、多群复用（避免每个会话都重新打包），
        # 成败只记日志，不计入「榜单是否送达」。
        if self.config.get("rank_cover_pdf", False):
            pack = await self._build_rank_cover_pdf(comics, token, tt)
            if pack.path is None:
                logger.warning(
                    f"{tt} 封面 PDF 生成失败，本次推送不带封面: {pack.error}"
                )
            else:
                for umo in groups:
                    try:
                        await self._send_rank_cover_pdf_file(umo, pack, tt)
                    except Exception as e:
                        logger.warning(f"向 {umo} 推送封面 PDF 失败: {e}")

        logger.info(
            f"排行榜定时推送完成: {tt} → {outcome.delivered}/{outcome.attempted} 个会话"
        )
        return outcome

    async def _subscription_identity(self, groups: list[str]) -> tuple[str, str] | None:
        """定时推送用的账号：优先订阅者自己绑定的账号，其次配置的默认账号"""
        for umo in groups:
            owner = self.subs.owner_of(umo)
            if not owner:
                continue
            try:
                return await self.auth.ensure_login(owner), owner
            except PicaError:
                continue
        try:
            return await self.auth.ensure_default_login(), "__default__"
        except PicaError as e:
            logger.warning(f"排行榜定时推送缺少可用账号，本次跳过: {e}")
            return None

    # ---------- 分区浏览 ----------

    @filter.command("picacomics")
    async def comics_command(
        self, event: AstrMessageEvent, category: str = None, page: int = 1
    ):
        """分区浏览：/picacomics <分区名> [页码]"""
        denied = self._guard(event)
        if denied:
            yield event.plain_result(denied)
            return
        if not category:
            yield event.plain_result(MessageFormatter.format_categories())
            return
        category = str(category).strip()
        matched = None
        for c in CATEGORIES:
            if category in c or c in category:
                matched = c
                break
        if not matched:
            yield event.plain_result(
                f"❌ 未找到分区「{category}」\n💡 使用 /picacat 查看全部分区"
            )
            return
        try:
            page = max(1, int(page))
        except (ValueError, TypeError):
            page = 1
        try:
            token = await self._auth_token(self._uid(event))
            result = await self.client.comics(
                block=matched, order="ua", page=page, token=token
            )
            comics = result.get("docs", [])
            page_size = int(self.config.get("page_size", 10))
            comics = comics[:page_size]
            if not comics:
                yield event.plain_result(f"📭 分区「{matched}」第{page}页没有内容")
                return
            lines = [f"🗂️ 分区: {matched} (第{page}页)", "━━━━━━━━━━━━━━━━━━━"]
            for i, c in enumerate(comics, 1):
                title = c.get("title", "未知")
                if len(title) > 40:
                    title = title[:40] + "..."
                lines.append(
                    f"{i}. {title}\n   ID: {c.get('_id', 'N/A')} | ❤️ {c.get('totalLikes', 0)}"
                )
            lines.append("━━━━━━━━━━━━━━━━━━━")
            lines.append("💡 回复 /picainfo <ID> 查看详情")
            yield event.plain_result("\n".join(lines))
        except PicaError as e:
            yield event.plain_result(f"❌ 获取分区失败: {e}")

    @filter.command("picacat")
    async def categories_command(self, event: AstrMessageEvent):
        """分区列表"""
        denied = self._guard(event)
        if denied:
            yield event.plain_result(denied)
            return
        yield event.plain_result(MessageFormatter.format_categories())

    # ---------- 收藏 ----------

    @filter.command("picafav")
    async def favourite_command(self, event: AstrMessageEvent, comic_id: str = None):
        """收藏/取消收藏：/picafav <ID>"""
        denied = self._guard(event)
        if denied:
            yield event.plain_result(denied)
            return
        if not comic_id:
            yield event.plain_result("❌ 用法: /picafav <ID>")
            return
        comic_id = str(comic_id).strip()
        try:
            token = await self._auth_token(self._uid(event))
            is_fav = await self.client.favourite(comic_id, token)
            if is_fav:
                yield event.plain_result(f"✅ 已收藏 {comic_id}")
            else:
                yield event.plain_result(f"🗑️ 已取消收藏 {comic_id}")
        except PicaError as e:
            yield event.plain_result(f"❌ 操作失败: {e}")

    @filter.command("picamyfav")
    async def my_favourite_command(self, event: AstrMessageEvent, page: int = 1):
        """我的收藏：/picamyfav [页码]"""
        denied = self._guard(event)
        if denied:
            yield event.plain_result(denied)
            return
        try:
            page = max(1, int(page))
        except (ValueError, TypeError):
            page = 1
        try:
            token = await self._auth_token(self._uid(event))
            result = await self.client.my_favourite(page=page, sort="ua", token=token)
            comics = result.get("docs", [])
            page_size = int(self.config.get("page_size", 10))
            comics = comics[:page_size]
            yield event.plain_result(MessageFormatter.format_favourites(comics, page))
        except PicaError as e:
            yield event.plain_result(f"❌ 获取收藏失败: {e}")

    # ---------- 签到 ----------

    @filter.command("picapunch")
    async def punch_command(self, event: AstrMessageEvent):
        """每日签到领币"""
        denied = self._guard(event)
        if denied:
            yield event.plain_result(denied)
            return
        try:
            token = await self._auth_token(self._uid(event))
            data = await self.client.punch_in(token)
            if data:
                yield event.plain_result("✅ 今日已签到！")
            else:
                yield event.plain_result("✅ 签到成功！")
        except PicaError as e:
            yield event.plain_result(f"❌ 签到失败: {e}")

    # ---------- 清理缓存 ----------

    @filter.command("picaclean")
    async def clean_command(self, event: AstrMessageEvent, days: str = None):
        """
        清理缓存：
        - /picaclean            清空全部缓存与打包产物
        - /picaclean <天数>     只清理 N 天前的缓存
        """
        denied = self._guard(event)
        if denied:
            yield event.plain_result(denied)
            return
        cache_dir = self.downloader.cache_dir()
        try:
            if days is not None and str(days).strip():
                try:
                    d = int(str(days).strip())
                except ValueError:
                    yield event.plain_result("❌ 天数必须是数字，如 /picaclean 7")
                    return
                yield event.plain_result(f"🧹 正在清理 {d} 天前的缓存...")
                stats = await asyncio.to_thread(
                    self.downloader.cleanup_cache, d, 0, True
                )
            else:
                yield event.plain_result("🧹 正在清空全部缓存...")
                count = sum(1 for _ in cache_dir.rglob("*") if _.is_file())
                shutil.rmtree(cache_dir, ignore_errors=True)
                cache_dir.mkdir(parents=True, exist_ok=True)
                packs_dir = self.data_dir / PACKS_DIR
                if packs_dir.exists():
                    pcount = sum(1 for _ in packs_dir.rglob("*") if _.is_file())
                    shutil.rmtree(packs_dir, ignore_errors=True)
                    count += pcount
                stats = {
                    "deleted_files": count,
                    "freed_mb": 0.0,
                    "current_mb": 0.0,
                }
            yield event.plain_result(
                f"🧹 清理完成：删除 {stats['deleted_files']} 个文件，"
                f"释放 {stats['freed_mb']}MB，当前缓存 {stats['current_mb']}MB"
            )
        except Exception as e:
            yield event.plain_result(f"❌ 清理失败: {e}")