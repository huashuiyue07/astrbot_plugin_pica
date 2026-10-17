"""排行榜订阅（/picasub）与定时推送（rank_cron_*）测试。

锁定的行为契约：
1. `/picasub [H24|D7|D30]` 为当前会话添加订阅，缺省日榜；`list` 查看、`off` 取消；
2. 订阅时自检刷新时间：未配置 / cron 非法都会在回复里明确提示；
3. 定时任务只在该类型 cron 命中的那一分钟推送一次，且只推给订阅了该类型的会话；
4. 推送账号优先用订阅者自己绑定的账号，其次配置的默认账号；都没有则跳过并记日志；
5. 推送内容 = 榜单文本 [+ 新上榜高亮]，与手动 /picarank 共享同一份快照；
6. 定时推送同样受 rank_cover_pdf 开关控制。
"""

import asyncio
import io
from datetime import datetime

import pytest
from PIL import Image

from conftest import import_plugin_main

main = import_plugin_main()
PicaPlugin = main.PicaPlugin
MessageChain = main.MessageChain
Comp = main.Comp
# 注意：conftest 把插件载入为 astrbot_plugin_pica.core.*，与直接 import 的顶层 core.*
# 是两份模块对象，异常类因此也不同；这里统一从 main 取，保证与插件内部一致。
PicaError = main.PicaError
PicaDownloader = main.PicaDownloader
RankHistoryStore = main.RankHistoryStore
RankSubscriptionStore = main.RankSubscriptionStore

UMO_A = "default:group:100"
UMO_B = "default:group:200"
UMO_C = "default:group:300"

MONDAY = datetime(2026, 8, 3, 9, 0)  # 周一 09:00
MONDAY_LATE = datetime(2026, 8, 3, 9, 1)
TUESDAY = datetime(2026, 8, 4, 9, 0)

H24_ON_SCHEDULE = {"rank_cron_h24": "0 9 * * *"}


# --------------------------------------------------------------------- 测试替身


def _comics(*ids, with_thumb=False):
    comics = []
    for cid in ids:
        comic = {"_id": cid, "title": f"本子{cid}", "totalLikes": 5}
        if with_thumb:
            comic["thumb"] = {
                "fileServer": "https://fs.picacomic.com",
                "path": f"{cid}.jpg",
            }
        comics.append(comic)
    return comics


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (12, 12), (10, 200, 10)).save(buf, format="JPEG")
    return buf.getvalue()


def _plain_text(comp) -> str:
    args = getattr(comp, "args", ())
    return str(args[0]) if args else str(getattr(comp, "kwargs", {}).get("text", ""))


class _FakeClient:
    def __init__(self, boards=None, fail=False):
        self.boards = boards or {}
        self.fail = fail
        self.fetches = []  # (tt, token)
        self.user_id = None

    async def leaderboard(self, tt="H24", token=None):
        self.fetches.append((tt, token))
        if self.fail:
            raise RuntimeError("leaderboard 挂了")
        return list(self.boards.get(tt, []))

    def set_current_user(self, user_id):
        self.user_id = user_id

    async def _download(self, url, token=None, timeout=60, _retry=0):
        return _jpeg()

    async def aclose(self):
        self.closed = True


class _FakeAuth:
    """bound: 已绑定用户的 token；default: 配置的默认账号 token"""

    def __init__(self, bound=None, default=None, default_fallback=False):
        self.bound = dict(bound or {})
        self.default = default
        self.default_fallback = default_fallback
        self.default_calls = 0

    async def ensure_login(self, user_id):
        if user_id in self.bound:
            return self.bound[user_id]
        if self.default_fallback and self.default is not None:
            return self.default
        raise PicaError(f"用户 {user_id} 未绑定账号")

    async def ensure_default_login(self):
        self.default_calls += 1
        if self.default is None:
            raise PicaError("未配置默认账号")
        return self.default


class _FakeContext:
    def __init__(self, fail_for=(), false_for=()):
        self.calls = []  # (umo, chain)
        self.fail_for = set(fail_for)
        # 模拟「找不到平台实例」：Context.send_message 返回 False 而不抛异常
        self.false_for = set(false_for)

    async def send_message(self, umo, chain):
        self.calls.append((umo, chain))
        if umo in self.fail_for:
            raise RuntimeError("send failed")
        if umo in self.false_for:
            return False
        return True

    def texts_for(self, umo):
        return [
            _plain_text(comp)
            for target, chain in self.calls
            if target == umo
            for comp in chain.chain
            if isinstance(comp, Comp.Plain)
        ]

    def files_for(self, umo):
        return [
            comp
            for target, chain in self.calls
            if target == umo
            for comp in chain.chain
            if isinstance(comp, Comp.File)
        ]


