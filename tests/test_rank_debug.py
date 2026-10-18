"""调试命令 `/picadebug` 测试：无视 cron 时间触发订阅推送。

锁定的行为契约：
1. 只允许 `admin_ids` 里的用户使用（未配置 admin_ids 时直接拒绝）；
2. 无视 cron 时间：定时任务在非命中时刻推 0 个会话，`/picadebug` 仍能推送；
3. 不带参数 = 全部榜单，也可用 `H24|D7|D30` 只触发某一类型；
4. **不**消耗定时推送的「本分钟已触发」标记，定时推送照常触发；
5. 触发同样会写入榜单快照，随后的定时推送不会重复提醒新上榜；
6. 无人订阅 / 无可用账号 / 参数非法都有明确提示。
"""

import asyncio
from datetime import datetime

from conftest import import_plugin_main

main = import_plugin_main()
PicaPlugin = main.PicaPlugin
MessageChain = main.MessageChain
Comp = main.Comp
# 与插件内部保持同一份模块对象（conftest 载入的是 astrbot_plugin_pica.core.*）
PicaError = main.PicaError
PicaDownloader = main.PicaDownloader
RankHistoryStore = main.RankHistoryStore
RankSubscriptionStore = main.RankSubscriptionStore

UMO_A = "default:group:100"
UMO_B = "default:group:200"

ADMIN_ID = "10000"
# 调试命令的权限来自 admin_ids；两个榜单各给一个 cron 供定时路径使用
BASE_CONFIG = {
    "admin_ids": ADMIN_ID,
    "rank_cron_h24": "0 9 * * *",
    "rank_cron_d7": "0 9 * * 1",
}

MONDAY_9 = datetime(2026, 8, 3, 9, 0)  # 命中 rank_cron_h24 / rank_cron_d7
FRIDAY_1530 = datetime(2026, 8, 7, 15, 30)  # 两个 cron 都不命中


# --------------------------------------------------------------------- 测试替身


def _comics(*ids):
    return [{"_id": cid, "title": f"本子{cid}", "totalLikes": 5} for cid in ids]


class _FakeClient:
    def __init__(self, boards=None):
        self.boards = boards or {}
        self.fetches = []
        self.user_id = None

    async def leaderboard(self, tt="H24", token=None):
        self.fetches.append((tt, token))
        return list(self.boards.get(tt, []))

    def set_current_user(self, user_id):
        self.user_id = user_id


class _FakeAuth:
    def __init__(self, bound=None, default=None):
        self.bound = dict(bound or {})
        self.default = default

    async def ensure_login(self, user_id):
        if user_id in self.bound:
            return self.bound[user_id]
        raise PicaError(f"用户 {user_id} 未绑定账号")

    async def ensure_default_login(self):
        if self.default is None:
            raise PicaError("未配置默认账号")
        return self.default


class _FakeContext:
    def __init__(self, fail_for=()):
        self.calls = []
        self.fail_for = set(fail_for)

    async def send_message(self, umo, chain):
        self.calls.append((umo, chain))
        if umo in self.fail_for:
            raise RuntimeError("send failed")
        return True

    def texts_for(self, umo):
        return [
            _plain_text(comp)
            for target, chain in self.calls
            if target == umo
            for comp in chain.chain
            if isinstance(comp, Comp.Plain)
        ]


def _plain_text(comp) -> str:
    args = getattr(comp, "args", ())
    return str(args[0]) if args else str(getattr(comp, "kwargs", {}).get("text", ""))


class _FakeEvent:
    def __init__(self, sender_id=ADMIN_ID):
        self._sender_id = sender_id
        self.unified_msg_origin = UMO_A
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


def _make_plugin(tmp_path, config=None, boards=None, bound=None, default=None):
    plugin = PicaPlugin.__new__(PicaPlugin)
    plugin.config = config if config is not None else {}
    plugin.context = _FakeContext()
    plugin.data_dir = tmp_path
    plugin.auth = _FakeAuth(bound, default)
    plugin.client = _FakeClient(boards)
    plugin.downloader = PicaDownloader(plugin.client, tmp_path)
    plugin.rank_history = RankHistoryStore(tmp_path)
    plugin.subs = RankSubscriptionStore(tmp_path)
    plugin._rank_fired = {}
    plugin._cron_warned = {}
    return plugin


