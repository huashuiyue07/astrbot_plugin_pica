"""
消息格式化工具
"""

from datetime import datetime

from .constants import CATEGORIES, ORDERS, RANK_TYPES


class MessageFormatter:
    """消息格式化器"""

    @staticmethod
    def _ts_to_str(ts) -> str:
        """时间戳转日期字符串"""
        if not ts:
            return "未知"
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except Exception:
            return str(ts)

    @staticmethod
    def ts_str(ts) -> str:
        """时间戳转完整时间字符串"""
        if not ts:
            return "未知"
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(ts)

    @staticmethod
    def format_comic(comic: dict, with_episodes: bool = False) -> str:
        """格式化单本漫画信息"""
        title = comic.get("title", "未知标题")
        comic_id = comic.get("_id", "N/A")
        author = comic.get("author", "未知")
        total_views = comic.get("totalViews", 0)
        total_likes = comic.get("totalLikes", 0)
        total_comments = comic.get("commentsCount", 0)
        likes_count = comic.get("likesCount", 0)
        categories = comic.get("categories", [])
        tags = comic.get("tags", [])
        description = comic.get("description", "")
        created_at = MessageFormatter._ts_to_str(comic.get("created_at"))
        updated_at = MessageFormatter._ts_to_str(comic.get("updated_at"))

        lines = [
            f"📖 {title}",
            "━━━━━━━━━━━━━━━━━━━",
            f"🆔 ID: {comic_id}",
            f"✍️ 作者: {author}",
        ]
        if categories:
            lines.append(f"🗂️ 分区: {', '.join(categories[:4])}")
        if tags:
            lines.append(f"🏷️ 标签: {', '.join(tags[:6])}")
        lines.append(
            f"👁️ 浏览: {total_views}  ❤️ 爱心: {total_likes or likes_count}  💬 评论: {total_comments}"
        )
        lines.append(f"📅 发布: {created_at}  🔄 更新: {updated_at}")
        if description:
            desc = description[:80].replace("\n", " ")
            lines.append(f"📝 简介: {desc}{'...' if len(description) > 80 else ''}")
        lines.append("━━━━━━━━━━━━━━━━━━━")
        if with_episodes:
            lines.append("💡 /picadl <ID> <章节号> 单章下载 | /picadl <ID> 整本下载")
        else:
            lines.append("💡 回复 /picainfo <ID> 查看详情")
        return "\n".join(lines)

    @staticmethod
    def format_search_results(
        comics: list, keyword: str, page: int = 1, total: int = None,
        page_size: int = 10,
    ) -> str:
        """格式化搜索结果（含翻页提示）"""
        if not comics:
            return f'🔍 未找到与 "{keyword}" 相关的结果'
        lines = [f"🔍 搜索: {keyword} (第{page}页)", "━━━━━━━━━━━━━━━━━━━"]
        for i, c in enumerate(comics, 1):
            title = c.get("title", "未知")
            if len(title) > 40:
                title = title[:40] + "..."
            lines.append(
                f"{i}. {title}\n   ID: {c.get('_id', 'N/A')} | 作者: {c.get('author', '?')} "
                f"| ❤️ {c.get('totalLikes', 0)}"
            )
        lines.append("━━━━━━━━━━━━━━━━━━━")
        # 翻页提示
        if total:
            max_page = max(1, (int(total) + page_size - 1) // page_size)
            if page < max_page:
                lines.append(
                    f"📄 共 {total} 条 / {max_page} 页 | "
                    f"下一页: /picasearch {keyword} {page + 1}"
                )
            else:
                lines.append(f"📄 已显示全部（共 {total} 条 / {page} 页）")
        lines.append("💡 回复 /picainfo <ID> 查看详情")
        return "\n".join(lines)

    @staticmethod
    def format_rank(comics: list, rank_type: str) -> str:
        """格式化排行榜"""
        if not comics:
            return "😢 排行榜为空"
        name = RANK_TYPES.get(rank_type, rank_type)
        lines = [f"🏆 哔咔排行榜 - {name}", "━━━━━━━━━━━━━━━━━━━"]
        for i, c in enumerate(comics, 1):
            title = c.get("title", "未知")
            if len(title) > 35:
                title = title[:35] + "..."
            lines.append(
                f"{i}. {title}\n   ID: {c.get('_id', 'N/A')} | ❤️ {c.get('totalLikes', 0)}"
            )
        lines.append("━━━━━━━━━━━━━━━━━━━")
        lines.append("💡 回复 /picainfo <ID> 查看详情")
        return "\n".join(lines)

    @staticmethod
    def format_favourites(comics: list, page: int = 1) -> str:
        """格式化我的收藏"""
        if not comics:
            return "📭 收藏列表为空"
        lines = [f"⭐ 我的收藏 (第{page}页)", "━━━━━━━━━━━━━━━━━━━"]
        for i, c in enumerate(comics, 1):
            title = c.get("title", "未知")
            if len(title) > 40:
                title = title[:40] + "..."
            lines.append(
                f"{i}. {title}\n   ID: {c.get('_id', 'N/A')} | 作者: {c.get('author', '?')}"
            )
        lines.append("━━━━━━━━━━━━━━━━━━━")
        lines.append("💡 回复 /picafav <ID> 取消收藏")
        return "\n".join(lines)

    @staticmethod
    def format_episodes(eps: dict, comic_id: str) -> str:
        """格式化章节列表"""
        docs = eps.get("docs", [])
        if not docs:
            return "📄 该漫画暂无章节"
        lines = [f"📄 章节列表 (共{eps.get('total', len(docs))}话)", "━━━━━━━━━━━━━━━━━━━"]
        # 显示前 20 话，含 order 与标题
        for ep in docs[:20]:
            order = ep.get("order", "?")
            title = ep.get("title", "未命名")[:30]
            lines.append(f"#{order} {title}")
        if len(docs) > 20:
            lines.append(f"... 等 {len(docs)} 话")
        lines.append("━━━━━━━━━━━━━━━━━━━")
        lines.append(f"💡 回复 /picadl {comic_id} <章节号> 下载")
        return "\n".join(lines)

    @staticmethod
    def format_categories(categories: list = None) -> str:
        """格式化分区列表"""
        cats = categories or CATEGORIES
        lines = ["🗂️ 哔咔全部分区:", "━━━━━━━━━━━━━━━━━━━"]
        lines.extend(f"· {c}" for c in cats)
        lines.append("━━━━━━━━━━━━━━━━━━━")
        lines.append("💡 回复 /picacomics <分区名> 浏览该分区")
        return "\n".join(lines)

    @staticmethod
    def help_text() -> str:
        """帮助文本"""
        return (
            "📖 哔咔漫画插件 (Pica-Comics)\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "/picasearch <关键词> [页码] - 搜索漫画\n"
            "/picainfo <ID> - 查看本子详情(含封面)\n"
            "/picaeps <ID> - 查看章节列表\n"
            "/picadl <ID> - 整本下载(后台，完成后通知)\n"
            "/picadl <ID> <章节号> - 下载单章节\n"
            "/picarank [H24|D7|D30] - 排行榜\n"
            "/picacomics <分区> [页码] - 浏览分区\n"
            "/picafav <ID> - 收藏/取消收藏\n"
            "/picamyfav [页码] - 我的收藏\n"
            "/picapunch - 每日签到\n"
            "/picalogin <邮箱> <密码> - 绑定自己的哔咔账号\n"
            "/picalogout - 解绑自己的账号\n"
            "/picastatus - 查看账号绑定状态\n"
            "/picacat - 分区列表\n"
            "/picaclean - 清理缓存\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "💡 打包格式与密码可在插件配置中调整 (pack_format/pack_password)"
        )
