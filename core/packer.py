"""
Pica 打包模块 - 支持 ZIP(可加密)/PDF/长图 打包

移植自 jm_cosmos 的 JMPacker，适配 pica 单章节下载目录结构。
"""

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:
    import pyzipper

    PYZIPPER_AVAILABLE = True
except ImportError:
    PYZIPPER_AVAILABLE = False

try:
    import fitz  # pymupdf

    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False

try:
    from PIL import Image

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# 长图打包参数
_LONG_IMG_WIDTH = 1200  # 统一宽度，所有图片缩放到此宽度后纵向拼接
_LONG_IMG_MAX_STRIP_HEIGHT = 12000  # 单段长图最大高度，超出则分段
_LONG_IMG_MAX_PER_STRIP = 30  # 单段长图最多包含的图片数
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _detect_image_ext(path: Path) -> str:
    """根据文件头嗅探图片真实格式（pica 部分图片是 WEBP 但文件名 .jpg）。

    Returns:
        真实格式后缀（.jpg/.png/.webp/.gif），未知时返回原后缀。
    """
    try:
        with open(path, "rb") as f:
            magic = f.read(12)
    except OSError:
        return path.suffix.lower()
    if magic.startswith(b"\xff\xd8"):
        return ".jpg"
    if magic.startswith(b"\x89PNG"):
        return ".png"
    if magic.startswith(b"RIFF") and magic[8:12] == b"WEBP":
        return ".webp"
    if magic.startswith(b"GIF8"):
        return ".gif"
    return path.suffix.lower()


def _image_to_pdf_bytes(path: Path) -> bytes:
    """图片 → PDF bytes。

    pica 部分图片真实格式是 WEBP 但文件名是 .jpg，fitz 直接打开会失败；
    这里用 Pillow（按文件头识别真实格式）解码 → 内存 PNG → fitz 转 PDF，
    完全绕开扩展名与文件句柄问题。
    """
    import io

    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="PNG")
    img = fitz.open(stream=buf.getvalue(), filetype="png")
    try:
        return img.convert_to_pdf()
    finally:
        img.close()


def _collect_images_sorted(source_dir: Path) -> list[Path]:
    """递归收集图片并按自然顺序排序（01,02,...,10 而非 1,10,2）"""

    def natural_key(path: Path):
        rel = str(path.relative_to(source_dir))
        return [
            (0, int(token)) if token.isdigit() else (1, token.lower())
            for token in re.split(r"(\d+)", rel)
        ]

    files = [
        Path(root) / name
        for root, _dirs, names in os.walk(source_dir)
        for name in names
        if (Path(root) / name).suffix.lower() in _IMAGE_EXTENSIONS
    ]
    files.sort(key=natural_key)
    return files


@dataclass
class PackResult:
    """打包结果"""

    success: bool
    output_path: Path | None
    format: str
    encrypted: bool
    error_message: str | None = None


