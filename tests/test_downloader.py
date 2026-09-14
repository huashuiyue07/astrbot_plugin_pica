"""downloader 纯函数测试"""

from core.downloader import (
    _modify_md5,
    _safe_name,
    build_image_url,
    pick_image_path,
)


def test_safe_name():
    assert _safe_name("hel/lo:wor*ld") == "hel-lo-wor-ld"
    assert _safe_name("\r\n\tstrip ") == "---strip"
    assert _safe_name("") == "unknown"
    assert len(_safe_name("x" * 100)) <= 60


def test_modify_md5_appends_byte():
    data = b"\xff\xd8\xff\xe0rest"
    out = _modify_md5(data)
    assert len(out) == len(data) + 1
    assert out[:-1] == data
    # 追加字节保持 ASCII（0x7F 以内），避免破坏部分解码器
    assert out[-1] & 0x7F == out[-1]


def test_build_image_url():
    assert (
        build_image_url("https://fs.picacomic.com", "x.jpg")
        == "https://fs.picacomic.com/static/x.jpg"
    )
    assert (
        build_image_url("https://fs.picacomic.com", "https://cdn.example.com/a.jpg")
        == "https://cdn.example.com/a.jpg"
    )


def test_pick_image_path():
    assert pick_image_path({"files": ["a.jpg", "b.jpg"], "path": "c.jpg"}) == [
        "a.jpg",
        "b.jpg",
        "c.jpg",
    ]
    assert pick_image_path({"path": "c.jpg"}) == ["c.jpg"]
    assert pick_image_path({}) == []