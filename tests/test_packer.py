"""packer 打包器测试"""

import zipfile

import pytest
from PIL import Image

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


def _make_jpeg(path, size=(12, 12)):
    Image.new("RGB", size, (30, 120, 200)).save(path, format="JPEG")


def test_pack_pdf_is_valid_and_leaves_no_tmp_file(tmp_path):
    """PDF 先写临时文件再原子替换：不应残留 .tmp，产物可正常打开"""
    pytest.importorskip("fitz")
    import fitz

    src = tmp_path / "src"
    src.mkdir()
    _make_jpeg(src / "001.jpg")
    _make_jpeg(src / "002.jpg")

    result = PicaPacker("pdf").pack(src, "book", tmp_path / "out")

    assert result.success
    assert result.output_path.suffix == ".pdf"
    assert list((tmp_path / "out").glob("*.tmp")) == []
    with fitz.open(result.output_path) as doc:
        assert doc.page_count == 2


def test_pack_pdf_overwrites_existing_atomically(tmp_path):
    """同名重复打包应整体替换，而不是把旧文件与临时文件混在一起"""
    pytest.importorskip("fitz")
    src = tmp_path / "src"
    src.mkdir()
    _make_jpeg(src / "001.jpg")

    first = PicaPacker("pdf").pack(src, "book", tmp_path)
    _make_jpeg(src / "002.jpg")
    second = PicaPacker("pdf").pack(src, "book", tmp_path)

    assert first.output_path == second.output_path
    assert list(tmp_path.glob("*.tmp")) == []
    assert second.success