class _FakeEvent:
    def __init__(self, umo=UMO_A, sender_id="10000"):
        self._sender_id = sender_id
        self.unified_msg_origin = umo
        self.bot = None
        self.results = []

    def get_sender_id(self):
        return self._sender_id

    def plain_result(self, text):
        self.results.append(text)
        return MessageChain([Comp.Plain(text)])

    def chain_result(self, chain):
        self.results.append("")
        return MessageChain(list(chain))


def _make_plugin(
    tmp_path, config=None, boards=None, bound=None, default=None, fail=False
):
    plugin = PicaPlugin.__new__(PicaPlugin)
    # 本文件只测订阅语义；权限门槛见 test_sub_permissions.py
    plugin.config = {"sub_admin_only": False, **(config or {})}
    plugin.context = _FakeContext()
    plugin.data_dir = tmp_path
    plugin.auth = _FakeAuth(bound, default)
    plugin.client = _FakeClient(boards, fail)
    plugin.downloader = PicaDownloader(plugin.client, tmp_path)
    plugin.rank_history = RankHistoryStore(tmp_path)
    plugin.subs = RankSubscriptionStore(tmp_path)
    plugin._rank_fired = {}
    plugin._cron_warned = {}
    return plugin


def _run_sub(plugin, event, arg=None):
    async def _collect():
        return [item async for item in plugin.subscribe_command(event, arg)]

    return asyncio.run(_collect())


def _run_rank(plugin, event, tt="H24"):
    async def _collect():
        return [item async for item in plugin.rank_command(event, tt)]

    return asyncio.run(_collect())


def _fire(plugin, now=MONDAY):
    return asyncio.run(plugin._run_rank_subscriptions(now))


def _reply(event):
    return event.results[-1]


# ------------------------------------------------------------------ /picasub 命令


def test_picasub_defaults_to_h24(tmp_path):
    plugin = _make_plugin(tmp_path, H24_ON_SCHEDULE)

    event = _FakeEvent()
    _run_sub(plugin, event)

    assert plugin.subs.targets(UMO_A) == ["H24"]
    assert "已为本群订阅「日榜(24H)」" in _reply(event)
    assert "0 9 * * *" in _reply(event)


def test_picasub_accepts_rank_type_case_insensitively(tmp_path):
    plugin = _make_plugin(tmp_path, {"rank_cron_d7": "0 9 * * 1"})

    event = _FakeEvent()
    _run_sub(plugin, event, "d7")

    assert plugin.subs.targets(UMO_A) == ["D7"]
    assert "周榜(7D)" in _reply(event)
    assert "0 9 * * 1" in _reply(event)


def test_picasub_is_idempotent(tmp_path):
    plugin = _make_plugin(tmp_path, H24_ON_SCHEDULE)
    _run_sub(plugin, _FakeEvent())

    event = _FakeEvent()
    _run_sub(plugin, event)

    assert plugin.subs.targets(UMO_A) == ["H24"]
    assert "早已订阅" in _reply(event)


def test_picasub_invalid_arg_shows_usage_without_subscribing(tmp_path):
    """打错字不能静默订阅日榜 —— 参数非法时报用法，且不写持久化订阅"""
    plugin = _make_plugin(tmp_path, H24_ON_SCHEDULE)

    event = _FakeEvent()
    _run_sub(plugin, event, "不存在的榜单")

    assert "用法" in _reply(event)
    assert plugin.subs.targets(UMO_A) == []
    assert plugin.subs.all() == {}


def test_picasub_without_arg_still_subscribes_h24(tmp_path):
    """无参数仍是订阅日榜（与 /picarank 的默认一致）"""
    plugin = _make_plugin(tmp_path, H24_ON_SCHEDULE)

    _run_sub(plugin, _FakeEvent())

    assert plugin.subs.targets(UMO_A) == ["H24"]


def test_picasub_list_shows_all_types(tmp_path):
    plugin = _make_plugin(tmp_path, H24_ON_SCHEDULE)
    _run_sub(plugin, _FakeEvent(), "D7")

    event = _FakeEvent()
    _run_sub(plugin, event, "list")

    reply = _reply(event)
    assert "✅ 周榜(7D)" in reply
    assert "➖ 日榜(24H)" in reply
    assert "➖ 月榜(30D)" in reply


