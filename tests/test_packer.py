"""packer 打包器测试"""

import zipfile

from core.packer import PicaPacker, _detect_image_ext


def test_detect_image_ext(tmp_path):
    jpg = tmp_path / "a.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xe0rest")
    assert _detect_image_ext(jpg) == ".jpg"

    webp = tmp_path / "b.jpg"  # 真实格式 WEBP，扩展名 jpg
    webp.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
    assert _detect_image_ext(webp) == ".webp"

    png = tmp_path / "c.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nrest")
    assert _detect_image_ext(png) == ".png"

    unknown = tmp_path / "d.bin"
    unknown.write_bytes(b"....")
    assert _detect_image_ext(unknown) == ".bin"


def test_pack_zip_no_password(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "1.jpg").write_bytes(b"img1")
    (src / "2.jpg").write_bytes(b"img2")
    out_dir = tmp_path / "out"

    result = PicaPacker("zip").pack(src, "book_第1话", out_dir)

    assert result.success
    assert result.output_path.suffix == ".zip"
    with zipfile.ZipFile(result.output_path) as zf:
        assert sorted(zf.namelist()) == ["1.jpg", "2.jpg"]


def test_pack_unknown_format(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    result = PicaPacker("unknown").pack(src, "x", tmp_path)
    assert not result.success
    assert "不支持" in result.error_message


def test_pack_none_returns_source_dir(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    result = PicaPacker("none").pack(src, "x", tmp_path)
    assert result.success
    assert result.output_path == src


def test_pack_missing_dir(tmp_path):
    result = PicaPacker("zip").pack(tmp_path / "nope", "x", tmp_path)
    assert not result.success
    assert "源目录不存在" in result.error_message