def _run_debug(plugin, event, arg=None):
    async def _collect():
        return [item async for item in plugin.debug_command(event, arg)]

    return asyncio.run(_collect())


def _fire(plugin, now):
    return asyncio.run(plugin._run_rank_subscriptions(now))


def _all(event):
    return "\n".join(event.results)


def _two_group_plugin(tmp_path, config=None):
    """A 群订阅日榜、B 群订阅周榜，账号可用"""
    plugin = _make_plugin(
        tmp_path,
        config if config is not None else BASE_CONFIG,
        boards={"H24": _comics("a"), "D7": _comics("b")},
        bound={"u1": "tk-owner"},
    )
    plugin.subs.add(UMO_A, "H24", "u1")
    plugin.subs.add(UMO_B, "D7", "u1")
    return plugin


# --------------------------------------------------------------------- 权限


def test_debug_denied_without_admin_ids(tmp_path):
    plugin = _two_group_plugin(tmp_path, {"rank_cron_h24": "0 9 * * *"})

    event = _FakeEvent()
    _run_debug(plugin, event)

    assert "admin_ids" in _all(event)
    assert plugin.context.calls == []


def test_debug_denied_for_non_admin(tmp_path):
    plugin = _two_group_plugin(tmp_path)

    event = _FakeEvent(sender_id="99999")
    _run_debug(plugin, event)

    assert "没有权限" in _all(event)
    assert plugin.context.calls == []


def test_debug_denied_in_admin_only_mode(tmp_path):
    config = {
        **BASE_CONFIG,
        "admin_only": True,
        "admin_ids": "777",
    }
    plugin = _two_group_plugin(tmp_path, config)

    event = _FakeEvent(sender_id=ADMIN_ID)
    _run_debug(plugin, event)

    assert "没有权限" in _all(event)
    assert plugin.context.calls == []


def test_admin_only_without_admin_ids_explains_what_is_missing(tmp_path):
    plugin = _two_group_plugin(tmp_path, {"admin_only": True, "admin_ids": ""})

    event = _FakeEvent(sender_id=ADMIN_ID)
    _run_debug(plugin, event)

    assert "未配置 admin_ids" in _all(event)
    assert plugin.context.calls == []


def test_admin_ids_are_parsed_from_comma_separated_config(tmp_path):
    plugin = _make_plugin(tmp_path, {"admin_ids": " 1, 2 ,,3 "})

    assert plugin._admin_ids() == ["1", "2", "3"]


# --------------------------------------------------------------------- 触发


def test_debug_pushes_all_types_ignoring_schedule(tmp_path):
    plugin = _two_group_plugin(tmp_path)

    # 定时任务在这个时刻不命中任何 cron
    assert _fire(plugin, FRIDAY_1530) == 0

    event = _FakeEvent()
    _run_debug(plugin, event)

    reply = _all(event)
    assert "✅ 日榜(24H): 已推送给 1 个会话" in reply
    assert "✅ 周榜(7D): 已推送给 1 个会话" in reply
    assert "➖ 月榜(30D): 无人订阅" in reply
    assert "共推送 2 个会话" in reply
    assert any("🏆 哔咔排行榜" in text for text in plugin.context.texts_for(UMO_A))
    assert any("🏆 哔咔排行榜" in text for text in plugin.context.texts_for(UMO_B))


def test_debug_can_target_single_type(tmp_path):
    plugin = _two_group_plugin(tmp_path)

    event = _FakeEvent()
    _run_debug(plugin, event, "d7")

    assert plugin.context.texts_for(UMO_A) == []
    assert plugin.context.texts_for(UMO_B)
    assert "✅ 周榜(7D)" in _all(event)
    assert "日榜" not in _all(event)


def test_debug_all_keyword_triggers_every_type(tmp_path):
    plugin = _two_group_plugin(tmp_path)

    event = _FakeEvent()
    _run_debug(plugin, event, "all")

    assert plugin.context.texts_for(UMO_A)
    assert plugin.context.texts_for(UMO_B)