def test_picasub_off_clears_subscriptions(tmp_path):
    plugin = _make_plugin(tmp_path, H24_ON_SCHEDULE)
    _run_sub(plugin, _FakeEvent(), "H24")
    _run_sub(plugin, _FakeEvent(), "D7")

    event = _FakeEvent()
    _run_sub(plugin, event, "off")

    assert plugin.subs.targets(UMO_A) == []
    assert "已取消本群的排行榜订阅" in _reply(event)

    event2 = _FakeEvent()
    _run_sub(plugin, event2, "off")
    assert "没有排行榜订阅" in _reply(event2)


def test_picasub_warns_when_cron_missing(tmp_path):
    plugin = _make_plugin(tmp_path, {})

    event = _FakeEvent()
    _run_sub(plugin, event)

    assert "未配置刷新时间" in _reply(event)
    assert "rank_cron_h24" in _reply(event)


def test_picasub_warns_when_cron_invalid(tmp_path):
    plugin = _make_plugin(tmp_path, {"rank_cron_h24": "不是 cron"})

    event = _FakeEvent()
    _run_sub(plugin, event)

    assert "刷新时间配置有误" in _reply(event)


def test_picasub_denied_in_admin_only_mode(tmp_path):
    plugin = _make_plugin(
        tmp_path, {"admin_only": True, "admin_ids": "999", **H24_ON_SCHEDULE}
    )

    event = _FakeEvent(sender_id="10000")
    _run_sub(plugin, event)

    assert "没有权限" in _reply(event)
    assert plugin.subs.targets(UMO_A) == []


# ------------------------------------------------------------------ 定时推送


def test_cron_fire_pushes_to_subscribed_group(tmp_path):
    plugin = _make_plugin(
        tmp_path,
        H24_ON_SCHEDULE,
        boards={"H24": _comics("a", "b")},
        bound={"u1": "tk-owner"},
    )
    plugin.subs.add(UMO_A, "H24", "u1")

    assert _fire(plugin) == 1

    assert any("🏆 哔咔排行榜" in text for text in plugin.context.texts_for(UMO_A))
    assert plugin.client.fetches == [("H24", "tk-owner")]
    assert plugin.client.user_id == "u1"


def test_off_schedule_does_not_push(tmp_path):
    plugin = _make_plugin(
        tmp_path, H24_ON_SCHEDULE, boards={"H24": _comics("a")}, bound={"u1": "tk"}
    )
    plugin.subs.add(UMO_A, "H24", "u1")

    assert _fire(plugin, MONDAY_LATE) == 0

    assert plugin.context.calls == []
    assert plugin.client.fetches == []


def test_same_minute_fires_only_once(tmp_path):
    plugin = _make_plugin(
        tmp_path, H24_ON_SCHEDULE, boards={"H24": _comics("a")}, bound={"u1": "tk"}
    )
    plugin.subs.add(UMO_A, "H24", "u1")

    assert _fire(plugin, MONDAY) == 1
    calls_after_first = len(plugin.context.calls)

    assert _fire(plugin, MONDAY) == 0
    assert len(plugin.context.calls) == calls_after_first


def test_next_day_fires_again(tmp_path):
    plugin = _make_plugin(
        tmp_path, H24_ON_SCHEDULE, boards={"H24": _comics("a")}, bound={"u1": "tk"}
    )
    plugin.subs.add(UMO_A, "H24", "u1")

    assert _fire(plugin, MONDAY) == 1
    assert _fire(plugin, TUESDAY) == 1


def test_no_subscriber_means_no_fetch(tmp_path):
    plugin = _make_plugin(tmp_path, H24_ON_SCHEDULE, boards={"H24": _comics("a")})

    assert _fire(plugin) == 0

    assert plugin.client.fetches == []


def test_empty_cron_disables_push(tmp_path):
    plugin = _make_plugin(
        tmp_path, {"rank_cron_h24": ""}, boards={"H24": _comics("a")}, bound={"u1": "tk"}
    )
    plugin.subs.add(UMO_A, "H24", "u1")

    assert _fire(plugin) == 0

    assert plugin.client.fetches == []


def test_invalid_cron_is_ignored_and_warned_once(tmp_path):
    plugin = _make_plugin(
        tmp_path,
        {"rank_cron_h24": "坏表达式"},
        boards={"H24": _comics("a")},
        bound={"u1": "tk"},
    )
    plugin.subs.add(UMO_A, "H24", "u1")

    assert _fire(plugin) == 0
    assert _fire(plugin, TUESDAY) == 0

    assert plugin._cron_warned == {"H24": "坏表达式"}
    assert plugin.client.fetches == []