class PicaPacker:
    """Pica 打包器：zip / pdf / long_img / none"""

    def __init__(self, pack_format: str = "zip", password: str = ""):
        self.pack_format = pack_format.lower()
        self.password = password

    def pack(
        self, source_dir: Path, output_name: str, output_dir: Path | None = None
    ) -> PackResult:
        """打包目录中的图片"""
        if not source_dir.exists():
            return PackResult(
                success=False,
                output_path=None,
                format=self.pack_format,
                encrypted=bool(self.password),
                error_message=f"源目录不存在: {source_dir}",
            )

        if output_dir is None:
            output_dir = source_dir.parent
        output_dir.mkdir(parents=True, exist_ok=True)

        if self.pack_format == "zip":
            return self._pack_zip(source_dir, output_name, output_dir)
        if self.pack_format == "pdf":
            return self._pack_pdf(source_dir, output_name, output_dir)
        if self.pack_format == "long_img":
            return self._pack_long_img(source_dir, output_name, output_dir)
        if self.pack_format == "none":
            return PackResult(
                success=True, output_path=source_dir, format="none", encrypted=False
            )
        return PackResult(
            success=False,
            output_path=None,
            format=self.pack_format,
            encrypted=False,
            error_message=f"不支持的打包格式: {self.pack_format}",
        )

    # ---------- ZIP ----------

    def _pack_zip(
        self, source_dir: Path, output_name: str, output_dir: Path
    ) -> PackResult:
        output_path = output_dir / f"{output_name}.zip"

        if self.password and not PYZIPPER_AVAILABLE:
            return PackResult(
                success=False,
                output_path=None,
                format="zip",
                encrypted=False,
                error_message="已设置打包密码但未安装 pyzipper，无法生成加密 ZIP；"
                "请安装 pyzipper 或清空打包密码",
            )

        try:
            if self.password:
                with pyzipper.AESZipFile(
                    output_path,
                    "w",
                    compression=pyzipper.ZIP_DEFLATED,
                    encryption=pyzipper.WZ_AES,
                ) as zf:
                    zf.setpassword(self.password.encode("utf-8"))
                    for root, _dirs, files in os.walk(source_dir):
                        for file in files:
                            file_path = Path(root) / file
                            zf.write(file_path, file_path.relative_to(source_dir))
            else:
                import zipfile

                with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for root, _dirs, files in os.walk(source_dir):
                        for file in files:
                            file_path = Path(root) / file
                            zf.write(file_path, file_path.relative_to(source_dir))

            return PackResult(
                success=True,
                output_path=output_path,
                format="zip",
                encrypted=bool(self.password),
            )
        except Exception as e:
            return PackResult(
                success=False,
                output_path=None,
                format="zip",
                encrypted=bool(self.password),
                error_message=str(e),
            )

    # ---------- PDF ----------

    def _pack_pdf(
        self, source_dir: Path, output_name: str, output_dir: Path
    ) -> PackResult:
        if not PYMUPDF_AVAILABLE:
            return PackResult(
                success=False,
                output_path=None,
                format="pdf",
                encrypted=False,
                error_message="pymupdf 库未安装，无法创建PDF",
            )

        output_path = output_dir / f"{output_name}.pdf"

        try:
            image_files = _collect_images_sorted(source_dir)
            if not image_files:
                return PackResult(
                    success=False,
                    output_path=None,
                    format="pdf",
                    encrypted=False,
                    error_message="未找到图片文件",
                )

            doc = fitz.open()
            for img_path in image_files:
                try:
                    pdfbytes = _image_to_pdf_bytes(img_path)
                    imgpdf = fitz.open("pdf", pdfbytes)
                    doc.insert_pdf(imgpdf)
                    imgpdf.close()
                except Exception:
                    continue

            if doc.page_count == 0:
                doc.close()
                return PackResult(
                    success=False,
                    output_path=None,
                    format="pdf",
                    encrypted=False,
                    error_message="无法创建PDF页面",
                )

            if self.password:
                doc.save(
                    output_path,
                    encryption=fitz.PDF_ENCRYPT_AES_256,
                    owner_pw=self.password,
                    user_pw=self.password,
                    permissions=fitz.PDF_PERM_ACCESSIBILITY,
                )
            else:
                doc.save(output_path)
            doc.close()

            return PackResult(
                success=True,
                output_path=output_path,
                format="pdf",
                encrypted=bool(self.password),
            )
        except Exception as e:
            return PackResult(
                success=False,
                output_path=None,
                format="pdf",
                encrypted=bool(self.password),
                error_message=str(e),
            )

    # ---------- 长图 ----------

    def _pack_long_img(
        self, source_dir: Path, output_name: str, output_dir: Path
    ) -> PackResult:
        if not PIL_AVAILABLE:
            return PackResult(
                success=False,
                output_path=None,
                format="long_img",
                encrypted=False,
                error_message="Pillow 库未安装，无法生成长图",
            )

        image_files = _collect_images_sorted(source_dir)
        if not image_files:
            return PackResult(
                success=False,
                output_path=None,
                format="long_img",
                encrypted=False,
                error_message="未找到图片文件",
            )

        try:
            strips = self._build_long_strips(image_files)
        except Exception as e:
            return PackResult(
                success=False,
                output_path=None,
                format="long_img",
                encrypted=False,
                error_message=str(e),
            )

        if not strips:
            return PackResult(
                success=False,
                output_path=None,
                format="long_img",
                encrypted=False,
                error_message="无法生成长图",
            )

        try:
            # 单段：直接输出一张长图
            if len(strips) == 1:
                output_path = output_dir / f"{output_name}.png"
                strips[0].save(output_path)
                strips[0].close()
                return PackResult(
                    success=True,
                    output_path=output_path,
                    format="long_img",
                    encrypted=False,
                )

            # 多段：先落地多张 png，再复用 ZIP 打包（支持加密）
            tmp_dir = Path(tempfile.mkdtemp(prefix="pica_longimg_"))
            try:
                for index, strip in enumerate(strips, 1):
                    strip.save(tmp_dir / f"{output_name}_{index:03d}.png")
                    strip.close()
                zip_result = self._pack_zip(
                    tmp_dir, f"{output_name}_长图分段", output_dir
                )
                return zip_result
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception as e:
            return PackResult(
                success=False,
                output_path=None,
                format="long_img",
                encrypted=bool(self.password),
                error_message=str(e),
            )

    def _build_long_strips(self, image_files: list[Path]) -> list:
        """纵向拼接长图，超限自动分段"""
        strips = []
        current_images = []
        current_height = 0

        for img_path in image_files:
            try:
                im = Image.open(img_path)
            except Exception:
                continue
            im = im.convert("RGB")
            if im.width != _LONG_IMG_WIDTH:
                ratio = _LONG_IMG_WIDTH / im.width
                im = im.resize(
                    (_LONG_IMG_WIDTH, max(1, int(im.height * ratio))),
                    Image.LANCZOS,
                )
            if (
                current_images
                and current_height + im.height > _LONG_IMG_MAX_STRIP_HEIGHT
            ) or len(current_images) >= _LONG_IMG_MAX_PER_STRIP:
                strips.append(self._join_images(current_images))
                current_images = []
                current_height = 0
            current_images.append(im)
            current_height += im.height

        if current_images:
            strips.append(self._join_images(current_images))
        return strips

    @staticmethod
    def _join_images(images) -> Image.Image:
        total_height = sum(im.height for im in images)
        canvas = Image.new("RGB", (_LONG_IMG_WIDTH, total_height), (255, 255, 255))
        offset_y = 0
        for im in images:
            canvas.paste(im, (0, offset_y))
            offset_y += im.height
            im.close()
        return canvas

    @staticmethod
    def cleanup(path: Path) -> bool:
        """清理文件或目录"""
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.is_file():
                path.unlink()
            return True
        except Exception:
            return False
