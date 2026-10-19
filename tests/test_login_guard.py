"""`/picalogin` 的私聊限制测试。

锁定的契约：
1. 带账号密码的绑定**只在私聊生效**，群聊直接被拒绝，不会调用登录接口；
2. 群聊里不带参数的绑定（走配置默认账号）仍然允许——它不在聊天里暴露密码；
3. 只给了邮箱或只给了密码时返回用法提示，不会误触发默认账号绑定。
"""

import asyncio

from conftest import import_plugin_main

main = import_plugin_main()
PicaPlugin = main.PicaPlugin

GROUP = "default:GroupMessage:123456"
PRIVATE = "default:FriendMessage:10000"


class _FakeAuth:
    def __init__(self):
        self.logins = []
        self.binds = []

    async def login(self, user_id, email, password):
        self.logins.append((user_id, email, password))
        return "token"

    async def bind_default(self, user_id):
        self.binds.append(user_id)
        return "token"


class _FakeEvent:
    def __init__(self, umo, sender_id="10000"):
        self.unified_msg_origin = umo
        self._sender_id = sender_id
        self.results = []

    def get_sender_id(self):
        return self._sender_id

    def plain_result(self, text):
        self.results.append(text)
        return main.MessageChain([main.Comp.Plain(text)])


def _plugin(**config):
    plugin = PicaPlugin.__new__(PicaPlugin)
    plugin.config = {"admin_only": False, **config}
    plugin.auth = _FakeAuth()
    return plugin


def _login(plugin, event, email=None, password=None):
    async def _collect():
        return [item async for item in plugin.login_command(event, email, password)]

    return asyncio.run(_collect())


def _reply(event):
    return "\n".join(event.results)


def test_group_login_with_credentials_is_rejected():
    plugin = _plugin()
    event = _FakeEvent(GROUP)

    _login(plugin, event, "a@b.c", "pw1")

    assert "私聊" in _reply(event)
    assert plugin.auth.logins == []


def test_private_login_with_credentials_works():
    plugin = _plugin()
    event = _FakeEvent(PRIVATE)

    _login(plugin, event, "a@b.c", "pw1")

    assert plugin.auth.logins == [("10000", "a@b.c", "pw1")]
    assert "绑定成功" in _reply(event)


def test_group_login_without_args_still_allowed():
    """不带参数走默认账号，聊天里没有密码，允许在群里执行"""
    plugin = _plugin()
    event = _FakeEvent(GROUP)

    _login(plugin, event)

    assert plugin.auth.binds == ["10000"]
    assert "绑定成功" in _reply(event)


def test_private_login_with_only_email_shows_usage():
    plugin = _plugin()
    event = _FakeEvent(PRIVATE)

    _login(plugin, event, "a@b.c", None)

    assert "用法" in _reply(event)
    assert plugin.auth.logins == []
    assert plugin.auth.binds == []


def test_group_login_with_only_email_shows_usage():
    plugin = _plugin()
    event = _FakeEvent(GROUP)

    _login(plugin, event, "a@b.c", None)

    assert "用法" in _reply(event)
    assert plugin.auth.logins == []
    assert plugin.auth.binds == []