def test_push_only_reaches_matching_subscribers(tmp_path):
    plugin = _make_plugin(
        tmp_path,
        H24_ON_SCHEDULE,
        boards={"H24": _comics("a"), "D7": _comics("b")},
        bound={"u1": "tk", "u2": "tk"},
    )
    plugin.subs.add(UMO_A, "H24", "u1")
    plugin.subs.add(UMO_B, "D7", "u2")

    assert _fire(plugin) == 1

    assert plugin.context.texts_for(UMO_A)
    assert plugin.context.texts_for(UMO_B) == []


def test_second_fire_highlights_new_entry(tmp_path):
    plugin = _make_plugin(
        tmp_path,
        H24_ON_SCHEDULE,
        boards={"H24": _comics("a", "b")},
        bound={"u1": "tk"},
    )
    plugin.subs.add(UMO_A, "H24", "u1")
    _fire(plugin, MONDAY)

    plugin.client.boards["H24"] = _comics("a", "new", "b")
    _fire(plugin, TUESDAY)

    highlights = [
        text for text in plugin.context.texts_for(UMO_A) if "新上榜" in text
    ]
    assert len(highlights) == 1
    assert "本子new" in highlights[0]


def test_highlight_disabled_in_push_but_snapshot_updated(tmp_path):
    plugin = _make_plugin(
        tmp_path,
        {**H24_ON_SCHEDULE, "rank_new_highlight": False},
        boards={"H24": _comics("a", "b")},
        bound={"u1": "tk"},
    )
    plugin.subs.add(UMO_A, "H24", "u1")
    _fire(plugin, MONDAY)

    plugin.client.boards["H24"] = _comics("a", "new", "b")
    _fire(plugin, TUESDAY)

    assert not any("新上榜" in text for text in plugin.context.texts_for(UMO_A))
    assert [item["_id"] for item in plugin.rank_history.load("H24")["items"]] == [
        "a",
        "new",
        "b",
    ]


def test_manual_rank_shares_snapshot_with_push(tmp_path):
    """/picarank 与定时推送共用同一份快照，不会互相重复报「新上榜」"""
    plugin = _make_plugin(
        tmp_path,
        H24_ON_SCHEDULE,
        boards={"H24": _comics("a", "b")},
        bound={"10000": "tk-user", "u1": "tk"},
    )
    plugin.subs.add(UMO_A, "H24", "u1")
    _run_rank(plugin, _FakeEvent())

    _fire(plugin, MONDAY)

    assert not any("新上榜" in text for text in plugin.context.texts_for(UMO_A))


def test_push_falls_back_to_default_account(tmp_path):
    plugin = _make_plugin(
        tmp_path,
        H24_ON_SCHEDULE,
        boards={"H24": _comics("a")},
        bound={},
        default="tk-default",
    )
    plugin.subs.add(UMO_A, "H24", "u1")

    assert _fire(plugin) == 1

    assert plugin.client.fetches == [("H24", "tk-default")]
    assert plugin.client.user_id == "__default__"


def test_push_skipped_without_any_account(tmp_path):
    plugin = _make_plugin(
        tmp_path, H24_ON_SCHEDULE, boards={"H24": _comics("a")}, bound={}, default=None
    )
    plugin.subs.add(UMO_A, "H24", "u1")

    assert _fire(plugin) == 0

    assert plugin.context.calls == []
    assert plugin.client.fetches == []


def test_leaderboard_error_does_not_break_scheduler(tmp_path):
    plugin = _make_plugin(
        tmp_path, H24_ON_SCHEDULE, boards={"H24": []}, bound={"u1": "tk"}, fail=True
    )
    plugin.subs.add(UMO_A, "H24", "u1")

    assert _fire(plugin) == 0


def test_partial_send_failure_counts_only_delivered(tmp_path):
    """一个会话发送失败时，只把真正提交成功的会话计入推送数"""
    plugin = _make_plugin(
        tmp_path,
        H24_ON_SCHEDULE,
        boards={"H24": _comics("a")},
        bound={"u1": "tk"},
    )
    plugin.subs.add(UMO_A, "H24", "u1")
    plugin.subs.add(UMO_B, "H24", "u1")
    plugin.context = _FakeContext(fail_for={UMO_A})

    assert _fire(plugin) == 1

    assert plugin.context.texts_for(UMO_B)