def test_debug_matching_cron_time_also_works(tmp_path):
    plugin = _two_group_plugin(tmp_path)

    event = _FakeEvent()
    _run_debug(plugin, event)

    assert plugin.context.texts_for(UMO_A)


def test_debug_reports_when_nobody_subscribed(tmp_path):
    plugin = _make_plugin(tmp_path, BASE_CONFIG, boards={"H24": _comics("a")})

    event = _FakeEvent()
    _run_debug(plugin, event)

    assert "无人订阅" in _all(event)
    assert "共推送 0 个会话" in _all(event)


def test_debug_reports_skipped_without_account(tmp_path):
    """没有可用账号属于「整批跳过」，不该谎报成「推送失败」"""
    plugin = _make_plugin(
        tmp_path, BASE_CONFIG, boards={"H24": _comics("a")}, bound={}, default=None
    )
    plugin.subs.add(UMO_A, "H24", "u1")

    event = _FakeEvent()
    _run_debug(plugin, event)

    assert "已跳过" in _all(event)
    assert "没有可用的哔咔账号" in _all(event)
    assert "共推送 0 个会话" in _all(event)


def test_debug_reports_empty_board_as_skipped(tmp_path):
    """空榜单时应跳过并在回执里说明原因，而不是发一条「排行榜为空」给所有群"""
    plugin = _make_plugin(
        tmp_path,
        BASE_CONFIG,
        boards={"H24": []},
        bound={"u1": "tk"},
    )
    plugin.subs.add(UMO_A, "H24", "u1")

    event = _FakeEvent()
    _run_debug(plugin, event, "H24")

    assert "已跳过（榜单为空）" in _all(event)
    assert plugin.context.calls == [], "空榜单不应发出任何消息"
    assert "共推送 0 个会话" in _all(event)


def test_debug_reports_partial_delivery(tmp_path):
    """部分会话失败时，回执要给出「已推送 x/y」而不是笼统的成功/失败"""
    plugin = _make_plugin(
        tmp_path,
        BASE_CONFIG,
        boards={"H24": _comics("a")},
        bound={"u1": "tk"},
    )
    plugin.subs.add(UMO_A, "H24", "u1")
    plugin.subs.add(UMO_B, "H24", "u1")
    plugin.context = _FakeContext(fail_for={UMO_A})

    event = _FakeEvent()
    _run_debug(plugin, event, "H24")

    assert "已推送给 1/2 个会话" in _all(event)
    assert "共推送 1 个会话" in _all(event)


def test_debug_invalid_arg_shows_usage(tmp_path):
    plugin = _two_group_plugin(tmp_path)

    event = _FakeEvent()
    _run_debug(plugin, event, "不存在的榜单")

    assert "用法" in _all(event)
    assert plugin.context.calls == []
    assert plugin.client.fetches == []


def test_debug_does_not_consume_scheduled_slot(tmp_path):
    """调试触发不应把「本分钟已触发」标记用掉，定时推送照常触发"""
    plugin = _two_group_plugin(tmp_path)
    _run_debug(plugin, _FakeEvent())

    assert plugin._rank_fired == {}
    assert _fire(plugin, MONDAY_9) == 2


def test_debug_run_prevents_duplicate_new_highlight(tmp_path):
    """调试触发同样写快照：随后同一榜单的定时推送不再重复提醒新上榜"""
    plugin = _make_plugin(
        tmp_path,
        BASE_CONFIG,
        boards={"H24": _comics("a")},
        bound={"u1": "tk"},
    )
    plugin.subs.add(UMO_A, "H24", "u1")
    _run_debug(plugin, _FakeEvent())

    plugin.client.boards["H24"] = _comics("a", "new")
    _run_debug(plugin, _FakeEvent())
    assert any("新上榜" in text for text in plugin.context.texts_for(UMO_A))

    sent_before = len(plugin.context.texts_for(UMO_A))
    _fire(plugin, MONDAY_9)
    sent_after = plugin.context.texts_for(UMO_A)
    assert not any("新上榜" in text for text in sent_after[sent_before:])
