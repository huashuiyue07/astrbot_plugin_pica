"""回归测试：整本下载完成后「重复发送」的修复（v1.4.1）。

背景：上传大文件的耗时可能超过 OneBot API 调用超时（AstrBot 的 aiocqhttp
适配器把该值硬编码为 180s）。这个超时只表示客户端不再等待，napcat 侧仍会把
文件传完并投递成功 —— 此时若按「失败」重发，用户就会收到两份文件。

因此本测试锁定的行为契约是：
- 含上传类组件（图片/文件/语音/视频）的消息链**只发送一次**，无论成功还是异常；
- 纯文本消息链保留失败重试语义；
- 发送上传类消息前会把 OneBot API 超时抬高，且只增不减。
"""

import asyncio
import types

from conftest import import_plugin_main

main = import_plugin_main()
PicaPlugin = main.PicaPlugin
MessageChain = main.MessageChain
Comp = main.Comp

# 会话标识占位符：UMO 在本测试中仅作字符串透传，不参与任何校验
UMO = "default:private:10000"


# --------------------------------------------------------------------- 测试替身


class _FakeContext:
    """记录每次 send_message 调用，并按 script 依次返回/抛出结果"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def send_message(self, umo, chain):
        self.calls.append((umo, chain))
        item = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeBot:
    """模拟 aiocqhttp 的 CQHttp 实例：`bot._api._wsr_api._timeout_sec`"""

    def __init__(self, timeout=180):
        self._api = types.SimpleNamespace(
            _timeout_sec=timeout,
            _wsr_api=types.SimpleNamespace(_timeout_sec=timeout),
            _http_api=types.SimpleNamespace(_timeout_sec=timeout),
        )


class _FakeEvent:
    def __init__(self, timeout=180):
        self.bot = _FakeBot(timeout)


def _make_plugin(script, config=None):
    """绕过 __init__ 构造插件实例（只测发送逻辑，不碰真实网络/文件系统）"""
    plugin = PicaPlugin.__new__(PicaPlugin)
    plugin.context = _FakeContext(script)
    plugin.config = config if config is not None else {}
    return plugin


def _text_chain(text="hello"):
    return MessageChain([Comp.Plain(text)])


def _file_chain(path="/tmp/a.zip"):
    return MessageChain([Comp.File(name="a.zip", file=path), Comp.Plain("done")])


def _image_chain(path="/tmp/a.png"):
    return MessageChain([Comp.Image(file=path)])


# --------------------------------------------------------------------- 组件判定


def test_upload_components_are_detected():
    for chain in (_file_chain(), _image_chain()):
        assert PicaPlugin._has_upload_components(chain) is True


def test_plain_chain_is_not_an_upload():
    assert PicaPlugin._has_upload_components(_text_chain()) is False


def test_api_timeout_detection():
    assert PicaPlugin._is_api_timeout(TimeoutError("WebSocket API call timeout"))
    assert PicaPlugin._is_api_timeout(RuntimeError("请求超时"))
    assert not PicaPlugin._is_api_timeout(RuntimeError("connection reset"))


# --------------------------------------------- 核心回归：上传类消息超时后不重发


def test_upload_timeout_does_not_retry():
    """超时后只发送一次 —— 这正是线上「收到两份本子」的根因"""
    plugin = _make_plugin([TimeoutError("WebSocket API call timeout"), True])

    result = asyncio.run(plugin._send_with_retry(_FakeEvent(), UMO, _file_chain()))

    assert result is None, "上传超时应返回 None（结果不确定），而不是失败"
    assert len(plugin.context.calls) == 1, "上传类消息绝不能被重发"


def test_upload_image_timeout_does_not_retry():
    plugin = _make_plugin([TimeoutError("WebSocket API call timeout"), True])
    asyncio.run(plugin._send_with_retry(_FakeEvent(), UMO, _image_chain()))
    assert len(plugin.context.calls) == 1


def test_upload_hard_failure_does_not_retry():
    """上传类消息即使遇到非超时异常也只尝试一次（避免重复投递）"""
    plugin = _make_plugin([RuntimeError("rich media transfer failed")])
    result = asyncio.run(plugin._send_with_retry(_FakeEvent(), UMO, _file_chain()))
    assert result is False
    assert len(plugin.context.calls) == 1


def test_upload_success_sends_once():
    plugin = _make_plugin([True])
    assert asyncio.run(plugin._send_with_retry(_FakeEvent(), UMO, _file_chain())) is True
    assert len(plugin.context.calls) == 1


def test_upload_raises_api_timeout_before_sending():
    event = _FakeEvent(timeout=180)
    plugin = _make_plugin([True], config={"send_api_timeout": 900})
    asyncio.run(plugin._send_with_retry(event, UMO, _file_chain()))
    assert event.bot._api._wsr_api._timeout_sec == 900


# ------------------------------------------------ 纯文本路径保持原有重试语义


def test_text_still_retries_on_failure():
    plugin = _make_plugin([TimeoutError("WebSocket API call timeout"), True])
    assert asyncio.run(plugin._send_with_retry(_FakeEvent(), UMO, _text_chain())) is True
    assert len(plugin.context.calls) == 2, "纯文本消息仍应重试一次"


def test_text_gives_up_after_retries():
    plugin = _make_plugin([RuntimeError("boom")])
    assert asyncio.run(plugin._send_with_retry(_FakeEvent(), UMO, _text_chain())) is False
    assert len(plugin.context.calls) == 3, "默认重试 2 次，共 3 次尝试"


def test_text_does_not_touch_api_timeout():
    event = _FakeEvent(timeout=180)
    plugin = _make_plugin([True], config={"send_api_timeout": 900})
    asyncio.run(plugin._send_with_retry(event, UMO, _text_chain()))
    assert event.bot._api._wsr_api._timeout_sec == 180, "纯文本无需调整 API 超时"


# ------------------------------------------------------------ API 超时抬高逻辑


def test_ensure_api_timeout_only_raises():
    event = _FakeEvent(timeout=180)
    plugin = _make_plugin([True], config={"send_api_timeout": 900})
    plugin._ensure_api_timeout(event)
    assert event.bot._api._timeout_sec == 900
    assert event.bot._api._wsr_api._timeout_sec == 900
    assert event.bot._api._http_api._timeout_sec == 900


def test_ensure_api_timeout_never_lowers():
    """只增不减 —— 并发发送时不会把别人已抬高的超时改小"""
    event = _FakeEvent(timeout=1200)
    plugin = _make_plugin([True], config={"send_api_timeout": 900})
    plugin._ensure_api_timeout(event)
    assert event.bot._api._wsr_api._timeout_sec == 1200


def test_ensure_api_timeout_disabled_by_zero():
    event = _FakeEvent(timeout=180)
    plugin = _make_plugin([True], config={"send_api_timeout": 0})
    plugin._ensure_api_timeout(event)
    assert event.bot._api._wsr_api._timeout_sec == 180


def test_ensure_api_timeout_tolerates_missing_bot():
    """非 onebot 平台（无 event.bot）或结构变动时静默跳过，不应抛异常"""
    plugin = _make_plugin([True], config={"send_api_timeout": 900})
    plugin._ensure_api_timeout(None)
    plugin._ensure_api_timeout(types.SimpleNamespace())
    plugin._ensure_api_timeout(types.SimpleNamespace(bot=None))
