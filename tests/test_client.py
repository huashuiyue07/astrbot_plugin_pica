"""client 签名/会话/上下文测试（不发起真实网络请求）"""

from core.client import PicaClient


def test_sign_deterministic_and_length():
    c = PicaClient()
    s1 = c._sign("GET", "/comics", "1700000000")
    s2 = c._sign("GET", "/comics", "1700000000")
    assert s1 == s2
    assert len(s1) == 64
    assert s1 != c._sign("POST", "/comics", "1700000000")
    assert s1 != c._sign("GET", "/comics", "1700000001")


def test_headers_include_required_fields():
    c = PicaClient()
    headers = c._headers("GET", "/comics")
    assert headers["api-key"]
    assert headers["accept"]
    assert headers["app-channel"]
    assert headers["time"]
    assert headers["nonce"]
    assert headers["signature"]
    assert "authorization" not in headers

    with_token = c._headers("GET", "/comics", token="tok123")
    assert with_token["authorization"] == "tok123"


def test_current_user_context_local():
    c = PicaClient()
    assert c.get_current_user() is None
    c.set_current_user("u123")
    assert c.get_current_user() == "u123"
    assert c._session is None  # 不发起请求时不创建会话