def test_empty_board_is_not_broadcast(tmp_path):
    """空榜单（HTTP 200 但无数据）不该广播给所有订阅群"""
    plugin = _make_plugin(
        tmp_path, H24_ON_SCHEDULE, boards={"H24": []}, bound={"u1": "tk"}
    )
    plugin.subs.add(UMO_A, "H24", "u1")

    assert _fire(plugin) == 0

    assert plugin.context.calls == [], "空榜单不应发出任何消息"
    assert plugin.client.fetches, "仍应拉取过一次榜单"


def test_notify_returns_true_on_success(tmp_path):
    plugin = _make_plugin(tmp_path, H24_ON_SCHEDULE)

    assert asyncio.run(plugin._notify(UMO_A, "hi")) is True


def test_notify_returns_false_when_platform_missing(tmp_path):
    """Context.send_message 返回 False（找不到平台实例）时不能算成功"""
    plugin = _make_plugin(tmp_path, H24_ON_SCHEDULE)
    plugin.context = _FakeContext(false_for={UMO_A})

    assert asyncio.run(plugin._notify(UMO_A, "hi")) is False


def test_push_outcome_failed_property():
    PushOutcome = main.PushOutcome

    assert PushOutcome(attempted=3, delivered=3).failed == 0
    assert PushOutcome(attempted=3, delivered=1).failed == 2
    # 整批跳过时没有真的尝试，不算失败
    assert PushOutcome(attempted=3, delivered=0, skipped="榜单为空").failed == 0


def test_push_sends_cover_pdf_when_enabled(tmp_path):
    pytest.importorskip("fitz")
    plugin = _make_plugin(
        tmp_path,
        {**H24_ON_SCHEDULE, "rank_cover_pdf": True},
        boards={"H24": _comics("a", "b", with_thumb=True)},
        bound={"u1": "tk"},
    )
    plugin.subs.add(UMO_A, "H24", "u1")

    assert _fire(plugin) == 1

    assert len(plugin.context.files_for(UMO_A)) == 1


def test_cover_pdf_disabled_by_default_in_push(tmp_path):
    plugin = _make_plugin(
        tmp_path,
        H24_ON_SCHEDULE,
        boards={"H24": _comics("a", "b", with_thumb=True)},
        bound={"u1": "tk"},
    )
    plugin.subs.add(UMO_A, "H24", "u1")

    assert _fire(plugin) == 1

    assert plugin.context.files_for(UMO_A) == []


def test_push_builds_cover_pdf_once_for_all_groups(tmp_path):
    """N 个订阅会话只打包一次 PDF，逐个会话复用同一份文件"""
    pytest.importorskip("fitz")
    plugin = _make_plugin(
        tmp_path,
        {**H24_ON_SCHEDULE, "rank_cover_pdf": True},
        boards={"H24": _comics("a", "b", with_thumb=True)},
        bound={"u1": "tk"},
    )
    plugin.subs.add(UMO_A, "H24", "u1")
    plugin.subs.add(UMO_B, "H24", "u1")

    builds = []
    real_build = plugin._build_rank_cover_pdf

    async def _counting_build(comics, token, tt):
        builds.append(tt)
        return await real_build(comics, token, tt)

    plugin._build_rank_cover_pdf = _counting_build

    assert _fire(plugin) == 2

    assert builds == ["H24"], "只应构建一次封面 PDF"
    files_a = plugin.context.files_for(UMO_A)
    files_b = plugin.context.files_for(UMO_B)
    assert len(files_a) == 1 and len(files_b) == 1
    assert files_a[0].kwargs["file"] == files_b[0].kwargs["file"], "两个群复用同一份 PDF"


# ------------------------------------------------------------- 后台任务生命周期


def test_spawn_task_without_running_loop_returns_none():
    """没有事件循环时（如被 import 而非加载）只记日志，不抛异常"""

    async def _never_run():
        raise AssertionError("不应被调度")

    assert PicaPlugin._spawn_task(_never_run, "测试") is None


def test_spawn_task_creates_task_inside_loop():
    async def _main():
        async def _job():
            return "done"

        task = PicaPlugin._spawn_task(_job, "测试")
        assert task is not None
        assert await task == "done"

    asyncio.run(_main())


def test_terminate_cancels_background_tasks(tmp_path):
    plugin = _make_plugin(tmp_path)

    async def _main():
        plugin._clean_task = asyncio.create_task(asyncio.sleep(30))
        plugin._sub_task = asyncio.create_task(asyncio.sleep(30))
        await plugin.terminate()
        assert plugin._clean_task.cancelled()
        assert plugin._sub_task.cancelled()
        assert plugin.client.closed is True

    asyncio.run(_main())
